from __future__ import annotations

import json
import hashlib
import math
import re
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VM_VERSION = 1
MAX_PROGRAM_BYTES = 192
MAX_INSTRUCTIONS = 64
MAX_STACK = 16
MAX_EXECUTION_COST = 128

OP = {
    "const": 1,
    "x": 2,
    "y": 3,
    "time": 4,
    "pixel": 5,
    "add": 16,
    "sub": 17,
    "mul": 18,
    "div": 19,
    "min": 20,
    "max": 21,
    "pow": 22,
    "sin": 32,
    "cos": 33,
    "abs": 34,
    "fract": 35,
    "clamp": 36,
    "neg": 37,
    "sqrt": 38,
    "hash": 39,
    "floor": 40,
    "mix": 48,
    "smoothstep": 49,
}

INPUTS = {"x", "y", "time", "t", "pixel"}
UNARY = {"sin", "cos", "abs", "fract", "clamp", "neg", "sqrt", "hash", "floor"}
BINARY = {"add", "sub", "mul", "div", "min", "max", "pow"}
TERNARY = {"mix", "smoothstep"}
OPERATION_COST = {
    "pow": 16,
    "sin": 8,
    "cos": 8,
    "sqrt": 4,
    "hash": 3,
}


class UploadedPatternError(ValueError):
    pass


@dataclass(frozen=True)
class CompiledUploadedPattern:
    program_id: int
    bytecode: bytes
    instruction_count: int
    uses_time: bool
    execution_cost: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id & 0xFFFFFFFF,
            "program_tag": self.program_id >> 32,
            "program_label": f"{self.program_id:016x}",
            "vm_version": VM_VERSION,
            "bytecode": self.bytecode.hex(),
            "bytes": len(self.bytecode),
            "instructions": self.instruction_count,
            "execution_cost": self.execution_cost,
            "static": not self.uses_time,
        }


class _Compiler:
    def __init__(self) -> None:
        self.code = bytearray()
        self.instructions = 0
        self.depth = 0
        self.max_depth = 0
        self.uses_time = False
        self.execution_cost = 0

    def emit(self, opcode: int, *, pop: int = 0, push: int = 0, cost: int = 1) -> None:
        if self.depth < pop:
            raise UploadedPatternError("expression stack underflow")
        self.code.append(opcode)
        self.instructions += 1
        self.execution_cost += cost
        self.depth = self.depth - pop + push
        self.max_depth = max(self.max_depth, self.depth)
        if self.instructions > MAX_INSTRUCTIONS:
            raise UploadedPatternError(f"program exceeds {MAX_INSTRUCTIONS} instructions")
        if self.max_depth > MAX_STACK:
            raise UploadedPatternError(f"program exceeds {MAX_STACK} stack values")
        if len(self.code) > MAX_PROGRAM_BYTES:
            raise UploadedPatternError(f"program exceeds {MAX_PROGRAM_BYTES} bytes")
        if self.execution_cost > MAX_EXECUTION_COST:
            raise UploadedPatternError(
                f"program exceeds {MAX_EXECUTION_COST} execution cost units"
            )

    def constant(self, value: Any) -> None:
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise UploadedPatternError("constants must be finite numbers") from error
        if not math.isfinite(number):
            raise UploadedPatternError("constants must be finite numbers")
        try:
            encoded = struct.pack("<f", number)
        except (OverflowError, struct.error) as error:
            raise UploadedPatternError(
                "constants must be finite float32 numbers"
            ) from error
        if not math.isfinite(struct.unpack("<f", encoded)[0]):
            raise UploadedPatternError("constants must be finite float32 numbers")
        self.code.append(OP["const"])
        self.code.extend(encoded)
        self.instructions += 1
        self.execution_cost += 1
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        if (
            self.instructions > MAX_INSTRUCTIONS
            or len(self.code) > MAX_PROGRAM_BYTES
            or self.max_depth > MAX_STACK
            or self.execution_cost > MAX_EXECUTION_COST
        ):
            raise UploadedPatternError("program exceeds the VM budget")

    def expression(self, expression: Any, nesting: int = 0) -> None:
        if nesting > 16:
            raise UploadedPatternError("expression nesting exceeds 16 levels")
        if isinstance(expression, (int, float)) and not isinstance(expression, bool):
            self.constant(expression)
            return
        if isinstance(expression, str):
            name = expression.strip().lower()
            if name not in INPUTS:
                raise UploadedPatternError(f"unknown input {expression!r}")
            if name == "t":
                name = "time"
            self.emit(OP[name], push=1)
            self.uses_time = self.uses_time or name == "time"
            return
        if not isinstance(expression, dict):
            raise UploadedPatternError("expressions must be numbers, inputs, or operation objects")
        operation = str(expression.get("op") or "").strip().lower()
        args = expression.get("args")
        if not isinstance(args, list):
            raise UploadedPatternError(f"{operation or 'operation'} requires an args array")

        # Friendly compiler-only primitives expand into the small firmware VM.
        if operation == "wave":
            if len(args) != 1:
                raise UploadedPatternError("wave requires one argument")
            self.expression(args[0], nesting + 1)
            self.emit(OP["sin"], pop=1, push=1, cost=OPERATION_COST["sin"])
            self.constant(1.0)
            self.emit(OP["add"], pop=2, push=1)
            self.constant(0.5)
            self.emit(OP["mul"], pop=2, push=1)
            return
        if operation == "distance":
            if len(args) != 4:
                raise UploadedPatternError("distance requires x1, y1, x2, y2")
            self.expression({"op": "sub", "args": [args[0], args[2]]}, nesting + 1)
            self.constant(2.0)
            self.emit(OP["pow"], pop=2, push=1, cost=OPERATION_COST["pow"])
            self.expression({"op": "sub", "args": [args[1], args[3]]}, nesting + 1)
            self.constant(2.0)
            self.emit(OP["pow"], pop=2, push=1, cost=OPERATION_COST["pow"])
            self.emit(OP["add"], pop=2, push=1)
            self.emit(OP["sqrt"], pop=1, push=1, cost=OPERATION_COST["sqrt"])
            return

        expected = 1 if operation in UNARY else 2 if operation in BINARY else 3 if operation in TERNARY else 0
        if not expected:
            raise UploadedPatternError(f"unknown operation {operation!r}")
        if len(args) != expected:
            raise UploadedPatternError(f"{operation} requires {expected} argument(s)")
        for arg in args:
            self.expression(arg, nesting + 1)
        self.emit(
            OP[operation],
            pop=expected,
            push=1,
            cost=OPERATION_COST.get(operation, 1),
        )


