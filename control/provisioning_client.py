from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import httpx


class ProvisioningClient(Protocol):
    async def status(self) -> dict[str, Any]: ...
    async def enable_auto_update(self, *, max_workers: int) -> dict[str, Any]: ...
    async def disable_auto_update(self) -> dict[str, Any]: ...
    async def install(self, job_id: str) -> dict[str, Any]: ...
    async def start_session(self, *, max_workers: int, factory: bool) -> dict[str, Any]: ...
    async def stop_session(self) -> dict[str, Any]: ...
    async def map_slot(self, *, port_id: str, slot: int) -> dict[str, Any]: ...
    async def retry(self, job_id: str) -> dict[str, Any]: ...


class ProvisioningClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


class UnixProvisioningClient:
    def __init__(self, socket_path: Path, *, timeout_s: float = 10.0):
        self.socket_path = socket_path
        self.timeout_s = timeout_s

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.socket_path.exists():
            raise ProvisioningClientError("USB provisioner is not running")
        transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://lightweave-provisioner",
                timeout=self.timeout_s,
            ) as client:
                response = await client.request(method, path, json=json)
        except (httpx.HTTPError, OSError) as error:
            raise ProvisioningClientError(f"USB provisioner unavailable: {error}") from error
        if response.is_error:
            try:
                detail = response.json().get("detail", response.reason_phrase)
            except ValueError:
                detail = response.reason_phrase
            raise ProvisioningClientError(str(detail), status_code=response.status_code)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ProvisioningClientError("USB provisioner returned an invalid response")
        return payload

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/status")

    async def enable_auto_update(self, *, max_workers: int) -> dict[str, Any]:
        return await self._request(
            "PUT",
            "/auto-update",
            json={"max_workers": max_workers},
        )

    async def disable_auto_update(self) -> dict[str, Any]:
        return await self._request("DELETE", "/auto-update")

    async def install(self, job_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/jobs/{job_id}/install")

    async def start_session(self, *, max_workers: int, factory: bool) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/session",
            json={"max_workers": max_workers, "factory": factory},
        )

    async def stop_session(self) -> dict[str, Any]:
        return await self._request("DELETE", "/session")

    async def map_slot(self, *, port_id: str, slot: int) -> dict[str, Any]:
        return await self._request(
            "PUT",
            "/slots",
            json={"port_id": port_id, "slot": slot},
        )

    async def retry(self, job_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/jobs/{job_id}/retry")


class UnavailableProvisioningClient:
    async def status(self) -> dict[str, Any]:
        return {
            "available": False,
            "revision": 0,
            "session": {
                "active": False,
                "auto_update_enabled": False,
                "max_workers": 5,
            },
            "artifact": None,
            "artifact_error": "USB provisioner is not configured on this host",
            "connected": 0,
            "running": 0,
            "jobs": [],
        }

    async def _unavailable(self) -> dict[str, Any]:
        raise ProvisioningClientError("USB provisioner is not configured on this host")

    async def enable_auto_update(self, *, max_workers: int) -> dict[str, Any]:
        return await self._unavailable()

    async def disable_auto_update(self) -> dict[str, Any]:
        return await self._unavailable()

    async def install(self, job_id: str) -> dict[str, Any]:
        return await self._unavailable()

    async def start_session(self, *, max_workers: int, factory: bool) -> dict[str, Any]:
        return await self._unavailable()

    async def stop_session(self) -> dict[str, Any]:
        return await self._unavailable()

    async def map_slot(self, *, port_id: str, slot: int) -> dict[str, Any]:
        return await self._unavailable()

    async def retry(self, job_id: str) -> dict[str, Any]:
        return await self._unavailable()
