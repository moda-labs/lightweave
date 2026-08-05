// Reversible field blackout state. Dependency-free so the save/restore rules
// can be exercised on the host instead of living only in Arduino glue.
#pragma once

#include <stdint.h>

#include "beacon.h"
#include "config.h"

static constexpr uint32_t BLACKOUT_STATE_MAGIC = 0x424C4B31;  // "BLK1"
static constexpr uint8_t BLACKOUT_STATE_VERSION = 1;

struct BlackoutState {
  uint32_t magic;
  uint8_t version;
  uint8_t restore_available;
  uint8_t brightness[GROUP_COUNT];
  uint8_t reserved[2];
};

static_assert(sizeof(BlackoutState) == 16,
              "BlackoutState NVS layout changed; bump its version");

inline void blackoutStateInit(BlackoutState& state) {
  state = {};
  state.magic = BLACKOUT_STATE_MAGIC;
  state.version = BLACKOUT_STATE_VERSION;
}

inline bool blackoutStateValid(const BlackoutState& state) {
  if (state.magic != BLACKOUT_STATE_MAGIC ||
      state.version != BLACKOUT_STATE_VERSION ||
      state.restore_available > 1) {
    return false;
  }
  for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++) {
    if (state.brightness[group_id] > MAX_BRIGHTNESS) return false;
  }
  return true;
}

// Returns true only when a new pre-blackout snapshot was captured. Repeated
// blackout commands keep the first snapshot, so a double-click cannot replace
// recoverable brightness values with zeroes.
inline bool blackoutApply(BlackoutState& state,
                          PatternConfig patterns[GROUP_COUNT]) {
  if (!blackoutStateValid(state)) blackoutStateInit(state);
  bool captured = !state.restore_available;
  if (captured) {
    for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++)
      state.brightness[group_id] = patterns[group_id].brightness;
    state.restore_available = 1;
  }
  for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++)
    patterns[group_id].brightness = 0;
  return captured;
}

inline bool blackoutRestore(BlackoutState& state,
                            PatternConfig patterns[GROUP_COUNT]) {
  if (!blackoutStateValid(state) || !state.restore_available) return false;
  for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++)
    patterns[group_id].brightness = state.brightness[group_id];
  state.restore_available = 0;
  return true;
}
