#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from control.releases import (
    canonical_release_asset_url,
    parse_release_channel,
    parse_release_manifest,
)


# The project moved from underminedsk/ to moda-labs/ after v0.7.0. Release
# manifests are immutable, so releases published before the move still name the
# old remote. Both stay allowed until every promoted release names moda-labs.
ALLOWED_REPOSITORIES = (
    "https://github.com/moda-labs/lightweave.git",
    "https://github.com/underminedsk/lightweave.git",
)


def canonical_manifest_url(manifest) -> str:
    if manifest.repository not in ALLOWED_REPOSITORIES:
        raise ValueError("release manifest repository is not allowed")
    return canonical_release_asset_url(
        manifest.repository, manifest.release, "lightweave-release.json"
    )


def read_manifest(source: str) -> tuple[bytes, str]:
    parsed = urlsplit(source)
    if parsed.scheme:
        if parsed.scheme != "https":
            raise ValueError("manifest URL must use HTTPS")
        request = urllib.request.Request(source, headers={"User-Agent": "lightweave-release/1"})
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read(128 * 1024 + 1)
        if len(data) > 128 * 1024:
            raise ValueError("manifest exceeds 128 KiB")
        return data, source
    path = Path(source)
    data = path.read_bytes()
    if len(data) > 128 * 1024:
        raise ValueError("manifest exceeds 128 KiB")
    document = json.loads(data)
    manifest = parse_release_manifest(document)
    return data, canonical_manifest_url(manifest)


def promoted_channel(manifest_bytes: bytes, manifest_url: str) -> dict:
    manifest = parse_release_manifest(json.loads(manifest_bytes))
    if manifest_url != canonical_manifest_url(manifest):
        raise ValueError("manifest URL must be the immutable Lightweave GitHub release asset")
    channel = {
        "schema_version": 1,
        "enabled": True,
        "manifest_url": manifest_url,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    return parse_release_channel(channel)


def main() -> int:
    parser = argparse.ArgumentParser(description="Point a Lightweave channel at an immutable release")
    parser.add_argument("manifest", help="local manifest file or immutable HTTPS release URL")
    parser.add_argument(
        "--channel",
        type=Path,
        default=REPO_ROOT / "deploy" / "channels" / "production.json",
    )
    args = parser.parse_args()
    manifest_bytes, manifest_url = read_manifest(args.manifest)
    channel = promoted_channel(manifest_bytes, manifest_url)
    args.channel.parent.mkdir(parents=True, exist_ok=True)
    args.channel.write_text(json.dumps(channel, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"promoted {manifest_url} in {args.channel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
