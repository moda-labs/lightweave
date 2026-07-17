// Do Baskets Dream — node firmware (single image for every node).
//
// One conductor broadcasts a clock beacon over ESP-NOW; performers lock to it and
// render against synced time. A performer that misses a beacon keeps free-running
// on its last known offset and re-locks on the next one — a dropped packet causes
// at most slight drift, never a blackout.
//
// Every board runs THIS SAME binary. Role (conductor/performer) is a runtime
// value stored in NVS, default performer, set once over serial with `role …`.
// So you flash one image everywhere, then provision role/id/pos per board; each
// node then boots into its role unattended (important for battery field nodes).
//
// The sync logic lives in include/sync.h (dependency-free, unit-tested); this
// file is the on-device glue: radio, LEDs, NVS config, serial, and the render loop.

#include <Arduino.h>
#include <NeoPixelBus.h>
#include <Preferences.h>
#include <Update.h>
#include <WiFi.h>
#include <Wire.h>            // INA228 power monitor (I2C)
#include <Adafruit_INA228.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_timer.h>
#include <esp_mac.h>    // esp_read_mac / ESP_MAC_WIFI_STA
#include <esp_sleep.h>  // light-sleep + timer/UART wakeup (Stage B naps)
#include <driver/uart.h>  // uart_set_wakeup_threshold

#include "config.h"
#include "beacon.h"
#include "bootplan.h"
#include "dusk.h"
#include "firmware_version.h"
#include "identity.h"
#include "keepalive.h"
#include "macaddr.h"
#include "napsched.h"
#include "patterns.h"
#include "power_table.h"
#include "powermon.h"
#include "powersave.h"
#include "roster.h"
#include "serial_json.h"
#include "sync.h"
#include "table.h"
#include "table_wire.h"

// 16x SK6812 RGBW ring. RMT channel 0 with SK6812 timing.
NeoPixelBus<NeoGrbwFeature, NeoEsp32Rmt0Sk6812Method> strip(LED_COUNT, LED_PIN);

