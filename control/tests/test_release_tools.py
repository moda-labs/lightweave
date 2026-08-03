import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


REPO_ROOT = Path(__file__).parents[2]
VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
TAG = f"v{VERSION}"
PUBLISHER = REPO_ROOT / "scripts" / "publish_release.sh"

FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import sys

args = sys.argv[1:]
state_path = Path(os.environ["FAKE_GH_STATE"])
remote = Path(os.environ["FAKE_GH_REMOTE"])
mode = os.environ["FAKE_GH_MODE"]
state = json.loads(state_path.read_text())
state["calls"].append(args)

def save():
    state_path.write_text(json.dumps(state))

def assets():
    names = sorted(path.name for path in remote.iterdir() if path.is_file())
    if mode == "asset_mismatch":
        names = [name for name in names if not name.endswith(".zip")]
    print("\n".join(names))

if args[0] == "api":
    target = next(item for item in args if item.startswith("repos/"))
    query = args[args.index("--jq") + 1]
    if "/releases/tags/" in target and query == ".draft":
        save()
        if mode == "published":
            print("false")
            raise SystemExit(0)
        if mode == "api_error":
            print("gh: server failed (HTTP 500)", file=sys.stderr)
            raise SystemExit(1)
        print("gh: Not Found (HTTP 404)", file=sys.stderr)
        raise SystemExit(1)
    if target.endswith("/releases?per_page=100"):
        state["list_calls"] += 1
        save()
        if mode == "multiple":
            print("42\n43")
        elif mode == "delayed":
            if state["list_calls"] > 3:
                print("42")
        elif mode == "never_visible":
            pass
        elif mode != "missing" or state["list_calls"] > 1:
            print("42")
        raise SystemExit(0)
    save()
    assets()
    raise SystemExit(0)

if args[:2] == ["release", "create"]:
    state["create_calls"] += 1
    save()
    print("https://github.com/example/releases/tag/untagged-test")
    raise SystemExit(0)

if args[:2] == ["release", "upload"]:
    state["upload_calls"] += 1
    for source in args[3:args.index("--clobber")]:
        shutil.copy2(source, remote / Path(source).name)
    save()
    raise SystemExit(0)

if args[:2] == ["release", "download"]:
    state["download_calls"] += 1
    destination = Path(args[args.index("--dir") + 1])
    destination.mkdir(parents=True, exist_ok=True)
    for source in remote.iterdir():
        if source.is_file():
            shutil.copy2(source, destination / source.name)
    save()
    raise SystemExit(0)

if args[:2] == ["release", "edit"]:
    state["edit_calls"] += 1
    save()
    raise SystemExit(0)

