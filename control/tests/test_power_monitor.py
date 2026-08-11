from __future__ import annotations

from pathlib import Path

import pytest

from control.power_monitor import (
    PowerDrawTracker,
    PowerHistoryError,
    PowerHistoryStore,
    PowerMonitorStore,
)


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


def test_draw_tracker_discards_window_after_zero_bus_with_live_current() -> None:
    tracker = PowerDrawTracker(window_s=15 * 60)
    tracker.observe(
        "meter",
        wh=0.150,
        elapsed_s=600,
        reported_at=1_000.0,
        bus_v=13.26,
        current_ma=70.0,
    )

    fault = tracker.observe(
        "meter",
        wh=0.150,
        elapsed_s=660,
        reported_at=1_060.0,
        bus_v=0.0,
        current_ma=70.0,
    )
    recovered = tracker.observe(
        "meter",
        wh=0.166,
        elapsed_s=720,
        reported_at=1_120.0,
        bus_v=13.26,
        current_ma=70.0,
    )
    next_report = tracker.observe(
        "meter",
        wh=0.18147,
        elapsed_s=780,
        reported_at=1_180.0,
        bus_v=13.26,
        current_ma=70.0,
    )

    assert fault.watts is None
    assert fault.source is None
    assert recovered.watts == pytest.approx(0.9282)
    assert recovered.source == "instantaneous"
    assert next_report.watts == pytest.approx(0.9282, abs=0.001)
    assert next_report.source == "recent_average"


def test_power_history_persists_distinct_reports_and_deduplicates_replays(
    tmp_path: Path,
) -> None:
    store = PowerHistoryStore(tmp_path)

    first = store.record_sample(
        mac="C0:CD:D6:C8:04:0C",
        received_at=1_000.0,
        wh=15.059,
        mah=1200.0,
        elapsed_s=4021,
        bus_v=13.26,
        current_ma=55.0,
        plausible=True,
    )
    replay = store.record_sample(
        mac="C0:CD:D6:C8:04:0C",
        received_at=1_005.0,
        wh=15.059,
        mah=1200.0,
        elapsed_s=4021,
        bus_v=13.26,
        current_ma=55.0,
        plausible=True,
    )
    second = store.record_sample(
        mac="C0:CD:D6:C8:04:0C",
        received_at=1_060.0,
        wh=15.079,
        mah=1201.5,
        elapsed_s=4081,
        bus_v=13.25,
        current_ma=56.0,
        plausible=True,
    )

    assert first is not None
    assert replay is None
    assert second is not None
    assert [sample.wh for sample in store.samples(since=0)] == [15.059, 15.079]
    assert PowerHistoryStore(tmp_path).samples(since=0) == store.samples(since=0)


def test_power_history_starts_new_energy_session_only_when_accumulator_resets(
    tmp_path: Path,
) -> None:
    store = PowerHistoryStore(tmp_path)
    common = {
        "mac": "meter",
        "mah": 100.0,
        "bus_v": 13.0,
        "current_ma": 100.0,
        "plausible": True,
    }
    store.record_sample(received_at=1_000.0, wh=20.0, elapsed_s=7200, **common)
    reboot = store.record_sample(
        received_at=1_060.0, wh=20.02, elapsed_s=30, **common
    )
    reset = store.record_sample(
        received_at=1_120.0, wh=0.01, elapsed_s=60, **common
    )

    assert reboot is not None and reboot.energy_session == 0
    assert reset is not None and reset.energy_session == 1


def test_power_history_disabled_store_and_invalid_identity_are_explicit(
    tmp_path: Path,
) -> None:
    disabled = PowerHistoryStore(None)

    assert disabled.enabled is False
    assert disabled.samples(since=0) == []
    assert disabled.draw_points(since=0) == []
    assert disabled.record_sample(
        mac="meter",
        received_at=1_000.0,
        wh=1.0,
        mah=None,
        elapsed_s=None,
        bus_v=None,
        current_ma=None,
        plausible=None,
    ) is None

    enabled = PowerHistoryStore(tmp_path)
    with pytest.raises(PowerHistoryError, match="invalid power history identity"):
        enabled.record_sample(
            mac="",
            received_at=1_000.0,
            wh=1.0,
            mah=None,
            elapsed_s=None,
            bus_v=None,
            current_ma=None,
            plausible=None,
        )


def test_draw_tracker_restores_recent_points_from_durable_history(
    tmp_path: Path,
) -> None:
    store = PowerHistoryStore(tmp_path)
    for received_at, wh, elapsed_s in (
        (1_000.0, 20.0, 7200),
        (1_060.0, 20.02, 30),
    ):
        store.record_sample(
            mac="meter",
            received_at=received_at,
            wh=wh,
            mah=None,
            elapsed_s=elapsed_s,
            bus_v=13.0,
            current_ma=100.0,
            plausible=True,
        )

    tracker = PowerDrawTracker(window_s=15 * 60)
    tracker.restore(store.draw_points(since=100.0))
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
