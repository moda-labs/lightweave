// Lever-2 boot classification: what kind of boot is this, and how should the
// wake/sleep state machines be seeded?
//
// Two kinds of boot exist. A TIMER WAKE is a dusk resample rendezvous: the
// node woke itself from daytime deep sleep to re-check the light and listen
// for a FIELD_AWAKE beacon, and should vanish again in ~10 s if it's still
// bright — so the dusk detector starts in "day" (the RTC-memory flag survived
// the sleep) and the serial grace is pre-expired (nothing is typing at a node
// that woke itself in a field). EVERY OTHER BOOT (power-cycle, flash,
// brownout) is a human: start in "night" (awake), hold dusk-sleep off for the
// long cold-boot window, and seed the serial grace so a fresh flash always has
// a responsive provisioning window. This is the power-cycle-always-wakes
// guarantee (fail-awake, see dusk.h).
//
// The serial seed is pre-expired past the LONGEST grace that reads it — both
// the dusk serial gate (DUSK_SERIAL_GRACE_US) and the Stage-B nap gate
// (SERIAL_NAP_GRACE_US) compare against the same last-serial timestamp, so
// using max() here removes the old unlabeled invariant that the dusk grace
// happened to be the longer one.
//
// Dependency-free and host-tested; setup() applies the returned plan.
#pragma once

#include <stdint.h>

struct BootPlanConfig {
  int64_t min_awake_timer_us;    // timer wake: resample + beacon-listen window
  int64_t min_awake_cold_us;     // human boot: long hold-off before any dusk sleep
  int64_t dusk_serial_grace_us;  // serial gate in duskShouldSleep
  int64_t nap_serial_grace_us;   // serial gate in napPlan (Stage B)
};

struct BootPlan {
  bool    dusk_start_day;    // seed the dusk detector in "day" (quick re-sleep)
  int64_t dusk_earliest_us;  // no dusk deep-sleep before this
  int64_t serial_seed_us;    // seed for the last-serial-activity timestamp
  bool    rtc_day_flag;      // value to write back to the RTC-memory day flag
};

inline BootPlan bootClassify(bool timer_wake, bool rtc_was_day, int64_t boot_us,
                             const BootPlanConfig& cfg) {
  BootPlan p;
  p.dusk_start_day = timer_wake && rtc_was_day;
  p.dusk_earliest_us =
      boot_us + (timer_wake ? cfg.min_awake_timer_us : cfg.min_awake_cold_us);
  int64_t longest_grace = cfg.dusk_serial_grace_us > cfg.nap_serial_grace_us
                              ? cfg.dusk_serial_grace_us
                              : cfg.nap_serial_grace_us;
  p.serial_seed_us = timer_wake ? boot_us - longest_grace - 1 : boot_us;
  // A human boot clears the flag: only a completed dusk deep-sleep may claim
  // "it was day when I slept".
  p.rtc_day_flag = timer_wake ? rtc_was_day : false;
  return p;
}

// A timer wake is the field's only chance to receive a changed schedule or wake
// override. Keep the node awake through a bounded rendezvous until either a
// fresh beacon arrives or the timer-wake holdoff expires. Cold boots already
// fail awake through their separate boot plan and do not need this gate.
inline bool bootWakeRendezvousActive(bool timer_wake, uint32_t beacons_rx,
                                     int64_t now_us,
                                     int64_t earliest_sleep_us) {
  return timer_wake && beacons_rx == 0 && now_us < earliest_sleep_us;
}
