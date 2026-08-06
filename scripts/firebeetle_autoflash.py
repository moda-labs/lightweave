#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit


LABEL = "com.lightweave.firebeetle-autoflash"
DEFAULT_CHANNEL = (
    "https://raw.githubusercontent.com/moda-labs/lightweave/"
    "main/deploy/channels/production.json"
)
# The project moved from underminedsk/ to moda-labs/ after v0.7.0. Release
# manifests are immutable, so releases published before the move still name the
# old remote. Both stay allowed until every promoted release names moda-labs.
ALLOWED_REPOSITORIES = (
    "https://github.com/moda-labs/lightweave.git",
    "https://github.com/underminedsk/lightweave.git",
)
WCH_VID = 0x1A86
WCH_PID = 0x7522
EXPECTED_SEGMENTS = {
    0x1000: "bootloader.bin",
    0x8000: "partitions.bin",
    0xE000: "boot_app0.bin",
    0x10000: "firmware.bin",
}
INFO_RE = re.compile(
    r"role=(?P<role>[A-Z]+)\s+id=(?P<id>\d+)\s+mac=(?P<mac>[0-9A-F:]{17})"
    r"\s+x=(?P<x>-?[0-9.]+)\s+y=(?P<y>-?[0-9.]+)",
    re.I,
)
FIRMWARE_RE = re.compile(
    r"firmware:\s+v(?P<version>\S+)\s+proto=(?P<proto>\d+)\s+"
    r"build=(?P<build>[0-9a-f]{8})(?P<dirty>\s+dirty)?",
    re.I,
)
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    print(f"{now()} {message}", flush=True)


@dataclass(frozen=True)
class DeviceInfo:
    role: str
    node_id: int
    mac: str
    x: float
    y: float
    version: str
    proto: int
    build: str
    dirty: bool


@dataclass(frozen=True)
class DeviceRecord:
    state: str
    node_id: int | None = None
    legacy_identity_pending: bool = False


@dataclass(frozen=True)
class PortCandidate:
    device: str
    location: str | None
    hardware_id: str
    instance_id: str | None = None


ProgressCallback = Callable[[str, str], None]
IdResolver = Callable[[str, int], tuple[int, bool]]


def parse_device_info(text: str) -> DeviceInfo | None:
    identity = INFO_RE.search(text)
    firmware = FIRMWARE_RE.search(text)
    if not identity or not firmware:
        return None
    return DeviceInfo(
        role=identity["role"].upper(),
        node_id=int(identity["id"]),
        mac=identity["mac"].upper(),
        x=float(identity["x"]),
        y=float(identity["y"]),
        version=firmware["version"],
        proto=int(firmware["proto"]),
        build=firmware["build"].lower(),
        dirty=bool(firmware["dirty"]),
    )


def parse_probe(text: str) -> dict[str, str] | None:
    chip = re.search(r"Chip is (ESP32-D0WD-V3)", text)
    crystal = re.search(r"Crystal is (40MHz)", text)
    macs = re.findall(r"MAC:\s*([0-9a-f:]{17})", text, re.I)
    size = re.search(r"Detected flash size:\s*(4MB)", text)
    if not chip or not crystal or not macs or not size:
        return None
    return {"chip": chip[1], "crystal": crystal[1], "mac": macs[-1].upper(), "size": size[1]}


