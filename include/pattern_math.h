// Pure pattern math — the time/space functions behind each pattern, with no
// dependency on the LED library. Patterns are f(x, y, t), so a pulse can travel
// across the physical field once real (x,y) coordinates arrive in Milestone 2.
// Kept separate from patterns.h so the wrap/continuity behavior is host-testable.
#pragma once

#include <math.h>
#include <stdint.h>

namespace pmath {

static constexpr float kPi = 3.14159265358979323846f;
static constexpr uint16_t kColorValueMarker = 0x8000u;
static constexpr uint16_t kFireflyScatterMask = 0x007Fu;
static constexpr uint16_t kOceanWavelengthMask = 0x03FFu;
static constexpr uint16_t kOceanAngleMask = 0x01FFu;

// Color value is metadata packed into otherwise-unused parameter bits. The
// marker distinguishes new full-HSV configs from legacy configs, where a zero
// saturation meant "use the old default". No beacon layout/protocol bump is
// needed, and existing saved patterns retain their legacy parameter defaults.
inline uint16_t colorValuePack(uint8_t value) {
  return (uint16_t)(kColorValueMarker | value);
}

inline bool colorValuePresent(uint16_t packed) {
  return (packed & kColorValueMarker) != 0;
}

inline float colorValueDecode(uint16_t packed) {
  return colorValuePresent(packed) ? (packed & 0xFFu) / 255.0f : 1.0f;
}

// Firefly already uses all four params. Keep scatter in the low 7 bits and put
// the 8-bit color value above it; bit 15 remains the full-HSV marker.
inline uint16_t fireflyMetaPack(uint8_t scatter, uint8_t value) {
  if (scatter > 100) scatter = 100;
  return (uint16_t)(kColorValueMarker | ((uint16_t)value << 7) | scatter);
}

inline uint8_t fireflyScatterDecode(uint16_t packed) {
  if (!colorValuePresent(packed)) {
    uint16_t scatter = packed ? packed : 100;
    return (uint8_t)(scatter > 100 ? 100 : scatter);
  }
  uint8_t scatter = (uint8_t)(packed & kFireflyScatterMask);
  return scatter > 100 ? 100 : scatter;
}

inline float fireflyValueDecode(uint16_t packed) {
  return colorValuePresent(packed)
             ? ((packed >> 7) & 0xFFu) / 255.0f
             : 1.0f;
}

// Ocean keeps wavelength and angle in their low bits. Six-bit saturation and
// value live in the high bits, giving 64 perceptual steps for each while leaving
// the current four-parameter wire shape intact.
inline uint16_t oceanWavelengthSaturationPack(uint16_t wavelength,
                                              uint8_t saturation_pct) {
  if (wavelength > kOceanWavelengthMask) wavelength = kOceanWavelengthMask;
  if (saturation_pct > 100) saturation_pct = 100;
  uint16_t sat6 = (uint16_t)lroundf(saturation_pct * 63.0f / 100.0f);
  return (uint16_t)(wavelength | (sat6 << 10));
}

inline uint16_t oceanAngleValuePack(uint16_t angle, uint8_t value) {
  uint16_t value6 = (uint16_t)lroundf(value * 63.0f / 255.0f);
  return (uint16_t)(kColorValueMarker | (value6 << 9) |
                    (angle & kOceanAngleMask));
}

inline bool oceanColorPresent(uint16_t angle_value) {
  return colorValuePresent(angle_value);
}

inline uint16_t oceanWavelengthDecode(uint16_t packed,
                                      uint16_t angle_value) {
  return oceanColorPresent(angle_value) ? packed & kOceanWavelengthMask : packed;
}

inline uint16_t oceanAngleDecode(uint16_t packed) {
  return oceanColorPresent(packed) ? packed & kOceanAngleMask : packed;
}

inline float oceanSaturationDecode(uint16_t wavelength_saturation,
                                   uint16_t angle_value) {
  return oceanColorPresent(angle_value)
             ? ((wavelength_saturation >> 10) & 0x3Fu) / 63.0f
             : 1.0f;
}

inline float oceanValueDecode(uint16_t angle_value) {
  return oceanColorPresent(angle_value)
             ? ((angle_value >> 9) & 0x3Fu) / 63.0f
             : 1.0f;
}

// Map microseconds to a phase in [0,1) over `period_s` seconds. Continuous and
// monotonic within a period; wraps cleanly at the boundary (no visible hitch).
inline float phase(int64_t t_us, float period_s) {
  double secs = (double)t_us / 1e6;
  double p = fmod(secs / (double)period_s, 1.0);
  if (p < 0) p += 1.0;  // fmod keeps the sign of the dividend; force [0,1)
  return (float)p;
}

// Breathing pulse intensity in [0,1]: a smooth raised cosine. `spatial` shifts
// the phase per-node so the field can ripple; it is 0 for Milestone 1.
inline float pulseIntensity(int64_t synced_us, float period_s, float spatial) {
  float p = phase(synced_us, period_s) + spatial;
  return 0.5f * (1.0f - cosf(2.0f * kPi * p));
}

// Floored division: rounds toward negative infinity (unlike C's truncation), so
// the heartbeat parity is correct even if synced time briefly goes negative.
inline int64_t floorDiv(int64_t a, int64_t b) {
  int64_t q = a / b;
  if ((a % b != 0) && ((a < 0) != (b < 0))) q--;
  return q;
}

// Traveling-wave sweep: a brightness wave that moves across the field in +x, so
// a pulse physically travels from one lantern to the next. A node at position x
// sees the same waveform as a node at x=0, delayed by period_s * x / wavelength.
//   period_s    time for one full cycle of the wave
//   wavelength  spatial distance between successive wave peaks (same units as x)
// Returns intensity in [0,1] (raised cosine).
inline float sweepIntensity(int64_t synced_us, float x, float period_s,
                            float wavelength) {
  double secs = (double)synced_us / 1e6;
  double ph = secs / (double)period_s - (double)x / (double)wavelength;
  double p = ph - floor(ph);  // wrap to [0,1)
  return 0.5f * (1.0f - cosf(2.0f * kPi * (float)p));
}

// Rainbow hue for a color drift: cycles 0->1 over period_s (one full trip around
// the color wheel), offset by position so the field can show a *moving* rainbow.
//   period_s  time for one full hue cycle
//   spatial   hue offset in cycles per unit x (0 => every node shares one hue)
// Returns hue in [0,1), wrapping cleanly (hue 1 == hue 0 == red, so the wrap is
// seamless on the wheel).
inline float driftHue(int64_t synced_us, float x, float period_s, float spatial) {
  double secs = (double)synced_us / 1e6;
  double h = secs / (double)period_s + (double)x * (double)spatial;
  return (float)(h - floor(h));
}

// Smoothstep 0->1 with zero slope at both ends (the classic cubic). Clamps
// outside [0,1]. Used to give the firefly flash a soft, gentle swell/fade.
inline float smoothstep01(float x) {
  if (x <= 0.0f) return 0.0f;
  if (x >= 1.0f) return 1.0f;
  return x * x * (3.0f - 2.0f * x);
}

// Deterministic per-node phase offset in [0,1) derived from position, so each
// lantern flashes on its own schedule and the field twinkles instead of blinking
// in unison. Kept to a linear combination plus one cross term (no sin-hash) so it
// is numerically stable in float — the host preview and the device agree. The
// cross term breaks the diagonal gradient a purely linear offset would produce.
// `scatter` in [0,1] scales how far positions push the phase apart (smaller =>
// the flashes cluster/synchronize; 1 => fully spread).
inline float fireflyStagger(float x, float y, float scatter) {
  float h = x * 0.7548f + y * 0.5698f + x * y * 0.3821f;
  h -= floorf(h);
  return h * scatter;
}

// Fraction of each firefly cycle the lantern is lit (the rest is the dark gap
// between flashes). The lantern stays lit for most of the cycle and dark only
// briefly: the dark gap is ~25% shorter than a flat 55% off at the 5 s baseline,
// and for longer periods it grows only slowly (~0.21 s per extra second past
// 5 s) so the added seconds become glow, not darkness — a 7-10 s firefly is on
// most of the time, off sparingly.
inline float fireflyFlashFrac(float period_s) {
  if (period_s <= 0.0f) return 0.45f;
  float dark_gap_s = 0.41f * period_s;
  if (period_s > 5.0f) dark_gap_s = 0.41f * 5.0f + 0.21f * (period_s - 5.0f);
  if (dark_gap_s < 0.6f) dark_gap_s = 0.6f;  // always keep a brief blink-off
  float frac = 1.0f - dark_gap_s / period_s;
  if (frac < 0.05f) frac = 0.05f;
  if (frac > 0.95f) frac = 0.95f;
  return frac;
}

// Firefly ("hotaru") flash intensity in [0,1]. Each node is lit for most of the
// cycle, then settles into a brief dark gap: a fast-ish swell up to a peak that
// gently shimmers, followed by a slower fade back to dark — the asymmetric
// attack/decay envelope real fireflies show. Nodes are staggered by position
// (see fireflyStagger) so the field flickers like a meadow of fireflies.
//   period_s  full cycle length (one flash + the dark gap after it)
//   scatter   0..1 position stagger (0 => whole field flashes in unison)
inline float fireflyIntensity(int64_t synced_us, float x, float y,
                              float period_s, float scatter) {
  float p = phase(synced_us, period_s) + fireflyStagger(x, y, scatter);
  p -= floorf(p);                             // wrap to [0,1)
  float flash_frac = fireflyFlashFrac(period_s);  // most of the cycle is lit
  if (p >= flash_frac) return 0.0f;
  float u = p / flash_frac;        // 0..1 across the flash
  const float attack = 0.28f;      // quick swell, then a slower fade
  float env;
  if (u < attack) {
    env = smoothstep01(u / attack);
  } else {
    env = 1.0f - smoothstep01((u - attack) / (1.0f - attack));
  }
  // A small, fast ripple riding on the envelope so the peak "glimmers" rather
  // than sitting flat — subtle (12% depth), not a strobe.
  float shimmer = 1.0f - 0.12f * (0.5f - 0.5f * cosf(2.0f * kPi * 6.0f * u));
  float v = env * shimmer;
  if (v < 0.0f) v = 0.0f;
  if (v > 1.0f) v = 1.0f;
  return v;
}

// One traveling sine wavefront sampled at (x,y): sin(2π (t/T - proj/λ)), where
// proj is the position projected onto the travel direction (cx,cy). Returns
// [-1,1]. Double-precision phase accumulation so long runs don't lose precision.
inline float oceanComponent(int64_t synced_us, float x, float y, float cx,
                            float cy, float period_s, float wavelength) {
  double secs = (double)synced_us / 1e6;
  double proj = (double)x * cx + (double)y * cy;
  double ph = secs / (double)period_s - proj / (double)wavelength;
  ph -= floor(ph);  // reduce to [0,1) in double so the float cast keeps full
                    // phase precision even when ph grows large on long runs
  return sinf(2.0f * kPi * (float)ph);
}

// Ocean swell height in [0,1]: a 2-D plane wave traveling across the field at
// `angle_rad`, built from three summed sine components with different
// wavelengths, speeds, and slightly fanned directions. The superposition makes
// the swell organic and slowly non-repeating instead of a single mechanical
// sine (the standard sum-of-sines water technique). A dominant primary swell
// carries the visible traveling crest; a shorter/faster component adds chop and
// a longer/slower one adds a ground-swell roll. 0 = trough, 1 = crest.
//   period_s    time for the primary swell to advance one wavelength
//   wavelength  spatial wavelength of the primary swell (same units as x,y)
//   angle_rad   travel direction across the field
inline float oceanIntensity(int64_t synced_us, float x, float y, float period_s,
                            float wavelength, float angle_rad) {
  // Three components sharing a dominant direction, each fanned within ~±25°
  // (narrow spread => long-crested swell, not choppy sea). Periods follow the
  // deep-water dispersion T ∝ √λ (longer waves travel faster), so the swell is
  // dominant, the short component adds subtle chop, the long one a slow roll —
  // and the incommensurate speeds keep the pattern from ever exactly repeating.
  float c1 = cosf(angle_rad), s1 = sinf(angle_rad);
  float c2 = cosf(angle_rad + 0.40f), s2 = sinf(angle_rad + 0.40f);
  float c3 = cosf(angle_rad - 0.30f), s3 = sinf(angle_rad - 0.30f);
  const float w1 = 0.60f, w2 = 0.22f, w3 = 0.34f;
  float h = w1 * oceanComponent(synced_us, x, y, c1, s1, period_s, wavelength) +
            w2 * oceanComponent(synced_us, x, y, c2, s2, period_s * 0.74f,
                                wavelength * 0.55f) +
            w3 * oceanComponent(synced_us, x, y, c3, s3, period_s * 1.38f,
                                wavelength * 1.90f);
  float n = 0.5f * (h / (w1 + w2 + w3)) + 0.5f;  // -> [0,1]
  if (n < 0.0f) n = 0.0f;
  if (n > 1.0f) n = 1.0f;
  return n;
}

// HSV -> RGB, all components in [0,1]. Standard six-sextant conversion; hue wraps
// so any real hue is valid. Kept pure (no LED type) so it is host-testable; the
// patterns layer scales the result into RGBW pixels.
inline void hsvToRgb(float h, float s, float v, float& r, float& g, float& b) {
  h -= floorf(h);  // wrap hue into [0,1)
  float hf = h * 6.0f;
  int i = (int)hf;  // sextant 0..5
  float f = hf - (float)i;
  float p = v * (1.0f - s);
  float q = v * (1.0f - f * s);
  float t = v * (1.0f - (1.0f - f) * s);
  switch (i % 6) {
    case 0:  r = v; g = t; b = p; break;  // red   -> yellow
    case 1:  r = q; g = v; b = p; break;  // yellow-> green
    case 2:  r = p; g = v; b = t; break;  // green -> cyan
    case 3:  r = p; g = q; b = v; break;  // cyan  -> blue
    case 4:  r = t; g = p; b = v; break;  // blue  -> magenta
    default: r = v; g = p; b = q; break;  // magenta-> red (case 5)
  }
}

struct RgbwUnit {
  float r;
  float g;
  float b;
  float w;
};

// Browser hex is sRGB-encoded, while LED PWM controls emitted light roughly
// linearly. Decode sRGB before generating channel values or intermediate colors
// carry far too much light and look flat/yellow on the physical LEDs.
inline float srgbToLinear(float value) {
  if (value <= 0.0f) return 0.0f;
  if (value >= 1.0f) return 1.0f;
  return value <= 0.04045f
             ? value / 12.92f
             : powf((value + 0.055f) / 1.055f, 2.4f);
}

// Convert an sRGB HSV color into linear RGBW. The SK6812's white die has a warm
// spectrum and a different luminous output from an equal RGB mix, so it cannot
// stand in for the common component of a chromatic browser color without a
// measured fixture calibration. Reserve it for neutral and nearly-neutral
// colors; otherwise preserve the selected hex entirely on the RGB dies. Fade
// the handoff over the first 5% saturation to avoid a discontinuity around gray.
// Intensity is applied after gamma decode so temporal fades remain smooth in
// emitted-light space.
inline RgbwUnit hsvToRgbw(float h, float s, float v, float intensity) {
  if (s < 0.0f) s = 0.0f;
  if (s > 1.0f) s = 1.0f;
  if (v < 0.0f) v = 0.0f;
  if (v > 1.0f) v = 1.0f;
  if (intensity < 0.0f) intensity = 0.0f;
  if (intensity > 1.0f) intensity = 1.0f;

  float sr, sg, sb;
  hsvToRgb(h, s, v, sr, sg, sb);
  float r = srgbToLinear(sr);
  float g = srgbToLinear(sg);
  float b = srgbToLinear(sb);
  constexpr float kNeutralBlendSaturation = 0.05f;
  float white_mix = 1.0f - fminf(s / kNeutralBlendSaturation, 1.0f);
  float w = fminf(r, fminf(g, b)) * white_mix;
  return {(r - w) * intensity, (g - w) * intensity,
          (b - w) * intensity, w * intensity};
}

// Square-wave heartbeat: ON for the first half_period_us of each full cycle, OFF
// for the second. Driven by synced time, so every node that agrees on the clock
// agrees on the blink — two boards blink in unison iff they are in sync.
inline bool heartbeatOn(int64_t synced_us, int64_t half_period_us) {
  return (floorDiv(synced_us, half_period_us) % 2) == 0;
}

inline uint8_t popcount16(uint16_t value) {
  uint8_t count = 0;
  while (value) {
    count += value & 1u;
    value >>= 1;
  }
  return count;
}

inline bool calibrationCodeFarEnough(uint16_t value, const uint16_t* selected,
                                     uint16_t selected_count, uint16_t bit_count,
                                     uint16_t min_hamming_distance) {
  uint16_t mask = bit_count >= 16 ? 0xFFFFu : (uint16_t)((1u << bit_count) - 1u);
  for (uint16_t index = 0; index < selected_count; index++) {
    uint16_t previous = selected[index];
    if (popcount16((uint16_t)((value ^ previous) & mask)) < min_hamming_distance) {
      return false;
    }
  }
  return true;
}

inline uint16_t calibrationCodeValue(uint16_t node_id, uint16_t first_code,
                                     uint16_t bit_count = 16,
                                     uint16_t min_hamming_distance = 1) {
  if (node_id == 0) return 0;
  uint16_t safe_bits = bit_count == 0 ? 1 : (bit_count > 16 ? 16 : bit_count);
  uint16_t safe_distance = min_hamming_distance == 0 ? 1 : min_hamming_distance;
  uint32_t max_value = safe_bits >= 16 ? 65535u : ((1u << safe_bits) - 1u);
  uint16_t code = first_code ? first_code : 1;
  uint16_t selected[256];
  uint16_t found = 0;
  while ((uint32_t)code <= max_value) {
    if (calibrationCodeFarEnough(code, selected, found, safe_bits, safe_distance)) {
      if (found < 256) selected[found] = code;
      found++;
      if (found == node_id) return code;
      if (found >= 256) return 0;
    }
    if (code == 65535u) break;
    code++;
  }
  return 0;
}

inline bool calibrationBitOn(int64_t synced_us, uint16_t node_id,
                             uint16_t slot_ms, uint16_t bit_count,
                             uint16_t first_code,
                             uint16_t min_hamming_distance = 1) {
  if (node_id == 0 || bit_count == 0) return false;
  uint16_t safe_slot_ms = slot_ms ? slot_ms : 1000;
  uint16_t safe_bits = bit_count > 16 ? 16 : bit_count;
  int64_t slot_us = (int64_t)safe_slot_ms * 1000;
  int64_t slot = floorDiv(synced_us, slot_us);
  uint16_t index = (uint16_t)(slot % safe_bits);
  uint16_t code = calibrationCodeValue(
      node_id, first_code ? first_code : 1, safe_bits, min_hamming_distance);
  if (code == 0) return false;
  uint16_t shift = (uint16_t)(safe_bits - 1 - index);
  return ((code >> shift) & 1u) != 0;
}

}  // namespace pmath
