from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DRAW_WINDOW_S = 15 * 60
MAX_DRAW_W = 50.0


@dataclass(frozen=True)
class PowerDraw:
    watts: float | None
    source: str | None


@dataclass(frozen=True)
class _PowerPoint:
    wh: float
    elapsed_s: int | None
    reported_at: float


class PowerDrawTracker:
    """Derive recent draw without modifying the meter's lifetime energy total."""

    def __init__(self, *, window_s: float = DEFAULT_DRAW_WINDOW_S) -> None:
        self.window_s = max(1.0, float(window_s))
        self._points: dict[str, list[_PowerPoint]] = {}

    def observe(
        self,
        mac: str,
        *,
        wh: float,
        elapsed_s: int | None,
        reported_at: float,
        bus_v: float | None,
        current_ma: float | None,
    ) -> PowerDraw:
        instantaneous = _instantaneous_watts(bus_v, current_ma)
        if _zero_bus_with_live_current(bus_v, current_ma):
            # Current comes from the shunt while energy uses VBUS.  A loose
            # VBUS lead can therefore report live current but accumulate zero
            # energy.  Do not let that broken interval dilute the rolling draw
            # after the voltage sense recovers.
            self._points.pop(str(mac), None)
            return PowerDraw(None, None)
        if not _finite_nonnegative(wh) or not math.isfinite(reported_at):
            return PowerDraw(instantaneous, "instantaneous" if instantaneous is not None else None)

        point = _PowerPoint(float(wh), _optional_nonnegative_int(elapsed_s), float(reported_at))
        points = self._points.setdefault(str(mac), [])
        if points:
            previous = points[-1]
            same_report = (
                point.wh == previous.wh
                and point.elapsed_s == previous.elapsed_s
            )
            if same_report:
                return self._draw(points, instantaneous)
            if point.wh < previous.wh or point.reported_at <= previous.reported_at:
                points.clear()

        points.append(point)
        cutoff = point.reported_at - self.window_s
        while len(points) > 2 and points[1].reported_at <= cutoff:
            points.pop(0)
        return self._draw(points, instantaneous)

    @staticmethod
    def _draw(points: list[_PowerPoint], instantaneous: float | None) -> PowerDraw:
        if len(points) >= 2:
            first = points[0]
            last = points[-1]
            elapsed = last.reported_at - first.reported_at
            energy = last.wh - first.wh
            if elapsed > 0 and energy >= 0:
                watts = energy * 3600.0 / elapsed
                if math.isfinite(watts) and 0 <= watts <= MAX_DRAW_W:
                    return PowerDraw(watts, "recent_average")
        return PowerDraw(instantaneous, "instantaneous" if instantaneous is not None else None)


class PowerMonitorStore:
    """Durable operator settings and full-charge anchors; meter Wh stays on-chip."""

    def __init__(self, root: Path | str | None) -> None:
        self.root = Path(root) if root is not None else None
        self.path = self.root / "power-monitor.json" if self.root is not None else None

    def load(self) -> dict[str, Any]:
        empty = {"config": {}, "full_anchors": {}}
        if self.path is None or not self.path.exists():
            return empty
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return empty
        if not isinstance(raw, dict):
            return empty
        config = raw.get("config")
        anchors = raw.get("full_anchors")
        if not isinstance(config, dict) or not isinstance(anchors, dict):
            return empty
        clean_config: dict[str, float] = {}
        for key in ("battery_capacity_wh", "full_voltage"):
            value = config.get(key)
            if _finite_positive(value):
                clean_config[key] = float(value)
        clean_anchors: dict[str, dict[str, Any]] = {}
        for mac, anchor in anchors.items():
            if not isinstance(mac, str) or not isinstance(anchor, dict):
                continue
            if not _finite_nonnegative(anchor.get("wh")) or not _finite_nonnegative(anchor.get("ts")):
                continue
            clean_anchors[mac] = dict(anchor)
        return {"config": clean_config, "full_anchors": clean_anchors}

    def save(self, state: dict[str, Any]) -> None:
        if self.path is None or self.root is None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def _instantaneous_watts(bus_v: float | None, current_ma: float | None) -> float | None:
    if not isinstance(bus_v, (int, float)) or not isinstance(current_ma, (int, float)):
        return None
    if not math.isfinite(float(bus_v)) or not math.isfinite(float(current_ma)):
        return None
    watts = float(bus_v) * float(current_ma) / 1000.0
    if watts < 0 or watts > MAX_DRAW_W:
        return None
    return watts


def _zero_bus_with_live_current(bus_v: float | None, current_ma: float | None) -> bool:
    if not isinstance(bus_v, (int, float)) or not isinstance(current_ma, (int, float)):
        return False
    if not math.isfinite(float(bus_v)) or not math.isfinite(float(current_ma)):
        return False
    return float(bus_v) <= 0.0 and abs(float(current_ma)) >= 1.0


def _finite_nonnegative(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and value >= 0


def _finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and value > 0


def _optional_nonnegative_int(value: Any) -> int | None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
        return None
    return int(value)
