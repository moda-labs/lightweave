from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any


SOLIX_STATUS_SCHEMA = 1
SOLIX_STATUS_STALE_S = 180.0
SOLIX_MODEL = "SOLIX S2000"

_READING_FIELDS = (
    "output_w",
    "input_w",
    "ac_output_w",
    "usb_output_w",
    "ac_input_w",
    "dc_input_w",
    "soc_percent",
    "temperature_c",
)


class SolixStatusError(ValueError):
    pass


def parse_solix_tlvs(payload: bytes) -> dict[int, bytes]:
    """Parse an Anker ZX payload into raw TLV values.

    Values intentionally retain the protocol's leading type byte. The AS220
    field offsets published for command c421 are relative to that raw value.
    """
    offset = 1 if payload.startswith(b"\x00") else 0
    values: dict[int, bytes] = {}
    while offset < len(payload):
        if offset + 2 > len(payload):
            raise SolixStatusError("truncated TLV header")
        key = payload[offset]
        length = payload[offset + 1]
        offset += 2
        end = offset + length
        if end > len(payload):
            raise SolixStatusError(f"truncated TLV value for {key:02x}")
        values[key] = payload[offset:end]
        offset = end
    return values


def decode_as220_status(payload: bytes) -> dict[str, Any]:
    """Decode the read-only power fields from an AS220 0421/c421 status payload."""
    values = parse_solix_tlvs(payload)
    a5 = values.get(0xA5)
    a6 = values.get(0xA6)
    if a5 is None or a6 is None:
        raise SolixStatusError("AS220 c421 payload is missing a5 or a6")

    output_w = _u16(a6, 1, "a6 total output")
    ac_input_w = _u16(a6, 3, "a6 AC input")
    dc_input_w = _u16(a6, 5, "a6 DC input")
    input_w = _u16(a6, 12, "a6 total input")
    temperature_c = _u8(a5, 1, "a5 temperature")
    soc_percent = _u8(a5, 3, "a5 state of charge")

    ac_output_w = _optional_u16(values.get(0xA7), 2)
    usb_output_w = _optional_u16(values.get(0xAA), 2)
    relationships: list[bool] = []
    if ac_output_w is not None and usb_output_w is not None:
        # The public AS220 map does not identify every possible DC output, so
        # known sub-outputs may be below the total but must not exceed it by
        # more than normal whole-watt reporting jitter.
        relationships.append(ac_output_w + usb_output_w <= output_w + 10)
    relationships.append(abs(input_w - (ac_input_w + dc_input_w)) <= 10)
    plausible = (
        0 <= output_w <= 5_000
        and 0 <= input_w <= 5_000
        and 0 <= soc_percent <= 100
        and -40 <= temperature_c <= 125
        and all(relationships)
    )

    return {
        "model": SOLIX_MODEL,
        "output_w": float(output_w),
        "input_w": float(input_w),
        "ac_output_w": _optional_float(ac_output_w),
        "usb_output_w": _optional_float(usb_output_w),
        "ac_input_w": float(ac_input_w),
        "dc_input_w": float(dc_input_w),
        "soc_percent": float(soc_percent),
        "temperature_c": float(temperature_c),
        "plausible": plausible,
    }


class SolixStatusStore:
    """Atomic handoff between the BLE probe and the web control process."""

    def __init__(
        self,
        path: Path | str | None,
        *,
        stale_after_s: float = SOLIX_STATUS_STALE_S,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.stale_after_s = max(1.0, float(stale_after_s))

    def write_reading(
        self,
        reading: dict[str, Any],
        *,
        address: str,
        updated_at: float | None = None,
    ) -> None:
        timestamp = time.time() if updated_at is None else float(updated_at)
        status = {
            "schema": SOLIX_STATUS_SCHEMA,
            "model": SOLIX_MODEL,
            "address": str(address),
            "connected": True,
            "updated_at": timestamp,
            "last_attempt_at": timestamp,
            "error": None,
            **{field: reading.get(field) for field in _READING_FIELDS},
            "plausible": reading.get("plausible"),
        }
        self._write(status)

    def write_error(
        self,
        error: str,
        *,
        address: str,
        attempted_at: float | None = None,
    ) -> None:
        status = self._read_raw() or {
            "schema": SOLIX_STATUS_SCHEMA,
            "model": SOLIX_MODEL,
            "address": str(address),
        }
        status.update(
            {
                "schema": SOLIX_STATUS_SCHEMA,
                "model": SOLIX_MODEL,
                "address": str(address),
                "connected": False,
                "last_attempt_at": time.time() if attempted_at is None else float(attempted_at),
                "error": str(error),
            }
        )
        self._write(status)

    def load(self, *, now: float | None = None) -> dict[str, Any]:
        if self.path is None:
            return _empty_status(configured=False)
        raw = self._read_raw()
        if raw is None:
            return _empty_status(configured=True)
        if raw.get("schema") != SOLIX_STATUS_SCHEMA:
            return {**_empty_status(configured=True), "error": "unsupported status schema"}

        current_time = time.time() if now is None else float(now)
        updated_at = _finite_number(raw.get("updated_at"))
        age_s = max(0.0, current_time - updated_at) if updated_at is not None else None
        stale = age_s is None or age_s > self.stale_after_s
        status = {
            **_empty_status(configured=True),
            "model": str(raw.get("model") or SOLIX_MODEL),
            "address": str(raw.get("address") or ""),
            "connected": raw.get("connected") is True and not stale,
            "stale": stale,
            "updated_at": updated_at,
            "age_s": age_s,
            "last_attempt_at": _finite_number(raw.get("last_attempt_at")),
            "error": str(raw.get("error")) if raw.get("error") else None,
            "plausible": raw.get("plausible") if isinstance(raw.get("plausible"), bool) else None,
        }
        for field in _READING_FIELDS:
            status[field] = _finite_number(raw.get(field))
        return status

    def _read_raw(self) -> dict[str, Any] | None:
        if self.path is None or not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def _write(self, status: dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(status, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _empty_status(*, configured: bool) -> dict[str, Any]:
    return {
        "configured": configured,
        "model": SOLIX_MODEL,
        "address": "",
        "connected": False,
        "stale": True,
        "updated_at": None,
        "age_s": None,
        "last_attempt_at": None,
        "error": None,
        "plausible": None,
        **{field: None for field in _READING_FIELDS},
    }


def _u8(value: bytes, offset: int, label: str) -> int:
    if offset >= len(value):
        raise SolixStatusError(f"{label} is truncated")
    return value[offset]


def _u16(value: bytes, offset: int, label: str) -> int:
    if offset + 2 > len(value):
        raise SolixStatusError(f"{label} is truncated")
    return int.from_bytes(value[offset : offset + 2], "little")


def _optional_u16(value: bytes | None, offset: int) -> int | None:
    if value is None or offset + 2 > len(value):
        return None
    return int.from_bytes(value[offset : offset + 2], "little")


def _optional_float(value: int | None) -> float | None:
    return float(value) if value is not None else None


def _finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
