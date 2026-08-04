// Pattern rendering: turns the pure pattern math (pattern_math.h) into SK6812
// RGBW colors on a NeoPixelBus strip. The time/space behavior lives in pmath::
// so it can be unit-tested; this layer only maps intensity -> pixels.
#pragma once

#include <Arduino.h>
#include <NeoPixelBus.h>

#include "beacon.h"
#include "pattern_ids.h"  // PatternId enum + patternIsStatic (dependency-free)
#include "pattern_math.h"

namespace patterns {

inline RgbwColor scaledRgbw(const pmath::RgbwUnit& color, uint8_t brightness) {
  return RgbwColor((uint8_t)lroundf(color.r * brightness),
                   (uint8_t)lroundf(color.g * brightness),
                   (uint8_t)lroundf(color.b * brightness),
                   (uint8_t)lroundf(color.w * brightness));
}

inline RgbwColor hsvColor(uint8_t brightness, float intensity,
                          const uint16_t params[4]) {
  float h = (params[0] % 360) / 360.0f;
  bool full_hsv = pmath::colorValuePresent(params[2]);
  uint16_t sp = full_hsv
                    ? (params[1] > 100 ? 100 : params[1])
                    : (params[1] ? (params[1] > 100 ? 100 : params[1]) : 100);
  pmath::RgbwUnit color = pmath::hsvToRgbw(
      h, sp / 100.0f, pmath::colorValueDecode(params[2]), intensity);
  return scaledRgbw(color, brightness);
}

// Uniform breathing pulse in the selected hue — every node in unison.
//   params[0] = hue in degrees, 0-359  (e.g. 30 = orange, 40 = amber)
//   params[1] = saturation percent; zero is valid when params[2] is marked
//   params[2] = full-HSV marker plus 8-bit sRGB value (legacy zero = full value)
inline RgbwColor pulse(int64_t synced_us, uint8_t brightness,
                       const uint16_t params[4]) {
  float s = pmath::pulseIntensity(synced_us, /*period_s*/ 4.0f, /*spatial*/ 0.0f);
  return hsvColor(brightness, s, params);
}

// Position-aware sweep: a wave of brightness travels across the field, so the
// pulse physically moves from lantern to lantern. params let the conductor tune
// it live: params[0] = period in ms, params[1] = wavelength in hundredths of a
// coordinate unit (both fall back to sensible defaults when 0).
inline RgbwColor sweep(int64_t synced_us, uint8_t brightness, float x, float y,
                       const uint16_t params[4]) {
  float period_s = params[0] ? params[0] / 1000.0f : 4.0f;
  float wavelength = params[1] ? params[1] / 100.0f : 3.0f;
  float s = pmath::sweepIntensity(synced_us, x, period_s, wavelength);
  return RgbwColor(0, 0, 0, (uint8_t)lroundf(s * brightness));
}

// Smooth rainbow drift: each node's color sweeps through the full color wheel
// (red -> orange -> yellow -> green -> blue -> violet -> red) over period_s. With
// a nonzero spatial term the hue is offset by position, so the rainbow travels
// across the field; the default (0) cycles every ring in unison. Colors are fully
// saturated, so the reserved neutral-white component remains zero.
//   params[0] = full-cycle period in ms              (default 8000)
//   params[1] = spatial hue offset, hundredths of a cycle per x unit (default 0)
inline RgbwColor paletteDrift(int64_t synced_us, uint8_t brightness, float x,
                              const uint16_t params[4]) {
  float period_s = params[0] ? params[0] / 1000.0f : 8.0f;
  float spatial = params[1] / 100.0f;  // 0 => unison
  float h = pmath::driftHue(synced_us, x, period_s, spatial);
  return scaledRgbw(pmath::hsvToRgbw(h, /*s*/ 1.0f, /*v*/ 1.0f,
                                    /*intensity*/ 1.0f),
                    brightness);
}

// Worst-case power draw: every pixel lit full white on all four RGBW channels at
// `brightness`. Not a show pattern — it's the bench rig for measuring the per-node
// LED ceiling (step brightness to trace the draw curve). Any real pattern draws
// strictly less at the same brightness, so if SOLID fits the budget, all do.
inline RgbwColor solid(uint8_t brightness) {
  return RgbwColor(brightness, brightness, brightness, brightness);
}

// Steady neutral white using only the SK6812 white channel. This is distinct
// from SOLID, which deliberately drives RGB and W together for worst-case bench
// power measurement.
inline RgbwColor white(uint8_t brightness) {
  return RgbwColor(0, 0, 0, brightness);
}

// Steady solid color: a constant hue with NO time dependence, so the whole field
// holds one calm color and the LED draw is flat (no pulse). Fits the warm/gentle
// aesthetic and is the realistic-conservative pattern for power measurement (a
// steady draw isolates the radio duty-cycle from LED variation). Neutral grays
// use the white emitter; chromatic pastels stay on RGB so their tint is not
// washed out by the fixture's warm-white die.
//   params[0] = hue in degrees, 0-359  (e.g. 30 = orange, 50 = amber/yellow)
//   params[1] = saturation percent; zero is valid when params[2] is marked
//   params[2] = full-HSV marker plus 8-bit sRGB value (legacy zero = full value)
inline RgbwColor glow(uint8_t brightness, const uint16_t params[4]) {
  return hsvColor(brightness, /*intensity*/ 1.0f, params);
}

// Firefly ("hotaru"): each node glows up, shimmers, and fades on its own cycle,
// staggered across the field by position so the lanterns twinkle like fireflies
// in a meadow. Warm yellow-green by default; all knobs are live-tunable. Because
// this pattern needs four independent knobs, its params are positional (the
// control plane sends p0..p3 directly rather than the hue/period aliases, which
// would collide on params[0]).
//   params[0] = full cycle period in ms                    (default 7000)
//   params[1] = hue in degrees, 0-359                       (default 58 = warm gold-green)
//   params[2] = marker + 8-bit value + 7-bit scatter         (legacy: plain scatter)
//   params[3] = saturation percent, including zero           (legacy default 85)
inline RgbwColor firefly(int64_t synced_us, uint8_t brightness, float x, float y,
                         const uint16_t params[4]) {
  float period_s = params[0] ? params[0] / 1000.0f : 7.0f;
  float hue = (params[1] % 360) / 360.0f;
  bool full_hsv = pmath::colorValuePresent(params[2]);
  uint16_t scatter_pct = pmath::fireflyScatterDecode(params[2]);
  uint16_t sat_pct = full_hsv
                         ? (params[3] > 100 ? 100 : params[3])
                         : (params[3] ? (params[3] > 100 ? 100 : params[3]) : 85);
  float s = pmath::fireflyIntensity(synced_us, x, y, period_s, scatter_pct / 100.0f);
  return scaledRgbw(pmath::hsvToRgbw(hue, sat_pct / 100.0f,
                                    pmath::fireflyValueDecode(params[2]), s),
                    brightness);
}

// Fire flicker: use every addressable LED in the ring. A low ember bed and
// whole-lantern billow keep the ring visually cohesive; angular flame waves
// give neighboring pixels distinct brightness and warmer/cooler color. Params
// use the same compact value+texture metadata shape as Firefly.
//   params[0] = primary flicker period in ms                 (default 1200)
//   params[1] = middle flame hue in degrees, 0-359           (default 24)
//   params[2] = marker + 8-bit value + 7-bit texture depth   (legacy: texture)
//   params[3] = saturation percent                           (default 95)
inline RgbwColor fireFlickerPixel(int64_t synced_us, uint8_t brightness,
                                  float x, float y, uint16_t pixel_index,
                                  uint16_t pixel_count,
                                  const uint16_t params[4]) {
  float period_s = params[0] ? params[0] / 1000.0f : 1.2f;
  bool full_hsv = pmath::colorValuePresent(params[2]);
  float base_hue = ((full_hsv || params[1]) ? params[1] % 360 : 24) / 360.0f;
  uint16_t texture_pct = full_hsv
                             ? pmath::fireflyScatterDecode(params[2])
                             : (params[2] ? (params[2] > 100 ? 100 : params[2])
                                          : 85);
  uint16_t sat_pct = full_hsv
                         ? (params[3] > 100 ? 100 : params[3])
                         : (params[3] ? (params[3] > 100 ? 100 : params[3]) : 95);
  pmath::FireFlickerSample sample = pmath::fireFlickerSample(
      synced_us, x, y, pixel_index, pixel_count, period_s,
      texture_pct / 100.0f);
  // Hot/brighter pixels lean yellow; cooler/dimmer pixels lean red. The shift
  // stays modest so a chosen flame color remains recognizable.
  float hue_shift_degrees = 18.0f * (sample.intensity - 0.55f) +
                            5.0f * sample.heat;
  float hue = base_hue + hue_shift_degrees / 360.0f;
  return scaledRgbw(pmath::hsvToRgbw(hue, sat_pct / 100.0f,
                                    pmath::fireflyValueDecode(params[2]),
                                    sample.intensity),
                    brightness);
}

// Ocean wave: a soft 2-D swell of light rolls across the field. Deep saturated
// blue in the troughs; as the swell rises the color brightens, desaturates, and
// shifts toward cyan while retaining its chromatic tint on the RGB emitters.
// Positional params (the control plane sends p0..p3) because wavelength/angle
// have no serial name aliases.
//   params[0] = primary swell period in ms                (default 9000)
//   params[1] = 6-bit saturation + 10-bit wavelength       (legacy: wavelength)
//   params[2] = marker + 6-bit value + 9-bit angle          (legacy: angle)
//   params[3] = base (mid-water) hue in degrees, 0-359     (default 205 = ocean blue)
inline RgbwColor oceanWave(int64_t synced_us, uint8_t brightness, float x,
                           float y, const uint16_t params[4]) {
  float period_s = params[0] ? params[0] / 1000.0f : 9.0f;
  bool full_hsv = pmath::oceanColorPresent(params[2]);
  uint16_t wavelength_raw = pmath::oceanWavelengthDecode(params[1], params[2]);
  float wavelength = wavelength_raw ? wavelength_raw / 100.0f : 1.0f;
  uint16_t angle = pmath::oceanAngleDecode(params[2]);
  float angle_rad = (angle % 360) * (pmath::kPi / 180.0f);
  float hue_base = full_hsv ? (float)(params[3] % 360)
                            : (params[3] ? (float)(params[3] % 360) : 205.0f);
  float base_sat = pmath::oceanSaturationDecode(params[1], params[2]);
  float base_value = pmath::oceanValueDecode(params[2]);
  float n = pmath::oceanIntensity(synced_us, x, y, period_s, wavelength, angle_rad);
  // Foam gates to the sharpest crest tops only (top ~28%), quadratic onset so
  // the whitecap stays soft rather than a hard sparkle.
  float foam = (n - 0.72f) / 0.28f;
  if (foam < 0.0f) foam = 0.0f;
  if (foam > 1.0f) foam = 1.0f;
  foam *= foam;
  // H, S, V all move with height (brightness-only reads flat): deep indigo-blue
  // and dark in the troughs -> azure mid-water -> cyan crest -> desaturated
  // white foam. Value keeps a small floor so troughs still glow faintly.
  float value = base_value * (0.14f + 0.86f * powf(n, 1.3f));
  float hue = (hue_base + 10.0f - 27.0f * n - 8.0f * foam) / 360.0f;
  float sat = ((0.98f - 0.15f * n) - 0.70f * foam) * base_sat;
  if (!full_hsv && sat < 0.06f) sat = 0.06f;
  if (sat < 0.0f) sat = 0.0f;
  if (sat > 1.0f) sat = 1.0f;
  return scaledRgbw(pmath::hsvToRgbw(hue, sat, value, /*intensity*/ 1.0f),
                    brightness);
}

inline RgbwColor calibrationId(int64_t synced_us, uint8_t brightness,
                               uint16_t node_id, const uint16_t params[4]) {
  bool on = pmath::calibrationBitOn(
      synced_us,
      node_id,
      params[0] ? params[0] : 1000,
      params[1] ? params[1] : 4,
      params[2] ? params[2] : 1,
      params[3] ? params[3] : 1);
  if (!on) return RgbwColor(0, 0, 0, 0);
  return RgbwColor(brightness, (uint8_t)lroundf(brightness * 0.72f), 0, 0);
}

// Render one pattern into a NeoPixelBus strip. Most field patterns intentionally
// treat a lantern as one sample of f(x,y,t); ring-aware patterns can branch here
// and evaluate f(x,y,pixel,t) without changing the beacon or sync model.
template <typename StripT>
inline void render(StripT& strip, const PatternConfig& b, int64_t synced_us, float x,
                   float y, uint16_t node_id = 0) {
  if (b.pattern_id == FIRE_FLICKER) {
    uint16_t count = strip.PixelCount();
    for (uint16_t i = 0; i < count; i++) {
      strip.SetPixelColor(
          i, fireFlickerPixel(synced_us, b.brightness, x, y, i, count, b.params));
    }
    return;
  }
  RgbwColor c;
  switch (b.pattern_id) {
    case SWEEP:
      c = sweep(synced_us, b.brightness, x, y, b.params);
      break;
    case PALETTE_DRIFT:
      c = paletteDrift(synced_us, b.brightness, x, b.params);
      break;
    case SOLID:
      c = solid(b.brightness);
      break;
    case WHITE:
      c = white(b.brightness);
      break;
    case GLOW:
      c = glow(b.brightness, b.params);
      break;
    case FIREFLY:
      c = firefly(synced_us, b.brightness, x, y, b.params);
      break;
    case OCEAN_WAVE:
      c = oceanWave(synced_us, b.brightness, x, y, b.params);
      break;
    case CALIBRATION:
      c = calibrationId(synced_us, b.brightness, node_id, b.params);
      break;
    case PULSE:
    default:
      c = pulse(synced_us, b.brightness, b.params);
      break;
  }
  for (uint16_t i = 0; i < strip.PixelCount(); i++) strip.SetPixelColor(i, c);
}

}  // namespace patterns
