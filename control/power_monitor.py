from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DRAW_WINDOW_S = 15 * 60
MAX_DRAW_W = 50.0


@dataclass(frozen=True)
class PowerDraw:
    watts: float | None
    source: str | None


class PowerHistoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class PowerHistorySample:
    mac: str
    received_at: float
    energy_session: int
    wh: float
    mah: float | None
    elapsed_s: int | None
    bus_v: float | None
    current_ma: float | None
    plausible: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mac": self.mac,
            "received_at": self.received_at,
            "energy_session": self.energy_session,
            "wh": self.wh,
            "mah": self.mah,
            "elapsed_s": self.elapsed_s,
            "bus_v": self.bus_v,
            "current_ma": self.current_ma,
            "plausible": self.plausible,
        }


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

    def restore(self, samples: Iterable[PowerHistorySample]) -> None:
        """Rehydrate recent Wh anchors after a control-plane restart."""
        for sample in samples:
            self.observe(
                sample.mac,
                wh=sample.wh,
                elapsed_s=sample.elapsed_s,
                reported_at=sample.received_at,
                bus_v=None,
                current_ma=None,
            )

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
        meter_key = str(mac).strip().upper()
        instantaneous = _instantaneous_watts(bus_v, current_ma)
        if _zero_bus_with_live_current(bus_v, current_ma):
            # Current comes from the shunt while energy uses VBUS.  A loose
            # VBUS lead can therefore report live current but accumulate zero
            # energy.  Do not let that broken interval dilute the rolling draw
            # after the voltage sense recovers.
            self._points.pop(meter_key, None)
            return PowerDraw(None, None)
        if not _finite_nonnegative(wh) or not math.isfinite(reported_at):
            return PowerDraw(instantaneous, "instantaneous" if instantaneous is not None else None)

        point = _PowerPoint(float(wh), _optional_nonnegative_int(elapsed_s), float(reported_at))
        points = self._points.setdefault(meter_key, [])
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


