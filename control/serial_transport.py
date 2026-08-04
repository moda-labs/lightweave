from __future__ import annotations

import time


SERIAL_READ_POLL_S = 0.005


class SerialTransportError(RuntimeError):
    pass


class PySerialTransport:
    def __init__(self, port: str, baud: int = 115200, reset_on_open: bool = False) -> None:
        import serial

        self._serial_module = serial
        self._serial_errors = (OSError, serial.SerialException)
        self._port = port
        self._baud = baud
        self._reset_on_open = reset_on_open
        self._read_buffer = bytearray()
        self._discard_until_newline = False
        self._serial = self._open()

    def _open(self):
        connection = self._serial_module.Serial(
            self._port,
            self._baud,
            timeout=0.1,
            write_timeout=2.0,
        )
        if not self._reset_on_open:
            time.sleep(0.2)
            connection.setDTR(False)
            connection.setRTS(False)
        time.sleep(2.0)
        connection.reset_input_buffer()
        self._read_buffer.clear()
        self._discard_until_newline = False
        return connection

    def _reconnect(self) -> None:
        try:
            self._serial.close()
        except self._serial_errors:
            pass
        try:
            self._serial = self._open()
        except self._serial_errors as error:
            raise SerialTransportError(f"serial reconnect failed on {self._port}: {error}") from error

    def write_line(self, line: str) -> None:
        payload = (line.rstrip("\r\n") + "\n").encode("utf-8")
        try:
            self._serial.write(payload)
        except self._serial_errors:
            self._reconnect()
            try:
                self._serial.write(payload)
            except self._serial_errors as error:
                raise SerialTransportError(f"serial write failed on {self._port}: {error}") from error

    def read_line(self, timeout_s: float) -> str | None:
        deadline = time.monotonic() + timeout_s
        try:
            self._serial.timeout = 0
            while time.monotonic() < deadline:
                newline = self._read_buffer.find(b"\n")
                if newline >= 0:
                    line = bytes(self._read_buffer[:newline + 1])
                    del self._read_buffer[:newline + 1]
                    if self._discard_until_newline:
                        self._discard_until_newline = False
                        continue
                    return line.decode("utf-8", errors="replace")

                waiting = self._serial.in_waiting
                if waiting:
                    self._read_buffer.extend(self._serial.read(waiting))
                else:
                    time.sleep(SERIAL_READ_POLL_S)
        except self._serial_errors:
            self._reconnect()
            return None
        if self._read_buffer:
            # Never expose a partial JSON frame as a line. The conductor may
            # still be transmitting it, so the next read discards through its
            # newline before returning a response to a later request.
            self._discard_until_newline = True
        return None

    def close(self) -> None:
        self._serial.close()
