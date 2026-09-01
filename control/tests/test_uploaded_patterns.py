from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from control.uploaded_patterns import (
    DEFAULT_UPLOADED_PROGRAM,
    MAX_EXECUTION_COST,
    MAX_PROGRAM_BYTES,
    UploadedPatternError,
    UploadedPatternStore,
    compile_uploaded_pattern,
    run_uploaded_pattern,
)


def test_compiler_produces_bounded_deterministic_program() -> None:
    first = compile_uploaded_pattern(DEFAULT_UPLOADED_PROGRAM)
    second = compile_uploaded_pattern(json.loads(json.dumps(DEFAULT_UPLOADED_PROGRAM)))

    assert first == second
    assert 0 < len(first.bytecode) <= MAX_PROGRAM_BYTES
    assert first.instruction_count <= 64
    assert first.uses_time is True
    assert first.program_id != 0


def test_runtime_wave_changes_with_time_and_stays_in_output_bounds() -> None:
    compiled = compile_uploaded_pattern(DEFAULT_UPLOADED_PROGRAM)
    first = run_uploaded_pattern(compiled, time_s=0, x=0.2, y=0.4)
    later = run_uploaded_pattern(compiled, time_s=1, x=0.2, y=0.4)

    assert first["value"] != later["value"]
    for frame in (first, later):
        assert all(0 <= frame[key] <= 1 for key in ("hue", "saturation", "value", "intensity"))


def test_compiler_identity_matches_firmware_blake2s_golden() -> None:
    program = {
        "hue": 0.55,
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
                            "args": ["x", {"op": "mul", "args": ["time", 0.25]}],
                        }
                    ],
                },
            ],
        },
        "intensity": 1.0,
    }

    assert compile_uploaded_pattern(program).program_id == 0x9B49FDB41BC0A22D


def test_compiler_rejects_unbounded_or_unknown_programs() -> None:
    with pytest.raises(UploadedPatternError, match="unknown operation"):
        compile_uploaded_pattern({"value": {"op": "jump", "args": [1]}})

    expression: object = 1.0
    for _ in range(80):
        expression = {"op": "add", "args": [expression, 1.0]}
    with pytest.raises(UploadedPatternError):
        compile_uploaded_pattern({"value": expression})

    expensive: object = "x"
    for _ in range(16):
        expensive = {"op": "sin", "args": [expensive]}
    with pytest.raises(UploadedPatternError, match="execution cost"):
        compile_uploaded_pattern({"hue": expensive})

    assert MAX_EXECUTION_COST > 0


def test_compiler_rejects_numbers_outside_firmware_float32_range() -> None:
    with pytest.raises(UploadedPatternError, match="finite float32"):
        compile_uploaded_pattern({"hue": 1e300})


def test_preview_sanitizes_float32_overflow_like_firmware() -> None:
    huge = {"op": "pow", "args": [10, 308]}
    program = {
        "hue": {
            "op": "fract",
            "args": [{"op": "mix", "args": [huge, {"op": "neg", "args": [huge]}, 1]}],
        }
    }

    output = run_uploaded_pattern(
        compile_uploaded_pattern(program), time_s=0, x=0, y=0
    )

    assert output["hue"] == 0


def test_preview_matches_firmware_float32_trig_argument_reduction() -> None:
    program = {
        "hue": {"op": "sin", "args": [1_000_000]},
        "saturation": {"op": "cos", "args": [1_000_000]},
        "value": {"op": "wave", "args": [1_000_000]},
        "intensity": 1,
    }
    compiled = compile_uploaded_pattern(program)
    output = run_uploaded_pattern(compiled, time_s=0, x=0, y=0)

    assert compiled.program_id == 0xBD154A269010422A
    assert output["hue"] == pytest.approx(0.1916278, abs=1e-6)
    assert output["saturation"] == pytest.approx(0.9814677, abs=1e-6)
    assert output["value"] == pytest.approx(0.5958139, abs=1e-6)


def test_program_identity_matches_firmware_at_blake2s_block_boundary() -> None:
    def additions(count: int) -> dict:
        expression: object = 0.1
        for _ in range(count):
            expression = {"op": "add", "args": [expression, "x"]}
        return expression  # type: ignore[return-value]

    base = {
        "hue": additions(10),
        "saturation": additions(11),
        "value": 1,
        "intensity": 1,
    }
    exactly_one_block = compile_uploaded_pattern(base)
    crosses_block = compile_uploaded_pattern({
        **base,
        "hue": {"op": "abs", "args": [base["hue"]]},
    })

    assert len(exactly_one_block.bytecode) + 2 == 64
    assert exactly_one_block.program_id == 0xD6F10D2541758792
    assert len(crosses_block.bytecode) + 2 == 65
    assert crosses_block.program_id == 0x7CA2B0C24FBECD73


def test_store_persists_source_and_compiled_identity(tmp_path) -> None:
    store = UploadedPatternStore(tmp_path)
    created = store.create("Blue diagonal", 56, DEFAULT_UPLOADED_PROGRAM)
    loaded = UploadedPatternStore(tmp_path).get(created["id"])

    assert loaded is not None
    assert loaded["compiled"]["program_id"] == created["compiled"]["program_id"]
    assert loaded["compiled"]["program_tag"] == created["compiled"]["program_tag"]
    assert loaded["compiled"]["static"] is False
    assert store.delete(created["id"]) is True
    assert store.list() == []


def test_store_serializes_concurrent_creates_without_corruption(tmp_path) -> None:
    store = UploadedPatternStore(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        created = list(executor.map(
            lambda index: store.create(
                f"Concurrent {index}", 48, DEFAULT_UPLOADED_PROGRAM
            ),
            range(24),
        ))

    loaded = UploadedPatternStore(tmp_path).list()
    assert len(created) == 24
    assert len(loaded) == 24
    assert {item["id"] for item in loaded} == {item["id"] for item in created}
