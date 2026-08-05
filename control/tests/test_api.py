import json
import hashlib
import fcntl
from pathlib import Path
import subprocess
import threading
import time
import zlib

from fastapi.testclient import TestClient
import pytest

import control.app as app_module
from control.adapters import SerialProtocolError
from control.app import create_app
from control.group_store import GroupStore
from control.mock_conductor import Lantern, MockConductor
from control.ota_store import OtaArtifactStore
from control.pattern_store import PatternStore
from control.preview import _fire_flicker_sample


@pytest.fixture
def managed_client():
    clients = []

    def create(app):
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield create

    for client in reversed(clients):
        client.__exit__(None, None, None)


def wait_for_ota_terminal(client: TestClient, timeout_s: float = 20) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        install = client.get("/api/operations/ota-install").json()["install"]
        if install.get("running") is not True:
            return install
        time.sleep(0.01)
    raise AssertionError("OTA install did not reach a terminal state")


def deployment_record(tmp_path: Path, data: bytes = b"gitops firmware") -> dict:
    firmware_path = tmp_path / "releases" / "v0.3.0" / "field.bin"
    firmware_path.parent.mkdir(parents=True)
    firmware_path.write_bytes(data)
    return {
        "schema_version": 1,
        "deployed_at": "2026-08-01T18:30:00Z",
        "previous_commit": "b" * 40,
        "backup": "/var/backups/lightweave/pre-upgrade.tgz",
        "firmware_local_path": str(firmware_path),
        "manifest": {
            "schema_version": 1,
            "release": "v0.3.0",
            "version": "0.3.0",
            "repository": "https://github.com/underminedsk/lightweave.git",
            "ref": "refs/tags/v0.3.0",
            "commit": "a" * 40,
            "published_at": "2026-08-01T18:00:00Z",
            "notes": {
                "version": "0.3.0",
                "date": "2026-08-01",
                "title": "Release title",
                "control_changes": ["Control change"],
                "firmware_changes": ["Firmware change"],
            },
            "firmware": {
                "filename": "lightweave-field-v0.3.0.bin",
                "url": "https://github.com/underminedsk/lightweave/releases/download/v0.3.0/lightweave-field-v0.3.0.bin",
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "crc32": zlib.crc32(data) & 0xFFFFFFFF,
            },
            "serial_flash": {
                "filename": "lightweave-serial-flash-v0.3.0.zip",
                "url": "https://github.com/underminedsk/lightweave/releases/download/v0.3.0/lightweave-serial-flash-v0.3.0.zip",
                "sha256": hashlib.sha256(b"serial bundle").hexdigest(),
                "size": len(b"serial bundle"),
            },
        },
    }


def test_release_api_separates_control_and_field_firmware(managed_client, tmp_path: Path) -> None:
    client = managed_client(create_app(MockConductor()))

    response = client.get("/api/releases")

    assert response.status_code == 200
    payload = response.json()
    assert payload["control"]["version"] == (Path(__file__).parents[2] / "VERSION").read_text().strip()
    assert payload["control"]["release"]["control_changes"]
    assert payload["firmware"]["version"] == "0.3.0"
    assert payload["firmware"]["release"]["firmware_changes"]
    assert payload["firmware"]["expected"] == 9
    assert payload["history"][0]["version"] == payload["control"]["version"]


def test_release_api_reuses_latest_state_snapshot(managed_client) -> None:
    class CountingConductor(MockConductor):
        def __init__(self):
            super().__init__()
            self.snapshot_calls = 0

        def snapshot(self):
            self.snapshot_calls += 1
            return super().snapshot()

    conductor = CountingConductor()
    client = managed_client(create_app(conductor))

    assert client.get("/api/state").status_code == 200
    assert client.get("/api/releases").status_code == 200
    assert conductor.snapshot_calls == 1

    client.app.state.latest_snapshot_at = 0
    assert client.get("/api/releases").status_code == 200
    assert conductor.snapshot_calls == 2


