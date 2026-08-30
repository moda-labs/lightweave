from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

import control.solix_probe as probe_module
from control.solix_probe import (
    NEGOTIATION_PATTERN,
    SESSION_PATTERN,
    SUBSCRIBE_COMMAND,
    AesGcmSession,
    Frame,
    S2000Connection,
    SolixProtocolError,
    build_frame,
    encode_tlvs,
    find_target,
    parse_tlvs,
    run,
    run_session,
)
from control.solix_status import SolixStatusStore


class _Client:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    async def write_gatt_char(self, *_args, **_kwargs) -> None:
        self.writes.append(bytes(_args[1]))


def _as220_payload() -> bytes:
    a5 = bytes((3, 31, 0, 73))
    a6 = bytearray(14)
    a6[0] = 3
    a6[1:3] = (384).to_bytes(2, "little")
    a6[3:5] = (600).to_bytes(2, "little")
    a6[5:7] = (250).to_bytes(2, "little")
    a6[9] = 73
    a6[12:14] = (850).to_bytes(2, "little")
    a7 = bytes((3, 0)) + (300).to_bytes(2, "little")
    aa = bytes((3, 0)) + (84).to_bytes(2, "little")
    return b"\x00" + encode_tlvs((0xA5, a5), (0xA6, bytes(a6)), (0xA7, a7), (0xAA, aa))


def test_secure_bootstrap_matches_captured_anker_vector() -> None:
    crypto = AesGcmSession()
    plaintext = bytes.fromhex("a104ef79b569")
    packet = build_frame(
        NEGOTIATION_PATTERN,
        bytes.fromhex("4001"),
        crypto.encrypt(plaintext),
    )

    assert packet.hex() == (
        "ff09200003000140010a82d0ab535303e3aa9f0c2f9c868465bc8476f556fb7d"
    )
    response = Frame.parse(
        bytes.fromhex(
            "ff091e000300014801ab273ed3e27270c3f4d676ac7d69a00572793732a6"
        )
    )
    assert crypto.decrypt(response.payload) == bytes.fromhex("00a10101")


def test_frame_rejects_length_and_checksum_corruption() -> None:
    packet = build_frame(SESSION_PATTERN, bytes.fromhex("4200"), b"payload")
    assert Frame.parse(packet).payload == b"payload"

    with pytest.raises(SolixProtocolError, match="length"):
        Frame.parse(packet[:-1])
    with pytest.raises(SolixProtocolError, match="checksum"):
        Frame.parse(packet[:-1] + bytes((packet[-1] ^ 1,)))


def test_protocol_helpers_reject_invalid_headers_tlvs_and_frame_dimensions() -> None:
    with pytest.raises(SolixProtocolError, match="header or size"):
        Frame.parse(b"not-a-frame")
    with pytest.raises(SolixProtocolError, match="truncated TLV header"):
        parse_tlvs(b"\xa1")
    with pytest.raises(SolixProtocolError, match="truncated TLV a1"):
        parse_tlvs(b"\xa1\x02\x01")
    with pytest.raises(ValueError, match="out of range"):
        encode_tlvs((0x100, b"value"))
    with pytest.raises(ValueError, match="out of range"):
        encode_tlvs((0xA1, bytes(256)))
    with pytest.raises(ValueError, match="pattern or command"):
        build_frame(b"bad", b"x", b"")


def test_session_uses_negotiated_key_material() -> None:
    first = AesGcmSession()
    second = AesGcmSession()
    secret = bytes(range(32))
    first.shared_secret = secret
    second.shared_secret = secret

    encrypted = first.encrypt(b"status")
    assert encrypted != b"status"
    assert second.decrypt(encrypted) == b"status"


