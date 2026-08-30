from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from bleak import BleakClient, BleakScanner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .solix_status import SolixStatusStore, decode_as220_status


LOGGER = logging.getLogger(__name__)

TARGET_NAME = "SOLIX S2000"
TARGET_ADDRESS = ""
WRITE_UUID = "8c850002-0302-41c5-b46e-cf057c562025"
NOTIFY_UUID = "8c850003-0302-41c5-b46e-cf057c562025"
NEGOTIATION_PATTERN = bytes.fromhex("030001")
SESSION_PATTERN = bytes.fromhex("03000f")
STATIC_KEY = bytes.fromhex("b8ff7422955d4eb6d554a2c470280559")
STATIC_NONCE = bytes.fromhex("6ba3e3f2f3a60f2971ce5d1f")
AAD = bytes.fromhex("3322110077665544bbaa9988ffeeddcc")
TELEMETRY_COMMANDS = frozenset((bytes.fromhex("0421"), bytes.fromhex("c421")))
SUBSCRIBE_COMMAND = bytes.fromhex("4200")
REGISTER_COMMAND = bytes.fromhex("420a")


class SolixProtocolError(RuntimeError):
    pass


def timestamp() -> bytes:
    return int(time.time()).to_bytes(4, "little")


def encode_tlvs(*values: tuple[int, bytes]) -> bytes:
    payload = bytearray()
    for key, value in values:
        if not 0 <= key <= 0xFF or len(value) > 0xFF:
            raise ValueError("TLV key/value is out of range")
        payload.extend((key, len(value)))
        payload.extend(value)
    return bytes(payload)


def parse_tlvs(payload: bytes) -> dict[int, bytes]:
    offset = 1 if payload.startswith(b"\x00") else 0
    values: dict[int, bytes] = {}
    while offset < len(payload):
        if offset + 2 > len(payload):
            raise SolixProtocolError("truncated TLV header")
        key = payload[offset]
        length = payload[offset + 1]
        offset += 2
        end = offset + length
        if end > len(payload):
            raise SolixProtocolError(f"truncated TLV {key:02x}")
        values[key] = payload[offset:end]
        offset = end
    return values


def build_frame(pattern: bytes, command: bytes, payload: bytes) -> bytes:
    if len(pattern) != 3 or len(command) != 2:
        raise ValueError("invalid pattern or command length")
    content = b"\xff\x09" + (10 + len(payload)).to_bytes(2, "little")
    content += pattern + command + payload
    checksum = 0
    for byte in content:
        checksum ^= byte
    return content + bytes((checksum,))


@dataclass(frozen=True)
class Frame:
    pattern: bytes
    command: bytes
    payload: bytes

    @classmethod
    def parse(cls, raw: bytes) -> "Frame":
        if len(raw) < 10 or raw[:2] != b"\xff\x09":
            raise SolixProtocolError("invalid frame header or size")
        declared = int.from_bytes(raw[2:4], "little")
        if declared != len(raw):
            raise SolixProtocolError(
                f"frame length {declared} does not match received length {len(raw)}"
            )
        checksum = 0
        for byte in raw[:-1]:
            checksum ^= byte
        if checksum != raw[-1]:
            raise SolixProtocolError("frame checksum mismatch")
        return cls(raw[4:7], raw[7:9], raw[9:-1])


class AesGcmSession:
    def __init__(self) -> None:
        self.shared_secret: bytes | None = None

    def encrypt(self, payload: bytes) -> bytes:
        key, nonce = self._material()
        return AESGCM(key).encrypt(nonce, payload, AAD)

    def decrypt(self, payload: bytes) -> bytes:
        key, nonce = self._material()
        return AESGCM(key).decrypt(nonce, payload, AAD)

    def _material(self) -> tuple[bytes, bytes]:
        if self.shared_secret is None:
            return STATIC_KEY, STATIC_NONCE
        return self.shared_secret[:16], self.shared_secret[16:28]


