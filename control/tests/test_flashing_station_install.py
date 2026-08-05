import plistlib
from pathlib import Path

from scripts import install_flashing_station as installer
from scripts.install_flashing_station import LABEL, build_plist


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
        "uvicorn",
        "control.provisioner:app",
    ]
    assert document["ProgramArguments"][5] == str(state / "provisioner.sock")
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

    assert installer.retire_legacy_agent(tmp_path, uid=501) is True
    assert not plist.exists()
    assert calls == [
        (("bootout", "gui/501", str(plist)), {"check": False})
    ]


def test_install_creates_private_reusable_token_and_loads_agent(
    tmp_path: Path, monkeypatch
) -> None:
    calls = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(installer.os, "getuid", lambda: 501)
    monkeypatch.setattr(installer.secrets, "token_hex", lambda _size: "a" * 64)
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
