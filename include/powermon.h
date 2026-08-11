// INA228 power-telemetry logic — Milestone 3 instrumentation (ARCHITECTURE §4.2).
//
// One or two "reference" nodes carry an INA228 breakout (15 mΩ shunt, hardware
// energy/charge accumulators) in series between battery+ and the buck input.
// The device integrates continuously in hardware, so firmware never does
// integration math — it just reads the accumulated totals occasionally and
// unicasts them to the conductor (MSG_POWER), which logs them. That turns any
// overnight sync test into a fleet power audit with no need to visit lanterns.
//
// Like sync.h/powersave.h, this header is deliberately dependency-free (no
// Arduino, no Wire, no Adafruit lib, no globals): the WHEN-to-report schedule,
// the unit conversions, and the is-this-reading-sane gate all run and are
// unit-tested on the host. src/main.cpp owns the actual I2C traffic
// (Adafruit_INA228 readEnergy()/resetAccumulators()) and the ESP-NOW send.
//
// All times are int64 microseconds (esp_timer_get_time on-device).
#pragma once

#include <stdint.h>
#include <math.h>

// One reading of the INA228's accumulated + instantaneous state. Mirrors the
// MSG_POWER wire payload (beacon.h); filled from the Adafruit lib on-device,
// by hand in tests.
struct PowerSample {
  float    energy_j;    // hardware ENERGY accumulator since last reset (Joules)
  float    charge_c;    // hardware CHARGE accumulator (Coulombs) — Ah cross-check
  float    bus_v;       // instantaneous bus (battery-side) voltage
  float    current_ma;  // instantaneous current
  uint32_t elapsed_s;   // node-tracked seconds since the accumulators were reset
};

// ---- Unit conversions ---------------------------------------------------------
// The INA228 accumulates in SI (Joules / Coulombs); humans budget in Wh / mAh.
inline float powerWh(float joules) { return joules / 3600.0f; }
inline float powerMah(float coulombs) { return coulombs / 3.6f; }

// Average draw over the accumulation window — the number that plugs straight
// into the nights-of-battery math. Zero/negative elapsed returns 0 rather than
// dividing by zero (a report can land the same second the accumulator resets).
inline float powerAvgW(float joules, uint32_t elapsed_s) {
  if (elapsed_s == 0) return 0.0f;
  return joules / (float)elapsed_s;
}

// ---- Plausibility gate ----------------------------------------------------------
// A broken sensor / wiring fault / garbled packet should be LOGGED but flagged,
// never trusted into the power budget. Bounds are generous — they only exist to
// catch NaN/inf and orders-of-magnitude nonsense, not to police real readings.
// (384 Wh battery ≈ 1.4 MJ; 30 Ah = 108 kC; 12 V LiFePO4 tops out ~14.6 V.)
static constexpr float POWER_MAX_ENERGY_J  = 5.0e6f;   // ~1.4 kWh — way past any night
static constexpr float POWER_MAX_CHARGE_C  = 5.0e5f;   // ~139 Ah
static constexpr float POWER_MAX_BUS_V     = 30.0f;    // divider/wiring fault above
static constexpr float POWER_MAX_CURRENT_MA = 20000.0f;  // 20 A — far past the buck
static constexpr float POWER_MAX_INSTANT_W = 50.0f;    // node worst case is ~5 W

inline bool powerPlausible(const PowerSample& s) {
  if (!isfinite(s.energy_j) || !isfinite(s.charge_c) || !isfinite(s.bus_v) ||
      !isfinite(s.current_ma))
    return false;
  if (s.energy_j < 0.0f || s.energy_j > POWER_MAX_ENERGY_J) return false;
  // Charge may legitimately run negative if the shunt is wired backwards on the
  // bench — bound its magnitude only.
  if (fabsf(s.charge_c) > POWER_MAX_CHARGE_C) return false;
  if (s.bus_v < 0.0f || s.bus_v > POWER_MAX_BUS_V) return false;
  if (fabsf(s.current_ma) > POWER_MAX_CURRENT_MA) return false;
  // Energy is a lifetime accumulator in the INA228 and can survive an ESP32
  // reboot while elapsed_s restarts. It therefore cannot be plausibility-tested
  // against this boot's uptime. Bound the independently measured instantaneous
  // draw instead; the Pi derives recent average draw from consecutive Wh deltas.
  float instant_w = fabsf(s.bus_v * s.current_ma / 1000.0f);
  if (!isfinite(instant_w) || instant_w > POWER_MAX_INSTANT_W) return false;
  return true;
}

// ---- Report scheduler -----------------------------------------------------------
// Decides WHEN an instrumented performer sends MSG_POWER. Reporting is pure
// convenience — the hardware accumulator keeps counting regardless — so the
// schedule defers freely: it fires only while the radio is up AND the conductor
// unicast peer exists (Stage-A duty-cycling keeps the radio off ~87% of the
// time), catching up at the first sendable moment after the interval elapses.
// next_us = 0 at boot means the first report goes out as soon as a send is
// possible — an immediate link check on the bench.
struct PowerSched {
  int64_t next_us;
};

inline void powerSchedInit(PowerSched& s) { s.next_us = 0; }

// True exactly when a report should be sent now; advances the schedule when it
// fires. `can_send` = radio powered && conductor peer added (caller's facts).
inline bool powerReportDue(PowerSched& s, int64_t now_us, int64_t interval_us,
                           bool can_send) {
  if (!can_send) return false;
  if (now_us < s.next_us) return false;
  s.next_us = now_us + interval_us;
  return true;
}
