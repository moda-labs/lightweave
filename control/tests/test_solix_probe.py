from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pytest

import control.solix_probe as probe_module
from control.solix_probe import (
    MODEL_CODE,
    MqttReadingBridge,
    SolixCloudError,
    SolixCredentials,
    _publish_status_request,
    run_session,
    select_s2000_device,
)
from control.solix_status import SolixStatusStore


def _mqtt_values(**overrides: Any) -> dict[str, Any]:
    values = {
        "output_power_total": 384,
        "input_power_total": 850,
        "ac_output_power": 300,
        "usb_power": 84,
        "ac_input_power": 600,
        "dc_input_power_total": 250,
        "battery_soc": 73,
        "temperature": 31,
    }
    values.update(overrides)
    return values


def _args(status_file: Path, **overrides: Any) -> argparse.Namespace:
    values = {
        "device_sn": "",
        "status_file": status_file,
        "subscription_delay": 0.0,
        "poll_interval": 5.0,
        "telemetry_timeout": 0.25,
        "reconnect_delay": 0.01,
        "once": True,
        "user": "owner@example.com",
        "password": "secret",
        "country": "US",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_credentials_are_validated_without_exposing_password() -> None:
    credentials = SolixCredentials.from_values(" owner@example.com ", "secret", "us")

    assert credentials.user == "owner@example.com"
    assert credentials.country == "US"
    assert "secret" not in repr(credentials)
    with pytest.raises(SolixCloudError, match="ANKERPASSWORD"):
        SolixCredentials.from_values("owner@example.com", "", "US")
    with pytest.raises(SolixCloudError, match="two-letter"):
        SolixCredentials.from_values("owner@example.com", "secret", "USA")


def test_device_selection_requires_one_owned_as220() -> None:
    device = select_s2000_device(
        {
            "other": {"device_sn": "other", "device_pn": "A1783"},
            "station": {
                "device_sn": "station",
                "device_pn": MODEL_CODE,
                "is_admin": True,
            },
        }
    )
    assert device["device_sn"] == "station"

    with pytest.raises(SolixCloudError, match="multiple"):
        select_s2000_device(
            {
                "one": {"device_pn": MODEL_CODE},
                "two": {"device_pn": MODEL_CODE},
            }
        )
    assert (
        select_s2000_device(
            {
                "one": {"device_pn": MODEL_CODE},
                "two": {"device_pn": MODEL_CODE},
            },
            "two",
        )["device_sn"]
        == "two"
    )


def test_device_selection_rejects_member_account_and_missing_station() -> None:
    with pytest.raises(SolixCloudError, match="owner account"):
        select_s2000_device(
            {"station": {"device_pn": MODEL_CODE, "is_admin": False}}
        )
    with pytest.raises(SolixCloudError, match="does not own"):
        select_s2000_device({"other": {"device_pn": "A1783"}})


def test_mqtt_bridge_filters_partial_and_other_device_messages() -> None:
    async def exercise() -> None:
        queue: asyncio.Queue[tuple[dict[str, Any], float]] = asyncio.Queue(maxsize=1)
        bridge = MqttReadingBridge(
            loop=asyncio.get_running_loop(),
            serial="station",
            queue=queue,
            clock=lambda: 1_700_000_000,
        )
        bridge(None, "topic", {}, b"", MODEL_CODE, "other", _mqtt_values())
        bridge(None, "topic", {}, b"", MODEL_CODE, "station", {"battery_soc": 50})
        await asyncio.sleep(0)
        assert queue.empty()

        bridge(None, "topic", {}, b"", MODEL_CODE, "station", _mqtt_values())
        await asyncio.sleep(0)
        reading, received_at = queue.get_nowait()
        assert reading["output_w"] == 384
        assert reading["plausible"] is True
        assert received_at == 1_700_000_000

    asyncio.run(exercise())


class _PublishResult:
    def __init__(self, published: bool = True) -> None:
        self.published = published

    def is_published(self) -> bool:
        return self.published


class _FakeMqtt:
    def __init__(self, *, emit: bool = True, connected: bool = True) -> None:
        self.emit = emit
        self.connected = connected
        self.callback = None
        self.subscriptions: list[str] = []
        self.status_requests = 0

    def is_connected(self) -> bool:
        return self.connected

    def get_topic_prefix(self, *, deviceDict: dict[str, Any]) -> str:
        return f"dt/app/{deviceDict['device_pn']}/{deviceDict['device_sn']}/"

    def subscribe(self, topic: str) -> None:
        self.subscriptions.append(topic)
        return None

    def status_request(self, *, deviceDict: dict[str, Any], wait_for_publish: int):
        assert wait_for_publish == 5
        self.status_requests += 1
        if self.emit and self.callback is not None:
            self.callback(
                self,
                "topic",
                {},
                b"",
                MODEL_CODE,
                deviceDict["device_sn"],
                _mqtt_values(),
            )
        return _PublishResult()


class _FakeApi:
    def __init__(self, mqtt: _FakeMqtt) -> None:
        self.mqtt = mqtt
        self.devices = {
            "station": {
                "device_sn": "station",
                "device_pn": MODEL_CODE,
                "is_admin": True,
            }
        }
        self.calls: list[str] = []

    async def async_authenticate(self) -> bool:
        self.calls.append("authenticate")
        return True

    async def update_sites(self) -> None:
        self.calls.append("sites")

    async def get_bind_devices(self) -> None:
        self.calls.append("devices")

    async def startMqttSession(self, *, message_callback):
        self.calls.append("mqtt")
        self.mqtt.callback = message_callback
        return self.mqtt

    def stopMqttSession(self) -> None:
        self.calls.append("stop")


class _WebSession:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args) -> None:
        return None