def compile_uploaded_pattern(document: dict[str, Any]) -> CompiledUploadedPattern:
    if not isinstance(document, dict):
        raise UploadedPatternError("program must be a JSON object")
    compiler = _Compiler()
    outputs = (
        document.get("hue", 0.1),
        document.get("saturation", 1.0),
        document.get("value", 1.0),
        document.get("intensity", 1.0),
    )
    for expression in outputs:
        compiler.expression(expression)
    if compiler.depth != 4:
        raise UploadedPatternError("program must produce hue, saturation, value, and intensity")
    bytecode = bytes(compiler.code)
    prefix = bytes((VM_VERSION, len(bytecode)))
    digest = hashlib.blake2s(prefix + bytecode, digest_size=8).digest()
    program_id = int.from_bytes(digest[:8], "little")
    if program_id == 0:
        raise UploadedPatternError("program produced the reserved zero ID")
    return CompiledUploadedPattern(
        program_id=program_id,
        bytecode=bytecode,
        instruction_count=compiler.instructions,
        uses_time=compiler.uses_time,
        execution_cost=compiler.execution_cost,
    )


def _clamp(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


def _round_float32(value: float) -> float:
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error):
        return math.copysign(math.inf, value)


def _firmware_value(value: float) -> float:
    rounded = _round_float32(value)
    return rounded if math.isfinite(rounded) else 0.0


def _hash(value: float) -> float:
    quantized = math.floor(value * 4096.0) & 0xFFFFFFFF
    h = quantized
    h ^= h >> 16
    h = (h * 0x7FEB352D) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x846CA68B) & 0xFFFFFFFF
    h ^= h >> 16
    return (h & 0x00FFFFFF) / 16777215.0


