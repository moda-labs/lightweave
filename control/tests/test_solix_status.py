from __future__ import annotations

import json
from pathlib import Path

import pytest

from control.solix_status import (
    SolixStatusError,
    SolixStatusStore,
    decode_as220_status,
    parse_solix_tlvs,
)


def _tlv(key: int, value: bytes) -> bytes:
    return bytes((key, len(value))) + value


def _as220_payload(
    *,
    output_w: int = 384,
    input_w: int = 850,
    ac_output_w: int = 300,
    usb_output_w: int = 84,
    ac_input_w: int = 600,
    dc_input_w: int = 250,
    soc_percent: int = 73,
    temperature_c: int = 31,
    include_ac_output: bool = True,
    include_usb_output: bool = True,
) -> bytes:
    a5 = bytearray(4)
    a5[0] = 3
    a5[1] = temperature_c
    a5[3] = soc_percent
    a6 = bytearray(14)
    a6[0] = 3
    a6[1:3] = output_w.to_bytes(2, "little")
    a6[3:5] = ac_input_w.to_bytes(2, "little")
    a6[5:7] = dc_input_w.to_bytes(2, "little")
    a6[9] = soc_percent
    a6[12:14] = input_w.to_bytes(2, "little")
    a7 = bytes((3, 0)) + ac_output_w.to_bytes(2, "little")
    aa = bytes((3, 0)) + usb_output_w.to_bytes(2, "little")
    values = [_tlv(0xA5, bytes(a5)), _tlv(0xA6, bytes(a6))]
    if include_ac_output:
        values.append(_tlv(0xA7, a7))
    if include_usb_output:
        values.append(_tlv(0xAA, aa))
    return b"\x00" + b"".join(values)


def test_decode_as220_c421_power_fields() -> None:
    status = decode_as220_status(_as220_payload())

    assert status == {
        "model": "SOLIX S2000",
        "output_w": 384.0,
        "input_w": 850.0,
        "ac_output_w": 300.0,
        "usb_output_w": 84.0,
        "ac_input_w": 600.0,
        "dc_input_w": 250.0,
        "soc_percent": 73.0,
        "temperature_c": 31.0,
        "plausible": True,
    }


def test_decode_as220_marks_broken_total_relationship_implausible() -> None:
    status = decode_as220_status(_as220_payload(output_w=300))

    assert status["output_w"] == 300.0
    assert status["plausible"] is False


def test_tlv_parser_rejects_truncated_payloads() -> None:
    with pytest.raises(SolixStatusError, match="truncated TLV header"):
        parse_solix_tlvs(bytes.fromhex("a5"))
    with pytest.raises(SolixStatusError, match="truncated TLV value"):
        parse_solix_tlvs(bytes.fromhex("a5040102"))
    with pytest.raises(SolixStatusError, match="missing a5 or a6"):
        decode_as220_status(_tlv(0xA5, bytes(4)))


@pytest.mark.parametrize(
    ("a5", "a6", "message"),
    [
        (bytes(1), bytes(14), "a5 temperature is truncated"),
        (bytes(3), bytes(14), "a5 state of charge is truncated"),
        (bytes(4), bytes(2), "a6 total output is truncated"),
        (bytes(4), bytes(4), "a6 AC input is truncated"),
        (bytes(4), bytes(6), "a6 DC input is truncated"),
        (bytes(4), bytes(13), "a6 total input is truncated"),
    ],
)
def test_decode_as220_rejects_truncated_required_fields(
    a5: bytes,
    a6: bytes,
    message: str,
) -> None:
    with pytest.raises(SolixStatusError, match=message):
        decode_as220_status(_tlv(0xA5, a5) + _tlv(0xA6, a6))


