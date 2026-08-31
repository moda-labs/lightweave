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
static constexpr uint16_t kFireflyChorusMarker = 0x8000u;
static constexpr uint16_t kOceanWavelengthMask = 0x03FFu;
static constexpr uint16_t kOceanAngleMask = 0x01FFu;
static constexpr float kOceanTroughValue = 0.22f;

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

// Firefly p3 historically held only saturation (0..100). Bit 15 now marks a
// backward-compatible packing that keeps saturation in the low seven bits and
// stores the chorus recurrence in seconds in bits 7..14.
inline uint16_t fireflyChorusPack(uint8_t saturation_pct,
                                  uint8_t interval_s) {
  if (saturation_pct > 100) saturation_pct = 100;
  return (uint16_t)(kFireflyChorusMarker |
                    ((uint16_t)interval_s << 7) | saturation_pct);
}

inline bool fireflyChorusPresent(uint16_t packed) {
  return (packed & kFireflyChorusMarker) != 0;
}

inline uint8_t fireflySaturationDecode(uint16_t packed, bool full_hsv) {
  if (fireflyChorusPresent(packed)) {
    uint8_t saturation = (uint8_t)(packed & 0x7fu);
    return saturation > 100 ? 100 : saturation;
  }
  if (full_hsv) return (uint8_t)(packed > 100 ? 100 : packed);
  if (!packed) return 85;
  return (uint8_t)(packed > 100 ? 100 : packed);
}

