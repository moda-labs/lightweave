#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


SEGMENTS = (
    (0x1000, "bootloader.bin"),
    (0x8000, "partitions.bin"),
    (0xE000, "boot_app0.bin"),
    (0x10000, "firmware.bin"),
)


def build_bundle(*, inputs: dict[str, Path], output: Path) -> dict:
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
    plan = {
        "schema_version": 1,
        "chip": "esp32",
        "flash_size": "4MB",
        "flash_mode": "dio",
        "flash_freq": "40m",
        "segments": segments,
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a deterministic serial flash bundle")
    parser.add_argument("--bootloader", type=Path, required=True)
    parser.add_argument("--partitions", type=Path, required=True)
    parser.add_argument("--boot-app", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_bundle(
        inputs={
            "bootloader.bin": args.bootloader,
            "partitions.bin": args.partitions,
            "boot_app0.bin": args.boot_app,
            "firmware.bin": args.firmware,
        },
        output=args.output,
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
