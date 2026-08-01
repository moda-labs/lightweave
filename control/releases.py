from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .ota_store import OtaArtifactError, OtaArtifactStore


SCHEMA_VERSION = 1
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReleaseMetadataError(ValueError):
    pass


@dataclass(frozen=True)
class ReleaseNotes:
    version: str
    date: str
    title: str
    control_changes: tuple[str, ...]
    firmware_changes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "date": self.date,
            "title": self.title,
            "control_changes": list(self.control_changes),
            "firmware_changes": list(self.firmware_changes),
        }


@dataclass(frozen=True)
class ReleaseManifest:
    release: str
    version: str
    repository: str
    ref: str
    commit: str
    published_at: str
    notes: ReleaseNotes
    firmware: Mapping[str, Any]
    serial_flash: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "release": self.release,
            "version": self.version,
            "repository": self.repository,
            "ref": self.ref,
            "commit": self.commit,
            "published_at": self.published_at,
            "notes": self.notes.as_dict(),
            "firmware": dict(self.firmware),
            "serial_flash": dict(self.serial_flash),
        }


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseMetadataError(f"{name} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ReleaseMetadataError(f"{name} keys invalid: missing={missing} extra={extra}")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ReleaseMetadataError(f"{name} must be a non-empty trimmed string")
    return value


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ReleaseMetadataError(f"{name} must be an array")
    items = tuple(_string(item, f"{name} item") for item in value)
    if len(set(items)) != len(items):
        raise ReleaseMetadataError(f"{name} contains duplicates")
    return items


def _https_url(value: Any, name: str) -> str:
    url = _string(value, name)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ReleaseMetadataError(f"{name} must be an HTTPS URL without credentials or fragment")
    return url


def canonical_release_asset_url(repository: str, release: str, filename: str) -> str:
    repository_url = _https_url(repository, "release repository")
    parsed = urlsplit(repository_url)
    path_parts = parsed.path.removeprefix("/").split("/")
    if (
        parsed.hostname != "github.com"
        or parsed.port is not None
        or parsed.query
        or len(path_parts) != 2
        or not path_parts[0]
        or not path_parts[1].endswith(".git")
        or not path_parts[1].removesuffix(".git")
    ):
        raise ReleaseMetadataError("release repository must be a canonical GitHub HTTPS Git URL")
    owner, repository_name = path_parts[0], path_parts[1].removesuffix(".git")
    return (
        f"https://github.com/{owner}/{repository_name}/releases/download/"
        f"{release}/{filename}"
    )


def release_from_manifest_url(url: str, repository: str) -> str:
    manifest_url = _https_url(url, "release channel.manifest_url")
    placeholder = canonical_release_asset_url(
        repository, "v0.0.0", "lightweave-release.json"
    )
    repository_base = placeholder.removesuffix("/v0.0.0/lightweave-release.json")
    prefix = f"{repository_base}/"
    if not manifest_url.startswith(prefix):
        raise ReleaseMetadataError("release channel manifest URL is not canonical")
    suffix = manifest_url.removeprefix(prefix)
    parts = suffix.split("/")
    if len(parts) != 2 or parts[1] != "lightweave-release.json":
        raise ReleaseMetadataError("release channel manifest URL is not canonical")
    release = parts[0]
    if not release.startswith("v") or not SEMVER_RE.fullmatch(release[1:]):
        raise ReleaseMetadataError("release channel manifest URL is not canonical")
    if manifest_url != canonical_release_asset_url(repository, release, parts[1]):
        raise ReleaseMetadataError("release channel manifest URL is not canonical")
    return release


def _iso_timestamp(value: Any, name: str) -> str:
    timestamp = _string(value, name)
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseMetadataError(f"{name} must be an ISO-8601 timestamp") from error
    return timestamp


def parse_release_notes(value: Any, name: str = "release notes") -> ReleaseNotes:
    item = _object(value, name)
    _exact_keys(
        item,
        {"version", "date", "title", "control_changes", "firmware_changes"},
        name,
    )
    version = _string(item["version"], f"{name}.version")
    if not SEMVER_RE.fullmatch(version):
        raise ReleaseMetadataError(f"{name}.version must be semantic x.y.z")
    date = _string(item["date"], f"{name}.date")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as error:
        raise ReleaseMetadataError(f"{name}.date must be YYYY-MM-DD") from error
    return ReleaseNotes(
        version=version,
        date=date,
        title=_string(item["title"], f"{name}.title"),
        control_changes=_string_list(item["control_changes"], f"{name}.control_changes"),
        firmware_changes=_string_list(item["firmware_changes"], f"{name}.firmware_changes"),
    )


def load_release_catalog(path: Path | str) -> tuple[ReleaseNotes, ...]:
    source = Path(path)
    try:
        document = _object(json.loads(source.read_text(encoding="utf-8")), "release catalog")
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseMetadataError(f"cannot read release catalog: {error}") from error
    _exact_keys(document, {"schema_version", "releases"}, "release catalog")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ReleaseMetadataError("unsupported release catalog schema")
    if not isinstance(document["releases"], list):
        raise ReleaseMetadataError("release catalog.releases must be an array")
    releases = tuple(
        parse_release_notes(item, f"release catalog.releases[{index}]")
        for index, item in enumerate(document["releases"])
    )
    versions = [item.version for item in releases]
    if len(set(versions)) != len(versions):
        raise ReleaseMetadataError("release catalog contains duplicate versions")
    dates = [item.date for item in releases]
    if dates != sorted(dates, reverse=True):
        raise ReleaseMetadataError("release catalog must be newest first")
    return releases


def parse_release_manifest(value: Any) -> ReleaseManifest:
    manifest = _object(value, "release manifest")
    _exact_keys(
        manifest,
        {
            "schema_version",
            "release",
            "version",
            "repository",
            "ref",
            "commit",
            "published_at",
            "notes",
            "firmware",
            "serial_flash",
        },
        "release manifest",
    )
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ReleaseMetadataError("unsupported release manifest schema")
    version = _string(manifest["version"], "release manifest.version")
    if not SEMVER_RE.fullmatch(version):
        raise ReleaseMetadataError("release manifest.version must be semantic x.y.z")
    release = _string(manifest["release"], "release manifest.release")
    if release != f"v{version}":
        raise ReleaseMetadataError("release manifest.release must equal v<version>")
    ref = _string(manifest["ref"], "release manifest.ref")
    if ref != f"refs/tags/{release}":
        raise ReleaseMetadataError("release manifest.ref must name its immutable tag")
    repository = _https_url(manifest["repository"], "release manifest.repository")
    canonical_release_asset_url(repository, release, "lightweave-release.json")
    commit = _string(manifest["commit"], "release manifest.commit").lower()
    if not COMMIT_RE.fullmatch(commit):
        raise ReleaseMetadataError("release manifest.commit must be a full Git SHA")
    notes = parse_release_notes(manifest["notes"], "release manifest.notes")
    if notes.version != version:
        raise ReleaseMetadataError("release manifest notes version mismatch")
    firmware = _object(manifest["firmware"], "release manifest.firmware")
    _exact_keys(firmware, {"filename", "url", "sha256", "size", "crc32"}, "release manifest.firmware")
    filename = _string(firmware["filename"], "release manifest.firmware.filename")
    expected_filename = f"lightweave-field-{release}.bin"
    if filename != expected_filename:
        raise ReleaseMetadataError("firmware filename is not canonical for this release")
    firmware_url = _https_url(firmware["url"], "release manifest.firmware.url")
    if firmware_url != canonical_release_asset_url(repository, release, filename):
        raise ReleaseMetadataError("firmware URL is not canonical for this release")
    sha256 = _string(firmware["sha256"], "release manifest.firmware.sha256").lower()
    if not SHA256_RE.fullmatch(sha256):
        raise ReleaseMetadataError("firmware sha256 must be 64 lowercase hex characters")
    size = firmware["size"]
    crc32 = firmware["crc32"]
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ReleaseMetadataError("firmware size must be a positive integer")
    if not isinstance(crc32, int) or isinstance(crc32, bool) or not 0 <= crc32 <= 0xFFFFFFFF:
        raise ReleaseMetadataError("firmware crc32 must be an unsigned 32-bit integer")
    serial_flash = _object(manifest["serial_flash"], "release manifest.serial_flash")
    _exact_keys(
        serial_flash,
        {"filename", "url", "sha256", "size"},
        "release manifest.serial_flash",
    )
    serial_filename = _string(
        serial_flash["filename"], "release manifest.serial_flash.filename"
    )
    expected_serial_filename = f"lightweave-serial-flash-{release}.zip"
    if serial_filename != expected_serial_filename:
        raise ReleaseMetadataError("serial flash filename is not canonical for this release")
    serial_url = _https_url(serial_flash["url"], "release manifest.serial_flash.url")
    if serial_url != canonical_release_asset_url(repository, release, serial_filename):
        raise ReleaseMetadataError("serial flash URL is not canonical for this release")
    serial_sha256 = _string(
        serial_flash["sha256"], "release manifest.serial_flash.sha256"
    ).lower()
    if not SHA256_RE.fullmatch(serial_sha256):
        raise ReleaseMetadataError("serial flash sha256 must be 64 lowercase hex characters")
    serial_size = serial_flash["size"]
    if not isinstance(serial_size, int) or isinstance(serial_size, bool) or serial_size <= 0:
        raise ReleaseMetadataError("serial flash size must be a positive integer")
    return ReleaseManifest(
        release=release,
        version=version,
        repository=repository,
        ref=ref,
        commit=commit,
        published_at=_iso_timestamp(manifest["published_at"], "release manifest.published_at"),
        notes=notes,
        firmware={
            "filename": filename,
            "url": firmware_url,
            "sha256": sha256,
            "size": size,
            "crc32": crc32,
        },
        serial_flash={
            "filename": serial_filename,
            "url": serial_url,
            "sha256": serial_sha256,
            "size": serial_size,
        },
    )


def parse_release_channel(value: Any) -> dict[str, Any]:
    channel = _object(value, "release channel")
    _exact_keys(channel, {"schema_version", "enabled", "manifest_url", "manifest_sha256"}, "release channel")
    if channel["schema_version"] != SCHEMA_VERSION:
        raise ReleaseMetadataError("unsupported release channel schema")
    if not isinstance(channel["enabled"], bool):
        raise ReleaseMetadataError("release channel.enabled must be boolean")
    if not channel["enabled"]:
        if channel["manifest_url"] is not None or channel["manifest_sha256"] is not None:
            raise ReleaseMetadataError("disabled release channel must not name a manifest")
        return dict(channel)
    url = _https_url(channel["manifest_url"], "release channel.manifest_url")
    sha256 = _string(channel["manifest_sha256"], "release channel.manifest_sha256").lower()
    if not SHA256_RE.fullmatch(sha256):
        raise ReleaseMetadataError("release channel.manifest_sha256 must be 64 lowercase hex characters")
    return {
        "schema_version": SCHEMA_VERSION,
        "enabled": True,
        "manifest_url": url,
        "manifest_sha256": sha256,
    }


def parse_deployment_record(value: Any) -> dict[str, Any]:
    record = _object(value, "deployment record")
    _exact_keys(
        record,
        {"schema_version", "deployed_at", "previous_commit", "backup", "firmware_local_path", "manifest"},
        "deployment record",
    )
    if record["schema_version"] != SCHEMA_VERSION:
        raise ReleaseMetadataError("unsupported deployment record schema")
    manifest = parse_release_manifest(record["manifest"])
    local_path = Path(_string(record["firmware_local_path"], "deployment record.firmware_local_path"))
    if not local_path.is_absolute():
        raise ReleaseMetadataError("deployment record.firmware_local_path must be absolute")
    previous_commit = _string(record["previous_commit"], "deployment record.previous_commit").lower()
    if not COMMIT_RE.fullmatch(previous_commit):
        raise ReleaseMetadataError("deployment record.previous_commit must be a full Git SHA")
    return {
        "schema_version": SCHEMA_VERSION,
        "deployed_at": _iso_timestamp(record["deployed_at"], "deployment record.deployed_at"),
        "previous_commit": previous_commit,
        "backup": _string(record["backup"], "deployment record.backup"),
        "firmware_local_path": str(local_path),
        "manifest": manifest.as_dict(),
    }


def load_deployment_record(path: Path | str) -> dict[str, Any] | None:
    source = Path(path)
    if not source.exists():
        return None
    try:
        return parse_deployment_record(json.loads(source.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseMetadataError(f"cannot read deployment record: {error}") from error


def stage_deployment_firmware(ota_store: OtaArtifactStore, record: Mapping[str, Any] | None) -> None:
    if not record:
        return
    manifest = parse_release_manifest(record["manifest"])
    current = ota_store.current()
    if current and current.get("sha256") == manifest.firmware["sha256"]:
        try:
            ota_store.read_verified()
            return
        except OtaArtifactError:
            pass
    path = Path(str(record["firmware_local_path"]))
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ReleaseMetadataError(f"cannot read deployment firmware: {error}") from error
    if len(data) != manifest.firmware["size"]:
        raise ReleaseMetadataError("deployment firmware size mismatch")
    if hashlib.sha256(data).hexdigest() != manifest.firmware["sha256"]:
        raise ReleaseMetadataError("deployment firmware SHA-256 mismatch")
    staged = ota_store.stage(str(manifest.firmware["filename"]), data)
    if staged["crc32"] != manifest.firmware["crc32"]:
        raise ReleaseMetadataError("deployment firmware CRC32 mismatch")


def current_source_commit(repo_root: Path) -> str | None:
    configured = os.getenv("CONTROL_RELEASE_COMMIT", "").strip().lower()
    if COMMIT_RE.fullmatch(configured):
        return configured
    commit_file = os.getenv("CONTROL_RELEASE_COMMIT_FILE", "").strip()
    if commit_file:
        try:
            recorded = Path(commit_file).read_text(encoding="utf-8").strip().lower()
        except OSError:
            return None
        return recorded if COMMIT_RE.fullmatch(recorded) else None
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip().lower()
    except (OSError, subprocess.CalledProcessError):
        return None
    return commit if COMMIT_RE.fullmatch(commit) else None


def release_status(
    catalog: tuple[ReleaseNotes, ...],
    deployment_record: Mapping[str, Any] | None,
    state: Mapping[str, Any],
    *,
    running_version: str,
    running_commit: str | None,
) -> dict[str, Any]:
    by_version = {release.version: release for release in catalog}
    deployed_manifest = (
        parse_release_manifest(deployment_record["manifest"])
        if deployment_record
        else None
    )
    control_notes = by_version.get(running_version)
    conductor_firmware = dict(state.get("conductor", {}).get("firmware") or {})
    firmware_version = str(conductor_firmware.get("version") or "") or None
    firmware_notes = by_version.get(firmware_version or "")
    summary = dict(state.get("summary", {}).get("firmware") or {})
    desired_commit = deployed_manifest.commit if deployed_manifest else None
    firmware_build = str(conductor_firmware.get("build_label") or "").lower()
    desired_firmware = dict(deployed_manifest.firmware) if deployed_manifest else None
    matching = summary.get("matching")
    expected = summary.get("expected")
    coverage_complete = (
        isinstance(matching, int)
        and not isinstance(matching, bool)
        and isinstance(expected, int)
        and not isinstance(expected, bool)
        and expected >= 0
        and matching == expected
    )
    identity_in_sync = (
        conductor_firmware.get("dirty") is False
        and (
            desired_commit is None
            or (firmware_build and desired_commit.startswith(firmware_build))
        )
    )
    return {
        "control": {
            "version": running_version,
            "commit": running_commit,
            "deployed_at": deployment_record.get("deployed_at") if deployment_record else None,
            "desired_commit": desired_commit,
            "in_sync": desired_commit is None or running_commit == desired_commit,
            "release": control_notes.as_dict() if control_notes else None,
        },
        "firmware": {
            "version": firmware_version,
            "commit": firmware_build or None,
            "dirty": conductor_firmware.get("dirty"),
            "consistent": summary.get("consistent"),
            "matching": summary.get("matching"),
            "seen": summary.get("seen"),
            "expected": summary.get("expected"),
            "desired": desired_firmware,
            "desired_version": deployed_manifest.version if deployed_manifest else None,
            "desired_release": deployed_manifest.notes.as_dict() if deployed_manifest else None,
            "identity_in_sync": identity_in_sync,
            "coverage_complete": coverage_complete,
            "in_sync": (
                identity_in_sync
                and coverage_complete
                and summary.get("consistent") is True
            ),
            "release": firmware_notes.as_dict() if firmware_notes else None,
        },
        "history": [release.as_dict() for release in catalog],
    }
