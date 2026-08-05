from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient

from control.app import create_app
from control.mock_conductor import MockConductor


STATUS = {
    "available": True,
    "revision": 7,
    "session": {
        "active": False,
        "max_workers": 5,
        "factory_armed": False,
        "factory_expires_at": None,
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
            {"active": True, "max_workers": max_workers, "factory_armed": factory}
        )
        return deepcopy(self.value)

    async def stop_session(self):
        self.calls.append(("stop",))
        self.value["revision"] += 1
        self.value["session"].update({"active": False, "factory_armed": False})
        return deepcopy(self.value)

    async def map_slot(self, *, port_id: str, slot: int):
        self.calls.append(("slot", port_id, slot))
        self.value["jobs"][0]["slot"] = slot
        return deepcopy(self.value)

    async def retry(self, job_id: str):
        self.calls.append(("retry", job_id))
        return deepcopy(self.value)


def test_provisioning_api_proxies_only_validated_station_operations() -> None:
    provisioner = FakeProvisioningClient()
    with TestClient(create_app(provisioning_client=provisioner)) as client:
        assert client.get("/api/provisioning/status").json()["revision"] == 7
        started = client.post(
            "/api/provisioning/session",
            json={"max_workers": 10, "factory": True},
        )
        assert started.status_code == 200
        assert started.json()["session"]["factory_armed"] is True
        assert client.put(
            "/api/provisioning/slots",
            json={"port_id": "2" * 16, "slot": 4},
        ).status_code == 200
        assert client.post(f"/api/provisioning/jobs/{'1' * 32}/retry").status_code == 200
        assert client.delete("/api/provisioning/session").status_code == 200

    assert provisioner.calls == [
        ("start", 10, True),
        ("slot", "2" * 16, 4),
        ("retry", "1" * 32),
        ("stop",),
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
            response = client.post(
                "/api/provisioning/session",
                json={"max_workers": 5, "factory": False},
            )
            assert response.status_code == 200
            event = websocket.receive_json()
            while event.get("type") != "provisioning":
                event = websocket.receive_json()

    assert event["type"] == "provisioning"
    assert event["provisioning"]["session"]["active"] is True


def test_internal_id_authority_requires_scoped_bearer_token(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONTROL_PROVISIONER_TOKEN", "station-secret-token")
    conductor = MockConductor()
    with TestClient(create_app(conductor=conductor)) as client:
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


def test_internal_id_authority_surfaces_conductor_conflict(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_PROVISIONER_TOKEN", "station-secret-token")
    conductor = MockConductor()
    conductor.reserve_id("AA:BB:CC:DD:EE:01", 53)
    with TestClient(create_app(conductor=conductor)) as client:
        response = client.post(
            "/api/internal/provisioning/reserve-id",
            json={"mac": "AA:BB:CC:DD:EE:02", "reported_id": 53},
            headers={"Authorization": "Bearer station-secret-token"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "permanent ID conflict"
