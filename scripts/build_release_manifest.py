#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from control.releases import load_release_catalog, parse_release_manifest


def firmware_protocol() -> int:
    source = (REPO_ROOT / "include" / "beacon.h").read_text(encoding="utf-8")
    match = re.search(
        r"^static constexpr uint8_t PROTO_VERSION = ([0-9]+);$",
        source,
        re.MULTILINE,
    )
    if match is None:
        raise ValueError("include/beacon.h does not define PROTO_VERSION")
    return int(match.group(1))


def build_manifest(
    *,
    firmware: Path,
    serial_flash: Path,
    repository: str,
    commit: str,
    tag: str,
    artifact_url: str,
    serial_flash_url: str,
    published_at: str,
) -> dict:
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if tag != f"v{version}":
        raise ValueError(f"tag {tag!r} does not match VERSION {version!r}")
    notes = {item.version: item for item in load_release_catalog(REPO_ROOT / "RELEASES.json")}
    if version not in notes:
        raise ValueError(f"RELEASES.json has no notes for VERSION {version}")
    firmware_bytes = firmware.read_bytes()
    serial_flash_bytes = serial_flash.read_bytes()
    document = {
        "schema_version": 1,
        "release": tag,
        "version": version,
        "repository": repository,
        "ref": f"refs/tags/{tag}",
        "commit": commit.lower(),
        "published_at": published_at,
        "notes": notes[version].as_dict(),
        "firmware": {
            "filename": firmware.name,
            "url": artifact_url,
            "sha256": hashlib.sha256(firmware_bytes).hexdigest(),
            "size": len(firmware_bytes),
            "crc32": zlib.crc32(firmware_bytes) & 0xFFFFFFFF,
            "protocol": firmware_protocol(),
        },
        "serial_flash": {
            "filename": serial_flash.name,
            "url": serial_flash_url,
            "sha256": hashlib.sha256(serial_flash_bytes).hexdigest(),
            "size": len(serial_flash_bytes),
        },
    }
    return parse_release_manifest(document).as_dict()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a verified Lightweave release manifest")
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--serial-flash", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--artifact-url", required=True)
    parser.add_argument("--serial-flash-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--published-at",
        default=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    args = parser.parse_args()
    document = build_manifest(
        firmware=args.firmware,
        serial_flash=args.serial_flash,
        repository=args.repository,
        commit=args.commit,
        tag=args.tag,
        artifact_url=args.artifact_url,
        serial_flash_url=args.serial_flash_url,
        published_at=args.published_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
