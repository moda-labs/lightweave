// Pattern identifiers + per-pattern facts, dependency-free (no Arduino, no LED
// library) so host-side logic can reason about patterns. patterns.h (the LED
// binding) includes this; the enum values are wire format (BeaconMsg.pattern_id)
// and must never be renumbered.
#pragma once

#include <stdint.h>

namespace patterns {

enum PatternId : uint16_t {
  PULSE = 0,         // uniform slow breathing pulse (all nodes in unison)
  PALETTE_DRIFT = 1, // smooth rainbow hue cycle (optionally traveling by position)
  SWEEP = 2,         // brightness wave that travels across the field by position
  SOLID = 3,         // every pixel full RGBW at `brightness` — the worst-case
                     // power draw, for bench-measuring the per-node ceiling
  GLOW = 4,          // steady solid color at a fixed hue (no time term): the
                     // field holds one calm warm color, flat (non-pulsing) draw
  CALIBRATION = 5,   // identity blink sequence for camera-based positioning
  FIREFLY = 6,       // "hotaru": irregular solo flashes periodically converge
                     // into three synchronized beats, then disperse again
  OCEAN_WAVE = 7,    // soft 2-D ocean swell: summed sine wavefronts travel across
                     // the field, deep blue in the troughs with foam-capped crests
  WHITE = 8,         // steady neutral white using only the SK6812 white channel
  FIRE_FLICKER = 9,  // ring-aware flame: every LED gets its own deterministic
                     // brightness/temperature sample while staying clock-synced
  FIRE2012 = 10,     // deterministic adaptation of FastLED's 1-D heat-cell fire:
                     // cool, diffuse upward, spark at the base, map heat to color
  WAVEFRONT = 11,    // one soft directional band crosses the field, with a dark
                     // interval between passes so its motion reads clearly
  POND_RIPPLE = 12,  // concentric water rings radiating from a live-tunable center
  UPLOADED = 13      // bounded bytecode program distributed and capability-gated
};

// True when f(x,y,t) has no time term: the rendered color never changes until
// the pattern does, so the LEDs (which latch their last color) don't need to be
// re-rendered every frame. Stage B uses this to let the CPU sleep through whole
// radio-off spans on calm scenes instead of waking ~30x/second to redraw the
// same pixels. An unknown/future pattern id must return false (assume animated —
// the safe direction: it only costs power, never a frozen show).
inline bool patternIsStatic(uint16_t pattern_id) {
  return pattern_id == SOLID || pattern_id == GLOW || pattern_id == WHITE;
}

inline bool patternNeedsCurrentFirmware(uint16_t pattern_id) {
  return pattern_id == POND_RIPPLE || pattern_id == UPLOADED;
}

inline uint16_t patternAfterFirmwareMismatch(uint16_t pattern_id) {
  return patternNeedsCurrentFirmware(pattern_id) ? (uint16_t)GLOW : pattern_id;
}

inline bool patternMismatchRequiresFallback(bool positioned,
                                            bool firmware_matches) {
  return positioned && !firmware_matches;
}

inline bool patternBrightnessRequiresReadiness(uint16_t pattern_id,
                                               uint8_t brightness) {
  return brightness > 0 && patternNeedsCurrentFirmware(pattern_id);
}

inline bool patternParamsMayChangeDirectly(uint16_t pattern_id) {
  return pattern_id != UPLOADED;
}

inline uint64_t uploadedPatternProgramId(const uint16_t params[4]) {
  return (uint64_t)params[0] | ((uint64_t)params[1] << 16) |
         ((uint64_t)params[2] << 32) | ((uint64_t)params[3] << 48);
}

inline void uploadedPatternSetProgramId(uint16_t params[4], uint64_t id) {
  params[0] = (uint16_t)(id & 0xFFFFU);
  params[1] = (uint16_t)(id >> 16);
  params[2] = (uint16_t)(id >> 32);
  params[3] = (uint16_t)(id >> 48);
}

// Boot guard: SOLID (full-white worst case) is a live-only bench pattern,
// never a show. A node must not power up rendering it — that would drain the
// battery on all four channels — so a persisted SOLID falls back to a safe
// pattern at boot. `pattern 3` still works live for a deliberate on-bench
// measurement. Every other id (including unknown/future ones) passes through
// untouched — the renderer, not the boot path, decides what they mean.
inline uint16_t patternBootSafe(uint16_t pattern_id) {
  return pattern_id == SOLID ? (uint16_t)SWEEP : pattern_id;
}

}  // namespace patterns
