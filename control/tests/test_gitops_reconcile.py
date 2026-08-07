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
GITOPS_UNITS = (
    "lightweave-gitops-recovery.service",
    "lightweave-gitops.service",
    "lightweave-gitops.timer",
)
SPEC = importlib.util.spec_from_file_location("gitops_reconcile", SCRIPT)
assert SPEC and SPEC.loader
gitops = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gitops
SPEC.loader.exec_module(gitops)

RELEASE = "v0.3.0"
LEGACY_REPOSITORY = "https://github.com/underminedsk/lightweave.git"
RELEASE_BASE = (
    f"{gitops.DEFAULT_REPOSITORY.removesuffix('.git')}/releases/download/{RELEASE}"
)
MANIFEST_URL = f"{RELEASE_BASE}/lightweave-release.json"
FIRMWARE_URL = f"{RELEASE_BASE}/lightweave-field-{RELEASE}.bin"
SERIAL_FLASH_URL = f"{RELEASE_BASE}/lightweave-serial-flash-{RELEASE}.zip"


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
            "filename": f"lightweave-field-{RELEASE}.bin",
            "url": FIRMWARE_URL,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "crc32": zlib.crc32(data) & 0xFFFFFFFF,
            "protocol": 6,
        },
        "serial_flash": {
            "filename": f"lightweave-serial-flash-{RELEASE}.zip",
            "url": SERIAL_FLASH_URL,
            "sha256": hashlib.sha256(b"serial bundle").hexdigest(),
            "size": len(b"serial bundle"),
        },
    }


def channel(manifest_bytes: bytes) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "enabled": True,
            "manifest_url": MANIFEST_URL,
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


def test_atomic_state_replacements_and_deletions_sync_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced = []
    monkeypatch.setattr(gitops, "_fsync_directory", synced.append)
    state = tmp_path / "state"
    state.mkdir()
    record = state / "record.json"
    link = state / "current"

    gitops._atomic_write(record, b"state")
    gitops._atomic_symlink(record, link)
    gitops._durable_unlink(record)

    assert synced == [state, state, state]


@pytest.mark.parametrize("completion_path", ["deploy", "rollback", "recovery"])
def test_transaction_is_only_deleted_after_filesystem_commit_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completion_path: str
) -> None:
    events = []

    monkeypatch.setattr(gitops, "_sync_filesystems", lambda: events.append("sync"))
    original_unlink = gitops._durable_unlink

    def unlink(path):
        if path.name == "transaction.json":
            events.append("unlink")
            assert events[-2:] == ["sync", "unlink"]
        original_unlink(path)

    monkeypatch.setattr(gitops, "_durable_unlink", unlink)

    configuration = config(tmp_path)
    reconciler = FakeReconciler(
        configuration,
        fake_responses(configuration),
        fail_health=completion_path == "rollback",
    )
    if completion_path == "recovery":
        reconciler._write_transaction(
            "b" * 40,
            reconciler._venv_path("b" * 40),
            None,
            reconciler._snapshot_runtime(),
        )
        reconciler.commit = "a" * 40
        assert reconciler.reconcile()["status"] == "recovered"
    elif completion_path == "rollback":
        with pytest.raises(gitops.ReconcileError, match="was rolled back"):
            reconciler.reconcile()
    else:
        assert reconciler.reconcile()["status"] == "deployed"
    assert events[-2:] == ["sync", "unlink"]


def test_manifest_hash_is_verified_before_parsing_or_git(tmp_path: Path) -> None:
    bad_channel = json.dumps(
        {
            "schema_version": 1,
            "enabled": True,
            "manifest_url": MANIFEST_URL,
            "manifest_sha256": "0" * 64,
        }
    ).encode()

    def get(url: str, _maximum: int) -> bytes:
        return bad_channel if url.endswith("production.json") else b"{}"

    reconciler = gitops.GitOpsReconciler(config(tmp_path), http_get=get)
    with pytest.raises(gitops.ReconcileError, match="manifest SHA-256 mismatch"):
        reconciler.desired_manifest()