save()
raise SystemExit(f"unexpected gh invocation: {args}")
'''


def load_script(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_fake_publisher(tmp_path: Path, mode: str, *, partial: bool = False):
    dist = tmp_path / "dist"
    dist.mkdir()
    assets = {
        "lightweave-release.json": b'{"release":"v0.4.0"}\n',
        f"lightweave-field-{TAG}.bin": b"firmware",
        f"lightweave-serial-flash-{TAG}.zip": b"serial bundle",
    }
    for name, data in assets.items():
        (dist / name).write_bytes(data)
    remote = tmp_path / "remote"
    remote.mkdir()
    if mode == "published":
        for name, data in assets.items():
            (remote / name).write_bytes(data)
    elif partial:
        (remote / "lightweave-release.json").write_bytes(b"interrupted")
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "calls": [],
                "list_calls": 0,
                "create_calls": 0,
                "upload_calls": 0,
                "download_calls": 0,
                "edit_calls": 0,
            }
        )
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(0o755)
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "GITHUB_REPOSITORY": "underminedsk/lightweave",
            "RELEASE_TAG": TAG,
            "FAKE_GH_MODE": mode,
            "FAKE_GH_STATE": str(state_path),
            "FAKE_GH_REMOTE": str(remote),
            "RELEASE_DRAFT_LOOKUP_DELAY_S": "0",
        }
    )
    result = subprocess.run(
        [str(PUBLISHER), str(dist)],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result, json.loads(state_path.read_text()), remote, assets


build = load_script("build_release_manifest")
bundle = load_script("build_serial_flash_bundle")
autoflash = load_script("firebeetle_autoflash")
promote = load_script("promote_release")


def autoflash_manifest(bundle_data: bytes) -> dict:
    filename = f"lightweave-serial-flash-{TAG}.zip"
    return {
        "version": VERSION,
        "release": TAG,
        "repository": "https://github.com/underminedsk/lightweave.git",
        "ref": f"refs/tags/{TAG}",
        "commit": "a" * 40,
        "serial_flash": {
            "filename": filename,
            "url": (
                "https://github.com/underminedsk/lightweave/releases/download/"
                f"{TAG}/{filename}"
            ),
            "size": len(bundle_data),
            "sha256": hashlib.sha256(bundle_data).hexdigest(),
        },
    }


def test_serial_flash_bundle_is_deterministic_and_self_verifying(tmp_path: Path) -> None:
    inputs = {}
    for _offset, filename in bundle.SEGMENTS:
        path = tmp_path / filename
        path.write_bytes(filename.encode())
        inputs[filename] = path
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    bundle.build_bundle(inputs=inputs, output=first)
    bundle.build_bundle(inputs=inputs, output=second)

    assert first.read_bytes() == second.read_bytes()
    plan = autoflash.extract_bundle(first, tmp_path / "extracted")
    assert {item["offset"] for item in plan["segments"]} == set(autoflash.EXPECTED_SEGMENTS)


def test_autoflash_rejects_compressed_serial_bundle(tmp_path: Path) -> None:
    archive = tmp_path / "compressed.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("flash-plan.json", "{}")
        for filename in autoflash.EXPECTED_SEGMENTS.values():
            output.writestr(filename, filename * 100)

    with pytest.raises(ValueError, match="stored members"):
        autoflash.extract_bundle(archive, tmp_path / "extracted")


def test_autoflash_parses_lightweave_identity_and_expected_rom_probe() -> None:
    info = autoflash.parse_device_info(
        "role=PERFORMER  id=4  mac=C0:CD:D6:C8:03:E0  x=0.30  y=0.45\n"
        "firmware: v0.4.0  proto=7  build=abcdef12"
    )
    probe = autoflash.parse_probe(
        "Chip is ESP32-D0WD-V3 (revision v3.1)\nCrystal is 40MHz\n"
        "MAC: c0:cd:d6:c8:03:e0\nDetected flash size: 4MB\n"
    )

    assert info and info.node_id == 4 and info.build == "abcdef12" and not info.dirty
    assert probe and probe["mac"] == "C0:CD:D6:C8:03:E0"


def test_autoflash_erases_only_unrecognized_firmware_and_skips_approved_clean_build() -> None:
    manifest = {"commit": "abcdef12" + "0" * 32}
    current = autoflash.DeviceInfo(
        "PERFORMER", 0, "C0:CD:D6:C8:03:E0", 0.0, 0.0, "0.4.0", 7, "abcdef12", False
    )
    dirty = autoflash.DeviceInfo(
        "PERFORMER", 0, "C0:CD:D6:C8:03:E0", 0.0, 0.0, "0.4.0", 7, "abcdef12", True
    )

    assert autoflash.should_erase(None, device_state=None, factory_authorized=False) is False
    assert autoflash.should_erase(None, device_state=None, factory_authorized=True) is True
    assert autoflash.should_erase(None, device_state="known", factory_authorized=True) is False
    assert autoflash.should_erase(
        None, device_state="erase_authorized", factory_authorized=False
    ) is True
    assert autoflash.should_erase(
        current, device_state=None, factory_authorized=True
    ) is False
    assert autoflash.should_skip(current, manifest) is True
    assert autoflash.should_skip(dirty, manifest) is False


def test_autoflash_allows_id_rehydration_but_not_role_or_position_drift() -> None:
    before = autoflash.DeviceInfo(
        "PERFORMER", 0, "C0:CD:D6:C8:03:E0", 0.3, 0.45, "0.3.0", 7, "b" * 8, False
    )
    rehydrated = autoflash.DeviceInfo(
        "PERFORMER", 4, before.mac, 0.3, 0.45, "0.4.0", 8, "a" * 8, False
    )
    moved = autoflash.DeviceInfo(
        "PERFORMER", 4, before.mac, 0.8, 0.45, "0.4.0", 8, "a" * 8, False
    )

    assert autoflash.preserves_role_position(before, rehydrated) is True
    assert autoflash.preserves_role_position(before, moved) is False


def test_autoflash_registry_migrates_and_allocates_unique_permanent_ids(tmp_path: Path) -> None:
    registry = tmp_path / "devices.json"
    first = "C0:CD:D6:C8:03:E0"
    second = "C0:CD:D6:C8:03:E1"
    registry.write_text(
        json.dumps({"schema_version": 1, "devices": {first: "known"}})
    )

    adopted, recorded = autoflash.ensure_node_id(registry, first, 4)
    autoflash.write_device_state(registry, second, "known")
    allocated, allocated_new = autoflash.ensure_node_id(registry, second, 0)

    assert (adopted, recorded) == (4, True)
    assert (allocated, allocated_new) == (1, True)
    assert autoflash.load_device_registry(registry) == {
        first: autoflash.DeviceRecord("known", 4),
        second: autoflash.DeviceRecord("known", 1),
    }
    document = json.loads(registry.read_text())
    assert document["schema_version"] == 2


def test_autoflash_legacy_migration_cannot_allocate_by_plug_order(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "devices.json"
    unnumbered = "C0:CD:D6:C8:03:E0"
    existing_one = "C0:CD:D6:C8:03:E1"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "devices": {unnumbered: "known", existing_one: "known"},
            }
        )
    )

    with pytest.raises(RuntimeError, match="scan 1 remaining known board"):
        autoflash.ensure_node_id(registry, unnumbered, 0)

    assert autoflash.ensure_node_id(registry, existing_one, 1) == (1, True)
    assert autoflash.ensure_node_id(registry, unnumbered, 0) == (2, True)
    assert autoflash.load_device_registry(registry) == {
        unnumbered: autoflash.DeviceRecord("known", 2),
        existing_one: autoflash.DeviceRecord("known", 1),
    }


def test_autoflash_registry_rejects_duplicate_or_changed_ids(tmp_path: Path) -> None:
    registry = tmp_path / "devices.json"
    first = "C0:CD:D6:C8:03:E0"
    second = "C0:CD:D6:C8:03:E1"
    autoflash.write_device_state(registry, first, "known")
    autoflash.ensure_node_id(registry, first, 4)
    autoflash.write_device_state(registry, second, "known")

    with pytest.raises(RuntimeError, match="already belongs"):
        autoflash.ensure_node_id(registry, second, 4)
    with pytest.raises(RuntimeError, match="permanently assigns #4"):
        autoflash.ensure_node_id(registry, first, 5)


def test_autoflash_current_board_gets_missing_id_without_reflash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac = "C0:CD:D6:C8:03:E0"
    manifest = {"commit": "a" * 40}
    current = autoflash.DeviceInfo(
        "PERFORMER", 0, mac, 0.0, 0.0, "0.4.0", 8, "a" * 8, False
    )
    assigned = autoflash.DeviceInfo(
        "PERFORMER", 1, mac, 0.0, 0.0, "0.4.0", 8, "a" * 8, False
    )
    monkeypatch.setattr(autoflash, "read_info", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(
        autoflash,
        "probe_board",
        lambda _port: {"mac": mac},
    )
    writes = []
    monkeypatch.setattr(
        autoflash,
        "write_node_id",
        lambda _port, node_id: writes.append(node_id) or assigned,
    )
    monkeypatch.setattr(
        autoflash,
        "flash_board",
        lambda *_args: pytest.fail("approved firmware must not be reflashed"),
    )
    messages = []
    monkeypatch.setattr(autoflash, "log", messages.append)

    result = autoflash.process_port(
        "/dev/cu.wchusbserial-test",
        manifest,
        tmp_path / "unused.zip",
        tmp_path / "work",
        device_registry=tmp_path / "devices.json",
        factory_authorized=False,
    )

    assert writes == [1]
    assert "permanent ID #1 verified" in result
    assert any("BOARD #1  NEW ID - LABEL THIS BOARD" in message for message in messages)


def test_autoflash_factory_board_is_numbered_after_flash_and_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mac = "C0:CD:D6:C8:03:E0"
    manifest = {"commit": "a" * 40}
    flashed = autoflash.DeviceInfo(
        "PERFORMER", 0, mac, 0.0, 0.0, "0.4.0", 8, "a" * 8, False
    )
    assigned = autoflash.DeviceInfo(
        "PERFORMER", 1, mac, 0.0, 0.0, "0.4.0", 8, "a" * 8, False
    )
    responses = iter([None, None, flashed])
    monkeypatch.setattr(
        autoflash, "read_info", lambda *_args, **_kwargs: next(responses)
    )
    monkeypatch.setattr(autoflash, "probe_board", lambda _port: {"mac": mac})
    monkeypatch.setattr(
        autoflash, "extract_bundle", lambda _bundle, _work: {"segments": []}
    )
    erased = []
    monkeypatch.setattr(autoflash, "erase_board", lambda _port: erased.append(True))
    monkeypatch.setattr(autoflash, "flash_board", lambda *_args: None)
    writes = []
    monkeypatch.setattr(
        autoflash,
        "write_node_id",
        lambda _port, node_id: writes.append(node_id) or assigned,
    )
    messages = []
    monkeypatch.setattr(autoflash, "log", messages.append)
    monkeypatch.setattr(autoflash.time, "sleep", lambda _seconds: None)
    registry = tmp_path / "devices.json"

    result = autoflash.process_port(
        "/dev/cu.wchusbserial-test",
        manifest,
        tmp_path / "bundle.zip",
        tmp_path / "work",
        device_registry=registry,
        factory_authorized=True,
    )

    assert erased == [True]
    assert writes == [1]
    assert autoflash.load_device_registry(registry) == {
        mac: autoflash.DeviceRecord("known", 1)
    }
    assert "permanent ID #1 verified" in result
    assert any("BOARD #1  NEW ID - LABEL THIS BOARD" in message for message in messages)


@pytest.mark.parametrize(
    ("wakes_after_probe", "factory_authorized", "expected_erase"),
    [(True, False, False), (False, True, True)],
)
def test_autoflash_retries_identity_after_rom_reset_before_deciding_to_erase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wakes_after_probe: bool,
    factory_authorized: bool,
    expected_erase: bool,
) -> None:
    manifest = {"commit": "a" * 40}
    prior = autoflash.DeviceInfo(
        "PERFORMER", 4, "C0:CD:D6:C8:03:E0", 0.3, 0.45, "0.3.0", 7, "b" * 8, False
    )
    flashed = autoflash.DeviceInfo(
        "PERFORMER", 4, "C0:CD:D6:C8:03:E0", 0.3, 0.45, "0.4.0", 7, "a" * 8, False
    )
    responses = iter([None, prior if wakes_after_probe else None, flashed])
    erase_calls = []
    messages = []

    monkeypatch.setattr(autoflash, "read_info", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(
        autoflash,
        "probe_board",
        lambda _port: {"mac": "C0:CD:D6:C8:03:E0"},
    )
    monkeypatch.setattr(autoflash, "extract_bundle", lambda _bundle, _work: {"segments": []})
    monkeypatch.setattr(autoflash, "erase_board", lambda _port: erase_calls.append(True))
    monkeypatch.setattr(autoflash, "flash_board", lambda _port, _plan, _work: None)
    monkeypatch.setattr(autoflash, "log", messages.append)
    monkeypatch.setattr(autoflash.time, "sleep", lambda _seconds: None)

    result = autoflash.process_port(
        "/dev/cu.usbserial-test",
        manifest,
        tmp_path / "bundle.zip",
        tmp_path / "work",
        device_registry=tmp_path / "devices.json",
        factory_authorized=factory_authorized,
    )

    assert erase_calls == ([True] if expected_erase else [])
    assert messages[0] == (
        "/dev/cu.usbserial-test: starting flash of C0:CD:D6:C8:03:E0 "
        "to production build aaaaaaaa"
    )
    assert "role/position verified" in result


def test_autoflash_unrecognized_board_fails_closed_without_factory_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(autoflash, "read_info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        autoflash,
        "probe_board",
        lambda _port: {"mac": "C0:CD:D6:C8:03:E0"},
    )
    monkeypatch.setattr(autoflash.time, "sleep", lambda _seconds: None)
    flashed = []
    monkeypatch.setattr(autoflash, "flash_board", lambda *_args, **_kwargs: flashed.append(True))

    with pytest.raises(RuntimeError, match="explicit factory authorization"):
        autoflash.process_port(
            "/dev/cu.usbserial-test",
            {"commit": "a" * 40},
            tmp_path / "bundle.zip",
            tmp_path / "work",
            device_registry=tmp_path / "devices.json",
            factory_authorized=False,
        )

    assert flashed == []
    assert not (tmp_path / "devices.json").exists()


def test_autoflash_does_not_record_factory_device_before_bundle_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "devices.json"
    monkeypatch.setattr(autoflash, "read_info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        autoflash,
        "probe_board",
        lambda _port: {"mac": "C0:CD:D6:C8:03:E0"},
    )
    monkeypatch.setattr(autoflash.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        autoflash,
        "extract_bundle",
        lambda _bundle, _work: (_ for _ in ()).throw(ValueError("bad bundle")),
    )

    with pytest.raises(ValueError, match="bad bundle"):
        autoflash.process_port(
            "/dev/cu.usbserial-test",
            {"commit": "a" * 40},
            tmp_path / "bundle.zip",
            tmp_path / "work",
            device_registry=registry,
            factory_authorized=True,
        )

    assert not registry.exists()


def test_autoflash_ambiguous_factory_erase_fails_closed_until_per_mac_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "devices.json"
    mac = "C0:CD:D6:C8:03:E0"
    homebrew = tmp_path / "homebrew"
    versioned_python = homebrew / "Cellar/platformio/6.1.19/libexec/bin/python"
    versioned_python.parent.mkdir(parents=True)
    versioned_python.write_text("")
    stable_python = homebrew / "opt/platformio/libexec/bin/python"
    stable_python.parent.mkdir(parents=True)
    stable_python.symlink_to(versioned_python)
    monkeypatch.setattr(autoflash.sys, "executable", str(versioned_python))
    monkeypatch.setattr(autoflash, "read_info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(autoflash, "probe_board", lambda _port: {"mac": mac})
    monkeypatch.setattr(autoflash.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(autoflash, "extract_bundle", lambda _bundle, _work: {"segments": []})
    flash_calls = []
    monkeypatch.setattr(autoflash, "flash_board", lambda *_args: flash_calls.append(True))
    erase_calls = []

    def fail_erase(_port):
        erase_calls.append("failed")
        raise RuntimeError("simulated erase failure")

    monkeypatch.setattr(autoflash, "erase_board", fail_erase)
    call = lambda: autoflash.process_port(
        "/dev/cu.usbserial-test",
        {"commit": "a" * 40},
        tmp_path / "bundle.zip",
        tmp_path / "work",
        device_registry=registry,
        factory_authorized=True,
    )

    with pytest.raises(RuntimeError, match="erase failure"):
        call()
    assert autoflash.load_device_registry(registry) == {
        mac: autoflash.DeviceRecord("erase_pending", None)
    }
    with pytest.raises(RuntimeError) as error:
        call()
    assert f"ambiguous result for {mac}" in str(error.value)
    retry_command = autoflash.shlex.join(
        [
            str(stable_python),
            str(Path(autoflash.__file__).resolve()),
            "retry-factory",
            mac,
        ]
    )
    assert f"run: {retry_command}" in str(error.value)
    assert erase_calls == ["failed"]
    assert flash_calls == []

    autoflash.authorize_factory_retry(registry, mac)
    monkeypatch.setattr(autoflash, "erase_board", lambda _port: erase_calls.append("succeeded"))
    monkeypatch.setattr(
        autoflash,
        "flash_board",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("simulated write failure")),
    )
    with pytest.raises(RuntimeError, match="write failure"):
        call()
    assert autoflash.load_device_registry(registry) == {
        mac: autoflash.DeviceRecord("known", None)
    }

    with pytest.raises(RuntimeError, match="write failure"):
        call()
    assert erase_calls == ["failed", "succeeded"]


def test_autoflash_reuses_only_hash_verified_cached_bundle(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    release_dir = cache / TAG
    release_dir.mkdir(parents=True)
    bundle_data = b"serial bundle"
    bundle_path = release_dir / f"lightweave-serial-flash-{TAG}.zip"
    bundle_path.write_bytes(bundle_data)
    manifest = autoflash_manifest(bundle_data)
    (release_dir / "lightweave-release.json").write_text(json.dumps(manifest))
    (cache / "current.json").write_text(json.dumps({"release": TAG}))

    loaded, loaded_path = autoflash.load_cached_artifact(cache)
    assert loaded["commit"] == "a" * 40
    assert loaded_path == bundle_path

    bundle_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        autoflash.load_cached_artifact(cache)

    (cache / "current.json").write_text(json.dumps({"release": "../../escape"}))
    with pytest.raises(ValueError, match="pointer release"):
        autoflash.load_cached_artifact(cache)


def test_autoflash_refreshes_only_canonical_hash_pinned_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel_url = (
        "https://raw.githubusercontent.com/underminedsk/lightweave/"
        "main/deploy/channels/production.json"
    )
    manifest_url = (
        "https://github.com/underminedsk/lightweave/releases/download/"
        f"{TAG}/lightweave-release.json"
    )
    bundle_data = b"serial bundle"
    manifest = autoflash_manifest(bundle_data)
    manifest_data = json.dumps(manifest).encode()
    channel_data = json.dumps(
        {
            "schema_version": 1,
            "enabled": True,
            "manifest_url": manifest_url,
            "manifest_sha256": hashlib.sha256(manifest_data).hexdigest(),
        }
    ).encode()
    downloads = {
        channel_url: channel_data,
        manifest_url: manifest_data,
        manifest["serial_flash"]["url"]: bundle_data,
    }
    requested = []

    def download(url, _limit):
        requested.append(url)
        return downloads[url]

    monkeypatch.setattr(autoflash, "_download", download)

    loaded, bundle_path = autoflash.refresh_artifact(channel_url, tmp_path / "cache")

    assert loaded["commit"] == "a" * 40
    assert bundle_path.read_bytes() == bundle_data
    assert autoflash.load_cached_artifact(tmp_path / "cache") == (loaded, bundle_path)
    assert autoflash.refresh_artifact(channel_url, tmp_path / "cache") == (loaded, bundle_path)
    assert requested == [
        channel_url,
        manifest_url,
        manifest["serial_flash"]["url"],
        channel_url,
    ]

    bad_channel = json.loads(channel_data)
    bad_channel["manifest_url"] = "https://example.com/not-canonical.json"
    downloads[channel_url] = json.dumps(bad_channel).encode()
    with pytest.raises(ValueError, match="manifest URL is not canonical"):
        autoflash.refresh_artifact(channel_url, tmp_path / "cache")


def test_autoflash_uses_stable_homebrew_platformio_interpreter(tmp_path: Path) -> None:
    prefix = tmp_path / "homebrew"
    versioned = prefix / "Cellar/platformio/6.1.19/libexec/bin/python"
    versioned.parent.mkdir(parents=True)
    versioned.write_text("")
    stable = prefix / "opt/platformio/libexec/bin/python"
    stable.parent.mkdir(parents=True)
    stable.symlink_to(versioned)
    pio = tmp_path / "pio"
    pio.write_text(f"#!{versioned}\n")

    assert autoflash.stable_pio_python(pio) == stable
    assert autoflash.stable_platformio_python(versioned) == stable


def test_autoflash_honors_disabled_production_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = json.dumps(
        {
            "schema_version": 1,
            "enabled": False,
            "manifest_url": None,
            "manifest_sha256": None,
        }
    ).encode()
    monkeypatch.setattr(autoflash, "_download", lambda _url, _limit: channel)

    assert autoflash.refresh_artifact("https://example.com/channel.json", tmp_path) is None


def test_build_manifest_binds_code_notes_and_firmware(tmp_path: Path) -> None:
    firmware = tmp_path / f"lightweave-field-{TAG}.bin"
    firmware.write_bytes(b"field firmware")
    serial_flash = tmp_path / f"lightweave-serial-flash-{TAG}.zip"
    serial_flash.write_bytes(b"serial bundle")

    document = build.build_manifest(
        firmware=firmware,
        serial_flash=serial_flash,
        repository="https://github.com/underminedsk/lightweave.git",
        commit="a" * 40,
        tag=TAG,
        artifact_url=(
            "https://github.com/underminedsk/lightweave/releases/download/"
            f"{TAG}/lightweave-field-{TAG}.bin"
        ),
        serial_flash_url=(
            "https://github.com/underminedsk/lightweave/releases/download/"
            f"{TAG}/lightweave-serial-flash-{TAG}.zip"
        ),
        published_at="2026-08-01T19:00:00Z",
    )

    assert document["version"] == VERSION
    assert document["notes"]["control_changes"]
    assert document["notes"]["firmware_changes"]
    assert document["firmware"]["sha256"] == hashlib.sha256(b"field firmware").hexdigest()
    assert document["serial_flash"]["sha256"] == hashlib.sha256(b"serial bundle").hexdigest()


def test_build_manifest_rejects_tag_version_mismatch(tmp_path: Path) -> None:
    firmware = tmp_path / "field.bin"
    firmware.write_bytes(b"field firmware")
    serial_flash = tmp_path / "serial.zip"
    serial_flash.write_bytes(b"serial bundle")
    with pytest.raises(ValueError, match="does not match VERSION"):
        build.build_manifest(
            firmware=firmware,
            serial_flash=serial_flash,
            repository="https://github.com/underminedsk/lightweave.git",
            commit="a" * 40,
            tag="v9.9.9",
            artifact_url="https://github.com/underminedsk/lightweave/releases/download/v9.9.9/field.bin",
            serial_flash_url="https://github.com/underminedsk/lightweave/releases/download/v9.9.9/serial.zip",
            published_at="2026-08-01T19:00:00Z",
        )


def test_promote_channel_pins_exact_manifest_bytes(tmp_path: Path) -> None:
    firmware = tmp_path / f"lightweave-field-{TAG}.bin"
    firmware.write_bytes(b"field firmware")
    serial_flash = tmp_path / f"lightweave-serial-flash-{TAG}.zip"
    serial_flash.write_bytes(b"serial bundle")
    document = build.build_manifest(
        firmware=firmware,
        serial_flash=serial_flash,
        repository="https://github.com/underminedsk/lightweave.git",
        commit="a" * 40,
        tag=TAG,
        artifact_url=(
            "https://github.com/underminedsk/lightweave/releases/download/"
            f"{TAG}/lightweave-field-{TAG}.bin"
        ),
        serial_flash_url=(
            "https://github.com/underminedsk/lightweave/releases/download/"
            f"{TAG}/lightweave-serial-flash-{TAG}.zip"
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
    serial_flash = tmp_path / f"lightweave-serial-flash-{TAG}.zip"
    serial_flash.write_bytes(b"serial bundle")
    document = build.build_manifest(
        firmware=firmware,
        serial_flash=serial_flash,
        repository="https://github.com/underminedsk/lightweave.git",
        commit="a" * 40,
        tag=TAG,
        artifact_url=(
            "https://github.com/underminedsk/lightweave/releases/download/"
            f"{TAG}/lightweave-field-{TAG}.bin"
        ),
        serial_flash_url=(
            "https://github.com/underminedsk/lightweave/releases/download/"
            f"{TAG}/lightweave-serial-flash-{TAG}.zip"
        ),
        published_at="2026-08-01T19:00:00Z",
    )
    with pytest.raises(ValueError, match="immutable Lightweave"):
        promote.promoted_channel(json.dumps(document).encode(), "https://example.com/release.json")


def test_release_publisher_is_retry_safe_and_verifies_assets_before_publish() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    publisher = PUBLISHER.read_text(encoding="utf-8")

    assert "scripts/publish_release.sh dist" in workflow
    assert "actions/checkout@" in workflow
    assert "gh release create \"$RELEASE_TAG\"" in publisher
    assert "--draft" in publisher
    assert "gh release upload" in publisher
    assert "--clobber" in publisher
    assert "verify_asset_set" in publisher
    assert "find_draft_id" in publisher
    assert 'release_api="repos/${GITHUB_REPOSITORY}/releases/${draft_id}"' in publisher
    assert "'.assets[].name'" in publisher
    assert '--published-at "$RELEASE_PUBLISHED_AT"' in workflow
    assert "grep -q 'HTTP 404'" in publisher
    assert publisher.count("cmp \"$manifest\"") == 2
    assert publisher.count("cmp \"$firmware\"") == 2
    assert publisher.count("cmp \"$serial_flash\"") == 2
    assert "build_serial_flash_bundle.py" in workflow
    assert "gh release edit \"$RELEASE_TAG\" --draft=false" in publisher

    ci_workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "build_serial_flash_bundle.py" in ci_workflow


@pytest.mark.parametrize(
    ("mode", "partial", "creates", "uploads", "edits"),
    [
        ("published", False, 0, 0, 0),
        ("existing", False, 0, 1, 1),
        ("missing", False, 1, 1, 1),
        ("delayed", False, 1, 1, 1),
        ("interrupted", True, 0, 1, 1),
    ],
)
def test_release_publisher_handles_published_new_and_resumed_drafts(
    tmp_path: Path,
    mode: str,
    partial: bool,
    creates: int,
    uploads: int,
    edits: int,
) -> None:
    result, state, remote, assets = run_fake_publisher(tmp_path, mode, partial=partial)

    assert result.returncode == 0, result.stderr
    assert state["create_calls"] == creates
    assert state["upload_calls"] == uploads
    assert state["edit_calls"] == edits
    if mode == "delayed":
        assert state["list_calls"] == 4
    assert {path.name: path.read_bytes() for path in remote.iterdir()} == assets


@pytest.mark.parametrize("mode", ["multiple", "api_error"])
def test_release_publisher_fails_closed_before_mutation_for_ambiguous_lookup(
    tmp_path: Path, mode: str
) -> None:
    result, state, _remote, _assets = run_fake_publisher(tmp_path, mode)

    assert result.returncode != 0
    assert state["create_calls"] == 0
    assert state["upload_calls"] == 0
    assert state["edit_calls"] == 0


def test_release_publisher_never_publishes_an_asset_set_mismatch(tmp_path: Path) -> None:
    result, state, _remote, _assets = run_fake_publisher(tmp_path, "asset_mismatch")

    assert result.returncode != 0
    assert state["upload_calls"] == 1
    assert state["edit_calls"] == 0


def test_release_publisher_stops_when_new_draft_never_becomes_visible(
    tmp_path: Path,
) -> None:
    result, state, _remote, _assets = run_fake_publisher(tmp_path, "never_visible")

    assert result.returncode != 0
    assert "draft release did not become visible after 10 attempts" in result.stderr
    assert state["list_calls"] == 11
    assert state["create_calls"] == 1
    assert state["upload_calls"] == 0
    assert state["edit_calls"] == 0


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