def test_cloud_session_requests_and_persists_real_power(tmp_path: Path) -> None:
    async def exercise() -> None:
        mqtt = _FakeMqtt()
        api = _FakeApi(mqtt)
        store = SolixStatusStore(tmp_path / "solix.json")
        credentials = SolixCredentials.from_values("owner@example.com", "secret", "US")

        await run_session(
            _args(store.path),
            store,
            credentials,
            api_factory=lambda *_args: api,
            websession_factory=_WebSession,
        )

        status = store.load()
        assert status["connected"] is True
        assert status["source"] == "anker_mqtt"
        assert status["address"] == "station"
        assert status["output_w"] == 384
        assert mqtt.subscriptions == ["dt/app/AS220/station/#"]
        assert mqtt.status_requests == 1
        assert api.calls == ["authenticate", "sites", "devices", "mqtt", "stop"]

    asyncio.run(exercise())


def test_cloud_session_times_out_without_complete_telemetry(tmp_path: Path) -> None:
    async def exercise() -> None:
        mqtt = _FakeMqtt(emit=False)
        api = _FakeApi(mqtt)
        store = SolixStatusStore(tmp_path / "solix.json")
        credentials = SolixCredentials.from_values("owner@example.com", "secret", "US")

        with pytest.raises(TimeoutError, match="S2000 MQTT telemetry"):
            await run_session(
                _args(store.path, telemetry_timeout=0.01),
                store,
                credentials,
                api_factory=lambda *_args: api,
                websession_factory=_WebSession,
            )
        assert api.calls[-1] == "stop"

    asyncio.run(exercise())


def test_status_publish_rejects_disconnected_or_unpublished_request() -> None:
    with pytest.raises(SolixCloudError, match="connection closed"):
        _publish_status_request(_FakeMqtt(connected=False), {"device_sn": "station"})

    mqtt = _FakeMqtt()
    mqtt.status_request = lambda **_kwargs: _PublishResult(False)  # type: ignore[method-assign]
    with pytest.raises(SolixCloudError, match="not published"):
        _publish_status_request(mqtt, {"device_sn": "station"})


def test_once_mode_records_cloud_failure_without_losing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*_args, **_kwargs) -> None:
        raise RuntimeError("cloud unavailable")

    monkeypatch.setattr(probe_module, "run_session", fail)
    args = _args(tmp_path / "solix.json")

    with pytest.raises(RuntimeError, match="cloud unavailable"):
        asyncio.run(probe_module.run(args))

    status = SolixStatusStore(args.status_file).load()
    assert status["source"] == "anker_mqtt"
    assert status["error"] == "Anker cloud operation failed (RuntimeError)"
    assert "secret" not in str(status)
