#pragma once

#include <stdint.h>

static constexpr uint8_t POWER_FLAG_SCHEDULE_ENABLED = 0x01;
static constexpr uint8_t POWER_FLAG_FORCE_AWAKE      = 0x02;
static constexpr uint8_t POWER_FLAG_FORCE_SLEEP      = 0x04;

static constexpr uint16_t POWER_LIGHT_CHECK_MIN_S = 1;
static constexpr uint16_t POWER_LIGHT_CHECK_MAX_S = 300;
static constexpr uint16_t POWER_DEEP_CHECK_MIN_MIN = 1;
static constexpr uint16_t POWER_DEEP_CHECK_MAX_MIN = 1440;
static constexpr uint16_t POWER_DAY_MINUTES = 1440;
static constexpr uint32_t POWER_SECONDS_PER_MINUTE = 60;

typedef struct __attribute__((packed)) {
  uint16_t light_sleep_check_s;  // radio/light-sleep update check interval
  uint16_t deep_sleep_check_min; // RTC wake interval while outside LED schedule
  uint16_t led_on_start_min;     // minutes after local midnight, inclusive
  uint16_t led_on_end_min;       // minutes after local midnight, exclusive
  uint16_t current_min;          // conductor's current local minute of day
  uint32_t current_epoch_s;      // conductor's current UTC epoch seconds
  uint8_t  flags;                // POWER_FLAG_* bits
} PowerPolicy;

inline uint16_t powerClampU16(uint16_t v, uint16_t lo, uint16_t hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

inline void powerPolicySanitize(PowerPolicy& p) {
  p.light_sleep_check_s =
      powerClampU16(p.light_sleep_check_s, POWER_LIGHT_CHECK_MIN_S,
                    POWER_LIGHT_CHECK_MAX_S);
  p.deep_sleep_check_min =
      powerClampU16(p.deep_sleep_check_min, POWER_DEEP_CHECK_MIN_MIN,
                    POWER_DEEP_CHECK_MAX_MIN);
  p.led_on_start_min %= POWER_DAY_MINUTES;
  p.led_on_end_min %= POWER_DAY_MINUTES;
  p.current_min %= POWER_DAY_MINUTES;
  p.flags &= (POWER_FLAG_SCHEDULE_ENABLED | POWER_FLAG_FORCE_AWAKE |
              POWER_FLAG_FORCE_SLEEP);
}

inline PowerPolicy powerPolicyDefault() {
  PowerPolicy p = {4, 15, 20 * 60, 6 * 60, 12 * 60, 0, 0};
  return p;
}

inline bool powerPolicyScheduleEnabled(const PowerPolicy& p) {
  return (p.flags & POWER_FLAG_SCHEDULE_ENABLED) != 0;
}

inline bool powerPolicyForceAwake(const PowerPolicy& p) {
  return (p.flags & POWER_FLAG_FORCE_AWAKE) != 0;
}

inline bool powerPolicyForceSleep(const PowerPolicy& p) {
  return (p.flags & POWER_FLAG_FORCE_SLEEP) != 0;
}

inline bool powerPolicyInLedWindow(uint16_t minute, uint16_t start,
                                   uint16_t end) {
  minute %= POWER_DAY_MINUTES;
  start %= POWER_DAY_MINUTES;
  end %= POWER_DAY_MINUTES;
  if (start == end) return true;  // all day
  if (start < end) return minute >= start && minute < end;
  return minute >= start || minute < end;  // wraps across midnight
}

inline bool powerPolicyLedsOn(const PowerPolicy& p) {
  if (powerPolicyForceAwake(p)) return true;
  if (powerPolicyForceSleep(p)) return false;
  if (!powerPolicyScheduleEnabled(p)) return true;
  return powerPolicyInLedWindow(p.current_min, p.led_on_start_min,
                                p.led_on_end_min);
}

inline bool powerPolicyShouldDeepSleep(const PowerPolicy& p) {
  return !powerPolicyLedsOn(p);
}

inline void powerPolicyAdvanceBySeconds(PowerPolicy& p, uint32_t elapsed_s) {
  if (p.current_epoch_s != 0) p.current_epoch_s += elapsed_s;
  p.current_min =
      (uint16_t)((p.current_min + elapsed_s / POWER_SECONDS_PER_MINUTE) %
                 POWER_DAY_MINUTES);
}

inline uint32_t powerPolicyAlignedSleepSeconds(const PowerPolicy& p) {
  uint32_t interval_s = (uint32_t)p.deep_sleep_check_min * POWER_SECONDS_PER_MINUTE;
  if (interval_s == 0) interval_s = POWER_SECONDS_PER_MINUTE;
  if (p.current_epoch_s == 0) return interval_s;
  uint32_t rem = p.current_epoch_s % interval_s;
  if (rem == 0) return interval_s;
  return interval_s - rem;
}
