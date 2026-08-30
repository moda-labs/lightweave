from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hmac
import ipaddress
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
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .adapters import (
    DEFAULT_SERIAL_REQUEST_TIMEOUT_S,
    DEFAULT_SERIAL_STATE_TIMEOUT_S,
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
from .uploaded_patterns import (
    VM_VERSION,
    UploadedPatternError,
    UploadedPatternStore,
    compile_uploaded_pattern,
    run_uploaded_pattern,
)
from .power_monitor import (
    DEFAULT_DRAW_WINDOW_S,
    PowerDraw,
    PowerDrawTracker,
    PowerHistoryError,
    PowerHistoryStore,
    PowerMonitorStore,
)
from .preview import (
    parse_params,
    render_field_preview_frames,
    render_preview_data,
    render_preview_frames,
    render_preview_png,
    review_preview,
)
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
    ReleaseMetadataError,
    current_source_commit,
    load_deployment_record,
    load_release_catalog,
    release_status,
    stage_deployment_firmware,
    stage_known_release_firmware,
)
from .serial_transport import PySerialTransport


STATIC_DIR = Path(__file__).with_name("static")
REPO_ROOT = STATIC_DIR.parents[1]
OTA_CHUNK_RETRIES = 3
OTA_STATUS_FRESH_S = 60
OTA_CHECKPOINT_CHUNKS = 256
OTA_STATUS_SETTLE_S = 0.35
OTA_REPAIR_STALL_ROUNDS = 2
OTA_ACTIVE_WRITER_PHASES = frozenset({"begin", "writing", "repairing"})
OTA_ACTIVATION_POLL_S = 0.5
OTA_POST_REBOOT_ATTEMPTS = 31
OTA_POST_REBOOT_POLL_S = 1.0
OTA_RETRY_TIMEOUT_S = 6 * 60 * 60
CONTROL_TICK_INTERVAL_S = float(os.getenv("CONTROL_STATE_POLL_INTERVAL_S", "15.0"))
OTA_AUTO_RETRY_INTERVAL_S = 60.0


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
# Protocol v11 is the first routed wire format. A v10 primary cannot observe a
# performer after that performer reboots into v11, so the one-time migration
# must dispatch every staged leaf activation before rebooting the primary.
ROUTED_PROTOCOL_VERSION = 11


def ota_activation_order(
    macs: set[str] | list[str],
    lanterns: dict[str, dict[str, Any]],
    *,
    expected_macs: set[str] | None = None,
    activated_macs: set[str] | None = None,
) -> list[str]:
    """Return eligible one-hop activations: all leaves before any relay."""
    candidates = {str(mac) for mac in macs}
    if expected_macs is not None and activated_macs is not None:
        candidates -= {str(mac) for mac in activated_macs}
        pending_leaves = {
            str(mac)
            for mac in expected_macs
            if str(lanterns.get(str(mac), {}).get("role") or "performer").lower()
            != "relay"
        } - {str(mac) for mac in activated_macs}
        if pending_leaves:
            candidates = {
                mac
                for mac in candidates
                if str(lanterns.get(mac, {}).get("role") or "performer").lower()
                != "relay"
            }
    return sorted(
        candidates,
        key=lambda mac: (
            1 if str(lanterns.get(str(mac), {}).get("role") or "performer").lower()
            == "relay" else 0,
            str(mac),
        ),
    )


def routed_protocol_downgrade_nodes(
    state: dict[str, Any],
    target_proto: int,
) -> list[str]:
    """Return nodes that a pre-routing protocol would strand after reboot."""
    conductor_firmware = (state.get("conductor") or {}).get("firmware") or {}
    source_proto = int(conductor_firmware.get("proto") or 0)
    if source_proto < ROUTED_PROTOCOL_VERSION or target_proto >= ROUTED_PROTOCOL_VERSION:
        return []

    blocked = []
    for item in state.get("lanterns") or []:
        mac = str(item.get("mac") or "")
        if not mac:
            continue
        role_value = item.get("role")
        route_value = item.get("route")
        route_hops = route_value.get("hops") if isinstance(route_value, dict) else None
        route_via = route_value.get("via") if isinstance(route_value, dict) else None
        if (
            not isinstance(role_value, str)
            or role_value.lower() not in {"performer", "relay"}
            or not isinstance(route_value, dict)
            or isinstance(route_hops, bool)
            or not isinstance(route_hops, int)
            or route_hops not in {0, 1}
            or not isinstance(route_via, str)
            or not route_via
        ):
            blocked.append(mac)
            continue
        role = role_value.lower()
        route = route_value
        if role == "relay" or route_hops > 0:
            blocked.append(mac)
    return sorted(set(blocked))


def reject_routed_protocol_downgrade(
    state: dict[str, Any],
    target_proto: int,
) -> None:
    blocked = routed_protocol_downgrade_nodes(state, target_proto)
    if not blocked:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            "cannot downgrade a routed field to a pre-relay protocol; "
            "direct-connect relayed performers, remove relay roles, and resolve "
            "every offline node with unknown route metadata first "
            f"({', '.join(blocked)})"
        ),
    )


def require_artifact_protocol(artifact: Any) -> int:
    protocol = getattr(artifact, "protocol", None)
    if isinstance(protocol, bool) or not isinstance(protocol, int):
        raise HTTPException(
            status_code=409,
            detail=(
                "firmware artifact has unknown protocol metadata; select its "
                "release again or upload it with the compiled wire protocol"
            ),
        )
    return protocol


def ota_reconcile_needed(
    state: dict[str, Any],
    artifact: Any,
    install: dict[str, Any],
) -> bool:
    """Return whether the desired image is missing from any online performer."""
    if install.get("auto_update_enabled") is not True or artifact is None:
        return False
    if (state.get("power") or {}).get("force_sleep") is True:
        return False
    alive = [
        item
        for item in state.get("lanterns") or []
        if item.get("status") == "alive" and item.get("mac")
    ]
    if not alive:
        return False
    if install.get("desired_artifact_sha256") != install.get("installed_artifact_sha256"):
        return True

    if artifact.version and artifact.commit:
        commit = artifact.commit.lower()
        conductor = (state.get("conductor") or {}).get("firmware") or {}
        firmware_targets = [item.get("firmware") or {} for item in alive]
        if conductor:
            firmware_targets.append(conductor)
        return any(
            str(firmware.get("version") or "") != artifact.version
            or not commit.startswith(
                str(firmware.get("build_label") or "").lower()
            )
            or bool(firmware.get("dirty"))
            for firmware in firmware_targets
        )

    conductor_firmware = (state.get("conductor") or {}).get("firmware") or {}
    comparable = ("version", "proto", "build_id", "dirty")
    return any(
        any(
            (item.get("firmware") or {}).get(key) != conductor_firmware.get(key)
            for key in comparable
        )
        for item in alive
    )


def ota_monotonic_offset(current: int, reported: int, image_size: int) -> int:
    """Keep passive status reports from undoing saved per-board progress."""
    return max(max(0, min(current, image_size)), max(0, min(reported, image_size)))


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


