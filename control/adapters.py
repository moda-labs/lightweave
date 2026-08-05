from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from control.serial_transport import SerialTransportError

COLOR_VALUE_MARKER = 0x8000


def _pattern_params_for_wire(
    pattern: str,
    params: dict[str, int | float | str],
) -> dict[str, int | float | str]:
    wire = dict(params)
    normalized = pattern.strip().lower().replace("_", " ").replace("-", " ")
    if normalized not in {"glow", "pulse"} or "p2" in wire:
        return wire

    has_hue = "hue" in wire or "p0" in wire
    has_saturation = "saturation" in wire or "p1" in wire
    if not (has_hue and has_saturation):
        return wire

    raw_value = wire.pop("value", 255)
    try:
        value = round(float(raw_value))
    except (TypeError, ValueError):
        value = 255
    wire["p2"] = COLOR_VALUE_MARKER | max(0, min(255, value))
    return wire


class ConductorAdapter(Protocol):
    def snapshot(self) -> dict[str, Any]: ...
    def lanterns(self) -> list[dict[str, Any]]: ...
    def tick(self) -> None: ...
    def identify(self, mac: str) -> dict[str, Any]: ...
    def assign(self, mac: str, x: float, y: float) -> dict[str, Any]: ...
    def assign_group(self, mac: str, group_id: int) -> dict[str, Any]: ...
    def assign_led_count(self, mac: str, led_count: int) -> dict[str, Any]: ...
    def forget(self, mac: str) -> dict[str, Any]: ...
    def replace(self, old_mac: str, new_mac: str) -> dict[str, Any]: ...
    def reserve_id(self, mac: str, reported_id: int = 0) -> dict[str, Any]: ...
    def update_pattern(self, pattern: str, brightness: int, params: dict[str, int | float | str], group_id: int | None = None) -> dict[str, Any]: ...
    def blackout(self) -> dict[str, Any]: ...
    def restore_blackout(self) -> dict[str, Any]: ...
    def update_power_policy(self, policy: dict[str, Any]) -> dict[str, Any]: ...
    def set_ota_mode(self, enabled: bool) -> dict[str, Any]: ...
    def ota_begin(self, size: int, crc32: int) -> dict[str, Any]: ...
    def ota_chunk(self, offset: int, data: bytes) -> dict[str, Any]: ...
    def ota_end(self) -> dict[str, Any]: ...
    def ota_progress(self) -> dict[str, Any]: ...


class JsonLineTransport(Protocol):
    def write_line(self, line: str) -> None: ...
    def read_line(self, timeout_s: float) -> str | None: ...


OTA_CHUNK_TIMEOUT_S = 30.0
OTA_FINALIZE_TIMEOUT_S = 120.0
# A full 128-board state snapshot is roughly 50 KiB. At the field UART's
# 115200 baud that needs over four seconds on the wire before Python overhead.
DEFAULT_SERIAL_REQUEST_TIMEOUT_S = 8.0


@dataclass
class JsonLineSerialConductor:
    transport: JsonLineTransport
    timeout_s: float = DEFAULT_SERIAL_REQUEST_TIMEOUT_S
    _next_id: int = field(default=1, init=False)
    _last_state: dict[str, Any] | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def snapshot(self) -> dict[str, Any]:
        response = self._request("state")
        state = response.get("state")
        if not isinstance(state, dict):
            raise SerialProtocolError("state response missing state object")
        self._last_state = state
        return state

    def lanterns(self) -> list[dict[str, Any]]:
        state = self.snapshot()
        lanterns = state.get("lanterns")
        if not isinstance(lanterns, list):
            raise SerialProtocolError("state response missing lanterns list")
        return lanterns

    def tick(self) -> None:
        return None

    def identify(self, mac: str) -> dict[str, Any]:
        return self._request("identify", mac=mac)

    def assign(self, mac: str, x: float, y: float) -> dict[str, Any]:
        return self._request("assign", mac=mac, x=x, y=y)

    def assign_group(self, mac: str, group_id: int) -> dict[str, Any]:
        return self._request("group", mac=mac, group_id=group_id)

    def assign_led_count(self, mac: str, led_count: int) -> dict[str, Any]:
        return self._request("led_count", mac=mac, led_count=led_count)

    def forget(self, mac: str) -> dict[str, Any]:
        return self._request("forget", mac=mac)

    def replace(self, old_mac: str, new_mac: str) -> dict[str, Any]:
        return self._request("replace", old_mac=old_mac, new_mac=new_mac)

    def reserve_id(self, mac: str, reported_id: int = 0) -> dict[str, Any]:
        return self._request("reserve_id", mac=mac, reported_id=reported_id)

    def update_pattern(self, pattern: str, brightness: int, params: dict[str, int | float | str], group_id: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pattern": pattern,
            "brightness": brightness,
            "params": _pattern_params_for_wire(pattern, params),
        }
        if group_id is not None:
            payload["group_id"] = group_id
        return self._request("pattern", **payload)

    def blackout(self) -> dict[str, Any]:
        return self._request("blackout")

    def restore_blackout(self) -> dict[str, Any]:
        return self._request("restore_blackout")

    def update_power_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        return self._request("power_policy", **policy)

    def set_ota_mode(self, enabled: bool) -> dict[str, Any]:
        return self._request("ota_mode", enabled=enabled)

    def ota_begin(self, size: int, crc32: int) -> dict[str, Any]:
        return self._request("ota_begin", _timeout_s=max(self.timeout_s, OTA_FINALIZE_TIMEOUT_S), size=size, crc32=crc32)

    def ota_chunk(self, offset: int, data: bytes) -> dict[str, Any]:
        return self._request("ota_chunk", _timeout_s=max(self.timeout_s, OTA_CHUNK_TIMEOUT_S), offset=offset, data=data.hex())

    def ota_end(self) -> dict[str, Any]:
        return self._request("ota_end", _timeout_s=max(self.timeout_s, OTA_FINALIZE_TIMEOUT_S))

    def ota_progress(self) -> dict[str, Any]:
        return self._request("ota_progress")

    def _request(self, command: str, _timeout_s: float | None = None, **payload: Any) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            request = {"id": request_id, "cmd": command, **payload}
            try:
                self.transport.write_line(json.dumps(request, separators=(",", ":")))
            except SerialTransportError as error:
                raise SerialProtocolError(str(error)) from error

            timeout_s = self.timeout_s if _timeout_s is None else _timeout_s
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SerialProtocolError(f"timeout waiting for {command} ack")
                try:
                    line = self.transport.read_line(remaining)
                except SerialTransportError as error:
                    raise SerialProtocolError(str(error)) from error
                if line is None:
                    raise SerialProtocolError(f"timeout waiting for {command} ack")
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if response.get("id") != request_id:
                    continue
                if response.get("ok") is not True:
                    return {
                        "ok": False,
                        "error": str(response.get("error") or "serial command failed"),
                        **{key: value for key, value in response.items() if key not in {"id", "ok", "error"}},
                    }
                return {"ok": True, **{key: value for key, value in response.items() if key not in {"id", "ok"}}}


class SerialProtocolError(RuntimeError):
    pass
