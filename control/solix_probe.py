from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .solix_status import (
    SOLIX_SOURCE_MQTT,
    SolixStatusError,
    SolixStatusStore,
    decode_as220_mqtt_values,
)


LOGGER = logging.getLogger(__name__)

MODEL_CODE = "AS220"
MODEL_NAME = "SOLIX S2000"


class SolixCloudError(RuntimeError):
    pass


@dataclass(frozen=True)
class SolixCredentials:
    user: str
    password: str = field(repr=False)
    country: str

    @classmethod
    def from_values(cls, user: str, password: str, country: str) -> "SolixCredentials":
        normalized_country = country.strip().upper()
        missing = [
            name
            for name, value in (
                ("ANKERUSER", user),
                ("ANKERPASSWORD", password),
                ("ANKERCOUNTRY", normalized_country),
            )
            if not value.strip()
        ]
        if missing:
            raise SolixCloudError(f"missing required setting: {', '.join(missing)}")
        if len(normalized_country) != 2 or not normalized_country.isalpha():
            raise SolixCloudError("ANKERCOUNTRY must be a two-letter country code")
        return cls(user.strip(), password, normalized_country)


def select_s2000_device(
    devices: Mapping[str, Mapping[str, Any]], requested_serial: str = ""
) -> dict[str, Any]:
    requested_serial = requested_serial.strip()
    candidates = []
    for key, raw_device in devices.items():
        device = dict(raw_device)
        serial = str(device.get("device_sn") or key)
        model = str(device.get("device_pn") or device.get("product_code") or "")
        if model == MODEL_CODE and (not requested_serial or serial == requested_serial):
            device["device_sn"] = serial
            device["device_pn"] = model
            candidates.append(device)

    if requested_serial and not candidates:
        raise SolixCloudError(
            f"Anker account does not own an {MODEL_CODE} device with serial {requested_serial}"
        )
    if not candidates:
        raise SolixCloudError(f"Anker account does not own a {MODEL_NAME} ({MODEL_CODE})")
    if len(candidates) > 1:
        raise SolixCloudError(
            "multiple S2000 stations found; set CONTROL_SOLIX_DEVICE_SN"
        )

    device = candidates[0]
    # Owner-only boundary: an absent is_admin flag must not pass as ownership.
    if device.get("is_admin") is not True:
        raise SolixCloudError("S2000 MQTT telemetry requires the Anker owner account")
    return device