def approved_build(manifest: dict[str, Any]) -> str:
    commit = str(manifest.get("commit", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("release manifest commit is invalid")
    return commit[:8]


def should_erase(
    info: DeviceInfo | None,
    *,
    device_state: str | None,
    factory_authorized: bool,
) -> bool:
    return info is None and (
        device_state == "erase_authorized"
        or (device_state is None and factory_authorized)
    )


def should_skip(info: DeviceInfo | None, manifest: dict[str, Any]) -> bool:
    return bool(info and not info.dirty and info.build == approved_build(manifest))


def _download(url: str, limit: int) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("release downloads must use credential-free HTTPS URLs")
    request = urllib.request.Request(url, headers={"User-Agent": "lightweave-autoflash/1"})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"download from {url} exceeds size limit")
    return data


def _json(data: bytes, name: str) -> dict[str, Any]:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _validate_channel(channel: dict[str, Any]) -> None:
    if set(channel) != {"schema_version", "enabled", "manifest_url", "manifest_sha256"}:
        raise ValueError("production channel keys are invalid")
    if channel["schema_version"] != 1 or not isinstance(channel["enabled"], bool):
        raise ValueError("production channel is invalid")
    if not channel["enabled"]:
        if channel["manifest_url"] is not None or channel["manifest_sha256"] is not None:
            raise ValueError("disabled production channel must not name a manifest")
        return
    if not isinstance(channel["manifest_sha256"], str) or not SHA256_RE.fullmatch(
        channel["manifest_sha256"]
    ):
        raise ValueError("production channel manifest SHA-256 is invalid")


def _release_asset_url(repository: str, release: str, filename: str) -> str:
    return f"{repository.removesuffix('.git')}/releases/download/{release}/{filename}"


def _canonical_manifest_urls(release: str) -> tuple[str, ...]:
    return tuple(
        _release_asset_url(repository, release, "lightweave-release.json")
        for repository in ALLOWED_REPOSITORIES
    )


def _validate_manifest(manifest: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    version = manifest.get("version")
    release = manifest.get("release")
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        raise ValueError("release manifest version is invalid")
    if release != f"v{version}":
        raise ValueError("release manifest tag is invalid")
    repository = manifest.get("repository")
    if repository not in ALLOWED_REPOSITORIES:
        raise ValueError("release manifest repository is not allowed")
    if manifest.get("ref") != f"refs/tags/{release}":
        raise ValueError("release manifest does not name an immutable tag")
    approved_build(manifest)
    serial = manifest.get("serial_flash")
    if not isinstance(serial, dict) or set(serial) != {"filename", "url", "sha256", "size"}:
        raise ValueError("release serial flash metadata is invalid")
    expected_filename = f"lightweave-serial-flash-{release}.zip"
    if serial.get("filename") != expected_filename:
        raise ValueError("release serial flash filename is invalid")
    # Tie the asset URL to the manifest's own repository, so a manifest cannot
    # name one remote and serve its bundle from the other.
    if serial.get("url") != _release_asset_url(repository, release, expected_filename):
        raise ValueError("release serial flash URL is not canonical")
    size = serial.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= 8 * 1024 * 1024:
        raise ValueError("release serial flash size is invalid")
    sha256 = serial.get("sha256")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise ValueError("release serial flash SHA-256 is invalid")
    return release, serial


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    with temporary.open("wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_device_registry(path: Path) -> dict[str, DeviceRecord]:
    if not path.is_file():
        return {}
    document = _json(path.read_bytes(), "known-device registry")
    if (
        set(document) != {"schema_version", "devices"}
        or document["schema_version"] not in {1, 2}
    ):
        raise ValueError("known-device registry is invalid")
    devices = document["devices"]
    if not isinstance(devices, dict):
        raise ValueError("known-device registry entries are invalid")
    records: dict[str, DeviceRecord] = {}
    for mac, value in devices.items():
        if not isinstance(mac, str) or not MAC_RE.fullmatch(mac):
            raise ValueError("known-device registry entries are invalid")
        if document["schema_version"] == 1:
            state = value
            node_id = None
            legacy_identity_pending = True
        elif (
            isinstance(value, dict)
            and set(value)
            in (
                {"state", "node_id"},
                {"state", "node_id", "legacy_identity_pending"},
            )
        ):
            state = value["state"]
            node_id = value["node_id"]
            legacy_identity_pending = value.get("legacy_identity_pending", False)
        else:
            raise ValueError("known-device registry entries are invalid")
        if (
            state not in {"known", "erase_pending", "erase_authorized"}
            or (
                node_id is not None
                and (
                    not isinstance(node_id, int)
                    or isinstance(node_id, bool)
                    or not 1 <= node_id <= 65535
                )
            )
            or not isinstance(legacy_identity_pending, bool)
        ):
            raise ValueError("known-device registry entries are invalid")
        records[mac] = DeviceRecord(state, node_id, legacy_identity_pending)
    assigned = [record.node_id for record in records.values() if record.node_id is not None]
    if len(assigned) != len(set(assigned)):
        raise ValueError("known-device registry contains duplicate board IDs")
    return records


@contextmanager
def _device_registry_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(f".{path.name}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_device_registry_unlocked(
    path: Path, devices: dict[str, DeviceRecord]
) -> None:
    document = {
        "schema_version": 2,
        "devices": {
            mac: {
                "state": record.state,
                "node_id": record.node_id,
                "legacy_identity_pending": record.legacy_identity_pending,
            }
            for mac, record in sorted(devices.items())
        },
    }
    _write_atomic(path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())


def write_device_registry(path: Path, devices: dict[str, DeviceRecord]) -> None:
    with _device_registry_lock(path):
        _write_device_registry_unlocked(path, devices)


def write_device_state(path: Path, mac: str, state: str) -> None:
    if not MAC_RE.fullmatch(mac):
        raise ValueError("cannot remember an invalid ROM MAC")
    if state not in {"known", "erase_pending", "erase_authorized"}:
        raise ValueError("cannot remember an invalid device state")
    with _device_registry_lock(path):
        devices = load_device_registry(path)
        prior = devices.get(mac)
        devices[mac] = DeviceRecord(
            state,
            prior.node_id if prior else None,
            prior.legacy_identity_pending if prior else False,
        )
        _write_device_registry_unlocked(path, devices)


def ensure_node_id(path: Path, mac: str, reported_id: int) -> tuple[int, bool]:
    if not MAC_RE.fullmatch(mac) or not 0 <= reported_id <= 65535:
        raise ValueError("invalid board identity")
    with _device_registry_lock(path):
        devices = load_device_registry(path)
        record = devices.get(mac)
        if record is None:
            raise ValueError("board state must be recorded before assigning an ID")
        if record.node_id is not None:
            if reported_id not in {0, record.node_id}:
                raise RuntimeError(
                    f"board {mac} reports ID #{reported_id}, but registry permanently assigns "
                    f"#{record.node_id}"
                )
            if record.legacy_identity_pending:
                devices[mac] = DeviceRecord(record.state, record.node_id, False)
                _write_device_registry_unlocked(path, devices)
            return record.node_id, False
        used = {item.node_id for item in devices.values() if item.node_id is not None}
        if reported_id:
            if reported_id in used:
                owner = next(
                    key for key, item in devices.items() if item.node_id == reported_id
                )
                raise RuntimeError(f"board ID #{reported_id} already belongs to {owner}")
            node_id = reported_id
        else:
            if record.legacy_identity_pending:
                devices[mac] = DeviceRecord(record.state, None, False)
            pending = [
                key for key, item in devices.items() if item.legacy_identity_pending
            ]
            if pending:
                if record.legacy_identity_pending:
                    _write_device_registry_unlocked(path, devices)
                raise RuntimeError(
                    "legacy ID inventory is incomplete; scan "
                    f"{len(pending)} remaining known board(s), then reconnect {mac}"
                )
            node_id = next(
                (candidate for candidate in range(1, 65536) if candidate not in used),
                0,
            )
            if node_id == 0:
                raise RuntimeError("no board IDs remain")
        devices[mac] = DeviceRecord(record.state, node_id, False)
        _write_device_registry_unlocked(path, devices)
        return node_id, True


def authorize_factory_retry(path: Path, mac: str) -> None:
    with _device_registry_lock(path):
        devices = load_device_registry(path)
        if not devices.get(mac) or devices[mac].state != "erase_pending":
            raise ValueError("device does not have an ambiguous factory erase")
        prior = devices[mac]
        devices[mac] = DeviceRecord(
            "erase_authorized", prior.node_id, prior.legacy_identity_pending
        )
        _write_device_registry_unlocked(path, devices)


def refresh_artifact(channel_url: str, cache: Path) -> tuple[dict[str, Any], Path] | None:
    channel = _json(_download(channel_url, 64 * 1024), "production channel")
    _validate_channel(channel)
    if not channel["enabled"]:
        return None
    try:
        cached = load_cached_artifact(cache)
    except (OSError, ValueError):
        cached = None
    if cached:
        cached_manifest = cache / cached[0]["release"] / "lightweave-release.json"
        if hashlib.sha256(cached_manifest.read_bytes()).hexdigest() == channel["manifest_sha256"]:
            if channel["manifest_url"] not in _canonical_manifest_urls(
                cached[0]["release"]
            ):
                raise ValueError("production channel manifest URL is not canonical")
            return cached
    manifest_url = str(channel["manifest_url"])
    manifest_bytes = _download(manifest_url, 128 * 1024)
    if hashlib.sha256(manifest_bytes).hexdigest() != channel.get("manifest_sha256"):
        raise ValueError("release manifest SHA-256 mismatch")
    manifest = _json(manifest_bytes, "release manifest")
    release, serial = _validate_manifest(manifest)
    if manifest_url not in _canonical_manifest_urls(release):
        raise ValueError("production channel manifest URL is not canonical")
    bundle = _download(str(serial["url"]), 8 * 1024 * 1024)
    if len(bundle) != serial.get("size"):
        raise ValueError("serial flash bundle size mismatch")
    if hashlib.sha256(bundle).hexdigest() != serial.get("sha256"):
        raise ValueError("serial flash bundle SHA-256 mismatch")
    release_dir = cache / release
    release_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = release_dir / str(serial["filename"])
    _write_atomic(bundle_path, bundle)
    _write_atomic(release_dir / "lightweave-release.json", manifest_bytes)
    _write_atomic(
        cache / "current.json",
        (json.dumps({"release": release}, sort_keys=True) + "\n").encode(),
    )
    return manifest, bundle_path


def load_cached_artifact(cache: Path) -> tuple[dict[str, Any], Path] | None:
    pointer = cache / "current.json"
    if not pointer.is_file():
        return None
    release = str(_json(pointer.read_bytes(), "cache pointer").get("release", ""))
    if not release.startswith("v") or not SEMVER_RE.fullmatch(release[1:]):
        raise ValueError("cache pointer release is invalid")
    release_dir = cache / release
    manifest = _json((release_dir / "lightweave-release.json").read_bytes(), "cached manifest")
    validated_release, serial = _validate_manifest(manifest)
    if release != validated_release:
        raise ValueError("cache pointer does not match cached manifest")
    bundle = release_dir / str(serial.get("filename", ""))
    data = bundle.read_bytes()
    if len(data) != serial.get("size") or hashlib.sha256(data).hexdigest() != serial.get("sha256"):
        raise ValueError("cached serial flash bundle failed integrity verification")
    return manifest, bundle


def extract_bundle(bundle: Path, destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        if len(names) != len(archive.namelist()):
            raise ValueError("serial flash bundle contains duplicate members")
        if any(
            PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
            for name in names
        ):
            raise ValueError("serial flash bundle contains an unsafe member path")
        members = archive.infolist()
        if any(item.compress_type != zipfile.ZIP_STORED for item in members):
            raise ValueError("serial flash bundle must use stored members")
        if sum(item.file_size for item in members) > 16 * 1024 * 1024:
            raise ValueError("serial flash bundle expands beyond its size limit")
        plan = _json(archive.read("flash-plan.json"), "flash plan")
        schema_version = plan.get("schema_version")
        if (
            schema_version not in {1, 2, 3}
            or plan.get("chip") != "esp32"
            or plan.get("flash_size") != "4MB"
            or plan.get("flash_mode") != "dio"
            or plan.get("flash_freq") != "40m"
        ):
            raise ValueError("flash plan settings are invalid")
        expected = {"flash-plan.json", *EXPECTED_SEGMENTS.values()}
        tool_members: list[dict[str, Any]] = []
        if schema_version in {2, 3}:
            tool = plan.get("tool")
            if not isinstance(tool, dict) or set(tool) != {"name", "members"}:
                raise ValueError("serial flash tool manifest is invalid")
            raw_tool_members = tool.get("members")
            if tool.get("name") != "esptool" or not isinstance(raw_tool_members, list):
                raise ValueError("serial flash tool manifest is invalid")
            tool_names: set[str] = set()
            for item in raw_tool_members:
                if not isinstance(item, dict) or set(item) != {"filename", "sha256", "size"}:
                    raise ValueError("serial flash tool manifest is invalid")
                filename = item.get("filename")
                size = item.get("size")
                sha256 = item.get("sha256")
                if (
                    not isinstance(filename, str)
                    or filename in tool_names
                    or (
                        filename
                        not in {"esptool.py", "esptool-LICENSE", "intelhex-LICENSE"}
                        and not filename.startswith(("esptool/", "intelhex/"))
                    )
                    or PurePosixPath(filename).is_absolute()
                    or ".." in PurePosixPath(filename).parts
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or not isinstance(sha256, str)
                    or not SHA256_RE.fullmatch(sha256)
                ):
                    raise ValueError("serial flash tool manifest is invalid")
                tool_names.add(filename)
                tool_members.append(item)
            expected.update(tool_names)
            if "esptool.py" not in expected or "esptool/__main__.py" not in expected:
                raise ValueError("serial flash tool manifest is incomplete")
            if schema_version == 3 and not {
                "intelhex/__init__.py",
                "intelhex-LICENSE",
            }.issubset(expected):
                raise ValueError("serial flash tool dependency manifest is incomplete")
        if names != expected:
            raise ValueError("serial flash bundle members are invalid")
        segments = plan.get("segments")
        if not isinstance(segments, list) or len(segments) != len(EXPECTED_SEGMENTS):
            raise ValueError("flash plan segments are invalid")
        seen = set()
        for segment in segments:
            offset = segment.get("offset") if isinstance(segment, dict) else None
            filename = segment.get("filename") if isinstance(segment, dict) else None
            if EXPECTED_SEGMENTS.get(offset) != filename or offset in seen:
                raise ValueError("flash plan offset is invalid")
            data = archive.read(filename)
            if len(data) != segment.get("size") or hashlib.sha256(data).hexdigest() != segment.get("sha256"):
                raise ValueError(f"flash segment {filename} failed integrity verification")
            (destination / filename).write_bytes(data)
            seen.add(offset)
        for member in tool_members:
            filename = member["filename"]
            data = archive.read(filename)
            if (
                len(data) != member.get("size")
                or hashlib.sha256(data).hexdigest() != member.get("sha256")
            ):
                raise ValueError(f"serial flash tool member {filename} failed integrity verification")
            target = destination / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return plan


def exchange_info(
    port: str, *, command: str | None = None, duration: float = 3.0
) -> DeviceInfo | None:
    import serial

    connection = serial.Serial()
    connection.port = port
    connection.baudrate = 115200
    connection.timeout = 0.2
    connection.dtr = False
    connection.rts = False
    connection.open()
    try:
        time.sleep(0.8)
        connection.reset_input_buffer()
        connection.write(b"\n")
        connection.flush()
        time.sleep(0.2)
        if command is not None:
            connection.write(command.encode("ascii") + b"\n")
            connection.flush()
            time.sleep(0.5)
        connection.write(b"info\n")
        connection.flush()
        deadline = time.monotonic() + duration
        output = bytearray()
        while time.monotonic() < deadline:
            output.extend(connection.read(1024))
        return parse_device_info(output.decode(errors="replace"))
    finally:
        connection.close()


def read_info(port: str, duration: float = 3.0) -> DeviceInfo | None:
    return exchange_info(port, duration=duration)


def write_node_id(port: str, node_id: int) -> DeviceInfo | None:
    if not 1 <= node_id <= 65535:
        raise ValueError("board ID is out of range")
    return exchange_info(port, command=f"id {node_id}", duration=5.0)


def log_board_id(port: str, mac: str, node_id: int, *, newly_assigned: bool) -> None:
    status = "NEW ID - LABEL THIS BOARD" if newly_assigned else "VERIFIED - LABEL THIS BOARD"
    log(f"{port}: ============================================================")
    log(f"{port}: BOARD #{node_id}  {status}")
    log(f"{port}: MAC {mac}")
    log(f"{port}: ============================================================")


def stable_platformio_python(interpreter: Path) -> Path:
    path_parts = interpreter.parts
    if "Cellar" in path_parts:
        cellar = path_parts.index("Cellar")
        if len(path_parts) > cellar + 1 and path_parts[cellar + 1] == "platformio":
            stable = Path(*path_parts[:cellar]) / "opt/platformio/libexec/bin/python"
            if stable.is_file():
                interpreter = stable
    if not interpreter.is_file():
        raise RuntimeError("PlatformIO Python interpreter is unavailable")
    return interpreter


def esptool_command(directory: Path | None = None) -> list[str]:
    bundled = directory / "esptool.py" if directory is not None else None
    if bundled is not None and bundled.is_file():
        return [sys.executable, str(bundled)]
    if importlib.util.find_spec("esptool") is not None:
        return [sys.executable, "-m", "esptool"]
    tool = Path.home() / ".platformio/packages/tool-esptoolpy/esptool.py"
    if not tool.is_file():
        raise RuntimeError("esptool is not installed in this environment or through PlatformIO")
    return [str(stable_platformio_python(Path(sys.executable))), str(tool)]


def _tool_environment() -> dict[str, str]:
    sensitive = ("TOKEN", "PASSWORD", "SECRET", "CREDENTIAL", "API_KEY", "PRIVATE_KEY")
    return {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in sensitive)
    }


def run_tool(arguments: list[str], *, timeout_s: float = 120.0) -> str:
    try:
        result = subprocess.run(
            arguments,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_tool_environment(),
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"flashing tool timed out after {timeout_s:g} seconds") from error
    if result.returncode:
        raise RuntimeError(result.stdout.strip())
    return result.stdout


def probe_board(port: str, directory: Path | None = None) -> dict[str, str]:
    output = run_tool(
        esptool_command(directory) + ["--port", port, "flash_id"], timeout_s=30
    )
    probe = parse_probe(output)
    if not probe:
        raise RuntimeError("device is not the expected ESP32-D0WD-V3/40MHz/4MB class")
    return probe


def erase_board(port: str, directory: Path | None = None) -> None:
    base = esptool_command(directory) + ["--chip", "esp32", "--port", port, "--baud", "115200"]
    log(f"{port}: factory/unrecognized firmware; performing one-time erase")
    run_tool(base + ["erase_flash"], timeout_s=60)


def flash_board(port: str, plan: dict[str, Any], directory: Path) -> None:
    base = esptool_command(directory) + ["--chip", "esp32", "--port", port, "--baud", "115200"]
    arguments = base + [
        "write_flash", "-z", "--flash_mode", plan["flash_mode"],
        "--flash_freq", plan["flash_freq"], "--flash_size", plan["flash_size"],
    ]
    for segment in sorted(plan["segments"], key=lambda item: item["offset"]):
        arguments += [hex(segment["offset"]), str(directory / segment["filename"])]
    run_tool(arguments, timeout_s=120)


def preserves_role_position(before: DeviceInfo, after: DeviceInfo) -> bool:
    return (
        before.role == after.role
        and abs(before.x - after.x) < 0.0001
        and abs(before.y - after.y) < 0.0001
    )


def process_port(
    port: str,
    manifest: dict[str, Any],
    bundle: Path,
    work: Path,
    *,
    device_registry: Path,
    factory_authorized: bool,
    progress: ProgressCallback | None = None,
    id_resolver: IdResolver | None = None,
) -> str:
    def report(stage: str, message: str) -> None:
        if progress is not None:
            progress(stage, message)

    def resolve_id(mac: str, reported_id: int) -> tuple[int, bool]:
        if id_resolver is None:
            assigned, recorded = ensure_node_id(device_registry, mac, reported_id)
            return assigned, recorded and reported_id == 0
        assigned, created = id_resolver(mac, reported_id)
        cached, _recorded = ensure_node_id(device_registry, mac, assigned)
        if cached != assigned:
            raise RuntimeError("local and authoritative permanent IDs disagree")
        return assigned, created

    devices = load_device_registry(device_registry)
    destination = work / hashlib.sha256(port.encode()).hexdigest()[:16]
    report("preparing", "Validating production firmware bundle")
    plan = extract_bundle(bundle, destination)
    report("probing", "Reading board identity")
    before = read_info(port)
    probe = probe_board(port, destination)
    if before is None:
        # A valid field node may be in daytime deep sleep and unable to answer
        # until esptool's non-destructive ROM probe resets it into a cold boot.
        time.sleep(1.5)
        before = read_info(port, 5.0)
    if before and before.mac != probe["mac"]:
        raise RuntimeError("serial identity and ROM MAC disagree")
    if before and before.role == "CONDUCTOR":
        raise RuntimeError("refusing to auto-flash a conductor")
    if before and before.role != "PERFORMER":
        raise RuntimeError(f"unsupported board role {before.role}")
    record = devices.get(probe["mac"])
    device_state = record.state if record else None
    if before is not None:
        write_device_state(device_registry, probe["mac"], "known")
        device_state = "known"
    elif device_state == "erase_pending":
        retry_python = stable_platformio_python(Path(sys.executable))
        retry_command = shlex.join(
            [
                str(retry_python),
                str(Path(__file__).resolve()),
                "retry-factory",
                probe["mac"],
                "--state",
                str(device_registry.parent),
            ]
        )
        raise RuntimeError(
            f"prior factory erase has an ambiguous result for {probe['mac']}; "
            f"run: {retry_command}"
        )
    if (
        before is None
        and device_state not in {"known", "erase_authorized"}
        and not factory_authorized
    ):
        raise RuntimeError(
            "device is not recognized; not flashing without explicit factory authorization"
        )
    assigned_id: int | None = None
    allocated_new = False
    if before is not None:
        report("reserving_id", "Verifying permanent board ID")
        assigned_id, allocated_new = resolve_id(probe["mac"], before.node_id)

    if should_skip(before, manifest):
        after = before
        action = f"already runs approved build {approved_build(manifest)}"
        report("verifying", "Approved firmware already installed")
    else:
        log(
            f"{port}: starting flash of {probe['mac']} "
            f"to production build {approved_build(manifest)}"
        )
        erase = should_erase(
            before,
            device_state=device_state,
            factory_authorized=factory_authorized,
        )
        if erase:
            # A pending marker is intentionally ambiguous. A crash or command
            # error requires explicit per-MAC operator authorization.
            write_device_state(device_registry, probe["mac"], "erase_pending")
            report("erasing", "Erasing factory flash")
            erase_board(port, destination)
            write_device_state(device_registry, probe["mac"], "known")
        report("flashing", f"Writing production build {approved_build(manifest)}")
        flash_board(port, plan, destination)
        report("rebooting", "Waiting for board to reboot")
        time.sleep(1.5)
        report("verifying", "Verifying firmware, MAC, and role")
        after = read_info(port, 5.0)
        if not after or after.build != approved_build(manifest) or after.dirty:
            raise RuntimeError("post-flash firmware identity did not match the production release")
        if after.mac != probe["mac"]:
            raise RuntimeError("post-flash firmware and ROM MAC disagree")
        if after.role != "PERFORMER":
            raise RuntimeError(f"post-flash role is {after.role}, expected PERFORMER")
        if before and not preserves_role_position(before, after):
            raise RuntimeError("post-flash NVS role/position changed")
        action = f"flashed build {after.build}; role/position verified"

    if after is None:
        raise RuntimeError("board identity unavailable after provisioning")
    if assigned_id is None:
        report("reserving_id", "Reserving permanent board ID")
        assigned_id, allocated_new = resolve_id(probe["mac"], after.node_id)
    if after.node_id != assigned_id:
        before_id_write = after
        report("assigning_id", f"Writing permanent board ID #{assigned_id}")
        after = write_node_id(port, assigned_id)
        if (
            not after
            or after.mac != probe["mac"]
            or after.node_id != assigned_id
            or after.build != approved_build(manifest)
            or after.dirty
            or not preserves_role_position(before_id_write, after)
        ):
            raise RuntimeError(f"failed to persist and verify permanent board ID #{assigned_id}")
    log_board_id(
        port,
        probe["mac"],
        assigned_id,
        newly_assigned=allocated_new,
    )
    report("done", f"BOARD #{assigned_id} ready to label")
    return f"{after.mac} {action}; permanent ID #{assigned_id} verified"


def candidate_port_infos() -> list[PortCandidate]:
    from serial.tools import list_ports

    candidates = []
    for item in list_ports.comports():
        if not (
            item.vid == WCH_VID
            and item.pid == WCH_PID
            and (
                item.device.startswith("/dev/cu.")
                or item.device.startswith("/dev/ttyUSB")
            )
        ):
            continue
        try:
            device_stat = os.stat(item.device)
            # Opening a character device changes its ctime on macOS. Including
            # ctime here made the provisioner mistake every serial inspection
            # for a physical disconnect/reconnect and discard the result.
            # The device number and devfs inode remain stable while connected
            # and change when the device node is recreated after a replug.
            instance_id = f"{device_stat.st_rdev}:{device_stat.st_ino}"
        except OSError:
            instance_id = None
        candidates.append(
            PortCandidate(
                device=item.device,
                location=item.location,
                hardware_id=item.hwid,
                instance_id=instance_id,
            )
        )
    return candidates


def candidate_ports() -> set[str]:
    return {item.device for item in candidate_port_infos()}


def watch(args: argparse.Namespace) -> int:
    state = Path(args.state).expanduser()
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / "watch.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        approved = load_cached_artifact(state / "cache")
    except Exception as error:
        log(f"cached artifact rejected: {error}")
        approved = None
    last_refresh = 0.0
    seen: set[str] = set()
    while True:
        if time.monotonic() - last_refresh >= args.refresh:
            try:
                refreshed = refresh_artifact(args.channel, state / "cache")
                approved = refreshed
                if refreshed:
                    log(f"approved production release {approved[0]['release']} build {approved_build(approved[0])}")
                else:
                    log("production channel is disabled")
            except Exception as error:
                log(f"artifact refresh failed; retaining cached artifact: {error}")
            last_refresh = time.monotonic()
        current = candidate_ports()
        for port in sorted(current - seen):
            if not approved:
                log(f"{port}: no approved production artifact; not flashing")
                continue
            try:
                result = process_port(
                    port,
                    approved[0],
                    approved[1],
                    state / "work",
                    device_registry=state / "devices.json",
                    factory_authorized=args.factory,
                )
                log(f"{port}: {result}")
            except Exception as error:
                log(f"{port}: FAILED: {error}")
        seen = current
        time.sleep(args.interval)


def stable_pio_python(pio: Path) -> Path:
    parts = shlex.split(pio.read_text(encoding="utf-8").splitlines()[0].removeprefix("#!"))
    if not parts:
        raise RuntimeError("cannot determine PlatformIO Python interpreter")
    if parts[0] == "/usr/bin/env":
        if len(parts) != 2 or not shutil.which(parts[1]):
            raise RuntimeError("cannot resolve PlatformIO Python interpreter")
        interpreter = Path(shutil.which(parts[1]) or "")
    elif len(parts) == 1:
        interpreter = Path(parts[0])
    else:
        raise RuntimeError("cannot determine PlatformIO Python interpreter")
    return stable_platformio_python(interpreter)


def install(args: argparse.Namespace) -> int:
    source = Path(__file__).resolve()
    home = Path.home()
    install_dir = home / "Library/Application Support/Lightweave/autoflash"
    install_dir.mkdir(parents=True, exist_ok=True)
    installed = install_dir / source.name
    shutil.copy2(source, installed)
    pio = Path(shutil.which("pio") or "")
    if not pio.is_file():
        raise RuntimeError("pio is not installed")
    python = stable_pio_python(pio)
    subprocess.run([str(python), "-c", "import serial"], check=True)
    logs = home / "Library/Logs"
    logs.mkdir(parents=True, exist_ok=True)
    plist = home / f"Library/LaunchAgents/{LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    program_arguments = [str(python), str(installed), "watch", "--channel", args.channel]
    if args.factory:
        program_arguments.append("--factory")
    document = {
        "Label": LABEL,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": str(logs / "lightweave-firebeetle-autoflash.log"),
        "StandardErrorPath": str(logs / "lightweave-firebeetle-autoflash.log"),
    }
    plist.write_bytes(plistlib.dumps(document))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    print(f"installed {LABEL}; log: {document['StandardOutPath']}")
    return 0


def uninstall(_args: argparse.Namespace) -> int:
    plist = Path.home() / f"Library/LaunchAgents/{LABEL}.plist"
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], check=False)
    plist.unlink(missing_ok=True)
    print(f"uninstalled {LABEL}; cached production artifacts were retained")
    return 0


def retry_factory(args: argparse.Namespace) -> int:
    state = Path(args.state).expanduser()
    mac = args.mac.upper()
    authorize_factory_retry(state / "devices.json", mac)
    print(f"authorized one factory-erase retry for {mac}; unplug and reconnect the board")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Flash connected FireBeetles from production releases")
    sub = parser.add_subparsers(dest="command", required=True)
    watcher = sub.add_parser("watch")
    watcher.add_argument("--channel", default=DEFAULT_CHANNEL)
    watcher.add_argument("--state", default="~/Library/Application Support/Lightweave/autoflash")
    watcher.add_argument("--interval", type=float, default=1.0)
    watcher.add_argument("--refresh", type=float, default=300.0)
    watcher.add_argument(
        "--factory",
        action="store_true",
        help="authorize one first erase for previously unseen, unrecognized boards",
    )
    watcher.set_defaults(function=watch)
    installer = sub.add_parser("install")
    installer.add_argument("--channel", default=DEFAULT_CHANNEL)
    installer.add_argument(
        "--factory",
        action="store_true",
        help="install a factory station allowed to erase new, unrecognized boards",
    )
    installer.set_defaults(function=install)
    sub.add_parser("uninstall").set_defaults(function=uninstall)
    retry = sub.add_parser("retry-factory")
    retry.add_argument("mac", help="ROM MAC printed in the failed watcher log")
    retry.add_argument("--state", default="~/Library/Application Support/Lightweave/autoflash")
    retry.set_defaults(function=retry_factory)
    args = parser.parse_args()
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