def test_subscribe_registers_client_with_replay_timestamp(monkeypatch, tmp_path: Path) -> None:
    async def exercise() -> None:
        client = _Client()
        connection = S2000Connection(
            client,
            address="device",
            identity="identity",
            status_store=SolixStatusStore(tmp_path / "solix.json"),
        )
        connection.crypto.shared_secret = bytes(range(32))
        monkeypatch.setattr("control.solix_probe.time.time", lambda: 1_700_000_000)

        await connection.subscribe()

        subscribe = Frame.parse(client.writes[0])
        assert subscribe.pattern == SESSION_PATTERN
        assert subscribe.command == SUBSCRIBE_COMMAND
        assert connection.crypto.decrypt(subscribe.payload) == encode_tlvs(
            (0xA1, b"\x21"),
            (0xFE, bytes.fromhex("00f15365")),
        )

        register = Frame.parse(client.writes[1])
        assert register.pattern == SESSION_PATTERN
        assert register.command == bytes.fromhex("420a")
        assert connection.crypto.decrypt(register.payload) == encode_tlvs(
            (0xA1, b"\x21"),
            (0xA2, b"\x04GB"),
            (0xA3, b"\x04identity"),
            (0xA5, b"\x01\x01"),
            (0xFE, bytes.fromhex("00f15365")),
        )

    asyncio.run(exercise())


def test_full_negotiation_uses_reported_mtu_and_derives_a_session_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        connection = S2000Connection(
            _Client(),
            address="device",
            identity="identity",
            status_store=SolixStatusStore(tmp_path / "solix.json"),
        )
        device_private_key = probe_module.ec.generate_private_key(probe_module.ec.SECP256R1())
        device_public_key = device_private_key.public_key().public_bytes(
            encoding=probe_module.serialization.Encoding.X962,
            format=probe_module.serialization.PublicFormat.UncompressedPoint,
        )[1:]
        responses = {
            "4803": encode_tlvs((0xA2, (185).to_bytes(2, "little"))),
            "4821": encode_tlvs((0xA1, device_public_key)),
        }
        sent: list[tuple[bytes, bytes, tuple[tuple[int, bytes], ...]]] = []

        async def send(pattern, command, *values) -> None:
            sent.append((pattern, command, values))

        async def expect(command, *, timeout_s=8.0) -> bytes:
            assert timeout_s == 8.0
            return responses.get(command, b"")

        monkeypatch.setenv("TZ", "UTC0")
        connection.send = send
        connection.expect = expect

        await connection.negotiate()

        assert [command.hex() for _, command, _ in sent] == [
            "4001",
            "4003",
            "4029",
            "4005",
            "4021",
            "4022",
            "4027",
        ]
        assert connection.mtu == 185
        assert connection.crypto.shared_secret is not None
        assert len(connection.crypto.shared_secret) == 32
        assert sent[-2][2][-1] == (0xA5, b"UTC0")

    asyncio.run(exercise())


def test_negotiation_rejects_an_invalid_device_public_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        connection = S2000Connection(
            _Client(),
            address="device",
            identity="identity",
            status_store=SolixStatusStore(tmp_path / "solix.json"),
        )

        async def send(*_args, **_kwargs) -> None:
            return None

        async def expect(command, *, timeout_s=8.0) -> bytes:
            if command == "4821":
                return encode_tlvs((0xA1, bytes(63)))
            return b""

        connection.send = send
        connection.expect = expect

        with pytest.raises(SolixProtocolError, match="invalid P-256 public key"):
            await connection.negotiate()

    asyncio.run(exercise())


@pytest.mark.parametrize("command", ("0421", "c421"))
def test_fragmented_telemetry_is_reassembled_decrypted_and_persisted(
    tmp_path: Path,
    command: str,
) -> None:
    async def exercise() -> None:
        store = SolixStatusStore(tmp_path / "solix.json")
        connection = S2000Connection(
            _Client(),
            address="device",
            identity="identity",
            status_store=store,
        )
        connection.crypto.shared_secret = bytes(range(32))
        encrypted = connection.crypto.encrypt(_as220_payload())
        split = len(encrypted) // 2
        first = build_frame(
            SESSION_PATTERN,
            bytes.fromhex(command),
            b"\x12" + encrypted[:split],
        )
        second = build_frame(
            SESSION_PATTERN,
            bytes.fromhex(command),
            b"\x22" + encrypted[split:],
        )
        connection.mtu = len(first)

        connection.notification(None, bytearray(first))
        assert connection.frames.empty()
        connection.notification(None, bytearray(second))
        reading = await connection.wait_for_reading(timeout_s=1)

        assert reading["output_w"] == 384
        assert store.load()["output_w"] == 384

    asyncio.run(exercise())