static const uint8_t BROADCAST_ADDR[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// ---- Node config in NVS (role + identity; set once over serial) --------------
static Preferences  g_prefs;
static NodeIdentity g_id   = {0, 0.0f, 0.0f};
static uint8_t      g_role = DEFAULT_ROLE;
static uint8_t      g_mac[6] = {0};  // this node's WiFi STA MAC — stable identity

// Performer radio duty-cycle (powersave.h logic, host-tested). g_radio_on tracks
// the actual radio power state so loop() never transmits while the radio is down;
// g_powersave is the runtime/NVS toggle (conductor ignores it — it must beacon).
static bool         g_powersave = (POWERSAVE_DEFAULT != 0);
static bool         g_radio_on  = true;  // radio is powered after radioBegin()
static DutyCycle    g_duty;

// Stage B: CPU light-sleep between work while the radio is off (napsched.h
// logic, host-tested). g_last_serial_us holds naps off around serial traffic so
// USB provisioning always wins; the nap counters feed the [nap] diag line —
// g_napped_us is MEASURED across each sleep (esp_timer delta), so it doubles as
// the on-hardware check that the clock is compensated across light sleep (if it
// weren't, slept-time would read ~0 while the nap count climbs, and synced time
// would visibly stall).
static const NapConfig NAP_CFG = {NAP_FRAME_US, NAP_MIN_US, NAP_MAX_US,
                                  SERIAL_NAP_GRACE_US};
static int64_t  g_last_serial_us = 0;
static uint32_t g_naps = 0;        // completed light-sleeps
static int64_t  g_napped_us = 0;   // total time measured asleep

// Lever 2: daytime deep-sleep (dusk.h logic, host-tested; fail-awake design).
// g_dusk_on is the runtime/NVS master switch, DEFAULT OFF — GPIO34 floats until
// the phototransistor is wired. g_rtc_was_day lives in RTC memory so it
// survives deep sleep: a timer wake starts the detector in "day" and re-sleeps
// after a short min-awake; every other boot starts in "night" (awake — the
// physical power-cycle override). g_wake_flag is the CONDUCTOR side: `wake on`
// sets BEACON_FLAG_FIELD_AWAKE in every beacon, summoning dusk-sleeping nodes
// at their next resample rendezvous (sticky in NVS so a conductor reboot can't
// drop the override). g_last_wake_flag_us is the PERFORMER side: when a flagged
// beacon last arrived (written in the recv callback under g_sync_mux).
static bool     g_dusk_on = (DUSK_DEFAULT != 0);
static Dusk     g_dusk;
static const DuskConfig DUSK_CFG = {DUSK_DAY_ABOVE,       DUSK_DAY_MV,
                                    DUSK_NIGHT_MV,        DUSK_FLOOR_MV,
                                    DUSK_CEIL_MV,         DUSK_DEBOUNCE_US,
                                    DUSK_SERIAL_GRACE_US, DUSK_WAKE_TTL_US};
static int64_t  g_dusk_earliest_us = 0;    // no dusk sleep before this (boot hold-off)
static int64_t  g_last_wake_flag_us = INT64_MIN / 2;  // "never" (avoids overflow)
static bool     g_wake_flag = false;       // conductor: field-awake override
static uint16_t g_light_mv = 0;            // last light sample, for diag/info
RTC_DATA_ATTR static bool g_rtc_was_day = false;
RTC_DATA_ATTR static bool g_rtc_have_power_policy = false;
RTC_DATA_ATTR static PowerPolicy g_rtc_power_policy = {4, 15, 20 * 60, 6 * 60,
                                                       12 * 60, 0, 0};

// Runtime field power policy. The conductor persists these knobs and includes
// them in every beacon; performers apply the latest received policy directly, so
// changing schedule/intervals is a control-plane action, not a reflash.
static PowerPolicy g_power_policy = {4, 15, 20 * 60, 6 * 60, 12 * 60, 0, 0};
static uint16_t    g_policy_base_min = 12 * 60;
static uint32_t    g_policy_base_epoch_s = 0;
static int64_t     g_policy_clock_set_us = 0;
static KeepAliveConfig g_keepalive = keepAliveDefault();
static bool        g_ota_maintenance = false;
static int64_t     g_ota_maintenance_until_us = 0;
static constexpr int64_t OTA_WINDOW_US = 15LL * 60LL * 1000000LL;
static constexpr uint32_t OTA_FINALIZE_WAIT_MS = 30000;
static constexpr int64_t OTA_STATUS_FRESH_US = 30000000LL;
static bool        g_ota_write_active = false;
static uint32_t    g_ota_write_size = 0;
static uint32_t    g_ota_write_written = 0;
static uint32_t    g_ota_write_crc = 0;
static uint32_t    g_ota_write_expected_crc = 0;
static bool        g_ota_finalize_pending = false;
static bool        g_ota_reboot_pending = false;
static OtaStatusTable g_ota_status;
static portMUX_TYPE   g_ota_status_mux = portMUX_INITIALIZER_UNLOCKED;
static OtaStatusMsg   g_ota_status_pending = {{BEACON_MAGIC, PROTO_VERSION, MSG_OTA_STATUS},
                                               {0}, OTA_PHASE_IDLE, OTA_ERR_NONE, 0, 0};
static bool           g_ota_status_pending_dirty = false;
// Keep large JSON snapshots off loopTask's stack. The machine-state response
// copies 64-entry tables before printing so radio callbacks can keep updating.
static Roster         g_state_roster_snapshot;
static OtaStatusTable g_state_ota_status_snapshot;

static bool otaMaintenanceActive(int64_t t);
static void otaWriteAbort();
static void otaSetLocalStatus(uint8_t phase, uint8_t error, uint32_t offset,
                              uint32_t crc32);
static void maybeOtaStatusReport();
static void otaRadioBegin(const OtaBeginMsg& msg);
static void otaRadioChunk(const OtaChunkMsg& msg);
static void otaRadioEnd();
static void otaFinalizePending();
static void otaBroadcastBegin(uint32_t size, uint32_t crc32);
static void otaBroadcastChunk(uint32_t offset, const uint8_t* data, uint8_t len);
static void otaBroadcastEnd();
static bool otaExpectedPerformersComplete(uint32_t size, uint32_t crc32);
static bool otaWaitForExpectedPerformers(uint32_t size, uint32_t crc32,
                                         uint32_t wait_ms);

// INA228 power telemetry (powermon.h logic, host-tested; ARCHITECTURE §4.2).
// Probed over I2C at boot: 1–2 reference nodes carry the breakout in series
// between battery+ and the buck input; every other node runs the same image and
// just skips telemetry. The chip accumulates energy/charge in hardware
// (continuous mode — REQUIRED, triggered mode invalidates the accumulators), so
// firmware only reads totals and reports them. g_power_reset_us anchors the
// elapsed_s in each report: seconds since boot or the last `power reset`,
// whichever is later. Caveat: the chip keeps accumulating across an ESP32
// reboot (it stays battery-powered), so after an unplanned reboot the energy
// total is still right but avg-W (energy/elapsed) overstates until the next
// `power reset` — the overnight flow is `power reset` at dusk, read in the
// morning with the no-reset pyserial trick (FLASHING.md; a DTR reset does NOT
// zero the chip, only the elapsed anchor).
static Adafruit_INA228 g_ina228;
static bool       g_have_ina228 = false;
static PowerSched g_power_sched = {0};
static int64_t    g_power_reset_us = 0;
// Conductor side: MSG_POWER reports land in the recv callback, which must not
// print — they stash here under a spinlock and loop() drains + logs them.
static constexpr uint8_t POWER_Q_MAX = 4;
static PowerMsg     g_power_q[POWER_Q_MAX];
static uint8_t      g_power_q_n = 0;
static uint32_t     g_power_q_dropped = 0;
static portMUX_TYPE g_power_mux = portMUX_INITIALIZER_UNLOCKED;
static PowerTable   g_power_table;

// Conductor's authoritative layout table (table.h logic, host-tested). Declared
// here so the NVS load/save below can reach it; edited only over serial on the
// conductor and read by the broadcast — both in loop() — so it needs no spinlock
// (unlike the roster, which the recv callback writes).
static LayoutTable  g_table;

static inline bool isConductor() { return g_role == ROLE_CONDUCTOR; }

static void configLoad() {
  g_prefs.begin("node", /*readonly*/ true);
  g_id.id = g_prefs.getUShort("id", 0);
  g_id.x = g_prefs.getFloat("x", 0.0f);
  g_id.y = g_prefs.getFloat("y", 0.0f);
  g_role = g_prefs.getUChar("role", DEFAULT_ROLE);
  g_powersave = g_prefs.getBool("ps", POWERSAVE_DEFAULT != 0);
  g_dusk_on = g_prefs.getBool("dusk", DUSK_DEFAULT != 0);
  g_wake_flag = g_prefs.getBool("wake", false);
  g_keepalive = keepAliveDefault();
  if (g_prefs.getBool("ka_en", false)) g_keepalive.flags |= KEEPALIVE_FLAG_ENABLED;
  g_keepalive.interval_ms = g_prefs.getUShort("ka_int", g_keepalive.interval_ms);
  g_keepalive.pulse_ms = g_prefs.getUShort("ka_pulse", g_keepalive.pulse_ms);
  g_keepalive.brightness = g_prefs.getUChar("ka_bri", g_keepalive.brightness);
  keepAliveSanitize(g_keepalive);
  g_power_policy = powerPolicyDefault();
  g_power_policy.light_sleep_check_s = g_prefs.getUShort("p_lchk", g_power_policy.light_sleep_check_s);
  g_power_policy.deep_sleep_check_min = g_prefs.getUShort("p_dchk", g_power_policy.deep_sleep_check_min);
  g_power_policy.led_on_start_min = g_prefs.getUShort("p_on", g_power_policy.led_on_start_min);
  g_power_policy.led_on_end_min = g_prefs.getUShort("p_off", g_power_policy.led_on_end_min);
  g_power_policy.current_min = g_prefs.getUShort("p_min", g_power_policy.current_min);
  g_power_policy.current_epoch_s = g_prefs.getUInt("p_epoch", g_power_policy.current_epoch_s);
  if (g_prefs.getBool("p_sched", false)) g_power_policy.flags |= POWER_FLAG_SCHEDULE_ENABLED;
  if (g_prefs.getBool("p_sleep", false)) g_power_policy.flags |= POWER_FLAG_FORCE_SLEEP;
  if (g_wake_flag) g_power_policy.flags |= POWER_FLAG_FORCE_AWAKE;
  powerPolicySanitize(g_power_policy);
  g_policy_base_min = g_power_policy.current_min;
  g_policy_base_epoch_s = g_power_policy.current_epoch_s;
  g_prefs.end();
}

static void powersaveSave() {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putBool("ps", g_powersave);
  g_prefs.end();
}

static void duskSave() {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putBool("dusk", g_dusk_on);
  g_prefs.end();
}

static void wakeFlagSave() {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putBool("wake", g_wake_flag);
  g_prefs.end();
}

static void keepAliveSave() {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putBool("ka_en", keepAliveEnabled(g_keepalive));
  g_prefs.putUShort("ka_int", g_keepalive.interval_ms);
  g_prefs.putUShort("ka_pulse", g_keepalive.pulse_ms);
  g_prefs.putUChar("ka_bri", g_keepalive.brightness);
  g_prefs.end();
}

static void powerPolicySave() {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putUShort("p_lchk", g_power_policy.light_sleep_check_s);
  g_prefs.putUShort("p_dchk", g_power_policy.deep_sleep_check_min);
  g_prefs.putUShort("p_on", g_power_policy.led_on_start_min);
  g_prefs.putUShort("p_off", g_power_policy.led_on_end_min);
  g_prefs.putUShort("p_min", g_power_policy.current_min);
  g_prefs.putUInt("p_epoch", g_power_policy.current_epoch_s);
  g_prefs.putBool("p_sched", powerPolicyScheduleEnabled(g_power_policy));
  g_prefs.putBool("p_sleep", powerPolicyForceSleep(g_power_policy));
  g_prefs.putBool("wake", g_wake_flag);
  g_prefs.end();
}

static void identitySave() {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putUShort("id", g_id.id);
  g_prefs.putFloat("x", g_id.x);
  g_prefs.putFloat("y", g_id.y);
  g_prefs.end();
}

static void roleSave() {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putUChar("role", g_role);
  g_prefs.end();
}

// The conductor's layout table persists as a single NVS blob, so it survives a
// power-cycle and the field runs with no laptop. (Performers don't broadcast, so
// an empty/absent blob just means "no table to advertise".)
static void tableLoad() {
  g_prefs.begin("node", /*readonly*/ true);
  size_t got = g_prefs.getBytes("table", &g_table, sizeof(g_table));
  g_prefs.end();
  if (got != sizeof(g_table) || g_table.count > TABLE_MAX) tableInit(g_table);
}

static void tableSave() {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putBytes("table", &g_table, sizeof(g_table));
  g_prefs.end();
}

// ---- Sync state --------------------------------------------------------------
// Written from the ESP-NOW recv callback, read from loop(). Guarded by a spinlock
// so 64-bit fields can't tear across the two contexts.
static SyncState g_sync;
static BeaconMsg g_beacon = {{BEACON_MAGIC, PROTO_VERSION, MSG_BEACON},
                             /*epoch*/ 0, patterns::SWEEP, /*brightness*/ 48,
                             /*palette*/ 0, /*flags*/ 0, {0, 0, 0, 0},
                             {4, 15, 20 * 60, 6 * 60, 12 * 60, 0, 0},
                             {0, 10000, 100, 64}, 0};
static uint32_t g_tx_seq = 0;
static portMUX_TYPE g_sync_mux = portMUX_INITIALIZER_UNLOCKED;

// Performer -> conductor registration state. The conductor's MAC is learned from
// the recv-info of an incoming beacon (written in the recv callback under
// g_sync_mux); the actual peer-add + unicast happens from loop(), since doing
// radio work inside the recv callback is unsafe.
static uint8_t  g_conductor_mac[6] = {0};
static bool     g_have_conductor = false;
static bool     g_conductor_peer_added = false;
static int64_t  g_next_register_us = 0;

// Conductor roster: every node that has registered, keyed on MAC. The logic lives
// in roster.h (host-tested); here we hold one instance and a spinlock, since it is
// written from the recv callback (MSG_REGISTER) and read from loop() (the `roster`
// command). The lock wraps each access, mirroring g_sync_mux around syncOnBeacon.
static Roster       g_roster;
static portMUX_TYPE g_roster_mux = portMUX_INITIALIZER_UNLOCKED;
// Row-reply requests: the recv callback stashes the MAC of a REGISTER that
// earns an immediate single-row table reply (tableRowReplyWanted — new to the
// roster or unprovisioned), and loop() drains + broadcasts the rows. Same
// stash-under-lock/drain-in-loop shape as the power-report queue: no radio
// work in the callback. Written under g_roster_mux (the REGISTER path already
// holds it). Overflow just drops the request — the node's next REGISTER (10 s)
// retries it, so nothing is permanently lost.
static constexpr uint8_t ROWREQ_MAX = 8;
static uint8_t      g_rowreq[ROWREQ_MAX][6];
static volatile uint8_t g_rowreq_n = 0;
// Next steady-state table broadcast. File-scope (not a loop-local static) so
// the `role` command can zero it: a re-promoted conductor must advertise the
// table immediately, not resume a stale schedule up to 60 s in the future.
static int64_t      g_next_table_us = 0;
static int64_t      g_next_cal_roster_us = 0;

// Dense rank in the conductor's sorted MAC roster, learned from MSG_ROSTER while
// calibration mode is active. 0 means "not learned yet" and renders off for
// calibration until the next roster chunk arrives.
static uint16_t     g_calibration_rank = 0;

// Performer position adopted from a MSG_TABLE row. The recv callback can't write
// flash, so it stashes the new (x,y) here (under g_sync_mux) and loop() applies +
// caches it to NVS.
static bool  g_pos_pending = false;
static float g_pos_pending_x = 0.0f;
static float g_pos_pending_y = 0.0f;

static inline int64_t now_us() { return esp_timer_get_time(); }

// ---- Pattern config in NVS (the broadcast pattern; tweaked live over serial) ---
// Defined here, *after* g_beacon, so it can touch it — configLoad() above can't
// (it's defined before g_beacon). Only the conductor's pattern drives the field,
// but every node persists/restores it so a conductor survives a power-cycle with
// its tuning intact, and this seeds the show-program storage later.
static void patternConfigLoad() {
  g_prefs.begin("node", /*readonly*/ true);
  // patternBootSafe (pattern_ids.h, host-tested): a persisted SOLID must not
  // survive a power-cycle — see its comment.
  g_beacon.pattern_id =
      patterns::patternBootSafe(g_prefs.getUShort("pat", patterns::SWEEP));
  g_beacon.brightness = g_prefs.getUChar("bri", 48);
  if (g_beacon.brightness > MAX_BRIGHTNESS) g_beacon.brightness = MAX_BRIGHTNESS;
  g_beacon.params[0] = g_prefs.getUShort("p0", 0);
  g_beacon.params[1] = g_prefs.getUShort("p1", 0);
  g_beacon.params[2] = g_prefs.getUShort("p2", 0);
  g_beacon.params[3] = g_prefs.getUShort("p3", 0);
  g_beacon.power = g_power_policy;
  g_beacon.keepalive = g_keepalive;
  g_prefs.end();
}

// Takes a caller-held snapshot, not the live g_beacon: on a performer the recv
// callback overwrites g_beacon (under g_sync_mux) from the WiFi task, and NVS
// writes are far too slow to hold a spinlock across — so the serial handlers
// mutate + snapshot under the lock, then persist from the copy.
static void patternConfigSave(const BeaconMsg& b) {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putUShort("pat", b.pattern_id);
  g_prefs.putUChar("bri", b.brightness);
  g_prefs.putUShort("p0", b.params[0]);
  g_prefs.putUShort("p1", b.params[1]);
  g_prefs.putUShort("p2", b.params[2]);
  g_prefs.putUShort("p3", b.params[3]);
  g_prefs.end();
}

static uint16_t powerPolicyCurrentMinute(int64_t t) {
  int64_t elapsed_min = 0;
  if (g_policy_clock_set_us > 0 && t >= g_policy_clock_set_us) {
    elapsed_min = (t - g_policy_clock_set_us) / 60000000LL;
  }
  return (uint16_t)((g_policy_base_min + elapsed_min) % POWER_DAY_MINUTES);
}

static uint32_t powerPolicyCurrentEpoch(int64_t t) {
  int64_t elapsed_s = 0;
  if (g_policy_clock_set_us > 0 && t >= g_policy_clock_set_us) {
    elapsed_s = (t - g_policy_clock_set_us) / 1000000LL;
  }
  return g_policy_base_epoch_s + (uint32_t)elapsed_s;
}

static PowerPolicy powerPolicySnapshot(int64_t t) {
  PowerPolicy p = g_power_policy;
  p.current_min = powerPolicyCurrentMinute(t);
  p.current_epoch_s = powerPolicyCurrentEpoch(t);
  if (g_wake_flag || otaMaintenanceActive(t)) p.flags |= POWER_FLAG_FORCE_AWAKE;
  else p.flags &= ~POWER_FLAG_FORCE_AWAKE;
  powerPolicySanitize(p);
  return p;
}

static DutyConfig currentDutyConfig(const PowerPolicy& p) {
  PowerPolicy clean = p;
  powerPolicySanitize(clean);
  return {(int64_t)clean.light_sleep_check_s * 1000000LL, DUTY_LISTEN_US};
}

static uint64_t powerPolicyDeepSleepUs(const PowerPolicy& p) {
  PowerPolicy clean = p;
  powerPolicySanitize(clean);
  return (uint64_t)powerPolicyAlignedSleepSeconds(clean) * 1000000ULL;
}

static void powerPolicyAdvanceToSyncedNow(PowerPolicy& p, const BeaconMsg& b,
                                          const SyncState& s, int64_t local_now_us) {
  if (p.current_epoch_s == 0 || !s.locked) return;
  int64_t synced_now = syncedTime(s, local_now_us);
  if (synced_now <= b.epoch_us) return;
  uint32_t elapsed_s = (uint32_t)((synced_now - b.epoch_us) / 1000000LL);
  p.current_epoch_s += elapsed_s;
}

static void powerPolicyApplyCommand(const SerialJsonCommand& cmd) {
  if (cmd.has_light_sleep_check_s)
    g_power_policy.light_sleep_check_s = cmd.light_sleep_check_s;
  if (cmd.has_deep_sleep_check_min)
    g_power_policy.deep_sleep_check_min = cmd.deep_sleep_check_min;
  if (cmd.has_led_on_start_min)
    g_power_policy.led_on_start_min = cmd.led_on_start_min;
  if (cmd.has_led_on_end_min)
    g_power_policy.led_on_end_min = cmd.led_on_end_min;
  if (cmd.has_schedule_enabled) {
    if (cmd.schedule_enabled) g_power_policy.flags |= POWER_FLAG_SCHEDULE_ENABLED;
    else g_power_policy.flags &= ~POWER_FLAG_SCHEDULE_ENABLED;
  }
  if (cmd.has_force_awake) {
    g_wake_flag = cmd.force_awake;
    if (g_wake_flag) {
      g_power_policy.flags |= POWER_FLAG_FORCE_AWAKE;
      g_power_policy.flags &= ~POWER_FLAG_FORCE_SLEEP;
    } else {
      g_power_policy.flags &= ~POWER_FLAG_FORCE_AWAKE;
    }
  }
  if (cmd.has_force_sleep) {
    if (cmd.force_sleep) {
      g_wake_flag = false;
      g_power_policy.flags &= ~POWER_FLAG_FORCE_AWAKE;
      g_power_policy.flags |= POWER_FLAG_FORCE_SLEEP;
    } else {
      g_power_policy.flags &= ~POWER_FLAG_FORCE_SLEEP;
    }
  }
  if (cmd.has_current_min) {
    g_policy_base_min = cmd.current_min % POWER_DAY_MINUTES;
    g_power_policy.current_min = g_policy_base_min;
  }
  if (cmd.has_current_epoch_s) {
    g_policy_base_epoch_s = cmd.current_epoch_s;
    g_power_policy.current_epoch_s = g_policy_base_epoch_s;
  }
  if (cmd.has_current_min || cmd.has_current_epoch_s)
    g_policy_clock_set_us = now_us();
  powerPolicySanitize(g_power_policy);
  powerPolicySave();
}

static void keepAliveApplyCommand(const SerialJsonCommand& cmd) {
  if (cmd.has_keepalive_enabled) {
    if (cmd.keepalive_enabled) g_keepalive.flags |= KEEPALIVE_FLAG_ENABLED;
    else g_keepalive.flags &= ~KEEPALIVE_FLAG_ENABLED;
  }
  if (cmd.has_keepalive_interval_ms)
    g_keepalive.interval_ms = cmd.keepalive_interval_ms;
  if (cmd.has_keepalive_pulse_ms)
    g_keepalive.pulse_ms = cmd.keepalive_pulse_ms;
  if (cmd.has_keepalive_brightness)
    g_keepalive.brightness = cmd.keepalive_brightness;
  keepAliveSanitize(g_keepalive);
  keepAliveSave();
}

// ---- ESP-NOW receive ---------------------------------------------------------
// Registered on every node. Validates the common header, then dispatches on the
// message type. The recv callback signature changed in Arduino-ESP32 3.x —
// support both, and grab the sender MAC, which we need for bidirectional traffic.
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
void onRecv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
  const uint8_t* src = info->src_addr;
#else
void onRecv(const uint8_t* mac, const uint8_t* data, int len) {
  const uint8_t* src = mac;
#endif
  if (len < (int)sizeof(MsgHeader)) return;
  MsgHeader hdr;
  memcpy(&hdr, data, sizeof(hdr));
  if (hdr.magic != BEACON_MAGIC || hdr.version != PROTO_VERSION) return;

  switch (hdr.type) {
    case MSG_BEACON: {
      if (isConductor()) return;  // a conductor follows no one
      if (len != (int)sizeof(BeaconMsg)) return;
      BeaconMsg b;
      memcpy(&b, data, sizeof(b));
      keepAliveSanitize(b.keepalive);
      int64_t local = now_us();
      portENTER_CRITICAL(&g_sync_mux);
      syncOnBeacon(g_sync, b.epoch_us, b.seq, local);
      g_beacon = b;
      memcpy(g_conductor_mac, src, 6);  // remember who to register with
      g_have_conductor = true;
      // Field-awake override: a flagged beacon blocks dusk-sleep for
      // DUSK_WAKE_TTL_US — this is what a resample rendezvous checks for.
      if (b.flags & BEACON_FLAG_FIELD_AWAKE) g_last_wake_flag_us = local;
      portEXIT_CRITICAL(&g_sync_mux);
      break;
    }
    case MSG_REGISTER: {
      if (!isConductor()) return;  // only the conductor keeps a roster
      if (len != (int)sizeof(RegisterMsg)) return;
      RegisterMsg r;
      memcpy(&r, data, sizeof(r));
      portENTER_CRITICAL(&g_roster_mux);
      // Known-ness is checked BEFORE the upsert so a full roster (which drops
      // the insert without a count change) can't mask a new node.
      bool known = rosterFind(g_roster, r.mac) >= 0;
      rosterUpsert(g_roster, r.mac, r.id, r.fw, r.build, r.dirty, r.version,
                   now_us());
      // A first-join or unprovisioned node needs its table row NOW — its radio
      // is provably on (it just transmitted), and the 60 s steady-state
      // rebroadcast is a lottery through the ~13% radio duty cycle. Stash the
      // MAC; loop() replies with just its row (tableRowBuild).
      if (tableRowReplyWanted(known, r.id) && g_rowreq_n < ROWREQ_MAX) {
        memcpy(g_rowreq[g_rowreq_n], r.mac, 6);
        g_rowreq_n = g_rowreq_n + 1;
      }
      portEXIT_CRITICAL(&g_roster_mux);
      break;
    }
    case MSG_ROSTER: {
      if (isConductor()) return;
      if (len != (int)sizeof(RosterMsg)) return;
      RosterMsg m;
      memcpy(&m, data, sizeof(m));
      if (m.n > ROSTER_MACS_PER_MSG || m.chunk >= m.chunks) return;
      uint16_t rank = rosterMsgFindRank(m, g_mac);
      if (rank) {
        portENTER_CRITICAL(&g_sync_mux);
        g_calibration_rank = rank;
        portEXIT_CRITICAL(&g_sync_mux);
      }
      break;
    }
    case MSG_TABLE: {
      if (isConductor()) return;  // conductor is the source, never adopts
      // Two-step validation (table_wire.h, host-tested): bounds before the
      // copy, exact length-vs-row-count after it.
      if (!tableMsgLenPlausible(len)) return;
      TableMsg m;
      memcpy(&m, data, len);
      if (!tableMsgLenValid(len, m.n)) return;
      // Find our own row; stash the position for loop() to adopt + persist.
      float px, py;
      if (tableMsgFindRow(m, g_mac, px, py)) {
        portENTER_CRITICAL(&g_sync_mux);
        g_pos_pending = true;
        g_pos_pending_x = px;
        g_pos_pending_y = py;
        portEXIT_CRITICAL(&g_sync_mux);
      }
      break;
    }
    case MSG_POWER: {
      if (!isConductor()) return;  // reports flow performer -> conductor only
      if (len != (int)sizeof(PowerMsg)) return;
      portENTER_CRITICAL(&g_power_mux);
      if (g_power_q_n < POWER_Q_MAX) {
        memcpy(&g_power_q[g_power_q_n], data, sizeof(PowerMsg));
        g_power_q_n++;
      } else {
        g_power_q_dropped++;  // can't happen at 1–2 nodes / 60 s, but never lie
      }
      portEXIT_CRITICAL(&g_power_mux);
      break;
    }
    case MSG_OTA_BEGIN: {
      if (isConductor()) return;
      if (len != (int)sizeof(OtaBeginMsg)) return;
      OtaBeginMsg m;
      memcpy(&m, data, sizeof(m));
      otaRadioBegin(m);
      break;
    }
    case MSG_OTA_CHUNK: {
      if (isConductor()) return;
      if (len < (int)offsetof(OtaChunkMsg, data)) return;
      OtaChunkMsg m;
      memcpy(&m, data, len);
      if (m.n == 0 || m.n > OTA_SERIAL_CHUNK_MAX) return;
      if (len != (int)(offsetof(OtaChunkMsg, data) + m.n)) return;
      otaRadioChunk(m);
      break;
    }
    case MSG_OTA_END: {
      if (isConductor()) return;
      if (len != (int)sizeof(OtaEndMsg)) return;
      otaRadioEnd();
      break;
    }
    case MSG_OTA_STATUS: {
      if (!isConductor()) return;
      if (len != (int)sizeof(OtaStatusMsg)) return;
      OtaStatusMsg m;
      memcpy(&m, data, sizeof(m));
      portENTER_CRITICAL(&g_ota_status_mux);
      otaStatusUpsert(g_ota_status, m.mac, m.phase, m.error, m.offset,
                      m.crc32, now_us());
      portEXIT_CRITICAL(&g_ota_status_mux);
      break;
    }
    default:
      break;
  }
}

// ---- Radio setup -------------------------------------------------------------
// Pin the channel, set modem-sleep, init ESP-NOW, add the broadcast peer, and
// register the recv callback. Shared by the initial bring-up and every duty-cycle
// wake — esp_wifi_stop()/start() tears the peer table down, so it must be rebuilt
// on each wake (recv-cb registration is re-applied here too, to be safe).
static void espnowStart() {
  // Pin the channel explicitly so every node agrees without scanning. The channel
  // can reset across an esp_wifi_start(), so (re)set it here on every bring-up.
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);

  // Modem-sleep is a battery-budget requirement (brief: don't-break list). It is
  // the default in STA mode; set it explicitly so it can't silently regress.
  esp_wifi_set_ps(WIFI_PS_MIN_MODEM);

  // Tolerate double-init: a redundant radioWake() (role churn, defensive
  // callers) must not abort peer/callback setup below.
  esp_err_t err = esp_now_init();
  if (err != ESP_OK && err != ESP_ERR_ESPNOW_EXIST) {
    Serial.println("ESP-NOW init failed");
    return;
  }

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, BROADCAST_ADDR, 6);
  peer.channel = WIFI_CHANNEL;
  peer.encrypt = false;
  esp_now_add_peer(&peer);

  esp_now_register_recv_cb(onRecv);  // every node; dispatches on message type
}