class S2000Connection:
    def __init__(
        self,
        client: BleakClient,
        *,
        address: str,
        identity: str,
        status_store: SolixStatusStore,
    ) -> None:
        self.client = client
        self.address = address
        self.identity = identity
        self.status_store = status_store
        self.crypto = AesGcmSession()
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.mtu = 253
        self.frames: asyncio.Queue[Frame] = asyncio.Queue()
        self.fragments: dict[bytes, list[bytes]] = {}

    def notification(self, _sender: object, data: bytearray) -> None:
        raw = bytes(data)
        try:
            frame = Frame.parse(raw)
            frame = self._reassemble(frame, received_size=len(raw))
        except SolixProtocolError as error:
            LOGGER.warning("discarding invalid S2000 notification: %s", error)
            return
        if frame is not None:
            self.frames.put_nowait(frame)

    def _reassemble(self, frame: Frame, *, received_size: int) -> Frame | None:
        key = frame.pattern + frame.command
        if received_size != self.mtu and key not in self.fragments:
            return frame
        if not frame.payload:
            raise SolixProtocolError("empty fragmented payload")
        marker = frame.payload[0]
        index, total = marker >> 4, marker & 0x0F
        parts = self.fragments.setdefault(key, [])
        if index != len(parts) + 1 or not 1 <= index <= total:
            self.fragments.pop(key, None)
            raise SolixProtocolError(
                f"out-of-order fragment {index}/{total} for {frame.command.hex()}"
            )
        parts.append(frame.payload[1:])
        if index != total:
            return None
        payload = b"".join(parts)
        self.fragments.pop(key, None)
        return Frame(frame.pattern, frame.command, payload)

    async def send(
        self,
        pattern: bytes,
        command: bytes,
        *values: tuple[int, bytes],
    ) -> None:
        plaintext = encode_tlvs(*values)
        packet = build_frame(pattern, command, self.crypto.encrypt(plaintext))
        LOGGER.debug("S2000 tx pattern=%s command=%s", pattern.hex(), command.hex())
        await self.client.write_gatt_char(WRITE_UUID, packet, response=False)

    async def expect(self, command: str, *, timeout_s: float = 8.0) -> bytes:
        wanted = bytes.fromhex(command)
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for S2000 command {command}")
            frame = await asyncio.wait_for(self.frames.get(), remaining)
            if frame.pattern == NEGOTIATION_PATTERN and frame.command == wanted:
                return self.crypto.decrypt(frame.payload)
            LOGGER.debug(
                "ignoring S2000 packet during negotiation pattern=%s command=%s",
                frame.pattern.hex(),
                frame.command.hex(),
            )

    async def negotiate(self) -> None:
        await self.send(NEGOTIATION_PATTERN, b"\x40\x01", (0xA1, timestamp()))
        await self.expect("4801")

        await self.send(
            NEGOTIATION_PATTERN,
            b"\x40\x03",
            (0xA1, timestamp()),
            (0xA3, b"\x20"),
            (0xA4, b"\x00\xf0"),
        )
        capability = parse_tlvs(await self.expect("4803"))
        if 0xA2 in capability:
            self.mtu = int.from_bytes(capability[0xA2], "little")

        await self.send(NEGOTIATION_PATTERN, b"\x40\x29", (0xA1, timestamp()))
        await self.expect("4829")

        await self.send(
            NEGOTIATION_PATTERN,
            b"\x40\x05",
            (0xA1, timestamp()),
            (0xA3, b"\x20"),
            (0xA4, b"\x29\x01"),
            (0xA5, b"\x44"),
            (0xA6, b"\x02"),
        )
        await self.expect("4805")

        public_key = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )[1:]
        await self.send(NEGOTIATION_PATTERN, b"\x40\x21", (0xA1, public_key))
        device_key_payload = parse_tlvs(await self.expect("4821"))
        if len(device_key_payload.get(0xA1, b"")) != 64:
            raise SolixProtocolError("S2000 returned an invalid P-256 public key")
        device_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), b"\x04" + device_key_payload[0xA1]
        )
        self.crypto.shared_secret = self.private_key.exchange(ec.ECDH(), device_key)

        timezone = os.environ.get("TZ", "PST8PDT,M3.2.0,M11.1.0").encode()
        await self.send(
            NEGOTIATION_PATTERN,
            b"\x40\x22",
            (0xA1, timestamp()),
            (0xA3, b"\x00\x00\x00\x00"),
            (0xA5, timezone),
        )
        await self.expect("4822")

        await self.send(
            NEGOTIATION_PATTERN,
            b"\x40\x27",
            (0xA1, timestamp()),
            (0xA2, self.identity.encode()),
        )
        await self.expect("4827")
        LOGGER.info("S2000 secure session negotiated")

    async def subscribe(self) -> None:
        await self.send(
            SESSION_PATTERN,
            SUBSCRIBE_COMMAND,
            (0xA1, b"\x21"),
            (0xFE, timestamp()),
        )
        await self.send(
            SESSION_PATTERN,
            REGISTER_COMMAND,
            (0xA1, b"\x21"),
            (0xA2, b"\x04GB"),
            (0xA3, b"\x04" + self.identity.encode()),
            (0xA5, b"\x01\x01"),
            (0xFE, timestamp()),
        )

    async def wait_for_reading(self, *, timeout_s: float) -> dict[str, object]:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for S2000 0421 telemetry")
            try:
                frame = await asyncio.wait_for(self.frames.get(), remaining)
            except TimeoutError as error:
                raise TimeoutError("timed out waiting for S2000 0421 telemetry") from error
            if frame.command not in TELEMETRY_COMMANDS:
                LOGGER.debug(
                    "S2000 rx pattern=%s command=%s",
                    frame.pattern.hex(),
                    frame.command.hex(),
                )
                continue
            plaintext = self.crypto.decrypt(frame.payload)
            reading = decode_as220_status(plaintext)
            self.status_store.write_reading(reading, address=self.address)
            LOGGER.info(
                "S2000 power output=%.0fW input=%.0fW soc=%.0f%%",
                reading["output_w"],
                reading["input_w"],
                reading["soc_percent"],
            )
            return reading


