// Compile-time configuration shared by all nodes.
//
// Hardware pins and radio settings live here so the rest of the firmware never
// hard-codes a GPIO number. See docs/do_baskets_firmware_brief.md for the wiring
// table and the don't-break list.
#pragma once

#include <stdint.h>

#include "led_profile.h"

// ---- Role --------------------------------------------------------------------
// Every node runs identical firmware; role is a runtime value stored in NVS and
// set once over serial (`role conductor|performer`). Default is performer so a
// fresh board never accidentally becomes a second conductor. See src/main.cpp.
static constexpr uint8_t ROLE_PERFORMER = 0;
static constexpr uint8_t ROLE_CONDUCTOR = 1;
static constexpr uint8_t DEFAULT_ROLE   = ROLE_PERFORMER;

// ---- LEDs --------------------------------------------------------------------
// Up to 64 SK6812 RGBW emitters on one data line (through a 470R series
// resistor). Each lantern's active count is 16, 32, or 64 from its cached
// conductor inventory profile; the driver allocates the maximum so one firmware
// image works on every variant.
// GPIO13 ("D13") sits next to VIN/GND on the DOIT DevKit V1 top header, so on a
// single breadboard the power, ground, and data pins are all reachable from one
// free row. (Any non-strapping output GPIO works; the brief's original GPIO18 is
// on the bottom header, which is awkward to reach when the board covers a row.)
static constexpr uint8_t  LED_PIN   = 13;

// Power guardrail. Every node clamps the rendered brightness to this, so no
// pattern or broadcast config can exceed the per-node power budget no matter what
// gets authored. Set to 192 (75%) after the worst-case bench measurement: solid
// white at 255 measured 0.76 A @ 5 V (~3 nights sustained), which is safe for the
// hardware but a battery risk if a pathological all-white show ran all night; 192
// keeps the worst case ~3.8 nights while barely dimming real colored shows. Raise
// toward 255 only if a brighter ceiling is wanted and battery life allows.
static constexpr uint8_t MAX_BRIGHTNESS = 192;

// ---- Onboard heartbeat LED (bring-up aid) ------------------------------------
// Blinks the board's built-in LED on the synced beat so two bare boards can be
// seen blinking in unison — a visual sync proof with no ring wiring. Drop it
// later by building with -D HEARTBEAT_LED=0.
#ifndef HEARTBEAT_LED
#define HEARTBEAT_LED 1
#endif
static constexpr uint8_t HEARTBEAT_LED_PIN = 2;       // built-in LED on most DevKitCs
static constexpr bool    HEARTBEAT_ACTIVE_LOW = false;  // set true if your LED is inverted
static constexpr int64_t HEARTBEAT_HALF_US = 500000;  // 500ms on / 500ms off => 1 Hz

// ---- Sensors (Milestone 3 — declared now so pins are never reused) -----------
// ADC1 ONLY. ADC2 silently dies whenever the radio is active.
static constexpr uint8_t PIN_LDR      = 34;  // dusk sensor, ADC1
static constexpr uint8_t PIN_VBAT     = 35;  // battery sense divider, ADC1

// ---- Radio -------------------------------------------------------------------
// Every node sets this channel explicitly (esp_wifi_set_channel) so they agree
// without scanning. All nodes MUST use the same channel.
static constexpr uint8_t WIFI_CHANNEL = 1;

// CPU at 160MHz is plenty for 64 pixels and saves ~15-30mA (brief: power discipline).
static constexpr uint32_t CPU_FREQ_MHZ = 160;

// ---- Sync protocol -----------------------------------------------------------
// Identifies our packets so stray ESP-NOW traffic is ignored.
static constexpr uint32_t BEACON_MAGIC = 0xD0BA5757;  // "DOBA" + baskets

// Conductor broadcasts at this interval (microseconds). 250ms => 4 Hz.
static constexpr int64_t BEACON_INTERVAL_US = 250000;

// A performer that hasn't heard a beacon for this long is considered "stale" for
// diagnostics — it keeps free-running regardless (no blanking on missed beacons).
static constexpr int64_t BEACON_STALE_US = 2000000;  // 2s

