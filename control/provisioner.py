from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from scripts.firebeetle_autoflash import (
    DEFAULT_CHANNEL,
    DeviceInfo,
    PortCandidate,
    approved_build,
    candidate_port_infos,
    esptool_command,
    extract_bundle,
    load_cached_artifact,
    process_port,
    read_info,
    refresh_artifact,
    run_tool,
    should_skip,
)


TERMINAL_STATES = {"done", "failed", "disconnected", "unsupported"}
ACTIVE_STATES = {
    "inspecting",
    "probing",
    "reserving_id",
    "preparing",
    "erasing",
    "flashing",
    "rebooting",
    "verifying",
    "assigning_id",
}
ID_RE = re.compile(r"permanent ID #(\d+)")


def validate_station_artifact(
    approved: tuple[dict[str, Any], Path], state_dir: Path
) -> tuple[dict[str, Any], Path]:
    manifest, bundle = approved
    destination = state_dir / "preflight" / approved_build(manifest)
    extract_bundle(bundle, destination)
    try:
        run_tool(esptool_command(destination) + ["version"], timeout_s=30)
    except RuntimeError as error:
        raise RuntimeError(
            "approved production serial bundle does not include a usable flashing runtime; "
            "promote a release with the complete bundled flashing runtime"
        ) from error
    return approved


@dataclass
class ProvisioningJob:
    id: str
    port_id: str
    slot: int | None
    state: str
    message: str
    connected: bool
    created_at: float
    updated_at: float
    mac: str | None = None
    node_id: int | None = None
    error: str | None = None
    role: str | None = None
    firmware_version: str | None = None
    firmware_build: str | None = None
    firmware_proto: int | None = None
    firmware_dirty: bool | None = None
    update_status: str = "unknown"


class SessionRequest(BaseModel):
    max_workers: int = Field(default=5, ge=1, le=10)
    factory: bool = False


class SlotRequest(BaseModel):
    port_id: str = Field(min_length=8, max_length=32)
    slot: int = Field(ge=1, le=32)


class HttpIdAuthority:
    def __init__(self, url: str, token: str, timeout_s: float = 12.0):
        parsed = urlsplit(url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("ID authority must use HTTPS or loopback HTTP")
        if not token:
            raise ValueError("ID authority token is required")
        self.url = url
        self.token = token
        self.timeout_s = timeout_s

    def reserve(self, mac: str, reported_id: int) -> tuple[int, bool]:
        body = json.dumps({"mac": mac, "reported_id": reported_id}).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "lightweave-provisioner/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read(64 * 1024))
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read(64 * 1024))
                detail = payload.get("detail", error.reason)
            except Exception:
                detail = error.reason
            raise RuntimeError(f"ID authority rejected board: {detail}") from error
        except (OSError, ValueError) as error:
            raise RuntimeError(f"ID authority unavailable: {error}") from error
        node_id = payload.get("node_id") if isinstance(payload, dict) else None
        created = payload.get("created") if isinstance(payload, dict) else None
        if not isinstance(node_id, int) or isinstance(node_id, bool) or not 1 <= node_id <= 65535:
            raise RuntimeError("ID authority returned an invalid permanent ID")
        if not isinstance(created, bool):
            raise RuntimeError("ID authority returned an invalid reservation result")
        return node_id, created


