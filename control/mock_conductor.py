from __future__ import annotations

import time
import zlib
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .ota_store import OTA_CHUNK_BYTES


def _now() -> float:
    return time.time()


FIELD_FIRMWARE = {
    "version": "0.3.0",
    "proto": 10,
    "build_id": 0x44D028FD,
    "build_label": "44d028fd",
    "dirty": False,
}

DEFAULT_POWER_POLICY = {
    "light_sleep_check_s": 4,
    "deep_sleep_check_min": 15,
    "led_on_start_min": 20 * 60,
    "led_on_end_min": 6 * 60,
    "current_min": 12 * 60,
    "current_epoch_s": 0,
    "schedule_enabled": False,
    "force_awake": True,
    "force_sleep": False,
    "leds_on": True,
}

OTA_WINDOW_S = 15 * 60
GROUP_COUNT = 8


def _firmware_matches(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    if not actual:
        return False
    return (
        actual.get("proto") == expected.get("proto")
        and actual.get("build_id") == expected.get("build_id")
        and bool(actual.get("dirty")) == bool(expected.get("dirty"))
        and actual.get("version") == expected.get("version")
    )


@dataclass
class Lantern:
    mac: str
    label: str
    status: str
    last_seen_s: float
    x: float | None
    y: float | None
    power_wh: float | None = None
    avg_w: float | None = None
    power_mah: float | None = None
    bus_v: float | None = None
    current_ma: float | None = None
    power_elapsed_s: int | None = None
    power_plausible: bool | None = None
    power_last_report_s: float | None = None
    node_id: int | None = None
    firmware: dict[str, Any] = field(default_factory=lambda: deepcopy(FIELD_FIRMWARE))
    group_id: int = 0
    led_count: int = 16

    def as_dict(self, now: float) -> dict[str, Any]:
        has_position = self.x is not None and self.y is not None
        node_id = self.permanent_id()
        attention = "None"
        if self.status == "missing":
            attention = "Not seen"
        elif self.status == "retired":
            attention = "Retired"
        elif not has_position:
            attention = "Needs position"
        elif not _firmware_matches(FIELD_FIRMWARE, self.firmware):
            attention = "Firmware mismatch"

        return {
            "mac": self.mac,
            "label": self.label,
            "node_id": node_id,
            "status": self.status,
            "last_seen_s": round(self.last_seen_s, 1),
            "last_seen_label": self._age_label(),
            "x": self.x,
            "y": self.y,
            "position": "Set" if has_position else "Missing",
            "group_id": self.group_id,
            "group": f"Group {self.group_id + 1}",
            "led_count": self.led_count,
            "attention": attention,
            "firmware": deepcopy(self.firmware),
            "power": {
                "wh": self.power_wh,
                "avg_w": self.avg_w,
                "mah": self.power_mah,
                "bus_v": self.bus_v,
                "current_ma": self.current_ma,
                "elapsed_s": self.power_elapsed_s,
                "plausible": self.power_plausible,
                "last_report_s": self.power_last_report_s,
                "last_report_label": self._age_label(self.power_last_report_s) if self.power_wh is not None else None,
            },
            "updated_at": now,
        }

    def _node_id_from_label(self) -> int:
        if self.label.startswith("#") and self.label[1:].isdigit():
            return int(self.label[1:])
        return 0

    def permanent_id(self) -> int:
        return self.node_id if self.node_id is not None else self._node_id_from_label()

    def _age_label(self, age: float | None = None) -> str:
        age_s = self.last_seen_s if age is None else age
        if age_s < 60:
            return f"{int(age_s)}s ago"
        minutes = int(age_s // 60)
        return f"{minutes}m ago"


@dataclass
class MockConductor:
    started_at: float = field(default_factory=_now)
    seq: int = 184221
    wake: bool = True
    connected: bool = True
    power: dict[str, Any] = field(default_factory=lambda: deepcopy(DEFAULT_POWER_POLICY))
    pattern: dict[str, Any] = field(
        default_factory=lambda: {
            "pattern": "Glow",
            "brightness": 48,
            "params": {"hue": 40, "saturation": 100},
        }
    )
    group_patterns: list[dict[str, Any]] = field(default_factory=list)
    _blackout_brightness: list[int] | None = field(default=None, init=False, repr=False)
    ota_started_at: float | None = None
    ota_installed_crc32: int | None = None
    _ota_write: bytearray | None = field(default=None, init=False, repr=False)
    _ota_expected_size: int = field(default=0, init=False, repr=False)
    _ota_expected_crc32: int = field(default=0, init=False, repr=False)
    _ota_nodes: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    events: list[dict[str, Any]] = field(default_factory=list)
    _lanterns: list[Lantern] = field(
        default_factory=lambda: [
            Lantern("8C:94:DF:8F:71:50", "#1", "alive", 4, 0.54, 0.47, 0.38, 0.71, 102.0, 13.28, 53.0, 1920, True, 4),
            Lantern("30:76:F5:93:67:3C", "#2", "alive", 8, 0.43, 0.36, 0.41, 0.76, 110.0, 13.21, 56.0, 1940, True, 8),
            Lantern("A0:B7:65:11:40:77", "#7", "alive", 9, 0.61, 0.35, None, None),
            Lantern("A0:B7:65:11:42:09", "#9", "alive", 13, 0.34, 0.52, None, None),
            Lantern("A0:B7:65:11:42:14", "#14", "alive", 17, 0.72, 0.55, None, None),
            Lantern("A0:B7:65:11:42:15", "#15", "alive", 20, 0.48, 0.64, None, None),
            Lantern("A0:B7:65:11:44:91", "#18", "missing", 42, 0.66, 0.69, None, None),
            Lantern("8C:94:DF:57:7F:14", "#57", "alive", 12, None, None, None, None),
            Lantern("A0:B7:65:11:42:21", "#21", "alive", 10, 0.76, 0.30, None, None),
            Lantern("A0:B7:65:11:42:24", "#24", "alive", 18, 0.22, 0.31, None, None),
        ]
    )

    def __post_init__(self) -> None:
        if not self.group_patterns:
            self.group_patterns = [deepcopy(self.pattern) for _ in range(GROUP_COUNT)]
        else:
            self.group_patterns = [deepcopy(item) for item in self.group_patterns[:GROUP_COUNT]]
            while len(self.group_patterns) < GROUP_COUNT:
                self.group_patterns.append(deepcopy(self.pattern))
        self.pattern = deepcopy(self.group_patterns[0])
        if not self.events:
            self._event("mock conductor started")
            self._event("beacon seq=184221 pattern=GLOW bri=48 wake=on")

    def snapshot(self) -> dict[str, Any]:
        now = _now()
        lanterns = self.lanterns()
        table_rows = sum(1 for item in lanterns if item["position"] == "Set")
        alive = sum(1 for item in lanterns if item["status"] == "alive" and item["position"] == "Set")
        attention = sum(1 for item in lanterns if item["attention"] != "None")
        firmware = self._firmware_summary(lanterns, table_rows)
        ota = self._ota_summary(lanterns, table_rows, now, firmware)
        return {
            "conductor": {
                "connected": self.connected,
                "uptime_s": round(now - self.started_at, 1),
                "seq": self.seq,
                "wake": self.wake,
                "sync": "locked",
                "firmware": deepcopy(FIELD_FIRMWARE),
            },
            "summary": {
                "alive": alive,
                "total": table_rows,
                "attention": attention,
                "table_rows": table_rows,
                "firmware": firmware,
            },
            "pattern": deepcopy(self.pattern),
            "patterns": [
                {"group_id": group_id, "config": deepcopy(config)}
                for group_id, config in enumerate(self.group_patterns)
            ],
            "blackout": {"restore_available": self._blackout_brightness is not None},
            "power": deepcopy(self.power),
            "ota": ota,
            "recovery": self._recovery_summary(lanterns, ota, firmware),
            "lanterns": lanterns,
            "events": list(reversed(self.events[-20:])),
        }

    def lanterns(self) -> list[dict[str, Any]]:
        now = _now()
        return [lantern.as_dict(now) for lantern in self._lanterns]

    def tick(self) -> None:
        self.seq += 20
        for lantern in self._lanterns:
            lantern.last_seen_s += 5
        self._event(f"beacon seq={self.seq} pattern={self.pattern['pattern'].upper()} bri={self.pattern['brightness']}")

    def identify(self, mac: str) -> dict[str, Any]:
        lantern = self._find(mac)
        if not lantern:
            return {"ok": False, "error": "unknown lantern"}
        self._event(f"identify {lantern.mac} {lantern.label}")
        return {"ok": True, "message": f"identify sent to {lantern.label}", "mac": lantern.mac}

    def assign(self, mac: str, x: float, y: float) -> dict[str, Any]:
        lantern = self._find(mac)
        if not lantern:
            return {"ok": False, "error": "unknown lantern"}
        lantern.x = x
        lantern.y = y
        self._event(f"assign {lantern.mac} x={x:.2f} y={y:.2f}")
        return {"ok": True, "message": f"assigned {lantern.label}", "mac": lantern.mac}

    def assign_group(self, mac: str, group_id: int) -> dict[str, Any]:
        lantern = self._find(mac)
        if not lantern:
            return {"ok": False, "error": "unknown lantern"}
        if group_id < 0 or group_id >= GROUP_COUNT:
            return {"ok": False, "error": "invalid group"}
        lantern.group_id = group_id
        self._event(f"group {lantern.mac}={group_id + 1}")
        return {"ok": True, "message": f"moved {lantern.label} to Group {group_id + 1}", "mac": lantern.mac}

    def assign_led_count(self, mac: str, led_count: int) -> dict[str, Any]:
        lantern = self._find(mac)
        if not lantern:
            return {"ok": False, "error": "unknown lantern"}
        if led_count not in {16, 32, 64}:
            return {"ok": False, "error": "invalid led count"}
        lantern.led_count = led_count
        self._event(f"leds {lantern.mac}={led_count}")
        return {"ok": True, "message": f"set {lantern.label} to {led_count} LEDs", "mac": lantern.mac}

    def forget(self, mac: str) -> dict[str, Any]:
        lantern = self._find(mac)
        if not lantern:
            return {"ok": False, "error": "unknown lantern"}
        lantern.x = None
        lantern.y = None
        self._event(f"forget {lantern.mac}")
        return {"ok": True, "message": f"forgot position for {lantern.label}", "mac": lantern.mac}

    def replace(self, old_mac: str, new_mac: str) -> dict[str, Any]:
        old = self._find(old_mac)
        new = self._find(new_mac)
        if not old or not new:
            return {"ok": False, "error": "old or new lantern not found"}
        if old.x is None or old.y is None:
            return {"ok": False, "error": "old lantern has no position to replace"}
        if new.x is not None or new.y is not None:
            return {"ok": False, "error": "replacement lantern already has a position"}
        if new.status != "alive":
            return {"ok": False, "error": "replacement lantern is not awake"}
        old_label = old.label
        new_label = new.label
        new.x = old.x
        new.y = old.y
        new.group_id = old.group_id
        old.x = None
        old.y = None
        self._event(
            f"replace old={old.mac} label={old_label} "
            f"new={new.mac} label={new_label}"
        )
        return {
            "ok": True,
            "message": f"moved position from {old_label} to {new_label}",
            "old_mac": old.mac,
            "new_mac": new.mac,
        }

    def reserve_id(self, mac: str, reported_id: int = 0) -> dict[str, Any]:
        normalized = mac.upper()
        existing = self._find(normalized)
        if existing and existing.permanent_id():
            permanent_id = existing.permanent_id()
            if reported_id not in {0, permanent_id}:
                return {"ok": False, "error": "permanent ID conflict"}
            return {
                "ok": True,
                "message": "permanent ID already reserved",
                "node_id": permanent_id,
                "created": False,
            }

        owner = next(
            (
                item
                for item in self._lanterns
                if reported_id and item.permanent_id() == reported_id
            ),
            None,
        )
        if owner and owner.mac != normalized:
            return {"ok": False, "error": "permanent ID conflict"}
        used = {item.permanent_id() for item in self._lanterns if item.permanent_id()}
        node_id = reported_id or next(
            candidate for candidate in range(1, 65536) if candidate not in used
        )
        if existing:
            existing.node_id = node_id
            existing.label = f"#{node_id}"
        else:
            self._lanterns.append(
                Lantern(
                    mac=normalized,
                    label=f"#{node_id}",
                    node_id=node_id,
                    status="missing",
                    last_seen_s=0,
                    x=None,
                    y=None,
                )
            )
        self._event(f"reserve_id {normalized} id={node_id}")
        return {
            "ok": True,
            "message": "permanent ID reserved",
            "node_id": node_id,
            "created": True,
        }

    def update_pattern(self, pattern: str, brightness: int, params: dict[str, int | float | str], group_id: int | None = None) -> dict[str, Any]:
        updated = {"pattern": pattern, "brightness": brightness, "params": dict(params)}
        targets = range(GROUP_COUNT) if group_id is None else [group_id]
        if group_id is not None and (group_id < 0 or group_id >= GROUP_COUNT):
            return {"ok": False, "error": "invalid group"}
        for target in targets:
            self.group_patterns[target] = deepcopy(updated)
        self.pattern = deepcopy(self.group_patterns[0])
        scope = "all groups" if group_id is None else f"Group {group_id + 1}"
        self._event(f"pattern={pattern} bri={brightness} scope={scope}")
        return {"ok": True, "message": f"pattern changed to {pattern} for {scope}", "pattern": deepcopy(updated)}

    def blackout(self) -> dict[str, Any]:
        if self._blackout_brightness is None:
            self._blackout_brightness = [
                int(current["brightness"]) for current in self.group_patterns
            ]
        for group_id, current in enumerate(self.group_patterns):
            self.group_patterns[group_id] = {
                "pattern": current["pattern"],
                "brightness": 0,
                "params": deepcopy(current["params"]),
            }
        self.pattern = deepcopy(self.group_patterns[0])
        self._event("blackout bri=0")
        return {"ok": True, "message": "blackout broadcast", "pattern": deepcopy(self.pattern)}

    def restore_blackout(self) -> dict[str, Any]:
        if self._blackout_brightness is None:
            return {"ok": False, "error": "no blackout to restore"}
        for group_id, brightness in enumerate(self._blackout_brightness):
            self.group_patterns[group_id]["brightness"] = brightness
        self._blackout_brightness = None
        self.pattern = deepcopy(self.group_patterns[0])
        self._event("blackout restored")
        return {"ok": True, "message": "blackout restored", "pattern": deepcopy(self.pattern)}

    def update_power_policy(self, policy: dict[str, Any]) -> dict[str, Any]:
        updated = deepcopy(self.power)
        updated.update(policy)
        updated["leds_on"] = bool(updated["force_awake"]) or (
            not bool(updated.get("force_sleep"))
            and (
                not bool(updated["schedule_enabled"])
                or self._minute_in_window(
                    int(updated["current_min"]), int(updated["led_on_start_min"]), int(updated["led_on_end_min"])
                )
            )
        )
        self.power = updated
        self.wake = bool(updated["force_awake"])
        self._event(
            f"power policy light={updated['light_sleep_check_s']}s deep={updated['deep_sleep_check_min']}m"
        )
        return {"ok": True, "message": "power policy changed", "power": deepcopy(self.power)}

    def set_ota_mode(self, enabled: bool) -> dict[str, Any]:
        self.ota_started_at = _now() if enabled else None
        if not enabled:
            self._ota_write = None
            self._ota_nodes = {}
        self._event("ota maintenance mode started" if enabled else "ota maintenance mode ended")
        return {
            "ok": True,
            "message": "ota maintenance mode started" if enabled else "ota maintenance mode ended",
        }

    def ota_begin(self, size: int, crc32: int) -> dict[str, Any]:
        if self.ota_started_at is None:
            self.ota_started_at = _now()
        self._ota_write = bytearray()
        self._ota_expected_size = size
        self._ota_expected_crc32 = crc32
        self._ota_nodes = {
            item.mac: {"mac": item.mac, "phase": "begin", "error": "none", "offset": 0, "crc32": 0, "last_seen_s": 0}
            for item in self._lanterns
            if item.status == "alive"
        }
        if not self._ota_nodes:
            self._ota_write = None
            return {"ok": False, "error": "no performers online"}
        return {
            "ok": True,
            "message": "ota write started",
            "targets": list(self._ota_nodes),
        }

    def ota_chunk(self, offset: int, data: bytes) -> dict[str, Any]:
        if self._ota_write is None:
            return {"ok": False, "error": "ota write is not active"}
        if offset < len(self._ota_write) and offset + len(data) <= len(self._ota_write):
            return {"ok": True, "message": "ota chunk already written"}
        if offset != len(self._ota_write):
            for node in self._ota_nodes.values():
                node.update({"phase": "failed", "error": "chunk offset mismatch", "offset": offset})
            return {"ok": False, "error": "ota chunk offset mismatch"}
        expected_len = min(OTA_CHUNK_BYTES, max(0, self._ota_expected_size - offset))
        if len(data) != expected_len:
            return {"ok": False, "error": "ota chunk length mismatch"}
        self._ota_write.extend(data)
        for node in self._ota_nodes.values():
            node.update({
                "phase": "writing",
                "error": "none",
                "offset": len(self._ota_write),
                "crc32": zlib.crc32(self._ota_write) & 0xFFFFFFFF,
                "last_seen_s": 0,
            })
        return {"ok": True, "message": "ota chunk written"}

    def ota_progress(self) -> dict[str, Any]:
        return {
            "ok": True,
            "active": self._ota_write is not None,
            "size": self._ota_expected_size,
            "written": len(self._ota_write or b""),
            "crc32": zlib.crc32(self._ota_write or b"") & 0xFFFFFFFF,
            "nodes": list(self._ota_nodes.values()),
            "staged": self._ota_write is None and self.ota_installed_crc32 is not None,
            "targets": list(self._ota_nodes),
        }

    def ota_repair(self, mac: str, offset: int, data: bytes) -> dict[str, Any]:
        node = self._ota_nodes.get(mac)
        if node is None:
            return {"ok": False, "error": "ota repair target is not active"}
        current = int(node.get("offset") or 0)
        if offset < current and offset + len(data) <= current:
            return {"ok": True, "message": "ota repair chunk already written"}
        if offset != current:
            return {"ok": False, "error": "ota repair range is invalid"}
        prefix = bytes(self._ota_write or b"")[: offset + len(data)]
        node.update({
            "phase": "writing",
            "error": "none",
            "offset": offset + len(data),
            "crc32": zlib.crc32(prefix) & 0xFFFFFFFF,
            "last_seen_s": 0,
        })
        return {"ok": True, "message": "ota repair chunk sent"}

    def ota_restart(self, mac: str) -> dict[str, Any]:
        node = self._ota_nodes.get(mac)
        if node is None:
            return {"ok": False, "error": "ota restart target is not active"}
        node.update({"phase": "begin", "error": "none", "offset": 0, "crc32": 0})
        return {"ok": True, "message": "ota performer restarted"}

    def ota_probe(self) -> dict[str, Any]:
        return {"ok": True, "message": "ota status requested", "settle_s": 0}

    def ota_end(self) -> dict[str, Any]:
        if self._ota_write is None:
            if self.ota_installed_crc32 == self._ota_expected_crc32 and self._ota_expected_size > 0:
                return {
                    "ok": True,
                    "message": "ota image already staged",
                    "staged": True,
                    "nodes": list(self._ota_nodes.values()),
                }
            return {"ok": False, "error": "ota write is not active"}
        if len(self._ota_write) != self._ota_expected_size:
            self._ota_write = None
            for node in self._ota_nodes.values():
                node.update({"phase": "failed", "error": "image incomplete", "offset": len(self._ota_write or b"")})
            return {"ok": False, "error": "ota image incomplete"}
        if (zlib.crc32(self._ota_write) & 0xFFFFFFFF) != self._ota_expected_crc32:
            self._ota_write = None
            for node in self._ota_nodes.values():
                node.update({"phase": "failed", "error": "crc mismatch", "offset": len(self._ota_write or b"")})
            return {"ok": False, "error": "ota crc mismatch"}
        self.ota_installed_crc32 = self._ota_expected_crc32
        for node in self._ota_nodes.values():
            if int(node.get("offset") or 0) != self._ota_expected_size:
                node.update({"phase": "repairing", "error": "image incomplete"})
                continue
            node.update({
                "phase": "staged",
                "error": "none",
                "offset": self._ota_expected_size,
                "crc32": self._ota_expected_crc32,
                "last_seen_s": 0,
            })
        self._ota_write = None
        self._event("ota image staged")
        return {
            "ok": True,
            "message": "ota image staged",
            "staged": True,
            "nodes": list(self._ota_nodes.values()),
        }

    def ota_activate(self, mac: str | None = None) -> dict[str, Any]:
        if mac is None:
            self._event("conductor firmware activated")
            return {"ok": True, "message": "activating conductor firmware"}
        node = self._ota_nodes.get(mac)
        if node is None or node.get("phase") not in {"staged", "activating", "complete"}:
            return {"ok": False, "error": "performer firmware is not staged"}
        node.update({"phase": "complete", "error": "none", "last_seen_s": 0})
        lantern = self._find(mac)
        if lantern is not None:
            lantern.firmware = deepcopy(FIELD_FIRMWARE)
            lantern.attention = "None"
        return {"ok": True, "message": "performer activation sent"}

    def _find(self, mac: str) -> Lantern | None:
        normalized = mac.upper()
        return next((lantern for lantern in self._lanterns if lantern.mac.upper() == normalized), None)

    def _event(self, message: str) -> None:
        self.events.append({"ts": _now(), "message": message})

    @staticmethod
    def _minute_in_window(minute: int, start: int, end: int) -> bool:
        minute %= 1440
        start %= 1440
        end %= 1440
        if start == end:
            return True
        if start < end:
            return start <= minute < end
        return minute >= start or minute < end

    def _firmware_summary(self, lanterns: list[dict[str, Any]], table_rows: int) -> dict[str, Any]:
        positioned = [item for item in lanterns if item["position"] == "Set" and item["status"] == "alive"]
        matching = sum(1 for item in positioned if _firmware_matches(FIELD_FIRMWARE, item.get("firmware")))
        consistent = all(_firmware_matches(FIELD_FIRMWARE, item.get("firmware")) for item in positioned)
        return {
            "consistent": consistent,
            "matching": matching,
            "seen": len(positioned),
            "expected": table_rows,
            "version": FIELD_FIRMWARE["version"],
            "build_label": FIELD_FIRMWARE["build_label"],
            "dirty": FIELD_FIRMWARE["dirty"],
        }

    def _ota_summary(
        self,
        lanterns: list[dict[str, Any]],
        table_rows: int,
        now: float,
        firmware_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.ota_started_at is not None and now - self.ota_started_at >= OTA_WINDOW_S:
            self.ota_started_at = None
        online = [item for item in lanterns if item["status"] == "alive"]
        firmware_summary = firmware_summary or self._firmware_summary(lanterns, table_rows)
        active = self.ota_started_at is not None
        deferred = max(0, table_rows - len(online))
        blocked: list[str] = []
        if not online:
            blocked.append("no registered performers" if table_rows == 0 else "no performers online")
        ready = bool(online)
        timeout_s = max(0, int(OTA_WINDOW_S - (now - self.ota_started_at))) if active else 0
        return {
            "mode": "updating" if active else "ready",
            "enabled": active,
            "ready": ready,
            "ready_count": len(online),
            "expected": table_rows,
            "missing": deferred,
            "deferred": deferred,
            "firmware_consistent": firmware_summary["consistent"],
            "timeout_s": timeout_s,
            "blocked": blocked,
            "nodes": list(self._ota_nodes.values()),
        }

    def _recovery_summary(
        self,
        lanterns: list[dict[str, Any]],
        ota: dict[str, Any],
        firmware_summary: dict[str, Any],
    ) -> dict[str, Any]:
        missing = [
            {"mac": item["mac"], "label": item["label"], "reason": "not seen"}
            for item in lanterns
            if item["status"] == "missing" and item["position"] == "Set"
        ]
        mismatched = [
            {
                "mac": item["mac"],
                "label": item["label"],
                "reason": "firmware mismatch",
                "firmware": deepcopy(item.get("firmware")),
            }
            for item in lanterns
            if item.get("attention") == "Firmware mismatch"
        ]
        failed_ota = []
        for node in ota.get("nodes", []):
            if node.get("phase") != "failed":
                continue
            mac = str(node.get("mac") or "")
            lantern = self._find(mac)
            failed_ota.append({
                "mac": mac,
                "label": lantern.label if lantern else (mac or "node"),
                "reason": node.get("error") or "ota failed",
                "phase": node.get("phase"),
            })
        ready = not missing and not mismatched and not failed_ota and firmware_summary.get("consistent") is True
        if failed_ota:
            status = "ota_failed"
            title = "Firmware update needs recovery"
            action = "Keep maintenance mode open. Power-cycle the listed lanterns, wait for them to check in, then rerun the same staged firmware."
        elif mismatched:
            status = "mixed_firmware"
            title = "Mixed firmware detected"
            action = "Enter maintenance mode and reinstall the staged firmware across the whole field. Do not run the show with mixed firmware."
        elif missing:
            status = "missing_nodes"
            title = "Placed lanterns are missing"
            action = "Wake or power-cycle the listed lanterns. If a lantern is physically gone, replace it with an awake unpositioned spare."
        else:
            status = "ready"
            title = "No recovery needed"
            action = "Field firmware is consistent and all placed lanterns are healthy."
        return {
            "status": status,
            "ready": ready,
            "title": title,
            "action": action,
            "missing": missing,
            "mismatched": mismatched,
            "failed_ota": failed_ota,
        }
