import hashlib
import importlib.util
import json
import fcntl
import os
import pwd
from datetime import datetime, timezone
from pathlib import Path
import sys
import zlib

import pytest


SCRIPT = Path(__file__).parents[2] / "deploy" / "pi" / "gitops_reconcile.py"
SPEC = importlib.util.spec_from_file_location("gitops_reconcile", SCRIPT)
assert SPEC and SPEC.loader
gitops = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gitops
SPEC.loader.exec_module(gitops)


def release_manifest(data: bytes = b"firmware") -> dict:
    version = "0.3.0"
    return {
        "schema_version": 1,
        "release": f"v{version}",
        "version": version,
        "repository": gitops.DEFAULT_REPOSITORY,
        "ref": f"refs/tags/v{version}",
        "commit": "a" * 40,
        "published_at": "2026-08-01T18:00:00Z",
        "notes": {
            "version": version,
            "date": "2026-08-01",
            "title": "Release title",
            "control_changes": ["Control change"],
            "firmware_changes": ["Firmware change"],
        },
        "firmware": {
            "filename": "field.bin",
            "url": "https://github.com/example/field.bin",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "crc32": zlib.crc32(data) & 0xFFFFFFFF,
        },
    }


def channel(manifest_bytes: bytes) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "enabled": True,
            "manifest_url": "https://github.com/example/release.json",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
    ).encode()


def config(tmp_path: Path) -> object:
    ota_lock = tmp_path / "firmware-ota.lock"
    ota_lock.parent.mkdir(parents=True, exist_ok=True)
    ota_lock.touch()
    return gitops.ReconcileConfig(
        channel_url="https://github.com/example/production.json",
        repo=Path(__file__).parents[2],
        data_dir=tmp_path / "data",
        deployment_dir=tmp_path / "deployments-root",
        backup_dir=tmp_path / "backups",
        stable_script=tmp_path / "runtime" / "gitops_reconcile.py",
        systemd_dir=tmp_path / "systemd",
        ota_lock_path=ota_lock,
        service_user=pwd.getpwuid(os.getuid()).pw_name,
        health_attempts=1,
        health_delay_s=0,
    )


def test_disabled_channel_is_a_safe_noop(tmp_path: Path) -> None:
    disabled = json.dumps(
        {"schema_version": 1, "enabled": False, "manifest_url": None, "manifest_sha256": None}
    ).encode()
    reconciler = gitops.GitOpsReconciler(config(tmp_path), http_get=lambda _url, _max: disabled)

    assert reconciler.reconcile() == {"status": "disabled"}


def test_manifest_hash_is_verified_before_parsing_or_git(tmp_path: Path) -> None:
    bad_channel = json.dumps(
        {
            "schema_version": 1,
            "enabled": True,
            "manifest_url": "https://github.com/example/release.json",
            "manifest_sha256": "0" * 64,
        }
    ).encode()

    def get(url: str, _maximum: int) -> bytes:
        return bad_channel if url.endswith("production.json") else b"{}"

    reconciler = gitops.GitOpsReconciler(config(tmp_path), http_get=get)
    with pytest.raises(gitops.ReconcileError, match="manifest SHA-256 mismatch"):
        reconciler.desired_manifest()


def test_unapproved_repository_is_rejected(tmp_path: Path) -> None:
    document = release_manifest()
    document["repository"] = "https://github.com/attacker/repository.git"
    manifest_bytes = json.dumps(document).encode()
    channel_bytes = channel(manifest_bytes)

    def get(url: str, _maximum: int) -> bytes:
        return channel_bytes if url.endswith("production.json") else manifest_bytes

    reconciler = gitops.GitOpsReconciler(config(tmp_path), http_get=get)
    with pytest.raises(gitops.ReconcileError, match="repository is not allowed"):
        reconciler.desired_manifest()