def run_uploaded_pattern(
    compiled: CompiledUploadedPattern,
    *,
    time_s: float,
    x: float,
    y: float,
    pixel: float = 0.0,
) -> dict[str, float]:
    stack: list[float] = []
    data = compiled.bytecode
    offset = 0
    while offset < len(data):
        opcode = data[offset]
        offset += 1
        if opcode == OP["const"]:
            stack.append(struct.unpack_from("<f", data, offset)[0])
            offset += 4
        elif opcode in (OP["x"], OP["y"], OP["time"], OP["pixel"]):
            stack.append(_firmware_value({OP["x"]: x, OP["y"]: y, OP["time"]: time_s % 4096.0, OP["pixel"]: pixel}[opcode]))
        elif opcode in {OP[name] for name in BINARY}:
            b, a = stack.pop(), stack.pop()
            if opcode == OP["add"]: result = a + b
            elif opcode == OP["sub"]: result = a - b
            elif opcode == OP["mul"]: result = a * b
            elif opcode == OP["div"]: result = 0.0 if abs(b) < 1e-6 else a / b
            elif opcode == OP["min"]: result = min(a, b)
            elif opcode == OP["max"]: result = max(a, b)
            else:
                try:
                    result = 0.0 if a < 0 else math.pow(a, b)
                except (OverflowError, ValueError):
                    result = 0.0
            stack.append(_firmware_value(result))
        elif opcode in {OP[name] for name in UNARY}:
            a = stack.pop()
            if opcode == OP["sin"]:
                result = math.sin(_round_float32(_round_float32(math.tau) * a))
            elif opcode == OP["cos"]:
                result = math.cos(_round_float32(_round_float32(math.tau) * a))
            elif opcode == OP["abs"]: result = abs(a)
            elif opcode == OP["fract"]: result = a - math.floor(a)
            elif opcode == OP["clamp"]: result = _clamp(a)
            elif opcode == OP["neg"]: result = -a
            elif opcode == OP["sqrt"]: result = 0.0 if a <= 0 else math.sqrt(a)
            elif opcode == OP["hash"]: result = _hash(a)
            else: result = math.floor(a)
            stack.append(_firmware_value(result))
        else:
            c, b, a = stack.pop(), stack.pop(), stack.pop()
            if opcode == OP["mix"]:
                delta = _round_float32(b - a)
                product = _round_float32(delta * _clamp(c))
                result = _round_float32(a + product)
            else:
                width = _round_float32(b - a)
                numerator = _round_float32(c - a)
                t = (1.0 if c >= b else 0.0) if abs(width) < 1e-6 else _clamp(_round_float32(numerator / width))
                result = _round_float32(_round_float32(t * t) * _round_float32(3.0 - 2.0 * t))
            stack.append(_firmware_value(result))
    return {
        "hue": stack[0] - math.floor(stack[0]),
        "saturation": _clamp(stack[1]),
        "value": _clamp(stack[2]),
        "intensity": _clamp(stack[3]),
    }


@dataclass
class UploadedPatternStore:
    root: Path = Path(".control_uploaded_patterns")
    _lock: threading.RLock = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.path = self.root / "patterns.json"
        self._lock = threading.RLock()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self._load().values(), key=lambda item: item["name"].lower())

    def get(self, pattern_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._load().get(pattern_id)
            return dict(item) if item else None

    def create(self, name: str, brightness: int, program: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            items = self._load()
            base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "uploaded-pattern"
            pattern_id = base
            suffix = 2
            while pattern_id in items:
                pattern_id = f"{base}-{suffix}"
                suffix += 1
            compiled = compile_uploaded_pattern(program)
            now = time.time()
            item = {
                "id": pattern_id,
                "name": name.strip(),
                "pattern": "Uploaded Pattern",
                "brightness": int(brightness),
                "program": program,
                "compiled": compiled.as_dict(),
                "created_at": now,
                "updated_at": now,
            }
            self._validate_item(item)
            items[pattern_id] = item
            self._save(items)
            return dict(item)

    def delete(self, pattern_id: str) -> bool:
        with self._lock:
            items = self._load()
            if pattern_id not in items:
                return False
            del items[pattern_id]
            self._save(items)
            return True

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise UploadedPatternError("uploaded pattern store is corrupt")
        for item in raw.values():
            self._validate_item(item)
        return raw

    def _validate_item(self, item: Any) -> None:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            raise UploadedPatternError("uploaded pattern id is required")
        if not str(item.get("name") or "").strip():
            raise UploadedPatternError("uploaded pattern name is required")
        brightness = int(item.get("brightness", -1))
        if brightness < 0 or brightness > 192:
            raise UploadedPatternError("brightness must be between 0 and 192")
        compiled = compile_uploaded_pattern(item.get("program"))
        stored = item.get("compiled") or {}
        stored_identity = int(stored.get("program_id", compiled.program_id & 0xFFFFFFFF)) | (
            int(stored.get("program_tag", compiled.program_id >> 32)) << 32
        )
        if stored_identity != compiled.program_id:
            raise UploadedPatternError("stored uploaded pattern bytecode is inconsistent")

    def _save(self, items: dict[str, dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        contents = json.dumps(items, indent=2, sort_keys=True) + "\n"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.root,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(contents)
                temporary = Path(handle.name)
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


DEFAULT_UPLOADED_PROGRAM: dict[str, Any] = {
    "hue": 0.56,
    "saturation": 0.9,
    "value": {
        "op": "mix",
        "args": [
            0.2,
            1.0,
            {
                "op": "wave",
                "args": [
                    {
                        "op": "add",
                        "args": [
                            {"op": "add", "args": ["x", "y"]},
                            {"op": "mul", "args": ["time", 0.12]},
                        ],
                    }
                ],
            },
        ],
    },
    "intensity": 1.0,
}