def test_gitops_deployment_record_stages_verified_firmware_on_startup(
    managed_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = deployment_record(tmp_path)
    record_path = tmp_path / "deployments" / "current.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setenv("CONTROL_DEPLOYMENT_RECORD", str(record_path))
    store = OtaArtifactStore(tmp_path / "ota")

    client = managed_client(create_app(MockConductor(), ota_store=store))
    release = client.get("/api/releases").json()

    assert store.current()["sha256"] == record["manifest"]["firmware"]["sha256"]
    assert release["control"]["desired_commit"] == "a" * 40
    assert release["firmware"]["desired"]["filename"] == "lightweave-field-v0.3.0.bin"


class DownConductor(MockConductor):
    def snapshot(self) -> dict:
        raise SerialProtocolError("timeout waiting for state ack")


class BlockingOtaConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.ota_started = threading.Event()
        self.ota_release = threading.Event()

    def ota_begin(self, size: int, crc32: int) -> dict:
        self.ota_started.set()
        if not self.ota_release.wait(timeout=10):
            raise RuntimeError("test OTA release timed out")
        return super().ota_begin(size, crc32)


class BlockingAssignmentOtaConductor(BlockingOtaConductor):
    def __init__(self) -> None:
        super().__init__()
        self.first_assign_started = threading.Event()
        self.assign_release = threading.Event()
        self.assign_order = []

    def assign(self, mac: str, x: float, y: float) -> dict:
        self.assign_order.append(mac)
        if len(self.assign_order) == 1:
            self.first_assign_started.set()
            if not self.assign_release.wait(timeout=5):
                raise RuntimeError("test assignment release timed out")
        return super().assign(mac, x, y)


class BlockingCalibrationOtaConductor(BlockingOtaConductor):
    def __init__(self) -> None:
        super().__init__()
        self.block_calibration_snapshot = False
        self.calibration_snapshot_started = threading.Event()
        self.calibration_snapshot_release = threading.Event()
        self.calibration_pattern_updated = threading.Event()

    def snapshot(self) -> dict:
        if (
            self.block_calibration_snapshot
            and not self.calibration_snapshot_started.is_set()
        ):
            self.calibration_snapshot_started.set()
            if not self.calibration_snapshot_release.wait(timeout=5):
                raise RuntimeError("test calibration snapshot release timed out")
        return super().snapshot()

    def update_pattern(
        self, pattern: str, brightness: int, params: dict
    ) -> dict:
        if pattern == "Calibration":
            self.calibration_pattern_updated.set()
        return super().update_pattern(pattern, brightness, params)


class ExplodingOtaConductor(MockConductor):
    def ota_begin(self, _size: int, _crc32: int) -> dict:
        raise RuntimeError("unexpected worker failure")


class RejectingPatternConductor(MockConductor):
    def update_pattern(self, pattern: str, brightness: int, params: dict) -> dict:
        return {"ok": False, "error": "bad pattern"}


class RejectingNodeConfigurationConductor(MockConductor):
    def assign_group(self, mac: str, group_id: int) -> dict:
        return {"ok": False, "error": "group rejected"}

    def assign_led_count(self, mac: str, led_count: int) -> dict:
        return {"ok": False, "error": "LED profile rejected"}


class FailingGroupSerialConductor(MockConductor):
    def assign_group(self, mac: str, group_id: int) -> dict:
        raise SerialProtocolError("timeout waiting for group ack")


class DroppingOtaChunkConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.dropped = False

    def ota_chunk(self, offset: int, data: bytes) -> dict:
        if offset > 0 and not self.dropped:
            self.dropped = True
            raise SerialProtocolError("timeout waiting for ota_chunk ack")
        return super().ota_chunk(offset, data)


class PerformerMissedChunkConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.target_mac = self._lanterns[0].mac
        self.stalled_offset: int | None = None
        self.repair_calls = 0
        self.restart_calls = 0

    def ota_chunk(self, offset: int, data: bytes) -> dict:
        ack = super().ota_chunk(offset, data)
        if offset > 0 and self.stalled_offset is None:
            self.stalled_offset = offset
        if self.stalled_offset is not None:
            node = self._ota_nodes[self.target_mac]
            node.update({
                "phase": "repairing",
                "offset": self.stalled_offset,
                "crc32": zlib.crc32(bytes(self._ota_write or b"")[: self.stalled_offset])
                & 0xFFFFFFFF,
            })
        return ack

    def ota_repair(self, mac: str, offset: int, data: bytes) -> dict:
        self.repair_calls += 1
        ack = super().ota_repair(mac, offset, data)
        if mac == self.target_mac and ack.get("ok"):
            self.stalled_offset = int(self._ota_nodes[mac]["offset"])
        return ack

    def ota_restart(self, mac: str) -> dict:
        self.restart_calls += 1
        return super().ota_restart(mac)


class RecordingActivationConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.activation_order: list[str | None] = []

    def ota_activate(self, mac: str | None = None) -> dict:
        self.activation_order.append(mac)
        return super().ota_activate(mac)


class PausableOtaConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.chunk_started = threading.Event()

    def ota_chunk(self, offset: int, data: bytes) -> dict:
        self.chunk_started.set()
        time.sleep(0.005)
        return super().ota_chunk(offset, data)


class NackingOtaChunkConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.nacked = False

    def ota_chunk(self, offset: int, data: bytes) -> dict:
        if offset > 0 and not self.nacked:
            self.nacked = True
            return {"ok": False, "error": "ota chunk offset mismatch"}
        return super().ota_chunk(offset, data)


class LengthMismatchOtaChunkConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.nacked = False

    def ota_chunk(self, offset: int, data: bytes) -> dict:
        if offset > 0 and not self.nacked:
            self.nacked = True
            return {"ok": False, "error": "ota chunk length mismatch"}
        return super().ota_chunk(offset, data)


class AdvancedThenNackingOtaChunkConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.nacked = False

    def ota_chunk(self, offset: int, data: bytes) -> dict:
        if offset > 0 and not self.nacked:
            self.nacked = True
            ack = super().ota_chunk(offset, data)
            assert ack["ok"] is True
            return {"ok": False, "error": "ota chunk offset mismatch"}
        return super().ota_chunk(offset, data)


class PartialAdvancedNackingOtaChunkConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.nacked = False
        self.partial_written = 0

    def ota_chunk(self, offset: int, data: bytes) -> dict:
        if offset > 0 and not self.nacked:
            self.nacked = True
            self._ota_write.extend(data[:17])
            self.partial_written = len(self._ota_write)
            return {"ok": False, "error": "ota chunk offset mismatch"}
        return super().ota_chunk(offset, data)


class ProgressFailedOtaConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.ended = False
        self.injected_failure = False
        self.restart_count = 0

    def ota_progress(self) -> dict:
        progress = super().ota_progress()
        if (
            not self.injected_failure
            and progress.get("written", 0) >= 64 * 128
            and progress.get("nodes")
        ):
            node = progress["nodes"][0]
            node.update({"phase": "failed", "error": "flash write failed"})
            self._ota_nodes[node["mac"]] = node
            self.injected_failure = True
        return progress

    def ota_restart(self, mac: str) -> dict:
        self.restart_count += 1
        return super().ota_restart(mac)

    def ota_end(self) -> dict:
        self.ended = True
        return super().ota_end()


class PartialOtaStatusConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.partial_reports = 2

    def ota_progress(self) -> dict:
        progress = super().ota_progress()
        if self.partial_reports > 0:
            self.partial_reports -= 1
            progress["nodes"] = progress.get("nodes", [])[:10]
        return progress


class FinalAckTimeoutConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.timed_out = False

    def ota_end(self) -> dict:
        ack = super().ota_end()
        assert ack["ok"] is True
        if not self.timed_out:
            self.timed_out = True
            raise SerialProtocolError("timeout waiting for ota_end ack")
        return ack


class EndIncompleteOtaConductor(MockConductor):
    def ota_end(self) -> dict:
        progress = self.ota_progress()
        nodes = progress.get("nodes", [])
        if nodes:
            nodes[0] = dict(nodes[0])
            nodes[0].update({"phase": "complete", "offset": self._ota_expected_size, "crc32": self._ota_expected_crc32})
        return {
            "ok": False,
            "error": "ota performers did not complete",
            "nodes": nodes,
        }


class ProgressTimeoutConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.progress_timeouts = 0

    def ota_progress(self) -> dict:
        if self._ota_write is not None and len(self._ota_write) >= 64 * 128 and self.progress_timeouts == 0:
            self.progress_timeouts += 1
            raise SerialProtocolError("timeout waiting for ota_progress ack")
        return super().ota_progress()


class NoOtaStatusConductor(MockConductor):
    def __init__(self) -> None:
        super().__init__()
        self.empty_reports = 2

    def ota_progress(self) -> dict:
        progress = super().ota_progress()
        if self.empty_reports > 0:
            self.empty_reports -= 1
            progress["nodes"] = []
        return progress


class OneNodeOnlyOtaStatusConductor(MockConductor):
    def ota_end(self) -> dict:
        ack = super().ota_end()
        if ack.get("ok"):
            first = self._lanterns[0]
            first.firmware = {
                "version": "0.3.0-mismatch",
                "proto": 6,
                "build_id": 0xED2E397F,
                "build_label": "ed2e397f",
                "dirty": True,
            }
            first.attention = "Firmware mismatch"
            ack = dict(ack)
            ack["nodes"] = [ack["nodes"][1]]
        return ack

    def ota_activate(self, mac: str | None = None) -> dict:
        ack = super().ota_activate(mac)
        if mac == self._lanterns[0].mac:
            self._lanterns[0].firmware = {
                "version": "0.3.0-mismatch",
                "proto": 6,
                "build_id": 0xED2E397F,
                "build_label": "ed2e397f",
                "dirty": True,
            }
            self._lanterns[0].attention = "Firmware mismatch"
        return ack


class LegacySnapshotConductor(MockConductor):
    def snapshot(self) -> dict:
        state = super().snapshot()
        state.pop("recovery", None)
        return state


class LegacyMixedFirmwareOtaConductor(MockConductor):
    def snapshot(self) -> dict:
        state = super().snapshot()
        if state["summary"]["firmware"]["consistent"] is False and state["ota"]["enabled"]:
            state["ota"]["ready"] = False
            state["ota"]["ready_count"] = state["summary"]["firmware"]["matching"]
            state["ota"]["blocked"] = ["firmware mismatch"]
        return state


def make_placed_conductor(count: int) -> MockConductor:
    conductor = MockConductor()
    conductor._lanterns = [
        Lantern(
            mac=f"02:00:00:00:{index // 256:02X}:{index % 256:02X}",
            label=f"#{index + 1}",
            status="alive",
            last_seen_s=index % 17,
            x=(index % 10) / 9,
            y=(index // 10) / 5,
        )
        for index in range(count)
    ]
    return conductor


def test_state_endpoint_returns_mock_state() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.get("/api/state")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["alive"] == 8
    assert body["summary"]["total"] == 9
    assert body["conductor"]["sync"] == "locked"
    assert body["conductor"]["firmware"]["version"] == "0.3.0"
    assert body["conductor"]["firmware"]["proto"] == 10
    assert body["summary"]["firmware"]["consistent"] is True
    assert body["power"]["light_sleep_check_s"] == 4
    assert body["groups"][0] == {"group_id": 0, "name": "", "label": "Group 1"}
    assert body["power_monitor"]["battery_capacity_wh"] == 384.0
    assert body["power_monitor"]["full_voltage"] == 14.4
    assert body["power_monitor"]["sample_count"] == 2
    assert body["power_monitor"]["usable_sample_count"] == 2
    assert body["power_monitor"]["estimated_node_soc_percent"] > 99
    assert body["recovery"]["status"] == "missing_nodes"


def test_health_endpoint_identifies_the_running_release(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_RELEASE_COMMIT", "a" * 40)
    client = TestClient(create_app(MockConductor()))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "version": Path("VERSION").read_text(encoding="utf-8").strip(),
        "commit": "a" * 40,
    }


def test_health_endpoint_identifies_the_started_process_when_marker_changes(
    monkeypatch, tmp_path: Path
) -> None:
    marker = tmp_path / "running-commit"
    marker.write_text("a" * 40 + "\n", encoding="utf-8")
    monkeypatch.delenv("CONTROL_RELEASE_COMMIT", raising=False)
    monkeypatch.setenv("CONTROL_RELEASE_COMMIT_FILE", str(marker))
    client = TestClient(create_app(MockConductor()))
    marker.write_text("b" * 40 + "\n", encoding="utf-8")

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["commit"] == "a" * 40


def test_wifi_status_reports_current_connection(monkeypatch) -> None:
    def fake_which(command: str) -> str | None:
      return "/usr/bin/nmcli" if command == "nmcli" else None

    def fake_run(command: list[str], **_kwargs):
        if command[:2] == ["git", "-C"]:
            return app_module.subprocess.CompletedProcess(
                command, 0, stdout="a" * 40 + "\n", stderr=""
            )
        if command[:4] == ["/usr/bin/nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"]:
            return app_module.subprocess.CompletedProcess(
                command,
                0,
                stdout="wlan0:wifi:connected:Basketnet\neth0:ethernet:unavailable:\n",
                stderr="",
            )
        if command[:5] == ["ip", "-4", "-o", "addr", "show"]:
            return app_module.subprocess.CompletedProcess(
                command,
                0,
                stdout="3: wlan0    inet 10.42.0.1/24 brd 10.42.0.255 scope global wlan0\n",
                stderr="",
            )
        raise AssertionError(command)

    monkeypatch.setattr(app_module.shutil, "which", fake_which)
    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = TestClient(create_app(MockConductor()))

    response = client.get("/api/network/wifi")

    assert response.status_code == 200
    assert response.json()["wifi"] == {
        "available": True,
        "error": None,
        "device": "wlan0",
        "state": "connected",
        "connection": "Basketnet",
        "addresses": ["10.42.0.1/24"],
        "allow_changes": True,
    }


def test_wifi_join_runs_join_command_in_background(monkeypatch) -> None:
    commands = []

    monkeypatch.setenv("CONTROL_WIFI_JOIN_DELAY_S", "0")
    monkeypatch.setenv("CONTROL_WIFI_JOIN_COMMAND", "/missing/lightweave-wifi-home")
    monkeypatch.setattr(
        app_module.shutil,
        "which",
        lambda command: {"nmcli": "/usr/bin/nmcli", "sudo": "/usr/bin/sudo"}.get(command),
    )

    def fake_run(command: list[str], **_kwargs):
        commands.append(command)
        return app_module.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = TestClient(create_app(MockConductor()))

    response = client.post("/api/network/wifi", json={"ssid": "New House", "password": "secret"})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert commands[-1:] == [["/usr/bin/sudo", "-n", "/usr/bin/nmcli", "dev", "wifi", "connect", "New House", "password", "secret"]]


def test_hotspot_start_runs_nmcli_connection(monkeypatch) -> None:
    commands = []

    monkeypatch.setenv("CONTROL_WIFI_JOIN_DELAY_S", "0")
    monkeypatch.setattr(
        app_module.shutil,
        "which",
        lambda command: {"nmcli": "/usr/bin/nmcli", "sudo": "/usr/bin/sudo"}.get(command),
    )

    def fake_run(command: list[str], **_kwargs):
        commands.append(command)
        return app_module.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(app_module.subprocess, "run", fake_run)
    client = TestClient(create_app(MockConductor()))

    response = client.post("/api/network/hotspot")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert commands[-1:] == [["/usr/bin/sudo", "-n", "/usr/bin/nmcli", "con", "up", "BasketsSetup"]]


def test_state_endpoint_enriches_legacy_snapshot_with_recovery() -> None:
    conductor = LegacySnapshotConductor()
    conductor._lanterns[0].firmware = {
        "version": "0.2.0",
        "proto": 6,
        "build_id": 0xDEADBEEF,
        "build_label": "deadbeef",
        "dirty": False,
    }
    client = TestClient(create_app(conductor))

    recovery = client.get("/api/state").json()["recovery"]

    assert recovery["status"] == "mixed_firmware"
    assert recovery["mismatched"][0]["mac"] == "8C:94:DF:8F:71:50"


def test_identify_unknown_lantern_is_404() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.post("/api/lanterns/00:00:00:00:00:00/identify")

    assert response.status_code == 404


def test_state_endpoint_reports_serial_timeout_as_503() -> None:
    client = TestClient(create_app(DownConductor()))

    response = client.get("/api/state")

    assert response.status_code == 503
    assert response.json()["detail"] == "timeout waiting for state ack"


def test_websocket_reports_serial_timeout_as_error_event() -> None:
    client = TestClient(create_app(DownConductor()))

    with client.websocket_connect("/ws") as ws:
        event = ws.receive_json()

    assert event["type"] == "error"
    assert event["message"] == "timeout waiting for state ack"


def test_pattern_update_round_trips_to_state() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.post(
        "/api/show/pattern",
        json={"pattern": "Sweep", "brightness": 64, "params": {"period": 8000}},
    )
    state = client.get("/api/state").json()

    assert response.status_code == 200
    assert state["pattern"]["pattern"] == "Sweep"
    assert state["pattern"]["brightness"] == 64
    assert state["pattern"]["params"] == {"period": 8000}


def test_blackout_restore_endpoint_recovers_previous_group_brightness() -> None:
    conductor = MockConductor()
    conductor.update_pattern("White", 24, {}, group_id=0)
    conductor.update_pattern("Fire Flicker", 56, {"period": 1200}, group_id=1)
    client = TestClient(create_app(conductor))

    blacked_out = client.post("/api/show/blackout")
    restored = client.post("/api/show/restore")
    state = client.get("/api/state").json()

    assert blacked_out.status_code == 200
    assert restored.status_code == 200
    assert state["blackout"] == {"restore_available": False}
    assert [entry["config"]["brightness"] for entry in state["patterns"][:2]] == [24, 56]


def test_pattern_update_rejected_by_conductor_is_400() -> None:
    client = TestClient(create_app(RejectingPatternConductor()))

    response = client.post(
        "/api/show/pattern",
        json={"pattern": "Bad", "brightness": 64, "params": {}},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "bad pattern"


def test_calibration_mode_toggle_restores_previous_pattern() -> None:
    conductor = MockConductor()
    conductor.update_pattern("Sweep", 72, {"period": 8000}, group_id=3)
    conductor.update_pattern("White", 0, {}, group_id=5)
    client = TestClient(create_app(conductor))

    started = client.post("/api/operations/calibration-mode", json={"enabled": True})
    running = client.get("/api/state").json()
    stopped = client.post("/api/operations/calibration-mode", json={"enabled": False})
    restored = client.get("/api/state").json()

    assert started.status_code == 200
    assert started.json()["plan"]["min_hamming_distance"] == 3
    assert running["pattern"]["pattern"] == "Calibration"
    assert running["pattern"]["params"]["p0"] == 1000
    assert running["pattern"]["params"]["p1"] == started.json()["plan"]["bit_count"]
    assert stopped.status_code == 200
    assert restored["pattern"]["pattern"] == "Glow"
    assert restored["pattern"]["brightness"] == 48
    assert restored["pattern"]["params"] == {"hue": 40, "saturation": 100}
    assert restored["patterns"][3]["config"] == {
        "pattern": "Sweep",
        "brightness": 72,
        "params": {"period": 8000},
    }
    assert restored["patterns"][5]["config"] == {
        "pattern": "White",
        "brightness": 0,
        "params": {},
    }


def test_group_name_round_trips_to_state_and_persists(tmp_path: Path) -> None:
    store = GroupStore(tmp_path)
    client = TestClient(create_app(MockConductor(), group_store=store))

    response = client.put("/api/groups/0", json={"name": "  Box   lanterns  "})
    state = client.get("/api/state").json()
    lanterns = client.get("/api/lanterns").json()

    assert response.status_code == 200
    assert response.json()["group"] == {
        "group_id": 0,
        "name": "Box lanterns",
        "label": "Group 1 · Box lanterns",
    }
    assert state["groups"][0] == response.json()["group"]
    assert state["lanterns"][0]["group"] == "Group 1 · Box lanterns"
    assert lanterns[0]["group"] == "Group 1 · Box lanterns"
    assert GroupStore(tmp_path).list()[0] == response.json()["group"]


def test_blank_group_name_restores_numbered_label(tmp_path: Path) -> None:
    store = GroupStore(tmp_path)
    store.update(2, "Bikes")
    client = TestClient(create_app(MockConductor(), group_store=store))

    response = client.put("/api/groups/2", json={"name": "   "})

    assert response.status_code == 200
    assert response.json()["group"] == {"group_id": 2, "name": "", "label": "Group 3"}
    assert GroupStore(tmp_path).list()[2]["name"] == ""


def test_group_name_api_rejects_unknown_group_and_long_name(tmp_path: Path) -> None:
    client = TestClient(create_app(MockConductor(), group_store=GroupStore(tmp_path)))

    unknown = client.put("/api/groups/8", json={"name": "Bikes"})
    too_long = client.put("/api/groups/0", json={"name": "x" * 49})

    assert unknown.status_code == 404
    assert too_long.status_code == 422


def test_calibration_mode_rejected_by_conductor_is_400() -> None:
    client = TestClient(create_app(RejectingPatternConductor()))

    response = client.post("/api/operations/calibration-mode", json={"enabled": True})

    assert response.status_code == 400
    assert response.json()["detail"] == "bad pattern"


def test_calibration_mode_includes_unprovisioned_nodes_by_mac_rank() -> None:
    conductor = MockConductor()
    conductor._lanterns.append(
        Lantern("AA:BB:CC:00:00:01", "Unknown", "alive", 2, None, None)
    )
    client = TestClient(create_app(conductor))

    response = client.post("/api/operations/calibration-mode", json={"enabled": True})
    state = client.get("/api/state").json()

    assert response.status_code == 200
    assert "AA:BB:CC:00:00:01" in [item["mac"] for item in response.json()["plan"]["codes"]]
    assert state["pattern"]["pattern"] == "Calibration"


def test_preview_endpoint_returns_png_for_positioned_lanterns() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.get(
        "/preview",
        params={
            "pattern": "Sweep",
            "brightness": 64,
            "period": 8000,
            "wavelength": 300,
            "t": 1200,
            "width": 180,
            "height": 120,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(response.content) > 100


def test_preview_endpoint_accepts_json_params() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.get(
        "/preview",
        params={
            "pattern": "Glow",
            "brightness": 48,
            "params": '{"hue": 40, "saturation": 100}',
            "width": 120,
            "height": 120,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_fire_flicker_preview_exposes_distinct_addressable_ring_pixels() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.get(
        "/preview.json",
        params={
            "pattern": "Fire Flicker",
            "brightness": 56,
            "period": 1200,
            "hue": 24,
            "saturation": 95,
            "texture": 85,
            "t": 730,
        },
    )

    assert response.status_code == 200
    body = response.json()
    first = body["lanterns"][0]
    assert len(first["pixels"]) == 16
    assert len({tuple(pixel["rgbw"]) for pixel in first["pixels"]}) >= 6
    assert first["ring_contrast"] > 0.03
    assert body["metrics"]["max_ring_contrast"] >= first["ring_contrast"]

    later = client.get(
        "/preview.json",
        params={
            "pattern": "Fire Flicker",
            "brightness": 56,
            "period": 1200,
            "texture": 85,
            "t": 980,
        },
    ).json()
    assert first["pixels"] != later["lanterns"][0]["pixels"]

    png = client.get(
        "/preview",
        params={
            "pattern": "Fire Flicker",
            "brightness": 56,
            "texture": 85,
            "t": 730,
            "width": 180,
            "height": 120,
        },
    )
    assert png.status_code == 200
    assert png.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_fire_flicker_preview_matches_firmware_golden_sample() -> None:
    intensity, heat = _fire_flicker_sample(
        730_000, 0.2, 0.8, 6, 16, 1.2, 0.85
    )

    assert intensity == pytest.approx(0.4318507, abs=1e-4)
    assert heat == pytest.approx(-0.7561197, abs=1e-4)


def test_hex_picker_preserves_value_and_packs_distinct_pattern_colors() -> None:
    script = r'''
const fs = require("fs");
const src = fs.readFileSync("control/static/app.js", "utf8");
eval(src.slice(0, src.indexOf("function isPatternDirty")));
function sample(hex) {
  const rgb = parseHexColor(hex);
  const hsv = rgbToHueSaturationValue(rgb.r, rgb.g, rgb.b);
  return {
    hex,
    hsv,
    roundTrip: hueSaturationValueToHex(hsv.hue, hsv.saturation, hsv.value),
    params: patternParams({
      pattern: "Glow", brightness: 48,
      hue: hsv.hue, saturation: hsv.saturation, value: hsv.value,
    }),
  };
}
console.log(JSON.stringify([sample("#ff8800"), sample("#804400"), sample("#808080")]));
'''
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    bright, half, gray = json.loads(result.stdout)

    assert bright["roundTrip"] == "#ff8800"
    assert half["roundTrip"] == "#804400"
    assert gray["roundTrip"] == "#808080"
    assert bright["params"]["p2"] != half["params"]["p2"]
    assert gray["params"]["p1"] == 0
    assert gray["params"]["p2"] & 0x8000


def test_fire_flicker_ui_packs_speed_color_value_and_texture_into_wire_params() -> None:
    script = r'''
const fs = require("fs");
const src = fs.readFileSync("control/static/app.js", "utf8");
eval(src.slice(0, src.indexOf("function isPatternDirty")));
console.log(JSON.stringify(patternParams({
  pattern: "Fire Flicker", brightness: 56,
  period: 1200, hue: 24, saturation: 95, value: 255, texture: 85,
})));
'''
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    params = json.loads(result.stdout)

    assert params["p0"] == 1200
    assert params["p1"] == 24
    assert params["p2"] & 0x8000
    assert params["p2"] & 0x7F == 85
    assert (params["p2"] >> 7) & 0xFF == 255
    assert params["p3"] == 95


def test_preview_distinguishes_hex_value_uses_white_for_gray_and_keeps_ocean_saturation() -> None:
    client = TestClient(create_app(MockConductor()))

    def glow_lantern(value: int, saturation: int = 100, hue: int = 32) -> dict:
        response = client.get(
            "/preview.json",
            params={
                "pattern": "Glow",
                "brightness": 48,
                "params": json.dumps({
                    "p0": hue if saturation else 0,
                    "p1": saturation,
                    "p2": 0x8000 | value,
                }),
                "t": 0,
            },
        )
        assert response.status_code == 200
        return response.json()["lanterns"][0]

    bright = glow_lantern(255)
    half = glow_lantern(128)
    gray = glow_lantern(128, saturation=0)
    pale_yellow = glow_lantern(238, saturation=35, hue=60)
    assert bright["rgbw"] == [48, 12, 0, 0]
    assert half["rgbw"] == [10, 3, 0, 0]
    assert sum(half["rgbw"]) < sum(bright["rgbw"])
    assert gray["rgbw"] == [0, 0, 0, 10]
    assert gray["rgb"][0] == gray["rgb"][1] == gray["rgb"][2]
    assert gray["rgb"][0] > gray["rgbw"][3]
    assert pale_yellow["rgbw"] == [41, 41, 16, 0]

    def ocean_rgbw(saturation: int) -> list[int]:
        sat6 = round(saturation * 63 / 100)
        value6 = 63
        response = client.get(
            "/preview.json",
            params={
                "pattern": "Ocean Wave",
                "brightness": 64,
                "params": json.dumps({
                    "p0": 9000,
                    "p1": 100 | (sat6 << 10),
                    "p2": 0x8000 | (value6 << 9) | 45,
                    "p3": 205,
                }),
                "t": 1500,
            },
        )
        assert response.status_code == 200
        return response.json()["lanterns"][0]["rgbw"]

    assert ocean_rgbw(100) != ocean_rgbw(20)


def test_preview_json_endpoint_returns_lantern_samples_and_metrics() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.get(
        "/preview.json",
        params={"pattern": "Sweep", "brightness": 64, "period": 8000, "spatial": 300, "t": 1200},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["pattern"] == "Sweep"
    assert body["brightness"] == 64
    assert body["t"] == 1200
    assert body["metrics"]["count"] == 9
    assert 0 <= body["metrics"]["lit_count"] <= body["metrics"]["count"]
    assert 0 <= body["metrics"]["contrast"] <= 1
    first = body["lanterns"][0]
    assert first["mac"] == "8C:94:DF:8F:71:50"
    assert len(first["rgbw"]) == 4
    assert len(first["rgb"]) == 3
    assert isinstance(first["luma"], float)


def test_preview_json_endpoint_renders_white_channel_pattern() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.get(
        "/preview.json",
        params={"pattern": "White", "brightness": 64, "t": 0},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["pattern"] == "White"
    assert body["lanterns"][0]["rgbw"] == [0, 0, 0, 64]


def test_preview_frames_endpoint_returns_sequence_metrics() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.get(
        "/preview/frames.json",
        params={
            "pattern": "Sweep",
            "brightness": 64,
            "period": 8000,
            "spatial": 300,
            "duration_ms": 2000,
            "fps": 2,
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["frame_count"] == 4
    assert [frame["t"] for frame in body["frames"]] == [0, 500, 1000, 1500]
    assert body["metrics"]["max_lit_count"] <= 9
    assert body["metrics"]["avg_luma_mean"] >= 0
    assert body["metrics"]["max_contrast"] >= body["metrics"]["min_contrast"]


def test_review_endpoint_scores_candidate_pattern() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.get(
        "/review",
        params={"pattern": "Glow", "brightness": 48, "hue": 40, "duration_ms": 2000, "fps": 2},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["rating"] in {"strong", "usable"}
    assert body["score"] >= 70
    assert body["metrics"]["avg_luma_mean"] > 0
    assert body["issues"] == []


def test_review_endpoint_rejects_blackout_candidate() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.get(
        "/review",
        params={"pattern": "Glow", "brightness": 0, "duration_ms": 2000, "fps": 2},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["ok"] is False
    assert body["rating"] == "reject"
    assert "blackout" in {issue["code"] for issue in body["issues"]}


def test_preview_endpoint_rejects_unknown_pattern() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.get("/preview", params={"pattern": "Bad"})

    assert response.status_code == 400
    assert response.json()["detail"] == "unknown pattern: Bad"


def test_preview_endpoint_reports_serial_timeout_as_503() -> None:
    client = TestClient(create_app(DownConductor()))

    response = client.get("/preview", params={"pattern": "Glow"})

    assert response.status_code == 503
    assert response.json()["detail"] == "timeout waiting for state ack"


def test_pattern_library_crud_round_trip(tmp_path) -> None:
    client = TestClient(create_app(MockConductor(), pattern_store=PatternStore(tmp_path)))

    created = client.post(
        "/api/patterns",
        json={
            "name": "Amber Glow",
            "pattern": "Glow",
            "brightness": 48,
            "params": {"hue": 40, "saturation": 100},
        },
    )
    pattern_id = created.json()["pattern"]["id"]
    updated = client.put(
        f"/api/patterns/{pattern_id}",
        json={
            "name": "Slow Sweep",
            "pattern": "Sweep",
            "brightness": 64,
            "params": {"period": 8000, "wavelength": 300},
        },
    )
    fetched = client.get(f"/api/patterns/{pattern_id}")
    listed = client.get("/api/patterns")
    deleted = client.delete(f"/api/patterns/{pattern_id}")
    missing = client.get(f"/api/patterns/{pattern_id}")

    assert created.status_code == 200
    assert pattern_id == "amber-glow"
    assert updated.status_code == 200
    assert updated.json()["pattern"]["pattern"] == "Sweep"
    assert fetched.json()["pattern"]["name"] == "Slow Sweep"
    assert [item["id"] for item in listed.json()["patterns"]] == [pattern_id]
    assert deleted.status_code == 200
    assert missing.status_code == 404


def test_pattern_library_corrupt_brightness_returns_500(tmp_path) -> None:
    (tmp_path / "patterns.json").write_text(
        '{"bad":{"id":"bad","name":"Bad","pattern":"Glow","params":{}}}',
        encoding="utf-8",
    )
    client = TestClient(create_app(MockConductor(), pattern_store=PatternStore(tmp_path)))

    response = client.get("/api/patterns")

    assert response.status_code == 500
    assert response.json()["detail"] == "brightness must be between 0 and 192"


def test_pattern_library_preview_returns_png(tmp_path) -> None:
    client = TestClient(create_app(MockConductor(), pattern_store=PatternStore(tmp_path)))
    created = client.post(
        "/api/patterns",
        json={
            "name": "Preview Me",
            "pattern": "Palette Drift",
            "brightness": 64,
            "params": {"period": 8000, "spatial": 300},
        },
    )
    pattern_id = created.json()["pattern"]["id"]

    response = client.get(f"/api/patterns/{pattern_id}/preview", params={"width": 120, "height": 120})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_pattern_library_preview_json_returns_saved_pattern_data(tmp_path) -> None:
    client = TestClient(create_app(MockConductor(), pattern_store=PatternStore(tmp_path)))
    created = client.post(
        "/api/patterns",
        json={
            "name": "Preview Json",
            "pattern": "Glow",
            "brightness": 48,
            "params": {"hue": 40, "saturation": 100},
        },
    )
    pattern_id = created.json()["pattern"]["id"]

    response = client.get(f"/api/patterns/{pattern_id}/preview.json", params={"t": 1000})
    body = response.json()

    assert response.status_code == 200
    assert body["pattern"] == "Glow"
    assert body["params"] == {"hue": 40, "saturation": 100}
    assert body["metrics"]["count"] == 9
    assert body["metrics"]["lit_count"] == 9


def test_pattern_library_preview_frames_json_returns_saved_sequence(tmp_path) -> None:
    client = TestClient(create_app(MockConductor(), pattern_store=PatternStore(tmp_path)))
    created = client.post(
        "/api/patterns",
        json={
            "name": "Frame Json",
            "pattern": "Sweep",
            "brightness": 64,
            "params": {"period": 8000, "spatial": 300},
        },
    )
    pattern_id = created.json()["pattern"]["id"]

    response = client.get(
        f"/api/patterns/{pattern_id}/preview/frames.json",
        params={"duration_ms": 2000, "fps": 2},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["pattern"] == "Sweep"
    assert body["frame_count"] == 4
    assert len(body["frames"]) == 4


def test_pattern_library_review_returns_saved_pattern_score(tmp_path) -> None:
    client = TestClient(create_app(MockConductor(), pattern_store=PatternStore(tmp_path)))
    created = client.post(
        "/api/patterns",
        json={
            "name": "Review Me",
            "pattern": "Glow",
            "brightness": 48,
            "params": {"hue": 40, "saturation": 100},
        },
    )
    pattern_id = created.json()["pattern"]["id"]

    response = client.get(f"/api/patterns/{pattern_id}/review", params={"duration_ms": 2000, "fps": 2})
    body = response.json()

    assert response.status_code == 200
    assert body["ok"] is True
    assert body["score"] >= 70
    assert body["recommendations"]


def test_pattern_library_broadcast_updates_live_pattern(tmp_path) -> None:
    client = TestClient(create_app(MockConductor(), pattern_store=PatternStore(tmp_path)))
    created = client.post(
        "/api/patterns",
        json={
            "name": "Broadcast Sweep",
            "pattern": "Sweep",
            "brightness": 64,
            "params": {"period": 8000, "spatial": 300},
        },
    )
    pattern_id = created.json()["pattern"]["id"]

    response = client.post(f"/api/patterns/{pattern_id}/broadcast")
    state = client.get("/api/state").json()

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["pattern"]["id"] == pattern_id
    assert state["pattern"]["pattern"] == "Sweep"
    assert state["pattern"]["brightness"] == 64
    assert state["pattern"]["params"] == {"period": 8000, "spatial": 300}


def test_pattern_library_broadcast_can_target_one_group(tmp_path) -> None:
    client = TestClient(create_app(MockConductor(), pattern_store=PatternStore(tmp_path)))
    created = client.post(
        "/api/patterns",
        json={
            "name": "Group Sweep",
            "pattern": "Sweep",
            "brightness": 64,
            "params": {"period": 8000, "spatial": 300},
        },
    )
    pattern_id = created.json()["pattern"]["id"]

    response = client.post(
        f"/api/patterns/{pattern_id}/broadcast", params={"group_id": 2}
    )
    patterns = client.get("/api/state").json()["patterns"]

    assert response.status_code == 200
    assert patterns[2]["config"]["pattern"] == "Sweep"
    assert patterns[0]["config"]["pattern"] == "Glow"


def test_pattern_library_broadcast_unknown_is_404(tmp_path) -> None:
    client = TestClient(create_app(MockConductor(), pattern_store=PatternStore(tmp_path)))

    response = client.post("/api/patterns/nope/broadcast")

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown pattern"


def test_pattern_library_broadcast_rejected_by_conductor_is_400(tmp_path) -> None:
    client = TestClient(create_app(RejectingPatternConductor(), pattern_store=PatternStore(tmp_path)))
    created = client.post(
        "/api/patterns",
        json={"name": "Bad Live Pattern", "pattern": "Bad", "brightness": 64, "params": {}},
    )
    pattern_id = created.json()["pattern"]["id"]

    response = client.post(f"/api/patterns/{pattern_id}/broadcast")

    assert response.status_code == 400
    assert response.json()["detail"] == "bad pattern"


def test_pattern_library_update_unknown_is_404(tmp_path) -> None:
    client = TestClient(create_app(MockConductor(), pattern_store=PatternStore(tmp_path)))

    response = client.put(
        "/api/patterns/nope",
        json={"name": "Nope", "pattern": "Glow", "brightness": 48, "params": {}},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown pattern"


def test_power_policy_update_round_trips_to_state() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.post(
        "/api/operations/power-policy",
        json={
            "light_sleep_check_s": 30,
            "deep_sleep_check_min": 60,
            "led_on_start_min": 19 * 60,
            "led_on_end_min": 5 * 60,
            "schedule_enabled": True,
            "force_awake": False,
            "force_sleep": False,
            "current_min": 12 * 60,
            "current_epoch_s": 1_720_123_456,
        },
    )
    state = client.get("/api/state").json()

    assert response.status_code == 200
    assert state["power"]["light_sleep_check_s"] == 30
    assert state["power"]["deep_sleep_check_min"] == 60
    assert state["power"]["schedule_enabled"] is True
    assert state["power"]["force_awake"] is False
    assert state["power"]["force_sleep"] is False
    assert state["power"]["current_epoch_s"] == 1_720_123_456
    assert state["power"]["leds_on"] is False


def test_field_power_actions_preserve_schedule_and_toggle_overrides() -> None:
    client = TestClient(create_app(MockConductor()))
    original = client.get("/api/state").json()["power"]

    sleeping = client.post("/api/operations/field-power", json={"mode": "sleep"})
    sleep_state = client.get("/api/state").json()["power"]
    assert sleeping.status_code == 200
    assert sleeping.json()["mode"] == "sleep"
    assert sleep_state["force_sleep"] is True
    assert sleep_state["force_awake"] is False
    assert sleep_state["leds_on"] is False

    waking = client.post("/api/operations/field-power", json={"mode": "wake"})
    wake_state = client.get("/api/state").json()["power"]
    assert waking.status_code == 200
    assert wake_state["force_sleep"] is False
    assert wake_state["force_awake"] is True
    assert wake_state["leds_on"] is True

    following = client.post("/api/operations/field-power", json={"mode": "schedule"})
    schedule_state = client.get("/api/state").json()["power"]
    assert following.status_code == 200
    assert schedule_state["force_sleep"] is False
    assert schedule_state["force_awake"] is False
    assert schedule_state["led_on_start_min"] == original["led_on_start_min"]
    assert schedule_state["led_on_end_min"] == original["led_on_end_min"]


def test_power_monitor_settings_and_manual_full_sync() -> None:
    conductor = MockConductor()
    client = TestClient(create_app(conductor))
    mac = "8C:94:DF:8F:71:50"

    settings = client.post(
        "/api/operations/power-monitor",
        json={"battery_capacity_wh": 200.0, "full_voltage": 14.4},
    )
    assert settings.status_code == 200
    state = client.get("/api/state").json()
    assert state["power_monitor"]["battery_capacity_wh"] == 200.0
    assert state["power_monitor"]["full_voltage"] == 14.4

    sync = client.post(f"/api/lanterns/{mac}/power-sync-full")
    assert sync.status_code == 200
    state = client.get("/api/state").json()
    sample = next(item for item in state["power_monitor"]["samples"] if item["mac"] == mac)
    assert sample["soc_percent"] == 100.0
    assert sample["full_anchor"]["manual"] is True


def test_power_monitor_auto_anchors_when_full_voltage_seen() -> None:
    conductor = MockConductor()
    conductor._lanterns[0].bus_v = 14.7
    client = TestClient(create_app(conductor))

    state = client.get("/api/state").json()

    sample = next(item for item in state["power_monitor"]["samples"] if item["mac"] == "8C:94:DF:8F:71:50")
    assert sample["full_detected"] is True
    assert sample["soc_percent"] == 100.0


def test_ota_mode_update_round_trips_to_state(managed_client) -> None:
    client = managed_client(create_app(MockConductor()))

    response = client.post("/api/operations/ota-mode", json={"enabled": True})
    state = client.get("/api/state").json()

    assert response.status_code == 200
    assert state["ota"]["mode"] == "updating"
    assert state["ota"]["enabled"] is True
    assert state["ota"]["expected"] == 9
    assert state["ota"]["missing"] == 0
    assert state["ota"]["deferred"] == 0
    assert state["ota"]["ready"] is True
    assert state["ota"]["blocked"] == []


def test_ota_artifact_upload_stages_firmware_metadata(tmp_path, managed_client) -> None:
    client = managed_client(create_app(MockConductor(), ota_store=OtaArtifactStore(tmp_path)))

    response = client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=b"\xe9" + b"\x00" * 4095,
        headers={"content-type": "application/octet-stream"},
    )
    current = client.get("/api/operations/ota-artifact").json()

    assert response.status_code == 200
    artifact = response.json()["artifact"]
    assert artifact["filename"] == "firmware.bin"
    assert artifact["size"] == 4096
    assert artifact["chunks"] == 32
    assert len(artifact["sha256"]) == 64
    assert current["artifact"]["sha256"] == artifact["sha256"]


def test_ota_artifact_store_reload_preserves_current_artifact(tmp_path, managed_client) -> None:
    firmware = b"\xe9" + b"\x00" * 4095
    staged = OtaArtifactStore(tmp_path).stage("firmware.bin", firmware)

    reloaded = OtaArtifactStore(tmp_path)

    assert reloaded.current() == staged
    assert reloaded.artifact() is not None
    assert reloaded.artifact().path.read_bytes() == firmware


def test_ota_artifact_upload_rejects_non_bin(tmp_path, managed_client) -> None:
    client = managed_client(create_app(MockConductor(), ota_store=OtaArtifactStore(tmp_path)))

    response = client.put(
        "/api/operations/ota-artifact?filename=firmware.txt",
        content=b"not firmware",
        headers={"content-type": "application/octet-stream"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "firmware artifact must be a .bin file"


def test_ota_install_requires_staged_artifact(tmp_path, managed_client) -> None:
    conductor = MockConductor()
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 400
    assert response.json()["detail"] == "no firmware staged"


def test_ota_install_fails_closed_if_staged_bytes_are_tampered(tmp_path, managed_client) -> None:
    conductor = MockConductor()
    conductor.set_ota_mode(True)
    store = OtaArtifactStore(tmp_path)
    client = managed_client(create_app(conductor, ota_store=store))
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=b"\xe9\x00",
        headers={"content-type": "application/octet-stream"},
    )
    artifact = store.artifact()
    assert artifact is not None
    artifact.path.write_bytes(b"\xe9\x01")

    response = client.post("/api/operations/ota-install")
    install = wait_for_ota_terminal(client)

    assert response.status_code == 202
    assert install["complete"] is False
    assert install["error"] == "staged firmware SHA-256 mismatch"
    assert conductor.ota_installed_crc32 is None


def test_ota_install_defers_while_software_deployment_holds_lock(tmp_path, managed_client) -> None:
    conductor = MockConductor()
    conductor.set_ota_mode(True)
    store = OtaArtifactStore(tmp_path / "ota")
    app = create_app(conductor, ota_store=store)
    lock_path = tmp_path / "operations" / "firmware-ota.lock"
    lock_path.parent.mkdir()
    lock_path.touch()
    app.state.ota_operation_lock_path = lock_path
    client = managed_client(app)
    store.stage("firmware.bin", b"\xe9\x00")

    with lock_path.open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        response = client.post("/api/operations/ota-install")

    assert response.status_code == 423
    assert response.json()["detail"] == "software deployment is in progress"


def test_ota_install_updates_online_cohort_and_defers_missing_lantern(tmp_path, managed_client) -> None:
    conductor = MockConductor()
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=b"\xe9" + bytes(range(255)),
        headers={"content-type": "application/octet-stream"},
    )

    state = client.get("/api/state").json()
    response = client.post("/api/operations/ota-install")

    assert state["recovery"]["status"] == "missing_nodes"
    assert state["recovery"]["missing"] == [
        {"mac": "A0:B7:65:11:44:91", "label": "#18", "reason": "not seen"}
    ]
    assert state["ota"]["enabled"] is True
    assert state["ota"]["ready"] is True
    assert state["ota"]["ready_count"] == 9
    assert state["ota"]["deferred"] == 0
    assert state["ota"]["blocked"] == []
    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert install["complete"] is True
    assert install["target_count"] == 9
    assert install["deferred_count"] == 1
    assert install["deferred"] == [
        {"mac": "A0:B7:65:11:44:91", "label": "#18"}
    ]
    assert {node["mac"] for node in install["nodes"]} == set(install["target_macs"])


def test_ota_install_streams_staged_artifact_when_ready(tmp_path, managed_client) -> None:
    conductor = MockConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 20
    stage = client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    ).json()

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    assert response.json()["message"] == "OTA install accepted"
    install = wait_for_ota_terminal(client)
    assert conductor.ota_installed_crc32 == stage["artifact"]["crc32"]
    assert install["running"] is False
    assert install["complete"] is True
    assert install["chunks_sent"] == stage["artifact"]["chunks"]
    assert install["bytes_sent"] == stage["artifact"]["size"]
    assert install["elapsed_s"] >= 0
    assert install["bytes_per_s"] >= 0
    assert install["eta_s"] == 0
    assert install["completed_at"] >= install["started_at"]
    assert {node["phase"] for node in install["nodes"]} == {"complete"}
    ota = client.get("/api/state").json()["ota"]
    assert ota["enabled"] is False
    assert all(node["offset"] == stage["artifact"]["size"] for node in ota["nodes"])


def test_ota_install_scales_to_60_expected_nodes(tmp_path, managed_client) -> None:
    conductor = make_placed_conductor(60)
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 40
    stage = client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    ).json()

    state = client.get("/api/state").json()
    response = client.post("/api/operations/ota-install")

    assert state["summary"]["total"] == 60
    assert state["ota"]["ready"] is True
    assert state["ota"]["ready_count"] == 60
    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert install["complete"] is True
    assert install["chunks_sent"] == stage["artifact"]["chunks"]
    assert len(install["nodes"]) == 60
    assert {node["phase"] for node in install["nodes"]} == {"complete"}
    assert {node["offset"] for node in install["nodes"]} == {stage["artifact"]["size"]}


def test_ota_install_60_nodes_waits_for_delayed_status_reports(tmp_path, managed_client) -> None:
    conductor = PartialOtaStatusConductor()
    conductor._lanterns = make_placed_conductor(60)._lanterns
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 40
    stage = client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    ).json()

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert install["complete"] is True
    assert len(install["nodes"]) == 60
    assert {node["offset"] for node in install["nodes"]} == {stage["artifact"]["size"]}


def test_ota_install_retries_transient_chunk_timeout(tmp_path, managed_client) -> None:
    conductor = DroppingOtaChunkConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 3
    stage = client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    ).json()

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert conductor.dropped is True
    assert install["complete"] is True
    assert install["chunks_sent"] == stage["artifact"]["chunks"]
    assert install["bytes_sent"] == stage["artifact"]["size"]
    assert install["last_retry"]["error"] == "timeout waiting for ota_chunk ack"


def test_ota_install_repairs_a_performer_that_missed_a_chunk_without_restarting_it(
    tmp_path, managed_client
) -> None:
    conductor = PerformerMissedChunkConductor()
    for lantern in conductor._lanterns:
        lantern.status = "alive"
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 3
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    )

    assert client.post("/api/operations/ota-install").status_code == 202
    install = wait_for_ota_terminal(client)

    assert install["complete"] is True
    assert conductor.repair_calls > 0
    assert conductor.restart_calls == 0
    assert install["repair_chunks"] == conductor.repair_calls
    assert {node["phase"] for node in install["nodes"]} == {"complete"}


def test_ota_install_activates_performers_one_at_a_time_then_conductor(
    tmp_path, managed_client
) -> None:
    conductor = RecordingActivationConductor()
    for lantern in conductor._lanterns:
        lantern.status = "alive"
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=b"\xe9" + bytes(range(255)) * 2,
        headers={"content-type": "application/octet-stream"},
    )

    assert client.post("/api/operations/ota-install").status_code == 202
    install = wait_for_ota_terminal(client)

    assert install["complete"] is True
    assert conductor.activation_order[:-1] == sorted(install["target_macs"])
    assert conductor.activation_order[-1] is None


def test_ota_install_can_pause_and_resume_from_the_conductor_prefix(
    tmp_path, managed_client
) -> None:
    conductor = PausableOtaConductor()
    for lantern in conductor._lanterns:
        lantern.status = "alive"
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 80
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    )

    assert client.post("/api/operations/ota-install").status_code == 202
    assert conductor.chunk_started.wait(timeout=2)
    paused = client.delete("/api/operations/ota-install")

    assert paused.status_code == 200
    paused_install = paused.json()["install"]
    assert paused_install["running"] is False
    assert paused_install["phase"] == "paused"
    prefix = int(paused_install["bytes_sent"])
    assert 0 < prefix < len(firmware)

    assert client.post("/api/operations/ota-install").status_code == 202
    resumed = wait_for_ota_terminal(client)
    assert resumed["complete"] is True
    assert resumed["bytes_sent"] == len(firmware)


def test_ota_install_restarts_and_repairs_polled_node_failure(tmp_path, managed_client) -> None:
    conductor = ProgressFailedOtaConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 40
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    )

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    assert conductor.ended is False
    install = wait_for_ota_terminal(client)
    assert install["complete"] is True
    assert install["error"] is None
    assert conductor.restart_count == 1
    assert install["repair_chunks"] > 0


def test_ota_install_continues_after_periodic_progress_timeout(tmp_path, managed_client) -> None:
    conductor = ProgressTimeoutConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 40
    stage = client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    ).json()

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert conductor.progress_timeouts == 1
    assert install["complete"] is True
    assert install["error"] is None
    assert install["chunks_sent"] == stage["artifact"]["chunks"]
    assert install["last_retry"]["error"] == "timeout waiting for ota_progress ack"


def test_ota_install_retries_retryable_chunk_nack(tmp_path, managed_client) -> None:
    conductor = NackingOtaChunkConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 3
    stage = client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    ).json()

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert conductor.nacked is True
    assert install["complete"] is True
    assert install["chunks_sent"] == stage["artifact"]["chunks"]
    assert install["last_retry"]["error"] == "ota chunk offset mismatch"


def test_ota_install_retries_chunk_length_mismatch_without_advancing(tmp_path, managed_client) -> None:
    conductor = LengthMismatchOtaChunkConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 3
    stage = client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    ).json()

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert conductor.nacked is True
    assert install["complete"] is True
    assert install["chunks_sent"] == stage["artifact"]["chunks"]
    assert install["bytes_sent"] == stage["artifact"]["size"]
    assert install["last_retry"]["error"] == "ota chunk length mismatch"


def test_ota_install_resyncs_after_already_advanced_chunk_nack(tmp_path, managed_client) -> None:
    conductor = AdvancedThenNackingOtaChunkConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 3
    stage = client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    ).json()

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert conductor.nacked is True
    assert install["complete"] is True
    assert install["chunks_sent"] == stage["artifact"]["chunks"]
    assert install["bytes_sent"] == stage["artifact"]["size"]
    assert install["last_retry"]["error"] == "ota chunk offset mismatch"


def test_ota_install_rejects_unrecoverable_partial_conductor_write(tmp_path, managed_client) -> None:
    conductor = PartialAdvancedNackingOtaChunkConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 3
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    )

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert install["complete"] is False
    assert install["error"] == "ota chunk offset mismatch"


def test_ota_install_waits_for_temporarily_missing_status_reports(tmp_path, managed_client) -> None:
    conductor = NoOtaStatusConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 3
    stage = client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    ).json()

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert install["complete"] is True
    assert {node["phase"] for node in install["nodes"]} == {"complete"}
    assert all(node["offset"] == stage["artifact"]["size"] for node in install["nodes"])


