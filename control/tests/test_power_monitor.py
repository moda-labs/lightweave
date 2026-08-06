from __future__ import annotations

from pathlib import Path

import pytest

from control.power_monitor import PowerDrawTracker, PowerMonitorStore


def test_draw_tracker_uses_energy_delta_across_esp_reboot() -> None:
    tracker = PowerDrawTracker(window_s=15 * 60)

    first = tracker.observe(
        "C0:CD:D6:C8:04:0C",
        wh=15.059,
        elapsed_s=4021,
        reported_at=1_000.0,
        bus_v=13.26,
        current_ma=55.0,
    )
    second = tracker.observe(
        "C0:CD:D6:C8:04:0C",
        wh=16.868,
        elapsed_s=11402,
        reported_at=1_000.0 + 7381,
        bus_v=13.26,
        current_ma=55.0,
    )

    assert first.watts == pytest.approx(0.7293)
    assert first.source == "instantaneous"
    assert second.watts == pytest.approx(0.882, abs=0.001)
    assert second.source == "recent_average"


def test_draw_tracker_keeps_energy_delta_valid_when_elapsed_time_restarts() -> None:
    tracker = PowerDrawTracker(window_s=15 * 60)
    tracker.observe(
        "meter",
        wh=20.0,
        elapsed_s=7200,
        reported_at=1_000.0,
        bus_v=13.0,
        current_ma=100.0,
    )

    draw = tracker.observe(
        "meter",
        wh=20.02,
        elapsed_s=30,
        reported_at=1_060.0,
        bus_v=13.0,
        current_ma=100.0,
    )

    assert draw.watts == pytest.approx(1.2)
    assert draw.source == "recent_average"


def test_draw_tracker_starts_a_new_session_when_hardware_energy_resets() -> None:
    tracker = PowerDrawTracker(window_s=15 * 60)
    tracker.observe(
        "meter",
        wh=20.0,
        elapsed_s=7200,
        reported_at=1_000.0,
        bus_v=13.0,
        current_ma=100.0,
    )

    reset = tracker.observe(
        "meter",
        wh=0.01,
        elapsed_s=60,
        reported_at=1_060.0,
        bus_v=13.0,
        current_ma=100.0,
    )

    assert reset.watts == pytest.approx(1.3)
    assert reset.source == "instantaneous"


def test_power_monitor_store_persists_config_and_full_anchors(tmp_path: Path) -> None:
    store = PowerMonitorStore(tmp_path)
    state = {
        "config": {"battery_capacity_wh": 384.0, "full_voltage": 14.4},
        "full_anchors": {
            "C0:CD:D6:C8:04:0C": {
                "wh": 14.073,
                "ts": 1_786_000_000.0,
                "manual": True,
            }
        },
    }

    store.save(state)

    assert PowerMonitorStore(tmp_path).load() == state


def test_power_monitor_store_ignores_corrupt_state(tmp_path: Path) -> None:
    store = PowerMonitorStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    store.path.write_text("not json", encoding="utf-8")

    assert store.load() == {"config": {}, "full_anchors": {}}