class ProvisioningManager:
    def __init__(
        self,
        state_dir: Path,
        *,
        channel: str = DEFAULT_CHANNEL,
        discover: Callable[[], list[PortCandidate]] = candidate_port_infos,
        processor: Callable[..., str] = process_port,
        inspector: Callable[[str], DeviceInfo | None] = read_info,
        id_resolver: Callable[[str, int], tuple[int, bool]] | None = None,
        artifact_loader: Callable[[Path], tuple[dict[str, Any], Path] | None] = load_cached_artifact,
        artifact_refresher: Callable[[str, Path], tuple[dict[str, Any], Path] | None] = refresh_artifact,
        artifact_validator: Callable[
            [tuple[dict[str, Any], Path], Path], tuple[dict[str, Any], Path]
        ] = validate_station_artifact,
        clock: Callable[[], float] = time.time,
        poll_interval_s: float = 1.0,
        refresh_interval_s: float = 300.0,
        operation_lock_path: Path | None = None,
    ):
        self.state_dir = state_dir
        self.channel = channel
        self.discover = discover
        self.processor = processor
        self.inspector = inspector
        self.id_resolver = id_resolver
        self.artifact_loader = artifact_loader
        self.artifact_refresher = artifact_refresher
        self.artifact_validator = artifact_validator
        self.clock = clock
        self.poll_interval_s = poll_interval_s
        self.refresh_interval_s = refresh_interval_s
        self.operation_lock_path = operation_lock_path
        self._lock = threading.RLock()
        self._runner: asyncio.Task[None] | None = None
        self._job_tasks: dict[str, asyncio.Task[None]] = {}
        self._job_devices: dict[str, str] = {}
        self._inspection_tasks: dict[str, asyncio.Task[None]] = {}
        self._inspection_devices: dict[str, str] = {}
        self._ports: dict[str, PortCandidate] = {}
        self._jobs: dict[str, ProvisioningJob] = {}
        self._port_job: dict[str, str] = {}
        self._slot_map: dict[str, int] = {}
        self._approved: tuple[dict[str, Any], Path] | None = None
        self._artifact_error: str | None = None
        self._last_refresh = 0.0
        self._active = False
        self._max_workers = 5
        self._factory_until = 0.0
        self._revision = 0
        self._load_config()
        self._load_jobs()

    def _identity_key(self, port: PortCandidate) -> str:
        if not port.location:
            raise ValueError("USB topology is unavailable for this port")
        return port.location

    def _port_id(self, port: PortCandidate) -> str:
        identity = port.location or f"unsupported:{port.hardware_id}"
        return hashlib.sha256(identity.encode()).hexdigest()[:16]

    def _load_config(self) -> None:
        path = self.state_dir / "station.json"
        if not path.is_file():
            return
        document = json.loads(path.read_text(encoding="utf-8"))
        slots = document.get("slots") if isinstance(document, dict) else None
        if not isinstance(slots, dict):
            raise ValueError("provisioning station config is invalid")
        parsed: dict[str, int] = {}
        for key, value in slots.items():
            if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool):
                raise ValueError("provisioning slot map is invalid")
            if not 1 <= value <= 32 or value in parsed.values():
                raise ValueError("provisioning slot map contains invalid or duplicate slots")
            parsed[key] = value
        self._slot_map = parsed

    def _save_config(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "station.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"schema_version": 1, "slots": self._slot_map}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def _load_jobs(self) -> None:
        path = self.state_dir / "jobs.json"
        if not path.is_file():
            return
        document = json.loads(path.read_text(encoding="utf-8"))
        records = document.get("jobs") if isinstance(document, dict) else None
        if not isinstance(records, list):
            raise ValueError("provisioning job history is invalid")
        recovered = False
        for record in records[-100:]:
            if not isinstance(record, dict):
                raise ValueError("provisioning job history contains an invalid record")
            try:
                job = ProvisioningJob(**record)
            except (TypeError, ValueError) as error:
                raise ValueError("provisioning job history contains an invalid record") from error
            job.connected = False
            if job.state not in TERMINAL_STATES:
                recovered = True
                job.state = "failed"
                job.error = "Provisioner restarted during operation"
                job.message = "Provisioner restarted during operation; reconnect and retry"
                job.updated_at = self.clock()
            self._jobs[job.id] = job
        if recovered:
            self._save_jobs()

    def _save_jobs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "jobs.json"
        temporary = path.with_suffix(".tmp")
        jobs = sorted(self._jobs.values(), key=lambda item: item.created_at)[-100:]
        temporary.write_text(
            json.dumps(
                {"schema_version": 1, "jobs": [asdict(job) for job in jobs]},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    async def start(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            cached = self.artifact_loader(self.state_dir / "cache")
            self._approved = (
                self.artifact_validator(cached, self.state_dir) if cached else None
            )
        except Exception as error:
            self._artifact_error = str(error)
        await self._refresh_artifact(force=True)
        self._runner = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
        active = list(self._job_tasks.values()) + list(self._inspection_tasks.values())
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    async def _refresh_artifact(self, *, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_refresh < self.refresh_interval_s:
            return
        self._last_refresh = now
        try:
            refreshed = await asyncio.to_thread(
                self.artifact_refresher, self.channel, self.state_dir / "cache"
            )
            if refreshed is not None:
                refreshed = await asyncio.to_thread(
                    self.artifact_validator, refreshed, self.state_dir
                )
            with self._lock:
                self._approved = refreshed
                self._artifact_error = None if refreshed else "production channel is disabled"
                self._reassess_inspected_jobs()
                self._revision += 1
        except Exception as error:
            with self._lock:
                self._artifact_error = str(error)
                self._revision += 1

    async def _run(self) -> None:
        while True:
            await self._refresh_artifact()
            try:
                ports = await asyncio.to_thread(self.discover)
                self._apply_ports(ports)
                self._schedule_inspections()
                self._schedule_jobs()
            except Exception as error:
                with self._lock:
                    self._artifact_error = f"USB discovery failed: {error}"
                    self._revision += 1
            await asyncio.sleep(self.poll_interval_s)

    def _apply_ports(self, ports: list[PortCandidate]) -> None:
        current = {port.device: port for port in ports}
        with self._lock:
            removed = set(self._ports) - set(current)
            for device in removed:
                job_id = self._port_job.get(device)
                if job_id and job_id in self._jobs:
                    job = self._jobs[job_id]
                    job.connected = False
                    job.updated_at = self.clock()
                    if job.state in {"detected", "inspecting", "queued", "unmapped", "unsupported"}:
                        job.state = "disconnected"
                        job.message = "Board disconnected before flashing"
                self._port_job.pop(device, None)

            for device, port in current.items():
                if device in self._ports and port == self._ports[device]:
                    continue
                prior_job_id = self._port_job.pop(device, None)
                if prior_job_id and prior_job_id in self._jobs:
                    prior = self._jobs[prior_job_id]
                    prior.connected = False
                    prior.updated_at = self.clock()
                    if prior.state in {"detected", "inspecting", "queued", "unmapped", "unsupported"}:
                        prior.state = "disconnected"
                        prior.message = "Board disconnected before flashing"
                now = self.clock()
                slot = self._slot_map.get(port.location) if port.location else None
                state = "detected"
                message = "Waiting for firmware inspection"
                error = None
                job = ProvisioningJob(
                    id=uuid4().hex,
                    port_id=self._port_id(port),
                    slot=slot,
                    state=state,
                    message=message,
                    connected=True,
                    created_at=now,
                    updated_at=now,
                    error=error,
                )
                self._jobs[job.id] = job
                self._port_job[device] = job.id
            if removed or current != self._ports:
                self._revision += 1
                self._save_jobs()
            self._ports = current

    def _schedule_inspections(self) -> None:
        with self._lock:
            available = 10 - sum(
                not task.done() for task in self._inspection_tasks.values()
            )
            if available <= 0:
                return
            detected = [
                job for job in self._jobs.values()
                if job.state == "detected"
                and job.connected
                and job.id not in self._inspection_tasks
            ]
            detected.sort(key=lambda item: (item.slot or 999, item.created_at))
            busy_devices = set(self._job_devices.values()) | set(
                self._inspection_devices.values()
            )
            for job in detected[:available]:
                device = next(
                    (name for name, job_id in self._port_job.items() if job_id == job.id),
                    None,
                )
                if device is None or device in busy_devices:
                    continue
                job.state = "inspecting"
                job.message = "Reading board identity and firmware"
                job.updated_at = self.clock()
                task = asyncio.create_task(self._run_inspection(job.id, device))
                self._inspection_tasks[job.id] = task
                self._inspection_devices[job.id] = device
                busy_devices.add(device)
                self._revision += 1
                self._save_jobs()

    async def _run_inspection(self, job_id: str, device: str) -> None:
        try:
            info = await asyncio.to_thread(self.inspector, device)
            with self._lock:
                job = self._jobs[job_id]
                if not job.connected or self._port_job.get(device) != job_id:
                    return
                job.error = None
                if info is None:
                    job.state = "queued"
                    job.update_status = "unknown"
                    job.message = (
                        "Firmware version unavailable; start the station to "
                        "identify and update"
                    )
                else:
                    job.mac = info.mac
                    job.node_id = info.node_id or None
                    job.role = info.role
                    job.firmware_version = info.version
                    job.firmware_build = info.build
                    job.firmware_proto = info.proto
                    job.firmware_dirty = info.dirty
                    if info.role != "PERFORMER":
                        job.state = "unsupported"
                        job.update_status = "unsupported"
                        job.message = (
                            f"{info.role.title()} detected; performer station "
                            "will not flash it"
                        )
                    else:
                        job.state = "queued"
                        current = self._approved is not None and should_skip(
                            info, self._approved[0]
                        )
                        job.update_status = "current" if current else "update_needed"
                        job.message = (
                            "Firmware is current"
                            if current
                            else "Firmware update needed"
                        )
                job.updated_at = self.clock()
                self._revision += 1
                self._save_jobs()
        except Exception as error:
            with self._lock:
                job = self._jobs[job_id]
                if job.connected and self._port_job.get(device) == job_id:
                    job.state = "queued"
                    job.update_status = "unknown"
                    job.message = (
                        "Could not read firmware version; station will retry "
                        "during update"
                    )
                    job.error = str(error).replace(device, "USB device")
                    job.updated_at = self.clock()
                    self._revision += 1
                    self._save_jobs()
        finally:
            with self._lock:
                self._inspection_tasks.pop(job_id, None)
                self._inspection_devices.pop(job_id, None)

    def _reassess_inspected_jobs(self) -> None:
        if self._approved is None:
            return
        target_build = approved_build(self._approved[0])
        changed = False
        for job in self._jobs.values():
            if (
                not job.connected
                or job.role != "PERFORMER"
                or job.firmware_build is None
                or job.id in self._job_tasks
            ):
                continue
            current = job.firmware_dirty is False and job.firmware_build == target_build
            update_status = "current" if current else "update_needed"
            if job.update_status == update_status:
                continue
            job.update_status = update_status
            if job.state == "done" and not current:
                job.state = "queued"
            if job.state == "queued":
                job.message = "Firmware is current" if current else "Firmware update needed"
            job.updated_at = self.clock()
            changed = True
        if changed:
            self._save_jobs()

    def _schedule_jobs(self) -> None:
        with self._lock:
            if not self._active or self._approved is None:
                return
            running = sum(not task.done() for task in self._job_tasks.values())
            available = self._max_workers - running
            if available <= 0:
                return
            queued = [
                job for job in self._jobs.values()
                if job.state == "queued" and job.connected and job.id not in self._job_tasks
            ]
            queued.sort(key=lambda item: (item.slot or 999, item.created_at))
            scheduled = 0
            busy_devices = set(self._job_devices.values()) | set(
                self._inspection_devices.values()
            )
            for job in queued:
                if scheduled >= available:
                    break
                device = next(
                    (name for name, job_id in self._port_job.items() if job_id == job.id),
                    None,
                )
                if device is None or device in busy_devices:
                    continue
                job.state = "probing"
                job.message = "Starting board probe"
                job.updated_at = self.clock()
                task = asyncio.create_task(self._run_job(job.id, device))
                self._job_tasks[job.id] = task
                self._job_devices[job.id] = device
                busy_devices.add(device)
                scheduled += 1
                self._revision += 1
                self._save_jobs()

    async def _run_job(self, job_id: str, device: str) -> None:
        with self._lock:
            approved = self._approved
            factory = self.clock() < self._factory_until
        if approved is None:
            self._fail_job(job_id, "No approved production artifact is available")
            return
        if self.id_resolver is None:
            self._fail_job(job_id, "Permanent-ID authority is unavailable")
            return

        def progress(stage: str, message: str) -> None:
            with self._lock:
                job = self._jobs[job_id]
                if not job.connected or self._port_job.get(device) != job_id:
                    raise RuntimeError("board disconnected or USB path was reused")
                job.state = stage
                job.message = message
                job.updated_at = self.clock()
                self._revision += 1
                self._save_jobs()

        try:
            result = await asyncio.to_thread(
                self._process_locked,
                device,
                approved,
                factory,
                progress,
            )
            match = ID_RE.search(result)
            with self._lock:
                job = self._jobs[job_id]
                if not job.connected or self._port_job.get(device) != job_id:
                    raise RuntimeError("board disconnected or USB path was reused")
                job.state = "done"
                job.message = match.group(0).upper() + " ready to label" if match else result
                job.node_id = int(match.group(1)) if match else None
                mac = re.match(r"([0-9A-F:]{17})", result)
                job.mac = mac.group(1) if mac else None
                job.role = "PERFORMER"
                job.firmware_version = approved[0]["version"]
                job.firmware_build = approved_build(approved[0])
                job.firmware_proto = (
                    job.firmware_proto if job.update_status == "current" else None
                )
                job.firmware_dirty = False
                job.update_status = "current"
                job.updated_at = self.clock()
                self._revision += 1
                self._save_jobs()
        except Exception as error:
            self._fail_job(job_id, str(error).replace(device, "USB device"))
        finally:
            with self._lock:
                self._job_tasks.pop(job_id, None)
                self._job_devices.pop(job_id, None)

    @contextmanager
    def _operation_lock(self):
        if self.operation_lock_path is None:
            yield
            return
        try:
            lock = self.operation_lock_path.open("rb")
        except OSError as error:
            raise RuntimeError("provisioning operation lock is unavailable") from error
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("deployment or firmware update is in progress") from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _process_locked(
        self,
        device: str,
        approved: tuple[dict[str, Any], Path],
        factory: bool,
        progress: Callable[[str, str], None],
    ) -> str:
        with self._operation_lock():
            return self.processor(
                device,
                approved[0],
                approved[1],
                self.state_dir / "work",
                device_registry=self.state_dir / "devices.json",
                factory_authorized=factory,
                progress=progress,
                id_resolver=self.id_resolver,
            )

    def _fail_job(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.state = "failed"
            job.message = error
            job.error = error
            job.updated_at = self.clock()
            self._revision += 1
            self._save_jobs()

    def start_session(self, *, max_workers: int, factory: bool) -> dict[str, Any]:
        with self._lock:
            if self._approved is None:
                raise RuntimeError(self._artifact_error or "approved production artifact unavailable")
            if self.id_resolver is None:
                raise RuntimeError("Permanent-ID authority is unavailable")
            self._active = True
            self._max_workers = max_workers
            self._factory_until = self.clock() + 15 * 60 if factory else 0.0
            for job in self._jobs.values():
                if job.connected and job.state == "disconnected":
                    job.state = "queued" if job.slot is not None else "unmapped"
            self._revision += 1
        return self.status()

    def stop_session(self) -> dict[str, Any]:
        with self._lock:
            self._active = False
            self._factory_until = 0.0
            self._revision += 1
        return self.status()

    def map_slot(self, *, port_id: str, slot: int) -> dict[str, Any]:
        with self._lock:
            port = next((item for item in self._ports.values() if self._port_id(item) == port_id), None)
            if port is None:
                raise KeyError("connected port not found")
            key = self._identity_key(port)
            if any(
                existing_slot == slot and existing_key != key
                for existing_key, existing_slot in self._slot_map.items()
            ):
                raise RuntimeError(f"slot {slot} is already assigned to another hub port")
            self._slot_map[key] = slot
            job_id = self._port_job.get(port.device)
            if job_id:
                job = self._jobs[job_id]
                job.slot = slot
                if job.state == "unmapped":
                    job.state = "queued"
                    job.message = "Waiting to flash"
                    job.updated_at = self.clock()
            self._save_config()
            self._save_jobs()
            self._revision += 1
        return self.status()

    def retry(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError("job not found")
            if job.state != "failed":
                raise RuntimeError("only failed jobs can be retried")
            if not job.connected:
                raise RuntimeError("reconnect the board before retrying")
            job.state = "queued"
            job.message = "Waiting to retry"
            job.error = None
            job.updated_at = self.clock()
            self._revision += 1
            self._save_jobs()
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self.clock()
            artifact = None
            if self._approved is not None:
                manifest = self._approved[0]
                artifact = {
                    "release": manifest["release"],
                    "version": manifest["version"],
                    "build": approved_build(manifest),
                    "sha256": manifest["serial_flash"]["sha256"],
                }
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)[:100]
            return {
                "available": True,
                "revision": self._revision,
                "session": {
                    "active": self._active,
                    "max_workers": self._max_workers,
                    "factory_armed": self._active and now < self._factory_until,
                    "factory_expires_at": self._factory_until or None,
                },
                "artifact": artifact,
                "artifact_error": self._artifact_error,
                "connected": len(self._ports),
                "running": sum(job.state in ACTIVE_STATES for job in self._jobs.values()),
                "jobs": [asdict(job) for job in jobs],
            }


def _default_state_dir() -> Path:
    configured = os.getenv("PROVISIONER_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Lightweave/provisioner"
    return Path("/var/lib/lightweave/provisioner")


def _default_discover() -> list[PortCandidate]:
    configured = {
        value
        for value in os.getenv("PROVISIONER_EXCLUDED_PORTS", "").split(os.pathsep)
        if value
    }
    conductor = os.getenv("CONTROL_SERIAL_PORT", "")
    if conductor:
        configured.add(conductor)
    excluded = {str(Path(value).expanduser().resolve()) for value in configured}
    return [
        port
        for port in candidate_port_infos()
        if str(Path(port.device).resolve()) not in excluded
    ]


def create_default_manager() -> ProvisioningManager:
    authority_url = os.getenv(
        "PROVISIONER_ID_AUTHORITY_URL",
        "http://127.0.0.1:8000/api/internal/provisioning/reserve-id",
    )
    authority_token = os.getenv("PROVISIONER_TOKEN", "")
    if not authority_token and sys.platform == "darwin":
        try:
            authority_token = (
                Path.home()
                / "Library/Application Support/Lightweave/provisioner/token"
            ).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    resolver = None
    if authority_token:
        resolver = HttpIdAuthority(authority_url, authority_token).reserve
    return ProvisioningManager(
        _default_state_dir(),
        channel=os.getenv("PROVISIONER_CHANNEL", DEFAULT_CHANNEL),
        discover=_default_discover,
        id_resolver=resolver,
        operation_lock_path=(
            Path(value).expanduser()
            if (value := os.getenv("PROVISIONER_OPERATION_LOCK"))
            else None
        ),
    )


def create_provisioner_app(manager: ProvisioningManager | None = None) -> FastAPI:
    resolved = manager or create_default_manager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.manager.start()
        try:
            yield
        finally:
            await app.state.manager.stop()

    app = FastAPI(title="Lightweave USB Provisioner", lifespan=lifespan)
    app.state.manager = resolved

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return app.state.manager.status()

    @app.post("/session")
    async def start_session(request: SessionRequest) -> dict[str, Any]:
        try:
            return app.state.manager.start_session(
                max_workers=request.max_workers,
                factory=request.factory,
            )
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.delete("/session")
    async def stop_session() -> dict[str, Any]:
        return app.state.manager.stop_session()

    @app.put("/slots")
    async def map_slot(request: SlotRequest) -> dict[str, Any]:
        try:
            return app.state.manager.map_slot(port_id=request.port_id, slot=request.slot)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/jobs/{job_id}/retry")
    async def retry(job_id: str) -> dict[str, Any]:
        try:
            return app.state.manager.retry(job_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return app


app = create_provisioner_app()


def prepare_socket_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(f"refusing to replace non-socket path: {path}")
    path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Lightweave USB provisioner")
    parser.add_argument(
        "--socket",
        type=Path,
        default=_default_state_dir() / "provisioner.sock",
    )
    args = parser.parse_args()
    socket_path = args.socket.expanduser()
    prepare_socket_path(socket_path)
    import uvicorn

    uvicorn.run(app, uds=str(socket_path), workers=1, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