def test_ota_install_treats_final_ack_timeout_as_verify_after_reboot(tmp_path, managed_client) -> None:
    conductor = FinalAckTimeoutConductor()
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 40
    stage = client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    ).json()

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert install["complete"] is True
    assert install["error"] is None
    assert install["last_retry"]["error"] == "timeout waiting for ota_end ack"
    assert install["message"] == "firmware updated across the online field"
    assert install["target_count"] == 9
    assert install["deferred_count"] == 1
    assert {node["source"] for node in install["nodes"]} == {"post_reboot_state"}
    assert {node["offset"] for node in install["nodes"]} == {stage["artifact"]["size"]}


def test_ota_install_surfaces_legacy_conductor_end_failure(tmp_path, managed_client) -> None:
    conductor = EndIncompleteOtaConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 3
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    )

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert install["complete"] is False
    assert install["error"] == "ota performers did not complete"
    assert install["nodes"]
    assert {node["phase"] for node in install["nodes"]} == {"writing"}


def test_ota_install_fails_when_not_all_expected_nodes_verify(
    tmp_path, managed_client, monkeypatch
) -> None:
    monkeypatch.setattr(app_module, "OTA_POST_REBOOT_ATTEMPTS", 1)
    conductor = OneNodeOnlyOtaStatusConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 3
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    )

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert install["complete"] is False
    assert install["error"] == "ota post-reboot verification failed"
    failed = [node for node in install["nodes"] if node["phase"] == "failed"]
    assert {node["mac"] for node in failed} == {"8C:94:DF:8F:71:50"}
    assert {node["error"] for node in failed} == {"post-reboot verification missing"}
    assert {node["source"] for node in failed} == {"post_reboot_verification"}


