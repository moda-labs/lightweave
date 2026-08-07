#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path


SEGMENTS = (
    (0x1000, "bootloader.bin"),
    (0x8000, "partitions.bin"),
    (0xE000, "boot_app0.bin"),
    (0x10000, "firmware.bin"),
)


def build_bundle(
    *,
    inputs: dict[str, Path],
    esptool_dir: Path,
    esptool_license: Path,
    output: Path,
) -> dict:
    members: dict[str, bytes] = {}
    segments = []
    for offset, filename in SEGMENTS:
        data = inputs[filename].read_bytes()
        if not data:
            raise ValueError(f"{filename} is empty")
        members[filename] = data
        segments.append(
            {
                "filename": filename,
                "offset": offset,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    tool_members = []
    for source in sorted(esptool_dir.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
            continue
        relative = source.relative_to(esptool_dir).as_posix()
        filename = f"esptool/{relative}"
        data = source.read_bytes()
        members[filename] = data
        tool_members.append(
            {
                "filename": filename,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    if not tool_members or not any(item["filename"] == "esptool/__main__.py" for item in tool_members):
        raise ValueError("esptool package is incomplete")
    contrib_dir = esptool_dir.parent / "_contrib"
    intelhex_dir = contrib_dir / "intelhex"
    intelhex_licenses = sorted(
        contrib_dir.glob("intelhex-*.dist-info/LICENSE.txt")
    )
    if not intelhex_dir.is_dir() or len(intelhex_licenses) != 1:
        raise ValueError("esptool IntelHex dependency is incomplete")
    for source in sorted(intelhex_dir.rglob("*")):
        if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
            continue
        relative = source.relative_to(intelhex_dir).as_posix()
        filename = f"intelhex/{relative}"
        data = source.read_bytes()
        members[filename] = data
        tool_members.append(
            {
                "filename": filename,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    if not any(item["filename"] == "intelhex/__init__.py" for item in tool_members):
        raise ValueError("esptool IntelHex dependency is incomplete")
    license_data = esptool_license.read_bytes()
    if not license_data:
        raise ValueError("esptool license is empty")
    members["esptool-LICENSE"] = license_data
    intelhex_license_data = intelhex_licenses[0].read_bytes()
    if not intelhex_license_data:
        raise ValueError("IntelHex license is empty")
    members["intelhex-LICENSE"] = intelhex_license_data
    members["esptool.py"] = (
        b"#!/usr/bin/env python3\n"
        b"import esptool\n"
        b"if __name__ == '__main__':\n"
        b"    esptool._main()\n"
    )
    tool_members.extend(
        {
            "filename": filename,
            "sha256": hashlib.sha256(members[filename]).hexdigest(),
            "size": len(members[filename]),
        }
        for filename in ("esptool-LICENSE", "esptool.py", "intelhex-LICENSE")
    )
    tool_members.sort(key=lambda item: item["filename"])
    plan = {
        "schema_version": 3,
        "chip": "esp32",
        "flash_size": "4MB",
        "flash_mode": "dio",
        "flash_freq": "40m",
        "segments": segments,
        "tool": {"name": "esptool", "members": tool_members},
    }
    members["flash-plan.json"] = (
        json.dumps(plan, indent=2, sort_keys=True) + "\n"
    ).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for filename in sorted(members):
            info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, members[filename])
    return plan


def verify_bundle_runtime(bundle: Path) -> None:
    from firebeetle_autoflash import esptool_command, extract_bundle, run_tool

    with tempfile.TemporaryDirectory(prefix="lightweave-bundle-") as temporary:
        destination = Path(temporary)
        extract_bundle(bundle, destination)
        run_tool(esptool_command(destination) + ["version"], timeout_s=30)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a deterministic serial flash bundle")
    parser.add_argument("--bootloader", type=Path, required=True)
    parser.add_argument("--partitions", type=Path, required=True)
    parser.add_argument("--boot-app", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--esptool-dir", type=Path, required=True)
    parser.add_argument("--esptool-license", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_bundle(
        inputs={
            "bootloader.bin": args.bootloader,
            "partitions.bin": args.partitions,
            "boot_app0.bin": args.boot_app,
            "firmware.bin": args.firmware,
        },
        esptool_dir=args.esptool_dir,
        esptool_license=args.esptool_license,
        output=args.output,
    )
    verify_bundle_runtime(args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
