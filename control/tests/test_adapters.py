from __future__ import annotations

import json

import pytest

from control.adapters import JsonLineSerialConductor, SerialProtocolError
from control.serial_transport import SerialTransportError


class FakeTransport:
    def __init__(self, replies: list[str | None] | None = None) -> None:
        self.replies = list(replies or [])
        self.writes: list[str] = []

    def write_line(self, line: str) -> None:
        self.writes.append(line)

    def read_line(self, timeout_s: float) -> str | None:
        if not self.replies:
            return None
        return self.replies.pop(0)


def test_snapshot_sends_state_command_and_skips_human_noise() -> None:
    state = {"conductor": {"connected": True}, "lanterns": [], "pattern": {"pattern": "Glow"}}
    transport = FakeTransport([
        "boot diag line",
        "{not json",
        json.dumps({"id": 99, "ok": True, "state": {"ignored": True}}),
        json.dumps({"id": 1, "ok": True, "state": state}),
    ])
    conductor = JsonLineSerialConductor(transport)

    assert conductor.snapshot() == state
    assert json.loads(transport.writes[0]) == {"id": 1, "cmd": "state"}


def test_assign_maps_to_json_command() -> None:
    transport = FakeTransport([json.dumps({"id": 1, "ok": True, "message": "assigned"})])
    conductor = JsonLineSerialConductor(transport)

    ack = conductor.assign("AA:BB:CC:DD:EE:FF", 0.25, 0.75)

    assert ack == {"ok": True, "message": "assigned"}
    assert json.loads(transport.writes[0]) == {
        "id": 1,
        "cmd": "assign",
        "mac": "AA:BB:CC:DD:EE:FF",
        "x": 0.25,
        "y": 0.75,
    }


def test_reserve_id_maps_to_json_command_and_returns_identity() -> None:
    transport = FakeTransport([
        json.dumps({
            "id": 1,
            "ok": True,
            "message": "permanent ID reserved",
            "node_id": 54,
            "created": True,
        })
    ])
    conductor = JsonLineSerialConductor(transport)

    ack = conductor.reserve_id("AA:BB:CC:DD:EE:FF", 54)

    assert ack["node_id"] == 54
    assert ack["created"] is True
    assert json.loads(transport.writes[0]) == {
        "id": 1,
        "cmd": "reserve_id",
        "mac": "AA:BB:CC:DD:EE:FF",
        "reported_id": 54,
    }


def test_group_assignment_maps_to_json_command() -> None:
    transport = FakeTransport([json.dumps({"id": 1, "ok": True, "message": "group changed"})])
    conductor = JsonLineSerialConductor(transport)

    conductor.assign_group("AA:BB:CC:DD:EE:FF", 3)

    assert json.loads(transport.writes[0]) == {
        "id": 1,
        "cmd": "group",
        "mac": "AA:BB:CC:DD:EE:FF",
        "group_id": 3,
    }


def test_led_count_assignment_maps_to_json_command() -> None:
    transport = FakeTransport([json.dumps({"id": 1, "ok": True, "message": "led count changed"})])
    conductor = JsonLineSerialConductor(transport)

    conductor.assign_led_count("AA:BB:CC:DD:EE:FF", 64)

    assert json.loads(transport.writes[0]) == {
        "id": 1,
        "cmd": "led_count",
        "mac": "AA:BB:CC:DD:EE:FF",
        "led_count": 64,
    }


def test_pattern_command_includes_brightness_and_params() -> None:
    transport = FakeTransport([json.dumps({"id": 1, "ok": True, "message": "pattern changed to Sweep"})])
    conductor = JsonLineSerialConductor(transport)

    conductor.update_pattern("Sweep", 64, {"period": 8000})

    assert json.loads(transport.writes[0]) == {
        "id": 1,
        "cmd": "pattern",
        "pattern": "Sweep",
        "brightness": 64,
        "params": {"period": 8000},
    }


def test_pattern_command_can_target_one_group() -> None:
    transport = FakeTransport([json.dumps({"id": 1, "ok": True})])
    conductor = JsonLineSerialConductor(transport)

    conductor.update_pattern("Sweep", 64, {"period": 8000}, group_id=2)

    assert json.loads(transport.writes[0])["group_id"] == 2


def test_restore_blackout_maps_to_json_command() -> None:
    transport = FakeTransport([json.dumps({"id": 1, "ok": True, "message": "blackout restored"})])
    conductor = JsonLineSerialConductor(transport)

    ack = conductor.restore_blackout()

    assert ack == {"ok": True, "message": "blackout restored"}
    assert json.loads(transport.writes[0]) == {"id": 1, "cmd": "restore_blackout"}


def test_legacy_glow_params_get_explicit_full_value_and_optional_value_is_packed() -> None:
    transport = FakeTransport([
        json.dumps({"id": 1, "ok": True}),
        json.dumps({"id": 2, "ok": True}),
    ])
    conductor = JsonLineSerialConductor(transport)

    conductor.update_pattern("Glow", 48, {"hue": 32, "saturation": 100})
    conductor.update_pattern("Pulse", 48, {"hue": 0, "saturation": 0, "value": 128})

    first = json.loads(transport.writes[0])["params"]
    second = json.loads(transport.writes[1])["params"]
    assert first == {"hue": 32, "saturation": 100, "p2": 0x80FF}
    assert second == {"hue": 0, "saturation": 0, "p2": 0x8080}


