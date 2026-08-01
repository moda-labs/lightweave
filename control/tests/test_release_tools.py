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


def test_release_publisher_is_retry_safe_and_verifies_assets_before_publish() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "gh release create \"$RELEASE_TAG\"" in workflow
    assert "--draft" in workflow
    assert "gh release upload" in workflow
    assert "--clobber" in workflow
    assert "verify_asset_set" in workflow
    assert "'.assets[].name'" in workflow
    assert '--published-at "$RELEASE_PUBLISHED_AT"' in workflow
    assert "grep -q 'HTTP 404'" in workflow
    assert workflow.count("cmp \"$manifest\"") == 2
    assert workflow.count("cmp \"$firmware\"") == 2
    assert "gh release edit \"$RELEASE_TAG\" --draft=false" in workflow


def test_firmware_release_inputs_are_exactly_pinned() -> None:
    platformio = (REPO_ROOT / "platformio.ini").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "platform = espressif32 @ 7.0.1" in platformio
    assert "makuna/NeoPixelBus @ 2.8.4" in platformio
    assert "adafruit/Adafruit INA228 Library @ 3.0.0" in platformio
    assert "adafruit/Adafruit BusIO @ 1.17.4" in platformio
    assert "requirements-tooling.lock" in workflow
    assert "pip install platformio" not in workflow


def test_operations_ui_names_partial_field_coverage_as_deferred() -> None:
    app_js = (REPO_ROOT / "control" / "static" / "app.js").read_text(encoding="utf-8")

    assert "firmware.coverage_complete === true" in app_js
    assert 'firmware.identity_in_sync === true && !firmwareCoverageComplete' in app_js
    assert '? "deferred"' in app_js