inline uint8_t fireflyChorusIntervalDecode(uint16_t packed) {
  return fireflyChorusPresent(packed) ? (uint8_t)((packed >> 7) & 0xffu)
                                      : 36;
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

// A single soft band enters from one side, crosses the normalized field, and
// exits the opposite side before repeating. Unlike SWEEP's endless sine train,
// WAVEFRONT has one legible crest with darkness ahead of and behind it. Angle
// zero travels left -> right; image-space y grows downward, so 90 degrees
// travels top -> bottom and 270 degrees travels bottom -> top.
inline float wavefrontIntensity(int64_t synced_us, float x, float y,
                                float period_s, float width,
                                float angle_rad) {
  if (period_s <= 0.0f) period_s = 6.0f;
  if (width < 0.04f) width = 0.04f;
  if (width > 1.0f) width = 1.0f;
  float cx = cosf(angle_rad);
  float cy = sinf(angle_rad);
  float min_projection = fminf(0.0f, cx) + fminf(0.0f, cy);
  float max_projection = fmaxf(0.0f, cx) + fmaxf(0.0f, cy);
  float span = max_projection - min_projection;
  float projection = span > 1e-5f
                         ? (x * cx + y * cy - min_projection) / span
                         : 0.5f;
  if (projection < 0.0f) projection = 0.0f;
  if (projection > 1.0f) projection = 1.0f;
  float center = -width + phase(synced_us, period_s) * (1.0f + 2.0f * width);
  float distance = fabsf(projection - center);
  if (distance >= width) return 0.0f;
  float edge = 1.0f - distance / width;
  return edge * edge * (3.0f - 2.0f * edge);
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

inline float fireflyRegularIntensity(int64_t synced_us, float x, float y,
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

inline uint32_t fireflyMix(uint32_t value) {
  value ^= value >> 16;
  value *= 0x7feb352du;
  value ^= value >> 15;
  value *= 0x846ca68bu;
  value ^= value >> 16;
  return value;
}

inline uint32_t fireflyNodeSeed(float x, float y) {
  int32_t xq = (int32_t)lroundf(x * 10000.0f);
  int32_t yq = (int32_t)lroundf(y * 10000.0f);
  return fireflyMix((uint32_t)xq * 0x9e3779b1u ^
                    (uint32_t)yq * 0x85ebca6bu ^ 0xf1ef17u);
}

inline float fireflyRandom01(uint32_t seed, int64_t epoch, uint32_t stream) {
  uint64_t raw_epoch = (uint64_t)epoch;
  uint32_t value = seed ^ (uint32_t)raw_epoch * 0x9e3779b1u;
  value ^= (uint32_t)(raw_epoch >> 32) * 0x85ebca6bu;
  value ^= stream * 0xc2b2ae35u;
  return (fireflyMix(value) >> 8) / 16777216.0f;
}

// One irregular, deterministic solo flash per time window. About one window in
// six is skipped, and start time, duration, amplitude, and shimmer vary by node
// and epoch. It reads as random but remains a pure f(x,y,t), so preview and
// performers agree through reboots and dropped beacons.
inline float fireflyRandomSoloIntensity(int64_t synced_us, float x, float y,
                                        float period_s) {
  if (period_s <= 0.0f) period_s = 7.0f;
  int64_t window_us = (int64_t)lroundf(period_s * 1000000.0f);
  if (window_us < 1) window_us = 1;
  int64_t epoch = floorDiv(synced_us, window_us);
  uint32_t seed = fireflyNodeSeed(x, y);
  if (fireflyRandom01(seed, epoch, 0) < 0.17f) return 0.0f;
  double window_start = (double)epoch * (double)window_us;
  double start = window_start + period_s * 1e6 *
                                    (0.06 + 0.60 * fireflyRandom01(seed, epoch, 1));
  double duration = period_s * 1e6 *
                    (0.18 + 0.12 * fireflyRandom01(seed, epoch, 2));
  float u = (float)(((double)synced_us - start) / duration);
  if (u < 0.0f || u >= 1.0f) return 0.0f;
  const float attack = 0.19f;
  float env = u < attack
                  ? smoothstep01(u / attack)
                  : 1.0f - smoothstep01((u - attack) / (1.0f - attack));
  float amplitude = 0.72f + 0.28f * fireflyRandom01(seed, epoch, 3);
  float shimmer_phase = fireflyRandom01(seed, epoch, 4);
  float shimmer = 0.94f + 0.06f *
                              cosf(2.0f * kPi * (5.0f * u + shimmer_phase));
  float value = amplitude * env * shimmer;
  if (value < 0.0f) value = 0.0f;
  if (value > 1.0f) value = 1.0f;
  return value;
}

inline float fireflyChorusBeat(float beat_phase) {
  beat_phase -= floorf(beat_phase);
  if (beat_phase >= 0.72f) return 0.0f;
  float u = beat_phase / 0.72f;
  const float attack = 0.18f;
  return u < attack
             ? smoothstep01(u / attack)
             : 1.0f - smoothstep01((u - attack) / (1.0f - attack));
}

// Firefly chorus: irregular independent flashes dominate most of the time.
// Near the end of each recurrence window, every node crossfades into exactly
// three shared 1.35-second beats, then returns smoothly to its own schedule.
// `scatter` blends from the legacy regular/unison cycle (0) to fully irregular
// solos (1), while the chorus remains field-synchronous at any setting.
inline float fireflyIntensity(int64_t synced_us, float x, float y,
                              float period_s, float scatter,
                              float chorus_interval_s = 36.0f) {
  if (period_s <= 0.0f) period_s = 7.0f;
  if (scatter < 0.0f) scatter = 0.0f;
  if (scatter > 1.0f) scatter = 1.0f;
  float regular = fireflyRegularIntensity(synced_us, x, y, period_s, 0.0f);
  float random = fireflyRandomSoloIntensity(synced_us, x, y, period_s);
  float solo = regular * (1.0f - scatter) + random * scatter;
  if (chorus_interval_s < 8.0f) return solo;

  const float beat_period_s = 1.35f;
  const float chorus_beats = 3.0f;
  const float chorus_duration_s = beat_period_s * chorus_beats;
  if (chorus_interval_s < chorus_duration_s + 2.0f)
    chorus_interval_s = chorus_duration_s + 2.0f;
  float cycle_s = phase(synced_us, chorus_interval_s) * chorus_interval_s;
  float chorus_start_s = chorus_interval_s - chorus_duration_s;
  if (cycle_s < chorus_start_s) return solo;
  float local_s = cycle_s - chorus_start_s;
  float synced = fireflyChorusBeat(local_s / beat_period_s);
  const float crossfade_s = 0.38f;
  float blend_in = smoothstep01(local_s / crossfade_s);
  float blend_out = 1.0f - smoothstep01(
      (local_s - (chorus_duration_s - crossfade_s)) / crossfade_s);
  float blend = blend_in * blend_out;
  return solo * (1.0f - blend) + synced * blend;
}

struct FireFlickerSample {
  float intensity;  // emitted-light level in [0,1]
  float heat;       // local color-temperature offset in [-1,1]
};

// Deterministic, ring-aware flame texture. Each pixel combines two slow
// whole-lantern billows with three angular waves around the ring. Adjacent LEDs
// therefore move coherently like tongues of flame instead of producing white
// noise, while the incommensurate periods keep the motion from looking like a
// simple chase. Position seeds each lantern differently without storing random
// state, so performers still free-run through dropped beacons and converge on
// the same result for the same (x,y,pixel,t).
//
//   period_s    primary flicker timescale (about 1.2 s by default)
//   texture     0..1 depth of per-pixel variation; global flicker remains at 0
inline FireFlickerSample fireFlickerSample(int64_t synced_us, float x, float y,
                                           uint16_t pixel_index,
                                           uint16_t pixel_count,
                                           float period_s, float texture) {
  if (period_s <= 0.0f) period_s = 1.2f;
  if (texture < 0.0f) texture = 0.0f;
  if (texture > 1.0f) texture = 1.0f;
  uint16_t count = pixel_count ? pixel_count : 1;
  float around = (pixel_index % count) / (float)count;
  float node = fireflyStagger(x, y, 1.0f);

  float billow_a = sinf(2.0f * kPi * (phase(synced_us, period_s * 1.47f) + node));
  float billow_b = sinf(2.0f * kPi * (phase(synced_us, period_s * 0.73f) - node * 0.61f));
  float global = 0.63f + 0.13f * billow_a + 0.09f * billow_b;

  float tongue_a = sinf(2.0f * kPi *
                         (phase(synced_us, period_s * 0.83f) +
                          2.0f * around + node * 0.37f));
  float tongue_b = sinf(2.0f * kPi *
                         (phase(synced_us, period_s * 0.41f) -
                          3.0f * around + node * 0.79f));
  float tongue_c = sinf(2.0f * kPi *
                         (phase(synced_us, period_s * 0.19f) +
                          5.0f * around - node * 0.53f));
  float local = 0.20f * tongue_a + 0.12f * tongue_b + 0.08f * tongue_c;

  float intensity = global + texture * local;
  if (intensity < 0.08f) intensity = 0.08f;  // retain a low ember bed
  if (intensity > 1.0f) intensity = 1.0f;
  float heat = texture * local / 0.40f;
  if (heat < -1.0f) heat = -1.0f;
  if (heat > 1.0f) heat = 1.0f;
  return {intensity, heat};
}

// Deterministic adaptation of Mark Kriegsman's FastLED Fire2012 heat-cell
// simulation. The canonical effect cools every cell, diffuses heat upward,
// injects random sparks near the base, then maps temperature through a
// black-body ramp. Ambient RNG would make previews, reboots, and clock recovery
// diverge, so each random draw here is hashed from the lantern seed and absolute
// simulation step. Three compact 64-byte heat arrays hold the blended output
// plus the overlapping simulations used at checkpoint boundaries.
//
// Reference algorithm:
// https://github.com/FastLED/FastLED/blob/master/examples/Fire2012/Fire2012.ino
static constexpr uint16_t kFire2012MaxCells = 64;

struct Fire2012State {
  uint8_t heat[kFire2012MaxCells];
  uint8_t primary_heat[kFire2012MaxCells];
  uint8_t next_heat[kFire2012MaxCells];
  int64_t last_step;
  int64_t next_last_step;
  int64_t origin_step;
  uint32_t signature;
  bool ready;
};

struct Fire2012Color {
  uint8_t r;
  uint8_t g;
  uint8_t b;
};

inline uint32_t fire2012Mix(uint32_t value) {
  value ^= value >> 16;
  value *= 0x7feb352du;
  value ^= value >> 15;
  value *= 0x846ca68bu;
  value ^= value >> 16;
  return value;
}

// The per-cell random stream is the hottest path in large control-plane
// previews. One strong multiplicative round is sufficient here because the
// inputs have already been separated by seed, step, and stream constants.
// Keep this dependency-free so firmware and preview use identical bytes.
inline uint32_t fire2012RandomMix(uint32_t value) {
  value ^= value >> 16;
  value *= 0x7feb352du;
  value ^= value >> 15;
  return value;
}

inline uint32_t fire2012NodeSeed(float x, float y, uint16_t node_id) {
  int32_t xq = (int32_t)lroundf(x * 10000.0f);
  int32_t yq = (int32_t)lroundf(y * 10000.0f);
  uint32_t seed = (uint32_t)xq * 0x9e3779b1u;
  seed ^= (uint32_t)yq * 0x85ebca6bu;
  seed ^= (uint32_t)node_id * 0xc2b2ae35u;
  return fire2012Mix(seed ^ 0x20120718u);
}

inline uint8_t fire2012Random8(uint32_t seed, int64_t step,
                               uint32_t stream) {
  uint64_t raw_step = (uint64_t)step;
  uint32_t value = seed ^ (uint32_t)raw_step * 0x9e3779b1u;
  value ^= (uint32_t)(raw_step >> 32) * 0x85ebca6bu;
  value ^= stream * 0xc2b2ae35u;
  return (uint8_t)(fire2012RandomMix(value) >> 24);
}

inline void fire2012Clear(Fire2012State& state) {
  for (uint16_t i = 0; i < kFire2012MaxCells; i++) {
    state.heat[i] = 0;
    state.primary_heat[i] = 0;
    state.next_heat[i] = 0;
  }
  state.last_step = 0;
  state.next_last_step = 0;
  state.origin_step = 0;
  state.signature = 0;
  state.ready = false;
}

inline void fire2012Step(uint8_t heat[kFire2012MaxCells], int64_t step,
                         uint32_t seed, uint16_t cell_count, uint8_t cooling,
                         uint8_t sparking) {
  if (cell_count < 1) cell_count = 1;
  if (cell_count > kFire2012MaxCells) cell_count = kFire2012MaxCells;

  // Fire2012 step 1: every cell loses a small, independently varying amount
  // of heat. random8(min,max) is upper-exclusive in the reference algorithm.
  uint16_t cooling_limit = ((uint16_t)cooling * 10u) / cell_count + 2u;
  if (cooling_limit > 256u) cooling_limit = 256u;
  for (uint16_t i = 0; i < cell_count; i++) {
    uint8_t loss = (uint8_t)(fire2012Random8(seed, step, i) % cooling_limit);
    heat[i] = loss >= heat[i] ? 0 : heat[i] - loss;
  }

  // Step 2: heat rises and diffuses, weighting the cell two places below twice.
  for (int k = (int)cell_count - 1; k >= 2; k--) {
    heat[k] = (uint8_t)(((uint16_t)heat[k - 1] +
                          (uint16_t)heat[k - 2] * 2u) /
                         3u);
  }

  // Step 3: sometimes ignite a strong spark in one of the bottom seven cells.
  if (fire2012Random8(seed, step, 0x100u) < sparking) {
    uint16_t base_cells = cell_count < 7 ? cell_count : 7;
    uint16_t y = fire2012Random8(seed, step, 0x101u) % base_cells;
    uint16_t added = 160u + fire2012Random8(seed, step, 0x102u) % 95u;
    uint16_t hot = (uint16_t)heat[y] + added;
    heat[y] = (uint8_t)(hot > 255u ? 255u : hot);
  }
}

inline uint32_t fire2012Signature(uint32_t seed, uint16_t cell_count,
                                  uint8_t fps, uint8_t cooling,
                                  uint8_t sparking) {
  uint32_t config = (uint32_t)cell_count | ((uint32_t)fps << 8) |
                    ((uint32_t)cooling << 16) | ((uint32_t)sparking << 24);
  return fire2012Mix(seed ^ config);
}

// Advance the heat simulation to the fixed absolute-time step containing
// synced_us. Four-second deterministic blocks bound cold-preview replay work.
// The primary simulation starts one block early as warm-up; a second simulation
// starts at the current block and crossfades in over its final half-second.
// At the boundary that second simulation becomes primary, keeping the flame
// continuous instead of resetting it at each checkpoint.
inline void fire2012Prepare(Fire2012State& state, int64_t synced_us,
                            uint32_t seed, uint16_t cell_count, uint8_t fps,
                            uint8_t cooling, uint8_t sparking) {
  if (cell_count < 1) cell_count = 1;
  if (cell_count > kFire2012MaxCells) cell_count = kFire2012MaxCells;
  if (fps < 10 || fps > 60) fps = 30;
  uint32_t signature = fire2012Signature(seed, cell_count, fps, cooling, sparking);
  int64_t target_step = floorDiv(synced_us * (int64_t)fps, 1000000LL);
  int64_t block_steps = (int64_t)fps * 4;
  int64_t origin_step = floorDiv(target_step, block_steps) * block_steps -
                        block_steps;
  bool same_config = state.ready && state.signature == signature;
  bool advance_one_block = same_config &&
                           origin_step == state.origin_step + block_steps &&
                           target_step >= state.next_last_step;
  bool rebuild = !same_config ||
                 (state.origin_step != origin_step && !advance_one_block) ||
                 target_step < state.last_step;
  if (rebuild) {
    fire2012Clear(state);
    state.signature = signature;
    state.origin_step = origin_step;
    state.last_step = origin_step;
    state.next_last_step = origin_step + block_steps;
    state.ready = true;
  } else if (advance_one_block) {
    for (uint16_t i = 0; i < cell_count; i++) {
      state.primary_heat[i] = state.next_heat[i];
      state.next_heat[i] = 0;
    }
    state.origin_step = origin_step;
    state.last_step = state.next_last_step;
    state.next_last_step = origin_step + block_steps;
  }
  while (state.last_step < target_step) {
    state.last_step++;
    fire2012Step(state.primary_heat, state.last_step, seed, cell_count,
                 cooling, sparking);
  }
  while (state.next_last_step < target_step) {
    state.next_last_step++;
    fire2012Step(state.next_heat, state.next_last_step, seed, cell_count,
                 cooling, sparking);
  }

  int64_t block_start = origin_step + block_steps;
  int64_t crossfade_steps = (int64_t)fps / 2;
  if (crossfade_steps < 1) crossfade_steps = 1;
  int64_t crossfade_start = block_start + block_steps - crossfade_steps;
  float blend = 0.0f;
  if (target_step > crossfade_start) {
    blend = (float)(target_step - crossfade_start) / (float)crossfade_steps;
    if (blend > 1.0f) blend = 1.0f;
    blend = smoothstep01(blend);
  }
  for (uint16_t i = 0; i < cell_count; i++) {
    state.heat[i] = (uint8_t)lroundf(
        state.primary_heat[i] * (1.0f - blend) + state.next_heat[i] * blend);
  }
}

// FastLED's HeatColor black-body approximation: black -> red -> yellow -> white.
inline Fire2012Color fire2012HeatColor(uint8_t temperature) {
  uint8_t t192 = (uint8_t)(((uint16_t)temperature * 191u + 255u) >> 8);
  uint8_t heat_ramp = (uint8_t)((t192 & 0x3fu) << 2);
  if (t192 & 0x80u) return {255, 255, heat_ramp};
  if (t192 & 0x40u) return {255, heat_ramp, 0};
  return {heat_ramp, 0, 0};
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

// Perceptual sRGB value for an ocean swell. A 14% floor could quantize to zero
// after sRGB-to-linear conversion at normal dim show brightnesses. Keep a 22%
// deep-water floor while retaining the full crest and most of its contrast.
inline float oceanSwellValue(float intensity, float base_value = 1.0f) {
  if (intensity < 0.0f) intensity = 0.0f;
  if (intensity > 1.0f) intensity = 1.0f;
  if (base_value < 0.0f) base_value = 0.0f;
  if (base_value > 1.0f) base_value = 1.0f;
  return base_value *
         (kOceanTroughValue + (1.0f - kOceanTroughValue) *
                                  powf(intensity, 1.3f));
}

// Concentric pond ripple sampled at (x,y). A crest begins at the selected
// center, expands radially, and repeats every period. Keeping this pure and
// position/time-derived preserves synchronization and free-run through dropped
// beacons.
//   period_s    time between successive rings emitted from the center
//   wavelength  distance between successive crests in normalized field units
//   center_x/y  ripple origin in normalized field coordinates
inline float pondRippleIntensity(int64_t synced_us, float x, float y,
                                 float period_s, float wavelength,
                                 float center_x, float center_y) {
  if (period_s <= 0.0f) period_s = 6.0f;
  if (wavelength <= 0.0f) wavelength = 0.50f;
  double secs = (double)synced_us / 1e6;
  double dx = (double)x - center_x;
  double dy = (double)y - center_y;
  double radius = sqrt(dx * dx + dy * dy);
  double ph = secs / (double)period_s - radius / (double)wavelength;
  ph -= floor(ph);
  // Crest at phase zero. Cubing the raised cosine produces a distinct narrow
  // ring with a soft edge instead of lighting most of the field at once.
  float broad = 0.5f * (1.0f + cosf(2.0f * kPi * (float)ph));
  float crest = broad * broad * broad;
  if (crest < 0.0f) crest = 0.0f;
  if (crest > 1.0f) crest = 1.0f;
  return crest;
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

// Preserve the selected ocean tint at the smallest representable PWM value.
// Exact black (brightness or color value zero) remains black. Other patterns,
// especially Pulse, retain their intentional fully-off phases.
inline RgbwUnit oceanEnsureVisible(RgbwUnit color, uint8_t brightness) {
  if (brightness == 0) return color;
  float channels[4] = {color.r, color.g, color.b, color.w};
  uint8_t strongest = 0;
  for (uint8_t i = 1; i < 4; i++)
    if (channels[i] > channels[strongest]) strongest = i;
  if (channels[strongest] <= 0.0f ||
      lroundf(channels[strongest] * brightness) > 0) {
    return color;
  }
  channels[strongest] = (0.5f + 1e-4f) / brightness;
  return {channels[0], channels[1], channels[2], channels[3]};
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
