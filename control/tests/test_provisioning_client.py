from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import uvicorn

from control.provisioner import create_provisioner_app
from control.provisioning_client import UnixProvisioningClient


class FakeManager:
    def __init__(self):
        self.active = False

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def status(self):
        return {
            "available": True,
            "revision": 1,
            "session": {
                "active": self.active,
                "auto_update_enabled": self.active,
                "max_workers": 5,
            },
            "artifact": None,
            "artifact_error": None,
            "connected": 0,
            "running": 0,
            "jobs": [],
        }

    def start_session(self, *, max_workers: int, factory: bool):
        self.active = True
        value = self.status()
        value["session"]["max_workers"] = max_workers
        return value

    def stop_session(self):
        self.active = False
        return self.status()

    def enable_auto_update(self, *, max_workers: int):
        self.active = True
        value = self.status()
        value["session"]["max_workers"] = max_workers
        return value

    def disable_auto_update(self):
        self.active = False
        return self.status()

    def install(self, job_id: str):
        value = self.status()
        value["installed_job"] = job_id
        return value

    def map_slot(self, *, port_id: str, slot: int):
        return self.status()

    def retry(self, job_id: str):
        return self.status()


def test_unix_socket_client_reaches_isolated_provisioner_daemon() -> None:
    async def exercise() -> None:
        socket = Path(tempfile.gettempdir()) / f"lw-provisioner-{id(exercise)}.sock"
        socket.unlink(missing_ok=True)
        server = uvicorn.Server(
            uvicorn.Config(
                create_provisioner_app(FakeManager()),
                uds=str(socket),
                log_level="error",
                lifespan="on",
            )
        )
        server.install_signal_handlers = lambda: None
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started and socket.exists():
                    break
                await asyncio.sleep(0.01)
            client = UnixProvisioningClient(socket)
            assert (await client.status())["available"] is True
            enabled = await client.enable_auto_update(max_workers=8)
            assert enabled["session"] == {
                "active": True,
                "auto_update_enabled": True,
                "max_workers": 8,
            }
            installed = await client.install("a" * 32)
            assert installed["installed_job"] == "a" * 32
        finally:
            server.should_exit = True
            await task
            socket.unlink(missing_ok=True)

    asyncio.run(exercise())