class PowerHistoryStore:
    """Append-only durable meter reports used for restart-safe draw tracking."""

    def __init__(self, root: Path | str | None) -> None:
        self.root = Path(root) if root is not None else None
        self.path = self.root / "power-history.sqlite3" if self.root is not None else None
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def record_sample(
        self,
        *,
        mac: str,
        received_at: float,
        wh: float,
        mah: float | None,
        elapsed_s: int | None,
        bus_v: float | None,
        current_ma: float | None,
        plausible: bool | None,
    ) -> PowerHistorySample | None:
        if not self.enabled:
            return None
        normalized_mac = str(mac).strip().upper()
        if not normalized_mac or not _finite_nonnegative(wh) or not _finite_nonnegative(received_at):
            raise PowerHistoryError("invalid power history identity or timestamp")
        clean_mah = _optional_finite_float(mah)
        clean_elapsed = _optional_nonnegative_int(elapsed_s)
        clean_bus_v = _optional_finite_float(bus_v)
        clean_current_ma = _optional_finite_float(current_ma)
        clean_plausible = plausible if isinstance(plausible, bool) else None
        identity = (
            float(wh),
            clean_mah,
            clean_elapsed,
            clean_bus_v,
            clean_current_ma,
            clean_plausible,
        )
        try:
            with closing(self._connect()) as database, database:
                latest = database.execute(
                    """
                    SELECT energy_session, wh, mah, elapsed_s, bus_v,
                           current_ma, plausible
                    FROM power_samples
                    WHERE mac = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (normalized_mac,),
                ).fetchone()
                if latest is not None and identity == (
                    float(latest["wh"]),
                    latest["mah"],
                    latest["elapsed_s"],
                    latest["bus_v"],
                    latest["current_ma"],
                    _sqlite_bool(latest["plausible"]),
                ):
                    return None
                energy_session = int(latest["energy_session"]) if latest is not None else 0
                if latest is not None and float(wh) + 0.001 < float(latest["wh"]):
                    energy_session += 1
                database.execute(
                    """
                    INSERT INTO power_samples (
                        mac, received_at, energy_session, wh, mah, elapsed_s,
                        bus_v, current_ma, plausible
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_mac,
                        float(received_at),
                        energy_session,
                        float(wh),
                        clean_mah,
                        clean_elapsed,
                        clean_bus_v,
                        clean_current_ma,
                        None if clean_plausible is None else int(clean_plausible),
                    ),
                )
        except (OSError, sqlite3.Error) as error:
            raise PowerHistoryError(f"failed to append power history: {error}") from error
        return PowerHistorySample(
            mac=normalized_mac,
            received_at=float(received_at),
            energy_session=energy_session,
            wh=float(wh),
            mah=clean_mah,
            elapsed_s=clean_elapsed,
            bus_v=clean_bus_v,
            current_ma=clean_current_ma,
            plausible=clean_plausible,
        )

    def samples(
        self,
        *,
        since: float,
        mac: str | None = None,
        limit: int = 5000,
    ) -> list[PowerHistorySample]:
        if not self.enabled:
            return []
        clean_limit = max(1, min(100_000, int(limit)))
        parameters: list[Any] = [float(since)]
        where = "received_at >= ?"
        if mac:
            where += " AND mac = ?"
            parameters.append(str(mac).strip().upper())
        parameters.append(clean_limit)
        try:
            with closing(self._connect()) as database:
                rows = database.execute(
                    f"""
                    SELECT * FROM (
                        SELECT mac, received_at, energy_session, wh, mah,
                               elapsed_s, bus_v, current_ma, plausible, id
                        FROM power_samples
                        WHERE {where}
                        ORDER BY received_at DESC, id DESC
                        LIMIT ?
                    )
                    ORDER BY received_at, id
                    """,
                    parameters,
                ).fetchall()
        except (OSError, sqlite3.Error) as error:
            raise PowerHistoryError(f"failed to read power history: {error}") from error
        return [_history_row(row) for row in rows]

    def draw_points(self, *, since: float) -> list[PowerHistorySample]:
        """Return plausible window samples plus one preceding anchor per meter."""
        if not self.enabled:
            return []
        try:
            with closing(self._connect()) as database:
                rows = database.execute(
                    """
                    SELECT mac, received_at, energy_session, wh, mah, elapsed_s,
                           bus_v, current_ma, plausible, id
                    FROM power_samples AS sample
                    WHERE sample.plausible IS NOT 0
                      AND (
                        sample.received_at >= ?
                        OR sample.id IN (
                          SELECT (
                            SELECT prior.id
                            FROM power_samples AS prior
                            WHERE prior.mac = meter.mac
                              AND prior.received_at < ?
                              AND prior.plausible IS NOT 0
                            ORDER BY prior.received_at DESC, prior.id DESC
                            LIMIT 1
                          )
                          FROM (SELECT DISTINCT mac FROM power_samples) AS meter
                        )
                      )
                    ORDER BY received_at, id
                    """,
                    (float(since), float(since)),
                ).fetchall()
        except (OSError, sqlite3.Error) as error:
            raise PowerHistoryError(f"failed to restore power history: {error}") from error
        return [_history_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        if self.path is None or self.root is None:
            raise PowerHistoryError("power history is disabled")
        self.root.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(self.path, timeout=5.0)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA busy_timeout = 5000")
        self._ensure_schema(database)
        return database

    def _ensure_schema(self, database: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS power_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mac TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    energy_session INTEGER NOT NULL,
                    wh REAL NOT NULL,
                    mah REAL,
                    elapsed_s INTEGER,
                    bus_v REAL,
                    current_ma REAL,
                    plausible INTEGER
                )
                """
            )
            database.execute(
                """
                CREATE INDEX IF NOT EXISTS power_samples_mac_received
                ON power_samples (mac, received_at)
                """
            )
            database.commit()
            self._schema_ready = True


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


def _optional_finite_float(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


def _sqlite_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _history_row(row: sqlite3.Row) -> PowerHistorySample:
    return PowerHistorySample(
        mac=str(row["mac"]),
        received_at=float(row["received_at"]),
        energy_session=int(row["energy_session"]),
        wh=float(row["wh"]),
        mah=row["mah"],
        elapsed_s=row["elapsed_s"],
        bus_v=row["bus_v"],
        current_ma=row["current_ma"],
        plausible=_sqlite_bool(row["plausible"]),
    )
