from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


TRUE = "true"
FALSE = "false"


@dataclass(frozen=True)
class RemoteSettings:
    conductor_mode: str
    password_hash: str | None
    allowed_origins: frozenset[str]
    allow_network_changes: bool
    require_https: bool
    data_dir: Path | None

    @property
    def serial_mode(self) -> bool:
        return self.conductor_mode in {"serial", "local-serial"}

    @property
    def remote_serial_mode(self) -> bool:
        return self.conductor_mode == "serial"

    @property
    def local_serial_mode(self) -> bool:
        return self.conductor_mode == "local-serial"


def _parse_bool(name: str, value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    if value == TRUE:
        return True
    if value == FALSE:
        return False
    raise RuntimeError(f"{name} must be exactly 'true' or 'false'")


def _parse_origins(value: str | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    origins: set[str] = set()
    for raw in value.split(","):
        origin = raw.strip()
        if not origin:
            raise RuntimeError("CONTROL_ALLOWED_ORIGINS contains an empty origin")
        parsed = urlsplit(origin)
        try:
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise RuntimeError(
                f"CONTROL_ALLOWED_ORIGINS contains an invalid origin: {origin!r}"
            ) from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not hostname
            or any(character.isspace() for character in parsed.netloc)
            or (port is not None and not 1 <= port <= 65535)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or origin != f"{parsed.scheme}://{parsed.netloc}"
        ):
            raise RuntimeError(f"CONTROL_ALLOWED_ORIGINS contains an invalid origin: {origin!r}")
        origins.add(origin)
    return frozenset(origins)


def load_remote_settings(environ: Mapping[str, str]) -> RemoteSettings:
    conductor_mode = environ.get("CONTROL_CONDUCTOR", "mock").strip().lower()
    if conductor_mode not in {"mock", "serial", "local-serial"}:
        raise RuntimeError(f"unknown CONTROL_CONDUCTOR={conductor_mode!r}")

    remote_serial_mode = conductor_mode == "serial"
    serial_mode = conductor_mode in {"serial", "local-serial"}
    password_hash = environ.get("CONTROL_PASSWORD_HASH")
    if password_hash is not None and not password_hash:
        raise RuntimeError("CONTROL_PASSWORD_HASH must not be empty")
    if remote_serial_mode and password_hash is None:
        raise RuntimeError("CONTROL_PASSWORD_HASH is required when CONTROL_CONDUCTOR=serial")

    allowed_origins = _parse_origins(environ.get("CONTROL_ALLOWED_ORIGINS"))
    if remote_serial_mode and not allowed_origins:
        raise RuntimeError("CONTROL_ALLOWED_ORIGINS is required when CONTROL_CONDUCTOR=serial")
    if remote_serial_mode and any(
        not origin.startswith("https://") for origin in allowed_origins
    ):
        raise RuntimeError("serial-mode CONTROL_ALLOWED_ORIGINS must use https")

    allow_network_changes = _parse_bool(
        "CONTROL_ALLOW_NETWORK_CHANGES",
        environ.get("CONTROL_ALLOW_NETWORK_CHANGES"),
        default=not serial_mode,
    )
    raw_require_https = environ.get("CONTROL_REQUIRE_HTTPS")
    if remote_serial_mode and raw_require_https is None:
        raise RuntimeError("CONTROL_REQUIRE_HTTPS=true is required when CONTROL_CONDUCTOR=serial")
    require_https = _parse_bool(
        "CONTROL_REQUIRE_HTTPS",
        raw_require_https,
        default=remote_serial_mode,
    )
    if remote_serial_mode and not require_https:
        raise RuntimeError("CONTROL_REQUIRE_HTTPS cannot be false when CONTROL_CONDUCTOR=serial")

    raw_data_dir = environ.get("CONTROL_DATA_DIR")
    if serial_mode and not raw_data_dir:
        raise RuntimeError("CONTROL_DATA_DIR is required when CONTROL_CONDUCTOR=serial")
    data_dir = Path(raw_data_dir) if raw_data_dir else None
    if serial_mode and data_dir is not None and not data_dir.is_absolute():
        raise RuntimeError("CONTROL_DATA_DIR must be absolute when CONTROL_CONDUCTOR=serial")

    return RemoteSettings(
        conductor_mode=conductor_mode,
        password_hash=password_hash,
        allowed_origins=allowed_origins,
        allow_network_changes=allow_network_changes,
        require_https=require_https,
        data_dir=data_dir,
    )


def select_client_ip(peer_ip: str, forwarded_ip: str | None) -> str:
    peer = ipaddress.ip_address(peer_ip)
    if not peer.is_loopback or forwarded_ip is None:
        return peer.compressed
    try:
        return ipaddress.ip_address(forwarded_ip).compressed
    except ValueError:
        return peer.compressed


def select_external_scheme(
    peer_ip: str,
    direct_scheme: str,
    forwarded_proto: str | None,
) -> str:
    scheme = {"ws": "http", "wss": "https"}.get(direct_scheme, direct_scheme)
    if (
        ipaddress.ip_address(peer_ip).is_loopback
        and forwarded_proto in {"http", "https"}
    ):
        return forwarded_proto
    return scheme