static void radioBegin() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();  // we never join an AP; just need the STA interface up
  espnowStart();
  g_radio_on = true;
}

// Power the radio DOWN between listen windows (performer duty-cycle). Tears down
// ESP-NOW, then stops the WiFi driver so the PHY/RX actually powers off — this is
// the draw we're cutting. Rendering keeps running from the synced clock meanwhile.
static void radioSleep() {
  esp_now_deinit();
  esp_wifi_stop();
  g_radio_on = false;
}

// Power the radio back UP for a listen window. esp_wifi_stop() dropped the peer
// table, so espnowStart() re-adds the broadcast peer and recv callback; the
// learned conductor unicast peer is gone too, so flag it for re-add on the next
// register.
static void radioWake() {
  esp_wifi_start();
  espnowStart();
  g_conductor_peer_added = false;
  g_radio_on = true;
}

static void broadcastBeacon() {
  BeaconMsg b = g_beacon;
  b.hdr.magic = BEACON_MAGIC;
  b.hdr.version = PROTO_VERSION;
  b.hdr.type = MSG_BEACON;
  b.epoch_us = now_us();
  b.seq = g_tx_seq++;
  b.power = powerPolicySnapshot(b.epoch_us);
  b.keepalive = g_keepalive;
  keepAliveSanitize(b.keepalive);
  b.flags = powerPolicyForceAwake(b.power) ? BEACON_FLAG_FIELD_AWAKE : 0;
  esp_now_send(BROADCAST_ADDR, (const uint8_t*)&b, sizeof(b));
}

// Snapshot the learned conductor MAC and make sure it exists as a unicast peer.
// esp_wifi_stop() drops the whole peer table on every duty-cycle sleep, so this
// re-adds the peer whenever it's missing — call it before ANY unicast to the
// conductor (REGISTER, POWER). Peer-add happens here in loop context, never in
// the recv callback. Returns true when a unicast can be sent to cmac.
static bool conductorPeerReady(uint8_t cmac[6]) {
  bool have;
  portENTER_CRITICAL(&g_sync_mux);
  have = g_have_conductor;
  if (have) memcpy(cmac, g_conductor_mac, 6);
  portEXIT_CRITICAL(&g_sync_mux);
  if (!have) return false;

  if (!g_conductor_peer_added) {
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, cmac, 6);
    peer.channel = WIFI_CHANNEL;
    peer.encrypt = false;
    esp_err_t err = esp_now_add_peer(&peer);
    if (err == ESP_OK || err == ESP_ERR_ESPNOW_EXIST) g_conductor_peer_added = true;
  }
  return g_conductor_peer_added;
}

// Performer: announce ourselves to the conductor we've heard, periodically so the
// roster self-heals after a conductor restart.
static void maybeRegister(int64_t t) {
  if (isConductor()) return;
  if (t < g_next_register_us) return;
  uint8_t cmac[6];
  if (!conductorPeerReady(cmac)) return;  // no conductor heard yet / peer-add failed
  g_next_register_us = t + REGISTER_INTERVAL_US;

  RegisterMsg r = {{BEACON_MAGIC, PROTO_VERSION, MSG_REGISTER}, {0}, g_id.id,
                   PROTO_VERSION, (uint32_t)FIRMWARE_BUILD_ID,
                   (uint8_t)FIRMWARE_BUILD_DIRTY, {0}};
  firmwareCopyVersion(r.version, FIRMWARE_VERSION);
  memcpy(r.mac, g_mac, 6);
  esp_now_send(cmac, (const uint8_t*)&r, sizeof(r));
}

// ---- INA228 power telemetry (ARCHITECTURE §4.2) --------------------------------

// Read one sample off the local INA228. Lib units (verified in its source):
// readEnergy Joules, readCharge Coulombs, readBusVoltage VOLTS, readCurrent mA.
// elapsed_s anchors avg-W (see the globals comment for the reboot caveat).
static PowerSample readPowerSample(int64_t t) {
  PowerSample s;
  s.energy_j   = g_ina228.readEnergy();
  s.charge_c   = g_ina228.readCharge();
  s.bus_v      = g_ina228.readBusVoltage();
  s.current_ma = g_ina228.readCurrent();
  s.elapsed_s  = (uint32_t)((t - g_power_reset_us) / 1000000);
  return s;
}

// One [power] log line — shared by the conductor's report drain and the local
// `power` bench command, so both paths print identical, comparable numbers.
static void printPowerSample(const uint8_t mac[6], const PowerSample& s) {
  char m[18];
  Serial.printf(
      "[power] %s  E=%.3f Wh  avg=%.2f W  Q=%.1f mAh  V=%.2f V  I=%.1f mA  (%lu s)%s\n",
      macStr(mac, m), powerWh(s.energy_j), powerAvgW(s.energy_j, s.elapsed_s),
      powerMah(s.charge_c), s.bus_v, s.current_ma, (unsigned long)s.elapsed_s,
      powerPlausible(s) ? "" : "  ** IMPLAUSIBLE — sensor/wiring fault?");
}

// Instrumented performer: unicast the hardware-accumulated totals to the
// conductor. Purely a logging path — the chip integrates regardless — so the
// schedule (powermon.h, host-tested) simply fires at the first radio-on moment
// after each interval; no retries or acks needed.
static void maybePowerReport(int64_t t) {
  if (!g_have_ina228 || isConductor()) return;
  // Cheap time gate BEFORE conductorPeerReady: the peer check takes the
  // g_sync_mux spinlock (the same lock the beacon recv callback contends), so
  // don't pay it every loop pass of a listen window for a once-a-minute
  // report. powerReportDue re-checks and stays authoritative.
  if (t < g_power_sched.next_us) return;
  uint8_t cmac[6];
  bool can_send = g_radio_on && conductorPeerReady(cmac);
  if (!powerReportDue(g_power_sched, t, POWER_REPORT_INTERVAL_US, can_send)) return;

  PowerMsg m = {{BEACON_MAGIC, PROTO_VERSION, MSG_POWER}, {0}, readPowerSample(t)};
  memcpy(m.mac, g_mac, 6);
  esp_now_send(cmac, (const uint8_t*)&m, sizeof(m));
}

// Conductor: drain + log the reports stashed by the recv callback. Deliberately
// NOT gated on recent serial activity like the diag lines — the whole point is
// a conductor left on USB overnight collecting every instrumented node's Wh
// (read the scrollback in the morning). ~1 line/min/node, negligible.
static void drainPowerReports() {
  PowerMsg q[POWER_Q_MAX];
  uint8_t n;
  uint32_t dropped;
  portENTER_CRITICAL(&g_power_mux);
  n = g_power_q_n;
  if (n) memcpy(q, g_power_q, sizeof(PowerMsg) * n);
  g_power_q_n = 0;
  dropped = g_power_q_dropped;
  g_power_q_dropped = 0;
  portEXIT_CRITICAL(&g_power_mux);

  for (uint8_t i = 0; i < n; i++) {
    PowerSample s = q[i].s;  // copy out of the packed msg (no packed-ref binding)
    printPowerSample(q[i].mac, s);
    powerTableUpsert(g_power_table, q[i].mac, s, now_us());
  }
  if (dropped)
    Serial.printf("[power] %lu report(s) dropped (queue full)\n",
                  (unsigned long)dropped);
}

// Conductor: broadcast the authoritative layout table, split into chunks that fit
// one ESP-NOW payload. Sent occasionally (positions are static); a node hears it,
// adopts its own row, and caches to NVS. Chunk math lives in table_wire.h
// (host-tested); this is just the radio call per chunk.
static void broadcastTable() {
  uint8_t chunks = tableChunkCount(g_table.count);  // 0 when table empty
  for (uint8_t c = 0; c < chunks; c++) {
    TableMsg m;
    size_t len = tableChunkBuild(g_table, c, m);
    esp_now_send(BROADCAST_ADDR, (const uint8_t*)&m, len);
  }
}

static int macCompare(const uint8_t a[6], const uint8_t b[6]) {
  for (uint8_t i = 0; i < 6; i++) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}

// Conductor: while calibration is active, broadcast the alive MAC roster sorted
// by MAC. Performers use their rank in this sorted list as the calibration
// identity, so no serial-provisioned `id` is needed and collisions are impossible
// within the current roster.
static void broadcastCalibrationRoster() {
  uint8_t macs[ROSTER_MAX][6];
  uint8_t count;
  portENTER_CRITICAL(&g_roster_mux);
  count = g_roster.count;
  for (uint8_t i = 0; i < count; i++) memcpy(macs[i], g_roster.entries[i].mac, 6);
  portEXIT_CRITICAL(&g_roster_mux);
  if (count == 0) return;

  for (uint8_t i = 1; i < count; i++) {
    uint8_t key[6];
    memcpy(key, macs[i], 6);
    int j = i - 1;
    while (j >= 0 && macCompare(macs[j], key) > 0) {
      memcpy(macs[j + 1], macs[j], 6);
      j--;
    }
    memcpy(macs[j + 1], key, 6);
  }

  uint8_t chunks = (uint8_t)((count + ROSTER_MACS_PER_MSG - 1) / ROSTER_MACS_PER_MSG);
  for (uint8_t c = 0; c < chunks; c++) {
    RosterMsg m = {};
    m.hdr.magic = BEACON_MAGIC;
    m.hdr.version = PROTO_VERSION;
    m.hdr.type = MSG_ROSTER;
    m.chunk = c;
    m.chunks = chunks;
    m.base_rank = (uint16_t)(c * ROSTER_MACS_PER_MSG);
    uint8_t start = (uint8_t)m.base_rank;
    uint8_t remaining = count - start;
    m.n = remaining > ROSTER_MACS_PER_MSG ? ROSTER_MACS_PER_MSG : remaining;
    for (uint8_t i = 0; i < m.n; i++) memcpy(m.macs[i], macs[start + i], 6);
    esp_now_send(BROADCAST_ADDR, (const uint8_t*)&m, sizeof(m));
  }
}