def test_decode_as220_accepts_absent_or_truncated_optional_output_fields() -> None:
    absent = decode_as220_status(
        _as220_payload(include_ac_output=False, include_usb_output=False)
    )
    truncated = decode_as220_status(
        _as220_payload(include_usb_output=False) + _tlv(0xAA, bytes(3))
    )

    assert absent["ac_output_w"] is None
    assert absent["usb_output_w"] is None
    assert absent["plausible"] is True
    assert truncated["ac_output_w"] == 300
    assert truncated["usb_output_w"] is None
    assert truncated["plausible"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"output_w": 5_001, "ac_output_w": 4_917, "usb_output_w": 84},
        {"input_w": 5_001, "ac_input_w": 4_751, "dc_input_w": 250},
        {"soc_percent": 101},
        {"temperature_c": 126},
        {"input_w": 861},
    ],
)
def test_decode_as220_marks_each_range_and_total_violation_implausible(
    overrides: dict[str, int],
) -> None:
    assert decode_as220_status(_as220_payload(**overrides))["plausible"] is False


def test_decode_as220_allows_whole_watt_reporting_jitter() -> None:
    reading = decode_as220_status(_as220_payload(output_w=374, input_w=840))

    assert reading["plausible"] is True


def test_status_store_reports_fresh_and_stale_readings(tmp_path: Path) -> None:
    store = SolixStatusStore(tmp_path / "solix.json", stale_after_s=45)
    store.write_reading(
        decode_as220_status(_as220_payload()),
        address="00:7F:1D:55:9B:B2",
        updated_at=1_000,
    )

    fresh = store.load(now=1_020)
    assert fresh["configured"] is True
    assert fresh["connected"] is True
    assert fresh["stale"] is False
    assert fresh["age_s"] == 20
    assert fresh["output_w"] == 384

    stale = store.load(now=1_046)
    assert stale["connected"] is False
    assert stale["stale"] is True
    assert stale["output_w"] == 384


def test_status_store_preserves_last_reading_across_connection_error(tmp_path: Path) -> None:
    store = SolixStatusStore(tmp_path / "solix.json")
    store.write_reading(
        decode_as220_status(_as220_payload()),
        address="device",
        updated_at=1_000,
    )
    store.write_error("Bluetooth disconnected", address="device", attempted_at=1_010)

    status = store.load(now=1_011)
    assert status["connected"] is False
    assert status["output_w"] == 384
    assert status["updated_at"] == 1_000
    assert status["last_attempt_at"] == 1_010
    assert status["error"] == "Bluetooth disconnected"


def test_status_store_fails_closed_for_missing_or_invalid_data(tmp_path: Path) -> None:
    disabled = SolixStatusStore(None).load(now=1_000)
    assert disabled["configured"] is False

    path = tmp_path / "solix.json"
    missing = SolixStatusStore(path).load(now=1_000)
    assert missing["configured"] is True
    assert missing["connected"] is False

    path.write_text(json.dumps({"schema": 99}), encoding="utf-8")
    invalid = SolixStatusStore(path).load(now=1_000)
    assert invalid["connected"] is False
    assert invalid["error"] == "unsupported status schema"


@pytest.mark.parametrize("contents", ("not-json", "[]"))
def test_status_store_fails_closed_for_malformed_or_non_object_json(
    tmp_path: Path,
    contents: str,
) -> None:
    path = tmp_path / "solix.json"
    path.write_text(contents, encoding="utf-8")

    status = SolixStatusStore(path).load(now=1_000)

    assert status["configured"] is True
    assert status["connected"] is False
    assert status["output_w"] is None


def test_status_store_normalizes_nonfinite_fields_and_future_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "solix.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "connected": True,
                "updated_at": 1_100,
                "last_attempt_at": float("inf"),
                "output_w": float("nan"),
                "input_w": True,
                "soc_percent": "73",
                "plausible": "yes",
            }
        ),
        encoding="utf-8",
    )

    status = SolixStatusStore(path).load(now=1_000)

    assert status["connected"] is True
    assert status["stale"] is False
    assert status["age_s"] == 0
    assert status["last_attempt_at"] is None
    assert status["output_w"] is None
    assert status["input_w"] is None
    assert status["soc_percent"] is None
    assert status["plausible"] is None


def test_status_store_records_an_error_before_any_reading(tmp_path: Path) -> None:
    store = SolixStatusStore(tmp_path / "solix.json")

    store.write_error("not found", address="device", attempted_at=1_000)

    status = store.load(now=1_001)
    assert status["connected"] is False
    assert status["updated_at"] is None
    assert status["last_attempt_at"] == 1_000
    assert status["error"] == "not found"