// ---- Registration / roster ---------------------------------------------------
// A performer re-announces itself (REGISTER) to the conductor this often, so the
// conductor's roster self-heals after a conductor restart. Cheap: one tiny packet
// per node every interval, far below the beacon rate. (The roster itself and its
// capacity ROSTER_MAX live in the dependency-free, host-tested include/roster.h.)
static constexpr int64_t REGISTER_INTERVAL_US = 10000000;  // 10s
// A successful board checks in every 10–12 s. When multiple expired deadlines
// collapse onto one shared radio wake, MAC-derived slots spread them across the
// next 500 ms. Failed unicast delivery retries with 100 ms–2 s bounded backoff.
static constexpr int64_t REGISTER_INTERVAL_JITTER_US = 2000000;  // 0–2s
static constexpr int64_t REGISTER_SLOT_SPREAD_US = 500000;       // 0–500ms
static constexpr int64_t REGISTER_RETRY_BASE_US = 200000;        // first <=200ms
static constexpr int64_t REGISTER_RETRY_MAX_US = 2000000;        // cap 2s

// The conductor re-broadcasts its inventory this often. IDs and positions are
// cached in node NVS, so this is a slow backstop. REGISTER drives immediate ID
// reconciliation and a targeted row reply while the sender's radio is on.
static constexpr int64_t TABLE_INTERVAL_US = 60000000;  // 60s steady-state backstop

// While the live calibration locator is active, the conductor rebroadcasts the
// sorted MAC roster frequently so duty-cycled, freshly flashed nodes can learn
// their dense calibration rank without USB serial provisioning.
static constexpr int64_t CAL_ROSTER_INTERVAL_US = 1000000;  // 1s

// ---- Performer radio duty-cycle (Milestone 3, Lever 1, Stage A) --------------
// A performer free-runs f(x,y,t) from the synced clock, so it does not need the
// radio on continuously — only periodic beacons for drift correction and pattern/
// table updates. So power the radio down between brief listen windows: this is the
// main attack on the RX-dominated night draw (modem-sleep can't help
// connectionless ESP-NOW — no AP/DTIM, so RX otherwise stays on). The conductor is
// exempt (it must beacon at 4 Hz and is typically wall-powered); the duty-cycle is
// gated on role == performer in src/main.cpp. The schedule logic is the
// dependency-free, host-tested include/powersave.h. Toggle live (and per-board,
// persisted to NVS key "ps") with the `powersave on|off` serial command.
#ifndef POWERSAVE_DEFAULT
#define POWERSAVE_DEFAULT 1  // performers duty-cycle by default; 0 = radio always on
#endif
// Radio OFF this long between windows, then ON for a window. At BEACON_INTERVAL_US
// = 250 ms (4 Hz), a 600 ms window spans ~2 beacons, so one is caught even with
// wake latency + jitter. ~600 ms ON / 4 s OFF ≈ 13% radio duty. Drift over a 4 s
// free-run on a ~tens-of-ppm crystal is sub-millisecond — invisible in the slow
// patterns. A pattern/position change now lands up to one OFF interval late, which
// is acceptable for the installation (noted in HANDOFF).
static constexpr int64_t DUTY_OFF_US    = 4000000;  // 4s radio off between listens
static constexpr int64_t DUTY_LISTEN_US = 600000;   // 600ms listen window

// ---- Performer CPU light-sleep (Milestone 3, Lever 1, Stage B) ---------------
// While the radio is off (Stage A above), the CPU light-sleeps between the
// moments it has real work instead of spinning delay(16): until the next
// animation frame on animated patterns, or clear to the next radio window /
// heartbeat edge on static ones (GLOW — SK6812s latch, so nothing needs the
// CPU). Decision logic is the dependency-free, host-tested include/napsched.h;
// main.cpp owns esp_light_sleep_start(). Gated on the same `powersave` toggle
// as Stage A (naps only ever happen inside radio-off spans, which only exist
// when powersave is on). Serial traffic (or a UART wakeup) holds naps off for a
// grace window so USB provisioning always wins over power.
static constexpr int64_t NAP_FRAME_US        = 33333;     // ~30 fps animated render
static constexpr int64_t NAP_MIN_US          = 5000;      // shorter isn't worth it
static constexpr int64_t NAP_MAX_US          = 1000000;   // safety cap per nap
static constexpr int64_t SERIAL_NAP_GRACE_US = 30000000;  // 30 s after serial traffic

