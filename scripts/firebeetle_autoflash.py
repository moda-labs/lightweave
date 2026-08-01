#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


LABEL = "com.lightweave.firebeetle-autoflash"
DEFAULT_CHANNEL = (
    "https://raw.githubusercontent.com/underminedsk/lightweave/"
    "main/deploy/channels/production.json"
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


def _validate_manifest(manifest: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    version = manifest.get("version")
    release = manifest.get("release")
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        raise ValueError("release manifest version is invalid")
    if release != f"v{version}":
        raise ValueError("release manifest tag is invalid")
    if manifest.get("repository") != "https://github.com/underminedsk/lightweave.git":
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
    expected_url = (
        "https://github.com/underminedsk/lightweave/releases/download/"
        f"{release}/{expected_filename}"
    )
    if serial.get("url") != expected_url:
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


def load_device_registry(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    document = _json(path.read_bytes(), "known-device registry")
    if set(document) != {"schema_version", "devices"} or document["schema_version"] != 1:
        raise ValueError("known-device registry is invalid")
    devices = document["devices"]
    if (
        not isinstance(devices, dict)
        or any(
            not isinstance(mac, str)
            or not MAC_RE.fullmatch(mac)
            or not isinstance(state, str)
            or state not in {"known", "erase_pending", "erase_authorized"}
            for mac, state in devices.items()
        )
    ):
        raise ValueError("known-device registry entries are invalid")
    return dict(devices)


def write_device_state(path: Path, mac: str, state: str) -> None:
    if not MAC_RE.fullmatch(mac):
        raise ValueError("cannot remember an invalid ROM MAC")
    if state not in {"known", "erase_pending", "erase_authorized"}:
        raise ValueError("cannot remember an invalid device state")
    devices = load_device_registry(path)
    devices[mac] = state
    document = {"schema_version": 1, "devices": dict(sorted(devices.items()))}
    _write_atomic(path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())


def authorize_factory_retry(path: Path, mac: str) -> None:
    devices = load_device_registry(path)
    if devices.get(mac) != "erase_pending":
        raise ValueError("device does not have an ambiguous factory erase")
    write_device_state(path, mac, "erase_authorized")


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
            expected_manifest_url = (
                "https://github.com/underminedsk/lightweave/releases/download/"
                f"{cached[0]['release']}/lightweave-release.json"
            )
            if channel["manifest_url"] != expected_manifest_url:
                raise ValueError("production channel manifest URL is not canonical")
            return cached
    manifest_url = str(channel["manifest_url"])
    manifest_bytes = _download(manifest_url, 128 * 1024)
    if hashlib.sha256(manifest_bytes).hexdigest() != channel.get("manifest_sha256"):
        raise ValueError("release manifest SHA-256 mismatch")
    manifest = _json(manifest_bytes, "release manifest")
    release, serial = _validate_manifest(manifest)
    expected_manifest_url = (
        "https://github.com/underminedsk/lightweave/releases/download/"
        f"{release}/lightweave-release.json"
    )
    if manifest_url != expected_manifest_url:
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
        expected = {"flash-plan.json", *EXPECTED_SEGMENTS.values()}
        if names != expected:
            raise ValueError("serial flash bundle members are invalid")
        members = archive.infolist()
        if any(item.compress_type != zipfile.ZIP_STORED for item in members):
            raise ValueError("serial flash bundle must use stored members")
        if sum(item.file_size for item in members) > 8 * 1024 * 1024:
            raise ValueError("serial flash bundle expands beyond its size limit")
        plan = _json(archive.read("flash-plan.json"), "flash plan")
        if (
            plan.get("schema_version") != 1
            or plan.get("chip") != "esp32"
            or plan.get("flash_size") != "4MB"
            or plan.get("flash_mode") != "dio"
            or plan.get("flash_freq") != "40m"
        ):
            raise ValueError("flash plan settings are invalid")
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
    return plan


def read_info(port: str, duration: float = 3.0) -> DeviceInfo | None:
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
        connection.write(b"info\n")
        connection.flush()
        deadline = time.monotonic() + duration
        output = bytearray()
        while time.monotonic() < deadline:
            output.extend(connection.read(1024))
        return parse_device_info(output.decode(errors="replace"))
    finally:
        connection.close()


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


def esptool_command() -> list[str]:
    tool = Path.home() / ".platformio/packages/tool-esptoolpy/esptool.py"
    if not tool.is_file():
        raise RuntimeError("PlatformIO esptool.py is not installed")
    return [str(stable_platformio_python(Path(sys.executable))), str(tool)]


def run_tool(arguments: list[str]) -> str:
    result = subprocess.run(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(result.stdout.strip())
    return result.stdout


def probe_board(port: str) -> dict[str, str]:
    output = run_tool(esptool_command() + ["--port", port, "flash_id"])
    probe = parse_probe(output)
    if not probe:
        raise RuntimeError("device is not the expected ESP32-D0WD-V3/40MHz/4MB class")
    return probe


def erase_board(port: str) -> None:
    base = esptool_command() + ["--chip", "esp32", "--port", port, "--baud", "115200"]
    log(f"{port}: factory/unrecognized firmware; performing one-time erase")
    run_tool(base + ["erase_flash"])


def flash_board(port: str, plan: dict[str, Any], directory: Path) -> None:
    base = esptool_command() + ["--chip", "esp32", "--port", port, "--baud", "115200"]
    arguments = base + [
        "write_flash", "-z", "--flash_mode", plan["flash_mode"],
        "--flash_freq", plan["flash_freq"], "--flash_size", plan["flash_size"],
    ]
    for segment in sorted(plan["segments"], key=lambda item: item["offset"]):
        arguments += [hex(segment["offset"]), str(directory / segment["filename"])]
    run_tool(arguments)


def preserves_identity(before: DeviceInfo, after: DeviceInfo) -> bool:
    return (
        before.role == after.role
        and before.node_id == after.node_id
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
) -> str:
    devices = load_device_registry(device_registry)
    before = read_info(port)
    probe = probe_board(port)
    if before is None:
        # A valid field node may be in daytime deep sleep and unable to answer
        # until esptool's non-destructive ROM probe resets it into a cold boot.
        time.sleep(1.5)
        before = read_info(port, 5.0)
    if before and before.mac != probe["mac"]:
        raise RuntimeError("serial identity and ROM MAC disagree")
    device_state = devices.get(probe["mac"])
    if before is not None:
        write_device_state(device_registry, probe["mac"], "known")
        device_state = "known"
    elif device_state == "erase_pending":
        raise RuntimeError(
            "prior factory erase has an ambiguous result; authorize this ROM MAC before retrying"
        )
    if before is None and device_state not in {"known", "erase_authorized"} and not factory_authorized:
        raise RuntimeError(
            "device is not recognized; not flashing without explicit factory authorization"
        )
    if should_skip(before, manifest):
        return f"{probe['mac']} already runs approved build {approved_build(manifest)}"
    plan = extract_bundle(bundle, work / probe["mac"].replace(":", ""))
    erase = should_erase(
        before,
        device_state=device_state,
        factory_authorized=factory_authorized,
    )
    if erase:
        # A pending marker is intentionally ambiguous. A crash or command error
        # leaves it in place and requires explicit per-MAC operator authorization.
        write_device_state(device_registry, probe["mac"], "erase_pending")
        erase_board(port)
        write_device_state(device_registry, probe["mac"], "known")
    flash_board(port, plan, work / probe["mac"].replace(":", ""))
    time.sleep(1.5)
    after = read_info(port, 5.0)
    if not after or after.build != approved_build(manifest) or after.dirty:
        raise RuntimeError("post-flash firmware identity did not match the production release")
    if after.mac != probe["mac"]:
        raise RuntimeError("post-flash firmware and ROM MAC disagree")
    if before and not preserves_identity(before, after):
        raise RuntimeError("post-flash NVS identity/position changed")
    return f"{after.mac} flashed build {after.build}; role/position verified"


def candidate_ports() -> set[str]:
    from serial.tools import list_ports

    return {
        item.device
        for item in list_ports.comports()
        if item.vid == WCH_VID and item.pid == WCH_PID and item.device.startswith("/dev/cu.")
    }


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
