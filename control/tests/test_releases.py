import hashlib
import json
from pathlib import Path
import zlib

import pytest

from control.ota_store import OtaArtifactStore
from control.releases import (
    ReleaseMetadataError,
    load_release_catalog,
    parse_deployment_record,
    parse_release_channel,
    parse_release_manifest,
    release_status,
    stage_deployment_firmware,
    stage_known_release_firmware,
    current_source_commit,
)


def notes(version: str = "0.3.0") -> dict:
    return {
        "version": version,
        "date": "2026-08-01",
        "title": "Release title",
        "control_changes": ["Control change"],
        "firmware_changes": ["Firmware change"],
    }


def manifest(data: bytes = b"firmware", version: str = "0.3.0") -> dict:
    return {
        "schema_version": 1,
        "release": f"v{version}",
        "version": version,
        "repository": "https://github.com/underminedsk/lightweave.git",
        "ref": f"refs/tags/v{version}",
        "commit": "a" * 40,
        "published_at": "2026-08-01T18:00:00Z",
        "notes": notes(version),
        "firmware": {
            "filename": f"lightweave-field-v{version}.bin",
            "url": f"https://github.com/underminedsk/lightweave/releases/download/v{version}/lightweave-field-v{version}.bin",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "crc32": zlib.crc32(data) & 0xFFFFFFFF,
        },
        "serial_flash": {
            "filename": f"lightweave-serial-flash-v{version}.zip",
            "url": f"https://github.com/underminedsk/lightweave/releases/download/v{version}/lightweave-serial-flash-v{version}.zip",
            "sha256": hashlib.sha256(b"serial bundle").hexdigest(),
            "size": len(b"serial bundle"),
        },
    }


def deployment_record(tmp_path: Path, data: bytes = b"firmware") -> dict:
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(data)
    return {
        "schema_version": 1,
        "deployed_at": "2026-08-01T18:30:00Z",
        "previous_commit": "b" * 40,
        "backup": "/var/backups/lightweave/pre-upgrade.tgz",
        "firmware_local_path": str(firmware),
        "manifest": manifest(data),
    }


def test_repository_release_catalog_is_valid_and_newest_first() -> None:
    repo_root = Path(__file__).parents[2]
    catalog = load_release_catalog(repo_root / "RELEASES.json")

    assert catalog[0].version == (repo_root / "VERSION").read_text(encoding="utf-8").strip()
    assert "0.3.0" in {release.version for release in catalog}
    # A release may touch only one side. RELEASING.md calls for an empty list on
    # the side that did not change, so require notes overall rather than both.
    assert catalog[0].control_changes or catalog[0].firmware_changes


def test_known_release_download_is_verified_and_staged(tmp_path: Path) -> None:
    data = b"published firmware"
    published = manifest(data, "0.7.1")
    published["repository"] = "https://github.com/moda-labs/lightweave.git"
    published["firmware"]["url"] = (
        "https://github.com/moda-labs/lightweave/releases/download/"
        "v0.7.1/lightweave-field-v0.7.1.bin"
    )
    published["serial_flash"]["url"] = (
        "https://github.com/moda-labs/lightweave/releases/download/"
        "v0.7.1/lightweave-serial-flash-v0.7.1.zip"
    )

    def download(url: str, _limit: int) -> bytes:
        return json.dumps(published).encode() if url.endswith("lightweave-release.json") else data

    store = OtaArtifactStore(tmp_path / "ota")
    result = stage_known_release_firmware("0.7.1", store, download=download)

    assert result["artifact"]["source"] == "release"
    assert result["artifact"]["version"] == "0.7.1"
    assert result["artifact"]["commit"] == "a" * 40
    assert store.read_verified() == data


def test_release_manifest_requires_immutable_tag_commit_and_hashed_firmware() -> None:
    parsed = parse_release_manifest(manifest())

    assert parsed.release == "v0.3.0"
    assert parsed.ref == "refs/tags/v0.3.0"
    assert parsed.commit == "a" * 40
    assert parsed.firmware["size"] == 8
    assert parsed.serial_flash["filename"].endswith(".zip")

    bad = manifest()
    bad["ref"] = "refs/heads/main"
    with pytest.raises(ReleaseMetadataError, match="immutable tag"):
        parse_release_manifest(bad)

    bad = manifest()
    bad["firmware"]["url"] = "https://example.com/lightweave-field-v0.3.0.bin"
    with pytest.raises(ReleaseMetadataError, match="firmware URL is not canonical"):
        parse_release_manifest(bad)

    bad = manifest()
    bad["serial_flash"]["filename"] = "serial.zip"
    with pytest.raises(ReleaseMetadataError, match="serial flash filename is not canonical"):
        parse_release_manifest(bad)


def test_release_channel_is_disabled_or_points_to_one_hashed_https_manifest() -> None:
    disabled = {
        "schema_version": 1,
        "enabled": False,
        "manifest_url": None,
        "manifest_sha256": None,
    }
    assert parse_release_channel(disabled)["enabled"] is False

    enabled = {
        "schema_version": 1,
        "enabled": True,
        "manifest_url": "https://github.com/example/release.json",
        "manifest_sha256": "c" * 64,
    }
    assert parse_release_channel(enabled)["manifest_sha256"] == "c" * 64

    enabled["manifest_url"] = "http://example.com/release.json"
    with pytest.raises(ReleaseMetadataError, match="HTTPS"):
        parse_release_channel(enabled)