// Performer: apply a position handed down by the conductor's table (stashed in the
// recv callback). Saves to NVS so the node keeps its spot across a reboot without
// hearing the table again — the "field runs with no laptop" guarantee.
static void maybeAdoptPosition() {
  bool pending;
  float px, py;
  portENTER_CRITICAL(&g_sync_mux);
  pending = g_pos_pending;
  px = g_pos_pending_x;
  py = g_pos_pending_y;
  g_pos_pending = false;
  portEXIT_CRITICAL(&g_sync_mux);
  if (!pending || (px == g_id.x && py == g_id.y)) return;  // unchanged: no write
  g_id.x = px;
  g_id.y = py;
  identitySave();
  Serial.printf("[table] adopted position x=%.2f y=%.2f from conductor\n", px, py);
}

// ---- Daytime deep-sleep entry (Lever 2) ---------------------------------------
// The one and only way into deep sleep, and it arms the RTC wake timer
// atomically (esp_deep_sleep = enable timer + sleep in one call) — there is no
// code path that sleeps without a scheduled wake. LEDs are cleared first (they
// would otherwise latch the last frame all day); the RTC-memory flag makes the
// next timer wake boot straight into "day" for a quick re-sleep if it's still
// bright. Never returns.
static void duskEnterDeepSleep(uint64_t sleep_us, const PowerPolicy& sleep_policy) {
  Serial.printf(
      "[sleep] off-window confirmed (light=%u mV) — deep sleeping %llu min; "
      "power-cycle wakes it immediately\n",
      g_light_mv, (unsigned long long)(sleep_us / 60000000ULL));
  Serial.flush();
  strip.ClearTo(RgbwColor(0, 0, 0, 0));
  strip.Show();
  while (!strip.CanShow()) delayMicroseconds(50);
#if HEARTBEAT_LED
  digitalWrite(HEARTBEAT_LED_PIN, HEARTBEAT_ACTIVE_LOW ? HIGH : LOW);
#endif
  g_rtc_was_day = true;
  g_rtc_power_policy = sleep_policy;
  powerPolicySanitize(g_rtc_power_policy);
  powerPolicyAdvanceBySeconds(g_rtc_power_policy,
                              (uint32_t)(sleep_us / 1000000ULL));
  g_rtc_have_power_policy = true;
  esp_deep_sleep(sleep_us);
}

// ---- Diagnostics -------------------------------------------------------------
// One-line status per second so boards can be range-walked to find where sync
// drops.
static void printDiag() {
  if (isConductor()) {
    Serial.printf("[conductor] t=%lld us  seq=%lu  pat=%u  bri=%u\n",
                  (long long)now_us(), (unsigned long)g_tx_seq, g_beacon.pattern_id,
                  g_beacon.brightness);
    return;
  }
  SyncState s;
  portENTER_CRITICAL(&g_sync_mux);
  s = g_sync;
  portEXIT_CRITICAL(&g_sync_mux);

  int64_t t = now_us();
  bool stale = syncIsStale(s, t, BEACON_STALE_US);
  int64_t age = beaconAge(s, t);
  Serial.printf(
      "[performer] %s  offset=%lld us  last_beacon=%lld ms ago  rx=%lu  gaps=%lu  rej=%lu  seq=%lu\n",
      stale ? "FREE-RUN" : "LOCKED  ", (long long)s.offset_us,
      (long long)(age < 0 ? -1 : age / 1000), (unsigned long)s.beacons_rx,
      (unsigned long)s.seq_gaps, (unsigned long)s.offset_rejects,
      (unsigned long)s.last_seq);
  if (g_powersave) {
    // windows/missed_windows tell whether the listen window is reliably catching a
    // beacon — the main risk of the duty-cycle (HANDOFF gotcha #1). naps/slept are
    // Stage B: slept is measured, so slept≈0 with a climbing nap count would mean
    // esp_timer is NOT compensated across light sleep (the Stage-B hardware risk).
    Serial.printf("  [duty] radio=%s  windows=%lu  missed=%lu  [nap] n=%lu  slept=%.1fs\n",
                  g_radio_on ? "ON " : "off", (unsigned long)g_duty.windows,
                  (unsigned long)g_duty.missed_windows, (unsigned long)g_naps,
                  (double)g_napped_us / 1e6);
  }
  if (g_dusk_on) {
    // day=DAY means deep sleep is pending only the fail-awake gates (boot
    // hold-off / serial grace / wake-flag TTL) — the node will vanish soon.
    Serial.printf("  [dusk] light=%u mV  %s\n", g_light_mv,
                  g_dusk.day ? "DAY — sleep pending gates" : "night");
  }
}

// ---- Serial command interface ------------------------------------------------
// Flash identical firmware to every board, then provision each over serial:
//   info                 print role + identity (incl. MAC) + pattern state
//   roster               (conductor) list nodes that have registered (MAC/id/fw)
//   table                (conductor) print the authoritative MAC->(x,y) table
//   assign <mac> <x> <y> (conductor) set a node's position by MAC; saved+broadcast
//   forget <mac>         (conductor) drop a node from the table (node replacement)
//   role <conductor|performer>   set this node's role and save to NVS
//   id <n>               set this node's id and save to NVS
//   pos <x> <y>          set this node's own (x,y) coordinate and save to NVS
//   powersave <on|off>   (performer) radio duty-cycle on/off; saved to NVS ("ps").
//                        Toggle it to A/B the night draw on the power meter.
//   dusk <on|off>        (performer) daytime deep-sleep; saved to NVS ("dusk").
//                        DEFAULT OFF — enable only once the light sensor is
//                        wired (a floating GPIO34 must never sleep a node).
//   wake <on|off>        (conductor) FIELD_AWAKE override in every beacon:
//                        summons dusk-sleeping nodes at their next resample
//                        (<= 15 min) and holds the field awake for daytime
//                        tests. Sticky in NVS ("wake").
//   keepalive <on|off> [interval_ms] [pulse_ms] [brightness]
//                        conductor broadcasts a brief scheduled-off LED pulse
//                        to keep USB power banks awake; saved to NVS.
//   power                (INA228 nodes) print the local energy/charge totals
//   power reset          (INA228 nodes) zero the accumulators — run at the
//                        start of a night for a clean "Wh consumed" figure
// Pattern controls (only the conductor's take effect field-wide; it broadcasts):
//   pattern <n>          0 = uniform pulse, 1 = rainbow drift, 2 = sweep,
//                        3 = solid full-white (worst-case draw, for measuring),
//                        4 = glow (steady solid color; params[0]=hue deg,
//                            params[1]=saturation %),
//                        6 = firefly, 7 = ocean wave,
//                        8 = white (SK6812 white channel only)
//   bri <n>              brightness 0-255
//   param <i> <v>        params[i] (i=0..3): sweep period_ms / wavelength*100;
//                        glow hue(deg) / saturation(%)
static void printInfo() {
  char mac[18];
  BeaconMsg b;
  portENTER_CRITICAL(&g_sync_mux);  // recv cb overwrites g_beacon on a performer
  b = g_beacon;
  portEXIT_CRITICAL(&g_sync_mux);
  Serial.printf("role=%s  id=%u  mac=%s  x=%.2f  y=%.2f\n",
                isConductor() ? "CONDUCTOR" : "PERFORMER", g_id.id,
                macStr(g_mac, mac), g_id.x, g_id.y);
  Serial.printf("  firmware: v%s  proto=%u  build=%08lx%s\n", FIRMWARE_VERSION,
                PROTO_VERSION,
                (unsigned long)(uint32_t)FIRMWARE_BUILD_ID,
                FIRMWARE_BUILD_DIRTY ? " dirty" : "");
  Serial.printf("  pattern=%u  bri=%u  params=[%u %u %u %u]\n", b.pattern_id,
                b.brightness, b.params[0], b.params[1],
                b.params[2], b.params[3]);
  Serial.printf("  powersave=%s%s  dusk=%s%s\n", g_powersave ? "on" : "off",
                isConductor() ? " (conductor: radio always on)" : "",
                g_dusk_on ? "on" : "off",
                isConductor() ? " (conductor: never dusk-sleeps)" : "");
  PowerPolicy p = isConductor() ? powerPolicySnapshot(now_us()) : b.power;
  Serial.printf("  power policy: light-check=%us  deep-check=%umin  schedule=%s "
                "on=%02u:%02u off=%02u:%02u  now=%02u:%02u  epoch=%lu  leds=%s\n",
                p.light_sleep_check_s, p.deep_sleep_check_min,
                powerPolicyScheduleEnabled(p) ? "on" : "off",
                p.led_on_start_min / 60, p.led_on_start_min % 60,
                p.led_on_end_min / 60, p.led_on_end_min % 60,
                p.current_min / 60, p.current_min % 60,
                (unsigned long)p.current_epoch_s,
                powerPolicyLedsOn(p) ? "on" : "off");
  KeepAliveConfig ka = isConductor() ? g_keepalive : b.keepalive;
  keepAliveSanitize(ka);
  Serial.printf("  keepalive=%s  interval=%ums  pulse=%ums  bri=%u\n",
                keepAliveEnabled(ka) ? "on" : "off", ka.interval_ms,
                ka.pulse_ms, ka.brightness);
  if (isConductor())
    Serial.printf("  wake-override=%s (FIELD_AWAKE flag in beacons)\n",
                  g_wake_flag ? "ON" : "off");
  // Raw sensor readings — garbage until the divider/phototransistor are wired.
  uint32_t vbat_mv = analogReadMilliVolts(PIN_VBAT);
  Serial.printf("  sensors: light=%u mV  vbat=%.2f V (raw %lu mV; unwired = noise)  ina228=%s\n",
                g_light_mv, vbat_mv * VBAT_DIVIDER / 1000.0f,
                (unsigned long)vbat_mv, g_have_ina228 ? "yes" : "no");
}

// Conductor-only: print the roster of nodes that have registered. Snapshots the
// shared array under the spinlock, then prints outside it.
static void printRoster() {
  if (!isConductor()) {
    Serial.println("(roster lives on the conductor)");
    return;
  }
  Roster snap;
  portENTER_CRITICAL(&g_roster_mux);
  snap = g_roster;
  portEXIT_CRITICAL(&g_roster_mux);

  int64_t t = now_us();
  Serial.printf("roster: %u node(s)\n", snap.count);
  for (uint8_t i = 0; i < snap.count; i++) {
    char mac[18];
    Serial.printf("  [%u] %s  id=%u  v%s  fw=%u  build=%08lx%s  last_seen=%lld ms ago\n", i,
                  macStr(snap.entries[i].mac, mac), snap.entries[i].id,
                  snap.entries[i].version, snap.entries[i].fw,
                  (unsigned long)snap.entries[i].build,
                  snap.entries[i].dirty ? " dirty" : "",
                  (long long)((t - snap.entries[i].last_us) / 1000));
  }
}

// Conductor-only: print the authoritative layout table.
static void printTable() {
  if (!isConductor()) {
    Serial.println("(table lives on the conductor)");
    return;
  }
  Serial.printf("table: %u row(s)\n", g_table.count);
  for (uint8_t i = 0; i < g_table.count; i++) {
    char mac[18];
    Serial.printf("  [%u] %s  x=%.2f  y=%.2f\n", i,
                  macStr(g_table.entries[i].mac, mac), g_table.entries[i].x,
                  g_table.entries[i].y);
  }
}

static const char* patternName(uint16_t id) {
  switch (id) {
    case patterns::PULSE: return "Pulse";
    case patterns::PALETTE_DRIFT: return "Palette Drift";
    case patterns::SWEEP: return "Sweep";
    case patterns::SOLID: return "Solid";
    case patterns::GLOW: return "Glow";
    case patterns::FIREFLY: return "Firefly";
    case patterns::OCEAN_WAVE: return "Ocean Wave";
    case patterns::CALIBRATION: return "Calibration";
    case patterns::WHITE: return "White";
    default: return "Unknown";
  }
}

static void jsonOk(uint32_t id, const char* message) {
  Serial.printf("{\"id\":%lu,\"ok\":true,\"message\":\"%s\"}\n",
                (unsigned long)id, message);
}

static void jsonError(uint32_t id, const char* error) {
  Serial.printf("{\"id\":%lu,\"ok\":false,\"error\":\"%s\"}\n",
                (unsigned long)id, error);
}

static void saveBeaconSnapshot() {
  BeaconMsg snap;
  portENTER_CRITICAL(&g_sync_mux);
  snap = g_beacon;
  portEXIT_CRITICAL(&g_sync_mux);
  patternConfigSave(snap);
}

static void printPatternJson(const BeaconMsg& b) {
  Serial.printf("\"pattern\":{\"pattern\":\"%s\",\"brightness\":%u,"
                "\"params\":{\"p0\":%u,\"p1\":%u,\"p2\":%u,\"p3\":%u",
                patternName(b.pattern_id), b.brightness, b.params[0], b.params[1],
                b.params[2], b.params[3]);
  if (b.pattern_id == patterns::GLOW || b.pattern_id == patterns::PULSE) {
    Serial.printf(",\"hue\":%u,\"saturation\":%u", b.params[0],
                  b.params[1] ? b.params[1] : 100);
  } else {
    Serial.printf(",\"period\":%u", b.params[0]);
  }
  Serial.print("}}");
}