def test_ota_mode_maps_to_json_command() -> None:
    transport = FakeTransport([json.dumps({"id": 1, "ok": True, "message": "ota maintenance mode started"})])
    conductor = JsonLineSerialConductor(transport)

    ack = conductor.set_ota_mode(True)

    assert ack == {"ok": True, "message": "ota maintenance mode started"}
    assert json.loads(transport.writes[0]) == {
        "id": 1,
        "cmd": "ota_mode",
        "enabled": True,
    }


def test_ota_write_commands_map_to_json() -> None:
    transport = FakeTransport([
        json.dumps({"id": 1, "ok": True, "message": "ota write started"}),
        json.dumps({"id": 2, "ok": True, "message": "ota chunk written"}),
        json.dumps({"id": 3, "ok": True, "active": True, "size": 4, "written": 4, "crc32": 0x12345678}),
        json.dumps({"id": 4, "ok": True, "message": "ota install complete; rebooting"}),
    ])
    conductor = JsonLineSerialConductor(transport)

    conductor.ota_begin(4, 0x12345678)
    conductor.ota_chunk(0, b"\xe9\x00\x10\xff")
    conductor.ota_progress()
    conductor.ota_end()

    assert json.loads(transport.writes[0]) == {
        "id": 1,
        "cmd": "ota_begin",
        "size": 4,
        "crc32": 0x12345678,
    }
    assert json.loads(transport.writes[1]) == {
        "id": 2,
        "cmd": "ota_chunk",
        "offset": 0,
        "data": "e90010ff",
    }
    assert json.loads(transport.writes[2]) == {"id": 3, "cmd": "ota_progress"}
    assert json.loads(transport.writes[3]) == {"id": 4, "cmd": "ota_end"}


def test_ota_repair_probe_restart_and_activation_commands_map_to_json() -> None:
    transport = FakeTransport([
        json.dumps({"id": 1, "ok": True}),
        json.dumps({"id": 2, "ok": True}),
        json.dumps({"id": 3, "ok": True}),
        json.dumps({"id": 4, "ok": True}),
        json.dumps({"id": 5, "ok": True}),
    ])
    conductor = JsonLineSerialConductor(transport)
    mac = "01:02:03:04:05:06"

    conductor.ota_repair(mac, 128, b"\xe9\x00")
    conductor.ota_restart(mac)
    conductor.ota_probe()
    conductor.ota_activate(mac)
    conductor.ota_activate()

    assert json.loads(transport.writes[0]) == {
        "id": 1, "cmd": "ota_repair", "mac": mac,
        "offset": 128, "data": "e900",
    }
    assert json.loads(transport.writes[1]) == {"id": 2, "cmd": "ota_restart", "mac": mac}
    assert json.loads(transport.writes[2]) == {"id": 3, "cmd": "ota_probe"}
    assert json.loads(transport.writes[3]) == {"id": 4, "cmd": "ota_activate", "mac": mac}
    assert json.loads(transport.writes[4]) == {"id": 5, "cmd": "ota_activate", "conductor": True}


def test_error_ack_returns_adapter_error() -> None:
    transport = FakeTransport([
        json.dumps({
            "id": 1,
            "ok": False,
            "error": "ota performers did not complete",
            "nodes": [{"mac": "AA", "phase": "writing"}],
        })
    ])
    conductor = JsonLineSerialConductor(transport)

    assert conductor.identify("00:00:00:00:00:00") == {
        "ok": False,
        "error": "ota performers did not complete",
        "nodes": [{"mac": "AA", "phase": "writing"}],
    }


def test_timeout_raises_protocol_error() -> None:
    conductor = JsonLineSerialConductor(FakeTransport([]), timeout_s=0.01)

    with pytest.raises(SerialProtocolError, match="timeout waiting for state ack"):
        conductor.snapshot()


def test_transport_write_failure_raises_protocol_error() -> None:
    class FailingTransport(FakeTransport):
        def write_line(self, line: str) -> None:
            raise SerialTransportError("serial reconnect failed")

    conductor = JsonLineSerialConductor(FailingTransport())

    with pytest.raises(SerialProtocolError, match="serial reconnect failed"):
        conductor.snapshot()


def test_transport_read_failure_raises_protocol_error() -> None:
    class FailingTransport(FakeTransport):
        def read_line(self, timeout_s: float) -> str | None:
            raise SerialTransportError("serial read failed")

    conductor = JsonLineSerialConductor(FailingTransport())

    with pytest.raises(SerialProtocolError, match="serial read failed"):
        conductor.snapshot()


def test_missing_state_object_raises_protocol_error() -> None:
    conductor = JsonLineSerialConductor(FakeTransport([json.dumps({"id": 1, "ok": True})]))

    with pytest.raises(SerialProtocolError, match="missing state object"):
        conductor.snapshot()