def test_deployment_record_rejects_relative_firmware_path(tmp_path: Path) -> None:
    record = deployment_record(tmp_path)
    record["firmware_local_path"] = "firmware.bin"

    with pytest.raises(ReleaseMetadataError, match="must be absolute"):
        parse_deployment_record(record)


def test_deployment_firmware_is_verified_and_staged_idempotently(tmp_path: Path) -> None:
    data = b"firmware"
    record = deployment_record(tmp_path, data)
    store = OtaArtifactStore(tmp_path / "ota")

    stage_deployment_firmware(store, record)
    uploaded_at = store.current()["uploaded_at"]
    stage_deployment_firmware(store, record)

    assert store.current()["sha256"] == hashlib.sha256(data).hexdigest()
    assert store.current()["uploaded_at"] == uploaded_at


def test_corrupt_cached_firmware_is_reverified_and_restaged(tmp_path: Path) -> None:
    data = b"firmware"
    record = deployment_record(tmp_path, data)
    store = OtaArtifactStore(tmp_path / "ota")
    stage_deployment_firmware(store, record)
    artifact = store.artifact()
    assert artifact is not None
    artifact.path.write_bytes(b"tampered")

    stage_deployment_firmware(store, record)

    assert store.read_verified() == data


def test_deployment_firmware_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    record = deployment_record(tmp_path)
    Path(record["firmware_local_path"]).write_bytes(b"tampered")

    with pytest.raises(ReleaseMetadataError, match="size mismatch|SHA-256 mismatch"):
        stage_deployment_firmware(OtaArtifactStore(tmp_path / "ota"), record)


def test_release_status_separates_running_control_and_observed_firmware(tmp_path: Path) -> None:
    catalog_path = tmp_path / "RELEASES.json"
    catalog_path.write_text(
        json.dumps({"schema_version": 1, "releases": [notes()]}),
        encoding="utf-8",
    )
    catalog = load_release_catalog(catalog_path)
    record = deployment_record(tmp_path)
    state = {
        "conductor": {
            "firmware": {
                "version": "0.3.0",
                "build_label": "aaaaaaaa",
                "dirty": False,
            }
        },
        "summary": {
            "firmware": {"consistent": True, "matching": 8, "seen": 8, "expected": 9}
        },
    }

    status = release_status(
        catalog,
        record,
        state,
        running_version="0.3.0",
        running_commit="a" * 40,
    )

    assert status["control"]["in_sync"] is True
    assert status["control"]["release"]["control_changes"] == ["Control change"]
    assert status["firmware"]["identity_in_sync"] is True
    assert status["firmware"]["coverage_complete"] is False
    assert status["firmware"]["in_sync"] is False
    assert status["firmware"]["matching"] == 8
    assert status["firmware"]["expected"] == 9
    assert status["firmware"]["release"]["firmware_changes"] == ["Firmware change"]


def test_release_status_exposes_pending_firmware_notes_separately(tmp_path: Path) -> None:
    catalog_path = tmp_path / "RELEASES.json"
    catalog_path.write_text(
        json.dumps({"schema_version": 1, "releases": [notes("0.4.0"), notes("0.3.0")]}),
        encoding="utf-8",
    )
    record = deployment_record(tmp_path)
    record["manifest"] = manifest(version="0.4.0")
    state = {
        "conductor": {"firmware": {"version": "0.3.0", "build_label": "bbbbbbbb", "dirty": False}},
        "summary": {"firmware": {"consistent": True, "matching": 1, "seen": 1, "expected": 4}},
    }

    status = release_status(
        load_release_catalog(catalog_path),
        record,
        state,
        running_version="0.4.0",
        running_commit="a" * 40,
    )

    assert status["control"]["in_sync"] is True
    assert status["firmware"]["version"] == "0.3.0"
    assert status["firmware"]["release"]["version"] == "0.3.0"
    assert status["firmware"]["desired_version"] == "0.4.0"
    assert status["firmware"]["desired_release"]["version"] == "0.4.0"
    assert status["firmware"]["in_sync"] is False


def test_dirty_firmware_is_never_reported_in_sync(tmp_path: Path) -> None:
    catalog_path = tmp_path / "RELEASES.json"
    catalog_path.write_text(
        json.dumps({"schema_version": 1, "releases": [notes()]}),
        encoding="utf-8",
    )
    state = {
        "conductor": {"firmware": {"version": "0.3.0", "build_label": "aaaaaaaa", "dirty": True}},
        "summary": {"firmware": {"consistent": True, "matching": 1, "seen": 1, "expected": 1}},
    }

    status = release_status(
        load_release_catalog(catalog_path),
        None,
        state,
        running_version="0.3.0",
        running_commit="a" * 40,
    )

    assert status["firmware"]["dirty"] is True
    assert status["firmware"]["in_sync"] is False


def test_running_commit_marker_avoids_git_ownership_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "running-commit"
    marker.write_text("a" * 40 + "\n", encoding="utf-8")
    monkeypatch.delenv("CONTROL_RELEASE_COMMIT", raising=False)
    monkeypatch.setenv("CONTROL_RELEASE_COMMIT_FILE", str(marker))
    monkeypatch.setattr(
        "control.releases.subprocess.check_output",
        lambda *_args, **_kwargs: pytest.fail("git must not run"),
    )

    assert current_source_commit(tmp_path / "root-owned-checkout") == "a" * 40

    marker.write_text("not-a-commit\n", encoding="utf-8")
    assert current_source_commit(tmp_path / "root-owned-checkout") is None
