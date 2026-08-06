// Performer REGISTER scheduling without a synchronized return-traffic herd.
//
// Performers normally share the same receive windows. Merely adding jitter to a
// ten-second interval is insufficient: deadlines that expire while the radio is
// asleep all collapse onto the next common wake. This scheduler therefore picks
// a stable MAC-derived slot *after* a radio window opens, keeps that one window
// awake until the send is queued/completed, and applies bounded backoff after a
// failed ESP-NOW delivery callback.
//
// Dependency-free so timing, bounds, and fleet spreading are host-tested.
#pragma once

#include <stdint.h>

struct RegistrationConfig {
  int64_t interval_us;
  int64_t interval_jitter_us;
  int64_t slot_spread_us;
  int64_t retry_base_us;
  int64_t retry_max_us;
};

struct RegistrationSchedule {
  int64_t next_due_us;
  int64_t slot_us;
  uint8_t failures;
  bool slot_pending;
  bool in_flight;
};

inline uint32_t registrationHash(const uint8_t mac[6], uint32_t salt) {
  uint32_t hash = 2166136261u ^ salt;
  for (uint8_t i = 0; i < 6; i++) {
    hash ^= mac[i];
    hash *= 16777619u;
  }
  hash ^= hash >> 16;
  hash *= 0x7feb352du;
  hash ^= hash >> 15;
  return hash;
}

inline int64_t registrationJitter(const uint8_t mac[6], uint32_t salt,
                                  int64_t span_us) {
  if (span_us <= 0) return 0;
  uint64_t span = (uint64_t)span_us + 1u;
  return (int64_t)((uint64_t)registrationHash(mac, salt) % span);
}

inline void registrationInit(RegistrationSchedule& schedule) {
  schedule.next_due_us = 0;
  schedule.slot_us = 0;
  schedule.failures = 0;
  schedule.slot_pending = false;
  schedule.in_flight = false;
}

// Called only while the performer radio is up. The first due poll chooses a
// per-board slot inside this window; a later poll at/after that slot authorizes
// exactly one send.
inline bool registrationSendDue(RegistrationSchedule& schedule, int64_t now_us,
                                const uint8_t mac[6],
                                const RegistrationConfig& config) {
  if (schedule.in_flight || now_us < schedule.next_due_us) return false;
  if (!schedule.slot_pending) {
    uint32_t salt = 0x51a7u + (uint32_t)schedule.failures * 0x9e3779b9u;
    schedule.slot_us = now_us +
        registrationJitter(mac, salt, config.slot_spread_us);
    schedule.slot_pending = true;
  }
  return now_us >= schedule.slot_us;
}

inline void registrationSendStarted(RegistrationSchedule& schedule) {
  schedule.slot_pending = false;
  schedule.in_flight = true;
}

inline int64_t registrationRetryDelay(const uint8_t mac[6], uint8_t failures,
                                      const RegistrationConfig& config) {
  if (config.retry_base_us <= 0 || config.retry_max_us <= 0) return 0;
  int64_t ceiling = config.retry_base_us;
  uint8_t shifts = failures > 0 ? (uint8_t)(failures - 1) : 0;
  while (shifts-- && ceiling < config.retry_max_us) {
    if (ceiling > config.retry_max_us / 2) {
      ceiling = config.retry_max_us;
      break;
    }
    ceiling *= 2;
  }
  if (ceiling > config.retry_max_us) ceiling = config.retry_max_us;
  int64_t floor = ceiling / 2;
  return floor + registrationJitter(
      mac, 0xbac0ffu + (uint32_t)failures * 0x45d9f3bu,
      ceiling - floor);
}

// `delivered` is the ESP-NOW unicast delivery callback, not merely queue
// acceptance. Success returns to the low-cost periodic cadence. Failure retries
// with a MAC-derived exponential delay capped by retry_max_us.
inline void registrationSendResult(RegistrationSchedule& schedule,
                                   int64_t now_us, const uint8_t mac[6],
                                   const RegistrationConfig& config,
                                   bool delivered) {
  schedule.in_flight = false;
  schedule.slot_pending = false;
  if (delivered) {
    schedule.failures = 0;
    schedule.next_due_us = now_us + config.interval_us +
        registrationJitter(mac, 0x1a7e4a1u, config.interval_jitter_us);
    return;
  }
  if (schedule.failures < UINT8_MAX) schedule.failures++;
  schedule.next_due_us = now_us +
      registrationRetryDelay(mac, schedule.failures, config);
}

inline bool registrationKeepsRadioAwake(
    const RegistrationSchedule& schedule) {
  return schedule.slot_pending || schedule.in_flight;
}