class MqttReadingBridge:
    """Move decoded MQTT callbacks safely onto the asyncio service loop."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        serial: str,
        queue: asyncio.Queue[tuple[dict[str, Any], float]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.loop = loop
        self.serial = serial
        self.queue = queue
        self.clock = clock

    def __call__(
        self,
        _session: Any,
        _topic: str,
        _message: Any,
        _data: Any,
        model: str | None,
        device_sn: str | None,
        extracted_values: Any,
    ) -> None:
        if model != MODEL_CODE or device_sn != self.serial:
            return
        if not isinstance(extracted_values, dict):
            return
        try:
            reading = decode_as220_mqtt_values(extracted_values)
        except SolixStatusError:
            # AS220 sends several message types; only 0421/0900 has the complete
            # power snapshot required by the Overview card.
            return
        self.loop.call_soon_threadsafe(self._offer_latest, reading, self.clock())

    def _offer_latest(self, reading: dict[str, Any], received_at: float) -> None:
        if self.queue.full():
            with suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        self.queue.put_nowait((reading, received_at))


def _build_api(credentials: SolixCredentials, websession: Any) -> Any:
    from anker_solix_api.api import AnkerSolixApi

    return AnkerSolixApi(
        credentials.user,
        credentials.password,
        credentials.country,
        websession,
        LOGGER,
    )


def _client_session() -> Any:
    from aiohttp import ClientSession

    return ClientSession()


def _force_mqtt_client_cleanup(client: Any) -> None:
    """Stop the Paho network thread even when the session is not connected.

    The pinned upstream cleanup() calls disconnect()/loop_stop() only while
    connected, so a session that dropped its connection (or never finished
    connecting after loop_start()) would leak its network thread and callbacks
    across reconnect attempts. The caller captures the client before upstream
    cleanup() clears it from the session.
    """
    if client is None:
        return
    with suppress(Exception):
        client.disconnect()
    with suppress(Exception):
        client.loop_stop()


def _publish_status_request(mqtt_session: Any, device: dict[str, Any]) -> None:
    if not mqtt_session.is_connected():
        raise SolixCloudError("Anker MQTT connection closed")
    response = mqtt_session.status_request(deviceDict=device, wait_for_publish=5)
    try:
        published = response.is_published()
    except (RuntimeError, ValueError) as error:
        raise SolixCloudError("Anker MQTT status request failed") from error
    if not published:
        raise SolixCloudError("Anker MQTT status request was not published")


async def _request_status_loop(
    mqtt_session: Any,
    device: dict[str, Any],
    *,
    initial_delay_s: float,
    interval_s: float,
) -> None:
    await asyncio.sleep(max(0.0, initial_delay_s))
    while True:
        # Paho's network loop runs in its own thread. Keep this bounded publish
        # wait on the service loop so cancellation cannot race MQTT cleanup.
        _publish_status_request(mqtt_session, device)
        await asyncio.sleep(max(5.0, interval_s))


async def run_session(
    args: argparse.Namespace,
    store: SolixStatusStore,
    credentials: SolixCredentials,
    *,
    api_factory: Callable[[SolixCredentials, Any], Any] = _build_api,
    websession_factory: Callable[[], Any] = _client_session,
) -> None:
    async with websession_factory() as websession:
        api = api_factory(credentials, websession)
        await api.async_authenticate()
        await api.update_sites()
        await api.get_bind_devices()
        device = select_s2000_device(api.devices, args.device_sn)
        serial = str(device["device_sn"])

        queue: asyncio.Queue[tuple[dict[str, Any], float]] = asyncio.Queue(maxsize=1)
        bridge = MqttReadingBridge(
            loop=asyncio.get_running_loop(), serial=serial, queue=queue
        )
        mqtt_session = await api.startMqttSession(message_callback=bridge)
        if mqtt_session is None:
            raise SolixCloudError("could not connect to the Anker MQTT service")
        mqtt_client = getattr(mqtt_session, "client", None)
        request_task: asyncio.Task[None] | None = None
        try:
            if not mqtt_session.is_connected():
                raise SolixCloudError("could not connect to the Anker MQTT service")
            prefix = mqtt_session.get_topic_prefix(deviceDict=device)
            if not prefix:
                raise SolixCloudError("Anker MQTT service did not provide a device topic")
            subscription_error = mqtt_session.subscribe(f"{prefix}#")
            if subscription_error is not None and subscription_error.is_failure:
                raise SolixCloudError("could not subscribe to S2000 MQTT telemetry")

            request_task = asyncio.create_task(
                _request_status_loop(
                    mqtt_session,
                    device,
                    initial_delay_s=args.subscription_delay,
                    interval_s=args.poll_interval,
                )
            )
            while True:
                reading_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    (reading_task, request_task),
                    timeout=args.telemetry_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if request_task in done:
                    reading_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await reading_task
                    request_task.result()
                    raise SolixCloudError("Anker MQTT request loop stopped")
                if reading_task not in done:
                    reading_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await reading_task
                    raise TimeoutError("timed out waiting for S2000 MQTT telemetry")

                reading, received_at = reading_task.result()
                store.write_reading(
                    reading,
                    address=serial,
                    source=SOLIX_SOURCE_MQTT,
                    updated_at=received_at,
                )
                LOGGER.info(
                    "S2000 cloud power output=%.0fW input=%.0fW soc=%.0f%%",
                    reading["output_w"],
                    reading["input_w"],
                    reading["soc_percent"],
                )
                if args.once:
                    return
        finally:
            if request_task is not None:
                request_task.cancel()
                with suppress(asyncio.CancelledError):
                    await request_task
            try:
                api.stopMqttSession()
            finally:
                _force_mqtt_client_cleanup(mqtt_client)


async def run(args: argparse.Namespace) -> None:
    credentials = SolixCredentials.from_values(args.user, args.password, args.country)
    store = SolixStatusStore(args.status_file)
    while True:
        try:
            await run_session(args, store, credentials)
            if args.once:
                return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Normal-level logs must carry only the redacted public form; raw
            # third-party exception text may embed account or token details.
            public_error = _public_error(error)
            LOGGER.warning("S2000 cloud probe unavailable: %s", public_error)
            LOGGER.debug("S2000 cloud probe failure detail", exc_info=error)
            store.write_error(
                public_error,
                address=args.device_sn,
                source=SOLIX_SOURCE_MQTT,
            )
            if args.once:
                raise
        await asyncio.sleep(max(1.0, args.reconnect_delay))


def _public_error(error: Exception) -> str:
    if isinstance(error, (SolixCloudError, TimeoutError)):
        return str(error)
    return f"Anker cloud operation failed ({type(error).__name__})"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only SOLIX S2000 Anker cloud MQTT power probe"
    )
    # Credentials intentionally have no command-line form so the password can
    # never appear in process listings. systemd loads them from a root-only file.
    parser.set_defaults(
        user=os.environ.get("ANKERUSER", ""),
        password=os.environ.get("ANKERPASSWORD", ""),
        country=os.environ.get("ANKERCOUNTRY", ""),
    )
    parser.add_argument(
        "--device-sn", default=os.environ.get("CONTROL_SOLIX_DEVICE_SN", "")
    )
    parser.add_argument(
        "--status-file",
        type=Path,
        default=Path("/var/lib/lightweave/power/solix-s2000.json"),
    )
    parser.add_argument("--subscription-delay", type=float, default=1.0)
    parser.add_argument("--poll-interval", type=float, default=15.0)
    parser.add_argument("--telemetry-timeout", type=float, default=45.0)
    parser.add_argument("--reconnect-delay", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
