from __future__ import annotations

from copy import deepcopy

import pytest

import control.preview as preview_module
from control.preview import render_field_preview_frames


def field_state() -> dict:
    return {
        "conductor": {"uptime_s": 12.5, "seq": 77},
        "patterns": [
            {
                "group_id": 0,
                "config": {"pattern": "White", "brightness": 64, "params": {}},
            },
            {
                "group_id": 1,
                "config": {
                    "pattern": "Fire Flicker",
                    "brightness": 96,
                    "params": {"p0": 1200, "p1": 24, "p2": 85, "p3": 95},
                },
            },
        ],
        "pattern": {"pattern": "White", "brightness": 64, "params": {}},
        "locator": {"enabled": False},
        "power": {"leds_on": True},
        "lanterns": [
            {
                "mac": "AA:00:00:00:00:01",
                "label": "#1",
                "node_id": 1,
                "status": "alive",
                "attention": "None",
                "x": 0.2,
                "y": 0.3,
                "group_id": 0,
                "led_count": 16,
            },
            {
                "mac": "BB:00:00:00:00:02",
                "label": "#2",
                "node_id": 2,
                "status": "alive",
                "attention": "None",
                "x": 0.7,
                "y": 0.6,
                "group_id": 1,
                "led_count": 64,
            },
            {
                "mac": "CC:00:00:00:00:03",
                "label": "#3",
                "node_id": 3,
                "status": "missing",
                "attention": "Not seen",
                "x": 0.5,
                "y": 0.8,
                "group_id": 0,
                "led_count": 32,
            },
            {
                "mac": "DD:00:00:00:00:04",
                "label": "#4",
                "node_id": 4,
                "status": "alive",
                "attention": "Needs position",
                "x": None,
                "y": None,
                "group_id": 0,
                "led_count": 16,
            },
        ],
    }


def test_field_preview_renders_group_patterns_and_health_metadata() -> None:
    preview = render_field_preview_frames(field_state(), 1000, 2, start_ms=0)

    assert preview["mode"] == "show"
    assert preview["source_seq"] == 77
    assert preview["positioned_count"] == 3
    assert preview["unpositioned_count"] == 1
    assert preview["frame_count"] == 2
    assert [frame["t"] for frame in preview["frames"]] == [0, 500]
    assert [node["label"] for node in preview["nodes"]] == ["#1", "#2", "#3"]
    assert preview["nodes"][1]["led_count"] == 64

    first_colors = preview["frames"][0]["colors"]
    assert first_colors[0]["rgb"] != [0, 0, 0]
    assert "pixels" not in first_colors[0]
    assert len(first_colors[1]["pixels"]) == 16
    assert first_colors[2]["rgb"] == [0, 0, 0]


def test_field_preview_preserves_fire2012_heat_cells_across_frames() -> None:
    state = field_state()
    state["patterns"][1]["config"] = {
        "pattern": "Fire2012",
        "brightness": 96,
        "params": {"p0": 30, "p1": 55, "p2": 120, "p3": 0},
    }

    preview = render_field_preview_frames(state, 1000, 2, start_ms=0)

    first = preview["frames"][0]["colors"][1]
    second = preview["frames"][1]["colors"][1]
    assert len(first["pixels"]) == 16
    assert len(set(map(tuple, first["pixels"]))) >= 4
    assert first["pixels"] != second["pixels"]


def test_field_preview_applies_locator_as_a_global_override() -> None:
    state = field_state()
    state["locator"] = {
        "enabled": True,
        "brightness": 96,
        "slot_ms": 1000,
        "bit_count": 3,
        "min_hamming_distance": 3,
    }

    preview = render_field_preview_frames(state, 500, 1, start_ms=2000)

    assert preview["mode"] == "locator"
    assert preview["frames"][0]["colors"][0]["rgb"] != [0, 0, 0]
    assert preview["frames"][0]["colors"][1]["rgb"] == [0, 0, 0]
    assert all("pixels" not in color for color in preview["frames"][0]["colors"])


def test_field_preview_computes_locator_code_plan_once_per_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = field_state()
    state["locator"] = {
        "enabled": True,
        "brightness": 96,
        "slot_ms": 1000,
        "bit_count": 8,
        "min_hamming_distance": 3,
    }
    calls = 0
    original = preview_module._calibration_code_values

    def counting_codes(*args: int) -> list[int]:
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(preview_module, "_calibration_code_values", counting_codes)

    preview = render_field_preview_frames(state, 1000, 8, start_ms=0)

    assert preview["frame_count"] == 8
    assert calls == 1


def test_field_preview_clamps_every_pattern_off_with_field_power() -> None:
    state = deepcopy(field_state())
    state["power"]["leds_on"] = False

    preview = render_field_preview_frames(state, 500, 1, start_ms=0)

    assert preview["leds_on"] is False
    assert all(
        color["rgb"] == [0, 0, 0]
        for frame in preview["frames"]
        for color in frame["colors"]
    )


def test_field_preview_uses_conductor_uptime_when_start_is_omitted() -> None:
    preview = render_field_preview_frames(field_state(), 500, 1)

    assert preview["start_ms"] == 12_500
    assert preview["frames"][0]["t"] == 12_500


def test_field_preview_handles_empty_retired_and_malformed_layout_data() -> None:
    state = field_state()
    state["patterns"] = []
    state["lanterns"] = [
        {**state["lanterns"][0], "group_id": "unexpected", "led_count": "unknown"},
        {**state["lanterns"][1], "status": "retired"},
        {**state["lanterns"][3], "status": "retired"},
    ]

    preview = render_field_preview_frames(state, 500, 1, start_ms=0)

    assert preview["positioned_count"] == 1
    assert preview["unpositioned_count"] == 0
    assert preview["nodes"][0]["group_id"] == 0
    assert preview["nodes"][0]["led_count"] == 16
    assert preview["frames"][0]["colors"][0]["rgb"] != [0, 0, 0]

    state["lanterns"] = [{**state["lanterns"][0], "status": "retired"}]
    empty = render_field_preview_frames(state, 500, 1, start_ms=0)
    assert empty["positioned_count"] == 0
    assert empty["unpositioned_count"] == 0
    assert empty["nodes"] == []
    assert empty["frames"][0]["colors"] == []

    with pytest.raises(ValueError, match="start_ms must be non-negative"):
        render_field_preview_frames(state, 500, 1, start_ms=-1)