static void printPowerPolicyJson(const PowerPolicy& p) {
  Serial.printf("\"power\":{\"light_sleep_check_s\":%u,"
                "\"deep_sleep_check_min\":%u,"
                "\"led_on_start_min\":%u,\"led_on_end_min\":%u,"
                "\"current_min\":%u,\"current_epoch_s\":%lu,"
                "\"schedule_enabled\":%s,\"force_awake\":%s,"
                "\"force_sleep\":%s,"
                "\"leds_on\":%s}",
                p.light_sleep_check_s, p.deep_sleep_check_min,
                p.led_on_start_min, p.led_on_end_min, p.current_min,
                (unsigned long)p.current_epoch_s,
                powerPolicyScheduleEnabled(p) ? "true" : "false",
                powerPolicyForceAwake(p) ? "true" : "false",
                powerPolicyForceSleep(p) ? "true" : "false",
                powerPolicyLedsOn(p) ? "true" : "false");
}

static void printKeepAliveJson(const KeepAliveConfig& k) {
  KeepAliveConfig clean = k;
  keepAliveSanitize(clean);
  Serial.printf("\"keepalive\":{\"enabled\":%s,\"interval_ms\":%u,"
                "\"pulse_ms\":%u,\"brightness\":%u}",
                keepAliveEnabled(clean) ? "true" : "false",
                clean.interval_ms, clean.pulse_ms, clean.brightness);
}

static void printOtaStatusNodesJson(int64_t t) {
  portENTER_CRITICAL(&g_ota_status_mux);
  g_state_ota_status_snapshot = g_ota_status;
  portEXIT_CRITICAL(&g_ota_status_mux);
  Serial.print("[");
  for (uint8_t i = 0; i < g_state_ota_status_snapshot.count; i++) {
    const OtaNodeStatusEntry& e = g_state_ota_status_snapshot.entries[i];
    char mac[18];
    int64_t age_s = e.last_us > 0 ? (t - e.last_us) / 1000000 : -1;
    if (i) Serial.print(",");
    Serial.printf("{\"mac\":\"%s\",\"phase\":\"%s\",\"error\":\"%s\","
                  "\"offset\":%lu,\"crc32\":%lu,\"last_seen_s\":%lld}",
                  macStr(e.mac, mac), otaPhaseName(e.phase),
                  otaErrorName(e.error), (unsigned long)e.offset,
                  (unsigned long)e.crc32, (long long)age_s);
  }
  Serial.print("]");
}

static bool otaMaintenanceActive(int64_t t) {
  if (!g_ota_maintenance) return false;
  if (g_ota_maintenance_until_us > 0 && t >= g_ota_maintenance_until_us) {
    g_ota_maintenance = false;
    g_ota_maintenance_until_us = 0;
    return false;
  }
  return true;
}

static void printOtaJson(uint8_t expected, uint8_t placed_alive,
                         uint8_t firmware_matching, bool firmware_mixed,
                         int64_t t) {
  bool active = otaMaintenanceActive(t);
  uint8_t missing = expected > placed_alive ? expected - placed_alive : 0;
  bool ready = active && expected > 0 && missing == 0;
  long timeout_s = active && g_ota_maintenance_until_us > t
                       ? (long)((g_ota_maintenance_until_us - t) / 1000000LL)
                       : 0;
  Serial.printf("\"ota\":{\"mode\":\"%s\",\"enabled\":%s,\"ready\":%s,"
                "\"ready_count\":%u,\"expected\":%u,\"missing\":%u,"
                "\"firmware_consistent\":%s,\"timeout_s\":%ld,\"blocked\":[",
                active ? "maintenance" : "idle", active ? "true" : "false",
                ready ? "true" : "false", placed_alive, expected, missing,
                firmware_mixed ? "false" : "true", timeout_s);
  bool first = true;
  if (!active) {
    Serial.print("\"not in maintenance mode\"");
    first = false;
  }
  if (expected == 0) {
    if (!first) Serial.print(",");
    Serial.print("\"no placed lanterns\"");
    first = false;
  }
  if (missing > 0) {
    if (!first) Serial.print(",");
    Serial.print("\"missing placed lanterns\"");
    first = false;
  }
  if (firmware_mixed && !ready) {
    if (!first) Serial.print(",");
    Serial.print("\"firmware mismatch\"");
  }
  Serial.print("],\"nodes\":");
  printOtaStatusNodesJson(t);
  Serial.print("}");
}

static void otaWriteAbort() {
  if (g_ota_write_active) Update.abort();
  g_ota_write_active = false;
  g_ota_write_size = 0;
  g_ota_write_written = 0;
  g_ota_write_crc = 0;
  g_ota_write_expected_crc = 0;
  g_ota_finalize_pending = false;
}

static void otaSetLocalStatus(uint8_t phase, uint8_t error, uint32_t offset,
                              uint32_t crc32) {
  if (isConductor()) return;
  OtaStatusMsg msg = {{BEACON_MAGIC, PROTO_VERSION, MSG_OTA_STATUS},
                      {0}, phase, error, offset, crc32};
  memcpy(msg.mac, g_mac, 6);
  portENTER_CRITICAL(&g_ota_status_mux);
  g_ota_status_pending = msg;
  g_ota_status_pending_dirty = true;
  portEXIT_CRITICAL(&g_ota_status_mux);
}

static void otaStatusReport(bool keep_pending) {
  if (isConductor() || !g_ota_status_pending_dirty || !g_radio_on) return;

  OtaStatusMsg msg;
  portENTER_CRITICAL(&g_ota_status_mux);
  msg = g_ota_status_pending;
  if (!keep_pending) g_ota_status_pending_dirty = false;
  portEXIT_CRITICAL(&g_ota_status_mux);
  esp_now_send(BROADCAST_ADDR, (const uint8_t*)&msg, sizeof(msg));
}

static void maybeOtaStatusReport() {
  otaStatusReport(/*keep_pending*/ false);
}

static void otaRadioBegin(const OtaBeginMsg& msg) {
  if (msg.size == 0) return;
  otaWriteAbort();
  if (!Update.begin(msg.size, U_FLASH)) {
    otaSetLocalStatus(OTA_PHASE_ERROR, OTA_ERR_BEGIN_FAILED, 0, 0);
    return;
  }
  g_ota_write_active = true;
  g_ota_write_size = msg.size;
  g_ota_write_written = 0;
  g_ota_write_crc = 0;
  g_ota_write_expected_crc = msg.crc32;
  otaSetLocalStatus(OTA_PHASE_BEGIN, OTA_ERR_NONE, 0, 0);
}

static void otaRadioChunk(const OtaChunkMsg& msg) {
  if (!g_ota_write_active) return;
  switch (otaChunkDecision(g_ota_write_written, g_ota_write_size,
                           msg.offset, msg.n)) {
    case OTA_CHUNK_DUPLICATE:
      return;
    case OTA_CHUNK_OFFSET_MISMATCH:
      otaSetLocalStatus(OTA_PHASE_ERROR, OTA_ERR_OFFSET_MISMATCH,
                        g_ota_write_written, g_ota_write_crc);
      otaWriteAbort();
      return;
    case OTA_CHUNK_OVERFLOW:
      otaSetLocalStatus(OTA_PHASE_ERROR, OTA_ERR_OVERFLOW,
                        g_ota_write_written, g_ota_write_crc);
      otaWriteAbort();
      return;
    case OTA_CHUNK_ACCEPT:
      break;
  }
  size_t written = Update.write((uint8_t*)msg.data, msg.n);
  if (written != msg.n) {
    otaSetLocalStatus(OTA_PHASE_ERROR, OTA_ERR_WRITE_FAILED,
                      g_ota_write_written, g_ota_write_crc);
    otaWriteAbort();
    return;
  }
  g_ota_write_crc = otaCrc32Update(g_ota_write_crc, msg.data, msg.n);
  g_ota_write_written += msg.n;
  if (g_ota_write_written == msg.n ||
      g_ota_write_written == g_ota_write_size ||
      (g_ota_write_written % 4096) == 0) {
    otaSetLocalStatus(OTA_PHASE_WRITING, OTA_ERR_NONE,
                      g_ota_write_written, g_ota_write_crc);
  }
}

static void otaRadioEnd() {
  if (!g_ota_write_active) return;
  if (g_ota_write_written != g_ota_write_size) {
    otaSetLocalStatus(OTA_PHASE_ERROR, OTA_ERR_INCOMPLETE,
                      g_ota_write_written, g_ota_write_crc);
    otaWriteAbort();
    return;
  }
  if (g_ota_write_crc != g_ota_write_expected_crc) {
    otaSetLocalStatus(OTA_PHASE_ERROR, OTA_ERR_CRC_MISMATCH,
                      g_ota_write_written, g_ota_write_crc);
    otaWriteAbort();
    return;
  }
  g_ota_finalize_pending = true;
}

static void otaFinalizePending() {
  if (!g_ota_finalize_pending) return;
  g_ota_finalize_pending = false;
  if (!g_ota_write_active) return;
  if (!Update.end(true)) {
    otaSetLocalStatus(OTA_PHASE_ERROR, OTA_ERR_END_FAILED,
                      g_ota_write_written, g_ota_write_crc);
    otaWriteAbort();
    return;
  }
  g_ota_write_active = false;
  otaSetLocalStatus(OTA_PHASE_COMPLETE, OTA_ERR_NONE,
                    g_ota_write_written, g_ota_write_crc);
  g_ota_reboot_pending = true;
}

static void otaSendRepeated(const uint8_t* data, size_t len) {
  uint8_t accepted = 0;
  for (uint8_t attempt = 0;
       accepted < OTA_RADIO_SEND_COPIES &&
       attempt < OTA_RADIO_SEND_MAX_ATTEMPTS;
       attempt++) {
    if (esp_now_send(BROADCAST_ADDR, data, len) == ESP_OK) accepted++;
    delay(OTA_RADIO_SEND_DELAY_MS);
  }
}

static void otaBroadcastBegin(uint32_t size, uint32_t crc32) {
  if (!isConductor()) return;
  OtaBeginMsg msg = {{BEACON_MAGIC, PROTO_VERSION, MSG_OTA_BEGIN}, size, crc32};
  otaSendRepeated((const uint8_t*)&msg, sizeof(msg));
}

static void otaBroadcastChunk(uint32_t offset, const uint8_t* data, uint8_t len) {
  if (!isConductor() || len == 0 || len > OTA_SERIAL_CHUNK_MAX) return;
  OtaChunkMsg msg = {{BEACON_MAGIC, PROTO_VERSION, MSG_OTA_CHUNK}, offset, len, {0}};
  memcpy(msg.data, data, len);
  otaSendRepeated((const uint8_t*)&msg, offsetof(OtaChunkMsg, data) + len);
}

static void otaBroadcastEnd() {
  if (!isConductor()) return;
  OtaEndMsg msg = {{BEACON_MAGIC, PROTO_VERSION, MSG_OTA_END}};
  otaSendRepeated((const uint8_t*)&msg, sizeof(msg));
}

static bool otaExpectedPerformersComplete(uint32_t size, uint32_t crc32) {
  OtaStatusTable status;
  portENTER_CRITICAL(&g_ota_status_mux);
  status = g_ota_status;
  portEXIT_CRITICAL(&g_ota_status_mux);

  int64_t t = now_us();
  for (uint8_t i = 0; i < g_table.count; i++) {
    const uint8_t* mac = g_table.entries[i].mac;
    if (memcmp(mac, g_mac, 6) == 0) continue;
    if (!otaStatusCompleteForMac(status, mac, size, crc32, t,
                                 OTA_STATUS_FRESH_US)) {
      return false;
    }
  }
  return true;
}

static bool otaWaitForExpectedPerformers(uint32_t size, uint32_t crc32,
                                         uint32_t wait_ms) {
  uint32_t start_ms = millis();
  while ((uint32_t)(millis() - start_ms) < wait_ms) {
    if (otaExpectedPerformersComplete(size, crc32)) return true;
    delay(100);
  }
  return otaExpectedPerformersComplete(size, crc32);
}

static void handleOtaBegin(const SerialJsonCommand& cmd) {
  if (!otaMaintenanceActive(now_us())) {
    jsonError(cmd.id, "ota maintenance mode is not active");
    return;
  }
  if (cmd.ota_size == 0) {
    jsonError(cmd.id, "bad ota size");
    return;
  }
  otaWriteAbort();
  if (!Update.begin(cmd.ota_size, U_FLASH)) {
    jsonError(cmd.id, "ota begin failed");
    return;
  }
  g_ota_write_active = true;
  g_ota_write_size = cmd.ota_size;
  g_ota_write_written = 0;
  g_ota_write_crc = 0;
  g_ota_write_expected_crc = cmd.ota_crc32;
  otaBroadcastBegin(cmd.ota_size, cmd.ota_crc32);
  jsonOk(cmd.id, "ota write started");
}

static void handleOtaChunk(const SerialJsonCommand& cmd) {
  if (!g_ota_write_active) {
    jsonError(cmd.id, "ota write is not active");
    return;
  }
  uint8_t bytes[OTA_SERIAL_CHUNK_MAX];
  size_t len = 0;
  if (!otaHexDecode(cmd.ota_data_hex, bytes, sizeof(bytes), len) || len == 0) {
    jsonError(cmd.id, "bad ota chunk data");
    return;
  }
  if (cmd.ota_offset < g_ota_write_written &&
      cmd.ota_offset + len <= g_ota_write_written) {
    jsonOk(cmd.id, "ota chunk already written");
    return;
  }
  if (cmd.ota_offset != g_ota_write_written) {
    jsonError(cmd.id, "ota chunk offset mismatch");
    return;
  }
  if (len != otaExpectedChunkLen(g_ota_write_size, cmd.ota_offset)) {
    jsonError(cmd.id, "ota chunk length mismatch");
    return;
  }
  if (g_ota_write_written + len > g_ota_write_size) {
    jsonError(cmd.id, "ota chunk exceeds image size");
    return;
  }
  otaBroadcastChunk(cmd.ota_offset, bytes, (uint8_t)len);
  size_t written = Update.write(bytes, len);
  if (written != len) {
    otaWriteAbort();
    jsonError(cmd.id, "ota flash write failed");
    return;
  }
  g_ota_write_crc = otaCrc32Update(g_ota_write_crc, bytes, len);
  g_ota_write_written += (uint32_t)len;
  jsonOk(cmd.id, "ota chunk written");
}

