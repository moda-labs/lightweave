import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).parents[2]
VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
TAG = f"v{VERSION}"


def load_script(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build = load_script("build_release_manifest")
promote = load_script("promote_release")


def test_build_manifest_binds_code_notes_and_firmware(tmp_path: Path) -> None:
    firmware = tmp_path / f"lightweave-field-{TAG}.bin"
    firmware.write_bytes(b"field firmware")

    document = build.build_manifest(
        firmware=firmware,
        repository="https://github.com/underminedsk/lightweave.git",
        commit="a" * 40,
        tag=TAG,
        artifact_url=(
            "https://github.com/underminedsk/lightweave/releases/download/"
            f"{TAG}/lightweave-field-{TAG}.bin"
        ),
        published_at="2026-08-01T19:00:00Z",
    )

    assert document["version"] == VERSION
    assert document["notes"]["control_changes"]
    assert document["notes"]["firmware_changes"]
    assert document["firmware"]["sha256"] == hashlib.sha256(b"field firmware").hexdigest()


def test_build_manifest_rejects_tag_version_mismatch(tmp_path: Path) -> None:
    firmware = tmp_path / "field.bin"
    firmware.write_bytes(b"field firmware")
    with pytest.raises(ValueError, match="does not match VERSION"):
        build.build_manifest(
            firmware=firmware,
            repository="https://github.com/underminedsk/lightweave.git",
            commit="a" * 40,
            tag="v9.9.9",
            artifact_url="https://github.com/underminedsk/lightweave/releases/download/v9.9.9/field.bin",
            published_at="2026-08-01T19:00:00Z",
        )


def test_promote_channel_pins_exact_manifest_bytes(tmp_path: Path) -> None:
    firmware = tmp_path / f"lightweave-field-{TAG}.bin"
    firmware.write_bytes(b"field firmware")
    document = build.build_manifest(
        firmware=firmware,
        repository="https://github.com/underminedsk/lightweave.git",
        commit="a" * 40,
        tag=TAG,
        artifact_url=(
            "https://github.com/underminedsk/lightweave/releases/download/"
            f"{TAG}/lightweave-field-{TAG}.bin"
        ),
        published_at="2026-08-01T19:00:00Z",
    )
    data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    url = (
        "https://github.com/underminedsk/lightweave/releases/download/"
        f"{TAG}/lightweave-release.json"
    )

    channel = promote.promoted_channel(data, url)

    assert channel["enabled"] is True
    assert channel["manifest_url"] == url
    assert channel["manifest_sha256"] == hashlib.sha256(data).hexdigest()


def test_promote_rejects_noncanonical_manifest_url(tmp_path: Path) -> None:
    firmware = tmp_path / f"lightweave-field-{TAG}.bin"
    firmware.write_bytes(b"field firmware")
    document = build.build_manifest(
        firmware=firmware,
        repository="https://github.com/underminedsk/lightweave.git",
        commit="a" * 40,
        tag=TAG,
        artifact_url=(
            "https://github.com/underminedsk/lightweave/releases/download/"
            f"{TAG}/lightweave-field-{TAG}.bin"
        ),
        published_at="2026-08-01T19:00:00Z",
    )
    with pytest.raises(ValueError, match="immutable Lightweave"):
        promote.promoted_channel(json.dumps(document).encode(), "https://example.com/release.json")
