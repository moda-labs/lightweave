from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zlib
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .adapters import (
    DEFAULT_SERIAL_REQUEST_TIMEOUT_S,
    ConductorAdapter,
    JsonLineSerialConductor,
    SerialProtocolError,
)
from .auth import AuthManager, AuthStatus, canonicalize_client_ip
from .calibration import CalibrationError, CalibrationStore, calibration_code_plan
from .group_store import GROUP_NAME_MAX_LENGTH, GroupStore, GroupStoreError
from .mock_conductor import MockConductor
from .ota_store import (
    OtaArtifactError,
    OtaArtifactStore,
    OtaInstallStore,
    PersistentOtaInstall,
)
from .pattern_store import PatternStore, PatternStoreError
from .preview import parse_params, render_preview_data, render_preview_frames, render_preview_png, review_preview
from .provisioning_client import (
    ProvisioningClient,
    ProvisioningClientError,
    UnavailableProvisioningClient,
    UnixProvisioningClient,
)
from .remote_config import (
    RemoteSettings,
    load_remote_settings,
    select_client_ip,
    select_external_scheme,
)
from .releases import (
    current_source_commit,
    load_deployment_record,
    load_release_catalog,
    release_status,
    stage_deployment_firmware,
)
from .serial_transport import PySerialTransport


STATIC_DIR = Path(__file__).with_name("static")
REPO_ROOT = STATIC_DIR.parents[1]
OTA_CHUNK_RETRIES = 3
OTA_STATUS_FRESH_S = 60
OTA_CHECKPOINT_CHUNKS = 256
OTA_STATUS_SETTLE_S = 0.35
OTA_REPAIR_STALL_ROUNDS = 2
OTA_ACTIVATION_POLL_S = 0.5
OTA_POST_REBOOT_ATTEMPTS = 31
OTA_POST_REBOOT_POLL_S = 1.0


class OtaPauseRequested(Exception):
    pass
