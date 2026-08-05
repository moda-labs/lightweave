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
                "max_workers": 5,
                "factory_armed": False,
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
        value["session"].update(
            {"max_workers": max_workers, "factory_armed": factory}
        )
        return value

    def stop_session(self):
        self.active = False
        return self.status()

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
            started = await client.start_session(max_workers=8, factory=True)
            assert started["session"] == {
                "active": True,
                "max_workers": 8,
                "factory_armed": True,
            }
        finally:
            server.should_exit = True
            await task
            socket.unlink(missing_ok=True)

    asyncio.run(exercise())