def test_ota_reservation_protects_artifact_while_field_commands_continue(
    tmp_path, managed_client
) -> None:
    conductor = BlockingOtaConductor()
    for lantern in conductor._lanterns:
        lantern.status = "alive"
    conductor.set_ota_mode(True)
    app = create_app(conductor, ota_store=OtaArtifactStore(tmp_path))
    client = managed_client(app)
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=b"\xe9" + bytes(range(255)) * 3,
        headers={"content-type": "application/octet-stream"},
    )
    accepted = client.post("/api/operations/ota-install")
    assert accepted.status_code == 202
    assert conductor.ota_started.wait(timeout=2)

    duplicate = client.post("/api/operations/ota-install")
    stage = client.put(
        "/api/operations/ota-artifact?filename=other.bin",
        content=b"\xe9\x00",
        headers={"content-type": "application/octet-stream"},
    )
    mode = client.post("/api/operations/ota-mode", json={"enabled": False})
    live_responses = {}

    def request_live_state() -> None:
        live_responses["state"] = client.get("/api/state")

    def request_live_pattern() -> None:
        live_responses["pattern"] = client.post(
            "/api/show/pattern",
            json={"pattern": "Solid", "brightness": 10, "params": {}},
        )

    state_thread = threading.Thread(target=request_live_state)
    pattern_thread = threading.Thread(target=request_live_pattern)
    state_thread.start()
    pattern_thread.start()
    time.sleep(0.05)
    conductor.ota_release.set()
    state_thread.join(timeout=2)
    pattern_thread.join(timeout=2)
    status = client.get("/api/operations/ota-install")

    assert duplicate.status_code == 409
    assert stage.status_code == 423
    assert mode.status_code == 409
    assert live_responses["state"].status_code == 200
    assert live_responses["pattern"].status_code == 200
    assert conductor.pattern["pattern"] == "Solid"
    assert status.status_code == 200

    assert wait_for_ota_terminal(client)["complete"] is True
    assert client.get("/api/state").status_code == 200