static void handleOtaEnd(const SerialJsonCommand& cmd) {
  if (!g_ota_write_active) {
    jsonError(cmd.id, "ota write is not active");
    return;
  }
  if (g_ota_write_written != g_ota_write_size) {
    otaWriteAbort();
    jsonError(cmd.id, "ota image incomplete");
    return;
  }
  if (g_ota_write_crc != g_ota_write_expected_crc) {
    otaWriteAbort();
    jsonError(cmd.id, "ota crc mismatch");
    return;
  }
  otaBroadcastEnd();
  if (!otaWaitForExpectedPerformers(g_ota_write_size, g_ota_write_crc,
                                    OTA_FINALIZE_WAIT_MS)) {
    Serial.printf("{\"id\":%lu,\"ok\":false,"
                  "\"error\":\"ota performers did not complete\","
                  "\"nodes\":", (unsigned long)cmd.id);
    printOtaStatusNodesJson(now_us());
    Serial.print("}\n");
    return;
  }
  if (!Update.end(true)) {
    otaWriteAbort();
    jsonError(cmd.id, "ota finalize failed");
    return;
  }
  g_ota_write_active = false;
  Serial.printf("{\"id\":%lu,\"ok\":true,\"message\":\"ota install complete; rebooting\","
                "\"nodes\":", (unsigned long)cmd.id);
  printOtaStatusNodesJson(now_us());
  Serial.print("}\n");
  Serial.flush();
  delay(100);
  ESP.restart();
}

static void handleOtaProgress(const SerialJsonCommand& cmd) {
  Serial.printf("{\"id\":%lu,\"ok\":true,\"active\":%s,"
                "\"size\":%lu,\"written\":%lu,\"crc32\":%lu,\"nodes\":",
                (unsigned long)cmd.id,
                g_ota_write_active ? "true" : "false",
                (unsigned long)g_ota_write_size,
                (unsigned long)g_ota_write_written,
                (unsigned long)g_ota_write_crc);
  printOtaStatusNodesJson(now_us());
  Serial.print("}\n");
}

static void printLanternJson(const uint8_t mac_bytes[6], const char* label,
                             uint16_t node_id,
                             const char* status, int64_t last_seen_s, float x,
                             float y, bool has_position, const char* attention,
                             const FirmwareVersion* firmware,
                             const PowerEntry* power, int64_t t) {
  char mac[18];
  Serial.printf("{\"mac\":\"%s\",\"label\":\"%s\",\"node_id\":%u,\"status\":\"%s\",",
                macStr(mac_bytes, mac), label, node_id, status);
  if (last_seen_s >= 0) {
    Serial.printf("\"last_seen_s\":%lld,\"last_seen_label\":\"%llds ago\",",
                  (long long)last_seen_s, (long long)last_seen_s);
  } else {
    Serial.print("\"last_seen_s\":999999,\"last_seen_label\":\"not seen\",");
  }
  if (has_position) {
    Serial.printf("\"x\":%.4f,\"y\":%.4f,\"position\":\"Set\",", x, y);
  } else {
    Serial.print("\"x\":null,\"y\":null,\"position\":\"Missing\",");
  }
  Serial.printf("\"attention\":\"%s\",", attention);
  if (firmware) {
    Serial.printf("\"firmware\":{\"version\":\"%s\",\"proto\":%u,\"build_id\":%lu,"
                  "\"build_label\":\"%08lx\",\"dirty\":%s},",
                  firmware->version, firmware->proto, (unsigned long)firmware->build_id,
                  (unsigned long)firmware->build_id,
                  firmware->dirty ? "true" : "false");
  } else {
    Serial.print("\"firmware\":null,");
  }
  if (power) {
    int64_t power_age_s = power->last_us > 0 ? (t - power->last_us) / 1000000 : -1;
    const PowerSample& s = power->sample;
    Serial.printf("\"power\":{\"wh\":%.3f,\"avg_w\":%.3f,\"mah\":%.1f,"
                  "\"bus_v\":%.2f,\"current_ma\":%.1f,\"elapsed_s\":%lu,"
                  "\"plausible\":%s,",
                  powerWh(s.energy_j), powerAvgW(s.energy_j, s.elapsed_s),
                  powerMah(s.charge_c), s.bus_v, s.current_ma,
                  (unsigned long)s.elapsed_s, powerPlausible(s) ? "true" : "false");
    if (power_age_s >= 0) {
      Serial.printf("\"last_report_s\":%lld,\"last_report_label\":\"%llds ago\"}}",
                    (long long)power_age_s, (long long)power_age_s);
    } else {
      Serial.print("\"last_report_s\":999999,\"last_report_label\":\"not seen\"}}");
    }
  } else {
    Serial.print("\"power\":{\"wh\":null,\"avg_w\":null,"
                 "\"mah\":null,\"bus_v\":null,\"current_ma\":null,"
                 "\"elapsed_s\":null,\"plausible\":null,"
                 "\"last_report_s\":null,\"last_report_label\":null}}");
  }
}

static void printMachineState(uint32_t id) {
  BeaconMsg b;
  bool locked = false;
  portENTER_CRITICAL(&g_roster_mux);
  g_state_roster_snapshot = g_roster;
  portEXIT_CRITICAL(&g_roster_mux);
  portENTER_CRITICAL(&g_sync_mux);
  b = g_beacon;
  locked = g_sync.locked;
  portEXIT_CRITICAL(&g_sync_mux);

  int64_t t = now_us();
  FirmwareVersion conductor_fw = currentFirmwareVersion(PROTO_VERSION);
  uint8_t attention = 0;
  uint8_t placed_alive = 0;
  uint8_t firmware_seen = 0;
  uint8_t firmware_matching = 0;
  bool firmware_mixed = false;
  for (uint8_t i = 0; i < g_table.count; i++) {
    int r = rosterFind(g_state_roster_snapshot, g_table.entries[i].mac);
    if (r < 0) {
      attention++;
    } else {
      placed_alive++;
      FirmwareVersion fw = rosterEntryFirmware(g_state_roster_snapshot.entries[r]);
      firmware_seen++;
      if (firmwareSame(conductor_fw, fw)) firmware_matching++;
      else {
        firmware_mixed = true;
        attention++;
      }
    }
  }
  for (uint8_t i = 0; i < g_state_roster_snapshot.count; i++) {
    FirmwareVersion fw = rosterEntryFirmware(g_state_roster_snapshot.entries[i]);
    if (tableFind(g_table, g_state_roster_snapshot.entries[i].mac) < 0) attention++;
    if (!firmwareSame(conductor_fw, fw)) firmware_mixed = true;
  }

  Serial.printf("{\"id\":%lu,\"ok\":true,\"state\":{", (unsigned long)id);
  PowerPolicy policy = isConductor() ? powerPolicySnapshot(t) : b.power;
  Serial.printf("\"conductor\":{\"connected\":true,\"uptime_s\":%.1f,"
                "\"seq\":%lu,\"wake\":%s,\"sync\":\"%s\","
                "\"firmware\":{\"version\":\"%s\",\"proto\":%u,\"build_id\":%lu,"
                "\"build_label\":\"%08lx\",\"dirty\":%s}},",
                millis() / 1000.0f, (unsigned long)g_tx_seq,
                g_wake_flag ? "true" : "false",
                isConductor() ? "locked" : (locked ? "locked" : "free-run"),
                conductor_fw.version, conductor_fw.proto, (unsigned long)conductor_fw.build_id,
                (unsigned long)conductor_fw.build_id,
                conductor_fw.dirty ? "true" : "false");
  Serial.printf("\"summary\":{\"alive\":%u,\"total\":%u,\"attention\":%u,"
                "\"table_rows\":%u,\"firmware\":{\"consistent\":%s,"
                "\"matching\":%u,\"seen\":%u,\"expected\":%u,"
                "\"version\":\"%s\",\"build_label\":\"%08lx\",\"dirty\":%s}},",
                placed_alive, g_table.count, attention, g_table.count,
                firmware_mixed ? "false" : "true", firmware_matching,
                firmware_seen, g_table.count,
                conductor_fw.version,
                (unsigned long)conductor_fw.build_id,
                conductor_fw.dirty ? "true" : "false");
  printPatternJson(b);
  Serial.print(",");
  printPowerPolicyJson(policy);
  Serial.print(",");
  printKeepAliveJson(isConductor() ? g_keepalive : b.keepalive);
  Serial.print(",");
  printOtaJson(g_table.count, placed_alive, firmware_matching, firmware_mixed, t);
  Serial.print(",\"lanterns\":[");
  bool first = true;
  for (uint8_t i = 0; i < g_table.count; i++) {
    const TableEntry& row = g_table.entries[i];
    int r = rosterFind(g_state_roster_snapshot, row.mac);
    char label[16];
    if (r >= 0 && g_state_roster_snapshot.entries[r].id) {
      snprintf(label, sizeof(label), "#%u", g_state_roster_snapshot.entries[r].id);
    }
    else snprintf(label, sizeof(label), "#?");
    if (!first) Serial.print(",");
    first = false;
    int64_t age_s = r >= 0 ? (t - g_state_roster_snapshot.entries[r].last_us) / 1000000 : -1;
    FirmwareVersion fw;
    FirmwareVersion* fw_ptr = nullptr;
    const char* attention_text = r >= 0 ? "None" : "Not seen";
    if (r >= 0) {
      fw = rosterEntryFirmware(g_state_roster_snapshot.entries[r]);
      fw_ptr = &fw;
      if (!firmwareSame(conductor_fw, fw)) attention_text = "Firmware mismatch";
    }
    int p = powerTableFind(g_power_table, row.mac);
    printLanternJson(row.mac, label, r >= 0 ? g_state_roster_snapshot.entries[r].id : 0,
                     r >= 0 ? "alive" : "missing", age_s, row.x,
                     row.y, true, attention_text, fw_ptr,
                     p >= 0 ? &g_power_table.entries[p] : nullptr, t);
  }
  for (uint8_t i = 0; i < g_state_roster_snapshot.count; i++) {
    const RosterEntry& row = g_state_roster_snapshot.entries[i];
    if (tableFind(g_table, row.mac) >= 0) continue;
    char label[16];
    if (row.id) snprintf(label, sizeof(label), "#%u", row.id);
    else snprintf(label, sizeof(label), "Unknown");
    if (!first) Serial.print(",");
    first = false;
    int64_t age_s = (t - row.last_us) / 1000000;
    FirmwareVersion fw = rosterEntryFirmware(row);
    const char* attention_text = firmwareSame(conductor_fw, fw) ? "Needs position"
                                                                : "Firmware mismatch";
    int p = powerTableFind(g_power_table, row.mac);
    printLanternJson(row.mac, label, row.id, "alive", age_s, 0.0f, 0.0f, false,
                     attention_text, &fw,
                     p >= 0 ? &g_power_table.entries[p] : nullptr, t);
  }
  Serial.print("],\"events\":[]}}\n");
}

static void handleMachineCommand(const SerialJsonCommand& cmd) {
  if (cmd.kind == SJ_STATE) {
    printMachineState(cmd.id);
  } else if (cmd.kind == SJ_IDENTIFY) {
    jsonOk(cmd.id, "identify acknowledged");
  } else if (cmd.kind == SJ_ASSIGN) {
    if (!isConductor()) {
      jsonError(cmd.id, "assign is conductor-only");
    } else if (tableSet(g_table, cmd.mac, cmd.x, cmd.y)) {
      tableSave();
      broadcastTable();
      jsonOk(cmd.id, "assigned");
    } else {
      jsonError(cmd.id, "table full");
    }
  } else if (cmd.kind == SJ_FORGET) {
    if (!isConductor()) {
      jsonError(cmd.id, "forget is conductor-only");
    } else if (tableRemove(g_table, cmd.mac)) {
      tableSave();
      jsonOk(cmd.id, "forgot");
    } else {
      jsonError(cmd.id, "unknown lantern");
    }
  } else if (cmd.kind == SJ_REPLACE) {
    if (!isConductor()) {
      jsonError(cmd.id, "replace is conductor-only");
      return;
    }
    float x = 0.0f, y = 0.0f;
    if (!tableLookup(g_table, cmd.old_mac, x, y)) {
      jsonError(cmd.id, "old lantern has no position");
    } else if (tableFind(g_table, cmd.new_mac) >= 0) {
      jsonError(cmd.id, "replacement lantern already has a position");
    } else if (!tableSet(g_table, cmd.new_mac, x, y)) {
      jsonError(cmd.id, "table full");
    } else {
      tableRemove(g_table, cmd.old_mac);
      tableSave();
      broadcastTable();
      jsonOk(cmd.id, "replaced");
    }
  } else if (cmd.kind == SJ_PATTERN) {
    portENTER_CRITICAL(&g_sync_mux);
    g_beacon.pattern_id = cmd.pattern_id;
    if (cmd.has_brightness) {
      g_beacon.brightness =
          cmd.brightness > MAX_BRIGHTNESS ? MAX_BRIGHTNESS : cmd.brightness;
    }
    for (uint8_t i = 0; i < 4; i++)
      if (cmd.has_params[i]) g_beacon.params[i] = cmd.params[i];
    portEXIT_CRITICAL(&g_sync_mux);
    saveBeaconSnapshot();
    jsonOk(cmd.id, "pattern changed");
  } else if (cmd.kind == SJ_BLACKOUT) {
    portENTER_CRITICAL(&g_sync_mux);
    g_beacon.brightness = 0;
    portEXIT_CRITICAL(&g_sync_mux);
    saveBeaconSnapshot();
    jsonOk(cmd.id, "blackout broadcast");
  } else if (cmd.kind == SJ_POWER_POLICY) {
    if (!isConductor()) {
      jsonError(cmd.id, "power policy is conductor-only");
    } else {
      powerPolicyApplyCommand(cmd);
      jsonOk(cmd.id, "power policy changed");
    }
  } else if (cmd.kind == SJ_KEEPALIVE) {
    if (!isConductor()) {
      jsonError(cmd.id, "keepalive is conductor-only");
    } else {
      keepAliveApplyCommand(cmd);
      jsonOk(cmd.id, "keepalive changed");
    }
  } else if (cmd.kind == SJ_OTA_MODE) {
    if (!isConductor()) {
      jsonError(cmd.id, "ota mode is conductor-only");
    } else {
      if (!cmd.ota_enabled) otaWriteAbort();
      g_ota_maintenance = cmd.ota_enabled;
      g_ota_maintenance_until_us = cmd.ota_enabled ? now_us() + OTA_WINDOW_US : 0;
      portENTER_CRITICAL(&g_ota_status_mux);
      otaStatusInit(g_ota_status);
      portEXIT_CRITICAL(&g_ota_status_mux);
      jsonOk(cmd.id, cmd.ota_enabled ? "ota maintenance mode started"
                                      : "ota maintenance mode ended");
    }
  } else if (cmd.kind == SJ_OTA_BEGIN) {
    handleOtaBegin(cmd);
  } else if (cmd.kind == SJ_OTA_CHUNK) {
    handleOtaChunk(cmd);
  } else if (cmd.kind == SJ_OTA_END) {
    handleOtaEnd(cmd);
  } else if (cmd.kind == SJ_OTA_PROGRESS) {
    handleOtaProgress(cmd);
  } else {
    jsonError(cmd.id, "unknown cmd");
  }
}