class FakeReconciler(gitops.GitOpsReconciler):
    def __init__(self, configuration, responses, *, fail_health=False):
        super().__init__(
            configuration,
            http_get=lambda url, _maximum: responses[url],
            clock=lambda: datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc),
            sleep=lambda _seconds: None,
        )
        self.commands = []
        self.commit = "b" * 40
        self.fail_health = fail_health
        self.health_calls = 0
        self.owned_paths = []
        self.dirty = False

    def _run(self, args, *, timeout=300):
        self.commands.append(tuple(args))

    def _git(self, *args, timeout=120):
        self.commands.append(("git", *args))
        if args[:2] == ("checkout", "--detach"):
            self.commit = args[2]

    def _git_output(self, *args):
        if args == ("remote", "get-url", "origin"):
            return gitops.DEFAULT_REPOSITORY
        if args == ("rev-parse", "HEAD"):
            return self.commit
        if args == ("status", "--porcelain", "--untracked-files=all"):
            return " M control/app.py" if self.dirty else ""
        if args[0:1] == ("rev-parse",) and args[1].endswith("^{commit}"):
            return "a" * 40
        raise AssertionError(args)

    def _backup(self, timestamp):
        path = self.config.backup_dir / f"pre-gitops-{timestamp}.tgz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"backup")
        return path

    def _install_python(self):
        self.commands.append(("install-python", self.commit))

    def _install_control_unit(self):
        self.commands.append(("install-control-unit", self.commit))

    def _install_gitops_runtime(self):
        self.commands.append(("install-gitops-runtime", self.commit))

    def _set_service_readable(self, path):
        self.owned_paths.append(path)

    def _healthcheck(self, expected_commit):
        self.health_calls += 1
        self.commands.append(("healthcheck", expected_commit))
        if self.fail_health and self.health_calls == 1:
            raise gitops.ReconcileError("simulated unhealthy release")


def fake_responses(configuration, firmware=b"firmware"):
    manifest_bytes = json.dumps(release_manifest(firmware), sort_keys=True).encode()
    return {
        configuration.channel_url: channel(manifest_bytes),
        "https://github.com/example/release.json": manifest_bytes,
        "https://github.com/example/field.bin": firmware,
    }


def write_current_record(configuration, *, firmware=b"firmware"):
    firmware_path = configuration.deployment_dir / "releases" / "v0.3.0" / "field.bin"
    firmware_path.parent.mkdir(parents=True)
    firmware_path.write_bytes(firmware)
    record = {
        "schema_version": 1,
        "deployed_at": "2026-08-01T17:00:00Z",
        "previous_commit": "b" * 40,
        "backup": "/var/backups/lightweave/pre-v0.3.0.tgz",
        "firmware_local_path": str(firmware_path),
        "manifest": release_manifest(firmware),
    }
    path = configuration.deployment_dir / "current.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return firmware_path


