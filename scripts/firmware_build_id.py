from __future__ import annotations

import subprocess
import sys
from pathlib import Path

Import("env")

sys.path.insert(0, str(Path(env.subst("$PROJECT_DIR")) / "scripts"))
from firmware_identity import reported_version


def _git(args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True).strip()
    except Exception:
        return ""


short = _git(["rev-parse", "--short=8", "HEAD"])
build_id = short if len(short) == 8 else "00000000"
base_version = Path("VERSION").read_text(encoding="utf-8").strip() or "0.0.0"

dirty_paths = [
    "VERSION",
    "include",
    "src",
    "platformio.ini",
    "scripts/firmware_build_id.py",
    "scripts/firmware_identity.py",
]
dirty = bool(_git(["status", "--porcelain", "--", *dirty_paths]))
version = reported_version(
    base_version,
    _git(["tag", "--points-at", "HEAD"]),
    dirty=dirty,
)

env.Append(
    CPPDEFINES=[
        ("FIRMWARE_BUILD_ID", f"0x{build_id}u"),
        ("FIRMWARE_BUILD_DIRTY", "1" if dirty else "0"),
        ("FIRMWARE_VERSION", f'\\"{version}\\"'),
    ]
)
