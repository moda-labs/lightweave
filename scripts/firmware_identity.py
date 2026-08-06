from __future__ import annotations


FIRMWARE_VERSION_MAX_BYTES = 15


def reported_version(base_version: str, tags_at_head: str, *, dirty: bool) -> str:
    """Return the on-device version without letting dev builds impersonate releases."""
    version = base_version.strip() or "0.0.0"
    exact_release = not dirty and f"v{version}" in tags_at_head.splitlines()
    reported = version if exact_release else f"{version}-dev"
    if len(reported.encode("utf-8")) > FIRMWARE_VERSION_MAX_BYTES:
        raise ValueError(
            f"firmware version {reported!r} does not fit the 15-byte wire format"
        )
    return reported
