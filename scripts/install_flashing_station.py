#!/usr/bin/env python3
"""Install or remove the per-user macOS Lightweave USB provisioner."""

from __future__ import annotations

import argparse
import os
import plistlib
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any


LABEL = "com.lightweave.provisioner"
LEGACY_LABEL = "com.lightweave.firebeetle-autoflash"


def build_plist(
    *,
    python: Path,
    repository: Path,
    state_dir: Path,
    conductor_port: str = "",
    authority_url: str = "",
) -> dict[str, Any]:
    environment = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PROVISIONER_STATE_DIR": str(state_dir),
    }
    if conductor_port:
        environment["CONTROL_SERIAL_PORT"] = conductor_port
    if authority_url:
        environment["PROVISIONER_ID_AUTHORITY_URL"] = authority_url
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "uvicorn",
            "control.provisioner:app",
            "--uds",
            str(state_dir / "provisioner.sock"),
            "--workers",
            "1",
            "--no-proxy-headers",
        ],
        "WorkingDirectory": str(repository),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(state_dir / "provisioner.log"),
        "StandardErrorPath": str(state_dir / "provisioner.log"),
    }


def _run_launchctl(*arguments: str, check: bool = True) -> None:
    subprocess.run(["launchctl", *arguments], check=check)


def retire_legacy_agent(home: Path, *, uid: int) -> bool:
    plist = home / "Library/LaunchAgents" / f"{LEGACY_LABEL}.plist"
    if not plist.exists():
        return False
    _run_launchctl("bootout", f"gui/{uid}", str(plist), check=False)
    plist.unlink()
    return True


def install(*, conductor_port: str, authority_url: str) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("the LaunchAgent installer is only supported on macOS")
    repository = Path(__file__).resolve().parents[1]
    state_dir = Path.home() / "Library/Application Support/Lightweave/provisioner"
    agents_dir = Path.home() / "Library/LaunchAgents"
    plist_path = agents_dir / f"{LABEL}.plist"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    agents_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    token_path = state_dir / "token"
    if not token_path.exists():
        token_path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    os.chmod(token_path, 0o600)
    document = build_plist(
        python=Path(sys.executable).resolve(),
        repository=repository,
        state_dir=state_dir,
        conductor_port=conductor_port,
        authority_url=authority_url,
    )
    temporary = plist_path.with_suffix(".tmp")
    temporary.write_bytes(plistlib.dumps(document, sort_keys=True))
    os.chmod(temporary, 0o600)
    temporary.replace(plist_path)
    domain = f"gui/{os.getuid()}"
    if retire_legacy_agent(Path.home(), uid=os.getuid()):
        print(f"Removed the conflicting legacy {LEGACY_LABEL} LaunchAgent")
    _run_launchctl("bootout", domain, str(plist_path), check=False)
    _run_launchctl("bootstrap", domain, str(plist_path))
    _run_launchctl("enable", f"{domain}/{LABEL}")
    print(f"Installed {LABEL}; socket and logs: {state_dir}")


def uninstall() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("the LaunchAgent installer is only supported on macOS")
    plist_path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    _run_launchctl("bootout", f"gui/{os.getuid()}", str(plist_path), check=False)
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass
    print(f"Removed {LABEL}; provisioning history and token were preserved")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    install_parser = subcommands.add_parser("install")
    install_parser.add_argument(
        "--conductor-port",
        default="",
        help="serial path to exclude when the conductor is attached to this Mac",
    )
    install_parser.add_argument(
        "--authority-url",
        default="",
        help="HTTPS reserve-ID endpoint when the authoritative conductor is remote",
    )
    subcommands.add_parser("uninstall")
    arguments = parser.parse_args()
    if arguments.command == "install":
        install(
            conductor_port=arguments.conductor_port,
            authority_url=arguments.authority_url,
        )
    else:
        uninstall()


if __name__ == "__main__":
    main()