def test_calibration_batch_finishes_before_ota_can_reserve_conductor(
    tmp_path, managed_client
) -> None:
    conductor = BlockingAssignmentOtaConductor()
    for lantern in conductor._lanterns:
        lantern.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(
        create_app(conductor, ota_store=OtaArtifactStore(tmp_path))
    )
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=b"\xe9\x00",
        headers={"content-type": "application/octet-stream"},
    )
    results = {}

    calibration = threading.Thread(
        target=lambda: results.update(
            calibration=client.post(
                "/api/calibration/apply-proposal",
                json={
                    "assignments": [
                        {"mac": conductor._lanterns[0].mac, "x": 0.12, "y": 0.34},
                        {"mac": conductor._lanterns[1].mac, "x": 0.56, "y": 0.78},
                    ]
                },
            )
        )
    )
    calibration.start()
    assert conductor.first_assign_started.wait(timeout=2)
    ota = threading.Thread(
        target=lambda: results.update(
            ota=client.post("/api/operations/ota-install")
        )
    )
    ota.start()
    time.sleep(0.1)
    assert ota.is_alive()
    assert conductor.ota_started.is_set() is False

    conductor.assign_release.set()
    calibration.join(timeout=3)
    ota.join(timeout=3)

    assert results["calibration"].status_code == 200
    assert len(results["calibration"].json()["saved"]) == 2
    assert conductor.assign_order == [
        conductor._lanterns[0].mac,
        conductor._lanterns[1].mac,
    ]
    assert results["ota"].status_code == 202
    assert conductor.ota_started.wait(timeout=2)
    conductor.ota_release.set()
    assert wait_for_ota_terminal(client)["complete"] is True


