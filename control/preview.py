from __future__ import annotations

import json
import math
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any


MAX_BRIGHTNESS = 192
MAX_PREVIEW_FRAMES = 240
COLOR_VALUE_MARKER = 0x8000
FIREFLY_SCATTER_MASK = 0x007F
FIREFLY_CHORUS_MARKER = 0x8000
OCEAN_WAVELENGTH_MASK = 0x03FF
OCEAN_ANGLE_MASK = 0x01FF
RING_PIXEL_COUNT = 16


@dataclass(frozen=True)
class Rgbw:
    r: int
    g: int
    b: int
    w: int = 0


@dataclass
class Fire2012State:
    heat: list[int] = field(default_factory=lambda: [0] * RING_PIXEL_COUNT)
    last_step: int = 0
    origin_step: int = 0
    signature: int = 0
    ready: bool = False


def parse_params(raw: str | None, aliases: dict[str, int | float | str | None]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("params must be valid JSON") from error
        if not isinstance(decoded, dict):
            raise ValueError("params must be a JSON object")
        params.update(decoded)
    for key, value in aliases.items():
        if value is not None:
            params[key] = value
    return params


def render_preview_png(
    state: dict[str, Any],
    pattern: str,
    brightness: int,
    params: dict[str, Any],
    t_ms: int,
    width: int,
    height: int,
) -> bytes:
    if brightness < 0 or brightness > MAX_BRIGHTNESS:
        raise ValueError(f"brightness must be between 0 and {MAX_BRIGHTNESS}")
    if width < 80 or width > 2000 or height < 80 or height > 2000:
        raise ValueError("width and height must be between 80 and 2000")

    lanterns = [
        item
        for item in state.get("lanterns", [])
        if isinstance(item.get("x"), (int, float)) and isinstance(item.get("y"), (int, float))
    ]
    if not lanterns:
        raise ValueError("no positioned lanterns to preview")

    bg = (12, 14, 18)
    pixels = bytearray(bg * width * height)
    margin = max(18, min(width, height) // 18)
    radius = max(4, min(width, height) // 42)
    synced_us = int(t_ms) * 1000
    normalized = _normalize_pattern(pattern)

    for lantern in lanterns:
        x = float(lantern["x"])
        y = float(lantern["y"])
        colors = _pattern_pixels(
            normalized, brightness, params, synced_us, x, y,
            node_id=_lantern_node_id(lantern),
        )
        color = _average_rgbw(colors)
        rgb = _rgbw_to_preview_rgb(color)
        cx = round(margin + x * (width - 2 * margin))
        cy = round(margin + y * (height - 2 * margin))
        if normalized in {"fire_flicker", "fire2012"}:
            _draw_pixel_ring(pixels, width, height, cx, cy, radius, colors)
        else:
            _draw_disc(pixels, width, height, cx, cy, radius + 2, (34, 38, 46))
            _draw_disc(pixels, width, height, cx, cy, radius, rgb)

    return _encode_png(width, height, pixels)


def render_preview_data(
    state: dict[str, Any],
    pattern: str,
    brightness: int,
    params: dict[str, Any],
    t_ms: int,
    _fire_states: dict[str, Fire2012State] | None = None,
) -> dict[str, Any]:
    if brightness < 0 or brightness > MAX_BRIGHTNESS:
        raise ValueError(f"brightness must be between 0 and {MAX_BRIGHTNESS}")
    lanterns = [
        item
        for item in state.get("lanterns", [])
        if isinstance(item.get("x"), (int, float)) and isinstance(item.get("y"), (int, float))
    ]
    if not lanterns:
        raise ValueError("no positioned lanterns to preview")

    normalized = _normalize_pattern(pattern)
    synced_us = int(t_ms) * 1000
    rendered = []
    lumas = []
    ring_contrasts = []
    lit_count = 0
    for lantern in lanterns:
        x = float(lantern["x"])
        y = float(lantern["y"])
        fire_state = None
        if normalized == "fire2012" and _fire_states is not None:
            state_key = str(lantern.get("mac") or lantern.get("label") or f"{x:.4f},{y:.4f}")
            fire_state = _fire_states.setdefault(state_key, Fire2012State())
        colors = _pattern_pixels(
            normalized, brightness, params, synced_us, x, y,
            node_id=_lantern_node_id(lantern), fire_state=fire_state,
        )
        color = _average_rgbw(colors)
        rgb = _rgbw_to_preview_rgb(color)
        luma = _luma(rgb)
        pixel_rgbs = [_rgbw_to_preview_rgb(pixel) for pixel in colors]
        pixel_lumas = [_luma(pixel_rgb) for pixel_rgb in pixel_rgbs]
        ring_contrast = (max(pixel_lumas) - min(pixel_lumas)) / 255.0
        lumas.append(luma)
        ring_contrasts.append(ring_contrast)
        if any(max(pixel.r, pixel.g, pixel.b, pixel.w) > 0 for pixel in colors):
            lit_count += 1
        lantern_render = {
            "mac": lantern.get("mac"),
            "label": lantern.get("label"),
            "x": x,
            "y": y,
            "status": lantern.get("status"),
            "rgbw": [color.r, color.g, color.b, color.w],
            "rgb": list(rgb),
            "luma": round(luma, 3),
            "ring_contrast": round(ring_contrast, 4),
        }
        if normalized in {"fire_flicker", "fire2012"}:
            lantern_render["pixels"] = [
                {"rgbw": [pixel.r, pixel.g, pixel.b, pixel.w], "rgb": list(pixel_rgb)}
                for pixel, pixel_rgb in zip(colors, pixel_rgbs)
            ]
        rendered.append(lantern_render)

    avg_luma = sum(lumas) / len(lumas)
    min_luma = min(lumas)
    max_luma = max(lumas)
    return {
        "pattern": pattern,
        "brightness": brightness,
        "params": dict(params),
        "t": t_ms,
        "lanterns": rendered,
        "metrics": {
            "count": len(rendered),
            "lit_count": lit_count,
            "avg_luma": round(avg_luma, 3),
            "min_luma": round(min_luma, 3),
            "max_luma": round(max_luma, 3),
            "contrast": round((max_luma - min_luma) / 255.0, 4),
            "avg_ring_contrast": round(sum(ring_contrasts) / len(ring_contrasts), 4),
            "max_ring_contrast": round(max(ring_contrasts), 4),
        },
    }


def render_preview_frames(
    state: dict[str, Any],
    pattern: str,
    brightness: int,
    params: dict[str, Any],
    duration_ms: int,
    fps: int,
) -> dict[str, Any]:
    frame_times = _frame_times(duration_ms, fps)
    fire_states: dict[str, Fire2012State] = {}
    frames = [
        render_preview_data(state, pattern, brightness, params, t_ms, fire_states)
        for t_ms in frame_times
    ]
    return {
        "pattern": pattern,
        "brightness": brightness,
        "params": dict(params),
        "duration_ms": duration_ms,
        "fps": fps,
        "frame_count": len(frames),
        "frames": frames,
        "metrics": _sequence_metrics(frames),
    }


def review_preview(
    state: dict[str, Any],
    pattern: str,
    brightness: int,
    params: dict[str, Any],
    duration_ms: int,
    fps: int,
) -> dict[str, Any]:
    sequence = render_preview_frames(state, pattern, brightness, params, duration_ms, fps)
    metrics = sequence["metrics"]
    normalized = _normalize_pattern(pattern)
    issues: list[dict[str, str]] = []

    if brightness == 0:
        issues.append(_issue("error", "blackout", "Brightness is zero; the field will be dark."))
    elif brightness < 8:
        issues.append(_issue("warn", "very_dim", "Brightness is very low; confirm this is intentional."))
    elif brightness > 128:
        issues.append(_issue("warn", "high_brightness", "Brightness is high for battery-powered field use."))

    if metrics["max_lit_count"] == 0:
        issues.append(_issue("error", "no_lit_lanterns", "No positioned lantern is lit in the sampled window."))
    if metrics["avg_luma_mean"] < 2 and brightness > 0:
        issues.append(_issue("warn", "mostly_dark", "Average luma is near black across the sampled window."))
    if normalized in {"sweep", "wavefront", "palette_drift", "pulse", "firefly", "ocean_wave", "fire_flicker", "fire2012"} and metrics["temporal_luma_range"] < 1:
        issues.append(_issue("warn", "no_temporal_change", "The sampled window has almost no visible temporal change."))
    if normalized in {"sweep", "wavefront", "palette_drift", "ocean_wave"} and metrics["max_contrast"] < 0.02:
        issues.append(_issue("warn", "low_spatial_contrast", "The field has little spatial variation at the sampled times."))
    if normalized == "solid":
        issues.append(_issue("warn", "bench_pattern", "SOLID is a bench power pattern, not a show pattern."))

    score = 100
    for issue in issues:
        score -= 35 if issue["severity"] == "error" else 12
    if metrics["avg_luma_mean"] > 90:
        score -= 8
    if metrics["max_contrast"] > 0.6:
        score -= 4
    score = max(0, min(100, score))

    recommendations = _recommendations(normalized, issues, metrics)
    return {
        "ok": not any(issue["severity"] == "error" for issue in issues),
        "score": score,
        "rating": _rating(score, issues),
        "pattern": pattern,
        "brightness": brightness,
        "params": dict(params),
        "duration_ms": duration_ms,
        "fps": fps,
        "metrics": metrics,
        "issues": issues,
        "recommendations": recommendations,
    }


def _normalize_pattern(pattern: str) -> str:
    key = pattern.strip().lower().replace("_", " ").replace("-", " ")
    aliases = {
        "pulse": "pulse",
        "palette drift": "palette_drift",
        "drift": "palette_drift",
        "sweep": "sweep",
        "wavefront": "wavefront",
        "wave front": "wavefront",
        "solid": "solid",
        "glow": "glow",
        "firefly": "firefly",
        "hotaru": "firefly",
        "ocean wave": "ocean_wave",
        "ocean": "ocean_wave",
        "fire flicker": "fire_flicker",
        "fire": "fire_flicker",
        "flame": "fire_flicker",
        "fire2012": "fire2012",
        "fire 2012": "fire2012",
        "white": "white",
    }
    try:
        return aliases[key]
    except KeyError as error:
        raise ValueError(f"unknown pattern: {pattern}") from error


def _pattern_color(pattern: str, brightness: int, params: dict[str, Any], synced_us: int, x: float, y: float) -> Rgbw:
    if pattern == "pulse":
        hue = _number(params, "hue", "p0", default=0) % 360
        saturation, value = _standard_color(params)
        intensity = _pulse_intensity(synced_us, 4.0, 0.0)
        return _hsv_color(brightness, intensity, hue, saturation, value)
    if pattern == "palette_drift":
        period_s = _number(params, "period", "p0", default=8000) / 1000.0
        spatial = _number(params, "spatial", "p1", default=0) / 100.0
        hue = _drift_hue(synced_us, x, period_s, spatial)
        return _hsv_color(brightness, 1.0, hue * 360.0, 1.0, 1.0)
    if pattern == "sweep":
        period_s = _number(params, "period", "p0", default=4000) / 1000.0
        wavelength = _number(params, "wavelength", "spatial", "p1", default=300) / 100.0
        intensity = _sweep_intensity(synced_us, x, period_s, wavelength)
        return Rgbw(0, 0, 0, round(intensity * brightness))
    if pattern == "wavefront":
        period_s = _number(params, "p0", "period", default=6000) / 1000.0
        width_saturation = int(_number(params, "p1", "front_width", default=28))
        angle_value = int(_number(params, "p2", "angle", default=0))
        full_hsv = _color_value_present(angle_value)
        width_raw = (width_saturation & OCEAN_WAVELENGTH_MASK
                     if full_hsv else width_saturation)
        angle_raw = angle_value & OCEAN_ANGLE_MASK if full_hsv else angle_value
        width = (width_raw or 28) / 100.0
        hue = _number(params, "p3", "hue", default=200)
        hue = hue % 360 if full_hsv else (200 if hue == 0 else hue % 360)
        saturation = (((width_saturation >> 10) & 0x3F) / 63.0
                      if full_hsv else 1.0)
        value = (((angle_value >> 9) & 0x3F) / 63.0
                 if full_hsv else 1.0)
        intensity = _wavefront_intensity(
            synced_us, x, y, period_s, width, math.radians(angle_raw % 360)
        )
        return _hsv_color(brightness, intensity, hue, saturation, value)
    if pattern == "solid":
        return Rgbw(brightness, brightness, brightness, brightness)
    if pattern == "white":
        return Rgbw(0, 0, 0, brightness)
    if pattern == "glow":
        hue = _number(params, "hue", "p0", default=40) % 360
        saturation, value = _standard_color(params)
        return _hsv_color(brightness, 1.0, hue, saturation, value)
    if pattern == "firefly":
        # Firefly params are positional on the wire (p0..p3) to avoid the
        # hue/period collision on params[0]; accept the friendly names too.
        period_s = _number(params, "p0", "period", default=7000) / 1000.0
        hue = _number(params, "p1", "hue", default=58) % 360
        meta = int(_number(params, "p2", default=0))
        full_hsv = _color_value_present(meta)
        if full_hsv:
            scatter = min(meta & FIREFLY_SCATTER_MASK, 100) / 100.0
            value = ((meta >> 7) & 0xFF) / 255.0
        else:
            scatter_raw = _number(params, "scatter", "p2", default=100)
            scatter = (100 if scatter_raw <= 0 else min(scatter_raw, 100)) / 100.0
            value = 1.0
        chorus_meta = int(_number(params, "p3", default=0))
        if chorus_meta & FIREFLY_CHORUS_MARKER:
            saturation_raw = min(chorus_meta & 0x7F, 100)
            chorus_interval_s = (chorus_meta >> 7) & 0xFF
        else:
            saturation_raw = _number(params, "saturation", "p3", default=85)
            saturation_raw = (min(max(saturation_raw, 0), 100) if full_hsv
                              else (85 if saturation_raw <= 0 else min(saturation_raw, 100)))
            chorus_interval_s = _number(params, "chorus", default=36)
        intensity = _firefly_intensity(
            synced_us, x, y, period_s, scatter, chorus_interval_s
        )
        saturation = saturation_raw / 100.0
        return _hsv_color(brightness, intensity, hue, saturation, value)
    if pattern == "ocean_wave":
        # Positional params (p0..p3); accept the friendly names too.
        period_s = _number(params, "p0", "period", default=9000) / 1000.0
        wavelength_saturation = int(_number(params, "p1", "wavelength", default=100))
        angle_value = int(_number(params, "p2", "angle", default=45))
        full_hsv = _color_value_present(angle_value)
        wavelength_raw = (wavelength_saturation & OCEAN_WAVELENGTH_MASK
                          if full_hsv else wavelength_saturation)
        angle_raw = angle_value & OCEAN_ANGLE_MASK if full_hsv else angle_value
        wavelength = (wavelength_raw or 100) / 100.0
        angle_rad = math.radians(angle_raw % 360)
        hue_base = _number(params, "p3", "hue", default=205)
        hue_base = hue_base % 360 if full_hsv else (205 if hue_base == 0 else hue_base % 360)
        base_saturation = (((wavelength_saturation >> 10) & 0x3F) / 63.0
                           if full_hsv else 1.0)
        base_value = (((angle_value >> 9) & 0x3F) / 63.0
                      if full_hsv else 1.0)
        n = _ocean_intensity(synced_us, x, y, period_s, wavelength, angle_rad)
        foam = max(0.0, min(1.0, (n - 0.72) / 0.28))
        foam *= foam
        value = base_value * (0.14 + 0.86 * (n ** 1.3))
        hue = (hue_base + 10.0 - 27.0 * n - 8.0 * foam) / 360.0
        saturation = min(1.0, max(0.0, ((0.98 - 0.15 * n) - 0.70 * foam) * base_saturation))
        if not full_hsv:
            saturation = max(0.06, saturation)
        return _hsv_color(brightness, 1.0, hue * 360.0, saturation, value)
    raise ValueError(f"unknown pattern: {pattern}")


def _pattern_pixels(
    pattern: str,
    brightness: int,
    params: dict[str, Any],
    synced_us: int,
    x: float,
    y: float,
    *,
    node_id: int = 0,
    fire_state: Fire2012State | None = None,
) -> list[Rgbw]:
    if pattern == "fire2012":
        fps = int(_number(params, "p0", "speed", default=30)) or 30
        cooling = int(_number(params, "p1", "cooling", default=55)) or 55
        sparking = int(_number(params, "p2", "sparking", default=120)) or 120
        fps = fps if 10 <= fps <= 60 else 30
        cooling = max(0, min(255, cooling))
        sparking = max(0, min(255, sparking))
        current = fire_state or Fire2012State()
        seed = _fire2012_node_seed(x, y, node_id)
        _fire2012_prepare(
            current, synced_us, seed, RING_PIXEL_COUNT, fps, cooling, sparking
        )
        return [
            _fire2012_color(heat, brightness)
            for heat in current.heat[:RING_PIXEL_COUNT]
        ]

    if pattern != "fire_flicker":
        color = _pattern_color(pattern, brightness, params, synced_us, x, y)
        return [color] * RING_PIXEL_COUNT

    period_s = _number(params, "p0", "period", default=1200) / 1000.0
    hue = _number(params, "p1", "hue", default=24) % 360
    meta = int(_number(params, "p2", default=0))
    full_hsv = _color_value_present(meta)
    if full_hsv:
        texture = min(meta & FIREFLY_SCATTER_MASK, 100) / 100.0
        value = ((meta >> 7) & 0xFF) / 255.0
    else:
        texture_raw = _number(params, "texture", "p2", default=85)
        texture = (85 if texture_raw <= 0 else min(texture_raw, 100)) / 100.0
        value = 1.0
    saturation = _number(params, "p3", "saturation", default=95)
    saturation = (min(max(saturation, 0), 100) if full_hsv
                  else (95 if saturation <= 0 else min(saturation, 100))) / 100.0

    pixels = []
    for pixel_index in range(RING_PIXEL_COUNT):
        intensity, heat = _fire_flicker_sample(
            synced_us, x, y, pixel_index, RING_PIXEL_COUNT, period_s, texture
        )
        hue_shift = 18.0 * (intensity - 0.55) + 5.0 * heat
        pixels.append(_hsv_color(
            brightness, intensity, hue + hue_shift, saturation, value
        ))
    return pixels


def _lantern_node_id(lantern: dict[str, Any]) -> int:
    value = lantern.get("node_id")
    if isinstance(value, int) and value > 0:
        return value
    label = str(lantern.get("label") or "")
    if label.startswith("#") and label[1:].isdigit():
        return int(label[1:])
    return 0


def _average_rgbw(colors: list[Rgbw]) -> Rgbw:
    count = len(colors)
    return Rgbw(
        round(sum(color.r for color in colors) / count),
        round(sum(color.g for color in colors) / count),
        round(sum(color.b for color in colors) / count),
        round(sum(color.w for color in colors) / count),
    )


def _frame_times(duration_ms: int, fps: int) -> list[int]:
    if duration_ms < 500 or duration_ms > 60_000:
        raise ValueError("duration_ms must be between 500 and 60000")
    if fps < 1 or fps > 24:
        raise ValueError("fps must be between 1 and 24")
    step_ms = max(1, round(1000 / fps))
    frame_count = max(1, math.ceil(duration_ms / step_ms))
    if frame_count > MAX_PREVIEW_FRAMES:
        raise ValueError(f"preview is limited to {MAX_PREVIEW_FRAMES} frames")
    return [i * step_ms for i in range(frame_count)]


def _sequence_metrics(frames: list[dict[str, Any]]) -> dict[str, Any]:
    frame_metrics = [frame["metrics"] for frame in frames]
    lit_counts = [metric["lit_count"] for metric in frame_metrics]
    avg_lumas = [metric["avg_luma"] for metric in frame_metrics]
    contrasts = [metric["contrast"] for metric in frame_metrics]
    ring_contrasts = [metric["max_ring_contrast"] for metric in frame_metrics]
    return {
        "min_lit_count": min(lit_counts),
        "max_lit_count": max(lit_counts),
        "avg_lit_count": round(sum(lit_counts) / len(lit_counts), 3),
        "avg_luma_min": round(min(avg_lumas), 3),
        "avg_luma_max": round(max(avg_lumas), 3),
        "avg_luma_mean": round(sum(avg_lumas) / len(avg_lumas), 3),
        "temporal_luma_range": round(max(avg_lumas) - min(avg_lumas), 3),
        "min_contrast": round(min(contrasts), 4),
        "max_contrast": round(max(contrasts), 4),
        "avg_contrast": round(sum(contrasts) / len(contrasts), 4),
        "max_ring_contrast": round(max(ring_contrasts), 4),
        "avg_ring_contrast": round(sum(ring_contrasts) / len(ring_contrasts), 4),
    }


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _rating(score: int, issues: list[dict[str, str]]) -> str:
    if any(issue["severity"] == "error" for issue in issues):
        return "reject"
    if score >= 85:
        return "strong"
    if score >= 70:
        return "usable"
    return "needs_review"


def _recommendations(normalized: str, issues: list[dict[str, str]], metrics: dict[str, Any]) -> list[str]:
    codes = {issue["code"] for issue in issues}
    recommendations = []
    if "blackout" in codes or "mostly_dark" in codes or "very_dim" in codes:
        recommendations.append("Raise brightness or sample a brighter phase before broadcasting.")
    if "high_brightness" in codes:
        recommendations.append("Lower brightness unless this is a short bench or special-event cue.")
    if "no_temporal_change" in codes and normalized == "sweep":
        recommendations.append("Shorten period or sample a longer duration to verify the sweep motion.")
    if "low_spatial_contrast" in codes and normalized == "palette_drift":
        recommendations.append("Increase spatial spread if the field should show a color gradient.")
    if "low_spatial_contrast" in codes and normalized == "sweep":
        recommendations.append("Reduce wavelength if the field should show a visible moving wave.")
    if "low_spatial_contrast" in codes and normalized == "ocean_wave":
        recommendations.append("Shorten the wavelength or change the angle so the swell reads across the field.")
    if metrics["avg_luma_mean"] > 90:
        recommendations.append("Average luma is high; check battery and glare before running this for long periods.")
    if not recommendations:
        recommendations.append("No blocking issues found in the sampled preview window.")
    return recommendations


def _number(params: dict[str, Any], *keys: str, default: float) -> float:
    for key in keys:
        value = params.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{key} must be numeric") from error
    return default


def _color_value_present(packed: int) -> bool:
    return bool(packed & COLOR_VALUE_MARKER)


def _standard_color(params: dict[str, Any]) -> tuple[float, float]:
    packed_value = int(_number(params, "p2", default=0))
    full_hsv = _color_value_present(packed_value)
    saturation = _number(params, "saturation", "p1", default=100)
    if full_hsv:
        return min(max(saturation, 0), 100) / 100.0, (packed_value & 0xFF) / 255.0
    if saturation <= 0:
        saturation = 100
    return min(saturation, 100) / 100.0, 1.0


def _phase(t_us: int, period_s: float) -> float:
    return (t_us / 1_000_000.0 / period_s) % 1.0


def _pulse_intensity(synced_us: int, period_s: float, spatial: float) -> float:
    p = _phase(synced_us, period_s) + spatial
    return 0.5 * (1.0 - math.cos(2.0 * math.pi * p))


def _sweep_intensity(synced_us: int, x: float, period_s: float, wavelength: float) -> float:
    if period_s <= 0 or wavelength <= 0:
        raise ValueError("period and wavelength must be positive")
    ph = synced_us / 1_000_000.0 / period_s - x / wavelength
    p = ph - math.floor(ph)
    return 0.5 * (1.0 - math.cos(2.0 * math.pi * p))


def _wavefront_intensity(
    synced_us: int,
    x: float,
    y: float,
    period_s: float,
    width: float,
    angle_rad: float,
) -> float:
    if period_s <= 0:
        period_s = 6.0
    width = min(1.0, max(0.04, width))
    cx = math.cos(angle_rad)
    cy = math.sin(angle_rad)
    minimum = min(0.0, cx) + min(0.0, cy)
    maximum = max(0.0, cx) + max(0.0, cy)
    span = maximum - minimum
    projection = ((x * cx + y * cy - minimum) / span
                  if span > 1e-5 else 0.5)
    projection = min(1.0, max(0.0, projection))
    center = -width + _phase(synced_us, period_s) * (1.0 + 2.0 * width)
    distance = abs(projection - center)
    if distance >= width:
        return 0.0
    edge = 1.0 - distance / width
    return edge * edge * (3.0 - 2.0 * edge)


def _smoothstep01(x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    return x * x * (3.0 - 2.0 * x)


def _firefly_stagger(x: float, y: float, scatter: float) -> float:
    h = x * 0.7548 + y * 0.5698 + x * y * 0.3821
    return (h - math.floor(h)) * scatter


def _firefly_flash_frac(period_s: float) -> float:
    if period_s <= 0:
        return 0.45
    dark_gap_s = 0.41 * period_s
    if period_s > 5.0:
        dark_gap_s = 0.41 * 5.0 + 0.21 * (period_s - 5.0)
    if dark_gap_s < 0.6:
        dark_gap_s = 0.6
    return max(0.05, min(0.95, 1.0 - dark_gap_s / period_s))


def _firefly_regular_intensity(
    synced_us: int, x: float, y: float, period_s: float, scatter: float
) -> float:
    if period_s <= 0:
        raise ValueError("period must be positive")
    p = _phase(synced_us, period_s) + _firefly_stagger(x, y, scatter)
    p -= math.floor(p)
    flash_frac = _firefly_flash_frac(period_s)
    if p >= flash_frac:
        return 0.0
    u = p / flash_frac
    attack = 0.28
    if u < attack:
        env = _smoothstep01(u / attack)
    else:
        env = 1.0 - _smoothstep01((u - attack) / (1.0 - attack))
    shimmer = 1.0 - 0.12 * (0.5 - 0.5 * math.cos(2.0 * math.pi * 6.0 * u))
    return max(0.0, min(1.0, env * shimmer))


def _firefly_node_seed(x: float, y: float) -> int:
    xq = _lround(x * 10000.0)
    yq = _lround(y * 10000.0)
    return _fire2012_mix(
        _u32(xq * 0x9E3779B1) ^ _u32(yq * 0x85EBCA6B) ^ 0xF1EF17
    )


def _firefly_random01(seed: int, epoch: int, stream: int) -> float:
    raw_epoch = epoch & 0xFFFFFFFFFFFFFFFF
    value = seed ^ _u32(raw_epoch * 0x9E3779B1)
    value ^= _u32((raw_epoch >> 32) * 0x85EBCA6B)
    value ^= _u32(stream * 0xC2B2AE35)
    return (_fire2012_mix(value) >> 8) / 16777216.0


def _firefly_random_solo_intensity(
    synced_us: int, x: float, y: float, period_s: float
) -> float:
    if period_s <= 0:
        period_s = 7.0
    window_us = max(1, _lround(period_s * 1_000_000.0))
    epoch = synced_us // window_us
    seed = _firefly_node_seed(x, y)
    if _firefly_random01(seed, epoch, 0) < 0.17:
        return 0.0
    start = epoch * window_us + period_s * 1_000_000.0 * (
        0.06 + 0.60 * _firefly_random01(seed, epoch, 1)
    )
    duration = period_s * 1_000_000.0 * (
        0.18 + 0.12 * _firefly_random01(seed, epoch, 2)
    )
    u = (synced_us - start) / duration
    if u < 0.0 or u >= 1.0:
        return 0.0
    attack = 0.19
    env = (_smoothstep01(u / attack) if u < attack
           else 1.0 - _smoothstep01((u - attack) / (1.0 - attack)))
    amplitude = 0.72 + 0.28 * _firefly_random01(seed, epoch, 3)
    shimmer_phase = _firefly_random01(seed, epoch, 4)
    shimmer = 0.94 + 0.06 * math.cos(
        2.0 * math.pi * (5.0 * u + shimmer_phase)
    )
    return max(0.0, min(1.0, amplitude * env * shimmer))


def _firefly_chorus_beat(beat_phase: float) -> float:
    beat_phase %= 1.0
    if beat_phase >= 0.72:
        return 0.0
    u = beat_phase / 0.72
    attack = 0.18
    return (_smoothstep01(u / attack) if u < attack
            else 1.0 - _smoothstep01((u - attack) / (1.0 - attack)))


def _firefly_intensity(
    synced_us: int,
    x: float,
    y: float,
    period_s: float,
    scatter: float,
    chorus_interval_s: float = 36.0,
) -> float:
    if period_s <= 0:
        period_s = 7.0
    scatter = min(1.0, max(0.0, scatter))
    regular = _firefly_regular_intensity(synced_us, x, y, period_s, 0.0)
    random = _firefly_random_solo_intensity(synced_us, x, y, period_s)
    solo = regular * (1.0 - scatter) + random * scatter
    if chorus_interval_s < 8.0:
        return solo
    beat_period_s = 1.35
    chorus_duration_s = beat_period_s * 3.0
    chorus_interval_s = max(chorus_interval_s, chorus_duration_s + 2.0)
    cycle_s = _phase(synced_us, chorus_interval_s) * chorus_interval_s
    chorus_start_s = chorus_interval_s - chorus_duration_s
    if cycle_s < chorus_start_s:
        return solo
    local_s = cycle_s - chorus_start_s
    synced = _firefly_chorus_beat(local_s / beat_period_s)
    crossfade_s = 0.38
    blend_in = _smoothstep01(local_s / crossfade_s)
    blend_out = 1.0 - _smoothstep01(
        (local_s - (chorus_duration_s - crossfade_s)) / crossfade_s
    )
    blend = blend_in * blend_out
    return solo * (1.0 - blend) + synced * blend


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _fire2012_mix(value: int) -> int:
    value = _u32(value)
    value ^= value >> 16
    value = _u32(value * 0x7FEB352D)
    value ^= value >> 15
    value = _u32(value * 0x846CA68B)
    value ^= value >> 16
    return _u32(value)


def _lround(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _fire2012_node_seed(x: float, y: float, node_id: int) -> int:
    xq = _lround(x * 10000.0)
    yq = _lround(y * 10000.0)
    seed = _u32(xq * 0x9E3779B1)
    seed ^= _u32(yq * 0x85EBCA6B)
    seed ^= _u32(node_id * 0xC2B2AE35)
    return _fire2012_mix(seed ^ 0x20120718)


def _fire2012_random8(seed: int, step: int, stream: int) -> int:
    raw_step = step & 0xFFFFFFFFFFFFFFFF
    value = seed ^ _u32(raw_step * 0x9E3779B1)
    value ^= _u32((raw_step >> 32) * 0x85EBCA6B)
    value ^= _u32(stream * 0xC2B2AE35)
    return _fire2012_mix(value) >> 24


def _fire2012_signature(seed: int, cell_count: int, fps: int, cooling: int, sparking: int) -> int:
    config = cell_count | (fps << 8) | (cooling << 16) | (sparking << 24)
    return _fire2012_mix(seed ^ _u32(config))


def _fire2012_step(
    state: Fire2012State,
    step: int,
    seed: int,
    cell_count: int,
    cooling: int,
    sparking: int,
) -> None:
    cooling_limit = min(256, (cooling * 10) // cell_count + 2)
    for index in range(cell_count):
        loss = _fire2012_random8(seed, step, index) % cooling_limit
        state.heat[index] = max(0, state.heat[index] - loss)
    for index in range(cell_count - 1, 1, -1):
        state.heat[index] = (
            state.heat[index - 1] + state.heat[index - 2] * 2
        ) // 3
    if _fire2012_random8(seed, step, 0x100) < sparking:
        base_cells = min(7, cell_count)
        index = _fire2012_random8(seed, step, 0x101) % base_cells
        added = 160 + _fire2012_random8(seed, step, 0x102) % 95
        state.heat[index] = min(255, state.heat[index] + added)


def _fire2012_prepare(
    state: Fire2012State,
    synced_us: int,
    seed: int,
    cell_count: int,
    fps: int,
    cooling: int,
    sparking: int,
) -> None:
    signature = _fire2012_signature(seed, cell_count, fps, cooling, sparking)
    target_step = (synced_us * fps) // 1_000_000
    block_steps = fps * 20
    origin_step = (target_step // block_steps) * block_steps - block_steps
    rebuild = (
        not state.ready
        or state.signature != signature
        or state.origin_step != origin_step
        or target_step < state.last_step
    )
    if rebuild:
        state.heat = [0] * cell_count
        state.signature = signature
        state.origin_step = origin_step
        state.last_step = origin_step
        state.ready = True
    while state.last_step < target_step:
        state.last_step += 1
        _fire2012_step(
            state, state.last_step, seed, cell_count, cooling, sparking
        )


def _fire2012_heat_color(temperature: int) -> tuple[int, int, int]:
    t192 = (temperature * 191 + 255) >> 8
    heat_ramp = (t192 & 0x3F) << 2
    if t192 & 0x80:
        return 255, 255, heat_ramp
    if t192 & 0x40:
        return 255, heat_ramp, 0
    return heat_ramp, 0, 0


def _fire2012_color(temperature: int, brightness: int) -> Rgbw:
    red, green, blue = _fire2012_heat_color(temperature)
    return Rgbw(
        (red * brightness + 127) // 255,
        (green * brightness + 127) // 255,
        (blue * brightness + 127) // 255,
        0,
    )


def _fire_flicker_sample(
    synced_us: int,
    x: float,
    y: float,
    pixel_index: int,
    pixel_count: int,
    period_s: float,
    texture: float,
) -> tuple[float, float]:
    if period_s <= 0:
        raise ValueError("period must be positive")
    texture = max(0.0, min(1.0, texture))
    count = pixel_count or 1
    around = (pixel_index % count) / count
    node = _firefly_stagger(x, y, 1.0)

    billow_a = math.sin(2.0 * math.pi * (_phase(synced_us, period_s * 1.47) + node))
    billow_b = math.sin(2.0 * math.pi * (_phase(synced_us, period_s * 0.73) - node * 0.61))
    global_level = 0.63 + 0.13 * billow_a + 0.09 * billow_b
    tongue_a = math.sin(2.0 * math.pi * (
        _phase(synced_us, period_s * 0.83) + 2.0 * around + node * 0.37
    ))
    tongue_b = math.sin(2.0 * math.pi * (
        _phase(synced_us, period_s * 0.41) - 3.0 * around + node * 0.79
    ))
    tongue_c = math.sin(2.0 * math.pi * (
        _phase(synced_us, period_s * 0.19) + 5.0 * around - node * 0.53
    ))
    local = 0.20 * tongue_a + 0.12 * tongue_b + 0.08 * tongue_c
    intensity = max(0.08, min(1.0, global_level + texture * local))
    heat = max(-1.0, min(1.0, texture * local / 0.40))
    return intensity, heat


def _ocean_component(synced_us: int, x: float, y: float, cx: float, cy: float, period_s: float, wavelength: float) -> float:
    if period_s <= 0 or wavelength <= 0:
        raise ValueError("period and wavelength must be positive")
    secs = synced_us / 1_000_000.0
    proj = x * cx + y * cy
    ph = secs / period_s - proj / wavelength
    ph -= math.floor(ph)  # reduce to [0,1); mirrors the firmware precision guard
    return math.sin(2.0 * math.pi * ph)


def _ocean_intensity(synced_us: int, x: float, y: float, period_s: float, wavelength: float, angle_rad: float) -> float:
    c1, s1 = math.cos(angle_rad), math.sin(angle_rad)
    c2, s2 = math.cos(angle_rad + 0.40), math.sin(angle_rad + 0.40)
    c3, s3 = math.cos(angle_rad - 0.30), math.sin(angle_rad - 0.30)
    w1, w2, w3 = 0.60, 0.22, 0.34
    h = (
        w1 * _ocean_component(synced_us, x, y, c1, s1, period_s, wavelength)
        + w2 * _ocean_component(synced_us, x, y, c2, s2, period_s * 0.74, wavelength * 0.55)
        + w3 * _ocean_component(synced_us, x, y, c3, s3, period_s * 1.38, wavelength * 1.90)
    )
    n = 0.5 * (h / (w1 + w2 + w3)) + 0.5
    return max(0.0, min(1.0, n))


def _drift_hue(synced_us: int, x: float, period_s: float, spatial: float) -> float:
    if period_s <= 0:
        raise ValueError("period must be positive")
    h = synced_us / 1_000_000.0 / period_s + x * spatial
    return h - math.floor(h)


def _hsv_color(
    brightness: int,
    intensity: float,
    hue_degrees: float,
    saturation: float,
    value: float,
) -> Rgbw:
    r, g, b = _hsv_to_rgb(hue_degrees / 360.0, saturation, value)
    r = _srgb_to_linear(r)
    g = _srgb_to_linear(g)
    b = _srgb_to_linear(b)
    # Match firmware: the fixture's warm-white die is not a color-neutral
    # substitute for the common RGB component of a chromatic browser color.
    # Blend it only across the first 5% saturation around neutral gray.
    white_mix = 1.0 - min(max(saturation, 0.0) / 0.05, 1.0)
    w = min(r, g, b) * white_mix
    scale = brightness * max(0.0, min(1.0, intensity))
    return Rgbw(
        round((r - w) * scale),
        round((g - w) * scale),
        round((b - w) * scale),
        round(w * scale),
    )


def _srgb_to_linear(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[float, float, float]:
    h = h - math.floor(h)
    hf = h * 6.0
    i = int(hf)
    f = hf - i
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    match i % 6:
        case 0:
            return v, t, p
        case 1:
            return q, v, p
        case 2:
            return p, v, t
        case 3:
            return p, q, v
        case 4:
            return t, p, v
        case _:
            return v, p, q


def _rgbw_to_preview_rgb(color: Rgbw) -> tuple[int, int, int]:
    # Firmware bytes are linear-light PWM values. Recombine the natural-white
    # emitter with RGB in linear space, then encode back to sRGB for a screen.
    return (
        _linear_byte_to_srgb(color.r + color.w),
        _linear_byte_to_srgb(color.g + color.w),
        _linear_byte_to_srgb(color.b + color.w),
    )


def _linear_byte_to_srgb(value: int) -> int:
    linear = max(0.0, min(1.0, value / 255.0))
    srgb = (
        12.92 * linear
        if linear <= 0.0031308
        else 1.055 * (linear ** (1.0 / 2.4)) - 0.055
    )
    return _clamp_byte(round(srgb * 255.0))


def _clamp_byte(value: int) -> int:
    return max(0, min(255, int(value)))


def _luma(rgb: tuple[int, int, int]) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _draw_disc(
    pixels: bytearray,
    width: int,
    height: int,
    cx: int,
    cy: int,
    radius: int,
    color: tuple[int, int, int],
) -> None:
    r2 = radius * radius
    for y in range(max(0, cy - radius), min(height, cy + radius + 1)):
        for x in range(max(0, cx - radius), min(width, cx + radius + 1)):
            if (x - cx) * (x - cx) + (y - cy) * (y - cy) > r2:
                continue
            idx = (y * width + x) * 3
            pixels[idx:idx + 3] = bytes(color)


def _draw_pixel_ring(
    pixels: bytearray,
    width: int,
    height: int,
    cx: int,
    cy: int,
    radius: int,
    colors: list[Rgbw],
) -> None:
    ring_radius = radius + 1
    led_radius = max(1, radius // 4)
    _draw_disc(pixels, width, height, cx, cy, ring_radius + led_radius + 1, (34, 38, 46))
    for index, color in enumerate(colors):
        angle = -math.pi / 2.0 + 2.0 * math.pi * index / len(colors)
        led_x = round(cx + ring_radius * math.cos(angle))
        led_y = round(cy + ring_radius * math.sin(angle))
        _draw_disc(
            pixels, width, height, led_x, led_y, led_radius,
            _rgbw_to_preview_rgb(color),
        )


def _encode_png(width: int, height: int, rgb_pixels: bytes | bytearray) -> bytes:
    scanlines = bytearray()
    stride = width * 3
    for y in range(height):
        scanlines.append(0)
        start = y * stride
        scanlines.extend(rgb_pixels[start:start + stride])
    return b"".join([
        b"\x89PNG\r\n\x1a\n",
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
        _png_chunk(b"IDAT", zlib.compress(bytes(scanlines), level=6)),
        _png_chunk(b"IEND", b""),
    ])


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )
