#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_CHANNEL_URL = (
    "https://raw.githubusercontent.com/moda-labs/lightweave/"
    "main/deploy/channels/production.json"
)
# The project moved from underminedsk/ to moda-labs/ after v0.7.0. Release
# manifests are immutable, so releases published before the move still name the
# old remote. Both stay allowed until every promoted release names moda-labs.
DEFAULT_REPOSITORIES = (
    "https://github.com/moda-labs/lightweave.git",
    "https://github.com/underminedsk/lightweave.git",
)
DEFAULT_REPOSITORY = DEFAULT_REPOSITORIES[0]
MAX_CHANNEL_BYTES = 16 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_FIRMWARE_BYTES = 0x140000


class ReconcileError(RuntimeError):
    pass


def _parse_repositories(value: str | None) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_REPOSITORIES
    entries = tuple(entry.strip() for entry in value.split(",") if entry.strip())
    if not entries:
        raise ReconcileError("allowed repository list is empty")
    return entries


def _same_repository(left: str, right: str) -> bool:
    return (
        left.rstrip("/").removesuffix(".git") == right.rstrip("/").removesuffix(".git")
    )


@dataclass(frozen=True)
class ReconcileConfig:
    channel_url: str = DEFAULT_CHANNEL_URL
    allowed_repositories: tuple[str, ...] = DEFAULT_REPOSITORIES
    repo: Path = Path("/opt/lightweave")
    data_dir: Path = Path("/var/lib/lightweave")
    deployment_dir: Path = Path("/var/lib/lightweave-gitops")
    backup_dir: Path = Path("/var/backups/lightweave")
    stable_script: Path = Path("/usr/local/lib/lightweave/gitops_reconcile.py")
    systemd_dir: Path = Path("/etc/systemd/system")
    ota_lock_path: Path = Path("/var/lib/lightweave-gitops/firmware-ota.lock")
    health_url: str = "http://127.0.0.1:8000/api/health"
    service_user: str = "lightweave"
    health_attempts: int = 30
    health_delay_s: float = 2.0

    @classmethod
    def from_environ(cls, environ: dict[str, str] | os._Environ[str]) -> "ReconcileConfig":
        return cls(
            channel_url=environ.get("LIGHTWEAVE_GITOPS_CHANNEL_URL", DEFAULT_CHANNEL_URL),
            allowed_repositories=_parse_repositories(
                environ.get("LIGHTWEAVE_GITOPS_ALLOWED_REPOSITORY")
            ),
            repo=Path(environ.get("LIGHTWEAVE_GITOPS_REPO", "/opt/lightweave")),
            data_dir=Path(environ.get("LIGHTWEAVE_GITOPS_DATA_DIR", "/var/lib/lightweave")),
            deployment_dir=Path(
                environ.get("LIGHTWEAVE_GITOPS_DEPLOYMENT_DIR", "/var/lib/lightweave-gitops")
            ),
            backup_dir=Path(environ.get("LIGHTWEAVE_GITOPS_BACKUP_DIR", "/var/backups/lightweave")),
            stable_script=Path(
                environ.get(
                    "LIGHTWEAVE_GITOPS_STABLE_SCRIPT",
                    "/usr/local/lib/lightweave/gitops_reconcile.py",
                )
            ),
            systemd_dir=Path(
                environ.get("LIGHTWEAVE_GITOPS_SYSTEMD_DIR", "/etc/systemd/system")
            ),
            ota_lock_path=Path(
                environ.get(
                    "LIGHTWEAVE_GITOPS_OTA_LOCK",
                    "/var/lib/lightweave-gitops/firmware-ota.lock",
                )
            ),
            health_url=environ.get(
                "LIGHTWEAVE_GITOPS_HEALTH_URL",
                "http://127.0.0.1:8000/api/health",
            ),
            service_user=environ.get("LIGHTWEAVE_GITOPS_SERVICE_USER", "lightweave"),
            health_attempts=int(environ.get("LIGHTWEAVE_GITOPS_HEALTH_ATTEMPTS", "30")),
            health_delay_s=float(environ.get("LIGHTWEAVE_GITOPS_HEALTH_DELAY_S", "2")),
        )