def test_calibration_mode_toggle_finishes_before_ota_can_reserve_conductor(
    tmp_path, managed_client
) -> None:
    conductor = BlockingCalibrationOtaConductor()
    for lantern in conductor._lanterns:
        lantern.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(
        create_app(conductor, ota_store=OtaArtifactStore(tmp_path))
    )
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=b"\xe9\x00",
        headers={"content-type": "application/octet-stream"},
    )
    conductor.block_calibration_snapshot = True
    results = {}
    calibration = threading.Thread(
        target=lambda: results.update(
            calibration=client.post(
                "/api/operations/calibration-mode",
                json={"enabled": True},
            )
        )
    )
    calibration.start()
    assert conductor.calibration_snapshot_started.wait(timeout=2)
    ota = threading.Thread(
        target=lambda: results.update(
            ota=client.post("/api/operations/ota-install")
        )
    )
    ota.start()
    time.sleep(0.1)
    assert ota.is_alive()
    assert conductor.ota_started.is_set() is False

    conductor.calibration_snapshot_release.set()
    calibration.join(timeout=3)
    ota.join(timeout=3)

    assert results["calibration"].status_code == 200
    assert conductor.calibration_pattern_updated.is_set()
    assert results["ota"].status_code == 202
    assert conductor.ota_started.wait(timeout=2)
    conductor.ota_release.set()
    assert wait_for_ota_terminal(client)["complete"] is True