RELEASE_SNAPSHOT_MAX_AGE_S = 1.0
POWER_SAMPLE_STALE_S = 5 * 60
GROUP_COUNT = 8
DEFAULT_BATTERY_CAPACITY_WH = 384.0
DEFAULT_FULL_VOLTAGE = 14.4
SESSION_COOKIE = "__Host-lightweave_session"
LOGIN_BODY_LIMIT = 2 * 1024
PROVISIONING_ID_BODY_LIMIT = 1024
PUBLIC_HTTP_ROUTES = {
    ("GET", "/login"),
    ("GET", "/static/login.js"),
    ("GET", "/static/login.css"),
    ("GET", "/api/auth/session"),
    ("GET", "/api/health"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/internal/provisioning/reserve-id"),
}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
OTA_CHUNK_RETRYABLE_ERRORS = {
    "bad ota chunk data",
    "ota chunk length mismatch",
    "ota chunk offset mismatch",
    "ota chunk exceeds image size",
}


class PatternUpdate(BaseModel):
    pattern: str = Field(min_length=1)
    brightness: int = Field(ge=0, le=192)
    params: dict[str, int | float | str] = Field(default_factory=dict)
    group_id: int | None = Field(default=None, ge=0, lt=GROUP_COUNT)


class PatternLibraryEntry(BaseModel):
    name: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    brightness: int = Field(ge=0, le=192)
    params: dict[str, int | float | str] = Field(default_factory=dict)


class AssignRequest(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class GroupUpdate(BaseModel):
    group_id: int = Field(ge=0, lt=GROUP_COUNT)


class GroupNameUpdate(BaseModel):
    name: str = Field(max_length=GROUP_NAME_MAX_LENGTH)


class LedCountUpdate(BaseModel):
    led_count: Literal[16, 32, 64]


class ReplaceRequest(BaseModel):
    old_mac: str
    new_mac: str


class PowerPolicyUpdate(BaseModel):
    light_sleep_check_s: int = Field(ge=1, le=300)
    deep_sleep_check_min: int = Field(ge=1, le=1440)
    led_on_start_min: int = Field(ge=0, le=1439)
    led_on_end_min: int = Field(ge=0, le=1439)
    schedule_enabled: bool
    force_awake: bool
    force_sleep: bool = False
    current_min: int = Field(ge=0, le=1439)
    current_epoch_s: int = Field(ge=0, le=4_294_967_295)


class FieldPowerUpdate(BaseModel):
    mode: Literal["sleep", "wake", "schedule"]


class PowerMonitorUpdate(BaseModel):
    battery_capacity_wh: float = Field(gt=0, le=10_000)
    full_voltage: float = Field(gt=0, le=100)


class OtaModeUpdate(BaseModel):
    enabled: bool


class CalibrationModeUpdate(BaseModel):
    enabled: bool


class CalibrationDetectRequest(BaseModel):
    threshold: int = Field(default=180, ge=0, le=255)
    min_area: int = Field(default=4, ge=1, le=100_000)


class CalibrationDecodeRequest(BaseModel):
    frame_ids: list[str] = Field(min_length=1, max_length=64)
    threshold: int = Field(default=180, ge=0, le=255)
    min_area: int = Field(default=4, ge=1, le=100_000)
    max_distance: float = Field(default=0.035, gt=0.0, le=1.0)


class CalibrationCodeMapEntry(BaseModel):
    mac: str = Field(min_length=1)
    code: int = Field(ge=1)
    bits: str = Field(min_length=1, max_length=32)


class CalibrationCodePlanRequest(BaseModel):
    roster_macs: list[str] | None = Field(default=None, max_length=128)
    first_code: int = Field(default=1, ge=1)
    bit_count: int | None = Field(default=None, ge=1, le=32)
    min_hamming_distance: int = Field(default=3, ge=1, le=12)


class CalibrationProposeRequest(BaseModel):
    frame_ids: list[str] = Field(min_length=1, max_length=64)
    roster_macs: list[str] | None = Field(default=None, max_length=128)
    code_map: list[CalibrationCodeMapEntry] | None = Field(default=None, max_length=128)
    threshold: int = Field(default=180, ge=0, le=255)
    min_area: int = Field(default=4, ge=1, le=100_000)
    max_distance: float = Field(default=0.035, gt=0.0, le=1.0)
    first_code: int = Field(default=1, ge=1)


class CalibrationApplyAssignment(BaseModel):
    mac: str = Field(min_length=1)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    code: int | None = Field(default=None, ge=1)
    bits: str | None = Field(default=None, min_length=1, max_length=32)


class CalibrationApplyRequest(BaseModel):
    assignments: list[CalibrationApplyAssignment] = Field(min_length=1, max_length=256)
    missing: list[dict[str, Any]] = Field(default_factory=list, max_length=256)
    ambiguous: list[dict[str, Any]] = Field(default_factory=list, max_length=256)


class CalibrationSyntheticNode(BaseModel):
    mac: str = Field(min_length=1)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class CalibrationSyntheticRequest(BaseModel):
    nodes: list[CalibrationSyntheticNode] | None = Field(default=None, max_length=128)
    width: int = Field(default=960, ge=40, le=4000)
    height: int = Field(default=720, ge=40, le=4000)
    first_code: int = Field(default=1, ge=1)
    bit_count: int | None = Field(default=None, ge=1, le=32)
    blob_radius: int = Field(default=5, ge=1, le=80)
    led_value: int = Field(default=255, ge=0, le=255)
    jitter_px: float = Field(default=0.0, ge=0.0, le=4000.0)
    glare_count: int = Field(default=0, ge=0, le=500)
    glare_value: int = Field(default=230, ge=0, le=255)
    missing_frames: list[int] = Field(default_factory=list, max_length=32)
    perspective: float = Field(default=0.0, ge=0.0, le=0.45)
    min_hamming_distance: int = Field(default=3, ge=1, le=12)
    threshold: int = Field(default=180, ge=0, le=255)
    min_area: int = Field(default=4, ge=1, le=100_000)
    max_distance: float = Field(default=0.035, gt=0.0, le=1.0)


class WifiJoinRequest(BaseModel):
    ssid: str = Field(min_length=1, max_length=64)
    password: str = Field(default="", max_length=128)


class ProvisioningSessionRequest(BaseModel):
    max_workers: int = Field(default=5, ge=1, le=10)
    factory: bool = False


class ProvisioningSlotRequest(BaseModel):
    port_id: str = Field(min_length=8, max_length=32)
    slot: int = Field(ge=1, le=32)


class ProvisioningIdRequest(BaseModel):
    mac: str = Field(pattern=r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
    reported_id: int = Field(default=0, ge=0, le=65535)


def create_default_conductor() -> ConductorAdapter:
    mode = os.getenv("CONTROL_CONDUCTOR", "mock").strip().lower()
    if mode in {"mock", ""}:
        return MockConductor()
    if mode != "serial":
        raise RuntimeError(f"unknown CONTROL_CONDUCTOR={mode!r}")

    port = os.getenv("CONTROL_SERIAL_PORT")
    if not port:
        raise RuntimeError("CONTROL_SERIAL_PORT is required when CONTROL_CONDUCTOR=serial")
    baud = int(os.getenv("CONTROL_SERIAL_BAUD", "115200"))
    timeout_s = float(
        os.getenv("CONTROL_SERIAL_TIMEOUT_S", str(DEFAULT_SERIAL_REQUEST_TIMEOUT_S))
    )
    reset_on_open = os.getenv("CONTROL_SERIAL_RESET_ON_OPEN", "0").strip().lower() in {"1", "true", "yes"}
    return JsonLineSerialConductor(
        PySerialTransport(port, baud=baud, reset_on_open=reset_on_open),
        timeout_s=timeout_s,
    )


def create_app(
    conductor: ConductorAdapter | None = None,
    ota_store: OtaArtifactStore | None = None,
    pattern_store: PatternStore | None = None,
    calibration_store: CalibrationStore | None = None,
    auth_manager: AuthManager | None = None,
    settings: RemoteSettings | None = None,
    provisioning_client: ProvisioningClient | None = None,
    group_store: GroupStore | None = None,
) -> FastAPI:
    resolved_settings = settings or load_remote_settings(os.environ)
    resolved_auth = auth_manager
    if resolved_auth is None:
        resolved_auth = (
            AuthManager.from_encoded_hash(resolved_settings.password_hash)
            if resolved_settings.password_hash is not None
            else AuthManager.disabled()
        )
    if resolved_settings.serial_mode and not resolved_auth.enabled:
        raise RuntimeError("authentication cannot be disabled when CONTROL_CONDUCTOR=serial")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def ticker() -> None:
            while True:
                await asyncio.sleep(5)
                try:
                    await conductor_call("tick")
                    await publish({"type": "state", "action": "tick", "state": await conductor_call("snapshot")})
                except HTTPException as error:
                    if error.status_code != 423:
                        raise
                except SerialProtocolError:
                    await publish({"type": "error", "action": "tick", "message": "conductor serial timeout"})

        async def session_reaper() -> None:
            while True:
                await asyncio.sleep(1)
                expired = set(app.state.auth_manager.reap_expired_sessions())
                app.state.auth_manager.reap_stale_failures()
                if expired:
                    await close_session_websockets(expired)

        async def provisioning_ticker() -> None:
            last_revision: int | None = None
            last_available: bool | None = None
            last_factory_armed: bool | None = None
            while True:
                await asyncio.sleep(1)
                try:
                    status = await app.state.provisioning_client.status()
                except ProvisioningClientError as error:
                    status = {
                        "available": False,
                        "revision": 0,
                        "session": {"active": False, "max_workers": 5, "factory_armed": False},
                        "artifact": None,
                        "artifact_error": str(error),
                        "connected": 0,
                        "running": 0,
                        "jobs": [],
                    }
                revision = int(status.get("revision") or 0)
                available = bool(status.get("available"))
                factory_armed = bool((status.get("session") or {}).get("factory_armed"))
                app.state.provisioning_snapshot = status
                if (
                    revision != last_revision
                    or available != last_available
                    or factory_armed != last_factory_armed
                ):
                    await publish({"type": "provisioning", "provisioning": status})
                    last_revision = revision
                    last_available = available
                    last_factory_armed = factory_armed

        ticker_task = asyncio.create_task(ticker())
        reaper_task = asyncio.create_task(session_reaper())
        provisioning_task = asyncio.create_task(provisioning_ticker())
        app.state.ticker_task = ticker_task
        app.state.session_reaper_task = reaper_task
        app.state.provisioning_task = provisioning_task
        if app.state.ota_install.get("running") is True:
            artifact = app.state.ota_store.artifact()
            matches = (
                artifact is not None
                and int(app.state.ota_install.get("size") or 0) == artifact.size
                and int(app.state.ota_install.get("crc32") or 0) == artifact.crc32
            )
            if not matches:
                app.state.ota_install.update({
                    "running": False,
                    "complete": False,
                    "error": "cannot resume OTA: staged artifact changed or is missing",
                    "completed_at": time.time(),
                })
            else:
                async def resume_when_available() -> None:
                    attempt = 0
                    while app.state.ota_install.get("running") is True:
                        attempt += 1
                        try:
                            app.state.ota_operation_lock = acquire_ota_operation_lock()
                            state = await conductor_call("snapshot")
                            app.state.ota_reserved = True
                            await ota_install_worker(artifact, state, resume=True)
                            return
                        except (HTTPException, SerialProtocolError) as error:
                            app.state.ota_reserved = False
                            release_ota_operation_lock()
                            app.state.ota_install.update({
                                "phase": "waiting",
                                "message": f"waiting to resume OTA: {error}",
                                "last_retry": {"attempt": attempt, "error": str(error)},
                            })
                            await asyncio.sleep(min(5.0, 0.25 * attempt))

                app.state.ota_task = asyncio.create_task(resume_when_available())
        try:
            yield
        finally:
            ota_task = app.state.ota_task
            if ota_task is not None and not ota_task.done():
                ota_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ota_task
                app.state.ota_install.update({
                    "running": True,
                    "complete": False,
                    "error": None,
                    "phase": "paused",
                    "message": "OTA paused for service restart; it will resume automatically",
                })
                app.state.ota_reserved = False
            ticker_task.cancel()
            reaper_task.cancel()
            provisioning_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker_task
            with contextlib.suppress(asyncio.CancelledError):
                await reaper_task
            with contextlib.suppress(asyncio.CancelledError):
                await provisioning_task
            revoked = set(app.state.auth_manager.revoke_all_sessions())
            if revoked:
                await close_session_websockets(revoked)
            release_ota_operation_lock()

    app = FastAPI(title="Do Baskets Dream Control Plane", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.auth_manager = resolved_auth
    app.state.conductor = conductor or create_default_conductor()
    if provisioning_client is not None:
        app.state.provisioning_client = provisioning_client
    else:
        socket_value = os.getenv("CONTROL_PROVISIONER_SOCKET")
        if socket_value:
            app.state.provisioning_client = UnixProvisioningClient(Path(socket_value).expanduser())
        elif resolved_settings.serial_mode:
            app.state.provisioning_client = UnixProvisioningClient(
                Path("/run/lightweave-provisioner/provisioner.sock")
            )
        elif sys.platform == "darwin":
            app.state.provisioning_client = UnixProvisioningClient(
                Path.home() / "Library/Application Support/Lightweave/provisioner/provisioner.sock"
            )
        else:
            app.state.provisioning_client = UnavailableProvisioningClient()
    app.state.provisioning_snapshot = None
    app.state.provisioner_token = os.getenv("CONTROL_PROVISIONER_TOKEN") or os.getenv(
        "PROVISIONER_TOKEN"
    )
    if not app.state.provisioner_token and sys.platform == "darwin":
        token_path = (
            Path.home() / "Library/Application Support/Lightweave/provisioner/token"
        )
        try:
            app.state.provisioner_token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    data_dir = resolved_settings.data_dir
    app.state.ota_store = ota_store or OtaArtifactStore(data_dir / "ota" if data_dir else ".control_ota")
    default_deployment_record = (
        "/var/lib/lightweave-gitops/current.json"
        if resolved_settings.serial_mode
        else str(data_dir / "deployments" / "current.json") if data_dir else ".control_deployment.json"
    )
    deployment_record_path = Path(
        os.getenv(
            "CONTROL_DEPLOYMENT_RECORD",
            default_deployment_record,
        )
    )
    app.state.release_catalog = load_release_catalog(REPO_ROOT / "RELEASES.json")
    app.state.deployment_record = load_deployment_record(deployment_record_path)
    app.state.running_version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    app.state.running_commit = current_source_commit(REPO_ROOT)
    app.state.latest_snapshot = None
    app.state.latest_snapshot_at = 0.0
    stage_deployment_firmware(app.state.ota_store, app.state.deployment_record)
    app.state.pattern_store = pattern_store or PatternStore(data_dir / "patterns" if data_dir else ".control_patterns")
    app.state.group_store = group_store or GroupStore(data_dir / "groups" if data_dir else ".control_groups")
    app.state.calibration_store = calibration_store or CalibrationStore(
        data_dir / "calibration" if data_dir else ".control_calibration"
    )
    app.state.ota_install_store = OtaInstallStore(app.state.ota_store.root)
    app.state.ota_install = PersistentOtaInstall(
        app.state.ota_install_store,
        app.state.ota_install_store.load(),
    )
    app.state.ota_reserved = False
    app.state.ota_pause_requested = False
    app.state.ota_task: asyncio.Task[None] | None = None
    app.state.ota_start_lock = asyncio.Lock()
    app.state.ota_operation_lock_path = (
        Path(os.getenv("CONTROL_OTA_LOCK", str(data_dir / "operations" / "firmware-ota.lock")))
        if data_dir
        else None
    )
    app.state.ota_operation_lock = None
    app.state.calibration_previous_pattern = None
    app.state.power_monitor_config = {
        "battery_capacity_wh": float(os.getenv("CONTROL_BATTERY_CAPACITY_WH", DEFAULT_BATTERY_CAPACITY_WH)),
        "full_voltage": float(os.getenv("CONTROL_BATTERY_FULL_VOLTAGE", DEFAULT_FULL_VOLTAGE)),
    }
    app.state.power_full_anchors = {}
    app.state.conductor_lock = asyncio.Lock()
    app.state.ws_clients: dict[WebSocket, str] = {}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def socket_peer_ip(scope: dict[str, Any]) -> str:
        client = scope.get("client")
        raw = client[0] if isinstance(client, (tuple, list)) and client else "127.0.0.1"
        if raw == "testclient":
            return "127.0.0.1"
        return canonicalize_client_ip(str(raw))

    def request_client_ip(request: Request) -> str:
        peer = socket_peer_ip(request.scope)
        return select_client_ip(peer, request.headers.get("cf-connecting-ip"))

    def external_scheme(scope: dict[str, Any], headers: Any) -> str:
        return select_external_scheme(
            socket_peer_ip(scope),
            str(scope.get("scheme") or "http"),
            headers.get("x-forwarded-proto"),
        )

    def allowed_origin(origin: str, scope: dict[str, Any], headers: Any) -> bool:
        configured = app.state.settings.allowed_origins
        if configured:
            return origin in configured
        host = headers.get("host")
        scheme = external_scheme(scope, headers)
        return bool(host) and origin == f"{scheme}://{host}"

    def live_session(token: str | None):
        return app.state.auth_manager.lookup_session(token) if token else None

    def acquire_ota_operation_lock():
        path = app.state.ota_operation_lock_path
        if path is None:
            return None
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(mode=0o600)
            handle = path.open("r")
        except OSError as error:
            raise HTTPException(status_code=503, detail=f"OTA operation lock is unavailable: {error}") from error
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise HTTPException(status_code=423, detail="software deployment is in progress")
        return handle

    def release_ota_operation_lock() -> None:
        handle = app.state.ota_operation_lock
        if handle is None:
            return
        app.state.ota_operation_lock = None
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()

    def secure_response(response: Response, scheme: str) -> Response:
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Frame-Options"] = "DENY"
        if scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    async def close_session_websockets(tokens: set[str]) -> None:
        for ws, token in list(app.state.ws_clients.items()):
            if token not in tokens:
                continue
            app.state.ws_clients.pop(ws, None)
            with contextlib.suppress(RuntimeError):
                await ws.close(code=4401, reason="session expired")

    @app.middleware("http")
    async def remote_boundary(request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        scheme = external_scheme(request.scope, request.headers)
        public = (method, path) in PUBLIC_HTTP_ROUTES

        if app.state.settings.require_https and scheme != "https":
            if path in {"/login", "/api/auth/login"}:
                return secure_response(
                    JSONResponse({"detail": "HTTPS is required"}, status_code=400),
                    scheme,
                )

        if method in MUTATING_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and not allowed_origin(origin, request.scope, request.headers):
                return secure_response(
                    JSONResponse({"detail": "origin not allowed"}, status_code=403),
                    scheme,
                )

        if app.state.auth_manager.enabled and not public:
            token = request.cookies.get(SESSION_COOKIE)
            if live_session(token) is None:
                if path.startswith("/api/"):
                    return secure_response(
                        JSONResponse({"detail": "authentication required"}, status_code=401),
                        scheme,
                    )
                return secure_response(RedirectResponse("/login", status_code=303), scheme)

        response = await call_next(request)
        return secure_response(response, scheme)

    def ota_install_progress(install: dict[str, Any]) -> dict[str, Any]:
        progress = dict(install)
        started_at = progress.get("started_at")
        if not isinstance(started_at, (int, float)) or started_at <= 0:
            return progress
        ended_at = progress.get("completed_at")
        now = float(ended_at) if isinstance(ended_at, (int, float)) else time.time()
        elapsed_s = max(0.0, now - float(started_at))
        bytes_sent = max(0, int(progress.get("bytes_sent") or 0))
        size = max(0, int(progress.get("size") or 0))
        bytes_per_s = bytes_sent / elapsed_s if elapsed_s > 0 else 0.0
        remaining = max(0, size - bytes_sent)
        eta_s = int(round(remaining / bytes_per_s)) if bytes_per_s > 0 and remaining > 0 else 0
        progress.update({
            "elapsed_s": int(round(elapsed_s)),
            "bytes_per_s": bytes_per_s,
            "eta_s": eta_s,
        })
        return progress

    def apply_ota_progress(progress: dict[str, Any], artifact: Any) -> list[dict[str, Any]]:
        updates: dict[str, Any] = {}
        if progress.get("ok") is True:
            written = int(progress.get("written") or 0)
            if 0 <= written <= int(artifact.size):
                updates["bytes_sent"] = max(int(app.state.ota_install.get("bytes_sent") or 0), written)
                updates["chunks_sent"] = max(
                    int(app.state.ota_install.get("chunks_sent") or 0),
                    min((written + artifact.chunk_size - 1) // artifact.chunk_size, artifact.chunks),
                )
            nodes = fresh_ota_nodes(progress.get("nodes") or [])
            if nodes:
                updates["nodes"] = nodes
        if updates:
            app.state.ota_install.update(updates)
        return list(app.state.ota_install.get("nodes") or [])

    def recovery_summary(state: dict[str, Any]) -> dict[str, Any]:
        lanterns = state.get("lanterns") or []
        ota = state.get("ota") or {}
        firmware = (state.get("summary") or {}).get("firmware") or {}
        missing = [
            {"mac": item.get("mac"), "label": item.get("label"), "reason": "not seen"}
            for item in lanterns
            if item.get("status") == "missing" and item.get("position") == "Set"
        ]
        mismatched = [
            {
                "mac": item.get("mac"),
                "label": item.get("label"),
                "reason": "firmware mismatch",
                "firmware": item.get("firmware"),
            }
            for item in lanterns
            if item.get("attention") == "Firmware mismatch"
        ]
        failed_ota = []
        for node in ota.get("nodes") or []:
            if node.get("phase") != "failed":
                continue
            mac = node.get("mac")
            lantern = next((item for item in lanterns if item.get("mac") == mac), None)
            failed_ota.append({
                "mac": mac,
                "label": (lantern or {}).get("label") or mac or "node",
                "reason": node.get("error") or "ota failed",
                "phase": node.get("phase"),
            })
        ready = not missing and not mismatched and not failed_ota and firmware.get("consistent") is True
        if failed_ota:
            status = "ota_failed"
            title = "Firmware update needs recovery"
            action = "Keep maintenance mode open. Power-cycle the listed lanterns, wait for them to check in, then rerun the same staged firmware."
        elif mismatched:
            status = "mixed_firmware"
            title = "Mixed firmware detected"
            action = "Enter maintenance mode and reinstall the staged firmware across the whole field. Do not run the show with mixed firmware."
        elif missing:
            status = "missing_nodes"
            title = "Placed lanterns are missing"
            action = "Wake or power-cycle the listed lanterns. If a lantern is physically gone, replace it with an awake unpositioned spare."
        else:
            status = "ready"
            title = "No recovery needed"
            action = "Field firmware is consistent and all placed lanterns are healthy."
        return {
            "status": status,
            "ready": ready,
            "title": title,
            "action": action,
            "missing": missing,
            "mismatched": mismatched,
            "failed_ota": failed_ota,
        }

    def power_monitor_summary(state: dict[str, Any]) -> dict[str, Any]:
        config = dict(app.state.power_monitor_config)
        capacity_wh = float(config.get("battery_capacity_wh") or DEFAULT_BATTERY_CAPACITY_WH)
        full_voltage = float(config.get("full_voltage") or DEFAULT_FULL_VOLTAGE)
        lanterns = state.get("lanterns") or []
        placed_count = sum(1 for item in lanterns if item.get("position") == "Set")
        samples = []
        stale_count = 0
        implausible_count = 0
        usable_wh = []
        usable_w = []
        now = time.time()
        anchors: dict[str, dict[str, Any]] = app.state.power_full_anchors
        for lantern in lanterns:
            power = lantern.get("power") or {}
            wh = power.get("wh")
            avg_w = power.get("avg_w")
            if not isinstance(wh, (int, float)) or not isinstance(avg_w, (int, float)):
                continue
            mac = str(lantern.get("mac") or "")
            bus_v = power.get("bus_v")
            plausible = power.get("plausible")
            last_report_s = power.get("last_report_s")
            stale = isinstance(last_report_s, (int, float)) and last_report_s > POWER_SAMPLE_STALE_S
            if stale:
                stale_count += 1
            if plausible is False:
                implausible_count += 1
            full_detected = isinstance(bus_v, (int, float)) and bus_v >= full_voltage
            anchor = anchors.get(mac)
            if full_detected:
                anchor = {"wh": float(wh), "ts": now, "bus_v": float(bus_v)}
                anchors[mac] = anchor
            anchor_wh = float(anchor["wh"]) if anchor and isinstance(anchor.get("wh"), (int, float)) else 0.0
            used_since_full_wh = max(0.0, float(wh) - anchor_wh)
            soc_percent = max(0.0, min(100.0, 100.0 * (1.0 - used_since_full_wh / capacity_wh)))
            sample = {
                "mac": mac,
                "label": lantern.get("label"),
                "wh": float(wh),
                "avg_w": float(avg_w),
                "used_since_full_wh": used_since_full_wh,
                "soc_percent": soc_percent,
                "bus_v": bus_v,
                "current_ma": power.get("current_ma"),
                "last_report_s": last_report_s,
                "last_report_label": power.get("last_report_label"),
                "stale": stale,
                "plausible": plausible,
                "full_detected": full_detected,
                "full_anchor": anchor,
            }
            samples.append(sample)
            if not stale and plausible is not False:
                usable_wh.append(used_since_full_wh)
                usable_w.append(float(avg_w))
        avg_node_wh = sum(usable_wh) / len(usable_wh) if usable_wh else None
        avg_node_w = sum(usable_w) / len(usable_w) if usable_w else None
        estimated_soc = (
            max(0.0, min(100.0, 100.0 * (1.0 - avg_node_wh / capacity_wh)))
            if avg_node_wh is not None else None
        )
        return {
            "battery_capacity_wh": capacity_wh,
            "full_voltage": full_voltage,
            "full_anchor_policy": "SOC resets to 100% when a sample reports pack voltage at or above the full-voltage threshold.",
            "placed_count": placed_count,
            "sample_count": len(samples),
            "usable_sample_count": len(usable_w),
            "stale_count": stale_count,
            "implausible_count": implausible_count,
            "avg_node_w": avg_node_w,
            "avg_node_wh_used": avg_node_wh,
            "estimated_field_avg_w": avg_node_w * placed_count if avg_node_w is not None else None,
            "estimated_field_wh_used": avg_node_wh * placed_count if avg_node_wh is not None else None,
            "estimated_node_soc_percent": estimated_soc,
            "samples": samples,
        }

    def enrich_lantern_groups(
        lanterns: list[dict[str, Any]], groups: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        labels = {item["group_id"]: item["label"] for item in groups}
        enriched = []
        for lantern in lanterns:
            group_id = int(lantern.get("group_id") or 0)
            enriched.append(
                {
                    **lantern,
                    "group": labels.get(group_id, f"Group {group_id + 1}"),
                }
            )
        return enriched

    def enrich_state(state: dict[str, Any]) -> dict[str, Any]:
        groups = app.state.group_store.list()
        state["groups"] = groups
        state["lanterns"] = enrich_lantern_groups(state.get("lanterns") or [], groups)
        state["power_monitor"] = power_monitor_summary(state)
        state["recovery"] = recovery_summary(state)
        return state

    def calibration_roster_macs(state: dict[str, Any]) -> list[str]:
        lanterns = [
            item
            for item in state.get("lanterns") or []
            if item.get("status") == "alive" and item.get("mac")
        ]
        lanterns.sort(key=lambda item: str(item.get("mac") or ""))
        return [str(item["mac"]) for item in lanterns]

    def calibration_positioned_nodes(state: dict[str, Any]) -> list[dict[str, Any]]:
        nodes = []
        for item in state.get("lanterns") or []:
            if item.get("status") != "alive" or not item.get("mac"):
                continue
            x = item.get("x")
            y = item.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                continue
            nodes.append({"mac": str(item["mac"]), "x": float(x), "y": float(y)})
        nodes.sort(key=lambda item: item["mac"])
        return nodes

    def ota_ready_for_install(state: dict[str, Any]) -> bool:
        ota = state.get("ota") or {}
        return ota.get("ready") is True or int(ota.get("ready_count") or 0) > 0

    async def conductor_call(method: str, *args: Any) -> Any:
        async with app.state.conductor_lock:
            result = await asyncio.to_thread(getattr(app.state.conductor, method), *args)
        if method == "snapshot" and isinstance(result, dict):
            enriched = enrich_state(result)
            app.state.latest_snapshot = enriched
            app.state.latest_snapshot_at = time.monotonic()
            return enriched
        return result

    async def pattern_store_call(method: str, *args: Any) -> Any:
        return await asyncio.to_thread(getattr(app.state.pattern_store, method), *args)

    async def group_store_call(method: str, *args: Any) -> Any:
        return await asyncio.to_thread(getattr(app.state.group_store, method), *args)

    async def calibration_store_call(method: str, *args: Any) -> Any:
        return await asyncio.to_thread(getattr(app.state.calibration_store, method), *args)

    async def publish(event: dict[str, Any]) -> None:
        event = {"ts": time.time(), **event}
        dead: list[WebSocket] = []
        for ws, token in list(app.state.ws_clients.items()):
            if app.state.auth_manager.enabled and live_session(token) is None:
                dead.append(ws)
                with contextlib.suppress(RuntimeError):
                    await ws.close(code=4401, reason="session expired")
                continue
            try:
                await ws.send_json(event)
            except (RuntimeError, WebSocketDisconnect):
                dead.append(ws)
        for ws in dead:
            app.state.ws_clients.pop(ws, None)

    async def publish_state(action: str) -> None:
        try:
            state = await conductor_call("snapshot")
        except (SerialProtocolError, HTTPException) as error:
            if isinstance(error, HTTPException) and error.status_code == 423:
                return
            await publish({"type": "error", "action": action, "message": str(error)})
            return
        await publish({"type": "state", "action": action, "state": state})

    async def provisioning_call(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = await getattr(app.state.provisioning_client, method)(*args, **kwargs)
        except ProvisioningClientError as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        app.state.provisioning_snapshot = result
        return result

    def calibration_mode_plan(state: dict[str, Any]) -> dict[str, Any]:
        return calibration_code_plan(
            calibration_roster_macs(state),
            first_code=1,
            min_hamming_distance=3,
        )

    async def set_live_calibration_mode(enabled: bool) -> dict[str, Any]:
        async with app.state.conductor_lock:
            state = await asyncio.to_thread(app.state.conductor.snapshot)
            if enabled:
                current = state.get("pattern") or {}
                previous = None
                if current.get("pattern") != "Calibration":
                    group_patterns = []
                    for fallback_group_id, item in enumerate(state.get("patterns") or []):
                        if not isinstance(item, dict):
                            continue
                        config = item.get("config") or {}
                        group_id = int(item.get("group_id", fallback_group_id))
                        if not isinstance(config, dict) or not 0 <= group_id < GROUP_COUNT:
                            continue
                        group_patterns.append(
                            {
                                "group_id": group_id,
                                "pattern": str(config.get("pattern") or "Glow"),
                                "brightness": int(
                                    48 if config.get("brightness") is None else config["brightness"]
                                ),
                                "params": dict(config.get("params") or {}),
                            }
                        )
                    if len(group_patterns) == GROUP_COUNT:
                        previous = {"groups": group_patterns}
                    else:
                        previous = {
                            "pattern": str(current.get("pattern") or "Glow"),
                            "brightness": int(
                                48 if current.get("brightness") is None else current["brightness"]
                            ),
                            "params": dict(current.get("params") or {}),
                        }
                plan = calibration_mode_plan(state)
                ack = await asyncio.to_thread(
                    app.state.conductor.update_pattern,
                    "Calibration",
                    96,
                    {
                        "p0": 1000,
                        "p1": int(plan["bit_count"]),
                        "p2": int(plan["first_code"]),
                        "p3": int(plan["min_hamming_distance"]),
                    },
                )
                if ack.get("ok"):
                    if previous is not None:
                        app.state.calibration_previous_pattern = previous
                    ack["plan"] = plan
                return ack
            previous = app.state.calibration_previous_pattern or {
                "pattern": "Glow",
                "brightness": 48,
                "params": {"hue": 40, "saturation": 100},
            }
            if "groups" in previous:
                for config in previous["groups"]:
                    ack = await asyncio.to_thread(
                        app.state.conductor.update_pattern,
                        config["pattern"],
                        config["brightness"],
                        config["params"],
                        config["group_id"],
                    )
                    if not ack.get("ok"):
                        return ack
                app.state.calibration_previous_pattern = None
                return {"ok": True, "message": "restored group patterns"}
            ack = await asyncio.to_thread(
                app.state.conductor.update_pattern,
                previous["pattern"],
                previous["brightness"],
                previous["params"],
            )
            if ack.get("ok"):
                app.state.calibration_previous_pattern = None
            return ack

    async def infer_ota_complete_nodes(
        size: int,
        crc32: int,
        expected_macs: set[str],
    ) -> list[dict[str, Any]]:
        if not expected_macs:
            return []
        last_nodes: list[dict[str, Any]] = []
        for attempt in range(OTA_POST_REBOOT_ATTEMPTS):
            if attempt:
                await asyncio.sleep(OTA_POST_REBOOT_POLL_S)
            try:
                state = await conductor_call("snapshot")
            except SerialProtocolError:
                continue
            conductor_firmware = (state.get("conductor") or {}).get("firmware") or {}
            nodes = []
            for lantern in state.get("lanterns") or []:
                mac = str(lantern.get("mac") or "")
                if mac not in expected_macs or lantern.get("status") != "alive":
                    continue
                performer_firmware = lantern.get("firmware") or {}
                comparable = ("version", "proto", "build_id", "dirty")
                if any(performer_firmware.get(key) != conductor_firmware.get(key) for key in comparable):
                    continue
                nodes.append({
                    "mac": mac,
                    "phase": "complete",
                    "error": "none",
                    "offset": size,
                    "crc32": crc32,
                    "source": "post_reboot_state",
                })
            if {str(node["mac"]) for node in nodes} == expected_macs:
                return nodes
            last_nodes = nodes
        return last_nodes

    def expected_ota_lanterns(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(lantern.get("mac")): lantern
            for lantern in state.get("lanterns") or []
            if lantern.get("status") == "alive"
            and lantern.get("mac")
        }

    def append_unverified_ota_failures(
        nodes: list[dict[str, Any]],
        expected: dict[str, dict[str, Any]],
        verified_macs: set[str],
    ) -> list[dict[str, Any]]:
        existing_macs = {str(node.get("mac")) for node in nodes if node.get("mac")}
        augmented = list(nodes)
        for mac, lantern in expected.items():
            if mac in verified_macs or mac in existing_macs:
                continue
            augmented.append({
                "mac": mac,
                "label": lantern.get("label") or mac,
                "phase": "failed",
                "error": "post-reboot verification missing",
                "offset": 0,
                "crc32": 0,
                "source": "post_reboot_verification",
            })
        return augmented

    def fresh_ota_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fresh = []
        for node in nodes:
            age = node.get("last_seen_s")
            if isinstance(age, (int, float)) and age > OTA_STATUS_FRESH_S:
                continue
            fresh.append(node)
        return fresh

    def nmcli_path() -> str | None:
        return shutil.which("nmcli")

    def sudo_command(command: list[str]) -> list[str]:
        sudo = shutil.which("sudo")
        if not sudo:
            return command
        return [sudo, "-n", *command]

    def wifi_status() -> dict[str, Any]:
        nmcli = nmcli_path()
        if not nmcli:
            return {
                "available": False,
                "error": "nmcli is not installed",
                "device": None,
                "state": "unavailable",
                "connection": None,
                "addresses": [],
            }
        try:
            devices = subprocess.run(
                [nmcli, "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {
                "available": False,
                "error": str(error),
                "device": None,
                "state": "unknown",
                "connection": None,
                "addresses": [],
            }

        wifi = None
        for line in devices.stdout.splitlines():
            parts = line.split(":", 3)
            if len(parts) != 4 or parts[1] != "wifi":
                continue
            wifi = {
                "device": parts[0],
                "state": parts[2],
                "connection": parts[3] or None,
            }
            if parts[2] == "connected":
                break
        if wifi is None:
            return {
                "available": False,
                "error": "no Wi-Fi device found",
                "device": None,
                "state": "unavailable",
                "connection": None,
                "addresses": [],
            }

        addresses: list[str] = []
        try:
            ip = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "dev", str(wifi["device"])],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in ip.stdout.splitlines():
                fields = line.split()
                if "inet" in fields:
                    addresses.append(fields[fields.index("inet") + 1])
        except (OSError, subprocess.SubprocessError):
            addresses = []

        return {
            "available": True,
            "error": None,
            "addresses": addresses,
            **wifi,
        }

    def run_wifi_join(ssid: str, password: str) -> None:
        delay_s = float(os.getenv("CONTROL_WIFI_JOIN_DELAY_S", "1.0"))
        if delay_s > 0:
            time.sleep(delay_s)
        helper = os.getenv("CONTROL_WIFI_JOIN_COMMAND", "/usr/local/bin/lightweave-wifi-home")
        helper_path = shutil.which(helper) if "/" not in helper else helper
        if helper_path and Path(helper_path).exists():
            command = [helper_path, ssid]
            if password:
                command.append(password)
        else:
            nmcli = nmcli_path()
            if not nmcli:
                raise RuntimeError("nmcli is not installed")
            command = sudo_command([nmcli, "dev", "wifi", "connect", ssid])
            if password:
                command.extend(["password", password])
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)

    def run_hotspot_start() -> None:
        delay_s = float(os.getenv("CONTROL_WIFI_JOIN_DELAY_S", "1.0"))
        if delay_s > 0:
            time.sleep(delay_s)
        nmcli = nmcli_path()
        if not nmcli:
            raise RuntimeError("nmcli is not installed")
        connection = os.getenv("CONTROL_HOTSPOT_CONNECTION", "BasketsSetup")
        subprocess.run(sudo_command([nmcli, "con", "up", connection]), check=True, capture_output=True, text=True, timeout=30)

    @app.get("/login")
    async def login_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "login.html")

    @app.get("/api/auth/session")
    async def auth_session(request: Request) -> dict[str, bool]:
        token = request.cookies.get(SESSION_COOKIE)
        return {"authenticated": live_session(token) is not None}

    @app.get("/api/health")
    async def health():
        result = {
            "ok": True,
            "version": app.state.running_version,
            "commit": app.state.running_commit,
        }
        if not app.state.settings.serial_mode:
            return result
        try:
            provisioner = await app.state.provisioning_client.status()
        except ProvisioningClientError as error:
            return JSONResponse(
                {**result, "ok": False, "provisioner": {"available": False, "error": str(error)}},
                status_code=503,
            )
        if provisioner.get("available") is not True:
            return JSONResponse(
                {**result, "ok": False, "provisioner": provisioner},
                status_code=503,
            )
        return {**result, "provisioner": {"available": True}}

    @app.post("/api/internal/provisioning/reserve-id")
    async def reserve_provisioning_id(
        request: Request,
    ) -> dict[str, Any]:
        expected = app.state.provisioner_token
        authorization = request.headers.get("authorization", "")
        supplied = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        if not expected:
            raise HTTPException(status_code=503, detail="provisioner ID authority is disabled")
        if not app.state.settings.serial_mode:
            raise HTTPException(status_code=503, detail="provisioner ID authority requires serial mode")
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid provisioner credential")
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > PROVISIONING_ID_BODY_LIMIT:
                    raise HTTPException(status_code=413, detail="request body too large")
            except ValueError as error:
                raise HTTPException(status_code=400, detail="invalid content length") from error
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > PROVISIONING_ID_BODY_LIMIT:
                raise HTTPException(status_code=413, detail="request body too large")
        try:
            payload = ProvisioningIdRequest.model_validate_json(body)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail="invalid ID reservation request") from error
        try:
            ack = await conductor_call("reserve_id", payload.mac.upper(), payload.reported_id)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if ack.get("ok") is not True:
            raise HTTPException(status_code=409, detail=str(ack.get("error") or "ID reservation failed"))
        node_id = ack.get("node_id")
        created = ack.get("created")
        if not isinstance(node_id, int) or isinstance(node_id, bool) or not 1 <= node_id <= 65535:
            raise HTTPException(status_code=502, detail="conductor returned an invalid permanent ID")
        if not isinstance(created, bool):
            raise HTTPException(status_code=502, detail="conductor returned an invalid reservation result")
        return {"mac": payload.mac.upper(), "node_id": node_id, "created": created}

    @app.post("/api/auth/login")
    async def auth_login(request: Request) -> Response:
        client_ip = request_client_ip(request)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > LOGIN_BODY_LIMIT:
                    app.state.auth_manager.record_failed_attempt(client_ip)
                    return JSONResponse({"detail": "request body too large"}, status_code=413)
            except ValueError:
                app.state.auth_manager.record_failed_attempt(client_ip)
                return JSONResponse({"detail": "invalid credentials"}, status_code=401)

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > LOGIN_BODY_LIMIT:
                app.state.auth_manager.record_failed_attempt(client_ip)
                return JSONResponse({"detail": "request body too large"}, status_code=413)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            app.state.auth_manager.record_failed_attempt(client_ip)
            return JSONResponse({"detail": "invalid credentials"}, status_code=401)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"password"}
            or not isinstance(payload["password"], str)
        ):
            app.state.auth_manager.record_failed_attempt(client_ip)
            return JSONResponse({"detail": "invalid credentials"}, status_code=401)

        outcome = await app.state.auth_manager.authenticate(client_ip, payload["password"])
        if outcome.status in {AuthStatus.RATE_LIMITED, AuthStatus.BUSY}:
            return JSONResponse({"detail": "try again later"}, status_code=429)
        if not outcome.authenticated or outcome.token is None:
            return JSONResponse({"detail": "invalid credentials"}, status_code=401)

        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            outcome.token,
            max_age=int(app.state.auth_manager.session_lifetime_s),
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/auth/logout", status_code=204)
    async def auth_logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            app.state.auth_manager.revoke_session(token)
            await close_session_websockets({token})
        response = Response(status_code=204)
        response.delete_cookie(
            SESSION_COOKIE,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        try:
            return await conductor_call("snapshot")
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/provisioning/status")
    async def get_provisioning_status() -> dict[str, Any]:
        return await provisioning_call("status")

    @app.post("/api/provisioning/session")
    async def start_provisioning_session(payload: ProvisioningSessionRequest) -> dict[str, Any]:
        result = await provisioning_call(
            "start_session",
            max_workers=payload.max_workers,
            factory=payload.factory,
        )
        await publish({"type": "provisioning", "provisioning": result})
        return result

    @app.delete("/api/provisioning/session")
    async def stop_provisioning_session() -> dict[str, Any]:
        result = await provisioning_call("stop_session")
        await publish({"type": "provisioning", "provisioning": result})
        return result

    @app.put("/api/provisioning/slots")
    async def map_provisioning_slot(payload: ProvisioningSlotRequest) -> dict[str, Any]:
        result = await provisioning_call(
            "map_slot",
            port_id=payload.port_id,
            slot=payload.slot,
        )
        await publish({"type": "provisioning", "provisioning": result})
        return result

    @app.post("/api/provisioning/jobs/{job_id}/retry")
    async def retry_provisioning_job(job_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(status_code=404, detail="provisioning job not found")
        result = await provisioning_call("retry", job_id)
        await publish({"type": "provisioning", "provisioning": result})
        return result

    @app.get("/api/releases")
    async def get_releases() -> dict[str, Any]:
        snapshot = app.state.latest_snapshot
        if (
            snapshot is None
            or time.monotonic() - app.state.latest_snapshot_at > RELEASE_SNAPSHOT_MAX_AGE_S
        ):
            try:
                snapshot = await conductor_call("snapshot")
            except SerialProtocolError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
        return release_status(
            app.state.release_catalog,
            app.state.deployment_record,
            snapshot,
            running_version=app.state.running_version,
            running_commit=app.state.running_commit,
        )

    @app.get("/api/lanterns")
    async def get_lanterns() -> list[dict[str, Any]]:
        try:
            lanterns = await conductor_call("lanterns")
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        groups = await group_store_call("list")
        return enrich_lantern_groups(lanterns, groups)

    @app.get("/api/network/wifi")
    async def get_wifi_status() -> dict[str, Any]:
        status = await asyncio.to_thread(wifi_status)
        status["allow_changes"] = app.state.settings.allow_network_changes
        return {"wifi": status}

    @app.post("/api/network/wifi")
    async def join_wifi(request: WifiJoinRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
        if not app.state.settings.allow_network_changes:
            raise HTTPException(status_code=403, detail="network changes are disabled")
        if not request.ssid.strip():
            raise HTTPException(status_code=400, detail="SSID is required")
        if not nmcli_path() and not Path(os.getenv("CONTROL_WIFI_JOIN_COMMAND", "/usr/local/bin/lightweave-wifi-home")).exists():
            raise HTTPException(status_code=503, detail="Wi-Fi management is not available on this host")
        background_tasks.add_task(run_wifi_join, request.ssid.strip(), request.password)
        return {
            "ok": True,
            "message": f"joining {request.ssid.strip()}",
            "note": "The Pi may leave this network and the browser may disconnect.",
        }

    @app.post("/api/network/hotspot")
    async def start_hotspot(background_tasks: BackgroundTasks) -> dict[str, Any]:
        if not app.state.settings.allow_network_changes:
            raise HTTPException(status_code=403, detail="network changes are disabled")
        if not nmcli_path():
            raise HTTPException(status_code=503, detail="Wi-Fi management is not available on this host")
        connection = os.getenv("CONTROL_HOTSPOT_CONNECTION", "BasketsSetup")
        background_tasks.add_task(run_hotspot_start)
        return {
            "ok": True,
            "message": "starting Basketnet",
            "connection": connection,
            "note": "The Pi may leave this network and the browser may disconnect.",
        }

    @app.get("/api/patterns")
    async def list_patterns() -> dict[str, Any]:
        try:
            return {"patterns": await pattern_store_call("list")}
        except PatternStoreError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.get("/api/groups")
    async def list_groups() -> dict[str, Any]:
        try:
            return {"groups": await group_store_call("list")}
        except GroupStoreError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.put("/api/groups/{group_id}")
    async def update_group_name(group_id: int, request: GroupNameUpdate) -> dict[str, Any]:
        if group_id < 0 or group_id >= GROUP_COUNT:
            raise HTTPException(status_code=404, detail="unknown group")
        try:
            group = await group_store_call("update", group_id, request.name)
        except GroupStoreError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        await publish_state("group-name")
        return {"ok": True, "message": f"renamed {group['label']}", "group": group}

    @app.post("/api/patterns")
    async def create_pattern(request: PatternLibraryEntry) -> dict[str, Any]:
        try:
            pattern = await pattern_store_call(
                "create",
                request.name,
                request.pattern,
                request.brightness,
                request.params,
            )
        except PatternStoreError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "pattern": pattern}

    @app.get("/api/patterns/{pattern_id}")
    async def get_pattern(pattern_id: str) -> dict[str, Any]:
        try:
            pattern = await pattern_store_call("get", pattern_id)
        except PatternStoreError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        if not pattern:
            raise HTTPException(status_code=404, detail="unknown pattern")
        return {"pattern": pattern}

    @app.put("/api/patterns/{pattern_id}")
    async def update_pattern_library_entry(pattern_id: str, request: PatternLibraryEntry) -> dict[str, Any]:
        try:
            pattern = await pattern_store_call(
                "update",
                pattern_id,
                request.name,
                request.pattern,
                request.brightness,
                request.params,
            )
        except PatternStoreError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if not pattern:
            raise HTTPException(status_code=404, detail="unknown pattern")
        return {"ok": True, "pattern": pattern}

    @app.delete("/api/patterns/{pattern_id}")
    async def delete_pattern(pattern_id: str) -> dict[str, Any]:
        try:
            deleted = await pattern_store_call("delete", pattern_id)
        except PatternStoreError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        if not deleted:
            raise HTTPException(status_code=404, detail="unknown pattern")
        return {"ok": True, "message": "pattern deleted"}

    @app.post("/api/patterns/{pattern_id}/broadcast")
    async def broadcast_pattern_library_entry(
        pattern_id: str,
        group_id: int | None = Query(default=None, ge=0, lt=GROUP_COUNT),
    ) -> dict[str, Any]:
        try:
            pattern = await pattern_store_call("get", pattern_id)
        except PatternStoreError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        if not pattern:
            raise HTTPException(status_code=404, detail="unknown pattern")
        try:
            args: tuple[Any, ...] = (
                pattern["pattern"], pattern["brightness"], pattern["params"]
            )
            if group_id is not None:
                args += (group_id,)
            ack = await conductor_call("update_pattern", *args)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_state("pattern")
        return {"ok": True, "message": ack.get("message", "pattern broadcast"), "pattern": pattern, "ack": ack}

    @app.get("/api/calibration/frames")
    async def list_calibration_frames() -> dict[str, Any]:
        return {"frames": await calibration_store_call("list_frames")}

    @app.get("/api/calibration/frames/{frame_id}/image")
    async def get_calibration_frame_image(frame_id: str) -> FileResponse:
        frame = app.state.calibration_store.frame(frame_id)
        if frame is None:
            raise HTTPException(status_code=404, detail="unknown calibration frame")
        return FileResponse(frame.path)

    @app.put("/api/calibration/frames")
    async def upload_calibration_frame(request: Request, filename: str = "calibration.png") -> dict[str, Any]:
        data = await request.body()
        try:
            frame = await calibration_store_call("add_image", filename, data)
        except CalibrationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        await publish({"type": "ack", "action": "calibration-frame", "frame": frame})
        return {"ok": True, "message": "calibration frame uploaded", "frame": frame}

    @app.post("/api/calibration/frames/{frame_id}/detect")
    async def detect_calibration_frame(frame_id: str, request: CalibrationDetectRequest) -> dict[str, Any]:
        try:
            detection = await calibration_store_call("detect", frame_id, request.threshold, request.min_area)
        except CalibrationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "frame_id": frame_id, "detection": detection}

    @app.post("/api/calibration/decode")
    async def decode_calibration_sequence(request: CalibrationDecodeRequest) -> dict[str, Any]:
        try:
            decoded = await calibration_store_call(
                "decode_sequence",
                request.frame_ids,
                request.threshold,
                request.min_area,
                request.max_distance,
            )
        except CalibrationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "decoded": decoded}

    @app.post("/api/calibration/code-plan")
    async def calibration_code_plan_endpoint(request: CalibrationCodePlanRequest) -> dict[str, Any]:
        roster_macs = request.roster_macs
        if roster_macs is None:
            try:
                roster_macs = calibration_roster_macs(await conductor_call("snapshot"))
            except SerialProtocolError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
        try:
            plan = calibration_code_plan(
                roster_macs,
                first_code=request.first_code,
                bit_count=request.bit_count,
                min_hamming_distance=request.min_hamming_distance,
            )
        except CalibrationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "plan": plan}

    @app.post("/api/calibration/propose-layout")
    async def propose_calibration_layout(request: CalibrationProposeRequest) -> dict[str, Any]:
        roster_macs = request.roster_macs
        code_map = [item.model_dump() for item in request.code_map] if request.code_map is not None else None
        if roster_macs is None and code_map is None:
            try:
                roster_macs = calibration_roster_macs(await conductor_call("snapshot"))
            except SerialProtocolError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
        elif roster_macs is None:
            roster_macs = [str(item["mac"]) for item in code_map or []]
        try:
            proposal = await calibration_store_call(
                "propose_layout",
                request.frame_ids,
                roster_macs,
                request.threshold,
                request.min_area,
                request.max_distance,
                request.first_code,
                code_map,
            )
        except CalibrationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "roster_macs": roster_macs, "proposal": proposal}

    @app.post("/api/calibration/apply-proposal")
    async def apply_calibration_proposal(request: CalibrationApplyRequest) -> dict[str, Any]:
        saved = []
        failed = []
        async with app.state.conductor_lock:
            for assignment in request.assignments:
                try:
                    ack = await asyncio.to_thread(
                        app.state.conductor.assign,
                        assignment.mac,
                        assignment.x,
                        assignment.y,
                    )
                except SerialProtocolError as error:
                    failed.append({"mac": assignment.mac, "error": str(error)})
                    continue
                if ack.get("ok"):
                    saved.append({
                        "mac": assignment.mac,
                        "x": assignment.x,
                        "y": assignment.y,
                        "code": assignment.code,
                        "bits": assignment.bits,
                    })
                else:
                    failed.append({
                        "mac": assignment.mac,
                        "error": str(ack.get("error") or "assign failed"),
                    })
        if saved:
            await publish_state("calibration-apply")
        skipped = list(request.missing) + list(request.ambiguous)
        message = f"saved {len(saved)} lantern location{'s' if len(saved) != 1 else ''}"
        if skipped:
            message += f"; {len(skipped)} skipped"
        if failed:
            message += f"; {len(failed)} failed"
        return {
            "ok": not failed,
            "message": message,
            "saved": saved,
            "skipped": skipped,
            "failed": failed,
        }

    @app.post("/api/calibration/simulate")
    async def simulate_calibration_sequence(request: CalibrationSyntheticRequest) -> dict[str, Any]:
        if request.nodes is None:
            try:
                nodes = calibration_positioned_nodes(await conductor_call("snapshot"))
            except SerialProtocolError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
        else:
            nodes = [node.model_dump() for node in request.nodes]
        try:
            simulation = await calibration_store_call(
                "add_synthetic_sequence",
                nodes,
                request.width,
                request.height,
                request.first_code,
                request.bit_count,
                request.blob_radius,
                request.led_value,
                request.jitter_px,
                request.glare_count,
                request.glare_value,
                request.missing_frames,
                request.perspective,
                request.min_hamming_distance,
                request.threshold,
                request.min_area,
                request.max_distance,
            )
        except CalibrationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "simulation": simulation}

    @app.get("/preview")
    async def preview(
        pattern: str,
        brightness: int = Query(default=48, ge=0, le=192),
        params: str | None = None,
        t: int = Query(default=0, ge=0),
        width: int = Query(default=640, ge=80, le=2000),
        height: int = Query(default=480, ge=80, le=2000),
        hue: int | None = None,
        saturation: int | None = None,
        period: int | None = None,
        spatial: int | None = None,
        wavelength: int | None = None,
        texture: int | None = None,
    ) -> Response:
        try:
            state = await conductor_call("snapshot")
            decoded_params = parse_params(params, {
                "hue": hue,
                "saturation": saturation,
                "period": period,
                "spatial": spatial,
                "wavelength": wavelength,
                "texture": texture,
            })
            png = await asyncio.to_thread(
                render_preview_png,
                state,
                pattern,
                brightness,
                decoded_params,
                t,
                width,
                height,
            )
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return Response(content=png, media_type="image/png")

    @app.get("/preview.json")
    async def preview_json(
        pattern: str,
        brightness: int = Query(default=48, ge=0, le=192),
        params: str | None = None,
        t: int = Query(default=0, ge=0),
        hue: int | None = None,
        saturation: int | None = None,
        period: int | None = None,
        spatial: int | None = None,
        wavelength: int | None = None,
        texture: int | None = None,
    ) -> dict[str, Any]:
        try:
            state = await conductor_call("snapshot")
            decoded_params = parse_params(params, {
                "hue": hue,
                "saturation": saturation,
                "period": period,
                "spatial": spatial,
                "wavelength": wavelength,
                "texture": texture,
            })
            return await asyncio.to_thread(render_preview_data, state, pattern, brightness, decoded_params, t)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/preview/frames.json")
    async def preview_frames_json(
        pattern: str,
        brightness: int = Query(default=48, ge=0, le=192),
        params: str | None = None,
        duration_ms: int = Query(default=8000, ge=500, le=60000),
        fps: int = Query(default=4, ge=1, le=24),
        hue: int | None = None,
        saturation: int | None = None,
        period: int | None = None,
        spatial: int | None = None,
        wavelength: int | None = None,
        texture: int | None = None,
    ) -> dict[str, Any]:
        try:
            state = await conductor_call("snapshot")
            decoded_params = parse_params(params, {
                "hue": hue,
                "saturation": saturation,
                "period": period,
                "spatial": spatial,
                "wavelength": wavelength,
                "texture": texture,
            })
            return await asyncio.to_thread(
                render_preview_frames,
                state,
                pattern,
                brightness,
                decoded_params,
                duration_ms,
                fps,
            )
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/review")
    async def review(
        pattern: str,
        brightness: int = Query(default=48, ge=0, le=192),
        params: str | None = None,
        duration_ms: int = Query(default=8000, ge=500, le=60000),
        fps: int = Query(default=4, ge=1, le=24),
        hue: int | None = None,
        saturation: int | None = None,
        period: int | None = None,
        spatial: int | None = None,
        wavelength: int | None = None,
        texture: int | None = None,
    ) -> dict[str, Any]:
        try:
            state = await conductor_call("snapshot")
            decoded_params = parse_params(params, {
                "hue": hue,
                "saturation": saturation,
                "period": period,
                "spatial": spatial,
                "wavelength": wavelength,
                "texture": texture,
            })
            return await asyncio.to_thread(
                review_preview,
                state,
                pattern,
                brightness,
                decoded_params,
                duration_ms,
                fps,
            )
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/patterns/{pattern_id}/preview")
    async def preview_pattern_library_entry(
        pattern_id: str,
        t: int = Query(default=0, ge=0),
        width: int = Query(default=640, ge=80, le=2000),
        height: int = Query(default=480, ge=80, le=2000),
    ) -> Response:
        try:
            item = await pattern_store_call("get", pattern_id)
            if not item:
                raise HTTPException(status_code=404, detail="unknown pattern")
            state = await conductor_call("snapshot")
            png = await asyncio.to_thread(
                render_preview_png,
                state,
                item["pattern"],
                item["brightness"],
                item["params"],
                t,
                width,
                height,
            )
        except HTTPException:
            raise
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except PatternStoreError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return Response(content=png, media_type="image/png")

    @app.get("/api/patterns/{pattern_id}/preview.json")
    async def preview_pattern_library_entry_json(
        pattern_id: str,
        t: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        try:
            item = await pattern_store_call("get", pattern_id)
            if not item:
                raise HTTPException(status_code=404, detail="unknown pattern")
            state = await conductor_call("snapshot")
            return await asyncio.to_thread(
                render_preview_data,
                state,
                item["pattern"],
                item["brightness"],
                item["params"],
                t,
            )
        except HTTPException:
            raise
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except PatternStoreError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/patterns/{pattern_id}/preview/frames.json")
    async def preview_pattern_library_entry_frames_json(
        pattern_id: str,
        duration_ms: int = Query(default=8000, ge=500, le=60000),
        fps: int = Query(default=4, ge=1, le=24),
    ) -> dict[str, Any]:
        try:
            item = await pattern_store_call("get", pattern_id)
            if not item:
                raise HTTPException(status_code=404, detail="unknown pattern")
            state = await conductor_call("snapshot")
            return await asyncio.to_thread(
                render_preview_frames,
                state,
                item["pattern"],
                item["brightness"],
                item["params"],
                duration_ms,
                fps,
            )
        except HTTPException:
            raise
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except PatternStoreError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/patterns/{pattern_id}/review")
    async def review_pattern_library_entry(
        pattern_id: str,
        duration_ms: int = Query(default=8000, ge=500, le=60000),
        fps: int = Query(default=4, ge=1, le=24),
    ) -> dict[str, Any]:
        try:
            item = await pattern_store_call("get", pattern_id)
            if not item:
                raise HTTPException(status_code=404, detail="unknown pattern")
            state = await conductor_call("snapshot")
            return await asyncio.to_thread(
                review_preview,
                state,
                item["pattern"],
                item["brightness"],
                item["params"],
                duration_ms,
                fps,
            )
        except HTTPException:
            raise
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except PatternStoreError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/lanterns/{mac}/identify")
    async def identify(mac: str) -> dict[str, Any]:
        try:
            ack = await conductor_call("identify", mac)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=404, detail=ack["error"])
        await publish({"type": "ack", "action": "identify", "mac": mac, "ack": ack})
        return ack

    @app.post("/api/lanterns/{mac}/assign")
    async def assign(mac: str, request: AssignRequest) -> dict[str, Any]:
        try:
            ack = await conductor_call("assign", mac, request.x, request.y)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=404, detail=ack["error"])
        await publish_state("assign")
        return ack

    @app.post("/api/lanterns/{mac}/forget")
    async def forget(mac: str) -> dict[str, Any]:
        try:
            ack = await conductor_call("forget", mac)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=404, detail=ack["error"])
        await publish_state("forget")
        return ack

    @app.post("/api/lanterns/{mac}/group")
    async def assign_group(mac: str, request: GroupUpdate) -> dict[str, Any]:
        try:
            ack = await conductor_call("assign_group", mac, request.group_id)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_state("group")
        return ack

    @app.post("/api/lanterns/{mac}/led-count")
    async def assign_led_count(mac: str, request: LedCountUpdate) -> dict[str, Any]:
        try:
            ack = await conductor_call("assign_led_count", mac, request.led_count)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_state("led-count")
        return ack

    @app.post("/api/lanterns/replace")
    async def replace(request: ReplaceRequest) -> dict[str, Any]:
        try:
            ack = await conductor_call("replace", request.old_mac, request.new_mac)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=404, detail=ack["error"])
        await publish_state("replace")
        return ack

    @app.post("/api/show/pattern")
    async def update_pattern(request: PatternUpdate) -> dict[str, Any]:
        try:
            args: tuple[Any, ...] = (
                request.pattern, request.brightness, request.params
            )
            if request.group_id is not None:
                args += (request.group_id,)
            ack = await conductor_call("update_pattern", *args)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_state("pattern")
        return ack

    @app.post("/api/show/blackout")
    async def blackout() -> dict[str, Any]:
        try:
            ack = await conductor_call("blackout")
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        await publish_state("blackout")
        return ack

    @app.post("/api/show/restore")
    async def restore_blackout() -> dict[str, Any]:
        try:
            ack = await conductor_call("restore_blackout")
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_state("blackout-restore")
        return ack

    @app.post("/api/operations/calibration-mode")
    async def update_calibration_mode(request: CalibrationModeUpdate) -> dict[str, Any]:
        try:
            ack = await set_live_calibration_mode(request.enabled)
        except CalibrationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_state("calibration-mode")
        return ack

    @app.post("/api/operations/power-policy")
    async def update_power_policy(request: PowerPolicyUpdate) -> dict[str, Any]:
        try:
            ack = await conductor_call("update_power_policy", request.model_dump())
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_state("power-policy")
        return ack

    @app.post("/api/operations/field-power")
    async def update_field_power(request: FieldPowerUpdate) -> dict[str, Any]:
        overrides = {
            "sleep": {"force_awake": False, "force_sleep": True},
            "wake": {"force_awake": True, "force_sleep": False},
            "schedule": {"force_awake": False, "force_sleep": False},
        }
        try:
            ack = await conductor_call("update_power_policy", overrides[request.mode])
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        ack["mode"] = request.mode
        await publish_state("field-power")
        return ack

    @app.post("/api/operations/power-monitor")
    async def update_power_monitor(request: PowerMonitorUpdate) -> dict[str, Any]:
        async with app.state.conductor_lock:
            app.state.power_monitor_config = request.model_dump()
        await publish_state("power-monitor")
        return {"ok": True, "message": "power monitor settings changed", "power_monitor": app.state.power_monitor_config}

    @app.post("/api/lanterns/{mac}/power-sync-full")
    async def sync_lantern_power_full(mac: str) -> dict[str, Any]:
        try:
            state = await conductor_call("snapshot")
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        lantern = next((item for item in state.get("lanterns") or [] if item.get("mac") == mac), None)
        if not lantern:
            raise HTTPException(status_code=404, detail="unknown lantern")
        power = lantern.get("power") or {}
        wh = power.get("wh")
        if not isinstance(wh, (int, float)):
            raise HTTPException(status_code=400, detail="lantern has no power reading")
        anchor = {"wh": float(wh), "ts": time.time(), "manual": True, "bus_v": power.get("bus_v")}
        app.state.power_full_anchors[mac] = anchor
        await publish_state("power-sync-full")
        return {"ok": True, "message": f"{lantern.get('label') or mac} synced to 100%", "anchor": anchor}

    @app.post("/api/operations/ota-mode")
    async def update_ota_mode(request: OtaModeUpdate) -> dict[str, Any]:
        if app.state.ota_reserved:
            raise HTTPException(status_code=409, detail="OTA mode is managed by the running update")
        try:
            ack = await conductor_call("set_ota_mode", request.enabled)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_state("ota-mode")
        return ack

    @app.get("/api/operations/ota-artifact")
    async def get_ota_artifact() -> dict[str, Any]:
        return {"artifact": app.state.ota_store.current()}

    @app.put("/api/operations/ota-artifact")
    async def stage_ota_artifact(request: Request, filename: str = "firmware.bin") -> dict[str, Any]:
        async with app.state.ota_start_lock:
            if app.state.ota_reserved:
                raise HTTPException(status_code=423, detail="OTA install owns the conductor")
            data = await request.body()
            try:
                artifact = await asyncio.to_thread(app.state.ota_store.stage, filename, data)
            except OtaArtifactError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        await publish({"type": "ack", "action": "ota-artifact", "artifact": artifact})
        return {"ok": True, "message": "firmware staged", "artifact": artifact}

    @app.get("/api/operations/ota-install")
    async def get_ota_install() -> dict[str, Any]:
        install = app.state.ota_install
        if install.get("complete") is True and not install.get("nodes") and install.get("size"):
            nodes = await infer_ota_complete_nodes(
                int(install["size"]),
                int(install.get("crc32") or 0),
                {str(mac) for mac in install.get("target_macs") or []},
            )
            if nodes:
                install.update({"nodes": nodes})
        return {"install": ota_install_progress(app.state.ota_install)}

    async def perform_ota_install(
        artifact: Any,
        state: dict[str, Any],
        data: bytes,
        *,
        resume: bool = False,
    ) -> dict[str, Any]:
        all_lanterns = {
            str(item.get("mac")): item
            for item in state.get("lanterns") or []
            if item.get("mac")
        }
        expected_lanterns = expected_ota_lanterns(state)
        expected_macs = set(str(mac) for mac in app.state.ota_install.get("target_macs") or [])
        missing_rounds: dict[str, int] = {}
        repair_offsets: dict[str, int] = {}
        repair_stalls: dict[str, int] = {}
        delivery_confirmed_offsets = {
            str(mac): int(offset)
            for mac, offset in dict(
                app.state.ota_install.get("delivery_confirmed_offsets") or {}
            ).items()
        }
        full_replay_macs = set(
            str(mac)
            for mac in app.state.ota_install.get("full_replay_macs") or []
        )

        def defer_full_replay(mac: str) -> None:
            full_replay_macs.add(mac)
            app.state.ota_install.update({
                "full_replay_macs": sorted(full_replay_macs),
            })

        async def call(method: str, *args: Any) -> dict[str, Any]:
            if app.state.ota_pause_requested:
                raise OtaPauseRequested
            return await conductor_call(method, *args)

        async def call_until_ok(method: str, *args: Any) -> dict[str, Any]:
            attempt = 0
            while True:
                attempt += 1
                try:
                    ack = await call(method, *args)
                except SerialProtocolError as error:
                    app.state.ota_install.update({
                        "phase": "waiting",
                        "last_retry": {"attempt": attempt, "error": str(error)},
                    })
                    await asyncio.sleep(min(5.0, 0.25 * attempt))
                    continue
                if ack.get("ok") is True:
                    return ack
                error = str(ack.get("error") or "OTA command failed")
                if (
                    "send failed" in error
                    or (error in OTA_CHUNK_RETRYABLE_ERRORS and attempt <= OTA_CHUNK_RETRIES)
                ):
                    app.state.ota_install.update({
                        "phase": "waiting",
                        "last_retry": {"attempt": attempt, "error": error},
                    })
                    await asyncio.sleep(min(5.0, 0.25 * attempt))
                    continue
                raise HTTPException(
                    status_code=503 if error in OTA_CHUNK_RETRYABLE_ERRORS else 400,
                    detail=error,
                )

        @lru_cache(maxsize=None)
        def checkpoint_crc(offset: int) -> int:
            return zlib.crc32(data[:offset]) & 0xFFFFFFFF

        async def progress() -> dict[str, Any]:
            value = await call_until_ok("ota_progress")
            apply_ota_progress(value, artifact)
            return value

        async def probe() -> dict[str, Any]:
            ack = await call_until_ok("ota_probe")
            settle_s = max(0.0, float(ack.get("settle_s", OTA_STATUS_SETTLE_S)))
            if settle_s:
                await asyncio.sleep(settle_s)
            return await progress()

        async def repair_checkpoint(frontier: int) -> list[dict[str, Any]]:
            repair_round = 0
            final_checkpoint = frontier == artifact.size
            while True:
                repair_round += 1
                if repair_round % 10 == 1:
                    await call_until_ok("set_ota_mode", True)
                current = await probe()
                nodes = fresh_ota_nodes(current.get("nodes") or [])
                by_mac = {str(node.get("mac")): node for node in nodes if node.get("mac")}
                pending: list[str] = []
                repaired_chunks = 0

                for mac in sorted(expected_macs):
                    if delivery_confirmed_offsets.get(mac) == frontier:
                        continue
                    if mac in full_replay_macs and not final_checkpoint:
                        continue
                    replayed_from_zero = False
                    node = by_mac.get(mac)
                    if node is None:
                        missing_rounds[mac] = missing_rounds.get(mac, 0) + 1
                        if missing_rounds[mac] < 2:
                            pending.append(mac)
                            continue
                        if not final_checkpoint:
                            defer_full_replay(mac)
                            continue
                        offset = 0
                        await call_until_ok("ota_restart", mac)
                        replayed_from_zero = True
                        repair_offsets[mac] = 0
                        repair_stalls[mac] = 0
                        app.state.ota_install.update({
                            "repair_restarts": int(
                                app.state.ota_install.get("repair_restarts") or 0
                            ) + 1,
                        })
                    else:
                        missing_rounds[mac] = 0
                        offset = int(node.get("offset") or 0)
                        crc32 = int(node.get("crc32") or 0)
                        phase = str(node.get("phase") or "idle")
                        error = str(node.get("error") or "none")
                        if (
                            offset == frontier
                            and crc32 == checkpoint_crc(frontier)
                            and phase not in {"failed"}
                        ):
                            repair_offsets.pop(mac, None)
                            repair_stalls.pop(mac, None)
                            continue
                        restart = (
                            offset < 0
                            or offset > frontier
                            or crc32 != checkpoint_crc(max(0, min(offset, frontier)))
                            or phase == "failed"
                            or error in {
                                "begin failed", "chunk exceeds image size",
                                "flash write failed", "crc mismatch", "finalize failed",
                            }
                        )
                        if not restart:
                            previous_offset = repair_offsets.get(mac)
                            if previous_offset == offset:
                                repair_stalls[mac] = repair_stalls.get(mac, 0) + 1
                            else:
                                repair_stalls[mac] = 0
                            repair_offsets[mac] = offset
                            restart = repair_stalls[mac] >= OTA_REPAIR_STALL_ROUNDS
                        if restart:
                            if not final_checkpoint:
                                defer_full_replay(mac)
                                repair_offsets.pop(mac, None)
                                repair_stalls.pop(mac, None)
                                continue
                            await call_until_ok("ota_restart", mac)
                            offset = 0
                            replayed_from_zero = True
                            repair_offsets[mac] = 0
                            repair_stalls[mac] = 0
                            app.state.ota_install.update({
                                "repair_restarts": int(
                                    app.state.ota_install.get("repair_restarts") or 0
                                ) + 1,
                            })

                    pending.append(mac)
                    app.state.ota_install.update({
                        "phase": "repairing",
                        "repair_round": repair_round,
                        "repairing_macs": pending,
                        "nodes": nodes,
                    })
                    while offset < frontier:
                        chunk = data[offset : min(frontier, offset + artifact.chunk_size)]
                        await call_until_ok("ota_repair", mac, offset, chunk)
                        offset += len(chunk)
                        repaired_chunks += 1
                    if replayed_from_zero:
                        # Legacy performers predate OTA_QUERY and abort their
                        # writer on the first broadcast gap. A targeted begin +
                        # delivery-acknowledged replay is the only migration
                        # path they understand; requiring an immediate query
                        # response would mistake their stale status row for a
                        # failed replay and restart them forever. The later
                        # ota_end + full-image CRC/staged barrier remains the
                        # authoritative proof before activation.
                        pending.remove(mac)
                        confirmed = set(
                            str(item)
                            for item in app.state.ota_install.get(
                                "delivery_confirmed_macs"
                            ) or []
                        )
                        confirmed.add(mac)
                        delivery_confirmed_offsets[mac] = frontier
                        app.state.ota_install.update({
                            "delivery_confirmed_macs": sorted(confirmed),
                            "delivery_confirmed_offsets": dict(
                                sorted(delivery_confirmed_offsets.items())
                            ),
                        })

                app.state.ota_install.update({
                    "repair_round": repair_round,
                    "repair_chunks": int(app.state.ota_install.get("repair_chunks") or 0)
                    + repaired_chunks,
                    "repairing_macs": pending,
                    "nodes": nodes,
                })
                if not pending:
                    return nodes
                await asyncio.sleep(min(5.0, 0.25 * repair_round))

        try:
            await call_until_ok("set_ota_mode", True)
            current = await progress()
            active = current.get("active") is True
            staged = current.get("staged") is True
            written = int(current.get("written") or 0)
            resumable = (
                (active or staged)
                and int(current.get("size") or 0) == artifact.size
                and 0 <= written <= artifact.size
                and int(current.get("crc32") or 0) == checkpoint_crc(written)
            )
            if not resumable:
                ack = await call_until_ok("ota_begin", artifact.size, artifact.crc32)
                reported_targets = {str(mac) for mac in ack.get("targets") or []}
                expected_macs = reported_targets or set(expected_lanterns)
                written = 0
                staged = False
            else:
                reported_targets = {str(mac) for mac in current.get("targets") or []}
                expected_macs = reported_targets or expected_macs or set(expected_lanterns)

            if not expected_macs:
                raise HTTPException(status_code=400, detail="no performers online")
            expected_lanterns = {
                mac: all_lanterns.get(mac, {"mac": mac, "label": mac})
                for mac in expected_macs
            }
            deferred = [
                {"mac": mac, "label": item.get("label") or mac}
                for mac, item in sorted(all_lanterns.items())
                if mac not in expected_macs
            ]
            app.state.ota_install.update({
                "phase": "broadcasting" if not staged else "staged",
                "target_macs": sorted(expected_macs),
                "target_count": len(expected_macs),
                "deferred": deferred,
                "deferred_count": len(deferred),
                "resumed": resume,
            })

            offset = written
            if offset > 0 and not staged:
                await repair_checkpoint(offset)
            while offset < len(data):
                chunk = data[offset : offset + artifact.chunk_size]
                ack = await call_until_ok("ota_chunk", offset, chunk)
                if ack.get("ok") is not True:
                    continue
                offset += len(chunk)
                chunks_sent = min(
                    (offset + artifact.chunk_size - 1) // artifact.chunk_size,
                    artifact.chunks,
                )
                app.state.ota_install.update_volatile({
                    "phase": "broadcasting",
                    "bytes_sent": offset,
                    "chunks_sent": chunks_sent,
                })
                if chunks_sent == artifact.chunks or chunks_sent % OTA_CHECKPOINT_CHUNKS == 0:
                    await repair_checkpoint(offset)

            await repair_checkpoint(artifact.size)
            app.state.ota_install.update({"phase": "staging", "repairing_macs": []})
            while True:
                await call_until_ok("ota_end")
                current = await probe()
                nodes = fresh_ota_nodes(current.get("nodes") or [])
                by_mac = {str(node.get("mac")): node for node in nodes if node.get("mac")}
                staged_macs = {
                    mac
                    for mac in expected_macs
                    if mac in by_mac
                    and by_mac[mac].get("phase") in {"staged", "activating", "complete"}
                    and int(by_mac[mac].get("offset") or 0) == artifact.size
                    and int(by_mac[mac].get("crc32") or 0) == artifact.crc32
                }
                app.state.ota_install.update({"nodes": nodes, "staged_macs": sorted(staged_macs)})
                if staged_macs == expected_macs:
                    break
                await repair_checkpoint(artifact.size)

            activated = set(str(mac) for mac in app.state.ota_install.get("activated_macs") or [])
            for mac in sorted(expected_macs):
                while mac not in activated:
                    current = await progress()
                    node = next(
                        (item for item in current.get("nodes") or [] if str(item.get("mac")) == mac),
                        None,
                    )
                    if node and node.get("phase") == "complete":
                        activated.add(mac)
                        break
                    app.state.ota_install.update({
                        "phase": "activating",
                        "active_mac": mac,
                        "activated_macs": sorted(activated),
                    })
                    await call_until_ok("ota_activate", mac)
                    current = await progress()
                    node = next(
                        (item for item in current.get("nodes") or [] if str(item.get("mac")) == mac),
                        None,
                    )
                    if node and node.get("phase") == "complete":
                        activated.add(mac)
                        break
                    await asyncio.sleep(OTA_ACTIVATION_POLL_S)
                app.state.ota_install.update({
                    "activated_macs": sorted(activated),
                    "nodes": fresh_ota_nodes((await progress()).get("nodes") or []),
                })

            app.state.ota_install.update({"phase": "activating-conductor", "active_mac": None})
            try:
                conductor_activation = await call("ota_activate", None)
            except SerialProtocolError as error:
                # The expected reboot can sever serial after the command was
                # accepted but before its JSON ACK reaches the Pi. Live firmware
                # identity below is the authoritative completion check.
                app.state.ota_install.update({
                    "last_retry": {"attempt": 1, "error": str(error)},
                })
            else:
                if conductor_activation.get("ok") is not True:
                    raise HTTPException(
                        status_code=400,
                        detail=str(conductor_activation.get("error") or "conductor activation failed"),
                    )
            nodes = await infer_ota_complete_nodes(artifact.size, artifact.crc32, expected_macs)
            verified_macs = {str(node["mac"]) for node in nodes}
            if verified_macs != expected_macs:
                error = "ota post-reboot verification failed"
                nodes = append_unverified_ota_failures(nodes, expected_lanterns, verified_macs)
                app.state.ota_install.update({"nodes": nodes})
                raise HTTPException(status_code=503, detail=error)
            await call_until_ok("set_ota_mode", False)
        except asyncio.CancelledError:
            app.state.ota_install.update({
                "running": True,
                "complete": False,
                "phase": "paused",
                "error": None,
                "message": "OTA paused for service restart; it will resume automatically",
            })
            raise
        except OtaPauseRequested:
            raise

        app.state.ota_install.update({
            "running": False,
            "complete": True,
            "phase": "complete",
            "error": None,
            "message": "firmware updated across the online field",
            "nodes": nodes,
            "completed_at": time.time(),
        })
        ack = {"ok": True, "message": "firmware updated across the online field", "nodes": nodes}
        await publish({"type": "ack", "action": "ota-install", "artifact": artifact.as_dict(), "ack": ack})
        return {"ok": True, "message": ack["message"], "artifact": artifact.as_dict()}

    async def ota_install_worker(
        artifact: Any,
        state: dict[str, Any],
        *,
        resume: bool = False,
    ) -> None:
        try:
            data = await asyncio.to_thread(app.state.ota_store.read_verified, artifact)
            await perform_ota_install(artifact, state, data, resume=resume)
        except asyncio.CancelledError:
            raise
        except OtaPauseRequested:
            app.state.ota_install.update({
                "running": False,
                "complete": False,
                "phase": "paused",
                "error": None,
                "message": "firmware update paused by operator; start it again to resume",
            })
        except HTTPException as error:
            app.state.ota_install.update({
                "running": False,
                "complete": False,
                "error": str(error.detail),
                "completed_at": app.state.ota_install.get("completed_at") or time.time(),
            })
        except Exception as error:
            app.state.ota_install.update({
                "running": False,
                "complete": False,
                "error": str(error) or type(error).__name__,
                "completed_at": time.time(),
            })
        finally:
            app.state.ota_reserved = False
            release_ota_operation_lock()

    @app.post("/api/operations/ota-install", status_code=202)
    async def install_ota_artifact() -> dict[str, Any]:
        async with app.state.ota_start_lock:
            if app.state.ota_reserved:
                raise HTTPException(status_code=409, detail="OTA install already running")
            artifact = app.state.ota_store.artifact()
            if artifact is None:
                raise HTTPException(status_code=400, detail="no firmware staged")
            operation_lock = acquire_ota_operation_lock()
            try:
                async with app.state.conductor_lock:
                    if app.state.ota_reserved:
                        raise HTTPException(status_code=409, detail="OTA install already running")
                    state = await asyncio.to_thread(app.state.conductor.snapshot)
                    state = enrich_state(state)
                    ota = state.get("ota") or {}
                    if not ota_ready_for_install(state):
                        blockers = ", ".join(ota.get("blocked") or ["field is not OTA-ready"])
                        raise HTTPException(status_code=400, detail=f"OTA not ready: {blockers}")
                    capability = await asyncio.to_thread(app.state.conductor.ota_probe)
                    capability_error = str(capability.get("error") or "").lower()
                    if capability.get("ok") is not True and "unknown cmd" in capability_error:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "the attached conductor must be direct-flashed to this release "
                                "before the first reliable live OTA"
                            ),
                        )
                    app.state.ota_reserved = True
            except Exception as error:
                if operation_lock is not None:
                    operation_lock.close()
                if isinstance(error, SerialProtocolError):
                    raise HTTPException(status_code=503, detail=str(error)) from error
                raise

            app.state.ota_operation_lock = operation_lock
            app.state.ota_pause_requested = False

            app.state.ota_install.reset({
                "running": True,
                "complete": False,
                "error": None,
                "phase": "starting",
                "filename": artifact.filename,
                "sha256": artifact.sha256,
                "size": artifact.size,
                "crc32": artifact.crc32,
                "bytes_sent": 0,
                "chunks_sent": 0,
                "chunks_total": artifact.chunks,
                "started_at": time.time(),
            })
            task = asyncio.create_task(ota_install_worker(artifact, state))
            app.state.ota_task = task
            return {
                "ok": True,
                "message": "OTA install accepted",
                "install": ota_install_progress(app.state.ota_install),
            }

    @app.delete("/api/operations/ota-install")
    async def pause_ota_install() -> dict[str, Any]:
        async with app.state.ota_start_lock:
            task = app.state.ota_task
            if task is None or task.done() or app.state.ota_install.get("running") is not True:
                raise HTTPException(status_code=409, detail="no OTA install is running")
            app.state.ota_pause_requested = True
            await task
            app.state.ota_task = None
            app.state.ota_reserved = False
            if app.state.ota_install.get("complete") is True:
                app.state.ota_pause_requested = False
                return {
                    "ok": True,
                    "message": "firmware update completed before it could be paused",
                    "install": ota_install_progress(app.state.ota_install),
                }
            app.state.ota_install.update({
                "running": False,
                "complete": False,
                "phase": "paused",
                "error": None,
                "message": "firmware update paused by operator; start it again to resume",
            })
            app.state.ota_pause_requested = False
            return {
                "ok": True,
                "message": "firmware update paused",
                "install": ota_install_progress(app.state.ota_install),
            }

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        token = ws.cookies.get(SESSION_COOKIE)
        session = live_session(token)
        origin = ws.headers.get("origin")
        if app.state.settings.require_https and external_scheme(ws.scope, ws.headers) != "https":
            await ws.close(code=4403)
            return
        if app.state.auth_manager.enabled and session is None:
            await ws.close(code=4401)
            return
        if origin is not None and not allowed_origin(origin, ws.scope, ws.headers):
            await ws.close(code=4403)
            return
        if token is None:
            token = "disabled-auth"
        await ws.accept()
        app.state.ws_clients[ws] = token
        try:
            state = await conductor_call("snapshot")
        except (SerialProtocolError, HTTPException) as error:
            if app.state.auth_manager.enabled and live_session(token) is None:
                app.state.ws_clients.pop(ws, None)
                with contextlib.suppress(RuntimeError):
                    await ws.close(code=4401, reason="session expired")
                return
            try:
                await ws.send_json({"type": "error", "message": str(error), "ts": time.time()})
            except (RuntimeError, WebSocketDisconnect):
                app.state.ws_clients.pop(ws, None)
                return
        else:
            if app.state.auth_manager.enabled and live_session(token) is None:
                app.state.ws_clients.pop(ws, None)
                with contextlib.suppress(RuntimeError):
                    await ws.close(code=4401, reason="session expired")
                return
            try:
                await ws.send_json({"type": "state", "state": state, "ts": time.time()})
            except (RuntimeError, WebSocketDisconnect):
                app.state.ws_clients.pop(ws, None)
                return
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            app.state.ws_clients.pop(ws, None)

    return app


app = create_app()