def test_read_timeout_names_expected_telemetry(tmp_path: Path) -> None:
    async def exercise() -> None:
        connection = S2000Connection(
            _Client(),
            address="device",
            identity="identity",
            status_store=SolixStatusStore(tmp_path / "solix.json"),
        )

        with pytest.raises(TimeoutError, match="S2000 0421 telemetry"):
            await connection.wait_for_reading(timeout_s=0.001)

    asyncio.run(exercise())


def test_notifications_handle_unfragmented_and_discard_invalid_fragments(
    tmp_path: Path,
) -> None:
    connection = S2000Connection(
        _Client(),
        address="device",
        identity="identity",
        status_store=SolixStatusStore(tmp_path / "solix.json"),
    )
    unfragmented = build_frame(SESSION_PATTERN, bytes.fromhex("9999"), b"payload")

    connection.notification(None, bytearray(unfragmented))

    assert connection.frames.get_nowait().payload == b"payload"

    connection.notification(None, bytearray(b"invalid"))
    out_of_order = build_frame(SESSION_PATTERN, bytes.fromhex("0421"), b"\x22payload")
    connection.mtu = len(out_of_order)
    connection.notification(None, bytearray(out_of_order))
    empty = build_frame(SESSION_PATTERN, bytes.fromhex("0421"), b"")
    connection.mtu = len(empty)
    connection.notification(None, bytearray(empty))

    assert connection.frames.empty()
    assert connection.fragments == {}


