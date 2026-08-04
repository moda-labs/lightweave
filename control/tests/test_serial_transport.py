from __future__ import annotations

import sys
import types

import control.serial_transport as serial_transport
from control.serial_transport import PySerialTransport


class FakeSerialException(Exception):
    pass


class FakeSerial:
    def __init__(self, port: str, baud: int, timeout: float, write_timeout: float) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.writes: list[bytes] = []
        self.dtr = True
        self.rts = True
        self.buffer = bytearray(b"diag\n" + b'{"id":1,"ok":true}\n')
        self.closed = False
        self.reset_count = 0
        self.read_sizes: list[int] = []

    @property
    def in_waiting(self) -> int:
        return len(self.buffer)

    def setDTR(self, value: bool) -> None:
        self.dtr = value

    def setRTS(self, value: bool) -> None:
        self.rts = value

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        self.reset_count += 1

    def read(self, size: int = 1) -> bytes:
        self.read_sizes.append(size)
        if not self.buffer:
            return b""
        chunk = bytes(self.buffer[:size])
        del self.buffer[:size]
        return chunk

    def close(self) -> None:
        self.closed = True


def test_pyserial_transport_writes_lines_and_deasserts_reset(monkeypatch) -> None:
    created: list[FakeSerial] = []

    def serial_factory(port: str, baud: int, timeout: float, write_timeout: float) -> FakeSerial:
        serial = FakeSerial(port, baud, timeout, write_timeout)
        created.append(serial)
        return serial

    monkeypatch.setitem(
        sys.modules,
        "serial",
        types.SimpleNamespace(Serial=serial_factory, SerialException=FakeSerialException),
    )
    monkeypatch.setattr(serial_transport.time, "sleep", lambda _seconds: None)

    transport = PySerialTransport("/dev/cu.test", baud=57600)
    transport.write_line('{"id":1}')

    assert created[0].port == "/dev/cu.test"
    assert created[0].baud == 57600
    assert created[0].write_timeout == 2.0
    assert created[0].dtr is False
    assert created[0].rts is False
    assert created[0].reset_count == 1
    assert created[0].writes == [b'{"id":1}\n']
    assert transport.read_line(0.1) == "diag\n"
    assert created[0].read_sizes == [len(b"diag\n" + b'{"id":1,"ok":true}\n')]

    transport.close()
    assert created[0].closed is True


def test_pyserial_transport_reconnects_and_retries_failed_write(monkeypatch) -> None:
    created: list[FakeSerial] = []

    class FailingWriteSerial(FakeSerial):
        def write(self, data: bytes) -> None:
            raise FakeSerialException("device not configured")

    def serial_factory(port: str, baud: int, timeout: float, write_timeout: float) -> FakeSerial:
        serial = (
            FailingWriteSerial(port, baud, timeout, write_timeout)
            if not created
            else FakeSerial(port, baud, timeout, write_timeout)
        )
        created.append(serial)
        return serial

    monkeypatch.setitem(
        sys.modules,
        "serial",
        types.SimpleNamespace(Serial=serial_factory, SerialException=FakeSerialException),
    )
    monkeypatch.setattr(serial_transport.time, "sleep", lambda _seconds: None)

    transport = PySerialTransport("/dev/cu.test")
    transport.write_line('{"id":1}')

    assert len(created) == 2
    assert created[0].closed is True
    assert created[1].dtr is False
    assert created[1].rts is False
    assert created[1].writes == [b'{"id":1}\n']


def test_pyserial_transport_reconnects_after_read_disconnect(monkeypatch) -> None:
    created: list[FakeSerial] = []

    class FailingReadSerial(FakeSerial):
        @property
        def in_waiting(self) -> int:
            raise FakeSerialException("device not configured")

    def serial_factory(port: str, baud: int, timeout: float, write_timeout: float) -> FakeSerial:
        serial = (
            FailingReadSerial(port, baud, timeout, write_timeout)
            if not created
            else FakeSerial(port, baud, timeout, write_timeout)
        )
        created.append(serial)
        return serial

    monkeypatch.setitem(
        sys.modules,
        "serial",
        types.SimpleNamespace(Serial=serial_factory, SerialException=FakeSerialException),
    )
    monkeypatch.setattr(serial_transport.time, "sleep", lambda _seconds: None)

    transport = PySerialTransport("/dev/cu.test")

    assert transport.read_line(0.1) is None
    assert len(created) == 2
    assert created[0].closed is True
    assert transport.read_line(0.1) == "diag\n"


def test_pyserial_transport_reads_large_frame_in_chunks(monkeypatch) -> None:
    created: list[FakeSerial] = []
    large_line = b'{"id":1,"state":"' + (b"x" * 50_000) + b'"}\n'

    def serial_factory(port: str, baud: int, timeout: float, write_timeout: float) -> FakeSerial:
        serial = FakeSerial(port, baud, timeout, write_timeout)
        serial.buffer = bytearray(large_line)
        created.append(serial)
        return serial

    monkeypatch.setitem(
        sys.modules,
        "serial",
        types.SimpleNamespace(Serial=serial_factory, SerialException=FakeSerialException),
    )
    monkeypatch.setattr(serial_transport.time, "sleep", lambda _seconds: None)

    transport = PySerialTransport("/dev/cu.test")

    assert transport.read_line(0.1) == large_line.decode()
    assert created[0].read_sizes == [len(large_line)]


def test_pyserial_transport_discards_timed_out_partial_frame(monkeypatch) -> None:
    created: list[FakeSerial] = []

    def serial_factory(port: str, baud: int, timeout: float, write_timeout: float) -> FakeSerial:
        serial = FakeSerial(port, baud, timeout, write_timeout)
        serial.buffer = bytearray(b'{"id":1,"state":"unfinished')
        created.append(serial)
        return serial

    monkeypatch.setitem(
        sys.modules,
        "serial",
        types.SimpleNamespace(Serial=serial_factory, SerialException=FakeSerialException),
    )
    monkeypatch.setattr(serial_transport.time, "sleep", lambda _seconds: None)

    transport = PySerialTransport("/dev/cu.test")
    assert transport.read_line(0.01) is None

    created[0].buffer.extend(b' tail"}\n{"id":2,"ok":true}\n')

    assert transport.read_line(0.1) == '{"id":2,"ok":true}\n'