static void handleCommand(char* line) {
  if (serialJsonLooksLike(line)) {
    SerialJsonCommand cmd;
    const char* error = nullptr;
    if (serialJsonParse(line, cmd, error)) handleMachineCommand(cmd);
    else jsonError(cmd.id, error ? error : "bad json");
    return;
  }

  char* cmd = strtok(line, " \t");
  if (!cmd) return;

  if (!strcmp(cmd, "info")) {
    printInfo();
  } else if (!strcmp(cmd, "roster")) {
    printRoster();
  } else if (!strcmp(cmd, "table")) {
    printTable();
  } else if (!strcmp(cmd, "assign")) {
    char* am = strtok(nullptr, " \t");
    char* ax = strtok(nullptr, " \t");
    char* ay = strtok(nullptr, " \t");
    uint8_t mac[6];
    if (!isConductor()) {
      Serial.println("? assign is conductor-only");
    } else if (am && ax && ay && parseMac(am, mac)) {
      if (tableSet(g_table, mac, atof(ax), atof(ay))) {
        tableSave();
        broadcastTable();  // push the change immediately, then on the usual cadence
        printTable();
      } else {
        Serial.println("? table full");
      }
    } else {
      Serial.println("? assign <mac> <x> <y>");
    }
  } else if (!strcmp(cmd, "forget")) {
    char* am = strtok(nullptr, " \t");
    uint8_t mac[6];
    if (!isConductor()) {
      Serial.println("? forget is conductor-only");
    } else if (am && parseMac(am, mac)) {
      if (tableRemove(g_table, mac)) { tableSave(); printTable(); }
      else Serial.println("? no such mac in table");
    } else {
      Serial.println("? forget <mac>");
    }
  } else if (!strcmp(cmd, "role")) {
    char* a = strtok(nullptr, " \t");
    if (a) {
      if (!strcmp(a, "conductor") || !strcmp(a, "1")) g_role = ROLE_CONDUCTOR;
      else if (!strcmp(a, "performer") || !strcmp(a, "0")) g_role = ROLE_PERFORMER;
      else { Serial.println("? role conductor|performer"); return; }
      roleSave();
      // Reconcile radio + duty state with the new role: a conductor must have
      // the radio up to beacon, and a (re-)performer must not resume a stale
      // duty schedule (frozen change_at_us => spurious instant sleep). Bringing
      // the radio up and re-initing the duty machine covers both directions;
      // dutyInit assumes a powered radio, so order matters. The table schedule
      // resets too: a (re-)promoted conductor advertises immediately instead
      // of resuming a schedule frozen up to 60 s in the future.
      if (!g_radio_on) radioWake();
      dutyInit(g_duty, currentDutyConfig(g_power_policy), now_us());
      g_next_table_us = 0;
      printInfo();
    }
  } else if (!strcmp(cmd, "id")) {
    char* a = strtok(nullptr, " \t");
    if (a) { g_id.id = (uint16_t)atoi(a); identitySave(); printInfo(); }
  } else if (!strcmp(cmd, "pos")) {
    char* ax = strtok(nullptr, " \t");
    char* ay = strtok(nullptr, " \t");
    if (ax && ay) { g_id.x = atof(ax); g_id.y = atof(ay); identitySave(); printInfo(); }
  } else if (!strcmp(cmd, "pattern")) {
    char* a = strtok(nullptr, " \t");
    if (a) {
      // g_beacon is overwritten whole by the recv callback on a performer, so
      // every read-modify-write goes under g_sync_mux; the NVS save works from
      // a snapshot taken inside the same critical section.
      uint16_t v = (uint16_t)atoi(a);
      portENTER_CRITICAL(&g_sync_mux);
      g_beacon.pattern_id = v;
      BeaconMsg snap = g_beacon;
      portEXIT_CRITICAL(&g_sync_mux);
      patternConfigSave(snap);
      printInfo();
    }
  } else if (!strcmp(cmd, "bri")) {
    char* a = strtok(nullptr, " \t");
    if (a) {
      int v = atoi(a);
      if (v < 0) v = 0;
      if (v > MAX_BRIGHTNESS) v = MAX_BRIGHTNESS;  // never store above the cap
      portENTER_CRITICAL(&g_sync_mux);
      g_beacon.brightness = (uint8_t)v;
      BeaconMsg snap = g_beacon;
      portEXIT_CRITICAL(&g_sync_mux);
      patternConfigSave(snap);
      printInfo();
    }
  } else if (!strcmp(cmd, "param")) {
    char* ai = strtok(nullptr, " \t");
    char* av = strtok(nullptr, " \t");
    if (ai && av) {
      int i = atoi(ai);
      if (i >= 0 && i < 4) {
        uint16_t v = (uint16_t)atoi(av);
        portENTER_CRITICAL(&g_sync_mux);
        g_beacon.params[i] = v;
        BeaconMsg snap = g_beacon;
        portEXIT_CRITICAL(&g_sync_mux);
        patternConfigSave(snap);
        printInfo();
      }
    }
  } else if (!strcmp(cmd, "dusk")) {
    char* a = strtok(nullptr, " \t");
    if (a && (!strcmp(a, "on") || !strcmp(a, "1"))) {
      g_dusk_on = true;
      duskInit(g_dusk, /*start_day*/ false, now_us());  // fresh detector, night
      duskSave();
      printInfo();
    } else if (a && (!strcmp(a, "off") || !strcmp(a, "0"))) {
      g_dusk_on = false;
      duskSave();
      printInfo();
    } else {
      Serial.println("? dusk on|off");
    }
  } else if (!strcmp(cmd, "wake")) {
    char* a = strtok(nullptr, " \t");
    if (!isConductor()) {
      Serial.println("? wake is conductor-only (sets FIELD_AWAKE in beacons)");
    } else if (a && (!strcmp(a, "on") || !strcmp(a, "1"))) {
      g_wake_flag = true;
      g_power_policy.flags |= POWER_FLAG_FORCE_AWAKE;
      g_power_policy.flags &= ~POWER_FLAG_FORCE_SLEEP;
      powerPolicySave();
      Serial.println("[wake] FIELD_AWAKE on — dusk-sleeping nodes join at their "
                     "next resample (<= 15 min)");
      printInfo();
    } else if (a && (!strcmp(a, "off") || !strcmp(a, "0"))) {
      g_wake_flag = false;
      g_power_policy.flags &= ~POWER_FLAG_FORCE_AWAKE;
      powerPolicySave();
      Serial.println("[wake] FIELD_AWAKE off — dusk logic resumes field-wide");
      printInfo();
    } else {
      Serial.println("? wake on|off");
    }
  } else if (!strcmp(cmd, "sleep")) {
    char* a = strtok(nullptr, " \t");
    if (!isConductor()) {
      Serial.println("? sleep is conductor-only (forces field deep-sleep)");
    } else if (a && (!strcmp(a, "on") || !strcmp(a, "1"))) {
      g_wake_flag = false;
      g_power_policy.flags &= ~POWER_FLAG_FORCE_AWAKE;
      g_power_policy.flags |= POWER_FLAG_FORCE_SLEEP;
      powerPolicySave();
      Serial.println("[sleep] field sleep override on");
      printInfo();
    } else if (a && (!strcmp(a, "off") || !strcmp(a, "0"))) {
      g_power_policy.flags &= ~POWER_FLAG_FORCE_SLEEP;
      powerPolicySave();
      Serial.println("[sleep] field sleep override off — schedule resumes");
      printInfo();
    } else {
      Serial.println("? sleep on|off");
    }
  } else if (!strcmp(cmd, "power")) {
    char* a = strtok(nullptr, " \t");
    if (!g_have_ina228) {
      Serial.println("(no INA228 detected on this node — telemetry lives on the "
                     "1-2 instrumented reference nodes)");
    } else if (a && !strcmp(a, "reset")) {
      g_ina228.resetAccumulators();
      g_power_reset_us = now_us();
      Serial.println("[power] accumulators zeroed — clean measurement window "
                     "starts now (run at dusk for a per-night Wh figure)");
    } else {
      PowerSample s = readPowerSample(now_us());
      printPowerSample(g_mac, s);
    }
  } else if (!strcmp(cmd, "keepalive")) {
    char* a = strtok(nullptr, " \t");
    if (!isConductor()) {
      Serial.println("? keepalive is conductor-only");
    } else if (a && (!strcmp(a, "on") || !strcmp(a, "1") ||
                     !strcmp(a, "off") || !strcmp(a, "0"))) {
      if (!strcmp(a, "on") || !strcmp(a, "1")) g_keepalive.flags |= KEEPALIVE_FLAG_ENABLED;
      else g_keepalive.flags &= ~KEEPALIVE_FLAG_ENABLED;
      char* interval = strtok(nullptr, " \t");
      char* pulse = strtok(nullptr, " \t");
      char* brightness = strtok(nullptr, " \t");
      if (interval) g_keepalive.interval_ms = (uint16_t)atoi(interval);
      if (pulse) g_keepalive.pulse_ms = (uint16_t)atoi(pulse);
      if (brightness) g_keepalive.brightness = (uint8_t)atoi(brightness);
      keepAliveSanitize(g_keepalive);
      keepAliveSave();
      portENTER_CRITICAL(&g_sync_mux);
      g_beacon.keepalive = g_keepalive;
      portEXIT_CRITICAL(&g_sync_mux);
      printInfo();
    } else {
      Serial.println("? keepalive on|off [interval_ms] [pulse_ms] [brightness]");
    }
  } else if (!strcmp(cmd, "powersave") || !strcmp(cmd, "ps")) {
    char* a = strtok(nullptr, " \t");
    if (a && (!strcmp(a, "on") || !strcmp(a, "1"))) {
      g_powersave = true;
      // dutyInit assumes the radio is powered (it starts in a listen window).
      // If the duty cycle currently has the radio physically off — ~87% of the
      // time when powersave was already on — a re-issued `powersave on` would
      // otherwise strand the node in a phantom window the radio can never
      // catch a beacon in, leaving it deaf until reboot.
      if (!g_radio_on) radioWake();
      dutyInit(g_duty, currentDutyConfig(g_power_policy), now_us());  // restart from a fresh listen window
      powersaveSave();
      printInfo();
    } else if (a && (!strcmp(a, "off") || !strcmp(a, "0"))) {
      g_powersave = false;
      if (!g_radio_on) radioWake();  // leave the radio powered when disabling
      powersaveSave();
      printInfo();
    } else {
      Serial.println("? powersave on|off");
    }
  } else {
    Serial.printf("? unknown command: %s\n", cmd);
  }
}

// Accumulate a newline-terminated command from serial without blocking.
static void pollSerialCommands() {
  static char buf[512];
  static uint16_t len = 0;
  if (Serial.available()) g_last_serial_us = now_us();  // hold Stage-B naps off
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (len) { buf[len] = '\0'; handleCommand(buf); len = 0; }
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    }
  }
}

