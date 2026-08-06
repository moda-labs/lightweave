from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from control.auth import AuthManager
from control.app import create_app
from control.mock_conductor import MockConductor
from control.remote_config import RemoteSettings


VALID_HASH = (
    "scrypt$n=131072,r=8,p=1$"
    "AAAAAAAAAAAAAAAAAAAAAA$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


STATUS = {
    "available": True,
    "revision": 7,
    "session": {
        "active": False,
        "auto_update_enabled": False,
        "max_workers": 5,
    },
    "artifact": {
        "release": "v0.5.1",
        "version": "0.5.1",
        "build": "aaaaaaaa",
        "sha256": "b" * 64,
    },
    "artifact_error": None,
    "connected": 1,
    "running": 0,
    "jobs": [
        {
            "id": "1" * 32,
            "port_id": "2" * 16,
            "slot": 1,
            "state": "queued",
            "message": "Waiting to flash",
            "connected": True,
            "created_at": 1.0,
            "updated_at": 1.0,
            "mac": None,
            "node_id": None,
            "error": None,
            "role": None,
            "firmware_version": None,
            "firmware_build": None,
            "firmware_proto": None,
            "firmware_dirty": None,
            "update_status": "unknown",
        }
    ],
}


class FakeProvisioningClient:
    def __init__(self):
        self.value = deepcopy(STATUS)
        self.calls = []

    async def status(self):
        return deepcopy(self.value)

    async def start_session(self, *, max_workers: int, factory: bool):
        self.calls.append(("start", max_workers, factory))
        self.value["revision"] += 1
        self.value["session"].update(
            {
                "active": True,
                "auto_update_enabled": True,
                "max_workers": max_workers,
            }
        )
        return deepcopy(self.value)

    async def stop_session(self):
        self.calls.append(("stop",))
        self.value["revision"] += 1
        self.value["session"].update(
            {"active": False, "auto_update_enabled": False}
        )
        return deepcopy(self.value)

    async def enable_auto_update(self, *, max_workers: int):
        self.calls.append(("enable_auto_update", max_workers))
        self.value["revision"] += 1
        self.value["session"].update(
            {
                "active": True,
                "auto_update_enabled": True,
                "max_workers": max_workers,
            }
        )
        return deepcopy(self.value)

    async def disable_auto_update(self):
        self.calls.append(("disable_auto_update",))
        self.value["revision"] += 1
        self.value["session"].update(
            {"active": False, "auto_update_enabled": False}
        )
        return deepcopy(self.value)

    async def install(self, job_id: str):
        self.calls.append(("install", job_id))
        return deepcopy(self.value)

    async def map_slot(self, *, port_id: str, slot: int):
        self.calls.append(("slot", port_id, slot))
        self.value["jobs"][0]["slot"] = slot
        return deepcopy(self.value)

    async def retry(self, job_id: str):
        self.calls.append(("retry", job_id))
        return deepcopy(self.value)


def serial_app(
    conductor: MockConductor,
    provisioner: FakeProvisioningClient | None = None,
):
    settings = RemoteSettings(
        conductor_mode="serial",
        password_hash=VALID_HASH,
        allowed_origins=frozenset({"https://control.example.test"}),
        allow_network_changes=False,
        require_https=True,
        data_dir=Path("/tmp/lightweave-provisioning-api-tests"),
    )
    return create_app(
        conductor=conductor,
        auth_manager=AuthManager.from_encoded_hash(VALID_HASH),
        settings=settings,
        provisioning_client=provisioner or FakeProvisioningClient(),
    )


def test_provisioning_api_proxies_only_validated_station_operations() -> None:
    provisioner = FakeProvisioningClient()
    with TestClient(create_app(provisioning_client=provisioner)) as client:
        assert client.get("/api/provisioning/status").json()["revision"] == 7
        enabled = client.put(
            "/api/provisioning/auto-update",
            json={"max_workers": 10},
        )
        assert enabled.status_code == 200
        assert enabled.json()["session"]["auto_update_enabled"] is True
        assert client.put(
            "/api/provisioning/slots",
            json={"port_id": "2" * 16, "slot": 4},
        ).status_code == 200
        assert client.post(f"/api/provisioning/jobs/{'1' * 32}/install").status_code == 200
        assert client.delete("/api/provisioning/auto-update").status_code == 200

    assert provisioner.calls == [
        ("enable_auto_update", 10),
        ("slot", "2" * 16, 4),
        ("install", "1" * 32),
        ("disable_auto_update",),
    ]


def test_provisioning_api_rejects_arbitrary_job_path_before_agent_call() -> None:
    provisioner = FakeProvisioningClient()
    with TestClient(create_app(provisioning_client=provisioner)) as client:
        response = client.post("/api/provisioning/jobs/../../etc/passwd/retry")

    assert response.status_code == 404
    assert provisioner.calls == []


def test_websocket_publishes_station_session_change() -> None:
    provisioner = FakeProvisioningClient()
    with TestClient(create_app(provisioning_client=provisioner)) as client:
        with client.websocket_connect("/ws") as websocket:
            response = client.put(
                "/api/provisioning/auto-update",
                json={"max_workers": 5},
            )
            assert response.status_code == 200
            event = websocket.receive_json()
            while event.get("type") != "provisioning":
                event = websocket.receive_json()

    assert event["type"] == "provisioning"
    assert event["provisioning"]["session"]["auto_update_enabled"] is True


def test_websocket_publishes_auto_update_change_without_revision_change() -> None:
    provisioner = FakeProvisioningClient()
    provisioner.value["session"]["auto_update_enabled"] = True
    with TestClient(create_app(provisioning_client=provisioner)) as client:
        with client.websocket_connect("/ws") as websocket:
            event = websocket.receive_json()
            while not (
                event.get("type") == "provisioning"
                and event["provisioning"]["session"]["auto_update_enabled"] is True
            ):
                event = websocket.receive_json()
            provisioner.value["session"]["auto_update_enabled"] = False
            expired = websocket.receive_json()
            while expired.get("type") != "provisioning":
                expired = websocket.receive_json()

    assert expired["provisioning"]["revision"] == STATUS["revision"]
    assert expired["provisioning"]["session"]["auto_update_enabled"] is False


def test_internal_id_authority_requires_scoped_bearer_token(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTROL_PROVISIONER_TOKEN", "station-secret-token")
    conductor = MockConductor()
    with TestClient(serial_app(conductor)) as client:
        path = "/api/internal/provisioning/reserve-id"
        body = {"mac": "AA:BB:CC:DD:EE:FF", "reported_id": 53}
        assert client.post(path, json=body).status_code == 401
        assert client.post(
            path,
            json=body,
            headers={"Authorization": "Bearer wrong"},
        ).status_code == 401
        accepted = client.post(
            path,
            json=body,
            headers={"Authorization": "Bearer station-secret-token"},
        )

    assert accepted.status_code == 200
    assert accepted.json() == {
        "mac": "AA:BB:CC:DD:EE:FF",
        "node_id": 53,
        "created": True,
    }


def test_internal_id_authority_authenticates_before_reading_or_parsing_body(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTROL_PROVISIONER_TOKEN", "station-secret-token")
    with TestClient(serial_app(MockConductor())) as client:
        unauthenticated = client.post(
            "/api/internal/provisioning/reserve-id",
            content=b"{" + b"x" * 4096,
        )
        oversized = client.post(
            "/api/internal/provisioning/reserve-id",
            content=b"{" + b"x" * 4096,
            headers={"Authorization": "Bearer station-secret-token"},
        )

    assert unauthenticated.status_code == 401
    assert oversized.status_code == 413


def test_internal_id_authority_is_disabled_outside_durable_serial_mode(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTROL_PROVISIONER_TOKEN", "station-secret-token")
    with TestClient(create_app(MockConductor())) as client:
        response = client.post(
            "/api/internal/provisioning/reserve-id",
            json={"mac": "AA:BB:CC:DD:EE:FF", "reported_id": 0},
            headers={"Authorization": "Bearer station-secret-token"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "provisioner ID authority requires serial mode"


def test_internal_id_reservations_do_not_force_full_inventory_snapshots(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTROL_PROVISIONER_TOKEN", "station-secret-token")

    class CountingConductor(MockConductor):
        snapshot_calls = 0

        def snapshot(self):
            self.snapshot_calls += 1
            return super().snapshot()

    conductor = CountingConductor()
    with TestClient(serial_app(conductor)) as client:
        response = client.post(
            "/api/internal/provisioning/reserve-id",
            json={"mac": "AA:BB:CC:DD:EE:FF", "reported_id": 53},
            headers={"Authorization": "Bearer station-secret-token"},
        )

    assert response.status_code == 200
    assert conductor.snapshot_calls == 0


def test_concurrent_internal_reservations_receive_unique_permanent_ids(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTROL_PROVISIONER_TOKEN", "station-secret-token")
    conductor = MockConductor()
    headers = {"Authorization": "Bearer station-secret-token"}
    with TestClient(serial_app(conductor)) as client:
        with ThreadPoolExecutor(max_workers=10) as pool:
            responses = list(
                pool.map(
                    lambda suffix: client.post(
                        "/api/internal/provisioning/reserve-id",
                        json={
                            "mac": f"AA:BB:CC:DD:EE:{suffix:02X}",
                            "reported_id": 0,
                        },
                        headers=headers,
                    ),
                    range(10),
                )
            )

    assert all(response.status_code == 200 for response in responses)
    ids = [response.json()["node_id"] for response in responses]
    assert len(ids) == len(set(ids)) == 10


def test_internal_id_authority_surfaces_conductor_conflict(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_PROVISIONER_TOKEN", "station-secret-token")
    conductor = MockConductor()
    conductor.reserve_id("AA:BB:CC:DD:EE:01", 53)
    with TestClient(serial_app(conductor)) as client:
        response = client.post(
            "/api/internal/provisioning/reserve-id",
            json={"mac": "AA:BB:CC:DD:EE:02", "reported_id": 53},
            headers={"Authorization": "Bearer station-secret-token"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "permanent ID conflict"


def test_serial_health_requires_the_provisioner_to_be_available(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_PROVISIONER_TOKEN", "station-secret-token")
    provisioner = FakeProvisioningClient()
    with TestClient(serial_app(MockConductor(), provisioner)) as client:
        healthy = client.get("/api/health")
        provisioner.value["available"] = False
        unhealthy = client.get("/api/health")

    assert healthy.status_code == 200
    assert healthy.json()["provisioner"] == {"available": True}
    assert unhealthy.status_code == 503
    assert unhealthy.json()["ok"] is False