def test_failed_calibration_restore_keeps_previous_pattern_for_retry() -> None:
    app = create_app(RejectingPatternConductor())
    previous = {"pattern": "Sweep", "brightness": 72, "params": {"period": 4000}}
    app.state.calibration_previous_pattern = dict(previous)
    client = TestClient(app)

    response = client.post(
        "/api/operations/calibration-mode",
        json={"enabled": False},
    )

    assert response.status_code == 400
    assert app.state.calibration_previous_pattern == previous


def test_ota_start_returns_before_maintenance_settle_delay(
    tmp_path, managed_client
) -> None:
    conductor = MockConductor()
    for lantern in conductor._lanterns:
        lantern.status = "alive"
    conductor.set_ota_mode(True)
    app = create_app(conductor, ota_store=OtaArtifactStore(tmp_path))
    app.state.ota_mode_started_at = time.time()
    client = managed_client(app)
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=b"\xe9\x00",
        headers={"content-type": "application/octet-stream"},
    )

    started_at = time.monotonic()
    accepted = client.post("/api/operations/ota-install")
    elapsed = time.monotonic() - started_at

    assert accepted.status_code == 202
    assert elapsed < 0.5
    assert accepted.json()["install"]["running"] is True
    assert conductor.ota_installed_crc32 is None


def test_ota_accepts_before_slow_artifact_read_finishes(
    tmp_path, managed_client, monkeypatch
) -> None:
    conductor = MockConductor()
    for lantern in conductor._lanterns:
        lantern.status = "alive"
    conductor.set_ota_mode(True)
    store = OtaArtifactStore(tmp_path)
    client = managed_client(create_app(conductor, ota_store=store))
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=b"\xe9\x00",
        headers={"content-type": "application/octet-stream"},
    )
    artifact = store.artifact()
    assert artifact is not None
    original_read_bytes = type(artifact.path).read_bytes
    read_started = threading.Event()
    read_release = threading.Event()

    def slow_read(path):
        if path == artifact.path:
            read_started.set()
            assert read_release.wait(timeout=3)
        return original_read_bytes(path)

    monkeypatch.setattr(type(artifact.path), "read_bytes", slow_read)
    started_at = time.monotonic()
    accepted = client.post("/api/operations/ota-install")
    elapsed = time.monotonic() - started_at

    assert accepted.status_code == 202
    assert elapsed < 0.5
    assert read_started.wait(timeout=2)
    read_release.set()
    assert wait_for_ota_terminal(client)["complete"] is True


def test_ota_worker_converts_unexpected_exception_to_terminal_state(
    tmp_path, managed_client
) -> None:
    conductor = ExplodingOtaConductor()
    for lantern in conductor._lanterns:
        lantern.status = "alive"
    conductor.set_ota_mode(True)
    client = managed_client(
        create_app(conductor, ota_store=OtaArtifactStore(tmp_path))
    )
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=b"\xe9\x00",
        headers={"content-type": "application/octet-stream"},
    )

    accepted = client.post("/api/operations/ota-install")
    install = wait_for_ota_terminal(client)

    assert accepted.status_code == 202
    assert install["running"] is False
    assert install["complete"] is False
    assert install["error"] == "unexpected worker failure"
    assert install["completed_at"] >= install["started_at"]
    assert client.get("/api/state").status_code == 200


