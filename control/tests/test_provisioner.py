from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from control.provisioner import (
    HttpIdAuthority,
    PortCandidate,
    ProvisioningManager,
    _default_discover,
    create_provisioner_app,
    validate_station_artifact,
)


MANIFEST = {
    "release": "v0.5.1",
    "version": "0.5.1",
    "commit": "a" * 40,
    "serial_flash": {"sha256": "b" * 64},
}


def approved(_channel: str, cache: Path):
    return MANIFEST, cache / "bundle.zip"


def no_cache(_cache: Path):
    return None


class Discovery:
    def __init__(self, ports: list[PortCandidate]):
        self.ports = ports

    def __call__(self) -> list[PortCandidate]:
        return list(self.ports)


def wait_for(client: TestClient, predicate, timeout_s: float = 2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = client.get("/status").json()
        if predicate(status):
            return status
        time.sleep(0.01)
    raise AssertionError(f"condition not reached; final status={client.get('/status').json()}")


def manager_for(tmp_path: Path, discovery: Discovery, processor, *, resolver=lambda _mac, _reported: (54, True)):
    return ProvisioningManager(
        tmp_path,
        discover=discovery,
        processor=processor,
        id_resolver=resolver,
        artifact_loader=no_cache,
        artifact_refresher=approved,
        artifact_validator=lambda artifact, _state: artifact,
        poll_interval_s=0.01,
        refresh_interval_s=3600,
    )


def test_unmapped_port_waits_until_operator_assigns_physical_slot(tmp_path: Path) -> None:
    discovery = Discovery([PortCandidate("/dev/ttyUSB0", "1-1.2", "USB VID:PID=1A86:7522")])
    calls = []

    def processor(port, *_args, progress, **_kwargs):
        calls.append(port)
        progress("flashing", "Writing firmware")
        return "AA:BB:CC:DD:EE:FF flashed build aaaaaaaa; permanent ID #54 verified"

    app = create_provisioner_app(manager_for(tmp_path, discovery, processor))
    with TestClient(app) as client:
        status = wait_for(client, lambda value: len(value["jobs"]) == 1)
        job = status["jobs"][0]
        assert job["state"] == "unmapped"
        assert "/dev/" not in str(status)

        client.post("/session", json={"max_workers": 5, "factory": False}).raise_for_status()
        time.sleep(0.05)
        assert calls == []

        client.put("/slots", json={"port_id": job["port_id"], "slot": 3}).raise_for_status()
        completed = wait_for(client, lambda value: value["jobs"][0]["state"] == "done")

    assert calls == ["/dev/ttyUSB0"]
    assert completed["jobs"][0]["slot"] == 3
    assert completed["jobs"][0]["node_id"] == 54


def test_worker_limit_bounds_parallel_flashes(tmp_path: Path) -> None:
    ports = [
        PortCandidate(f"/dev/ttyUSB{index}", f"1-1.{index + 1}", "wch")
        for index in range(6)
    ]
    discovery = Discovery(ports)
    release = threading.Event()
    entered: list[str] = []
    entered_lock = threading.Lock()

    def processor(port, *_args, progress, **_kwargs):
        with entered_lock:
            entered.append(port)
        progress("flashing", "Writing firmware")
        release.wait(2)
        return f"AA:BB:CC:DD:EE:{int(port[-1]):02X} flashed; permanent ID #54 verified"

    manager = manager_for(tmp_path, discovery, processor)
    manager._slot_map = {port.location: index + 1 for index, port in enumerate(ports)}
    app = create_provisioner_app(manager)
    try:
        with TestClient(app) as client:
            wait_for(client, lambda value: len(value["jobs"]) == 6)
            client.post("/session", json={"max_workers": 5, "factory": False}).raise_for_status()
            wait_for(client, lambda value: value["running"] == 5)
            with entered_lock:
                assert len(entered) == 5
            release.set()
            wait_for(client, lambda value: sum(job["state"] == "done" for job in value["jobs"]) == 6)
    finally:
        release.set()


def test_factory_authorization_is_session_scoped_and_retry_recovers(tmp_path: Path) -> None:
    discovery = Discovery([PortCandidate("/dev/ttyUSB0", "1-1.2", "wch")])
    attempts = 0
    factory_values: list[bool] = []

    def processor(_port, *_args, factory_authorized, **_kwargs):
        nonlocal attempts
        attempts += 1
        factory_values.append(factory_authorized)
        if attempts == 1:
            raise RuntimeError("serial cable disconnected")
        return "AA:BB:CC:DD:EE:FF flashed; permanent ID #54 verified"

    manager = manager_for(tmp_path, discovery, processor)
    manager._slot_map = {"1-1.2": 1}
    app = create_provisioner_app(manager)
    with TestClient(app) as client:
        wait_for(client, lambda value: len(value["jobs"]) == 1)
        armed = client.post("/session", json={"max_workers": 1, "factory": True}).json()
        assert armed["session"]["factory_armed"] is True
        failed = wait_for(client, lambda value: value["jobs"][0]["state"] == "failed")
        job_id = failed["jobs"][0]["id"]
        client.post(f"/jobs/{job_id}/retry").raise_for_status()
        wait_for(client, lambda value: value["jobs"][0]["state"] == "done")

    assert factory_values == [True, True]


def test_missing_id_authority_fails_closed(tmp_path: Path) -> None:
    discovery = Discovery([PortCandidate("/dev/ttyUSB0", "1-1.2", "wch")])
    manager = manager_for(tmp_path, discovery, lambda *_args, **_kwargs: "unused", resolver=None)
    manager._slot_map = {"1-1.2": 1}
    app = create_provisioner_app(manager)
    with TestClient(app) as client:
        wait_for(client, lambda value: len(value["jobs"]) == 1)
        response = client.post("/session", json={"max_workers": 1})

    assert response.status_code == 503
    assert response.json()["detail"] == "Permanent-ID authority is unavailable"


def test_slot_mapping_persists_without_raw_device_path(tmp_path: Path) -> None:
    discovery = Discovery([PortCandidate("/dev/ttyUSB7", "2-3.4", "wch")])
    manager = manager_for(tmp_path, discovery, lambda *_args, **_kwargs: "unused")
    app = create_provisioner_app(manager)
    with TestClient(app) as client:
        status = wait_for(client, lambda value: len(value["jobs"]) == 1)
        client.put(
            "/slots",
            json={"port_id": status["jobs"][0]["port_id"], "slot": 7},
        ).raise_for_status()

    restarted = manager_for(tmp_path, discovery, lambda *_args, **_kwargs: "unused")
    assert restarted._slot_map == {"2-3.4": 7}
    assert "/dev/ttyUSB7" not in (tmp_path / "station.json").read_text()


def test_job_history_persists_and_interrupted_work_fails_on_restart(tmp_path: Path) -> None:
    discovery = Discovery([PortCandidate("/dev/ttyUSB7", "2-3.4", "wch")])
    manager = manager_for(tmp_path, discovery, lambda *_args, **_kwargs: "unused")
    manager._apply_ports(discovery())
    job = next(iter(manager._jobs.values()))
    job.state = "flashing"
    job.message = "Writing firmware"
    manager._save_jobs()

    restarted = manager_for(tmp_path, Discovery([]), lambda *_args, **_kwargs: "unused")
    restored = restarted.status()["jobs"][0]

    assert restored["state"] == "failed"
    assert restored["connected"] is False
    assert restored["error"] == "Provisioner restarted during operation"
    assert "/dev/ttyUSB7" not in (tmp_path / "jobs.json").read_text()


def test_duplicate_physical_slot_is_rejected(tmp_path: Path) -> None:
    discovery = Discovery(
        [
            PortCandidate("/dev/ttyUSB0", "1-1.1", "wch"),
            PortCandidate("/dev/ttyUSB1", "1-1.2", "wch"),
        ]
    )
    app = create_provisioner_app(
        manager_for(tmp_path, discovery, lambda *_args, **_kwargs: "unused")
    )
    with TestClient(app) as client:
        jobs = wait_for(client, lambda value: len(value["jobs"]) == 2)["jobs"]
        client.put("/slots", json={"port_id": jobs[0]["port_id"], "slot": 4}).raise_for_status()
        response = client.put(
            "/slots", json={"port_id": jobs[1]["port_id"], "slot": 4}
        )

    assert response.status_code == 409
    assert "already assigned" in response.json()["detail"]


def test_id_authority_rejects_plaintext_remote_url() -> None:
    with pytest.raises(ValueError, match="HTTPS or loopback"):
        HttpIdAuthority("http://example.com/reserve", "secret")


def test_station_artifact_requires_usable_flash_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("control.provisioner.extract_bundle", lambda *_args: {"schema_version": 1})
    monkeypatch.setattr(
        "control.provisioner.esptool_command",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("missing")),
    )

    with pytest.raises(RuntimeError, match="flash-plan schema 2"):
        validate_station_artifact((MANIFEST, tmp_path / "legacy.zip"), tmp_path)


def test_default_discovery_excludes_configured_conductor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conductor = tmp_path / "ttyUSB0"
    performer = tmp_path / "ttyUSB1"
    conductor.touch()
    performer.touch()
    alias = tmp_path / "conductor-by-path"
    alias.symlink_to(conductor)
    monkeypatch.setenv("CONTROL_SERIAL_PORT", str(alias))
    monkeypatch.setattr(
        "control.provisioner.candidate_port_infos",
        lambda: [
            PortCandidate(str(conductor), "1-1.1", "wch"),
            PortCandidate(str(performer), "1-1.2", "wch"),
        ],
    )

    assert _default_discover() == [PortCandidate(str(performer), "1-1.2", "wch")]