class UploadedPatternEntry(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    brightness: int = Field(ge=0, le=192)
    program: dict[str, Any]


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
    schedule_enabled: bool | None = None
    force_awake: bool | None = None
    force_sleep: bool | None = None
    current_min: int = Field(ge=0, le=1439)
    current_epoch_s: int = Field(ge=0, le=4_294_967_295)


class FieldPowerUpdate(BaseModel):
    mode: Literal["sleep", "wake", "schedule"]


class PowerMonitorUpdate(BaseModel):
    battery_capacity_wh: float = Field(gt=0, le=10_000)
    full_voltage: float = Field(gt=0, le=100)


class OtaModeUpdate(BaseModel):
    enabled: bool


class OtaAutoUpdate(BaseModel):
    enabled: bool


class OtaReleaseSelection(BaseModel):
    version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


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
    if mode not in {"serial", "local-serial"}:
        raise RuntimeError(f"unknown CONTROL_CONDUCTOR={mode!r}")

    port = os.getenv("CONTROL_SERIAL_PORT")
    if not port:
        raise RuntimeError("CONTROL_SERIAL_PORT is required when CONTROL_CONDUCTOR=serial")
    baud = int(os.getenv("CONTROL_SERIAL_BAUD", "115200"))
    timeout_s = float(
        os.getenv("CONTROL_SERIAL_TIMEOUT_S", str(DEFAULT_SERIAL_REQUEST_TIMEOUT_S))
    )
    state_timeout_s = float(
        os.getenv("CONTROL_SERIAL_STATE_TIMEOUT_S", str(DEFAULT_SERIAL_STATE_TIMEOUT_S))
    )
    reset_on_open = os.getenv("CONTROL_SERIAL_RESET_ON_OPEN", "0").strip().lower() in {"1", "true", "yes"}
    return JsonLineSerialConductor(
        PySerialTransport(port, baud=baud, reset_on_open=reset_on_open),
        timeout_s=timeout_s,
        state_timeout_s=state_timeout_s,
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
    power_monitor_store: PowerMonitorStore | None = None,
    power_history_store: PowerHistoryStore | None = None,
    uploaded_pattern_store: UploadedPatternStore | None = None,
) -> FastAPI:
    resolved_settings = settings or load_remote_settings(os.environ)
    resolved_auth = auth_manager
    if resolved_auth is None:
        resolved_auth = (
            AuthManager.from_encoded_hash(resolved_settings.password_hash)
            if resolved_settings.password_hash is not None
            else AuthManager.disabled()
        )
    if resolved_settings.remote_serial_mode and not resolved_auth.enabled:
        raise RuntimeError("authentication cannot be disabled when CONTROL_CONDUCTOR=serial")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def ticker() -> None:
            while True:
                await asyncio.sleep(CONTROL_TICK_INTERVAL_S)
                try:
                    await conductor_call("tick")
                    state = await conductor_call("snapshot")
                    await publish({"type": "state", "action": "tick", "state": state})
                    await maybe_start_ota_reconcile(state)
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
            last_auto_update: bool | None = None
            while True:
                await asyncio.sleep(1)
                try:
                    status = await app.state.provisioning_client.status()
                except ProvisioningClientError as error:
                    status = {
                        "available": False,
                        "revision": 0,
                        "session": {
                            "active": False,
                            "auto_update_enabled": False,
                            "max_workers": 5,
                        },
                        "artifact": None,
                        "artifact_error": str(error),
                        "connected": 0,
                        "running": 0,
                        "jobs": [],
                    }
                revision = int(status.get("revision") or 0)
                available = bool(status.get("available"))
                auto_update = bool(
                    (status.get("session") or {}).get("auto_update_enabled")
                )
                app.state.provisioning_snapshot = status
                if (
                    revision != last_revision
                    or available != last_available
                    or auto_update != last_auto_update
                ):
                    await publish({"type": "provisioning", "provisioning": status})
                    last_revision = revision
                    last_available = available
                    last_auto_update = auto_update

        ticker_task = asyncio.create_task(ticker())
        reaper_task = asyncio.create_task(session_reaper())
        provisioning_task = asyncio.create_task(provisioning_ticker())
        app.state.ticker_task = ticker_task
        app.state.session_reaper_task = reaper_task
        app.state.provisioning_task = provisioning_task
        if (
            app.state.ota_install.get("running") is True
            and not (
                app.state.ota_install.get("automatic") is True
                and app.state.ota_install.get("auto_update_enabled") is not True
            )
        ):
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
                            await ota_install_worker(
                                artifact,
                                state,
                                resume=True,
                                activate=bool(
                                    app.state.ota_install.get(
                                        "activate_after_stage",
                                        True,
                                    )
                                ),
                            )
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
        elif app.state.ota_install.get("running") is True:
            app.state.ota_install.update({
                "running": False,
                "complete": False,
                "phase": "paused",
                "message": "automatic firmware update paused because automatic updates are off",
            })
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
        elif resolved_settings.remote_serial_mode:
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
        if resolved_settings.remote_serial_mode
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
    app.state.uploaded_pattern_store = uploaded_pattern_store or UploadedPatternStore(
        data_dir / "uploaded-patterns" if data_dir else ".control_uploaded_patterns"
    )
    app.state.group_store = group_store or GroupStore(data_dir / "groups" if data_dir else ".control_groups")
    app.state.calibration_store = calibration_store or CalibrationStore(
        data_dir / "calibration" if data_dir else ".control_calibration"
    )
    app.state.ota_install_store = OtaInstallStore(app.state.ota_store.root)
    app.state.ota_install = PersistentOtaInstall(
        app.state.ota_install_store,
        app.state.ota_install_store.load(),
    )
    current_artifact = app.state.ota_store.artifact()
    ota_defaults: dict[str, Any] = {}
    if "auto_update_enabled" not in app.state.ota_install:
        ota_defaults["auto_update_enabled"] = True
    if current_artifact is not None and current_artifact.source == "release":
        # A promoted release replaces the companion image atomically.  Keep the
        # durable desired pointer aligned even when an older completed journal
        # already contains a desired hash from the previous release.
        ota_defaults["desired_artifact_sha256"] = current_artifact.sha256
    elif "desired_artifact_sha256" not in app.state.ota_install and current_artifact is not None:
        ota_defaults["desired_artifact_sha256"] = current_artifact.sha256
    if (
        "installed_artifact_sha256" not in app.state.ota_install
        and app.state.ota_install.get("complete") is True
        and app.state.ota_install.get("sha256")
    ):
        ota_defaults["installed_artifact_sha256"] = app.state.ota_install["sha256"]
    if ota_defaults:
        app.state.ota_install.update(ota_defaults)
    app.state.ota_auto_last_attempt = 0.0
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
    app.state.power_monitor_store = power_monitor_store or PowerMonitorStore(
        data_dir / "power" if data_dir else None
    )
    power_root = (
        data_dir / "power"
        if data_dir
        else app.state.power_monitor_store.root
    )
    app.state.power_history_store = power_history_store or PowerHistoryStore(power_root)
    app.state.power_history_error = None
    stored_power_monitor = app.state.power_monitor_store.load()
    app.state.power_monitor_config = {
        "battery_capacity_wh": float(os.getenv("CONTROL_BATTERY_CAPACITY_WH", DEFAULT_BATTERY_CAPACITY_WH)),
        "full_voltage": float(os.getenv("CONTROL_BATTERY_FULL_VOLTAGE", DEFAULT_FULL_VOLTAGE)),
        **stored_power_monitor.get("config", {}),
    }
    app.state.power_full_anchors = dict(stored_power_monitor.get("full_anchors", {}))
    app.state.power_draw_tracker = PowerDrawTracker()
    try:
        app.state.power_draw_tracker.restore(
            app.state.power_history_store.draw_points(
                since=time.time() - DEFAULT_DRAW_WINDOW_S
            )
        )
    except PowerHistoryError as error:
        app.state.power_history_error = str(error)
    app.state.conductor_lock = asyncio.Lock()
    app.state.show_generation = 0
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

    def direct_loopback_request(scope: dict[str, Any], headers: Any) -> bool:
        if not app.state.settings.local_serial_mode:
            return True
        try:
            peer_is_loopback = ipaddress.ip_address(socket_peer_ip(scope)).is_loopback
            hostname = urlsplit(f"//{headers.get('host', '')}").hostname
            host_is_loopback = hostname == "localhost" or (
                hostname is not None and ipaddress.ip_address(hostname).is_loopback
            )
        except ValueError:
            return False
        forwarded = {
            "cf-connecting-ip",
            "forwarded",
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-proto",
        }
        return peer_is_loopback and host_is_loopback and not any(
            headers.get(name) is not None for name in forwarded
        )

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

        if not direct_loopback_request(request.scope, request.headers):
            return secure_response(
                JSONResponse(
                    {"detail": "local serial mode accepts direct loopback requests only"},
                    status_code=403,
                ),
                scheme,
            )

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
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
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

    def save_power_monitor_state() -> None:
        app.state.power_monitor_store.save({
            "config": dict(app.state.power_monitor_config),
            "full_anchors": dict(app.state.power_full_anchors),
        })

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
            lifetime_avg_w = power.get("avg_w")
            if not isinstance(wh, (int, float)):
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
            anchor_eligible = plausible is not False and not stale
            full_detected = (
                anchor_eligible
                and isinstance(bus_v, (int, float))
                and bus_v >= full_voltage
            )
            anchor = anchors.get(mac)
            anchor_wh = float(anchor["wh"]) if anchor and isinstance(anchor.get("wh"), (int, float)) else 0.0
            if anchor_eligible and anchor and float(wh) + 0.001 < anchor_wh:
                anchor = {
                    "wh": float(wh),
                    "ts": now,
                    "reason": "meter accumulator restarted",
                }
                anchors[mac] = anchor
                save_power_monitor_state()
            if full_detected:
                if not anchor or float(anchor.get("wh", -1)) != float(wh) or anchor.get("full_detected") is not True:
                    anchor = {
                        "wh": float(wh),
                        "ts": now,
                        "bus_v": float(bus_v),
                        "full_detected": True,
                    }
                    anchors[mac] = anchor
                    save_power_monitor_state()
            anchor_wh = float(anchor["wh"]) if anchor and isinstance(anchor.get("wh"), (int, float)) else 0.0
            used_since_full_wh = max(0.0, float(wh) - anchor_wh)
            soc_percent = max(0.0, min(100.0, 100.0 * (1.0 - used_since_full_wh / capacity_wh)))
            report_age = max(0.0, float(last_report_s)) if isinstance(last_report_s, (int, float)) else 0.0
            reported_at = now - report_age
            try:
                app.state.power_history_store.record_sample(
                    mac=mac,
                    received_at=reported_at,
                    wh=float(wh),
                    mah=power.get("mah"),
                    elapsed_s=power.get("elapsed_s"),
                    bus_v=bus_v,
                    current_ma=power.get("current_ma"),
                    plausible=plausible,
                )
                app.state.power_history_error = None
            except PowerHistoryError as error:
                app.state.power_history_error = str(error)
            draw = (
                app.state.power_draw_tracker.observe(
                    mac,
                    wh=float(wh),
                    elapsed_s=power.get("elapsed_s"),
                    reported_at=reported_at,
                    bus_v=bus_v,
                    current_ma=power.get("current_ma"),
                )
                if plausible is not False
                else PowerDraw(None, None)
            )
            sample = {
                "mac": mac,
                "label": lantern.get("label"),
                "wh": float(wh),
                "avg_w": draw.watts,
                "draw_source": draw.source,
                "lifetime_avg_w": float(lifetime_avg_w) if isinstance(lifetime_avg_w, (int, float)) else None,
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
                if draw.watts is not None:
                    usable_w.append(draw.watts)
        avg_node_wh = sum(usable_wh) / len(usable_wh) if usable_wh else None
        avg_node_w = sum(usable_w) / len(usable_w) if usable_w else None
        estimated_soc = (
            max(0.0, min(100.0, 100.0 * (1.0 - avg_node_wh / capacity_wh)))
            if avg_node_wh is not None else None
        )
        return {
            "battery_capacity_wh": capacity_wh,
            "full_voltage": full_voltage,
            "full_anchor_policy": "Lifetime Wh stays on the meter; the durable full-charge anchor resets SOC to 100% without clearing accumulated energy.",
            "history": {
                "enabled": app.state.power_history_store.enabled,
                "error": app.state.power_history_error,
            },
            "placed_count": placed_count,
            "sample_count": len(samples),
            "usable_sample_count": len(usable_w),
            "stale_count": stale_count,
            "implausible_count": implausible_count,
            "soc_percent": estimated_soc,
            "average_performer_draw_w": avg_node_w,
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
            enriched = await asyncio.to_thread(enrich_state, result)
            app.state.latest_snapshot = enriched
            app.state.latest_snapshot_at = time.monotonic()
            return enriched
        return result

    async def show_mutation_call(method: str, *args: Any) -> Any:
        """Serialize a show change and invalidate older staged activations."""
        async with app.state.conductor_lock:
            app.state.show_generation += 1
            return await asyncio.to_thread(
                getattr(app.state.conductor, method), *args
            )

    async def begin_uploaded_show_operation() -> int:
        async with app.state.conductor_lock:
            app.state.show_generation += 1
            return int(app.state.show_generation)

    def cached_snapshot(max_age_s: float = CONTROL_TICK_INTERVAL_S) -> dict[str, Any] | None:
        snapshot = app.state.latest_snapshot
        if snapshot is None or time.monotonic() - app.state.latest_snapshot_at > max_age_s:
            return None
        return snapshot

    async def require_pattern_firmware_ready(pattern: str, brightness: int) -> None:
        if brightness == 0:
            return
        normalized = re.sub(r"[^a-z0-9]", "", pattern.lower())
        if normalized.isdigit():
            normalized = normalized.lstrip("0") or "0"
        if normalized not in {
            "12", "13", "pondripple", "ripple", "uploaded", "uploadedpattern"
        }:
            return
        # A new pattern ID is safe on the stable v11 transport, but older
        # renderers intentionally fall back to Pulse. Require a fresh, complete
        # fleet view so an API caller cannot bypass the UI and split the show.
        try:
            state = await conductor_call("snapshot")
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        firmware = (state.get("summary") or {}).get("firmware") or {}
        conductor_firmware = (state.get("conductor") or {}).get("firmware") or {}
        features = conductor_firmware.get("features") or []
        expected = int(firmware.get("expected") or 0)
        matching = int(firmware.get("matching") or 0)
        seen = int(firmware.get("seen") or 0)
        firmware_ready = (
            expected > 0
            and firmware.get("consistent") is True
            and matching == expected
            and seen == expected
        )
        if normalized in {"12", "pondripple", "ripple"}:
            ready = firmware_ready and "pond_ripple" in features
            detail = (
                "Pond Ripple requires ripple-capable firmware on every placed lantern. "
                "Finish firmware reconciliation before broadcasting it."
            )
        else:
            uploaded = state.get("uploaded_program") or {}
            ready = (
                firmware_ready
                and "uploaded_patterns_v1" in features
                and uploaded.get("ready") is True
                and int(uploaded.get("ready_count") or 0) == expected
            )
            detail = (
                "Uploaded Pattern requires interpreter firmware and the exact program "
                "on every placed lantern. Finish program distribution before activation."
            )
        if not ready:
            raise HTTPException(status_code=409, detail=detail)

    async def pattern_store_call(method: str, *args: Any) -> Any:
        return await asyncio.to_thread(getattr(app.state.pattern_store, method), *args)

    async def uploaded_pattern_store_call(method: str, *args: Any) -> Any:
        return await asyncio.to_thread(
            getattr(app.state.uploaded_pattern_store, method), *args
        )

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

    async def publish_command_accepted(action: str) -> None:
        # The serial ACK means the conductor accepted and persisted the desired
        # state. Performer convergence is observed later through the periodic
        # snapshot instead of blocking the operator command.
        app.state.latest_snapshot_at = 0.0
        await publish({"type": "desired-state", "action": action})

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
                plan = calibration_mode_plan(state)
                ack = await asyncio.to_thread(
                    app.state.conductor.set_locator,
                    True,
                    brightness=96,
                    slot_ms=1000,
                    bit_count=int(plan["bit_count"]),
                    min_hamming_distance=int(plan["min_hamming_distance"]),
                )
                if ack.get("ok"):
                    ack["plan"] = plan
                return ack
            return await asyncio.to_thread(app.state.conductor.set_locator, False)

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
    async def get_state(fresh: bool = True) -> dict[str, Any]:
        if not fresh and (snapshot := cached_snapshot()) is not None:
            return snapshot
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

    @app.put("/api/provisioning/auto-update")
    async def enable_provisioning_auto_update(
        payload: ProvisioningSessionRequest,
    ) -> dict[str, Any]:
        result = await provisioning_call(
            "enable_auto_update",
            max_workers=payload.max_workers,
        )
        await publish({"type": "provisioning", "provisioning": result})
        return result

    @app.delete("/api/provisioning/auto-update")
    async def disable_provisioning_auto_update() -> dict[str, Any]:
        result = await provisioning_call("disable_auto_update")
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

    @app.post("/api/provisioning/jobs/{job_id}/install")
    async def install_provisioning_job(job_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(status_code=404, detail="provisioning job not found")
        result = await provisioning_call("install", job_id)
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

    @app.get("/api/uploaded-patterns")
    async def list_uploaded_patterns() -> dict[str, Any]:
        try:
            return {"patterns": await uploaded_pattern_store_call("list")}
        except UploadedPatternError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.post("/api/uploaded-patterns")
    async def create_uploaded_pattern(
        request: UploadedPatternEntry,
    ) -> dict[str, Any]:
        try:
            pattern = await uploaded_pattern_store_call(
                "create", request.name, request.brightness, request.program
            )
        except UploadedPatternError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"ok": True, "pattern": pattern}

    @app.post("/api/uploaded-patterns/preview")
    async def preview_uploaded_pattern(
        request: UploadedPatternEntry,
    ) -> dict[str, Any]:
        try:
            compiled = compile_uploaded_pattern(request.program)
        except UploadedPatternError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            state = await conductor_call("snapshot")
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        nodes = [
            item for item in state.get("lanterns") or []
            if isinstance(item.get("x"), (int, float))
            and isinstance(item.get("y"), (int, float))
        ]
        samples = []
        for time_s in (0.0, 1.0, 2.0, 4.0):
            samples.append({
                "time_s": time_s,
                "nodes": [
                    {
                        "mac": item.get("mac"),
                        **run_uploaded_pattern(
                            compiled,
                            time_s=time_s,
                            x=float(item["x"]),
                            y=float(item["y"]),
                        ),
                    }
                    for item in nodes
                ],
            })
        return {"ok": True, "compiled": compiled.as_dict(), "samples": samples}

    @app.delete("/api/uploaded-patterns/{pattern_id}")
    async def delete_uploaded_pattern(pattern_id: str) -> dict[str, Any]:
        try:
            deleted = await uploaded_pattern_store_call("delete", pattern_id)
        except UploadedPatternError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        if not deleted:
            raise HTTPException(status_code=404, detail="unknown uploaded pattern")
        return {"ok": True, "message": "uploaded pattern deleted"}

    async def broadcast_uploaded_pattern_value(
        pattern: dict[str, Any], group_id: int | None
    ) -> dict[str, Any]:
        # Preflight the complete placed inventory before distributing anything.
        # This is intentionally stricter than ordinary eventual convergence:
        # interpreter activation must never split a mixed-version field.
        try:
            state = await conductor_call("snapshot")
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        firmware = (state.get("summary") or {}).get("firmware") or {}
        features = ((state.get("conductor") or {}).get("firmware") or {}).get("features") or []
        expected = int(firmware.get("expected") or 0)
        firmware_ready = (
            expected > 0
            and firmware.get("consistent") is True
            and int(firmware.get("matching") or 0) == expected
            and int(firmware.get("seen") or 0) == expected
            and "uploaded_patterns_v1" in features
        )
        if not firmware_ready:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Uploaded Pattern is blocked until every placed lantern is online "
                    "on interpreter-capable firmware. Existing patterns remain active."
                ),
            )

        try:
            compiled = compile_uploaded_pattern(pattern["program"])
        except UploadedPatternError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        operation_generation = await begin_uploaded_show_operation()
        try:
            ack = await conductor_call(
                "install_uploaded_program",
                compiled.program_id,
                VM_VERSION,
                compiled.bytecode,
            )
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack.get("ok"):
            raise HTTPException(status_code=400, detail=ack.get("error", "program install failed"))

        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            try:
                distributed = await conductor_call("uploaded_program_progress")
            except SerialProtocolError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
            distributed_id = int(distributed.get("target_id") or 0) | (
                int(distributed.get("target_tag") or 0) << 32
            )
            if (
                distributed_id == compiled.program_id
                and distributed.get("ready") is True
                and int(distributed.get("ready_count") or 0) == expected
            ):
                break
            await asyncio.sleep(1.0)
        else:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Program was staged but not activated because every placed lantern "
                    "did not verify the exact program within 45 seconds."
                ),
            )

        params = {
            "p0": compiled.program_id & 0xFFFF,
            "p1": (compiled.program_id >> 16) & 0xFFFF,
            "p2": (compiled.program_id >> 32) & 0xFFFF,
            "p3": (compiled.program_id >> 48) & 0xFFFF,
        }
        args: tuple[Any, ...] = (
            "Uploaded Pattern", int(pattern["brightness"]), params
        )
        if group_id is not None:
            args += (group_id,)
        try:
            async with app.state.conductor_lock:
                if app.state.show_generation != operation_generation:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Program was staged but not activated because a newer "
                            "show command took priority."
                        ),
                    )
                activation = await asyncio.to_thread(
                    app.state.conductor.update_pattern, *args
                )
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not activation.get("ok"):
            raise HTTPException(status_code=409, detail=activation.get("error", "activation refused"))
        await publish_command_accepted("uploaded-pattern")
        return {
            "ok": True,
            "message": "uploaded pattern verified and activated",
            "pattern": pattern,
            "compiled": compiled.as_dict(),
            "ack": activation,
        }

    @app.post("/api/uploaded-patterns/broadcast")
    async def broadcast_uploaded_pattern_draft(
        request: UploadedPatternEntry,
        group_id: int | None = Query(default=None, ge=0, lt=GROUP_COUNT),
    ) -> dict[str, Any]:
        pattern = {
            "name": request.name,
            "brightness": request.brightness,
            "program": request.program,
        }
        return await broadcast_uploaded_pattern_value(pattern, group_id)

    @app.post("/api/uploaded-patterns/{pattern_id}/broadcast")
    async def broadcast_uploaded_pattern(
        pattern_id: str,
        group_id: int | None = Query(default=None, ge=0, lt=GROUP_COUNT),
    ) -> dict[str, Any]:
        try:
            pattern = await uploaded_pattern_store_call("get", pattern_id)
        except UploadedPatternError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        if not pattern:
            raise HTTPException(status_code=404, detail="unknown uploaded pattern")
        return await broadcast_uploaded_pattern_value(pattern, group_id)

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
        await publish_command_accepted("group-name")
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
            await require_pattern_firmware_ready(
                pattern["pattern"], int(pattern["brightness"])
            )
            args: tuple[Any, ...] = (
                pattern["pattern"], pattern["brightness"], pattern["params"]
            )
            if group_id is not None:
                args += (group_id,)
            ack = await show_mutation_call("update_pattern", *args)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_command_accepted("pattern")
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
            await publish_command_accepted("calibration-apply")
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

    @app.get("/api/field-preview/frames.json")
    async def field_preview_frames_json(
        duration_ms: int = Query(default=6000, ge=500, le=12000),
        fps: int = Query(default=8, ge=1, le=12),
        start_ms: int | None = Query(default=None, ge=0),
    ) -> dict[str, Any]:
        try:
            state = cached_snapshot(max_age_s=10.0)
            if state is None:
                state = await conductor_call("snapshot")
            render_start_ms = start_ms
            if render_start_ms is None:
                uptime_s = (state.get("conductor") or {}).get("uptime_s")
                if isinstance(uptime_s, (int, float)):
                    snapshot_age_s = max(0.0, time.monotonic() - app.state.latest_snapshot_at)
                    render_start_ms = round((float(uptime_s) + snapshot_age_s) * 1000)
            return await asyncio.to_thread(
                render_field_preview_frames,
                state,
                duration_ms,
                fps,
                render_start_ms,
            )
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

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
        speed: int | None = None,
        cooling: int | None = None,
        sparking: int | None = None,
        front_width: int | None = None,
        chorus: int | None = None,
        center_x: int | None = Query(default=None, ge=0, le=1000),
        center_y: int | None = Query(default=None, ge=0, le=1000),
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
                "speed": speed,
                "cooling": cooling,
                "sparking": sparking,
                "front_width": front_width,
                "chorus": chorus,
                "center_x": center_x,
                "center_y": center_y,
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
        speed: int | None = None,
        cooling: int | None = None,
        sparking: int | None = None,
        front_width: int | None = None,
        chorus: int | None = None,
        center_x: int | None = Query(default=None, ge=0, le=1000),
        center_y: int | None = Query(default=None, ge=0, le=1000),
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
                "speed": speed,
                "cooling": cooling,
                "sparking": sparking,
                "front_width": front_width,
                "chorus": chorus,
                "center_x": center_x,
                "center_y": center_y,
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
        speed: int | None = None,
        cooling: int | None = None,
        sparking: int | None = None,
        front_width: int | None = None,
        chorus: int | None = None,
        center_x: int | None = Query(default=None, ge=0, le=1000),
        center_y: int | None = Query(default=None, ge=0, le=1000),
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
                "speed": speed,
                "cooling": cooling,
                "sparking": sparking,
                "front_width": front_width,
                "chorus": chorus,
                "center_x": center_x,
                "center_y": center_y,
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
        speed: int | None = None,
        cooling: int | None = None,
        sparking: int | None = None,
        front_width: int | None = None,
        chorus: int | None = None,
        center_x: int | None = Query(default=None, ge=0, le=1000),
        center_y: int | None = Query(default=None, ge=0, le=1000),
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
                "speed": speed,
                "cooling": cooling,
                "sparking": sparking,
                "front_width": front_width,
                "chorus": chorus,
                "center_x": center_x,
                "center_y": center_y,
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
        await publish_command_accepted("assign")
        return ack

    @app.post("/api/lanterns/{mac}/forget")
    async def forget(mac: str) -> dict[str, Any]:
        try:
            ack = await conductor_call("forget", mac)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=404, detail=ack["error"])
        await publish_command_accepted("forget")
        return ack

    @app.post("/api/lanterns/{mac}/group")
    async def assign_group(mac: str, request: GroupUpdate) -> dict[str, Any]:
        try:
            ack = await conductor_call("assign_group", mac, request.group_id)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_command_accepted("group")
        return ack

    @app.post("/api/lanterns/{mac}/led-count")
    async def assign_led_count(mac: str, request: LedCountUpdate) -> dict[str, Any]:
        try:
            ack = await conductor_call("assign_led_count", mac, request.led_count)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_command_accepted("led-count")
        return ack

    @app.post("/api/lanterns/replace")
    async def replace(request: ReplaceRequest) -> dict[str, Any]:
        try:
            ack = await conductor_call("replace", request.old_mac, request.new_mac)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=404, detail=ack["error"])
        await publish_command_accepted("replace")
        return ack

    @app.post("/api/show/pattern")
    async def update_pattern(request: PatternUpdate) -> dict[str, Any]:
        try:
            await require_pattern_firmware_ready(request.pattern, request.brightness)
            args: tuple[Any, ...] = (
                request.pattern, request.brightness, request.params
            )
            if request.group_id is not None:
                args += (request.group_id,)
            ack = await show_mutation_call("update_pattern", *args)
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_command_accepted("pattern")
        return ack

    @app.post("/api/show/blackout")
    async def blackout() -> dict[str, Any]:
        try:
            ack = await show_mutation_call("blackout")
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        await publish_command_accepted("blackout")
        return ack

    @app.post("/api/show/restore")
    async def restore_blackout() -> dict[str, Any]:
        try:
            ack = await show_mutation_call("restore_blackout")
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_command_accepted("blackout-restore")
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
        await publish_command_accepted("calibration-mode")
        return ack

    @app.post("/api/operations/power-policy")
    async def update_power_policy(request: PowerPolicyUpdate) -> dict[str, Any]:
        try:
            ack = await conductor_call(
                "update_power_policy",
                request.model_dump(exclude_none=True),
            )
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        await publish_command_accepted("power-policy")
        return ack

    @app.post("/api/operations/field-power")
    async def update_field_power(request: FieldPowerUpdate) -> dict[str, Any]:
        overrides = {
            "sleep": {
                "schedule_enabled": False,
                "force_awake": False,
                "force_sleep": True,
            },
            "wake": {
                "schedule_enabled": False,
                "force_awake": True,
                "force_sleep": False,
            },
            "schedule": {
                "schedule_enabled": True,
                "force_awake": False,
                "force_sleep": False,
            },
        }
        if request.mode == "sleep" and app.state.ota_install.get("running") is True:
            raise HTTPException(
                status_code=409,
                detail="pause the firmware update before sleeping the field",
            )
        try:
            if request.mode == "sleep":
                ota_ack = await conductor_call("set_ota_mode", False)
                if not ota_ack["ok"]:
                    raise HTTPException(status_code=400, detail=ota_ack["error"])
            ack = await conductor_call("update_power_policy", overrides[request.mode])
        except SerialProtocolError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not ack["ok"]:
            raise HTTPException(status_code=400, detail=ack["error"])
        ack["mode"] = request.mode
        await publish_command_accepted("field-power")
        return ack

    @app.post("/api/operations/power-monitor")
    async def update_power_monitor(request: PowerMonitorUpdate) -> dict[str, Any]:
        async with app.state.conductor_lock:
            app.state.power_monitor_config = request.model_dump()
            save_power_monitor_state()
        await publish_command_accepted("power-monitor")
        return {"ok": True, "message": "power monitor settings changed", "power_monitor": app.state.power_monitor_config}

    @app.get("/api/power/history")
    async def get_power_history(
        mac: str | None = Query(default=None),
        hours: float = Query(default=24.0, gt=0, le=24 * 366 * 10),
        limit: int = Query(default=5000, ge=1, le=100_000),
    ) -> dict[str, Any]:
        since = time.time() - hours * 60 * 60
        try:
            samples = await asyncio.to_thread(
                app.state.power_history_store.samples,
                since=since,
                mac=mac,
                limit=limit,
            )
        except PowerHistoryError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {
            "enabled": app.state.power_history_store.enabled,
            "hours": hours,
            "mac": mac.upper() if mac else None,
            "count": len(samples),
            "samples": [sample.as_dict() for sample in samples],
        }

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
        save_power_monitor_state()
        await publish_command_accepted("power-sync-full")
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
        await publish_command_accepted("ota-mode")
        return ack

    @app.get("/api/operations/ota-artifact")
    async def get_ota_artifact() -> dict[str, Any]:
        return {"artifact": app.state.ota_store.current()}

    @app.put("/api/operations/ota-artifact")
    async def stage_ota_artifact(
        request: Request,
        filename: str = "firmware.bin",
        protocol: int = Query(..., ge=1, le=255),
    ) -> dict[str, Any]:
        async with app.state.ota_start_lock:
            if app.state.ota_reserved or app.state.ota_install.get("phase") == "ready-to-activate":
                raise HTTPException(status_code=423, detail="OTA install owns the conductor")
            data = await request.body()
            try:
                artifact = await asyncio.to_thread(
                    app.state.ota_store.stage,
                    filename,
                    data,
                    protocol=protocol,
                )
            except OtaArtifactError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            app.state.ota_install.update({"desired_artifact_sha256": artifact["sha256"]})
        await publish({"type": "ack", "action": "ota-artifact", "artifact": artifact})
        return {"ok": True, "message": "firmware file uploaded", "artifact": artifact}

    @app.put("/api/operations/ota-auto-update")
    async def update_ota_auto_update(request: OtaAutoUpdate) -> dict[str, Any]:
        app.state.ota_install.update({"auto_update_enabled": request.enabled})
        if (
            not request.enabled
            and app.state.ota_install.get("running") is True
            and app.state.ota_install.get("automatic") is True
        ):
            app.state.ota_pause_requested = True
            message = "automatic updates disabled; the current automatic transfer is pausing"
        elif request.enabled:
            app.state.ota_auto_last_attempt = 0.0
            message = "automatic firmware updates enabled"
        else:
            message = "automatic firmware updates disabled"
        await publish({
            "type": "ack",
            "action": "ota-auto-update",
            "install": ota_install_progress(app.state.ota_install),
        })
        return {
            "ok": True,
            "message": message,
            "install": ota_install_progress(app.state.ota_install),
        }

    @app.post("/api/operations/ota-release")
    async def select_ota_release(request: OtaReleaseSelection) -> dict[str, Any]:
        known_versions = {item.version for item in app.state.release_catalog}
        if request.version not in known_versions:
            raise HTTPException(status_code=404, detail="unknown firmware release")
        async with app.state.ota_start_lock:
            if app.state.ota_reserved or app.state.ota_install.get("phase") == "ready-to-activate":
                raise HTTPException(status_code=423, detail="OTA install owns the conductor")
            current = app.state.ota_store.current()
            if (
                current
                and current.get("source") == "release"
                and current.get("version") == request.version
                and isinstance(current.get("protocol"), int)
                and not isinstance(current.get("protocol"), bool)
            ):
                result = {"release": None, "artifact": current}
            else:
                try:
                    result = await asyncio.to_thread(
                        stage_known_release_firmware,
                        request.version,
                        app.state.ota_store,
                    )
                except ReleaseMetadataError as error:
                    raise HTTPException(status_code=502, detail=str(error)) from error
            app.state.ota_install.update({
                "desired_artifact_sha256": result["artifact"]["sha256"],
            })
            app.state.ota_auto_last_attempt = 0.0
        await publish({"type": "ack", "action": "ota-release", **result})
        return {"ok": True, "message": f"firmware v{request.version} selected", **result}

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
        activate: bool = True,
    ) -> dict[str, Any]:
        all_lanterns = {
            str(item.get("mac")): item
            for item in state.get("lanterns") or []
            if item.get("mac")
        }
        expected_lanterns = expected_ota_lanterns(state)
        persisted_target_macs = set(
            str(mac) for mac in app.state.ota_install.get("target_macs") or []
        )
        persisted_selective = bool(
            resume
            and app.state.ota_install.get("cohort_mode") == "selective"
            and persisted_target_macs
        )
        expected_macs = set(persisted_target_macs)
        targeted_mode = persisted_selective
        conductor_firmware = (state.get("conductor") or {}).get("firmware") or {}
        source_proto = int(conductor_firmware.get("proto") or 0)
        target_proto = require_artifact_protocol(artifact)
        reject_routed_protocol_downgrade(state, target_proto)
        protocol_migration = bool(
            app.state.ota_install.get("protocol_migration")
            or (source_proto > 0 and source_proto != target_proto)
        )
        activation_attempted = {
            str(mac)
            for mac in app.state.ota_install.get("activation_attempted_macs") or []
        }
        activation_dispatched = {
            str(mac)
            for mac in app.state.ota_install.get("activation_dispatched_macs") or []
        }
        migration_activation_started = bool(
            app.state.ota_install.get("migration_activation_started")
        )
        app.state.ota_install.update({
            "source_proto": source_proto or None,
            "target_proto": target_proto,
            "protocol_migration": protocol_migration,
        })

        def already_installed(item: dict[str, Any]) -> bool:
            firmware = item.get("firmware") or {}
            if artifact.version and artifact.commit:
                label = str(firmware.get("build_label") or "").lower()
                return (
                    firmware.get("version") == artifact.version
                    and len(label) >= 7
                    and artifact.commit.lower().startswith(label)
                    and not bool(firmware.get("dirty"))
                )
            comparable = ("version", "proto", "build_id", "dirty")
            return bool(conductor_firmware) and all(
                firmware.get(key) == conductor_firmware.get(key) for key in comparable
            )

        legacy_delivery_macs = {
            str(mac)
            for mac in app.state.ota_install.get("delivery_confirmed_macs") or []
        }
        previously_activated_macs = {
            str(mac)
            for mac in app.state.ota_install.get("activated_macs") or []
        }
        installed_evidence_macs = legacy_delivery_macs | previously_activated_macs
        artifact_has_identity = bool(artifact.version and artifact.commit)
        installed_macs = {
            mac
            for mac, item in expected_lanterns.items()
            if (artifact_has_identity or mac in installed_evidence_macs)
            and already_installed(item)
        }
        field_installed_macs = set(installed_macs)
        # Only immutable release metadata can prove that the primary already
        # has this exact binary. A manually uploaded artifact has no build
        # identity, so it must retain the legacy full-field/local-writer path.
        conductor_matches_reference = already_installed(
            {"firmware": conductor_firmware}
        )
        conductor_installed = artifact_has_identity and conductor_matches_reference
        selective_cohort = (
            persisted_target_macs
            if persisted_selective
            else set(expected_lanterns) - installed_macs
        )
        if not persisted_selective:
            stale_relays = {
                mac
                for mac in selective_cohort
                if str(expected_lanterns.get(mac, {}).get("role") or "")
                == "relay"
            }
            # A stale relay predates tokened per-frame receipts. Upgrade that
            # directly reachable infrastructure node first, then let the next
            # automatic reconciliation target its stale children through the
            # now-current relay. This keeps one job inside the six-hour bound
            # without widening it to the current field.
            blocked_by_stale_relay = {
                mac
                for mac in selective_cohort
                if int(
                    (expected_lanterns.get(mac, {}).get("route") or {}).get(
                        "hops"
                    )
                    or 0
                )
                == 1
                and str(
                    (expected_lanterns.get(mac, {}).get("route") or {}).get(
                        "via"
                    )
                    or ""
                )
                in stale_relays
            }
            selective_cohort -= blocked_by_stale_relay
        selective_candidates = selective_cohort - installed_macs
        pre_routed_targets = {
            mac
            for mac in selective_candidates
            if int(
                (expected_lanterns.get(mac, {}).get("firmware") or {}).get(
                    "proto"
                )
                or 0
            )
            < ROUTED_PROTOCOL_VERSION
        }
        if conductor_installed and not persisted_selective and pre_routed_targets:
            raise HTTPException(
                status_code=409,
                detail=(
                    "a pre-v11 node cannot rejoin after the primary migrated to "
                    "routed transport; update it once at the USB flashing station "
                    f"({', '.join(sorted(pre_routed_targets))})"
                ),
            )

        def selective_ota_supported(macs: set[str]) -> bool:
            # Routed protocol v11 already understands logical-MAC-addressed OTA
            # packets (the repair path uses them). A matching primary also
            # proves that its serial command set includes exact-cohort begin.
            return bool(
                conductor_installed
                and macs
                and all(
                    int(
                        (expected_lanterns.get(mac, {}).get("firmware") or {}).get(
                            "proto"
                        )
                        or 0
                    )
                    >= ROUTED_PROTOCOL_VERSION
                    for mac in macs
                )
            )
        if (
            not persisted_selective
            and not expected_macs
            and expected_lanterns
            and installed_macs == set(expected_lanterns)
            and conductor_matches_reference
        ):
            nodes = [
                {
                    "mac": mac,
                    "phase": "complete",
                    "error": "none",
                    "offset": artifact.size,
                    "crc32": artifact.crc32,
                    "source": "live_firmware_identity",
                }
                for mac in sorted(installed_macs)
            ]
            app.state.ota_install.update({
                "running": False,
                "complete": True,
                "phase": "complete",
                "error": None,
                "message": "firmware already installed across the online field",
                "nodes": nodes,
                "target_macs": sorted(installed_macs),
                "target_count": len(installed_macs),
                "already_installed_macs": sorted(installed_macs),
                "activated_macs": sorted(installed_macs),
                "node_offsets": {
                    mac: artifact.size for mac in sorted(installed_macs)
                },
                "completed_at": time.time(),
                "installed_artifact_sha256": artifact.sha256,
            })
            return {
                "ok": True,
                "message": "firmware already installed across the online field",
                "artifact": artifact.as_dict(),
            }
        missing_rounds: dict[str, int] = {}
        repair_offsets: dict[str, int] = {}
        repair_stalls: dict[str, int] = {}
        delivery_confirmed_offsets = {
            str(mac): int(offset)
            for mac, offset in dict(
                app.state.ota_install.get("delivery_confirmed_offsets") or {}
            ).items()
        }
        node_offsets = {
            str(mac): int(offset)
            for mac, offset in dict(
                app.state.ota_install.get("node_offsets") or {}
            ).items()
        }
        full_replay_macs = set(
            str(mac)
            for mac in app.state.ota_install.get("full_replay_macs") or []
        )
        # Activation evidence only survives a fresh job when the live firmware
        # identity still proves that the same artifact is installed.
        activated = set(installed_macs)
        retry_deadline_at = float(
            app.state.ota_install.get("retry_deadline_at")
            or (time.time() + OTA_RETRY_TIMEOUT_S)
        )
        if not app.state.ota_install.get("retry_deadline_at"):
            app.state.ota_install.update({
                "retry_timeout_s": OTA_RETRY_TIMEOUT_S,
                "retry_deadline_at": retry_deadline_at,
            })

        def ensure_retry_window() -> None:
            if time.time() >= retry_deadline_at:
                raise HTTPException(
                    status_code=504,
                    detail=(
                        "OTA retry window expired before every target verified; "
                        "start staging again to resume from verified device progress"
                    ),
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
                ensure_retry_window()
                attempt += 1
                try:
                    ack = await call(method, *args)
                except SerialProtocolError as error:
                    app.state.ota_install.update({
                        "phase": "waiting",
                        "last_retry": {"attempt": attempt, "error": str(error)},
                    })
                    ensure_retry_window()
                    await asyncio.sleep(min(5.0, 0.25 * attempt))
                    continue
                if ack.get("ok") is True:
                    return ack
                error = str(ack.get("error") or "OTA command failed")
                if (
                    "send failed" in error
                    or error == "targeted ota performer is not online"
                    or (error in OTA_CHUNK_RETRYABLE_ERRORS and attempt <= OTA_CHUNK_RETRIES)
                ):
                    app.state.ota_install.update({
                        "phase": "waiting",
                        "last_retry": {"attempt": attempt, "error": error},
                    })
                    ensure_retry_window()
                    await asyncio.sleep(min(5.0, 0.25 * attempt))
                    continue
                raise HTTPException(
                    status_code=503 if error in OTA_CHUNK_RETRYABLE_ERRORS else 400,
                    detail=error,
                )

        async def dispatch_protocol_migration_activation(mac: str) -> None:
            """Dispatch once without accepting ambiguous status as proof.

            The attempted marker documents the crash window, but only a
            positive serial ACK proves that the primary accepted an activation
            frame. If a restart loses that ACK, the migration remains pending
            rather than risking reboot of the primary with a stranded node.
            """
            activation_attempted.add(mac)
            app.state.ota_install.update({
                "activation_attempted_macs": sorted(activation_attempted),
            })
            attempt = 0
            while True:
                ensure_retry_window()
                attempt += 1
                try:
                    ack = await call("ota_activate", mac)
                except SerialProtocolError as error:
                    app.state.ota_install.update({
                        "phase": "waiting",
                        "last_retry": {"attempt": attempt, "error": str(error)},
                    })
                    await asyncio.sleep(min(5.0, 0.25 * attempt))
                    continue
                if ack.get("ok") is True:
                    return
                error = str(ack.get("error") or "OTA command failed")
                if "send failed" in error:
                    app.state.ota_install.update({
                        "phase": "waiting",
                        "last_retry": {"attempt": attempt, "error": error},
                    })
                    await asyncio.sleep(min(5.0, 0.25 * attempt))
                    continue
                raise HTTPException(status_code=400, detail=error)

        async def recover_dispatched_protocol_migration() -> dict[str, Any] | None:
            """Finish or truthfully fail a migration whose leaf cohort was sent.

            Once every activation has a positive dispatch ACK, a retry must not
            rediscover its cohort from the now-incompatible live field. The
            durable target set remains authoritative through primary activation
            and post-reboot identity verification.
            """
            if not (
                protocol_migration
                and migration_activation_started
                and expected_macs
                and activation_dispatched == expected_macs
            ):
                return None
            if source_proto != target_proto:
                app.state.ota_install.update({
                    "phase": "activating-conductor",
                    "active_mac": None,
                })
                try:
                    ack = await call("ota_activate", None)
                except SerialProtocolError as error:
                    app.state.ota_install.update({
                        "last_retry": {"attempt": 1, "error": str(error)},
                    })
                else:
                    if ack.get("ok") is not True:
                        raise HTTPException(
                            status_code=400,
                            detail=str(
                                ack.get("error") or "conductor activation failed"
                            ),
                        )
            nodes = await infer_ota_complete_nodes(
                artifact.size,
                artifact.crc32,
                expected_macs,
            )
            verified_macs = {str(node["mac"]) for node in nodes}
            if verified_macs != expected_macs:
                expected = {
                    mac: all_lanterns.get(mac, {"mac": mac, "label": mac})
                    for mac in expected_macs
                }
                nodes = append_unverified_ota_failures(
                    nodes,
                    expected,
                    verified_macs,
                )
                app.state.ota_install.update({"nodes": nodes})
                raise HTTPException(
                    status_code=503,
                    detail="ota post-reboot verification failed",
                )
            await call_until_ok("set_ota_mode", False)
            app.state.ota_install.update({
                "running": False,
                "complete": True,
                "phase": "complete",
                "error": None,
                "message": "firmware updated across the online field",
                "nodes": nodes,
                "completed_at": time.time(),
                "installed_artifact_sha256": artifact.sha256,
            })
            result = {
                "ok": True,
                "message": "firmware updated across the online field",
                "artifact": artifact.as_dict(),
            }
            await publish({
                "type": "ack",
                "action": "ota-install",
                "artifact": artifact.as_dict(),
                "ack": {**result, "nodes": nodes},
            })
            return result

        recovered_migration = await recover_dispatched_protocol_migration()
        if recovered_migration is not None:
            return recovered_migration

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
            value = await progress()
            for node in fresh_ota_nodes(value.get("nodes") or []):
                mac = str(node.get("mac") or "")
                offset = int(node.get("offset") or 0)
                if mac:
                    node_offsets[mac] = ota_monotonic_offset(
                        node_offsets.get(mac, 0), offset, artifact.size
                    )
            app.state.ota_install.update_volatile({
                "node_offsets": dict(sorted(node_offsets.items())),
            })
            return value

        async def restart_performer_writer(mac: str) -> None:
            await call_until_ok("ota_restart", mac)
            node_offsets[mac] = 0
            repair_offsets[mac] = 0
            repair_stalls[mac] = 0
            app.state.ota_install.update_volatile({
                "node_offsets": dict(sorted(node_offsets.items())),
            })
            app.state.ota_install.update({
                "repair_restarts": int(
                    app.state.ota_install.get("repair_restarts") or 0
                ) + 1,
            })

        async def repair_checkpoint(
            frontier: int, *, single_round: bool = False
        ) -> list[dict[str, Any]]:
            repair_round = 0
            final_checkpoint = frontier == artifact.size
            while True:
                ensure_retry_window()
                repair_round += 1
                if repair_round % 10 == 1:
                    await call_until_ok("set_ota_mode", True)
                current = await probe()
                nodes = fresh_ota_nodes(current.get("nodes") or [])
                by_mac = {str(node.get("mac")): node for node in nodes if node.get("mac")}

                # A valid zero-byte checkpoint is not sufficient evidence that
                # a performer has an open OTA writer. Boards that missed BEGIN
                # report idle and intentionally ignore every chunk. Restart
                # those writers first so one shared replay can advance them.
                repair_target_macs = expected_macs - activated
                for mac in sorted(repair_target_macs):
                    node = by_mac.get(mac)
                    if node is None:
                        continue
                    offset = int(node.get("offset") or 0)
                    crc32 = int(node.get("crc32") or 0)
                    phase = str(node.get("phase") or "idle")
                    if (
                        0 <= offset < frontier
                        and crc32 == checkpoint_crc(offset)
                        and phase not in OTA_ACTIVE_WRITER_PHASES
                        and phase != "failed"
                    ):
                        await restart_performer_writer(mac)
                        node.update({
                            "phase": "begin",
                            "error": "none",
                            "offset": 0,
                            "crc32": 0,
                        })

                shared_repair_offsets: dict[str, int] = {}
                for mac in sorted(repair_target_macs):
                    node = by_mac.get(mac)
                    if node is None:
                        continue
                    offset = int(node.get("offset") or 0)
                    crc32 = int(node.get("crc32") or 0)
                    if (
                        0 <= offset < frontier
                        and crc32 == checkpoint_crc(offset)
                        and str(node.get("phase") or "") != "failed"
                    ):
                        shared_repair_offsets[mac] = offset

                # Exact-cohort delivery already has a target-specific repair
                # path. Avoid expanding one lagging target into another full
                # cohort fan-out, which can repeatedly favor the same relay
                # queue prefix under loss.
                if not targeted_mode and len(shared_repair_offsets) >= 2:
                    replay_offset = min(shared_repair_offsets.values())
                    app.state.ota_install.update({
                        "phase": "repairing",
                        "repair_round": repair_round,
                        "repairing_macs": sorted(shared_repair_offsets),
                        "nodes": nodes,
                        "shared_repair_from": replay_offset,
                    })
                    shared_chunks = 0
                    while replay_offset < frontier:
                        chunk = data[
                            replay_offset : min(frontier, replay_offset + artifact.chunk_size)
                        ]
                        await call_until_ok("ota_rebroadcast", replay_offset, chunk)
                        replay_offset += len(chunk)
                        shared_chunks += 1
                    app.state.ota_install.update({
                        "repair_chunks": int(app.state.ota_install.get("repair_chunks") or 0)
                        + shared_chunks,
                        "shared_repair_chunks": int(
                            app.state.ota_install.get("shared_repair_chunks") or 0
                        ) + shared_chunks,
                    })
                    if single_round:
                        return nodes
                    continue
                pending: list[str] = []
                repaired_chunks = 0

                for mac in sorted(repair_target_macs):
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
                        await restart_performer_writer(mac)
                        replayed_from_zero = True
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
                            await restart_performer_writer(mac)
                            offset = 0
                            replayed_from_zero = True

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
                        node_offsets[mac] = offset
                        app.state.ota_install.update_volatile({
                            "node_offsets": dict(sorted(node_offsets.items())),
                        })
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
                if single_round:
                    return nodes
                ensure_retry_window()
                await asyncio.sleep(min(5.0, 0.25 * repair_round))

        try:
            await call_until_ok("set_ota_mode", True)
            current = await progress()
            active = current.get("active") is True
            staged = current.get("staged") is True
            written = int(current.get("written") or 0)
            resumable = (
                not bool(app.state.ota_install.get("refresh_cohort"))
                and (active or staged)
                and int(current.get("size") or 0) == artifact.size
                and 0 <= written <= artifact.size
                and int(current.get("crc32") or 0) == checkpoint_crc(written)
            )
            if not resumable:
                if migration_activation_started:
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "cannot resume protocol migration staging after activation began; "
                            "keep the current primary online and retry recovery"
                        ),
                    )
                targeted_mode = bool(
                    persisted_selective
                    or selective_ota_supported(selective_candidates)
                )
                if targeted_mode:
                    expected_macs = set(selective_candidates)
                    # Persist the safety boundary before asking the primary to
                    # begin. A shared Pi/conductor power loss must resume this
                    # exact cohort, never reinterpret it as a full-field job.
                    app.state.ota_install.update({
                        "cohort_mode": "selective",
                        "target_macs": sorted(selective_cohort),
                        "target_count": len(selective_cohort),
                    })
                    if expected_macs:
                        ack = await call_until_ok(
                            "ota_begin_targets",
                            artifact.size,
                            artifact.crc32,
                            sorted(expected_macs),
                        )
                        reported_targets = {
                            str(mac) for mac in ack.get("targets") or []
                        }
                        if reported_targets != expected_macs:
                            raise HTTPException(
                                status_code=503,
                                detail="conductor accepted the wrong targeted OTA cohort",
                            )
                else:
                    ack = await call_until_ok(
                        "ota_begin", artifact.size, artifact.crc32
                    )
                    reported_targets = {
                        str(mac) for mac in ack.get("targets") or []
                    }
                    expected_macs = (
                        expected_macs or reported_targets or set(expected_lanterns)
                    )
                    # Legacy begin broadcasts to the frozen cohort and opens a
                    # local conductor writer. Every receiver must then finish
                    # and activate, including nodes that were already current.
                    installed_macs.clear()
                    field_installed_macs.clear()
                    activated.clear()
                for mac in expected_macs:
                    node_offsets[mac] = 0
                    delivery_confirmed_offsets.pop(mac, None)
                app.state.ota_install.update({
                    "activated_macs": [],
                    "activation_attempted_macs": [],
                    "activation_dispatched_macs": [],
                    "migration_activation_started": False,
                    "delivery_confirmed_macs": [],
                    "delivery_confirmed_offsets": {},
                })
                activation_attempted.clear()
                activation_dispatched.clear()
                migration_activation_started = False
                written = 0
                staged = False
            else:
                reported_targets = {str(mac) for mac in current.get("targets") or []}
                expected_macs = reported_targets or expected_macs or set(expected_lanterns)
                targeted_mode = bool(
                    current.get("targeted") is True
                    or app.state.ota_install.get("cohort_mode") == "selective"
                )
                if staged:
                    # A finalized writer may report no active byte prefix even
                    # though its image and performer cohort are fully staged.
                    # The per-node full-size/full-CRC barrier below is the
                    # authority; do not try to append chunk zero to a closed
                    # writer while resuming for explicit activation.
                    written = artifact.size

            # A previous legacy activation may already have rebooted part of
            # this cohort. Those performers no longer retain an OTA writer,
            # so requiring them to report `staged` again deadlocks the safety
            # barrier even though their live firmware identity proves success.
            if targeted_mode:
                installed_macs &= selective_cohort
            expected_macs -= installed_macs
            activated &= expected_macs
            cohort_macs = expected_macs | field_installed_macs

            if not expected_macs:
                await call_until_ok("set_ota_mode", False)
                nodes = [
                    {
                        "mac": mac,
                        "phase": "complete",
                        "error": "none",
                        "offset": artifact.size,
                        "crc32": artifact.crc32,
                        "source": "live_firmware_identity",
                    }
                    for mac in sorted(installed_macs)
                ]
                app.state.ota_install.update({
                    "running": False,
                    "complete": True,
                    "phase": "complete",
                    "error": None,
                    "message": "firmware already installed across the online field",
                    "nodes": nodes,
                    "target_macs": sorted(installed_macs),
                    "target_count": len(installed_macs),
                    "already_installed_macs": sorted(field_installed_macs),
                    "activated_macs": sorted(installed_macs),
                    "node_offsets": {
                        mac: artifact.size for mac in sorted(installed_macs)
                    },
                    "completed_at": time.time(),
                    "installed_artifact_sha256": artifact.sha256,
                })
                return {
                    "ok": True,
                    "message": "firmware already installed across the online field",
                    "artifact": artifact.as_dict(),
                }
            expected_lanterns = {
                mac: all_lanterns.get(mac, {"mac": mac, "label": mac})
                for mac in expected_macs
            }
            for mac in expected_macs:
                node_offsets.setdefault(mac, 0)
            deferred = [
                {"mac": mac, "label": item.get("label") or mac}
                for mac, item in sorted(all_lanterns.items())
                if mac not in cohort_macs
            ]
            app.state.ota_install.update({
                "phase": "broadcasting" if not staged else "staged",
                "cohort_mode": "selective" if targeted_mode else "full-field",
                "target_macs": sorted(
                    selective_cohort if targeted_mode else expected_macs
                ),
                "target_count": len(
                    selective_cohort if targeted_mode else expected_macs
                ),
                "already_installed_macs": sorted(field_installed_macs),
                "node_offsets": dict(sorted(node_offsets.items())),
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
                if (
                    chunks_sent < artifact.chunks
                    and chunks_sent % OTA_CHECKPOINT_CHUNKS == 0
                ):
                    await repair_checkpoint(offset)

            # Same-protocol updates activate each verified node independently.
            # The v10-to-v11 migration is different: an activated v11 node is
            # invisible to the still-v10 primary, so every target must reach
            # the staged barrier before any activation is dispatched.
            nodes: list[dict[str, Any]] = []
            while True:
                app.state.ota_install.update({"phase": "staging", "repairing_macs": []})
                # Capture the final writer state before END closes the
                # conductor's local writer. This preserves late performer
                # failures and gives timeout retries a durable checkpoint.
                current = await probe()
                nodes = fresh_ota_nodes(current.get("nodes") or [])
                app.state.ota_install.update({"nodes": nodes})
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
                app.state.ota_install.update({
                    "nodes": nodes,
                    "staged_macs": sorted(staged_macs | activated),
                    "activated_macs": sorted(activated),
                })

                if activate and protocol_migration:
                    barrier_reached = staged_macs == expected_macs
                    if barrier_reached and not migration_activation_started:
                        migration_activation_started = True
                        app.state.ota_install.update({
                            "migration_activation_started": True,
                            "phase": "activating-protocol-migration",
                        })
                    if migration_activation_started:
                        for mac in ota_activation_order(
                            expected_macs - activation_dispatched,
                            expected_lanterns,
                        ):
                            ensure_retry_window()
                            app.state.ota_install.update({
                                "phase": "activating-protocol-migration",
                                "active_mac": mac,
                                "activation_dispatched_macs": sorted(
                                    activation_dispatched
                                ),
                            })
                            await dispatch_protocol_migration_activation(mac)
                            activation_dispatched.add(mac)
                            app.state.ota_install.update({
                                "activation_dispatched_macs": sorted(
                                    activation_dispatched
                                ),
                            })
                        if activation_dispatched == expected_macs:
                            break
                elif activate:
                    for mac in ota_activation_order(
                        staged_macs - activated,
                        expected_lanterns,
                        expected_macs=expected_macs,
                        activated_macs=activated,
                    ):
                        while mac not in activated:
                            ensure_retry_window()
                            app.state.ota_install.update({
                                "phase": "activating",
                                "active_mac": mac,
                                "activated_macs": sorted(activated),
                            })
                            # OTA status on the primary is freshness-gated. A
                            # prior performer's reboot can take long enough for
                            # the remaining staged rows to age out, even though
                            # their writers are still valid. Refresh immediately
                            # before each activation; if this target still is not
                            # staged, leave the loop so the normal final-checkpoint
                            # repair path can complete it first.
                            current = await probe()
                            node = next(
                                (
                                    item
                                    for item in fresh_ota_nodes(
                                        current.get("nodes") or []
                                    )
                                    if str(item.get("mac")) == mac
                                ),
                                None,
                            )
                            if not (
                                node
                                and node.get("phase")
                                in {"staged", "activating", "complete"}
                                and int(node.get("offset") or 0) == artifact.size
                                and int(node.get("crc32") or 0) == artifact.crc32
                            ):
                                break
                            await call_until_ok("ota_activate", mac)
                            current = await progress()
                            node = next(
                                (
                                    item
                                    for item in current.get("nodes") or []
                                    if str(item.get("mac")) == mac
                                ),
                                None,
                            )
                            if node and node.get("phase") == "complete":
                                activated.add(mac)
                                break
                            await asyncio.sleep(OTA_ACTIVATION_POLL_S)
                        if mac not in activated:
                            break
                        app.state.ota_install.update({
                            "activated_macs": sorted(activated),
                            "nodes": fresh_ota_nodes(
                                (await progress()).get("nodes") or []
                            ),
                        })
                    if activated == expected_macs:
                        break
                elif (staged_macs | activated) == expected_macs:
                    break

                await repair_checkpoint(artifact.size, single_round=True)

            if not activate:
                staged_at = time.time()
                app.state.ota_install.update({
                    "running": False,
                    "complete": False,
                    "phase": "ready-to-activate",
                    "error": None,
                    "message": "firmware staged and verified; activation is waiting for the operator",
                    "nodes": nodes,
                    "staged_at": staged_at,
                    "completed_at": staged_at,
                })
                ack = {
                    "ok": True,
                    "message": "firmware staged and verified; ready to activate",
                    "nodes": nodes,
                }
                await publish({
                    "type": "ack",
                    "action": "ota-stage",
                    "artifact": artifact.as_dict(),
                    "ack": ack,
                })
                return {
                    "ok": True,
                    "message": ack["message"],
                    "artifact": artifact.as_dict(),
                }

            if not targeted_mode:
                app.state.ota_install.update({
                    "phase": "activating-conductor",
                    "active_mac": None,
                })
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
                            detail=str(
                                conductor_activation.get("error")
                                or "conductor activation failed"
                            ),
                        )
            nodes = await infer_ota_complete_nodes(artifact.size, artifact.crc32, expected_macs)
            verified_macs = {str(node["mac"]) for node in nodes}
            if verified_macs != expected_macs:
                error = "ota post-reboot verification failed"
                nodes = append_unverified_ota_failures(nodes, expected_lanterns, verified_macs)
                app.state.ota_install.update({"nodes": nodes})
                raise HTTPException(status_code=503, detail=error)
            nodes.extend(
                {
                    "mac": mac,
                    "phase": "complete",
                    "error": "none",
                    "offset": artifact.size,
                    "crc32": artifact.crc32,
                    "source": "live_firmware_identity",
                }
                for mac in sorted(field_installed_macs)
            )
            await call_until_ok("set_ota_mode", False)
            app.state.latest_snapshot_at = 0.0
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
            "installed_artifact_sha256": artifact.sha256,
        })
        ack = {"ok": True, "message": "firmware updated across the online field", "nodes": nodes}
        await publish({"type": "ack", "action": "ota-install", "artifact": artifact.as_dict(), "ack": ack})
        return {"ok": True, "message": ack["message"], "artifact": artifact.as_dict()}

    async def ota_install_worker(
        artifact: Any,
        state: dict[str, Any],
        *,
        resume: bool = False,
        activate: bool = True,
    ) -> None:
        try:
            data = await asyncio.to_thread(app.state.ota_store.read_verified, artifact)
            await perform_ota_install(
                artifact,
                state,
                data,
                resume=resume,
                activate=activate,
            )
        except asyncio.CancelledError:
            raise
        except OtaPauseRequested:
            ota_mode_error = None
            try:
                ack = await conductor_call("set_ota_mode", False)
                if ack.get("ok") is not True:
                    ota_mode_error = str(ack.get("error") or "conductor rejected OTA shutdown")
            except SerialProtocolError as error:
                ota_mode_error = str(error)
            app.state.ota_install.update({
                "running": False,
                "complete": False,
                "phase": "paused",
                "error": None,
                "message": (
                    "firmware update paused by operator; start it again to resume"
                    if ota_mode_error is None
                    else "firmware transfer paused, but OTA maintenance mode is still active"
                ),
                "ota_mode_error": ota_mode_error,
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

    async def start_ota_install(*, activate: bool, automatic: bool = False) -> dict[str, Any]:
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
                    state = await asyncio.to_thread(enrich_state, state)
                    reject_routed_protocol_downgrade(
                        state,
                        require_artifact_protocol(artifact),
                    )
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

            started_at = time.time()
            resuming = (
                app.state.ota_install.get("phase") == "paused"
                and app.state.ota_install.get("sha256") == artifact.sha256
            )
            persistent_settings = {
                "auto_update_enabled": app.state.ota_install.get("auto_update_enabled") is True,
                "desired_artifact_sha256": artifact.sha256,
                "installed_artifact_sha256": app.state.ota_install.get("installed_artifact_sha256"),
            }
            same_artifact_retry = (
                app.state.ota_install.get("sha256") == artifact.sha256
            )
            completed_same_artifact = bool(
                same_artifact_retry
                and app.state.ota_install.get("complete") is True
            )
            preserve_migration_recovery = bool(
                same_artifact_retry
                and app.state.ota_install.get("complete") is not True
                and app.state.ota_install.get("protocol_migration") is True
                and app.state.ota_install.get("migration_activation_started") is True
            )
            preserve_selective_recovery = bool(
                resuming
                and same_artifact_retry
                and app.state.ota_install.get("cohort_mode") == "selective"
                and app.state.ota_install.get("target_macs")
            )
            preserved_activated = (
                sorted(
                    str(mac)
                    for mac in app.state.ota_install.get("activated_macs") or []
                )
                if same_artifact_retry
                else []
            )
            preserved_delivery = (
                sorted(
                    str(mac)
                    for mac in app.state.ota_install.get("delivery_confirmed_macs") or []
                )
                if same_artifact_retry
                else []
            )
            preserved_activation_attempted = (
                sorted(
                    str(mac)
                    for mac in app.state.ota_install.get(
                        "activation_attempted_macs"
                    ) or []
                )
                if preserve_migration_recovery
                else []
            )
            preserved_activation_dispatched = (
                sorted(
                    str(mac)
                    for mac in app.state.ota_install.get(
                        "activation_dispatched_macs"
                    ) or []
                )
                if preserve_migration_recovery
                else []
            )
            preserved_targets = (
                sorted(
                    str(mac)
                    for mac in app.state.ota_install.get("target_macs") or []
                )
                if preserve_migration_recovery or preserve_selective_recovery
                else []
            )
            previous_delivery_offsets = dict(
                app.state.ota_install.get("delivery_confirmed_offsets") or {}
            )
            preserved_delivery_offsets = {
                mac: max(
                    0,
                    min(int(previous_delivery_offsets.get(mac) or 0), artifact.size),
                )
                for mac in preserved_delivery
            }
            preserved_evidence = set(preserved_activated) | set(preserved_delivery)
            previous_offsets = dict(app.state.ota_install.get("node_offsets") or {})
            preserved_offsets = {
                mac: max(0, min(int(previous_offsets.get(mac) or 0), artifact.size))
                for mac in preserved_evidence
            }
            for mac in preserved_activated:
                preserved_offsets[mac] = artifact.size
            job = {
                **persistent_settings,
                "running": True,
                "complete": False,
                "error": None,
                "phase": "starting",
                "activate_after_stage": activate,
                "filename": artifact.filename,
                "sha256": artifact.sha256,
                "size": artifact.size,
                "crc32": artifact.crc32,
                "bytes_sent": int(app.state.ota_install.get("bytes_sent") or 0) if resuming else 0,
                "chunks_sent": int(app.state.ota_install.get("chunks_sent") or 0) if resuming else 0,
                "chunks_total": artifact.chunks,
                "started_at": app.state.ota_install.get("started_at") if resuming else started_at,
                "retry_timeout_s": OTA_RETRY_TIMEOUT_S,
                "retry_deadline_at": started_at + OTA_RETRY_TIMEOUT_S,
                "automatic": automatic,
                "activated_macs": preserved_activated,
                "activation_attempted_macs": preserved_activation_attempted,
                "activation_dispatched_macs": preserved_activation_dispatched,
                "target_macs": preserved_targets,
                "target_count": len(preserved_targets),
                "cohort_mode": (
                    "selective" if preserve_selective_recovery else None
                ),
                "migration_activation_started": bool(
                    preserve_migration_recovery
                    and app.state.ota_install.get("migration_activation_started")
                ),
                "protocol_migration": bool(
                    preserve_migration_recovery
                    and app.state.ota_install.get("protocol_migration")
                ),
                # A completed conductor may retain a staged local image but no
                # performer cohort. Force ota_begin so newly returned nodes are
                # frozen into a fresh cohort instead of resuming that residue.
                "refresh_cohort": completed_same_artifact,
                "delivery_confirmed_macs": preserved_delivery,
                "delivery_confirmed_offsets": dict(
                    sorted(preserved_delivery_offsets.items())
                ),
                "node_offsets": dict(sorted(preserved_offsets.items())),
            }
            if resuming:
                app.state.ota_install.update(job)
            else:
                app.state.ota_install.reset(job)
            task = asyncio.create_task(
                ota_install_worker(artifact, state, resume=resuming, activate=activate)
            )
            app.state.ota_task = task
            return {
                "ok": True,
                "message": "OTA install accepted" if activate else "OTA staging accepted",
                "install": ota_install_progress(app.state.ota_install),
            }

    async def start_ota_activation(*, automatic: bool = False) -> dict[str, Any]:
        async with app.state.ota_start_lock:
            if app.state.ota_reserved:
                raise HTTPException(status_code=409, detail="OTA operation already running")
            if app.state.ota_install.get("phase") != "ready-to-activate":
                raise HTTPException(status_code=409, detail="no verified staged field is ready to activate")
            artifact = app.state.ota_store.artifact()
            if artifact is None:
                raise HTTPException(status_code=400, detail="no firmware staged")
            if (
                int(app.state.ota_install.get("size") or 0) != artifact.size
                or int(app.state.ota_install.get("crc32") or 0) != artifact.crc32
            ):
                raise HTTPException(status_code=409, detail="staged field does not match the current artifact")
            operation_lock = acquire_ota_operation_lock()
            try:
                async with app.state.conductor_lock:
                    state = await asyncio.to_thread(app.state.conductor.snapshot)
                    state = await asyncio.to_thread(enrich_state, state)
                    reject_routed_protocol_downgrade(
                        state,
                        require_artifact_protocol(artifact),
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
            app.state.ota_install.update({
                "running": True,
                "complete": False,
                "phase": "preparing-activation",
                "activate_after_stage": True,
                "automatic": automatic,
                "error": None,
                "completed_at": None,
                "activation_requested_at": time.time(),
                "retry_timeout_s": OTA_RETRY_TIMEOUT_S,
                "retry_deadline_at": time.time() + OTA_RETRY_TIMEOUT_S,
            })
            task = asyncio.create_task(
                ota_install_worker(artifact, state, resume=True, activate=True)
            )
            app.state.ota_task = task
            return {
                "ok": True,
                "message": "OTA activation accepted",
                "install": ota_install_progress(app.state.ota_install),
            }

    async def maybe_start_ota_reconcile(state: dict[str, Any]) -> None:
        if app.state.ota_reserved or app.state.ota_install.get("running") is True:
            return
        artifact = app.state.ota_store.artifact()
        if not ota_reconcile_needed(state, artifact, app.state.ota_install):
            return
        now = time.monotonic()
        if now - app.state.ota_auto_last_attempt < OTA_AUTO_RETRY_INTERVAL_S:
            return
        app.state.ota_auto_last_attempt = now
        try:
            if app.state.ota_install.get("phase") == "ready-to-activate":
                await start_ota_activation(automatic=True)
            else:
                await start_ota_install(activate=True, automatic=True)
        except (HTTPException, SerialProtocolError) as error:
            app.state.ota_install.update({
                "auto_update_last_error": str(getattr(error, "detail", error)),
                "auto_update_last_attempt_at": time.time(),
            })

    @app.post("/api/operations/ota-install", status_code=202)
    async def install_ota_artifact() -> dict[str, Any]:
        # Normal operator path: activate each performer as soon as that board is
        # verified, keep repairing stragglers, and activate the conductor last.
        # Stage-only remains available to API clients.
        return await start_ota_install(activate=True)

    @app.post("/api/operations/ota-stage", status_code=202)
    async def stage_ota_artifact_on_field() -> dict[str, Any]:
        return await start_ota_install(activate=False)

    @app.post("/api/operations/ota-activate", status_code=202)
    async def activate_staged_ota() -> dict[str, Any]:
        return await start_ota_activation()

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
            if app.state.ota_install.get("ota_mode_error"):
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "firmware transfer paused, but the conductor did not exit OTA maintenance: "
                        f"{app.state.ota_install['ota_mode_error']}"
                    ),
                )
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
        if not direct_loopback_request(ws.scope, ws.headers):
            await ws.close(code=4403)
            return
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
            state = cached_snapshot()
            if state is None:
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