async def find_target(address: str, name: str, timeout_s: float):
    if address:
        device = await BleakScanner.find_device_by_address(address, timeout=timeout_s)
        if device is not None:
            return device
    return await BleakScanner.find_device_by_name(name, timeout=timeout_s)


async def run_session(args: argparse.Namespace, store: SolixStatusStore) -> None:
    device = await find_target(args.address, args.name, args.scan_timeout)
    if device is None:
        target = f"{args.name} ({args.address})" if args.address else args.name
        raise RuntimeError(f"could not find {target}")
    LOGGER.info("connecting to %s at %s", device.name, device.address)
    async with BleakClient(device, timeout=args.connect_timeout) as client:
        connection = S2000Connection(
            client,
            address=args.address,
            identity=args.identity,
            status_store=store,
        )
        await client.start_notify(NOTIFY_UUID, connection.notification)
        await connection.negotiate()
        await connection.subscribe()
        misses = 0
        while client.is_connected:
            try:
                await connection.wait_for_reading(timeout_s=args.telemetry_timeout)
                # Disconnect after every sample. The station accepts one BLE
                # client, so an always-connected probe would lock out the
                # official app and field diagnostics.
                return
            except TimeoutError:
                misses += 1
                if misses >= args.max_misses:
                    raise
                await connection.subscribe()


async def run(args: argparse.Namespace) -> None:
    store = SolixStatusStore(args.status_file)
    while True:
        try:
            await run_session(args, store)
            if args.once:
                return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.warning("S2000 probe unavailable: %s", error)
            store.write_error(str(error), address=args.address)
            if args.once:
                raise
        await asyncio.sleep(args.reconnect_delay)


def default_identity() -> str:
    try:
        machine_id = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        machine_id = "lightweave-control"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"lightweave-solix:{machine_id}"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only SOLIX S2000 BLE power probe")
    parser.add_argument(
        "--address",
        default=os.environ.get("CONTROL_SOLIX_ADDRESS", TARGET_ADDRESS),
    )
    parser.add_argument(
        "--name",
        default=os.environ.get("CONTROL_SOLIX_NAME", TARGET_NAME),
    )
    parser.add_argument("--identity", default=default_identity())
    parser.add_argument(
        "--status-file",
        type=Path,
        default=Path("/var/lib/lightweave/power/solix-s2000.json"),
    )
    parser.add_argument("--scan-timeout", type=float, default=15.0)
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    parser.add_argument("--telemetry-timeout", type=float, default=25.0)
    parser.add_argument("--max-misses", type=int, default=1)
    parser.add_argument("--reconnect-delay", type=float, default=120.0)
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