def test_noncanonical_manifest_url_is_rejected_before_download(tmp_path: Path) -> None:
    manifest_bytes = json.dumps(release_manifest()).encode()
    channel_bytes = json.dumps(
        {
            "schema_version": 1,
            "enabled": True,
            "manifest_url": "https://example.com/lightweave-release.json",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
    ).encode()
    requested = []

    def get(url: str, _maximum: int) -> bytes:
        requested.append(url)
        return channel_bytes

    configuration = config(tmp_path)
    reconciler = gitops.GitOpsReconciler(configuration, http_get=get)
    with pytest.raises(ValueError, match="manifest URL is not canonical"):
        reconciler.desired_manifest()

    assert requested == [configuration.channel_url]


def test_manifest_release_must_match_canonical_url(tmp_path: Path) -> None:
    document = release_manifest()
    document["release"] = "v0.3.1"
    document["version"] = "0.3.1"
    document["ref"] = "refs/tags/v0.3.1"
    document["notes"]["version"] = "0.3.1"
    document["firmware"]["filename"] = "lightweave-field-v0.3.1.bin"
    other_base = (
        f"{gitops.DEFAULT_REPOSITORY.removesuffix('.git')}/releases/download/v0.3.1"
    )
    document["firmware"]["url"] = f"{other_base}/lightweave-field-v0.3.1.bin"
    document["serial_flash"]["filename"] = "lightweave-serial-flash-v0.3.1.zip"
    document["serial_flash"]["url"] = f"{other_base}/lightweave-serial-flash-v0.3.1.zip"
    manifest_bytes = json.dumps(document).encode()
    channel_bytes = channel(manifest_bytes)
    configuration = config(tmp_path)

    def get(url: str, _maximum: int) -> bytes:
        return channel_bytes if url == configuration.channel_url else manifest_bytes

    reconciler = gitops.GitOpsReconciler(configuration, http_get=get)
    with pytest.raises(gitops.ReconcileError, match="does not match its canonical URL"):
        reconciler.desired_manifest()


def test_unapproved_repository_is_rejected(tmp_path: Path) -> None:
    document = release_manifest()
    document["repository"] = "https://github.com/attacker/repository.git"
    attacker_base = "https://github.com/attacker/repository/releases/download/v0.3.0"
    document["firmware"]["url"] = f"{attacker_base}/{document['firmware']['filename']}"
    document["serial_flash"]["url"] = (
        f"{attacker_base}/{document['serial_flash']['filename']}"
    )
    manifest_bytes = json.dumps(document).encode()
    channel_bytes = channel(manifest_bytes)

    def get(url: str, _maximum: int) -> bytes:
        return channel_bytes if url.endswith("production.json") else manifest_bytes

    reconciler = gitops.GitOpsReconciler(config(tmp_path), http_get=get)
    with pytest.raises(gitops.ReconcileError, match="repository is not allowed"):
        reconciler.desired_manifest()


def manifest_for_repository(repository: str) -> dict:
    document = release_manifest()
    base = f"{repository.removesuffix('.git')}/releases/download/{RELEASE}"
    document["repository"] = repository
    document["firmware"]["url"] = f"{base}/{document['firmware']['filename']}"
    document["serial_flash"]["url"] = f"{base}/{document['serial_flash']['filename']}"
    return document


def test_default_channel_url_is_served_from_the_current_remote() -> None:
    # GitHub redirects the old owner's paths after a transfer, but only until
    # that name is claimed again -- at which point a channel URL left pointing
    # at the old owner would silently serve an unrelated repo's content.
    owner_repo = gitops.DEFAULT_REPOSITORY.removeprefix(
        "https://github.com/"
    ).removesuffix(".git")
    assert gitops.DEFAULT_CHANNEL_URL.startswith(
        f"https://raw.githubusercontent.com/{owner_repo}/"
    )


def test_allowed_repositories_cover_both_remotes_and_accept_an_env_override() -> None:
    assert gitops.DEFAULT_REPOSITORY in gitops.DEFAULT_REPOSITORIES
    assert LEGACY_REPOSITORY in gitops.DEFAULT_REPOSITORIES
    assert (
        gitops.ReconcileConfig.from_environ({}).allowed_repositories
        == gitops.DEFAULT_REPOSITORIES
    )
    override = gitops.ReconcileConfig.from_environ(
        {
            "LIGHTWEAVE_GITOPS_ALLOWED_REPOSITORY": (
                f" {LEGACY_REPOSITORY} , https://github.com/example/other.git "
            )
        }
    )
    assert override.allowed_repositories == (
        LEGACY_REPOSITORY,
        "https://github.com/example/other.git",
    )
    with pytest.raises(gitops.ReconcileError, match="allowed repository list is empty"):
        gitops.ReconcileConfig.from_environ({"LIGHTWEAVE_GITOPS_ALLOWED_REPOSITORY": " , "})


def test_release_published_before_the_org_move_is_still_accepted(tmp_path: Path) -> None:
    document = manifest_for_repository(LEGACY_REPOSITORY)
    manifest_bytes = json.dumps(document).encode()
    legacy_manifest_url = (
        f"{LEGACY_REPOSITORY.removesuffix('.git')}/releases/download/"
        f"{RELEASE}/lightweave-release.json"
    )
    channel_bytes = json.dumps(
        {
            "schema_version": 1,
            "enabled": True,
            "manifest_url": legacy_manifest_url,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
    ).encode()
    configuration = config(tmp_path)

    def get(url: str, _maximum: int) -> bytes:
        return channel_bytes if url == configuration.channel_url else manifest_bytes

    reconciler = gitops.GitOpsReconciler(configuration, http_get=get)
    manifest = reconciler.desired_manifest()
    assert manifest.release == RELEASE
    assert manifest.repository == LEGACY_REPOSITORY


def test_manifest_may_not_name_a_different_remote_than_its_own_url(
    tmp_path: Path,
) -> None:
    # Channel URL is served from the current remote; the manifest claims the
    # legacy one. Both are allowed individually, but mixing them is not.
    document = manifest_for_repository(LEGACY_REPOSITORY)
    manifest_bytes = json.dumps(document).encode()
    channel_bytes = channel(manifest_bytes)
    configuration = config(tmp_path)

    def get(url: str, _maximum: int) -> bytes:
        return channel_bytes if url == configuration.channel_url else manifest_bytes

    reconciler = gitops.GitOpsReconciler(configuration, http_get=get)
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
        old_venv = self._venv_path(self.commit)
        old_venv.mkdir(parents=True, exist_ok=True)
        if not self._venv_link().exists():
            self._venv_link().symlink_to(old_venv)

    def _venv_root(self):
        return self.config.deployment_dir / "fake-venvs"

    def _venv_link(self):
        return self.config.deployment_dir / "fake-venv"

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

    def _install_python(self, commit):
        self.commands.append(("install-python", self.commit))
        target = self._venv_path(commit)
        target.mkdir(parents=True, exist_ok=True)
        return target

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
        MANIFEST_URL: manifest_bytes,
        FIRMWARE_URL: firmware,
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
    target_venv = reconciler._venv_path("a" * 40)
    target_venv.mkdir(parents=True)
    reconciler._switch_python(target_venv)
    reconciler._write_running_commit("a" * 40)

    assert reconciler.reconcile() == {
        "status": "current",
        "release": "v0.3.0",
        "commit": "a" * 40,
    }
    assert not any(command[:3] == ("git", "checkout", "--detach") for command in reconciler.commands)
    assert ("healthcheck", "a" * 40) in reconciler.commands

    firmware_path.unlink()
    result = reconciler.reconcile()
    assert result["status"] == "deployed"
    record = json.loads(reconciler._current_record_path().read_text())
    assert Path(record["firmware_local_path"]).read_bytes() == b"firmware"


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
    assert reconciler._venv_link().resolve() == reconciler._venv_path("a" * 40)
    assert (configuration.deployment_dir / "running-commit").read_text().strip() == "a" * 40


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
    assert reconciler._venv_link().resolve() == reconciler._venv_path("b" * 40)
    assert (configuration.deployment_dir / "running-commit").read_text().strip() == "b" * 40
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


def test_active_ota_defers_interrupted_deployment_recovery(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    reconciler = FakeReconciler(configuration, fake_responses(configuration))
    reconciler._write_transaction(
        "b" * 40,
        reconciler._venv_path("b" * 40),
        None,
        reconciler._snapshot_runtime(),
    )

    with configuration.ota_lock_path.open("r") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = reconciler.reconcile()

    assert result == {"status": "deferred", "reason": "ota_active", "recovery": True}
    assert reconciler._transaction_path().exists()
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
    for name in GITOPS_UNITS:
        (configuration.systemd_dir / name).write_bytes(b"old unit")
    reconciler = LateFailingReconciler(configuration, fake_responses(configuration))

    with pytest.raises(gitops.ReconcileError, match="was rolled back"):
        reconciler.reconcile()

    assert reconciler.runtime_restored is True
    assert configuration.stable_script.read_bytes() == b"old runtime"


class TransactionOrderReconciler(FakeReconciler):
    def _git(self, *args, timeout=120):
        if args[:2] == ("checkout", "--detach") and args[2] == "a" * 40:
            assert self._transaction_path().is_file()
        super()._git(*args, timeout=timeout)


def test_rollback_transaction_is_durable_before_checkout(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    reconciler = TransactionOrderReconciler(
        configuration, fake_responses(configuration)
    )

    assert reconciler.reconcile()["status"] == "deployed"


class StopFailingReconciler(FakeReconciler):
    def __init__(self, configuration, responses):
        super().__init__(configuration, responses)
        self.stop_calls = 0

    def _run(self, args, *, timeout=300):
        self.commands.append(tuple(args))
        if args == ["systemctl", "stop", "lightweave-control.service"]:
            self.stop_calls += 1
            if self.stop_calls == 1:
                raise gitops.ReconcileError("simulated stop timeout")


def test_initial_stop_failure_attempts_a_full_service_recovery(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    reconciler = StopFailingReconciler(configuration, fake_responses(configuration))

    with pytest.raises(gitops.ReconcileError, match="was rolled back"):
        reconciler.reconcile()

    assert reconciler.commit == "b" * 40
    assert reconciler.stop_calls == 2
    assert ("systemctl", "start", "lightweave-control.service") in reconciler.commands
    assert ("healthcheck", "b" * 40) in reconciler.commands


def test_installer_provisions_hardened_unit_paths_and_versioned_runtime() -> None:
    installer = (SCRIPT.parent / "install-gitops.sh").read_text(encoding="utf-8")
    control_unit = (SCRIPT.parent / "lightweave-control.service").read_text(encoding="utf-8")
    provisioner_unit = (SCRIPT.parent / "lightweave-provisioner.service").read_text(
        encoding="utf-8"
    )
    runbook = (SCRIPT.parent / "README.md").read_text(encoding="utf-8")

    assert "install -d -o root -g root -m 0700 /var/backups/lightweave" in installer
    assert 'release_venv="$repo/.venvs/$running_commit"' in installer
    assert 'mv -Tf "$repo/.venv.new" "$repo/.venv"' in installer
    assert "lightweave-gitops-recovery.service" in installer
    assert '"$repo/deploy/pi/lightweave-control.service"' in installer
    assert '"$repo/deploy/pi/lightweave-provisioner.service"' in installer
    assert "systemctl restart lightweave-control.service" in installer
    assert 'if [ "$health_commit" != "$running_commit" ]' in installer
    assert "ReadOnlyPaths=-/var/lib/lightweave-gitops" in control_unit
    assert "ExecStart=/opt/lightweave/.venv/bin/python -m uvicorn" in control_unit
    assert "Wants=network-online.target lightweave-provisioner.service" in control_unit
    assert "PartOf=lightweave-control.service" in provisioner_unit
    assert "-m control.provisioner --socket /run/lightweave-provisioner/provisioner.sock" in provisioner_unit
    assert "UnsetEnvironment=CONTROL_PASSWORD_HASH" in provisioner_unit
    assert "ReadWritePaths=/var/lib/lightweave/provisioner" in provisioner_unit
    assert "PROVISIONER_OPERATION_LOCK=/var/lib/lightweave-gitops/firmware-ota.lock" in provisioner_unit
    assert "sudo systemctl stop lightweave-provisioner 2>/dev/null || true" in runbook
    assert "sudo rm -f /etc/systemd/system/lightweave-provisioner.service" in runbook
    assert "sudo systemctl start lightweave-provisioner" in runbook
    emergency_upgrade = runbook.split("The commands below are retained", 1)[1].split(
        "## 13. Emergency manual rollback", 1
    )[0]
    emergency_rollback = runbook.split("## 13. Emergency manual rollback", 1)[1]
    assert '"/opt/lightweave/.venvs/$new_commit/bin/python" -m pip install' in emergency_upgrade
    assert "running-commit.new" in emergency_upgrade
    assert "pip install" not in emergency_rollback
    assert '"/opt/lightweave/.venvs/$previous_commit"' in emergency_rollback
    assert "disable --now lightweave-gitops.timer" in emergency_rollback
    assert "systemctl start lightweave-gitops.timer" not in emergency_rollback
    assert runbook.index("install-gitops.sh") < runbook.index(
        "enable --now lightweave-control.service"
    )


class EnvironmentBuildingReconciler(FakeReconciler):
    def _install_python(self, commit):
        return gitops.GitOpsReconciler._install_python(self, commit)

    def _run(self, args, *, timeout=300):
        self.commands.append(tuple(args))
        if args[1:3] == ["-m", "venv"]:
            python = Path(args[3]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"fake python")


def test_python_environment_is_built_fresh_and_reused_only_after_completion(
    tmp_path: Path,
) -> None:
    configuration = config(tmp_path)
    reconciler = EnvironmentBuildingReconciler(
        configuration, fake_responses(configuration)
    )
    commit = "a" * 40

    target = reconciler._install_python(commit)

    assert target == reconciler._venv_path(commit)
    assert (target / "bin" / "python").is_file()
    assert target.stat().st_mode & 0o777 == 0o755
    install = next(command for command in reconciler.commands if "install" in command)
    assert "--require-hashes" in install
    assert "--only-binary=:all:" in install
    venv_commands = [
        command for command in reconciler.commands if command[1:3] == ("-m", "venv")
    ]
    assert len(venv_commands) == 1

    target.chmod(0o700)
    reconciler._install_python(commit)

    venv_commands = [
        command for command in reconciler.commands if command[1:3] == ("-m", "venv")
    ]
    assert len(venv_commands) == 1
    assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("boundary", [1, 2, 3, 4, 5])
def test_interrupted_deployment_recovers_durable_previous_state(
    tmp_path: Path, boundary: int
) -> None:
    configuration = config(tmp_path)
    reconciler = FakeReconciler(configuration, fake_responses(configuration))
    previous_commit = "b" * 40
    previous_venv = reconciler._venv_path(previous_commit)
    previous_record = b'{"old":"record"}\n'
    configuration.stable_script.parent.mkdir(parents=True)
    configuration.systemd_dir.mkdir(parents=True)
    configuration.stable_script.write_bytes(b"old runtime")
    for name in GITOPS_UNITS:
        (configuration.systemd_dir / name).write_bytes(b"old unit")
    runtime_snapshots = reconciler._snapshot_runtime()
    reconciler._write_transaction(
        previous_commit,
        previous_venv,
        previous_record,
        runtime_snapshots,
    )
    reconciler.commit = "a" * 40
    target_venv = reconciler._venv_path("a" * 40)
    target_venv.mkdir()
    if boundary >= 2:
        reconciler._switch_python(target_venv)
    if boundary >= 3:
        reconciler._write_running_commit("a" * 40)
    if boundary >= 4:
        reconciler._current_record_path().write_bytes(b'{"target":"record"}\n')
    if boundary >= 5:
        configuration.stable_script.write_bytes(b"new runtime")
        for name in GITOPS_UNITS:
            (configuration.systemd_dir / name).write_bytes(b"new unit")

    result = reconciler.reconcile()

    assert result == {"status": "recovered", "commit": previous_commit}
    assert reconciler.commit == previous_commit
    assert reconciler._venv_link().resolve() == previous_venv
    assert reconciler._running_commit_path().read_text().strip() == previous_commit
    assert reconciler._current_record_path().read_bytes() == previous_record
    assert configuration.stable_script.read_bytes() == b"old runtime"
    for name in GITOPS_UNITS:
        assert (configuration.systemd_dir / name).read_bytes() == b"old unit"
    assert not reconciler._transaction_path().exists()
    assert ("healthcheck", previous_commit) in reconciler.commands


def test_boot_recovery_restores_state_without_starting_control(tmp_path: Path) -> None:
    configuration = config(tmp_path)
    reconciler = FakeReconciler(configuration, fake_responses(configuration))
    previous_commit = "b" * 40
    previous_venv = reconciler._venv_path(previous_commit)
    reconciler._write_transaction(
        previous_commit,
        previous_venv,
        None,
        reconciler._snapshot_runtime(),
    )
    reconciler.commit = "a" * 40
    target_venv = reconciler._venv_path("a" * 40)
    target_venv.mkdir()
    reconciler._switch_python(target_venv)
    reconciler.commands.clear()

    result = reconciler.recover_before_control_start()

    assert result == {"status": "restored", "commit": previous_commit}
    assert reconciler.commit == previous_commit
    assert reconciler._venv_link().resolve() == previous_venv
    assert reconciler._transaction_path().exists()
    assert ("systemctl", "daemon-reload") in reconciler.commands
    assert ("systemctl", "start", "lightweave-control.service") not in reconciler.commands
    assert not any(command[0] == "healthcheck" for command in reconciler.commands)


def test_systemd_orders_boot_recovery_before_control_start() -> None:
    control_unit = (SCRIPT.parent / "lightweave-control.service").read_text(encoding="utf-8")
    gitops_unit = (SCRIPT.parent / "lightweave-gitops.service").read_text(encoding="utf-8")
    recovery_unit = (SCRIPT.parent / "lightweave-gitops-recovery.service").read_text(
        encoding="utf-8"
    )

    assert "Requires=lightweave-gitops-recovery.service" in control_unit
    assert "After=network-online.target lightweave-gitops-recovery.service" in control_unit
    assert "Requires=lightweave-gitops-recovery.service" in gitops_unit
    assert "After=network-online.target lightweave-gitops-recovery.service" in gitops_unit
    assert (
        "Before=lightweave-control.service lightweave-provisioner.service "
        "lightweave-gitops.service"
    ) in recovery_unit
    assert "ExecStart=/usr/local/lib/lightweave/gitops_reconcile.py --recover-only" in recovery_unit
    assert "RemainAfterExit=yes" in recovery_unit


def test_boot_recovery_lock_blocks_while_normal_reconcile_lock_is_nonblocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flags = []

    def record(_lock, value):
        flags.append(value)

    monkeypatch.setattr(gitops.fcntl, "flock", record)

    assert gitops._acquire_process_lock(object(), recover_only=True) is True
    assert gitops._acquire_process_lock(object(), recover_only=False) is True
    assert flags == [gitops.fcntl.LOCK_EX, gitops.fcntl.LOCK_EX | gitops.fcntl.LOCK_NB]


def test_boot_recovery_lock_contention_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def contend(_lock, _flags):
        raise BlockingIOError

    monkeypatch.setattr(gitops.fcntl, "flock", contend)

    with pytest.raises(gitops.ReconcileError, match="boot recovery lock"):
        gitops._acquire_process_lock(object(), recover_only=True)
    assert gitops._acquire_process_lock(object(), recover_only=False) is False