def test_wait_for_reading_ignores_unrelated_frames(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = SolixStatusStore(tmp_path / "solix.json")
        connection = S2000Connection(
            _Client(),
            address="device",
            identity="identity",
            status_store=store,
        )
        connection.crypto.shared_secret = bytes(range(32))
        unrelated = build_frame(
            SESSION_PATTERN,
            bytes.fromhex("9999"),
            connection.crypto.encrypt(b"ignored"),
        )
        telemetry = build_frame(
            SESSION_PATTERN,
            bytes.fromhex("0421"),
            connection.crypto.encrypt(_as220_payload()),
        )
        connection.notification(None, bytearray(unrelated))
        connection.notification(None, bytearray(telemetry))

        reading = await connection.wait_for_reading(timeout_s=1)

        assert reading["output_w"] == 384
        assert store.load()["connected"] is True

    asyncio.run(exercise())


def test_find_target_prefers_address_then_falls_back_to_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        calls: list[tuple[str, str, float]] = []
        addressed = object()

        async def by_address(address, *, timeout):
            calls.append(("address", address, timeout))
            return addressed if address == "hit" else None

        async def by_name(name, *, timeout):
            calls.append(("name", name, timeout))
            return f"named:{name}"

        monkeypatch.setattr(
            probe_module.BleakScanner,
            "find_device_by_address",
            staticmethod(by_address),
        )
        monkeypatch.setattr(
            probe_module.BleakScanner,
            "find_device_by_name",
            staticmethod(by_name),
        )

        assert await find_target("hit", "station", 4.0) is addressed
        assert await find_target("miss", "station", 5.0) == "named:station"
        assert await find_target("", "station", 6.0) == "named:station"
        assert calls == [
            ("address", "hit", 4.0),
            ("address", "miss", 5.0),
            ("name", "station", 5.0),
            ("name", "station", 6.0),
        ]

    asyncio.run(exercise())


def test_run_session_returns_after_one_reading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        events: list[str] = []
        device = SimpleNamespace(name="S2000", address="device")

        class Client:
            is_connected = True

            def __init__(self, found, *, timeout):
                assert found is device
                assert timeout == 2.0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def start_notify(self, uuid, callback):
                assert uuid == probe_module.NOTIFY_UUID
                assert callable(callback)
                events.append("notify")

        class Connection:
            def __init__(self, client, **_kwargs):
                assert isinstance(client, Client)
                self.notification = lambda *_args: None

            async def negotiate(self):
                events.append("negotiate")

            async def subscribe(self):
                events.append("subscribe")

            async def wait_for_reading(self, *, timeout_s):
                assert timeout_s == 3.0
                events.append("reading")
                return {"output_w": 1}

        async def found(*_args):
            return device

        monkeypatch.setattr(probe_module, "find_target", found)
        monkeypatch.setattr(probe_module, "BleakClient", Client)
        monkeypatch.setattr(probe_module, "S2000Connection", Connection)
        args = SimpleNamespace(
            address="device",
            name="S2000",
            scan_timeout=1.0,
            connect_timeout=2.0,
            identity="identity",
            telemetry_timeout=3.0,
            max_misses=2,
            once=False,
        )

        await run_session(args, SolixStatusStore(tmp_path / "solix.json"))

        assert events == ["notify", "negotiate", "subscribe", "reading"]

    asyncio.run(exercise())


def test_run_session_resubscribes_then_raises_at_max_misses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        device = SimpleNamespace(name="S2000", address="device")

        class Client:
            is_connected = True

            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def start_notify(self, *_args):
                return None

        class Connection:
            instance = None

            def __init__(self, *_args, **_kwargs):
                Connection.instance = self
                self.notification = lambda *_args: None
                self.subscriptions = 0
                self.reads = 0

            async def negotiate(self):
                return None

            async def subscribe(self):
                self.subscriptions += 1

            async def wait_for_reading(self, **_kwargs):
                self.reads += 1
                raise TimeoutError("miss")

        async def found(*_args):
            return device

        monkeypatch.setattr(probe_module, "find_target", found)
        monkeypatch.setattr(probe_module, "BleakClient", Client)
        monkeypatch.setattr(probe_module, "S2000Connection", Connection)
        args = SimpleNamespace(
            address="device",
            name="S2000",
            scan_timeout=1.0,
            connect_timeout=2.0,
            identity="identity",
            telemetry_timeout=3.0,
            max_misses=2,
            once=False,
        )

        with pytest.raises(TimeoutError, match="miss"):
            await run_session(args, SolixStatusStore(tmp_path / "solix.json"))

        assert Connection.instance.reads == 2
        assert Connection.instance.subscriptions == 2

    asyncio.run(exercise())


def test_run_once_propagates_error_after_persisting_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        async def fail(_args, _store):
            raise RuntimeError("Bluetooth unavailable")

        monkeypatch.setattr(probe_module, "run_session", fail)
        path = tmp_path / "solix.json"
        args = SimpleNamespace(
            status_file=path,
            address="device",
            once=True,
            reconnect_delay=0,
        )

        with pytest.raises(RuntimeError, match="Bluetooth unavailable"):
            await run(args)

        status = SolixStatusStore(path).load()
        assert status["connected"] is False
        assert status["error"] == "Bluetooth unavailable"

    asyncio.run(exercise())


def test_run_retries_after_error_and_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        attempts = 0
        sleeps: list[float] = []

        async def session(_args, _store):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary failure")
            raise asyncio.CancelledError

        async def sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(probe_module, "run_session", session)
        monkeypatch.setattr(probe_module.asyncio, "sleep", sleep)
        path = tmp_path / "solix.json"
        args = SimpleNamespace(
            status_file=path,
            address="device",
            once=False,
            reconnect_delay=7.0,
        )

        with pytest.raises(asyncio.CancelledError):
            await run(args)

        assert attempts == 2
        assert sleeps == [7.0]
        assert SolixStatusStore(path).load()["error"] == "temporary failure"

    asyncio.run(exercise())


def test_default_identity_is_stable_and_has_a_read_failure_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: "machine-123\n")
    expected = str(
        uuid.uuid5(uuid.NAMESPACE_DNS, "lightweave-solix:machine-123")
    )
    assert probe_module.default_identity() == expected

    def fail(*_args, **_kwargs):
        raise OSError("missing")

    monkeypatch.setattr(Path, "read_text", fail)
    assert probe_module.default_identity() == str(
        uuid.uuid5(uuid.NAMESPACE_DNS, "lightweave-solix:lightweave-control")
    )