def test_ota_graceful_shutdown_persists_running_job_for_resume(tmp_path) -> None:
    conductor = BlockingOtaConductor()
    for lantern in conductor._lanterns:
        lantern.status = "alive"
    conductor.set_ota_mode(True)
    app = create_app(conductor, ota_store=OtaArtifactStore(tmp_path))

    with TestClient(app) as client:
        client.put(
            "/api/operations/ota-artifact?filename=firmware.bin",
            content=b"\xe9\x00",
            headers={"content-type": "application/octet-stream"},
        )
        assert client.post("/api/operations/ota-install").status_code == 202
        assert conductor.ota_started.wait(timeout=2)
        threading.Timer(0.1, conductor.ota_release.set).start()

    assert app.state.ota_install["running"] is True
    assert app.state.ota_install["complete"] is False
    assert app.state.ota_install["error"] is None
    assert app.state.ota_install["phase"] == "paused"
    assert "resume automatically" in app.state.ota_install["message"]
    assert app.state.ota_reserved is False

    restarted = create_app(conductor, ota_store=OtaArtifactStore(tmp_path))
    with TestClient(restarted) as client:
        resumed = wait_for_ota_terminal(client)
        assert resumed["complete"] is True
        assert resumed["message"] == "firmware updated across the online field"


def test_ota_abrupt_restart_uses_persisted_artifact_and_live_recovery(
    tmp_path, managed_client
) -> None:
    store = OtaArtifactStore(tmp_path)
    staged = store.stage("firmware.bin", b"\xe9\x00")
    conductor = MockConductor()
    for lantern in conductor._lanterns:
        lantern.status = "alive"
    conductor._lanterns[0].firmware = {
        "version": "0.3.0-mismatch",
        "proto": 7,
        "build_id": 0x44D028FD,
        "build_label": "44d028fd",
        "dirty": False,
    }

    restarted = managed_client(
        create_app(conductor, ota_store=OtaArtifactStore(tmp_path))
    )
    artifact = restarted.get("/api/operations/ota-artifact").json()["artifact"]
    install = restarted.get("/api/operations/ota-install").json()["install"]
    recovery = restarted.get("/api/state").json()["recovery"]

    assert artifact["sha256"] == staged["sha256"]
    assert install == {"running": False, "complete": False, "error": None}
    assert recovery["status"] == "mixed_firmware"
    assert recovery["ready"] is False


def test_ota_install_allows_mixed_firmware_recovery_with_legacy_ready_blocker(tmp_path, managed_client) -> None:
    conductor = LegacyMixedFirmwareOtaConductor()
    missing = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    missing.status = "alive"
    conductor._lanterns[0].firmware = {
        "version": "0.3.0-mismatch",
        "proto": 6,
        "build_id": 0x44D028FD,
        "build_label": "44d028fd",
        "dirty": False,
    }
    conductor.set_ota_mode(True)
    client = managed_client(create_app(conductor, ota_store=OtaArtifactStore(tmp_path)))
    firmware = b"\xe9" + bytes(range(255)) * 3
    client.put(
        "/api/operations/ota-artifact?filename=firmware.bin",
        content=firmware,
        headers={"content-type": "application/octet-stream"},
    )

    state = client.get("/api/state").json()
    assert state["recovery"]["status"] == "mixed_firmware"
    assert state["ota"]["ready"] is False
    assert state["ota"]["blocked"] == ["firmware mismatch"]

    response = client.post("/api/operations/ota-install")

    assert response.status_code == 202
    install = wait_for_ota_terminal(client)
    assert install["complete"] is True
    assert install["message"] == "firmware updated across the online field"


def test_assign_endpoint_updates_lantern_position() -> None:
    client = TestClient(create_app(MockConductor()))
    mac = "8C:94:DF:57:7F:14"

    response = client.post(f"/api/lanterns/{mac}/assign", json={"x": 0.2, "y": 0.3})
    lanterns = client.get("/api/lanterns").json()
    lantern = next(item for item in lanterns if item["mac"] == mac)

    assert response.status_code == 200
    assert lantern["position"] == "Set"
    assert lantern["attention"] == "None"


def test_group_endpoint_and_pattern_update_are_independent() -> None:
    conductor = MockConductor()
    client = TestClient(create_app(conductor))
    mac = conductor._lanterns[0].mac

    grouped = client.post(f"/api/lanterns/{mac}/group", json={"group_id": 3})
    changed = client.post(
        "/api/show/pattern",
        json={"pattern": "Sweep", "brightness": 72, "params": {"period": 8000}, "group_id": 3},
    )
    state = client.get("/api/state").json()

    assert grouped.status_code == 200
    assert changed.status_code == 200
    lantern = next(item for item in state["lanterns"] if item["mac"] == mac)
    assert lantern["group_id"] == 3
    assert state["patterns"][3]["config"]["pattern"] == "Sweep"
    assert state["patterns"][0]["config"]["pattern"] == "Glow"


def test_group_endpoint_accepts_unpositioned_lantern() -> None:
    conductor = MockConductor()
    client = TestClient(create_app(conductor))
    mac = "8C:94:DF:57:7F:14"

    grouped = client.post(f"/api/lanterns/{mac}/group", json={"group_id": 5})
    lantern = next(item for item in client.get("/api/lanterns").json() if item["mac"] == mac)

    assert grouped.status_code == 200
    assert lantern["position"] == "Missing"
    assert lantern["group_id"] == 5


def test_led_count_endpoint_accepts_supported_profiles_for_unpositioned_lantern() -> None:
    conductor = MockConductor()
    client = TestClient(create_app(conductor))
    mac = "8C:94:DF:57:7F:14"

    configured = client.post(f"/api/lanterns/{mac}/led-count", json={"led_count": 64})
    rejected = client.post(f"/api/lanterns/{mac}/led-count", json={"led_count": 24})
    lantern = next(item for item in client.get("/api/lanterns").json() if item["mac"] == mac)

    assert configured.status_code == 200
    assert rejected.status_code == 422
    assert lantern["position"] == "Missing"
    assert lantern["led_count"] == 64


def test_group_and_led_count_endpoints_surface_conductor_rejections() -> None:
    conductor = RejectingNodeConfigurationConductor()
    client = TestClient(create_app(conductor))
    mac = conductor._lanterns[0].mac

    grouped = client.post(f"/api/lanterns/{mac}/group", json={"group_id": 2})
    configured = client.post(
        f"/api/lanterns/{mac}/led-count", json={"led_count": 32}
    )

    assert grouped.status_code == 400
    assert grouped.json()["detail"] == "group rejected"
    assert configured.status_code == 400
    assert configured.json()["detail"] == "LED profile rejected"


def test_group_endpoint_surfaces_serial_failures() -> None:
    conductor = FailingGroupSerialConductor()
    client = TestClient(create_app(conductor))

    response = client.post(
        f"/api/lanterns/{conductor._lanterns[0].mac}/group",
        json={"group_id": 2},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "timeout waiting for group ack"


def test_calibration_apply_proposal_saves_assignments_and_skips_uncertain() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.post(
        "/api/calibration/apply-proposal",
        json={
            "assignments": [
                {"mac": "8C:94:DF:57:7F:14", "x": 0.21, "y": 0.31, "code": 1, "bits": "001"},
                {"mac": "8C:94:DF:8F:71:50", "x": 0.62, "y": 0.44, "code": 6, "bits": "110"},
            ],
            "missing": [{"mac": "A0:B7:65:11:42:09", "code": 4, "reason": "not detected"}],
            "ambiguous": [],
        },
    )
    lanterns = client.get("/api/lanterns").json()
    placed = {item["mac"]: item for item in lanterns}

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "saved 2 lantern locations; 1 skipped"
    assert len(body["saved"]) == 2
    assert len(body["skipped"]) == 1
    assert placed["8C:94:DF:57:7F:14"]["x"] == 0.21
    assert placed["8C:94:DF:57:7F:14"]["y"] == 0.31
    assert placed["8C:94:DF:8F:71:50"]["x"] == 0.62
    assert placed["8C:94:DF:8F:71:50"]["y"] == 0.44


def test_calibration_apply_proposal_reports_unknown_lantern_without_blocking_valid_saves() -> None:
    client = TestClient(create_app(MockConductor()))

    response = client.post(
        "/api/calibration/apply-proposal",
        json={
            "assignments": [
                {"mac": "8C:94:DF:57:7F:14", "x": 0.21, "y": 0.31},
                {"mac": "AA:AA:AA:AA:AA:AA", "x": 0.62, "y": 0.44},
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "saved 1 lantern location; 1 failed"
    assert body["saved"][0]["mac"] == "8C:94:DF:57:7F:14"
    assert body["failed"] == [{"mac": "AA:AA:AA:AA:AA:AA", "error": "unknown lantern"}]


def test_replace_endpoint_moves_position_to_spare() -> None:
    client = TestClient(create_app(MockConductor()))
    old_mac = "A0:B7:65:11:44:91"
    new_mac = "8C:94:DF:57:7F:14"

    response = client.post("/api/lanterns/replace", json={"old_mac": old_mac, "new_mac": new_mac})
    lanterns = client.get("/api/lanterns").json()
    old = next(item for item in lanterns if item["mac"] == old_mac)
    new = next(item for item in lanterns if item["mac"] == new_mac)

    assert response.status_code == 200
    assert response.json()["new_mac"] == new_mac
    assert old["position"] == "Missing"
    assert new["position"] == "Set"
    assert new["label"] == "#57"