def _load_release_module(repo: Path):
    repo_text = str(repo)
    if repo_text not in sys.path:
        sys.path.insert(0, repo_text)
    from control import releases

    return releases


def _atomic_write(path: Path, data: bytes, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), mode)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _sync_filesystems() -> None:
    """Flush the checked-out repository and deployment state before commit."""
    os.sync()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(path: Path) -> None:
    for root, _directories, filenames in os.walk(path, topdown=False):
        directory = Path(root)
        for filename in filenames:
            candidate = directory / filename
            if not candidate.is_symlink():
                _fsync_file(candidate)
        _fsync_directory(directory)


def _atomic_symlink(target: Path, link: Path) -> None:
    temporary = link.with_name(f".{link.name}.{os.getpid()}.new")
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
        _fsync_directory(link.parent)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    data: bytes | None
    mode: int | None


class GitOpsReconciler:
    def __init__(
        self,
        config: ReconcileConfig,
        *,
        http_get: Callable[[str, int], bytes] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        load_releases: bool = True,
    ) -> None:
        self.config = config
        self.http_get = http_get or self._http_get
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.releases = _load_release_module(config.repo) if load_releases else None

    @staticmethod
    def _http_get(url: str, maximum: int) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "lightweave-gitops/1"})
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read(maximum + 1)
        if len(data) > maximum:
            raise ReconcileError(f"download exceeds {maximum} bytes")
        return data

    def _run(self, args: list[str], *, timeout: int = 300) -> None:
        subprocess.run(args, check=True, timeout=timeout)

    def _output(self, args: list[str], *, timeout: int = 60) -> str:
        return subprocess.check_output(args, text=True, timeout=timeout).strip()

    def _git(self, *args: str, timeout: int = 120) -> None:
        self._run(["git", "-C", str(self.config.repo), *args], timeout=timeout)

    def _git_output(self, *args: str) -> str:
        return self._output(["git", "-C", str(self.config.repo), *args])

    def _json_download(self, url: str, maximum: int, name: str) -> tuple[bytes, Any]:
        data = self.http_get(url, maximum)
        try:
            return data, json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReconcileError(f"{name} is not valid JSON") from error

    def desired_manifest(self):
        if self.releases is None:
            raise ReconcileError("release metadata is unavailable in recovery-only mode")
        _, channel_document = self._json_download(
            self.config.channel_url,
            MAX_CHANNEL_BYTES,
            "release channel",
        )
        channel = self.releases.parse_release_channel(channel_document)
        if not channel["enabled"]:
            return None
        expected_release = None
        expected_repository = None
        last_error = None
        for repository in self.config.allowed_repositories:
            try:
                expected_release = self.releases.release_from_manifest_url(
                    channel["manifest_url"], repository
                )
            except self.releases.ReleaseMetadataError as error:
                last_error = error
                continue
            expected_repository = repository
            break
        if expected_repository is None:
            if last_error is None:
                raise ReconcileError("no allowed repository is configured")
            # Re-raise the release module's own error so callers keep seeing a
            # ReleaseMetadataError for a non-canonical URL.
            raise last_error
        manifest_bytes, manifest_document = self._json_download(
            channel["manifest_url"],
            MAX_MANIFEST_BYTES,
            "release manifest",
        )
        if hashlib.sha256(manifest_bytes).hexdigest() != channel["manifest_sha256"]:
            raise ReconcileError("release manifest SHA-256 mismatch")
        manifest = self.releases.parse_release_manifest(manifest_document)
        # The manifest must name the same remote its own URL was served from,
        # so a moda-labs channel entry cannot smuggle in an underminedsk build.
        if not _same_repository(manifest.repository, expected_repository):
            raise ReconcileError("release manifest repository is not allowed")
        if manifest.release != expected_release:
            raise ReconcileError("release manifest does not match its canonical URL")
        return manifest

    def _current_record_path(self) -> Path:
        return self.config.deployment_dir / "current.json"

    def _running_commit_path(self) -> Path:
        return self.config.deployment_dir / "running-commit"

    def _transaction_path(self) -> Path:
        return self.config.deployment_dir / "transaction.json"

    def _venv_root(self) -> Path:
        return self.config.repo / ".venvs"

    def _venv_path(self, commit: str) -> Path:
        return self._venv_root() / commit

    def _venv_link(self) -> Path:
        return self.config.repo / ".venv"

    def _current_record(self) -> dict[str, Any] | None:
        if self.releases is None:
            raise ReconcileError("release metadata is unavailable in recovery-only mode")
        return self.releases.load_deployment_record(self._current_record_path())

    def _service_account(self) -> pwd.struct_passwd:
        return pwd.getpwnam(self.config.service_user)

    def _prepare_deployment_dir(self) -> None:
        path = self.config.deployment_dir
        try:
            status = path.lstat()
        except FileNotFoundError:
            try:
                path.mkdir(mode=0o750)
            except OSError as error:
                raise ReconcileError(f"cannot create root-owned deployment directory: {error}") from error
            status = path.lstat()
        if path.is_symlink() or not path.is_dir():
            raise ReconcileError("deployment directory must be a real directory")
        if status.st_uid != os.geteuid():
            raise ReconcileError("deployment directory is not owned by the reconciler user")
        if status.st_mode & 0o022:
            raise ReconcileError("deployment directory must not be group/world writable")
        service = self._service_account()
        os.chown(path, os.geteuid(), service.pw_gid)
        os.chmod(path, 0o750)

    def _set_service_readable(self, path: Path) -> None:
        service = self._service_account()
        os.chown(path, os.geteuid(), service.pw_gid)
        os.chmod(path, 0o640)
        _fsync_file(path)

    def _prepare_release_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        service = self._service_account()
        os.chown(path, os.geteuid(), service.pw_gid)
        os.chmod(path, 0o750)
        current = path
        while True:
            _fsync_directory(current)
            if current == self.config.deployment_dir:
                break
            current = current.parent

    def _verify_origin(self) -> None:
        actual = self._git_output("remote", "get-url", "origin")
        if not any(
            _same_repository(actual, repository)
            for repository in self.config.allowed_repositories
        ):
            raise ReconcileError("repository origin does not match the allowed repository")
        if self._git_output("status", "--porcelain", "--untracked-files=all"):
            raise ReconcileError("repository working tree is not clean")

    def _download_firmware(self, manifest) -> bytes:
        data = self.http_get(str(manifest.firmware["url"]), MAX_FIRMWARE_BYTES)
        if len(data) != manifest.firmware["size"]:
            raise ReconcileError("firmware size mismatch")
        if hashlib.sha256(data).hexdigest() != manifest.firmware["sha256"]:
            raise ReconcileError("firmware SHA-256 mismatch")
        if zlib.crc32(data) & 0xFFFFFFFF != manifest.firmware["crc32"]:
            raise ReconcileError("firmware CRC32 mismatch")
        return data

    def _record_has_verified_firmware(self, record: dict[str, Any], manifest) -> bool:
        if self.releases is None:
            raise ReconcileError("release metadata is unavailable in recovery-only mode")
        deployed = self.releases.parse_release_manifest(record["manifest"])
        if deployed.commit != manifest.commit:
            return False
        try:
            data = Path(record["firmware_local_path"]).read_bytes()
        except OSError:
            return False
        return (
            len(data) == manifest.firmware["size"]
            and hashlib.sha256(data).hexdigest() == manifest.firmware["sha256"]
            and zlib.crc32(data) & 0xFFFFFFFF == manifest.firmware["crc32"]
        )

    def _backup(self, timestamp: str) -> Path:
        self.config.backup_dir.mkdir(parents=True, exist_ok=True)
        backup = self.config.backup_dir / f"pre-gitops-{timestamp}.tgz"
        self._run(
            [
                "tar",
                "-C",
                str(self.config.data_dir.parent),
                "-czf",
                str(backup),
                self.config.data_dir.name,
            ],
            timeout=180,
        )
        return backup

    def _install_python(self, commit: str) -> Path:
        root = self._venv_root()
        root.mkdir(mode=0o755, exist_ok=True)
        target = self._venv_path(commit)
        if target.exists():
            python = target / "bin" / "python"
            if not python.is_file():
                raise ReconcileError(f"release environment is incomplete: {target}")
            self._run([str(python), "-m", "pip", "check"], timeout=120)
            os.chmod(target, 0o755)
            _fsync_directory(target)
            return target

        temporary = Path(tempfile.mkdtemp(prefix=f".{commit}.", dir=root))
        try:
            self._run([sys.executable, "-m", "venv", str(temporary)], timeout=180)
            python = temporary / "bin" / "python"
            self._run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "--only-binary=:all:",
                    "--find-links",
                    str(self.config.repo / "control" / "wheels"),
                    "--requirement",
                    str(self.config.repo / "control" / "requirements.lock"),
                ],
                timeout=600,
            )
            self._run([str(python), "-m", "pip", "check"], timeout=120)
            os.chmod(temporary, 0o755)
            _fsync_tree(temporary)
            os.replace(temporary, target)
            _fsync_directory(root)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return target

    def _switch_python(self, target: Path) -> None:
        _atomic_symlink(target, self._venv_link())

    def _install_control_unit(self) -> None:
        _atomic_write(
            self.config.systemd_dir / "lightweave-control.service",
            (self.config.repo / "deploy" / "pi" / "lightweave-control.service").read_bytes(),
            mode=0o644,
        )
        provisioner_source = (
            self.config.repo / "deploy" / "pi" / "lightweave-provisioner.service"
        )
        provisioner_target = self.config.systemd_dir / "lightweave-provisioner.service"
        if provisioner_source.is_file():
            _atomic_write(provisioner_target, provisioner_source.read_bytes(), mode=0o644)
        else:
            _durable_unlink(provisioner_target)
        solix_source = self.config.repo / "deploy" / "pi" / "lightweave-solix.service"
        solix_target = self.config.systemd_dir / "lightweave-solix.service"
        if solix_source.is_file():
            _atomic_write(solix_target, solix_source.read_bytes(), mode=0o644)
        else:
            _durable_unlink(solix_target)

    def _install_gitops_runtime(self) -> None:
        _atomic_write(
            self.config.stable_script,
            (self.config.repo / "deploy" / "pi" / "gitops_reconcile.py").read_bytes(),
            mode=0o755,
        )
        for name in (
            "lightweave-gitops-recovery.service",
            "lightweave-gitops.service",
            "lightweave-gitops.timer",
        ):
            _atomic_write(
                self.config.systemd_dir / name,
                (self.config.repo / "deploy" / "pi" / name).read_bytes(),
                mode=0o644,
            )

    def _runtime_paths(self) -> tuple[Path, ...]:
        return (
            self.config.stable_script,
            self.config.systemd_dir / "lightweave-gitops-recovery.service",
            self.config.systemd_dir / "lightweave-gitops.service",
            self.config.systemd_dir / "lightweave-gitops.timer",
        )

    def _snapshot_runtime(self) -> tuple[FileSnapshot, ...]:
        snapshots = []
        for path in self._runtime_paths():
            try:
                status = path.stat()
                snapshots.append(FileSnapshot(path, path.read_bytes(), status.st_mode & 0o777))
            except FileNotFoundError:
                snapshots.append(FileSnapshot(path, None, None))
        return tuple(snapshots)

    def _restore_runtime(self, snapshots: tuple[FileSnapshot, ...]) -> None:
        for snapshot in snapshots:
            if snapshot.data is None:
                _durable_unlink(snapshot.path)
            else:
                _atomic_write(snapshot.path, snapshot.data, mode=snapshot.mode or 0o644)

    def _healthcheck(self, expected_commit: str) -> None:
        last_error: Exception | None = None
        for _ in range(self.config.health_attempts):
            try:
                document = json.loads(self.http_get(self.config.health_url, 4096))
                if (
                    isinstance(document, dict)
                    and document.get("ok") is True
                    and document.get("commit") == expected_commit
                ):
                    return
                last_error = ReconcileError("health response does not identify the expected commit")
            except Exception as error:
                last_error = error
            self.sleep(self.config.health_delay_s)
        raise ReconcileError(f"control health check failed: {last_error}")

    def _write_history(self, record: dict[str, Any]) -> None:
        history = self.config.deployment_dir / "history.jsonl"
        with history.open("ab") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            os.chmod(history, 0o600)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(history.parent)

    def _restore_record(self, previous: bytes | None) -> None:
        path = self._current_record_path()
        if previous is None:
            _durable_unlink(path)
            return
        _atomic_write(path, previous)
        self._set_service_readable(path)

    def _write_transaction(
        self,
        previous_commit: str,
        previous_venv: Path,
        previous_record: bytes | None,
        runtime_snapshots: tuple[FileSnapshot, ...],
    ) -> None:
        document = {
            "schema_version": 1,
            "previous_commit": previous_commit,
            "previous_venv": str(previous_venv),
            "previous_record": (
                base64.b64encode(previous_record).decode("ascii")
                if previous_record is not None
                else None
            ),
            "runtime": [
                {
                    "data": (
                        base64.b64encode(snapshot.data).decode("ascii")
                        if snapshot.data is not None
                        else None
                    ),
                    "mode": snapshot.mode,
                }
                for snapshot in runtime_snapshots
            ],
        }
        _atomic_write(
            self._transaction_path(),
            json.dumps(document, sort_keys=True, indent=2).encode() + b"\n",
            mode=0o600,
        )

    @staticmethod
    def _decode_transaction_data(value: Any, name: str) -> bytes | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ReconcileError(f"deployment transaction {name} is invalid")
        try:
            return base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ReconcileError(f"deployment transaction {name} is invalid") from error

    def _load_transaction(
        self,
    ) -> tuple[str, Path, bytes | None, tuple[FileSnapshot, ...]] | None:
        path = self._transaction_path()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise ReconcileError(f"cannot read deployment transaction: {error}") from error
        if not isinstance(document, dict) or set(document) != {
            "schema_version",
            "previous_commit",
            "previous_venv",
            "previous_record",
            "runtime",
        }:
            raise ReconcileError("deployment transaction has an invalid shape")
        previous_commit = document["previous_commit"]
        if (
            document["schema_version"] != 1
            or not isinstance(previous_commit, str)
            or len(previous_commit) != 40
            or any(character not in "0123456789abcdef" for character in previous_commit)
        ):
            raise ReconcileError("deployment transaction has an invalid previous commit")
        previous_venv_value = document["previous_venv"]
        if not isinstance(previous_venv_value, str):
            raise ReconcileError("deployment transaction has an invalid previous environment")
        previous_venv = Path(previous_venv_value)
        try:
            expected_venv = self._venv_path(previous_commit).resolve(strict=True)
            resolved_previous_venv = previous_venv.resolve(strict=True)
        except OSError as error:
            raise ReconcileError(
                "deployment transaction previous environment is unavailable"
            ) from error
        if resolved_previous_venv != expected_venv:
            raise ReconcileError("deployment transaction has an invalid previous environment")
        runtime = document["runtime"]
        paths = self._runtime_paths()
        if not isinstance(runtime, list) or len(runtime) != len(paths):
            raise ReconcileError("deployment transaction has invalid runtime snapshots")
        snapshots = []
        for index, (item, runtime_path) in enumerate(zip(runtime, paths, strict=True)):
            if not isinstance(item, dict) or set(item) != {"data", "mode"}:
                raise ReconcileError("deployment transaction has invalid runtime snapshots")
            mode = item["mode"]
            if mode is not None and (
                not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o777
            ):
                raise ReconcileError("deployment transaction has invalid runtime mode")
            snapshots.append(
                FileSnapshot(
                    runtime_path,
                    self._decode_transaction_data(item["data"], f"runtime[{index}].data"),
                    mode,
                )
            )
        return (
            previous_commit,
            previous_venv,
            self._decode_transaction_data(document["previous_record"], "previous_record"),
            tuple(snapshots),
        )

    def _restore_transaction_state(
        self,
        transaction: tuple[str, Path, bytes | None, tuple[FileSnapshot, ...]],
        *,
        finalize: bool,
    ) -> str:
        previous_commit, previous_venv, previous_record, runtime_snapshots = transaction
        if finalize:
            with contextlib.suppress(Exception):
                self._run(["systemctl", "stop", "lightweave-control.service"], timeout=180)
        self._git("checkout", "--detach", previous_commit)
        self._install_control_unit()
        self._switch_python(previous_venv)
        self._write_running_commit(previous_commit)
        self._restore_record(previous_record)
        self._restore_runtime(runtime_snapshots)
        self._run(["systemctl", "daemon-reload"])
        if finalize:
            self._run(["systemctl", "start", "lightweave-control.service"])
            self._healthcheck(previous_commit)
            _sync_filesystems()
            _durable_unlink(self._transaction_path())
        return previous_commit

    def _recover_interrupted_deployment(self) -> dict[str, Any] | None:
        transaction = self._load_transaction()
        if transaction is None:
            return None
        try:
            previous_commit = self._restore_transaction_state(transaction, finalize=True)
        except Exception as error:
            raise ReconcileError(f"interrupted deployment recovery failed: {error}") from error
        return {"status": "recovered", "commit": previous_commit}

    def recover_before_control_start(self) -> dict[str, Any]:
        self._prepare_deployment_dir()
        transaction = self._load_transaction()
        if transaction is None:
            return {"status": "clean"}
        with self._ota_guard() as ota_available:
            if not ota_available:
                raise ReconcileError("cannot restore interrupted deployment while OTA is active")
            try:
                previous_commit = self._restore_transaction_state(transaction, finalize=False)
            except Exception as error:
                raise ReconcileError(f"boot deployment recovery failed: {error}") from error
        return {"status": "restored", "commit": previous_commit}

    def _write_running_commit(self, commit: str) -> None:
        _atomic_write(self._running_commit_path(), f"{commit}\n".encode(), mode=0o640)
        self._set_service_readable(self._running_commit_path())

    @contextlib.contextmanager
    def _ota_guard(self):
        try:
            lock = self.config.ota_lock_path.open("r")
        except OSError as error:
            raise ReconcileError(f"cannot open the installed OTA operation lock: {error}") from error
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def reconcile(self) -> dict[str, Any]:
        self._prepare_deployment_dir()
        if self._transaction_path().exists():
            with self._ota_guard() as ota_available:
                if not ota_available:
                    return {
                        "status": "deferred",
                        "reason": "ota_active",
                        "recovery": True,
                    }
                recovered = self._recover_interrupted_deployment()
                if recovered is not None:
                    return recovered
        manifest = self.desired_manifest()
        if manifest is None:
            return {"status": "disabled"}
        self._verify_origin()
        current_commit = self._git_output("rev-parse", "HEAD").lower()
        current_record = self._current_record()
        marker_commit = None
        try:
            marker_commit = self._running_commit_path().read_text(encoding="utf-8").strip()
        except OSError:
            pass
        current_venv = None
        try:
            current_venv = self._venv_link().resolve(strict=True)
        except OSError:
            pass
        if (
            current_commit == manifest.commit
            and current_record
            and self._record_has_verified_firmware(current_record, manifest)
            and marker_commit == manifest.commit
            and current_venv == self._venv_path(manifest.commit).resolve()
        ):
            self._healthcheck(manifest.commit)
            return {"status": "current", "release": manifest.release, "commit": manifest.commit}

        firmware = self._download_firmware(manifest)
        self._git("fetch", "origin", f"{manifest.ref}:{manifest.ref}")
        resolved = self._git_output("rev-parse", f"{manifest.ref}^{{commit}}").lower()
        if resolved != manifest.commit:
            raise ReconcileError("release tag does not resolve to the manifest commit")

        with self._ota_guard() as ota_available:
            if not ota_available:
                return {"status": "deferred", "reason": "ota_active", "release": manifest.release}
            return self._deploy(manifest, firmware, current_commit)

    def _deploy(self, manifest, firmware: bytes, current_commit: str) -> dict[str, Any]:
        now = self.clock()
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        backup = self._backup(timestamp)
        release_dir = self.config.deployment_dir / "releases" / manifest.release
        self._prepare_release_dir(release_dir)
        firmware_path = release_dir / str(manifest.firmware["filename"])
        _atomic_write(firmware_path, firmware, mode=0o640)
        self._set_service_readable(firmware_path)
        previous_record = (
            self._current_record_path().read_bytes()
            if self._current_record_path().exists()
            else None
        )
        record = {
            "schema_version": 1,
            "deployed_at": now.isoformat().replace("+00:00", "Z"),
            "previous_commit": current_commit,
            "backup": str(backup),
            "firmware_local_path": str(firmware_path),
            "manifest": manifest.as_dict(),
        }

        runtime_snapshots = self._snapshot_runtime()
        previous_venv = self._venv_link().resolve(strict=True)
        self._write_transaction(
            current_commit,
            previous_venv,
            previous_record,
            runtime_snapshots,
        )
        try:
            self._run(["systemctl", "stop", "lightweave-control.service"], timeout=180)
            self._git("checkout", "--detach", manifest.commit)
            target_venv = self._install_python(manifest.commit)
            self._install_control_unit()
            self._switch_python(target_venv)
            self._write_running_commit(manifest.commit)
            _atomic_write(
                self._current_record_path(),
                json.dumps(record, sort_keys=True, indent=2).encode() + b"\n",
            )
            self._set_service_readable(self._current_record_path())
            self._run(["systemctl", "daemon-reload"])
            self._run(["systemctl", "start", "lightweave-control.service"])
            self._healthcheck(manifest.commit)
            self._install_gitops_runtime()
            self._run(["systemctl", "daemon-reload"])
            self._run(["systemctl", "enable", "--now", "lightweave-gitops.timer"])
            self._write_history(record)
            _sync_filesystems()
            _durable_unlink(self._transaction_path())
            return {"status": "deployed", "release": manifest.release, "commit": manifest.commit}
        except Exception as deploy_error:
            try:
                with contextlib.suppress(Exception):
                    self._run(["systemctl", "stop", "lightweave-control.service"], timeout=180)
                self._git("checkout", "--detach", current_commit)
                self._install_control_unit()
                self._switch_python(previous_venv)
                self._write_running_commit(current_commit)
                self._restore_record(previous_record)
                self._restore_runtime(runtime_snapshots)
                self._run(["systemctl", "daemon-reload"])
                self._run(["systemctl", "start", "lightweave-control.service"])
                self._healthcheck(current_commit)
                _sync_filesystems()
                _durable_unlink(self._transaction_path())
            except Exception as rollback_error:
                raise ReconcileError(
                    f"deployment failed ({deploy_error}); rollback also failed ({rollback_error})"
                ) from rollback_error
            raise ReconcileError(f"deployment failed and was rolled back: {deploy_error}") from deploy_error


def _acquire_process_lock(lock: Any, *, recover_only: bool) -> bool:
    flags = fcntl.LOCK_EX if recover_only else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(lock, flags)
    except BlockingIOError as error:
        if recover_only:
            raise ReconcileError("boot recovery lock is unexpectedly unavailable") from error
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile the Pi to the approved Lightweave release")
    parser.add_argument("--check", action="store_true", help="validate desired state without deploying")
    parser.add_argument(
        "--recover-only",
        action="store_true",
        help="restore any interrupted deployment without starting the control service",
    )
    args = parser.parse_args()
    config = ReconcileConfig.from_environ(os.environ)
    reconciler = GitOpsReconciler(config, load_releases=not args.recover_only)
    lock_path = Path("/run/lock/lightweave-gitops.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        if not _acquire_process_lock(lock, recover_only=args.recover_only):
            print("lightweave GitOps reconciliation is already running", file=sys.stderr)
            return 0
        if args.recover_only:
            result = reconciler.recover_before_control_start()
        elif args.check:
            manifest = reconciler.desired_manifest()
            result = {"status": "disabled"} if manifest is None else {
                "status": "valid",
                "release": manifest.release,
                "commit": manifest.commit,
            }
        else:
            result = reconciler.reconcile()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
