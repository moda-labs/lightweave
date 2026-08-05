// Supported local emitter counts for mixed lantern hardware.
//
// Every firmware image allocates the largest strip once, then renders only the
// active prefix selected by the conductor-authoritative inventory row. Keeping
// this validation dependency-free makes table, wire, and serial behavior host
// testable.
#pragma once

#include <stdint.h>

static constexpr uint8_t DEFAULT_LED_COUNT = 16;
static constexpr uint8_t MAX_LED_COUNT = 64;

inline bool ledCountValid(uint8_t count) {
  return count == 16 || count == 32 || count == 64;
}

inline bool ledCountInputValid(int count) {
  return count == 16 || count == 32 || count == 64;
}

inline uint8_t ledCountSafe(uint8_t count) {
  return ledCountValid(count) ? count : DEFAULT_LED_COUNT;
}

inline uint16_t activeLedCount(uint8_t configured, uint16_t capacity) {
  uint16_t count = ledCountSafe(configured);
  return count < capacity ? count : capacity;
}
