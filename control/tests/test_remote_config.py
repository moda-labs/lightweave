from pathlib import Path

import pytest

from control.remote_config import (
    RemoteSettings,
    load_remote_settings,
    select_client_ip,
    select_external_scheme,
)


def test_mock_defaults_are_local_development_safe() -> None:
    settings = load_remote_settings({})

    assert settings == RemoteSettings(
        conductor_mode="mock",
        password_hash=None,
        allowed_origins=frozenset(),
        allow_network_changes=True,
        require_https=False,
        data_dir=None,
    )


def test_serial_mode_requires_complete_remote_contract() -> None:
    required = {
        "CONTROL_CONDUCTOR": "serial",
        "CONTROL_PASSWORD_HASH": "encoded",
        "CONTROL_ALLOWED_ORIGINS": "https://control.example.com",
        "CONTROL_REQUIRE_HTTPS": "true",
        "CONTROL_DATA_DIR": "/var/lib/lightweave",
    }

    settings = load_remote_settings(required)

    assert settings.serial_mode is True
    assert settings.password_hash == "encoded"
    assert settings.allowed_origins == frozenset({"https://control.example.com"})
    assert settings.allow_network_changes is False
    assert settings.require_https is True
    assert settings.data_dir == Path("/var/lib/lightweave")

    for key in (
        "CONTROL_PASSWORD_HASH",
        "CONTROL_ALLOWED_ORIGINS",
        "CONTROL_REQUIRE_HTTPS",
        "CONTROL_DATA_DIR",
    ):
        incomplete = dict(required)
        incomplete.pop(key)
        with pytest.raises(RuntimeError, match=key):
            load_remote_settings(incomplete)


def test_local_serial_mode_is_loopback_only_without_remote_credentials(
    tmp_path: Path,
) -> None:
    settings = load_remote_settings(
        {
            "CONTROL_CONDUCTOR": "local-serial",
            "CONTROL_DATA_DIR": str(tmp_path),
        }
    )

    assert settings.serial_mode is True
    assert settings.local_serial_mode is True
    assert settings.remote_serial_mode is False
    assert settings.password_hash is None
    assert settings.allowed_origins == frozenset()
    assert settings.allow_network_changes is False
    assert settings.require_https is False
    assert settings.data_dir == tmp_path


@pytest.mark.parametrize("value", ["1", "TRUE", "yes", "", " false "])
def test_boolean_settings_are_strict(value: str) -> None:
    with pytest.raises(RuntimeError, match="exactly"):
        load_remote_settings({"CONTROL_ALLOW_NETWORK_CHANGES": value})


def test_serial_mode_cannot_disable_https() -> None:
    with pytest.raises(RuntimeError, match="cannot be false"):
        load_remote_settings({
            "CONTROL_CONDUCTOR": "serial",
            "CONTROL_PASSWORD_HASH": "encoded",
            "CONTROL_ALLOWED_ORIGINS": "https://control.example.com",
            "CONTROL_REQUIRE_HTTPS": "false",
            "CONTROL_DATA_DIR": "/var/lib/lightweave",
        })


@pytest.mark.parametrize(
    "origin",
    [
        "control.example.com",
        "https://control.example.com/",
        "https://user@control.example.com",
        "https://control.example.com/path",
        "https://control.example.com?query",
        "https://control.example.com#fragment",
        "https://control.example.com:notaport",
        "https://control.example.com:99999",
        "https://control .example.com",
        "https://one.example,,https://two.example",
    ],
)
def test_origins_must_be_exact_http_origins(origin: str) -> None:
    with pytest.raises(RuntimeError, match="origin"):
        load_remote_settings({"CONTROL_ALLOWED_ORIGINS": origin})


def test_serial_origins_must_be_https() -> None:
    with pytest.raises(RuntimeError, match="must use https"):
        load_remote_settings({
            "CONTROL_CONDUCTOR": "serial",
            "CONTROL_PASSWORD_HASH": "encoded",
            "CONTROL_ALLOWED_ORIGINS": "http://control.example.com",
            "CONTROL_DATA_DIR": "/var/lib/lightweave",
        })


def test_serial_data_directory_must_be_absolute() -> None:
    with pytest.raises(RuntimeError, match="must be absolute"):
        load_remote_settings({
            "CONTROL_CONDUCTOR": "serial",
            "CONTROL_PASSWORD_HASH": "encoded",
            "CONTROL_ALLOWED_ORIGINS": "https://control.example.com",
            "CONTROL_REQUIRE_HTTPS": "true",
            "CONTROL_DATA_DIR": ".control-data",
        })


def test_forwarded_headers_are_trusted_only_from_loopback() -> None:
    assert select_client_ip("127.0.0.1", "2001:0db8::0001") == "2001:db8::1"
    assert select_external_scheme("127.0.0.1", "http", "https") == "https"

    assert select_client_ip("192.0.2.10", "198.51.100.7") == "192.0.2.10"
    assert select_external_scheme("192.0.2.10", "http", "https") == "http"


def test_malformed_or_nonexact_forwarding_values_are_ignored() -> None:
    assert select_client_ip("127.0.0.1", "not-an-ip") == "127.0.0.1"
    assert select_external_scheme("127.0.0.1", "http", "https,http") == "http"
    assert select_external_scheme("127.0.0.1", "wss", None) == "https"
