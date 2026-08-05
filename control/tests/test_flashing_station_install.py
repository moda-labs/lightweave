import plistlib
from pathlib import Path

from scripts import install_flashing_station as installer
from scripts.install_flashing_station import LABEL, build_plist


REPO_ROOT = Path(__file__).parents[2]


def test_launch_agent_uses_current_release_and_private_station_paths(tmp_path: Path) -> None:
    repository = tmp_path / "checkout"
    python = repository / ".venv/bin/python"
    state = tmp_path / "state"

    document = build_plist(
        python=python,
        repository=repository,
        state_dir=state,
        conductor_port="/dev/cu.conductor",
        authority_url="https://control.example.test/api/internal/provisioning/reserve-id",
    )

    assert document["Label"] == LABEL
    assert document["WorkingDirectory"] == str(repository)
    assert document["ProgramArguments"][:4] == [
        str(python),
        "-m",
        "control.provisioner",
        "--socket",
    ]
    assert document["ProgramArguments"][4] == str(state / "provisioner.sock")
    assert document["EnvironmentVariables"]["CONTROL_SERIAL_PORT"] == "/dev/cu.conductor"
    assert document["EnvironmentVariables"]["PROVISIONER_ID_AUTHORITY_URL"].startswith(
        "https://control.example.test/"
    )
    assert "PROVISIONER_TOKEN" not in document["EnvironmentVariables"]


def test_install_retires_conflicting_legacy_watcher(
    tmp_path: Path, monkeypatch
) -> None:
    plist = (
        tmp_path
        / "Library/LaunchAgents"
        / f"{installer.LEGACY_LABEL}.plist"
    )
    plist.parent.mkdir(parents=True)
    plist.write_text("legacy")
    calls = []
    monkeypatch.setattr(
        installer,
        "_run_launchctl",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(installer, "_launchctl_loaded", lambda _target: False)

    assert installer.retire_legacy_agent(tmp_path, uid=501) is True
    assert not plist.exists()
    assert calls == [
        (("bootout", "gui/501", str(plist)), {"check": False})
    ]


def test_legacy_agent_is_not_removed_when_it_cannot_be_stopped(
    tmp_path: Path, monkeypatch
) -> None:
    plist = tmp_path / "Library/LaunchAgents" / f"{installer.LEGACY_LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("legacy")
    monkeypatch.setattr(installer, "_run_launchctl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "_launchctl_loaded", lambda _target: True)

    try:
        installer.retire_legacy_agent(tmp_path, uid=501)
    except RuntimeError as error:
        assert "could not stop" in str(error)
    else:
        raise AssertionError("loaded legacy agent must block installation")
    assert plist.exists()


def test_install_creates_private_reusable_token_and_loads_agent(
    tmp_path: Path, monkeypatch
) -> None:
    calls = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(installer.os, "getuid", lambda: 501)
    monkeypatch.setattr(installer.secrets, "token_hex", lambda _size: "a" * 64)
    monkeypatch.setattr(installer, "_launchctl_loaded", lambda _target: False)
    monkeypatch.setattr(
        installer,
        "_run_launchctl",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    installer.install(conductor_port="/dev/cu.conductor", authority_url="")

    state = tmp_path / "Library/Application Support/Lightweave/provisioner"
    token = state / "token"
    plist = tmp_path / "Library/LaunchAgents" / f"{installer.LABEL}.plist"
    assert token.read_text() == "a" * 64 + "\n"
    assert token.stat().st_mode & 0o777 == 0o600
    assert state.stat().st_mode & 0o777 == 0o700
    document = plistlib.loads(plist.read_bytes())
    assert document["EnvironmentVariables"]["CONTROL_SERIAL_PORT"] == "/dev/cu.conductor"
    assert calls == [
        (("bootout", "gui/501", str(plist)), {"check": False}),
        (("bootstrap", "gui/501", str(plist)), {}),
        (("enable", "gui/501/com.lightweave.provisioner"), {}),
    ]


def test_install_preserves_virtualenv_interpreter_symlink(
    tmp_path: Path, monkeypatch
) -> None:
    base_python = tmp_path / "base-python"
    base_python.write_text("")
    venv_python = tmp_path / "checkout/.venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(base_python)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(installer.sys, "executable", str(venv_python))
    monkeypatch.setattr(installer, "_launchctl_loaded", lambda _target: False)
    monkeypatch.setattr(installer, "_run_launchctl", lambda *_args, **_kwargs: None)

    installer.install(conductor_port="", authority_url="")

    plist = (
        tmp_path
        / "home/Library/LaunchAgents"
        / f"{installer.LABEL}.plist"
    )
    document = plistlib.loads(plist.read_bytes())
    assert document["ProgramArguments"][0] == str(venv_python)


def test_pi_service_isolated_state_secrets_and_deployment_lock() -> None:
    unit = (REPO_ROOT / "deploy/pi/lightweave-provisioner.service").read_text()

    assert "UnsetEnvironment=CONTROL_PASSWORD_HASH" in unit
    assert "StateDirectory=lightweave/provisioner" in unit
    assert "ReadWritePaths=/var/lib/lightweave/provisioner" in unit
    assert "ReadWritePaths=/var/lib/lightweave\n" not in unit
    assert (
        "Environment=PROVISIONER_OPERATION_LOCK="
        "/var/lib/lightweave-gitops/firmware-ota.lock"
    ) in unit
    assert "ReadOnlyPaths=/var/lib/lightweave-gitops/firmware-ota.lock" in unit