// ---- Daytime deep-sleep (Milestone 3, Lever 2) --------------------------------
// A performer deep-sleeps through daylight (the calendar-life fix) and wakes
// every DUSK_RESAMPLE_US to re-check the light and listen for a beacon — a
// beacon carrying BEACON_FLAG_FIELD_AWAKE (`wake on` at the conductor) pins it
// awake for daytime testing. Decision logic is the dependency-free, host-tested
// include/dusk.h (fail-awake by design — see its header comment); main.cpp owns
// the ADC sampling and esp_deep_sleep(). Toggle per node with `dusk on|off`
// (NVS "dusk"); DEFAULT OFF — GPIO34 floats until the phototransistor is wired,
// and a floating pin must never be able to sleep a node.
#ifndef DUSK_DEFAULT
#define DUSK_DEFAULT 0
#endif
// Polarity + thresholds are PLACEHOLDERS until the phototransistor divider is
// bench-calibrated (pilot batch). day_above=true assumes PT from 3V3 with a
// pull-down (daylight pulls the reading up). Pick the divider so real noon and
// real night both land inside [DUSK_FLOOR_MV, DUSK_CEIL_MV] — outside reads as
// a broken sensor and fails awake.
static constexpr bool     DUSK_DAY_ABOVE = true;
static constexpr uint16_t DUSK_DAY_MV    = 1800;  // calibrate on the bench
static constexpr uint16_t DUSK_NIGHT_MV  = 900;   // calibrate on the bench
static constexpr uint16_t DUSK_FLOOR_MV  = 20;    // below: broken/floating sensor
static constexpr uint16_t DUSK_CEIL_MV   = 3100;  // above: broken/floating sensor
static constexpr int64_t  DUSK_SAMPLE_US       = 1000000;    // sample light @ 1 Hz
static constexpr int64_t  DUSK_DEBOUNCE_US     = 60000000;   // 60 s steady to flip
static constexpr int64_t  DUSK_SERIAL_GRACE_US = 300000000;  // 5 min after serial
static constexpr int64_t  DUSK_WAKE_TTL_US     = 60000000;   // flagged-beacon hold
static constexpr int64_t  DUSK_MIN_AWAKE_TIMER_US = 10000000;   // resample wake: 10 s
static constexpr int64_t  DUSK_MIN_AWAKE_COLD_US  = 600000000;  // cold boot: 10 min
static constexpr uint64_t DUSK_RESAMPLE_US = 900000000ULL;  // 15 min between wakes
// Battery-sense divider on PIN_VBAT: 47k over 10k => Vbat = Vpin * 5.7. Reading
// is reported in `info` (garbage until the divider is wired); low-batt policy
// comes later with the pilot hardware.
static constexpr float VBAT_DIVIDER = 5.7f;

// ---- INA228 power telemetry (Milestone 3 instrumentation, ARCHITECTURE §4.2) --
// A precision power monitor on 1–2 reference nodes only, wired in series between
// battery+ and the buck input. Probed over I2C at boot — the one shared firmware
// image runs everywhere, and a node without the chip just skips telemetry
// silently. The breakout's on-board shunt is 15 mΩ; max-expected-current sets the
// device's current LSB (our whole node draws <0.2 A, but the lib default 10 A
// scaling is still micro-amp-class resolution — plenty). MUST run in continuous
// conversion mode: in triggered mode the hardware energy/charge accumulators are
// invalid (the device stops tracking elapsed time). Default ESP32 I2C pins
// (SDA 21 / SCL 22 on both DevKitC and FireBeetle 2).
static constexpr uint8_t INA228_I2C_ADDR      = 0x40;    // breakout default
static constexpr float   INA228_SHUNT_OHMS    = 0.015f;  // on-board shunt
static constexpr float   INA228_MAX_CURRENT_A = 10.0f;   // current-LSB scaling
// How often an instrumented performer unicasts MSG_POWER to the conductor. The
// hardware accumulator integrates continuously regardless, so this is a logging
// cadence, not a sampling rate; the scheduler (powermon.h) defers sends until a
// radio-on window anyway.
static constexpr int64_t POWER_REPORT_INTERVAL_US = 60000000;  // 60 s

// ---- Diagnostics -------------------------------------------------------------
// How often each node prints a sync status line to serial (microseconds).
static constexpr int64_t DIAG_INTERVAL_US = 1000000;  // 1s