// ---- Arduino entry points ----------------------------------------------------
void setup() {
  setCpuFrequencyMhz(CPU_FREQ_MHZ);
  Serial.begin(115200);
  delay(200);
  Serial.printf("\nDo Baskets Dream — channel %u\n", WIFI_CHANNEL);
  bool timer_wake = (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER);

  configLoad();
  g_policy_clock_set_us = now_us();
  patternConfigLoad();
  if (timer_wake && g_rtc_have_power_policy) {
    g_power_policy = g_rtc_power_policy;
    powerPolicySanitize(g_power_policy);
    g_policy_base_min = g_power_policy.current_min;
    g_policy_base_epoch_s = g_power_policy.current_epoch_s;
    g_policy_clock_set_us = now_us();
    g_beacon.power = g_power_policy;
  } else if (!timer_wake) {
    g_rtc_have_power_policy = false;
  }
  esp_read_mac(g_mac, ESP_MAC_WIFI_STA);  // stable identity, read from efuse
  rosterInit(g_roster);
  powerTableInit(g_power_table);
  otaStatusInit(g_ota_status);
  tableLoad();
  if (!identityProvisioned(g_id))
    Serial.println("  (unprovisioned — set 'role …', 'id <n>', 'pos <x> <y>')");
  printInfo();

  syncInit(g_sync);

#if HEARTBEAT_LED
  pinMode(HEARTBEAT_LED_PIN, OUTPUT);
#endif

  strip.Begin();
  strip.Show();  // clear

  radioBegin();
  dutyInit(g_duty, currentDutyConfig(g_power_policy), now_us());  // performer radio duty-cycle (no-op for conductor)

  // Stage B naps: typing on serial wakes a sleeping node (threshold = a few RX
  // edges, so line noise doesn't), and the wake handler below then holds naps
  // off for the grace window.
  uart_set_wakeup_threshold(UART_NUM_0, 3);
  esp_sleep_enable_uart_wakeup(UART_NUM_0);

  // Lever 2 boot classification (bootplan.h, host-tested): timer wake = dusk
  // resample rendezvous (start in "day", short min-awake, serial grace
  // pre-expired); anything else = a human (start awake in "night", long
  // hold-off, full provisioning grace — the power-cycle-always-wakes
  // guarantee). This is pure glue; the reasoning lives in the header.
  int64_t boot = now_us();
  static const BootPlanConfig BOOT_CFG = {DUSK_MIN_AWAKE_TIMER_US,
                                          DUSK_MIN_AWAKE_COLD_US,
                                          DUSK_SERIAL_GRACE_US,
                                          SERIAL_NAP_GRACE_US};
  BootPlan plan = bootClassify(timer_wake, g_rtc_was_day, boot, BOOT_CFG);
  duskInit(g_dusk, plan.dusk_start_day, boot);
  g_dusk_earliest_us = plan.dusk_earliest_us;
  g_rtc_was_day = plan.rtc_day_flag;
  g_last_serial_us = plan.serial_seed_us;
  if (timer_wake)
    Serial.println("[dusk] timer wake — re-sampling daylight + listening for FIELD_AWAKE");
  analogSetPinAttenuation(PIN_LDR, ADC_11db);   // full ~0-3.1V range
  analogSetPinAttenuation(PIN_VBAT, ADC_11db);

  // INA228 probe (ARCHITECTURE §4.2): one image everywhere — a node without the
  // chip fails the probe in ~ms and runs without telemetry, silently.
  // skipReset=true is load-bearing: the chip stays battery-powered across an
  // ESP32 reset, and the default begin() would hardware-reset it — wiping the
  // night's accumulated Wh the moment a serial monitor's DTR auto-reset hits.
  // Zeroing is only ever explicit (`power reset`). Continuous conversion mode
  // is set explicitly (skipReset skips the lib's own setMode; triggered mode
  // would invalidate the accumulators) — a same-value ADC-config write doesn't
  // disturb the running totals, only the RSTACC bit does.
  Wire.begin();
  g_have_ina228 = g_ina228.begin(INA228_I2C_ADDR, &Wire, /*skipReset=*/true);
  if (g_have_ina228) {
    g_ina228.setShunt(INA228_SHUNT_OHMS, INA228_MAX_CURRENT_A);
    g_ina228.setMode(INA2XX_MODE_CONTINUOUS);
    powerSchedInit(g_power_sched);
    Serial.println("[power] INA228 found — energy telemetry ON "
                   "(`power` / `power reset`; reports unicast to conductor)");
  }
}

void loop() {
  otaFinalizePending();
  if (g_ota_reboot_pending) {
    for (uint8_t i = 0; i < 20; i++) {
      otaStatusReport(/*keep_pending*/ true);
      delay(250);
    }
    ESP.restart();
  }

  int64_t t = now_us();

  pollSerialCommands();
  maybeOtaStatusReport();

  // Snapshot sync + beacon together up front; render uses them below. The
  // beacon credit (dutyNoteBeacon) must run BEFORE dutyStep: a beacon that
  // lands in the final loop tick of a listen window would otherwise be
  // discovered only after dutyStep has already closed the window as "missed"
  // and powered the radio down (where dutyNoteBeacon no-ops) — silently
  // corrupting the missed_windows health metric.
  SyncState s;
  BeaconMsg b;
  portENTER_CRITICAL(&g_sync_mux);
  s = g_sync;
  b = g_beacon;
  portEXIT_CRITICAL(&g_sync_mux);
  if (isConductor()) b.keepalive = g_keepalive;
  keepAliveSanitize(b.keepalive);
  static uint32_t last_rx = 0;
  if (s.beacons_rx != last_rx) { dutyNoteBeacon(g_duty); last_rx = s.beacons_rx; }

  if (isConductor()) {
    static int64_t next_beacon = 0;
    if (t >= next_beacon) {
      broadcastBeacon();
      next_beacon = t + BEACON_INTERVAL_US;
    }
    // Slow steady-state table rebroadcast — a backstop only; targeted delivery
    // happens via the row replies below and `assign`'s immediate broadcast.
    if (t >= g_next_table_us) {
      broadcastTable();
      g_next_table_us = t + TABLE_INTERVAL_US;
    }
    if (b.pattern_id == patterns::CALIBRATION && t >= g_next_cal_roster_us) {
      broadcastCalibrationRoster();
      g_next_cal_roster_us = t + CAL_ROSTER_INTERVAL_US;
    }
    // Row replies: answer each queued first-join/unprovisioned REGISTER with
    // just that node's row, while its radio is still up (it transmitted
    // moments ago). The volatile pre-check keeps the ~60 Hz loop from taking
    // the spinlock when the queue is empty (nearly always); at worst a request
    // is seen one 16 ms pass late.
    if (g_rowreq_n) {
      uint8_t req[ROWREQ_MAX][6];
      uint8_t n;
      portENTER_CRITICAL(&g_roster_mux);
      n = g_rowreq_n;
      if (n) memcpy(req, g_rowreq, sizeof(uint8_t) * 6 * n);
      g_rowreq_n = 0;
      portEXIT_CRITICAL(&g_roster_mux);
      for (uint8_t i = 0; i < n; i++) {
        TableMsg m;
        size_t len = tableRowBuild(g_table, req[i], m);
        if (len) esp_now_send(BROADCAST_ADDR, (const uint8_t*)&m, len);
        // len == 0: no row assigned yet — `assign` broadcasts when there is.
      }
    }
    drainPowerReports();  // log performers' MSG_POWER (ungated — overnight audit)
    // A conductor carrying the chip logs its own draw on the same cadence, so a
    // single instrumented board benches the sensor with no second node needed.
    // Same host-tested scheduler as the performer path (can_send always true:
    // "sending" here is a local print, no radio involved).
    if (g_have_ina228) {
      static PowerSched self_sched = {0};
      if (powerReportDue(self_sched, t, POWER_REPORT_INTERVAL_US, /*can_send*/ true))
        printPowerSample(g_mac, readPowerSample(t));
    }
  } else {
    // Duty-cycle the radio: off between brief listen windows, rendering the whole
    // time from the synced clock. dutyStep returns a transition to apply, if any.
    PowerPolicy policy = b.power;
    powerPolicySanitize(policy);
    powerPolicyAdvanceToSyncedNow(policy, b, s, t);
    bool field_awake = powerPolicyForceAwake(policy);
    if (g_ota_write_active && !g_radio_on) radioWake();
    if (field_awake && !g_radio_on) radioWake();
    if (g_powersave && !g_ota_write_active && !field_awake) {
      DutyAction act = dutyStep(g_duty, currentDutyConfig(policy), t);
      if (act == DUTY_WAKE) radioWake();
      else if (act == DUTY_SLEEP) radioSleep();
    }
    if (g_radio_on) maybeRegister(t);  // TX only when the radio is powered
    maybePowerReport(t);   // no-op without the INA228; defers until radio-on
    maybeOtaStatusReport();
    maybeAdoptPosition();  // pure NVS; flush a pending table adoption regardless

    // Primary field sleep policy: when the broadcast schedule says LEDs are off,
    // clear the pixels and deep-sleep until the next check interval. A recent
    // serial session still wins, so a board on the bench stays reachable.
    if (powerPolicyShouldDeepSleep(policy, keepAliveEnabled(b.keepalive)) &&
        t - g_last_serial_us >= DUSK_SERIAL_GRACE_US) {
      duskEnterDeepSleep(powerPolicyDeepSleepUs(policy), policy);
    }

    // Lever 2: sample the light sensor at 1 Hz and deep-sleep through daylight.
    // Every gate here fails toward "awake" (see dusk.h): debounced day + boot
    // hold-off passed + no recent serial + no recent FIELD_AWAKE beacon. The
    // conductor never dusk-sleeps (it's the wall-powered clock anchor), and the
    // whole feature is off until `dusk on` (GPIO34 floats until wired).
    if (g_dusk_on) {
      static int64_t next_dusk_sample = 0;
      if (t >= next_dusk_sample) {
        next_dusk_sample = t + DUSK_SAMPLE_US;
        g_light_mv = (uint16_t)analogReadMilliVolts(PIN_LDR);
        duskOnSample(g_dusk, DUSK_CFG, g_light_mv, t);
        int64_t last_flag;
        portENTER_CRITICAL(&g_sync_mux);
        last_flag = g_last_wake_flag_us;
        portEXIT_CRITICAL(&g_sync_mux);
        if (duskShouldSleep(g_dusk, DUSK_CFG, t, g_dusk_earliest_us,
                            g_last_serial_us, last_flag)) {
          duskEnterDeepSleep(powerPolicyDeepSleepUs(policy), policy);  // never returns
        }
      }
    }
  }

  // Power safety: hard-clamp the rendered brightness to MAX_BRIGHTNESS on every
  // node, so the per-node draw is bounded no matter what a pattern asks for.
  if (b.brightness > MAX_BRIGHTNESS) b.brightness = MAX_BRIGHTNESS;
  powerPolicySanitize(b.power);
  if (!powerPolicyLedsOn(b.power)) b.brightness = 0;

  // Conductor renders against its own clock; a performer against synced time
  // (which free-runs on the last offset when no beacon arrives).
  int64_t render_us = isConductor() ? t : syncedTime(s, t);
  uint16_t calibration_rank;
  portENTER_CRITICAL(&g_sync_mux);
  calibration_rank = g_calibration_rank;
  portEXIT_CRITICAL(&g_sync_mux);
  uint16_t render_node_id = (b.pattern_id == patterns::CALIBRATION && calibration_rank)
                                ? calibration_rank
                                : g_id.id;
  bool keepalive_pulse = powerPolicyKeepaliveWindow(b.power) &&
                         keepAlivePulseOn(b.keepalive, render_us);

  // Static patterns (GLOW/SOLID) latch: pushing the identical frame at 60 Hz is
  // pure RMT + CPU waste, and it delays every Stage-B nap behind the CanShow()
  // wait. Re-render them only when the pattern changes, plus a ~1 Hz safety
  // refresh (self-heals a noise-glitched pixel). Animated patterns render every
  // pass as before.
  static BeaconMsg last_shown = {};
  static bool shown_once = false;
  static int64_t next_static_refresh = 0;
  bool pattern_changed = !shown_once || last_shown.pattern_id != b.pattern_id ||
                        last_shown.brightness != b.brightness ||
                        memcmp(last_shown.params, b.params, sizeof(b.params)) != 0 ||
                        memcmp(&last_shown.keepalive, &b.keepalive,
                               sizeof(b.keepalive)) != 0;
  bool keepalive_active_window = powerPolicyKeepaliveWindow(b.power) &&
                                 keepAliveEnabled(b.keepalive);
  if (keepalive_active_window || !patterns::patternIsStatic(b.pattern_id) || pattern_changed ||
      t >= next_static_refresh) {
    if (keepalive_pulse) {
      RgbwColor c(0, 0, 0, b.keepalive.brightness);
      for (uint16_t i = 0; i < strip.PixelCount(); i++) strip.SetPixelColor(i, c);
    } else {
      patterns::render(strip, b, render_us, g_id.x, g_id.y, render_node_id);
    }
    strip.Show();
    last_shown = b;
    shown_once = true;
    next_static_refresh = t + DIAG_INTERVAL_US;
  }

#if HEARTBEAT_LED
  bool on = pmath::heartbeatOn(render_us, HEARTBEAT_HALF_US);
  digitalWrite(HEARTBEAT_LED_PIN, (on != HEARTBEAT_ACTIVE_LOW) ? HIGH : LOW);
#endif

  static int64_t next_diag = 0;
  if (t >= next_diag) {
    // A headless battery node shouldn't spend ~13 ms of forced-awake UART
    // drain every second printing to a disconnected port (the pre-nap
    // Serial.flush() waits it out). Diag prints only within the serial-activity
    // window — hit Enter on the monitor to revive it for another 5 minutes.
    if (t - g_last_serial_us < DUSK_SERIAL_GRACE_US) printDiag();
    next_diag = t + DIAG_INTERVAL_US;
  }

  // Stage B: while the radio is off (performer, powersave on), light-sleep until
  // the next real deadline instead of spinning delay(16) — the CPU floor is the
  // biggest constant draw after Stage A. napPlan (host-tested) picks the length;
  // 0 means "stay awake" (radio on, serial grace, or nothing worth sleeping for).
  int64_t nap = 0;
  if (!isConductor() && g_powersave && !keepalive_active_window) {
    NapInputs in;
    in.now_us = now_us();
    in.synced_us = syncedTime(s, in.now_us);
    in.radio_on = g_radio_on;
    in.radio_change_at_us = g_duty.change_at_us;
    in.pattern_static = patterns::patternIsStatic(b.pattern_id);
    in.last_serial_us = g_last_serial_us;
    in.heartbeat_half_us = HEARTBEAT_LED ? HEARTBEAT_HALF_US : 0;
    nap = napPlan(NAP_CFG, in);
  }
  if (nap > 0) {
    // The RMT transfer from strip.Show() runs in the background — sleeping mid-
    // frame would truncate it and glitch the pixels, so wait for it to finish
    // (16 RGBW pixels ≈ 0.7 ms, a bounded wait). Same for the UART TX FIFO:
    // light sleep drops chars still shifting out, garbling the diag lines.
    while (!strip.CanShow()) delayMicroseconds(50);
    Serial.flush();
    // Hold the UART0 TX pad through the nap: light sleep releases the pin,
    // which floats the line and sprays a junk byte at the host on every sleep
    // transition (bench-observed as 0xFF spam on the monitor, ~2/s).
    gpio_hold_en(GPIO_NUM_1);
    int64_t before = now_us();
    esp_sleep_enable_timer_wakeup((uint64_t)nap);
    esp_light_sleep_start();
    gpio_hold_dis(GPIO_NUM_1);
    g_naps++;
    g_napped_us += now_us() - before;  // measured, not requested (see NAP_CFG note)
    if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_UART) {
      // Someone's typing: hold naps off for the grace window so the next chars
      // land. (The waking keystroke itself is consumed by the wake — hit Enter
      // once, then type commands normally.)
      g_last_serial_us = now_us();
      Serial.println("[nap] UART wake — naps held off for provisioning");
    }
  } else {
    delay(16);  // ~60 fps render cap; keeps the CPU mostly idle for modem-sleep
  }
}