def test_current_release_is_a_noop_only_when_staged_firmware_is_verified(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    firmware_path = write_current_record(configuration)
    reconciler = FakeReconciler(configuration, fake_responses(configuration))
    reconciler.commit = "a" * 40

    assert reconciler.reconcile() == {
        "status": "current",
        "release": "v0.3.0",
        "commit": "a" * 40,
    }
    assert not any(command[:3] == ("git", "checkout", "--detach") for command in reconciler.commands)

    firmware_path.unlink()
    result = reconciler.reconcile()
    assert result["status"] == "deployed"
    assert firmware_path.read_bytes() == b"firmware"


def test_dirty_checkout_fails_before_deployment(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    reconciler = FakeReconciler(configuration, fake_responses(configuration))
    reconciler.dirty = True

    with pytest.raises(gitops.ReconcileError, match="working tree is not clean"):
        reconciler.reconcile()

    assert not configuration.backup_dir.exists()


def test_successful_reconcile_records_release_and_verified_firmware(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    reconciler = FakeReconciler(configuration, fake_responses(configuration))

    result = reconciler.reconcile()

    assert result == {"status": "deployed", "release": "v0.3.0", "commit": "a" * 40}
    record = json.loads((configuration.deployment_dir / "current.json").read_text())
    assert record["previous_commit"] == "b" * 40
    assert record["manifest"]["commit"] == "a" * 40
    assert Path(record["firmware_local_path"]).read_bytes() == b"firmware"
    assert Path(record["firmware_local_path"]) in reconciler.owned_paths
    assert ("git", "fetch", "origin", "refs/tags/v0.3.0:refs/tags/v0.3.0") in reconciler.commands
    assert ("systemctl", "start", "lightweave-control.service") in reconciler.commands
    assert ("install-gitops-runtime", "a" * 40) in reconciler.commands


def test_failed_healthcheck_rolls_code_and_record_back(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    configuration.deployment_dir.mkdir(parents=True)
    current = configuration.deployment_dir / "current.json"
    old_manifest = release_manifest(b"old firmware")
    old_manifest["commit"] = "b" * 40
    old_firmware = configuration.deployment_dir / "releases" / "v0.3.0" / "field.bin"
    old_firmware.parent.mkdir(parents=True)
    old_firmware.write_bytes(b"old firmware")
    old_record = {
        "schema_version": 1,
        "deployed_at": "2026-07-01T18:00:00Z",
        "previous_commit": "c" * 40,
        "backup": "/var/backups/lightweave/pre-v0.3.0.tgz",
        "firmware_local_path": str(old_firmware),
        "manifest": old_manifest,
    }
    old_bytes = (json.dumps(old_record, indent=2, sort_keys=True) + "\n").encode()
    current.write_bytes(old_bytes)
    reconciler = FakeReconciler(
        configuration,
        fake_responses(configuration),
        fail_health=True,
    )

    with pytest.raises(gitops.ReconcileError, match="was rolled back"):
        reconciler.reconcile()

    assert reconciler.commit == "b" * 40
    assert current.read_bytes() == old_bytes
    assert ("install-python", "b" * 40) in reconciler.commands
    assert reconciler.health_calls == 2
    assert ("healthcheck", "a" * 40) in reconciler.commands
    assert ("healthcheck", "b" * 40) in reconciler.commands


def test_insecure_or_symlinked_deployment_directory_is_rejected(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    target = tmp_path / "service-controlled"
    target.mkdir()
    configuration.deployment_dir.symlink_to(target, target_is_directory=True)
    reconciler = FakeReconciler(configuration, fake_responses(configuration))

    with pytest.raises(gitops.ReconcileError, match="real directory"):
        reconciler.reconcile()


def test_active_ota_defers_deployment_before_backup_or_service_stop(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    reconciler = FakeReconciler(configuration, fake_responses(configuration))
    with configuration.ota_lock_path.open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = reconciler.reconcile()

    assert result == {"status": "deferred", "reason": "ota_active", "release": "v0.3.0"}
    assert not configuration.backup_dir.exists()
    assert ("systemctl", "stop", "lightweave-control.service") not in reconciler.commands


def test_healthcheck_requires_the_expected_release_commit(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    reconciler = gitops.GitOpsReconciler(
        configuration,
        http_get=lambda _url, _maximum: json.dumps({"ok": True, "commit": "b" * 40}).encode(),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(gitops.ReconcileError, match="expected commit"):
        reconciler._healthcheck("a" * 40)


class LateFailingReconciler(FakeReconciler):
    def __init__(self, configuration, responses):
        super().__init__(configuration, responses)
        self.runtime_restored = False

    def _run(self, args, *, timeout=300):
        self.commands.append(tuple(args))
        if args == ["systemctl", "enable", "--now", "lightweave-gitops.timer"]:
            raise gitops.ReconcileError("simulated timer enable failure")

    def _restore_runtime(self, snapshots):
        self.runtime_restored = True
        super()._restore_runtime(snapshots)


def test_late_failure_restores_the_previous_gitops_runtime(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    configuration.stable_script.parent.mkdir(parents=True)
    configuration.systemd_dir.mkdir(parents=True)
    configuration.stable_script.write_bytes(b"old runtime")
    for name in ("lightweave-gitops.service", "lightweave-gitops.timer"):
        (configuration.systemd_dir / name).write_bytes(b"old unit")
    reconciler = LateFailingReconciler(configuration, fake_responses(configuration))

    with pytest.raises(gitops.ReconcileError, match="was rolled back"):
        reconciler.reconcile()

    assert reconciler.runtime_restored is True
    assert configuration.stable_script.read_bytes() == b"old runtime"
