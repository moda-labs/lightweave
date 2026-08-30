// Do Baskets Dream — node firmware (single image for every node).
//
// One conductor broadcasts a clock beacon over ESP-NOW; performers lock to it and
// render against synced time. A performer that misses a beacon keeps free-running
// on its last known offset and re-locks on the next one — a dropped packet causes
// at most slight drift, never a blackout.
//
// Every board runs THIS SAME binary. Role (conductor/performer) is a runtime
// value stored in NVS, default performer, set once over serial with `role …`.
// So you flash one image everywhere, then provision role/position per board;
// factory tooling and the conductor reconcile the permanent MAC-keyed ID. Each
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
#include "blackout.h"
#include "bootplan.h"
#include "dusk.h"
#include "firmware_version.h"
#include "identity.h"
#include "macaddr.h"
#include "napsched.h"
#include "patterns.h"
#include "power_table.h"
#include "powermon.h"
#include "powersave.h"
#include "performer_tx.h"
#include "registration.h"
#include "relay.h"
#include "roster.h"
#include "serial_json.h"
#include "sync.h"
#include "table.h"
#include "table_wire.h"
#include "uploaded_pattern.h"

// One RGBW data chain, sized for the largest supported profile. Renderers clear
// inactive pixels, so the same image drives 16, 32, or 64 physical emitters.
NeoPixelBus<NeoGrbwFeature, NeoEsp32Rmt0Sk6812Method> strip(MAX_LED_COUNT, LED_PIN);

static const uint8_t BROADCAST_ADDR[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// ---- Node config in NVS (role + identity; set once over serial) --------------
static Preferences  g_prefs;
static NodeIdentity g_id   = {0, 0.0f, 0.0f, 0, DEFAULT_LED_COUNT};
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
static bool g_timer_wake = false;

// Runtime field power policy. The conductor persists these knobs and includes
// them in every beacon; performers apply the latest received policy directly, so
// changing schedule/intervals is a control-plane action, not a reflash.
static PowerPolicy g_power_policy = {4, 15, 20 * 60, 6 * 60, 12 * 60, 0, 0};
static uint16_t    g_policy_base_min = 12 * 60;
static uint32_t    g_policy_base_epoch_s = 0;
static int64_t     g_policy_clock_set_us = 0;
static bool        g_ota_maintenance = false;
static int64_t     g_ota_maintenance_until_us = 0;
static constexpr int64_t OTA_WINDOW_US = 15LL * 60LL * 1000000LL;
static constexpr int64_t OTA_STATUS_FRESH_US = 30000000LL;
// REGISTER arrives every 10–12 s. Three longest intervals distinguish an online
// performer from a stale roster row without making one dropped registration
// flap it.
static constexpr int64_t OTA_COHORT_FRESH_US =
    3 * (REGISTER_INTERVAL_US + REGISTER_INTERVAL_JITTER_US);
static OtaSessionMode g_ota_session = OTA_SESSION_IDLE;
static uint32_t    g_ota_write_size = 0;
static uint32_t    g_ota_write_written = 0;
static uint32_t    g_ota_write_crc = 0;
static uint32_t    g_ota_write_expected_crc = 0;
static bool        g_ota_finalize_pending = false;
static bool        g_ota_reboot_pending = false;
static OtaStatusTable g_ota_status;
static OtaCohort      g_ota_cohort;
static OtaPeerLease   g_ota_unicast_peer = {{0}, false};
static OtaSendAck     g_ota_send_ack = {{0}, OTA_SEND_ACK_IDLE};
static portMUX_TYPE   g_ota_status_mux = portMUX_INITIALIZER_UNLOCKED;
static portMUX_TYPE   g_ota_send_ack_mux = portMUX_INITIALIZER_UNLOCKED;
static OtaStatusMsg   g_ota_status_pending = {makeMsgHeader(MSG_OTA_STATUS),
                                               {0}, OTA_PHASE_IDLE, OTA_ERR_NONE, 0, 0};
static bool           g_ota_status_pending_dirty = false;
static int64_t        g_ota_status_due_us = 0;
static uint8_t        g_ota_local_phase = OTA_PHASE_IDLE;
static uint8_t        g_ota_local_error = OTA_ERR_NONE;
// Keep large JSON snapshots off loopTask's stack. The machine-state response
// copies 64-entry tables before printing so radio callbacks can keep updating.
static Roster         g_state_roster_snapshot;
static OtaStatusTable g_state_ota_status_snapshot;

// Performer return traffic is serialized because ESP-NOW delivery callbacks
// identify only the destination MAC. REGISTER, power telemetry, and OTA status
// all target the same conductor; tracking one purpose at a time prevents the
// wrong callback from advancing the registration scheduler.
static const RegistrationConfig REGISTER_CONFIG = {
    REGISTER_INTERVAL_US, REGISTER_INTERVAL_JITTER_US,
    REGISTER_SLOT_SPREAD_US, REGISTER_RETRY_BASE_US,
    REGISTER_RETRY_MAX_US, REGISTER_REPAIR_WAIT_US};
static RegistrationSchedule g_register_schedule;
static PerformerTxState      g_performer_tx;
static portMUX_TYPE g_register_mux = portMUX_INITIALIZER_UNLOCKED;
static portMUX_TYPE g_performer_tx_mux = portMUX_INITIALIZER_UNLOCKED;

// Uploaded patterns are isolated from the compiled renderer. Nine validated NVS
// slots cover one active program per group plus an inactive staging slot, so a
// replacement can converge without overwriting anything visible. Status is separate from the
// byte-stable REGISTER layout so stable transport v11 remains intact.
static UploadedProgramSlots g_uploaded_programs;
static uint64_t g_uploaded_target_id = 0;
static UploadedProgramStatusTable g_uploaded_status;
static portMUX_TYPE g_uploaded_status_mux = portMUX_INITIALIZER_UNLOCKED;
static UploadedProgram g_uploaded_install_pending = {};
static bool g_uploaded_install_pending_dirty = false;
static uint64_t g_uploaded_status_requested_id = 0;
static UploadedStatusTxSchedule g_uploaded_status_schedule = {};
static constexpr int64_t UPLOADED_VERIFICATION_WINDOW_US = 60000000LL;
static int64_t g_uploaded_verification_until_us = 0;
static portMUX_TYPE g_uploaded_pending_mux = portMUX_INITIALIZER_UNLOCKED;

// A relay copies packets out of the Wi-Fi callback into this bounded queue, then
// performs all peer management and sends from loop(). One send is in flight at
// a time so the ESP-NOW callback remains attributable.
static RelayQueue     g_relay_queue;
static RelayReceipt   g_relay_receipt;
static OtaPeerLease   g_relay_peer = {{0}, false};
static bool           g_relay_send_pending = false;
static uint8_t        g_relay_send_mac[6] = {0};
static portMUX_TYPE   g_relay_mux = portMUX_INITIALIZER_UNLOCKED;
static OtaFrameAckWait g_ota_relay_delivery_ack = {};
static constexpr uint16_t OTA_RELAY_DELIVERY_TIMEOUT_MS = 3000;

static bool otaMaintenanceActive(int64_t t);
static void otaWriteAbort();
static void otaUnicastPeerRelease();
static void otaSetLocalStatus(uint8_t phase, uint8_t error, uint32_t offset,
                              uint32_t crc32);
static void maybeOtaStatusReport();
static void otaRadioBegin(const OtaBeginMsg& msg);
static void otaRadioChunk(const OtaChunkMsg& msg);
static void otaRadioEnd();
static void otaFinalizePending();
static void otaBroadcastBegin(uint32_t size, uint32_t crc32);
static void otaBroadcastChunk(uint32_t offset, const uint8_t* data, uint8_t len,
                              bool strong = false);
static void otaBroadcastEnd();
static bool otaUnicastChunk(const uint8_t mac[6], uint32_t offset,
                            const uint8_t* data, uint8_t len);
static bool otaUnicastBegin(const uint8_t mac[6], uint32_t size,
                            uint32_t crc32);
static bool otaUnicastActivate(const uint8_t mac[6]);
static void maybeRelayDeliveryReceipt();
static void drainRelayQueue();
static void maybeInstallUploadedProgram();
static void maybeUploadedStatusReport();
static bool uploadedProgramSend(const uint8_t destination[6],
                                const UploadedProgram& program);
static bool uploadedProgramQuerySend(const uint8_t destination[6],
                                     uint64_t requested_id);

static void onSend(const uint8_t* mac, esp_now_send_status_t status) {
  bool delivered = status == ESP_NOW_SEND_SUCCESS;
  if (g_role == ROLE_CONDUCTOR) {
    portENTER_CRITICAL(&g_ota_send_ack_mux);
    otaSendAckComplete(g_ota_send_ack, mac, delivered);
    portEXIT_CRITICAL(&g_ota_send_ack_mux);
    return;
  }

  if (g_role == ROLE_RELAY) {
    bool relay_completion = false;
    portENTER_CRITICAL(&g_relay_mux);
    if (g_relay_send_pending && routeMacEqual(g_relay_send_mac, mac)) {
      g_relay_send_pending = false;
      RelayCompletion completion;
      if (relayQueueCompleteCopy(g_relay_queue, delivered, completion))
        relayReceiptSchedule(g_relay_receipt, completion);
      relay_completion = true;
    }
    portEXIT_CRITICAL(&g_relay_mux);
    if (relay_completion) return;
  }

  PerformerTxCompletion completion;
  portENTER_CRITICAL(&g_performer_tx_mux);
  completion = performerTxComplete(g_performer_tx, mac, delivered);
  portEXIT_CRITICAL(&g_performer_tx_mux);
  if (completion.matched && completion.purpose == PERFORMER_TX_RELAY_ACK) {
    portENTER_CRITICAL(&g_relay_mux);
    relayReceiptSendResult(g_relay_receipt, completion.delivered);
    portEXIT_CRITICAL(&g_relay_mux);
  }
  if (completion.matched && completion.purpose == PERFORMER_TX_REGISTER) {
    portENTER_CRITICAL(&g_register_mux);
    registrationSendResult(g_register_schedule, esp_timer_get_time(), g_mac,
                           REGISTER_CONFIG, completion.delivered);
    portEXIT_CRITICAL(&g_register_mux);
  }
  if (completion.matched &&
      completion.purpose == PERFORMER_TX_PROGRAM_STATUS) {
    portENTER_CRITICAL(&g_uploaded_pending_mux);
    uploadedStatusScheduleResult(g_uploaded_status_schedule,
                                 esp_timer_get_time(), g_mac,
                                 completion.delivered);
    portEXIT_CRITICAL(&g_uploaded_pending_mux);
  }
}

static bool performerTxReserve(const uint8_t mac[6], uint8_t purpose) {
  if (g_role == ROLE_RELAY) {
    portENTER_CRITICAL(&g_relay_mux);
    bool forwarding = g_relay_send_pending;
    portEXIT_CRITICAL(&g_relay_mux);
    if (forwarding) return false;
  }
  bool reserved;
  portENTER_CRITICAL(&g_performer_tx_mux);
  reserved = performerTxBegin(g_performer_tx, mac, purpose);
  portEXIT_CRITICAL(&g_performer_tx_mux);
  return reserved;
}

static void performerTxRelease(const uint8_t mac[6], uint8_t purpose) {
  portENTER_CRITICAL(&g_performer_tx_mux);
  performerTxCancel(g_performer_tx, mac, purpose);
  portEXIT_CRITICAL(&g_performer_tx_mux);
}

static bool performerTxReady() {
  bool ready;
  portENTER_CRITICAL(&g_performer_tx_mux);
  ready = performerTxAvailable(g_performer_tx);
  portEXIT_CRITICAL(&g_performer_tx_mux);
  return ready;
}

// Queue non-registration performer traffic under the same one-at-a-time
// ownership used by REGISTER. The delivery callback releases the reservation;
// callers retain their existing retry/persistence policy.
static bool performerSend(const uint8_t mac[6], const uint8_t* data, size_t len,
                          uint8_t purpose) {
  if (!performerTxReserve(mac, purpose)) return false;
  if (esp_now_send(mac, data, len) == ESP_OK) return true;
  performerTxRelease(mac, purpose);
  return false;
}

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

// Conductor's authoritative board inventory (table.h logic, host-tested). Declared
// here so the NVS load/save below can reach it; edited only over serial on the
// conductor and read by the broadcast — both in loop() — so it needs no spinlock
// (unlike the roster, which the recv callback writes).
static LayoutTable  g_table;

static inline bool isConductor() { return g_role == ROLE_CONDUCTOR; }
static inline bool isRelay() { return g_role == ROLE_RELAY; }
static inline bool isPerformer() { return g_role == ROLE_PERFORMER; }

static void configLoad() {
  g_prefs.begin("node", /*readonly*/ true);
  g_id.id = g_prefs.getUShort("id", 0);
  g_id.x = g_prefs.getFloat("x", 0.0f);
  g_id.y = g_prefs.getFloat("y", 0.0f);
  g_id.group_id = groupIdSafe(g_prefs.getUChar("grp", 0));
  g_id.led_count = ledCountSafe(g_prefs.getUChar("leds", DEFAULT_LED_COUNT));
  g_role = nodeRoleSafe(g_prefs.getUChar("role", DEFAULT_ROLE));
  g_powersave = g_prefs.getBool("ps", POWERSAVE_DEFAULT != 0);
  g_dusk_on = g_prefs.getBool("dusk", DUSK_DEFAULT != 0);
  g_wake_flag = g_prefs.getBool("wake", false);
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
  g_prefs.putUChar("grp", groupIdSafe(g_id.group_id));
  g_prefs.putUChar("leds", ledCountSafe(g_id.led_count));
  g_prefs.end();
}

static void roleSave() {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putUChar("role", g_role);
  g_prefs.end();
}

// Protocol v7 stored only MAC + position. Keep the exact old native layout so a
// v10 conductor still migrates existing v7 placements instead of discarding them.
// The conductor's inventory persists as one NVS blob. Current rows carry a
// permanent ID plus optional placement; legacy position-only rows migrate with
// id=0 and learn the existing number from the performer's next REGISTER.
static void tableLoad() {
  g_prefs.begin("node", /*readonly*/ true);
  uint8_t version = g_prefs.getUChar("table_v", 0);
  size_t stored = g_prefs.getBytesLength("table");
  bool current = false;
  bool migrated = false;
  tableInit(g_table);
  if (stored == sizeof(g_table)) {
    bool loaded = g_prefs.getBytes("table", &g_table, sizeof(g_table)) ==
                  sizeof(g_table);
    if (loaded && g_table.count <= TABLE_MAX && version < 3) {
      // Protocol v8's TableEntry had identical size/field offsets, with this
      // byte as unspecified trailing padding. Never interpret it as a group.
      for (uint8_t i = 0; i < g_table.count; i++)
        g_table.entries[i].group_id = 0;
    }
    if (loaded && g_table.count <= TABLE_MAX && version < 4) {
      // v9's second trailing padding byte was unspecified. Existing hardware is
      // the original 16-emitter ring unless explicitly changed after migration.
      for (uint8_t i = 0; i < g_table.count; i++)
        g_table.entries[i].led_count = DEFAULT_LED_COUNT;
    }
    current = loaded && tableValid(g_table);
  } else if (stored == sizeof(LegacyLayoutTable)) {
    LegacyLayoutTable legacy = {};
    if (g_prefs.getBytes("table", &legacy, sizeof(legacy)) == sizeof(legacy))
      migrated = tableMigrateLegacy(legacy, g_table);
  }
  g_prefs.end();
  if (!current && !migrated) tableInit(g_table);
  if ((migrated || (current && version < 4)) && isConductor()) {
    g_prefs.begin("node", /*readonly*/ false);
    g_prefs.putBytes("table", &g_table, sizeof(g_table));
    g_prefs.putUChar("table_v", 4);
    g_prefs.end();
  }
}

static bool tableSave(const LayoutTable& table = g_table) {
  bool opened = g_prefs.begin("node", /*readonly*/ false);
  size_t written = 0;
  size_t version_written = 0;
  if (opened) {
    written = g_prefs.putBytes("table", &table, sizeof(table));
    version_written = g_prefs.putUChar("table_v", 4);
  }
  g_prefs.end();
  return opened && written == sizeof(table) && version_written == 1;
}

// ---- Sync state --------------------------------------------------------------
// Written from the ESP-NOW recv callback, read from loop(). Guarded by a spinlock
// so 64-bit fields can't tear across the two contexts.
static SyncState g_sync;
static BeaconMsg g_beacon = {};
static BlackoutState g_blackout_state = {};
static uint32_t g_tx_seq = 0;
static portMUX_TYPE g_sync_mux = portMUX_INITIALIZER_UNLOCKED;

// Performer/relay -> primary routing state. The logical primary and physical
// parent are learned together from a valid beacon. They differ behind a relay.
// Peer-add and sends remain in loop context; the receive callback only updates
// this pure state under g_sync_mux.
static ParentRoute g_parent_route = {};
static OtaPeerLease g_parent_peer = {{0}, false};

// Conductor roster: every node that has registered, keyed on MAC. The logic lives
// in roster.h (host-tested); here we hold one instance and a spinlock, since it is
// written from the recv callback (MSG_REGISTER) and read from loop() (the `roster`
// command). The lock wraps each access, mirroring g_sync_mux around syncOnBeacon.
static Roster       g_roster;
static portMUX_TYPE g_roster_mux = portMUX_INITIALIZER_UNLOCKED;
// Registration requests: the recv callback stashes MAC + reported ID, and loop()
// reconciles the persistent inventory and sends any authoritative row reply. Same
// stash-under-lock/drain-in-loop shape as the power-report queue: no radio
// work in the callback. Written under g_roster_mux (the REGISTER path already
// holds it). Overflow just drops the request — the node's next REGISTER (10–12 s)
// retries it, so nothing is permanently lost.
struct RegistrationRequest {
  uint8_t mac[6];
  uint16_t reported_id;
  uint8_t reported_group;
  uint8_t reported_led_count;
  bool mac_known;
};
static constexpr uint8_t ROWREQ_MAX = 8;
static RegistrationRequest g_rowreq[ROWREQ_MAX];
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

// Performer identity/position/group adopted from a MSG_TABLE row. The recv callback
// cannot write flash, so loop() applies and caches the assignment.
static bool g_assignment_pending = false;
static TableAssignment g_assignment_pending_value = {0, 0.0f, 0.0f, false, 0};

static inline int64_t now_us() { return esp_timer_get_time(); }

// ---- Pattern config in NVS (the broadcast pattern; tweaked live over serial) ---
// Defined here, *after* g_beacon, so it can touch it — configLoad() above can't
// (it's defined before g_beacon). Only the conductor's pattern drives the field,
// but every node persists/restores it so a conductor survives a power-cycle with
// its tuning intact, and this seeds the show-program storage later.
static void sanitizePatternConfig(PatternConfig& p) {
  p.pattern_id = patterns::patternBootSafe(p.pattern_id);
  if (p.brightness > MAX_BRIGHTNESS) p.brightness = MAX_BRIGHTNESS;
}

static void patternConfigLoad() {
  g_prefs.begin("node", /*readonly*/ true);
  PatternConfig legacy = {
      g_prefs.getUShort("pat", patterns::SWEEP),
      g_prefs.getUChar("bri", 48),
      0,
      {g_prefs.getUShort("p0", 0), g_prefs.getUShort("p1", 0),
       g_prefs.getUShort("p2", 0), g_prefs.getUShort("p3", 0)}};
  sanitizePatternConfig(legacy);
  size_t got = g_prefs.getBytes("patterns", g_beacon.patterns,
                                sizeof(g_beacon.patterns));
  if (got != sizeof(g_beacon.patterns)) {
    // First v9 boot: copying the old field-wide look to every group guarantees
    // that assigning groups later is opt-in and the upgrade itself is invisible.
    for (uint8_t i = 0; i < GROUP_COUNT; i++) g_beacon.patterns[i] = legacy;
  } else {
    for (uint8_t i = 0; i < GROUP_COUNT; i++)
      sanitizePatternConfig(g_beacon.patterns[i]);
  }
  g_beacon.hdr = makeMsgHeader(MSG_BEACON);
  g_beacon.epoch_us = 0;
  g_beacon.flags = 0;
  g_beacon.power = g_power_policy;
  beaconLocatorClear(g_beacon);  // locator mode is intentionally not persisted
  g_beacon.seq = 0;
  g_prefs.end();
}

static void blackoutStateLoad() {
  g_prefs.begin("node", /*readonly*/ true);
  size_t got = g_prefs.getBytes("blackout", &g_blackout_state,
                                sizeof(g_blackout_state));
  g_prefs.end();
  if (got != sizeof(g_blackout_state) ||
      !blackoutStateValid(g_blackout_state)) {
    blackoutStateInit(g_blackout_state);
  }
}

static void blackoutStateSave() {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putBytes("blackout", &g_blackout_state, sizeof(g_blackout_state));
  g_prefs.end();
}

// Takes a caller-held snapshot, not the live g_beacon: on a performer the recv
// callback overwrites g_beacon (under g_sync_mux) from the WiFi task, and NVS
// writes are far too slow to hold a spinlock across — so the serial handlers
// mutate + snapshot under the lock, then persist from the copy.
static void patternConfigSave(const BeaconMsg& b) {
  g_prefs.begin("node", /*readonly*/ false);
  g_prefs.putBytes("patterns", b.patterns, sizeof(b.patterns));
  // Keep the legacy keys as a readable Group 1 fallback for downgrade/repair.
  const PatternConfig& p = b.patterns[0];
  g_prefs.putUShort("pat", p.pattern_id);
  g_prefs.putUChar("bri", p.brightness);
  g_prefs.putUShort("p0", p.params[0]);
  g_prefs.putUShort("p1", p.params[1]);
  g_prefs.putUShort("p2", p.params[2]);
  g_prefs.putUShort("p3", p.params[3]);
  g_prefs.end();
}

static void uploadedProgramsLoad() {
  uploadedProgramSlotsInit(g_uploaded_programs);
  g_prefs.begin("node", /*readonly*/ true);
  if (g_prefs.getBytesLength("uprog") == sizeof(g_uploaded_programs))
    g_prefs.getBytes("uprog", &g_uploaded_programs,
                     sizeof(g_uploaded_programs));
  g_uploaded_target_id = g_prefs.getULong64("uprog_t", 0);
  g_prefs.end();
  for (uint8_t i = 0; i < UPLOADED_PROGRAM_SLOTS; i++) {
    if (!uploadedProgramValid(g_uploaded_programs.slots[i]))
      memset(&g_uploaded_programs.slots[i], 0,
             sizeof(g_uploaded_programs.slots[i]));
  }
  if (uploadedProgramFind(g_uploaded_programs, g_uploaded_target_id) < 0)
    g_uploaded_target_id = 0;
}

static bool uploadedProgramsSave() {
  g_prefs.begin("node", /*readonly*/ false);
  size_t programs = g_prefs.putBytes("uprog", &g_uploaded_programs,
                                     sizeof(g_uploaded_programs));
  size_t target = g_prefs.putULong64("uprog_t", g_uploaded_target_id);
  g_prefs.end();
  return programs == sizeof(g_uploaded_programs) && target == sizeof(uint64_t);
}

static bool currentFirmwareFleetReady(int64_t t) {
  const uint8_t expected = tablePositionedCount(g_table);
  uint8_t seen = 0;
  uint8_t matching = 0;
  const FirmwareVersion conductor_firmware =
      currentFirmwareVersion(PROTO_VERSION);
  for (uint8_t i = 0; i < g_table.count; i++) {
    if (!tableHasPosition(g_table.entries[i])) continue;
    int64_t registered_us = 0;
    FirmwareVersion reported = {};
    bool known = false;
    portENTER_CRITICAL(&g_roster_mux);
    const int roster_index = rosterFind(g_roster, g_table.entries[i].mac);
    if (roster_index >= 0) {
      registered_us = g_roster.entries[roster_index].last_us;
      reported = rosterEntryFirmware(g_roster.entries[roster_index]);
      known = true;
    }
    portEXIT_CRITICAL(&g_roster_mux);
    if (!known ||
        !otaSeenRecently(registered_us, t, OTA_COHORT_FRESH_US))
      continue;
    seen++;
    if (firmwareSame(conductor_firmware, reported)) matching++;
  }
  return firmwareFleetReady(expected, seen, matching);
}

static bool uploadedProgramInstallLocal(const UploadedProgram& program) {
  UploadedProgramSlots previous_programs = g_uploaded_programs;
  uint64_t previous_target = g_uploaded_target_id;
  BeaconMsg beacon;
  portENTER_CRITICAL(&g_sync_mux);
  beacon = g_beacon;
  portEXIT_CRITICAL(&g_sync_mux);
  if (!uploadedProgramValid(program)) return false;
  int existing = uploadedProgramFind(g_uploaded_programs, program.id);
  uint8_t changed_slot = UINT8_MAX;
  if (existing < 0) {
    uint64_t active_ids[UPLOADED_PROGRAM_SLOTS] = {0};
    uint8_t active_count = 0;
    for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++) {
      const PatternConfig& pattern = beacon.patterns[group_id];
      if (pattern.pattern_id != patterns::UPLOADED) continue;
      uint64_t id = patterns::uploadedPatternProgramId(pattern.params);
      bool known = id == 0;
      for (uint8_t i = 0; i < active_count; i++) known = known || active_ids[i] == id;
      if (!known && active_count < UPLOADED_PROGRAM_SLOTS)
        active_ids[active_count++] = id;
    }
    if (!uploadedProgramInstallPreserving(
            g_uploaded_programs, program, active_ids, active_count,
            &changed_slot))
      return false;
  }
  g_uploaded_target_id = program.id;
  bool persistence_changed = changed_slot != UINT8_MAX ||
                             previous_target != g_uploaded_target_id;
  if (!persistence_changed) return true;
  if (uploadedProgramsSave()) return true;
  g_uploaded_programs = previous_programs;
  g_uploaded_target_id = previous_target;
  return false;
}

static bool uploadedFleetReady(uint64_t requested_id, int64_t t,
                               uint8_t* ready_count = nullptr,
                               uint8_t* seen_count = nullptr) {
  uint8_t ready = 0;
  uint8_t seen = 0;
  uint8_t expected = tablePositionedCount(g_table);
  FirmwareVersion conductor_firmware = currentFirmwareVersion(PROTO_VERSION);
  if (!requested_id ||
      uploadedProgramFind(g_uploaded_programs, requested_id) < 0) {
    if (ready_count) *ready_count = 0;
    if (seen_count) *seen_count = 0;
    return false;
  }
  for (uint8_t i = 0; i < g_table.count; i++) {
    if (!tableHasPosition(g_table.entries[i])) continue;
    bool online = false;
    int64_t registered_us = 0;
    FirmwareVersion roster_firmware = {};
    portENTER_CRITICAL(&g_roster_mux);
    int roster_index = rosterFind(g_roster, g_table.entries[i].mac);
    if (roster_index >= 0) {
      registered_us = g_roster.entries[roster_index].last_us;
      roster_firmware = rosterEntryFirmware(g_roster.entries[roster_index]);
      online = otaSeenRecently(registered_us, t, OTA_COHORT_FRESH_US) &&
               firmwareSame(conductor_firmware, roster_firmware);
    }
    portEXIT_CRITICAL(&g_roster_mux);
    bool capable = false;
    bool available = false;
    uint64_t installed_id = 0;
    portENTER_CRITICAL(&g_uploaded_status_mux);
    int status = uploadedStatusFind(g_uploaded_status, g_table.entries[i].mac);
    if (status >= 0) {
      const UploadedProgramStatusEntry& entry =
          g_uploaded_status.entries[status];
      capable = entry.vm_version == UPLOADED_VM_VERSION && entry.last_us > 0 &&
                firmwareSame(entry.firmware, conductor_firmware) &&
                firmwareSame(entry.firmware, roster_firmware) &&
                entry.last_us >= registered_us;
      available = entry.available;
      installed_id = entry.requested_id;
    }
    portEXIT_CRITICAL(&g_uploaded_status_mux);
    if (online && capable) seen++;
    if (online && capable && available && installed_id == requested_id)
      ready++;
  }
  if (ready_count) *ready_count = ready;
  if (seen_count) *seen_count = seen;
  return expected > 0 && ready == expected && seen == expected;
}

static bool fallbackPatternsForMismatchedFirmware(const uint8_t mac[6],
                                                  bool positioned,
                                                  BeaconMsg& beacon) {
  FirmwareVersion reported = {};
  bool known = false;
  portENTER_CRITICAL(&g_roster_mux);
  int roster_index = rosterFind(g_roster, mac);
  if (roster_index >= 0) {
    reported = rosterEntryFirmware(g_roster.entries[roster_index]);
    known = true;
  }
  portEXIT_CRITICAL(&g_roster_mux);
  bool firmware_matches = known &&
      firmwareSame(currentFirmwareVersion(PROTO_VERSION), reported);
  if (!patterns::patternMismatchRequiresFallback(positioned,
                                                  firmware_matches))
    return false;

  bool changed = false;
  portENTER_CRITICAL(&g_sync_mux);
  for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++) {
    PatternConfig& pattern = g_beacon.patterns[group_id];
    uint16_t fallback = patterns::patternAfterFirmwareMismatch(pattern.pattern_id);
    if (fallback == pattern.pattern_id) continue;
    pattern.pattern_id = fallback;
    pattern.params[0] = 40;
    pattern.params[1] = 100;
    pattern.params[2] = pmath::colorValuePack(128);
    pattern.params[3] = 0;
    changed = true;
  }
  beacon = g_beacon;
  portEXIT_CRITICAL(&g_sync_mux);
  if (!changed) return false;

  patternConfigSave(beacon);
  char formatted[18];
  Serial.printf("[pattern] firmware mismatch from %s; reverted new-only "
                "patterns to Glow\n", macStr(mac, formatted));
  return true;
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

// ---- ESP-NOW receive ---------------------------------------------------------
// Registered on every node. Validates the common header, then dispatches on the
// message type. The recv callback signature changed in Arduino-ESP32 3.x —
// support both, and grab the sender MAC, which we need for bidirectional traffic.
static bool relayQueuePacket(const uint8_t* data, size_t len,
                             const uint8_t transport_destination[6],
                             int64_t received_us, uint8_t copies = 1) {
  bool queued;
  portENTER_CRITICAL(&g_relay_mux);
  queued = relayQueuePush(g_relay_queue, data, len, transport_destination,
                          received_us, copies);
  portEXIT_CRITICAL(&g_relay_mux);
  return queued;
}

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
  if (!routeHeaderBasicValid(hdr)) return;
  int64_t received_us = now_us();

  // Beacons establish the single logical primary and a sticky physical parent.
  // A relay accepts only a direct primary beacon, then emits the same logical
  // beacon at hop one. Performers may fail over between direct/relay parents
  // only after the current path is stale and only for the same primary.
  if (hdr.type == MSG_BEACON) {
    if (isConductor() || len != (int)sizeof(BeaconMsg)) return;
    ParentDecision decision;
    portENTER_CRITICAL(&g_sync_mux);
    decision = parentRouteOnBeacon(g_parent_route, isRelay(), g_mac, src, hdr,
                                   received_us, ROUTE_PARENT_STALE_US);
    if (decision != PARENT_REJECT) {
      BeaconMsg b;
      memcpy(&b, data, sizeof(b));
      syncOnBeacon(g_sync, b.epoch_us, b.seq, received_us);
      g_beacon = b;
      if (b.flags & BEACON_FLAG_FIELD_AWAKE)
        g_last_wake_flag_us = received_us;
    }
    portEXIT_CRITICAL(&g_sync_mux);
    if (decision != PARENT_REJECT && isRelay())
      relayQueuePacket(data, len, BROADCAST_ADDR, received_us);
    return;
  }

  bool from_primary = false;
  if (isConductor()) {
    bool via_is_relay = false;
    if (hdr.hops == 1) {
      portENTER_CRITICAL(&g_roster_mux);
      int via = rosterFind(g_roster, src);
      via_is_relay = via >= 0 && g_roster.entries[via].role == ROLE_RELAY &&
                     g_roster.entries[via].hops == 0;
      portEXIT_CRITICAL(&g_roster_mux);
    }
    if (!routePrimaryReceiveValid(g_mac, src, via_is_relay, hdr)) return;
  } else {
    ParentRoute route;
    portENTER_CRITICAL(&g_sync_mux);
    route = g_parent_route;
    portEXIT_CRITICAL(&g_sync_mux);
    if (isRelay()) {
      if (routeFromCurrentParentAnyDestination(route, src, hdr)) {
        from_primary = true;
        if (routeMacBroadcast(hdr.destination)) {
          relayQueuePacket(data, len, BROADCAST_ADDR, received_us);
        } else if (!routeMacEqual(hdr.destination, g_mac)) {
          relayQueuePacket(data, len, hdr.destination, received_us,
                           relayTargetCopies(hdr.type));
          return;
        }
      } else if (routeChildUplinkValid(route, src, hdr)) {
        uint8_t copies = (hdr.type == MSG_REGISTER ||
                          hdr.type == MSG_OTA_STATUS ||
                          hdr.type == MSG_POWER ||
                          hdr.type == MSG_PROGRAM_STATUS) ? 2 : 1;
        relayQueuePacket(data, len, route.primary, received_us, copies);
        return;
      } else {
        return;
      }
    } else {
      if (!routeFromCurrentParent(route, src, hdr, g_mac)) return;
      from_primary = true;
    }
  }

  switch (hdr.type) {
    case MSG_REGISTER: {
      if (!isConductor()) return;  // only the conductor keeps a roster
      if (len != (int)sizeof(RegisterMsg)) return;
      RegisterMsg r;
      memcpy(&r, data, sizeof(r));
      // The packet body is diagnostic data, not an identity authority. Bind it
      // to the routed logical origin; the route validator above separately
      // proves a direct sender or a known direct relay carried that origin.
      if (!routeMacEqual(r.mac, hdr.origin)) return;
      portENTER_CRITICAL(&g_roster_mux);
      // Known-ness is checked BEFORE the upsert so a full roster (which drops
      // the insert without a count change) can't mask a new node.
      bool known = rosterFind(g_roster, r.mac) >= 0;
      rosterUpsert(g_roster, r.mac, r.id, r.fw, r.build, r.dirty, r.version,
                   received_us, nodeRoleSafe(r.role), src, hdr.hops);
      // Loop owns the persistent inventory and NVS writes. Queue every report
      // so it can learn a factory-assigned ID, detect conflicts, and decide
      // whether this performer needs the authoritative ID/group row sent back.
      if (g_rowreq_n < ROWREQ_MAX) {
        RegistrationRequest& request = g_rowreq[g_rowreq_n];
        memcpy(request.mac, r.mac, 6);
        request.reported_id = r.id;
        request.reported_group = groupIdSafe(r.group_id);
        request.reported_led_count = ledCountSafe(r.led_count);
        request.mac_known = known;
        g_rowreq_n = g_rowreq_n + 1;
      }
      portEXIT_CRITICAL(&g_roster_mux);
      FirmwareVersion reported_firmware = {r.fw, r.build, r.dirty, {0}};
      firmwareCopyVersion(reported_firmware.version, r.version);
      if (!firmwareSame(currentFirmwareVersion(PROTO_VERSION),
                        reported_firmware)) {
        portENTER_CRITICAL(&g_uploaded_status_mux);
        uploadedStatusInvalidate(g_uploaded_status, r.mac);
        portEXIT_CRITICAL(&g_uploaded_status_mux);
      }
      // A staged performer reports again only after the targeted activation
      // reboot. Preserve its verified size/CRC while advancing the conductor's
      // job table to complete.
      portENTER_CRITICAL(&g_ota_status_mux);
      int ota_status_index = otaStatusFind(g_ota_status, r.mac);
      if (ota_status_index >= 0 &&
          g_ota_status.entries[ota_status_index].phase == OTA_PHASE_ACTIVATING) {
        const OtaNodeStatusEntry previous =
            g_ota_status.entries[ota_status_index];
        otaStatusUpsert(g_ota_status, r.mac, OTA_PHASE_COMPLETE, OTA_ERR_NONE,
                        previous.offset, previous.crc32, now_us());
      }
      portEXIT_CRITICAL(&g_ota_status_mux);
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
    case MSG_ACK: {
      if (!isConductor() || len != (int)sizeof(AckMsg)) return;
      AckMsg m;
      memcpy(&m, data, sizeof(m));
      if (m.acked_type != MSG_OTA_ACTIVATE || m.delivered > 1) return;
      bool expected_route = false;
      portENTER_CRITICAL(&g_roster_mux);
      int child = rosterFind(g_roster, hdr.origin);
      expected_route = child >= 0 && g_roster.entries[child].hops == 1 &&
                       g_roster.entries[child].role != ROLE_RELAY &&
                       routeMacEqual(g_roster.entries[child].via, src);
      portEXIT_CRITICAL(&g_roster_mux);
      if (!expected_route) return;
      portENTER_CRITICAL(&g_ota_send_ack_mux);
      otaFrameAckComplete(g_ota_relay_delivery_ack, hdr.origin,
                          m.acked_type, g_ota_relay_delivery_ack.token,
                          m.delivered != 0);
      portEXIT_CRITICAL(&g_ota_send_ack_mux);
      break;
    }
    case MSG_OTA_FRAME_ACK: {
      if (!isConductor() || len != (int)sizeof(OtaFrameAckMsg)) return;
      OtaFrameAckMsg m;
      memcpy(&m, data, sizeof(m));
      if (!relayReceiptSupportsType(m.acked_type) ||
          m.acked_type == MSG_OTA_ACTIVATE || m.delivered > 1) return;
      bool expected_route = false;
      portENTER_CRITICAL(&g_roster_mux);
      int child = rosterFind(g_roster, hdr.origin);
      expected_route = child >= 0 && g_roster.entries[child].hops == 1 &&
                       g_roster.entries[child].role != ROLE_RELAY &&
                       routeMacEqual(g_roster.entries[child].via, src);
      portEXIT_CRITICAL(&g_roster_mux);
      if (!expected_route) return;
      portENTER_CRITICAL(&g_ota_send_ack_mux);
      otaFrameAckComplete(g_ota_relay_delivery_ack, hdr.origin,
                          m.acked_type, m.frame_token, m.delivered != 0);
      portEXIT_CRITICAL(&g_ota_send_ack_mux);
      break;
    }
    case MSG_TABLE: {
      if (isConductor()) return;  // conductor is the source, never adopts
      if (!from_primary) return;
      // Two-step validation (table_wire.h, host-tested): bounds before the
      // copy, exact length-vs-row-count after it.
      if (!tableMsgLenPlausible(len)) return;
      TableMsg m;
      memcpy(&m, data, len);
      if (!tableMsgLenValid(len, m.n)) return;
      // Find our own row; stash identity + optional position + group for loop() to
      // adopt and persist outside the radio callback.
      TableAssignment assignment;
      if (tableMsgFindRow(m, g_mac, assignment)) {
        portENTER_CRITICAL(&g_register_mux);
        registrationRepairReceived(g_register_schedule);
        portEXIT_CRITICAL(&g_register_mux);
        portENTER_CRITICAL(&g_sync_mux);
        g_assignment_pending = true;
        g_assignment_pending_value = assignment;
        portEXIT_CRITICAL(&g_sync_mux);
      }
      break;
    }
    case MSG_POWER: {
      if (!isConductor()) return;  // reports flow performer -> conductor only
      if (len != (int)sizeof(PowerMsg)) return;
      PowerMsg m;
      memcpy(&m, data, sizeof(m));
      if (!routeMacEqual(m.mac, hdr.origin)) return;
      portENTER_CRITICAL(&g_power_mux);
      if (g_power_q_n < POWER_Q_MAX) {
        g_power_q[g_power_q_n] = m;
        g_power_q_n++;
      } else {
        g_power_q_dropped++;  // can't happen at 1–2 nodes / 60 s, but never lie
      }
      portEXIT_CRITICAL(&g_power_mux);
      break;
    }
    case MSG_PROGRAM_INSTALL: {
      if (isConductor() || !from_primary) return;
      if (!programInstallMsgLenPlausible(len)) return;
      ProgramInstallMsg message = {};
      memcpy(&message, data, len);
      if (message.vm_version != UPLOADED_VM_VERSION ||
          !programInstallMsgLenValid(len, message.length))
        return;
      UploadedProgram program = {};
      program.id = message.program_id;
      program.version = message.vm_version;
      program.length = message.length;
      memcpy(program.data, message.data, message.length);
      if (!uploadedProgramValid(program)) return;
      portENTER_CRITICAL(&g_uploaded_pending_mux);
      g_uploaded_install_pending = program;
      g_uploaded_install_pending_dirty = true;
      portEXIT_CRITICAL(&g_uploaded_pending_mux);
      break;
    }
    case MSG_PROGRAM_QUERY: {
      if (isConductor() || !from_primary ||
          len != (int)sizeof(ProgramQueryMsg)) return;
      ProgramQueryMsg message;
      memcpy(&message, data, sizeof(message));
      portENTER_CRITICAL(&g_uploaded_pending_mux);
      g_uploaded_status_requested_id = message.requested_id;
      uploadedStatusScheduleRequest(g_uploaded_status_schedule, received_us,
                                    g_mac);
      portEXIT_CRITICAL(&g_uploaded_pending_mux);
      break;
    }
    case MSG_PROGRAM_STATUS: {
      if (!isConductor() || len != (int)sizeof(ProgramStatusMsg)) return;
      ProgramStatusMsg message;
      memcpy(&message, data, sizeof(message));
      if (!routeMacEqual(message.mac, hdr.origin)) return;
      FirmwareVersion firmware = {message.fw, message.build, message.dirty, {0}};
      firmwareCopyVersion(firmware.version, message.version);
      portENTER_CRITICAL(&g_uploaded_status_mux);
      uploadedStatusUpsert(g_uploaded_status, message.mac,
                           message.vm_version, message.requested_id,
                           message.available != 0, firmware, received_us);
      portEXIT_CRITICAL(&g_uploaded_status_mux);
      break;
    }
    case MSG_OTA_BEGIN: {
      if (isConductor()) return;
      if (!from_primary) return;
      if (len != (int)sizeof(OtaBeginMsg)) return;
      OtaBeginMsg m;
      memcpy(&m, data, sizeof(m));
      otaRadioBegin(m);
      break;
    }
    case MSG_OTA_CHUNK: {
      if (isConductor()) return;
      if (!from_primary) return;
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
      if (!from_primary) return;
      if (len != (int)sizeof(OtaEndMsg)) return;
      otaRadioEnd();
      break;
    }
    case MSG_OTA_STATUS: {
      if (!isConductor()) return;
      if (len != (int)sizeof(OtaStatusMsg)) return;
      if (!otaCohortContains(g_ota_cohort, hdr.origin)) return;
      OtaStatusMsg m;
      memcpy(&m, data, sizeof(m));
      if (!routeMacEqual(m.mac, hdr.origin)) return;
      portENTER_CRITICAL(&g_ota_status_mux);
      otaStatusUpsert(g_ota_status, m.mac, m.phase, m.error, m.offset,
                      m.crc32, now_us());
      portEXIT_CRITICAL(&g_ota_status_mux);
      break;
    }
    case MSG_OTA_QUERY: {
      if (isConductor() || !from_primary) return;
      if (len != (int)sizeof(OtaQueryMsg)) return;
      otaSetLocalStatus(g_ota_local_phase, g_ota_local_error,
                        g_ota_write_written, g_ota_write_crc);
      break;
    }
    case MSG_OTA_ACTIVATE: {
      if (isConductor() || !from_primary) return;
      if (len != (int)sizeof(OtaActivateMsg) ||
          !otaSessionIsStaged(g_ota_session)) return;
      otaSetLocalStatus(OTA_PHASE_ACTIVATING, OTA_ERR_NONE,
                        g_ota_write_written, g_ota_write_crc);
      g_ota_reboot_pending = true;
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
  esp_now_register_send_cb(onSend);  // repair traffic waits for radio delivery
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
// learned conductor unicast peer is gone too, so clear its lease for re-add on
// the next register.
static void radioWake() {
  esp_wifi_start();
  espnowStart();
  otaPeerLeaseInit(g_parent_peer);
  g_radio_on = true;
}

static void broadcastBeacon() {
  BeaconMsg b = g_beacon;
  b.hdr = makeMsgHeader(MSG_BEACON);
  routeHeaderSet(b.hdr, g_mac, BROADCAST_ADDR);
  b.epoch_us = now_us();
  b.seq = g_tx_seq++;
  b.power = powerPolicySnapshot(b.epoch_us);
  b.flags = powerPolicyForceAwake(b.power) ? BEACON_FLAG_FIELD_AWAKE : 0;
  esp_now_send(BROADCAST_ADDR, (const uint8_t*)&b, sizeof(b));
}

// Snapshot the learned conductor MAC and make sure it exists as a unicast peer.
// esp_wifi_stop() drops the whole peer table on every duty-cycle sleep, so this
// re-adds the peer whenever it's missing — call it before ANY unicast to the
// conductor (REGISTER, POWER). Peer-add happens here in loop context, never in
// the recv callback. Returns true when a unicast can be sent to cmac.
static bool conductorPeerReady(uint8_t cmac[6], uint8_t primary[6] = nullptr) {
  bool have;
  portENTER_CRITICAL(&g_sync_mux);
  have = g_parent_route.valid;
  if (have) {
    memcpy(cmac, g_parent_route.parent, 6);
    if (primary) memcpy(primary, g_parent_route.primary, 6);
  }
  portEXIT_CRITICAL(&g_sync_mux);
  if (!have) return false;

  if (g_parent_peer.active && !otaPeerLeaseMatches(g_parent_peer, cmac)) {
    esp_now_del_peer(g_parent_peer.mac);
    otaPeerLeaseInit(g_parent_peer);
  }
  if (!esp_now_is_peer_exist(cmac)) {
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, cmac, 6);
    peer.channel = WIFI_CHANNEL;
    peer.encrypt = false;
    esp_err_t err = esp_now_add_peer(&peer);
    if (err != ESP_OK && err != ESP_ERR_ESPNOW_EXIST) return false;
  }
  otaPeerLeaseSet(g_parent_peer, cmac);
  return true;
}

static bool relayPeerReady(const uint8_t mac[6]) {
  ParentRoute route;
  portENTER_CRITICAL(&g_sync_mux);
  route = g_parent_route;
  portEXIT_CRITICAL(&g_sync_mux);
  RelayPeerKind kind = relayPeerKind(route, mac);
  if (kind == RELAY_PEER_BROADCAST) return true;
  if (kind == RELAY_PEER_UPSTREAM) {
    uint8_t parent[6];
    return conductorPeerReady(parent) && routeMacEqual(parent, mac);
  }
  if (g_relay_peer.active && !otaPeerLeaseMatches(g_relay_peer, mac)) {
    esp_now_del_peer(g_relay_peer.mac);
    otaPeerLeaseInit(g_relay_peer);
  }
  if (esp_now_is_peer_exist(mac)) return true;
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, mac, 6);
  peer.channel = WIFI_CHANNEL;
  peer.encrypt = false;
  esp_err_t error = esp_now_add_peer(&peer);
  if (error != ESP_OK && error != ESP_ERR_ESPNOW_EXIST) return false;
  if (error == ESP_OK) otaPeerLeaseSet(g_relay_peer, mac);
  return true;
}

static void maybeRelayDeliveryReceipt() {
  if (!isRelay() || !g_radio_on || !performerTxReady()) return;
  RelayReceipt receipt;
  portENTER_CRITICAL(&g_relay_mux);
  receipt = g_relay_receipt;
  portEXIT_CRITICAL(&g_relay_mux);
  if (!receipt.pending) return;

  uint8_t parent[6], primary[6];
  if (!conductorPeerReady(parent, primary)) return;
  if (receipt.type == MSG_OTA_ACTIVATE) {
    AckMsg ack = {makeMsgHeader(MSG_ACK), receipt.type,
                  (uint8_t)(receipt.delivered ? 1 : 0)};
    routeHeaderSet(ack.hdr, receipt.destination, primary, 1);
    performerSend(parent, (const uint8_t*)&ack, sizeof(ack),
                  PERFORMER_TX_RELAY_ACK);
  } else {
    OtaFrameAckMsg ack = {makeMsgHeader(MSG_OTA_FRAME_ACK), receipt.type,
                          (uint8_t)(receipt.delivered ? 1 : 0),
                          receipt.token};
    routeHeaderSet(ack.hdr, receipt.destination, primary, 1);
    performerSend(parent, (const uint8_t*)&ack, sizeof(ack),
                  PERFORMER_TX_RELAY_ACK);
  }
}

static void drainRelayQueue() {
  if (!isRelay() || !g_radio_on || !performerTxReady()) return;

  RelayFrame frame;
  portENTER_CRITICAL(&g_relay_mux);
  if (g_relay_send_pending || !g_relay_queue.count) {
    portEXIT_CRITICAL(&g_relay_mux);
    return;
  }
  frame = *relayQueueFront(g_relay_queue);
  portEXIT_CRITICAL(&g_relay_mux);

  if (!relayPeerReady(frame.transport_destination)) return;
  uint8_t packet[RELAY_PACKET_MAX];
  if (!relayFramePrepare(frame, now_us(), packet)) {
    portENTER_CRITICAL(&g_relay_mux);
    relayQueuePopCopy(g_relay_queue);
    portEXIT_CRITICAL(&g_relay_mux);
    return;
  }

  portENTER_CRITICAL(&g_relay_mux);
  g_relay_send_pending = true;
  memcpy(g_relay_send_mac, frame.transport_destination, 6);
  portEXIT_CRITICAL(&g_relay_mux);
  if (esp_now_send(frame.transport_destination, packet, frame.len) != ESP_OK) {
    portENTER_CRITICAL(&g_relay_mux);
    g_relay_send_pending = false;
    portEXIT_CRITICAL(&g_relay_mux);
  }
}

// Performer: announce ourselves to the conductor we've heard. Expired periodic
// deadlines are assigned a stable MAC-derived slot inside the current radio
// window instead of all transmitting at the wake edge. Queue failures and
// delivery-callback failures use bounded backoff (registration.h, host-tested).
static void maybeRegister(int64_t t) {
  if (isConductor()) return;
  bool due;
  portENTER_CRITICAL(&g_register_mux);
  due = registrationSendDue(g_register_schedule, t, g_mac, REGISTER_CONFIG);
  portEXIT_CRITICAL(&g_register_mux);
  if (!due) return;

  uint8_t cmac[6];
  uint8_t primary[6];
  if (!conductorPeerReady(cmac, primary)) {
    portENTER_CRITICAL(&g_register_mux);
    registrationSendResult(g_register_schedule, t, g_mac, REGISTER_CONFIG,
                           /*delivered*/ false);
    portEXIT_CRITICAL(&g_register_mux);
    return;
  }

  // Reserve the single performer-unicast slot before marking registration
  // in-flight. This ordering makes even an unusually fast send callback
  // unambiguous; a busy slot simply leaves this MAC's due slot pending.
  if (!performerTxReserve(cmac, PERFORMER_TX_REGISTER)) return;
  portENTER_CRITICAL(&g_register_mux);
  registrationSendStarted(g_register_schedule);
  portEXIT_CRITICAL(&g_register_mux);

  RegisterMsg r = {makeMsgHeader(MSG_REGISTER), {0}, g_id.id,
                   groupIdSafe(g_id.group_id), ledCountSafe(g_id.led_count),
                   nodeRoleSafe(g_role),
                   PROTO_VERSION,
                   (uint32_t)FIRMWARE_BUILD_ID,
                   (uint8_t)FIRMWARE_BUILD_DIRTY, {0}};
  firmwareCopyVersion(r.version, FIRMWARE_VERSION);
  memcpy(r.mac, g_mac, 6);
  routeHeaderSet(r.hdr, g_mac, primary);
  if (esp_now_send(cmac, (const uint8_t*)&r, sizeof(r)) != ESP_OK) {
    performerTxRelease(cmac, PERFORMER_TX_REGISTER);
    portENTER_CRITICAL(&g_register_mux);
    registrationSendResult(g_register_schedule, t, g_mac, REGISTER_CONFIG,
                           /*delivered*/ false);
    portEXIT_CRITICAL(&g_register_mux);
  }
}

static bool registrationHoldingRadio(int64_t t) {
  bool hold;
  portENTER_CRITICAL(&g_register_mux);
  hold = registrationKeepsRadioAwake(g_register_schedule, t);
  portEXIT_CRITICAL(&g_register_mux);
  return hold;
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
  uint8_t primary[6];
  bool can_send = g_radio_on && performerTxReady() &&
                  conductorPeerReady(cmac, primary);
  if (!powerReportDue(g_power_sched, t, POWER_REPORT_INTERVAL_US, can_send)) return;

  PowerMsg m = {makeMsgHeader(MSG_POWER), {0}, readPowerSample(t)};
  memcpy(m.mac, g_mac, 6);
  routeHeaderSet(m.hdr, g_mac, primary);
  performerSend(cmac, (const uint8_t*)&m, sizeof(m), PERFORMER_TX_POWER);
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

static void maybeInstallUploadedProgram() {
  if (isConductor()) return;
  UploadedProgram program = {};
  bool pending = false;
  portENTER_CRITICAL(&g_uploaded_pending_mux);
  if (g_uploaded_install_pending_dirty) {
    program = g_uploaded_install_pending;
    g_uploaded_install_pending_dirty = false;
    pending = true;
  }
  portEXIT_CRITICAL(&g_uploaded_pending_mux);
  if (!pending) return;

  bool installed = uploadedProgramInstallLocal(program);
  portENTER_CRITICAL(&g_uploaded_pending_mux);
  g_uploaded_status_requested_id = program.id;
  uploadedStatusScheduleRequest(g_uploaded_status_schedule, now_us(), g_mac);
  portEXIT_CRITICAL(&g_uploaded_pending_mux);
  if (!installed)
    Serial.println("[program] rejected or failed to persist uploaded program");
}

static void maybeUploadedStatusReport() {
  if (isConductor() || !g_radio_on || !performerTxReady()) return;
  uint64_t requested_id = 0;
  bool due = false;
  int64_t t = now_us();
  portENTER_CRITICAL(&g_uploaded_pending_mux);
  if (uploadedStatusScheduleExpired(g_uploaded_status_schedule, t))
    uploadedStatusScheduleInit(g_uploaded_status_schedule);
  else
    due = uploadedStatusScheduleDue(g_uploaded_status_schedule, t);
  requested_id = g_uploaded_status_requested_id;
  portEXIT_CRITICAL(&g_uploaded_pending_mux);
  if (!due) return;

  uint8_t parent[6], primary[6];
  if (!conductorPeerReady(parent, primary)) return;
  bool available = requested_id == 0 ||
      uploadedProgramFind(g_uploaded_programs, requested_id) >= 0;
  ProgramStatusMsg message = {};
  message.hdr = makeMsgHeader(MSG_PROGRAM_STATUS);
  message.vm_version = UPLOADED_VM_VERSION;
  message.available = available ? 1 : 0;
  message.requested_id = requested_id;
  FirmwareVersion firmware = currentFirmwareVersion(PROTO_VERSION);
  message.fw = firmware.proto;
  message.build = firmware.build_id;
  message.dirty = firmware.dirty;
  firmwareCopyVersion(message.version, firmware.version);
  memcpy(message.mac, g_mac, 6);
  routeHeaderSet(message.hdr, g_mac, primary);
  if (!performerSend(parent, (const uint8_t*)&message, sizeof(message),
                     PERFORMER_TX_PROGRAM_STATUS)) {
    portENTER_CRITICAL(&g_uploaded_pending_mux);
    uploadedStatusScheduleResult(g_uploaded_status_schedule, t, g_mac, false);
    portEXIT_CRITICAL(&g_uploaded_pending_mux);
  }
}

static bool uploadedProgramSend(const uint8_t destination[6],
                                const UploadedProgram& program) {
  if (!isConductor() || !uploadedProgramValid(program)) return false;
  ProgramInstallMsg message = {};
  message.hdr = makeMsgHeader(MSG_PROGRAM_INSTALL);
  routeHeaderSet(message.hdr, g_mac, destination);
  message.program_id = program.id;
  message.vm_version = program.version;
  message.length = program.length;
  memcpy(message.data, program.data, program.length);
  size_t len = offsetof(ProgramInstallMsg, data) + program.length;
  bool queued = false;
  for (uint8_t copy = 0; copy < 3; copy++)
    queued = esp_now_send(BROADCAST_ADDR, (const uint8_t*)&message, len) == ESP_OK ||
             queued;
  return queued;
}

static bool uploadedProgramQuerySend(const uint8_t destination[6],
                                     uint64_t requested_id) {
  if (!isConductor() || !requested_id) return false;
  ProgramQueryMsg message = {};
  message.hdr = makeMsgHeader(MSG_PROGRAM_QUERY);
  routeHeaderSet(message.hdr, g_mac, destination);
  message.requested_id = requested_id;
  bool queued = false;
  for (uint8_t copy = 0; copy < 2; copy++)
    queued = esp_now_send(BROADCAST_ADDR, (const uint8_t*)&message,
                          sizeof(message)) == ESP_OK || queued;
  return queued;
}

// Conductor: broadcast the authoritative inventory in ESP-NOW-sized chunks. A
// node adopts its permanent ID and optional placement, then caches both in NVS.
// Chunk math lives in table_wire.h; this is just the radio call per chunk.
static void broadcastTable() {
  uint8_t chunks = tableChunkCount(g_table.count);  // 0 when table empty
  for (uint8_t c = 0; c < chunks; c++) {
    TableMsg m;
    size_t len = tableChunkBuild(g_table, c, m);
    routeHeaderSet(m.hdr, g_mac, BROADCAST_ADDR);
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
    m.hdr = makeMsgHeader(MSG_ROSTER);
    routeHeaderSet(m.hdr, g_mac, BROADCAST_ADDR);
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

// Performer: apply the conductor's permanent ID and optional position. Saves to
// NVS so an erased/reflashed board rehydrates itself from its MAC-keyed row.
static void maybeAdoptTableAssignment() {
  bool pending;
  TableAssignment assignment;
  portENTER_CRITICAL(&g_sync_mux);
  pending = g_assignment_pending;
  assignment = g_assignment_pending_value;
  g_assignment_pending = false;
  portEXIT_CRITICAL(&g_sync_mux);
  if (!pending) return;
  TableIdentityDecision id_decision =
      tableIdentityDecision(g_id.id, assignment.id);
  if (id_decision == TABLE_ID_AUTHORITY_CONFLICT) {
    Serial.printf("[table] ID CONFLICT: board is #%u, conductor says #%u; "
                  "keeping physical board ID\n",
                  g_id.id, assignment.id);
  }
  uint16_t next_id = id_decision == TABLE_ID_ADOPT_AUTHORITY
                         ? assignment.id
                         : g_id.id;
  float next_x = assignment.has_position ? assignment.x : 0.0f;
  float next_y = assignment.has_position ? assignment.y : 0.0f;
  uint8_t next_group = groupIdSafe(assignment.group_id);
  uint8_t next_led_count = ledCountSafe(assignment.led_count);
  if (next_id == g_id.id && next_x == g_id.x && next_y == g_id.y &&
      next_group == g_id.group_id && next_led_count == g_id.led_count) return;
  bool id_changed = next_id != g_id.id;
  g_id.id = next_id;
  g_id.x = next_x;
  g_id.y = next_y;
  g_id.group_id = next_group;
  g_id.led_count = next_led_count;
  identitySave();
  if (id_changed)
    Serial.printf("[table] adopted permanent ID #%u from conductor\n", g_id.id);
  if (assignment.has_position)
    Serial.printf("[table] adopted position x=%.2f y=%.2f group=%u leds=%u from conductor\n",
                  g_id.x, g_id.y, (unsigned)(g_id.group_id + 1),
                  (unsigned)g_id.led_count);
  else
    Serial.printf("[table] placement cleared; group=%u leds=%u from conductor\n",
                  (unsigned)(g_id.group_id + 1), (unsigned)g_id.led_count);
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
    PatternConfig p = beaconRenderPattern(g_beacon, 0);
    Serial.printf("[conductor] t=%lld us  seq=%lu  pat=%u  bri=%u\n",
                  (long long)now_us(), (unsigned long)g_tx_seq, p.pattern_id,
                  p.brightness);
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
      "[%s] %s  offset=%lld us  last_beacon=%lld ms ago  rx=%lu  gaps=%lu  rej=%lu  seq=%lu\n",
      isRelay() ? "relay" : "performer",
      stale ? "FREE-RUN" : "LOCKED  ", (long long)s.offset_us,
      (long long)(age < 0 ? -1 : age / 1000), (unsigned long)s.beacons_rx,
      (unsigned long)s.seq_gaps, (unsigned long)s.offset_rejects,
      (unsigned long)s.last_seq);
  if (isPerformer() && g_powersave) {
    // windows/missed_windows tell whether the listen window is reliably catching a
    // beacon — the main risk of the duty-cycle (HANDOFF gotcha #1). naps/slept are
    // Stage B: slept is measured, so slept≈0 with a climbing nap count would mean
    // esp_timer is NOT compensated across light sleep (the Stage-B hardware risk).
    Serial.printf("  [duty] radio=%s  windows=%lu  missed=%lu  [nap] n=%lu  slept=%.1fs\n",
                  g_radio_on ? "ON " : "off", (unsigned long)g_duty.windows,
                  (unsigned long)g_duty.missed_windows, (unsigned long)g_naps,
                  (double)g_napped_us / 1e6);
  }
  if (isPerformer() && g_dusk_on) {
    // day=DAY means deep sleep is pending only the fail-awake gates (boot
    // hold-off / serial grace / wake-flag TTL) — the node will vanish soon.
    Serial.printf("  [dusk] light=%u mV  %s\n", g_light_mv,
                  g_dusk.day ? "DAY — sleep pending gates" : "night");
  }
  if (isRelay()) {
    portENTER_CRITICAL(&g_relay_mux);
    uint8_t queued = g_relay_queue.count;
    uint32_t dropped = g_relay_queue.dropped;
    portEXIT_CRITICAL(&g_relay_mux);
    Serial.printf("  [relay] queued=%u dropped=%lu\n", queued,
                  (unsigned long)dropped);
  }
}

// ---- Serial command interface ------------------------------------------------
// Flash identical firmware to every board, then provision each over serial:
//   info                 print role + identity (incl. MAC) + pattern state
//   roster               (conductor) list nodes that have registered (MAC/id/fw)
//   table                (conductor) print permanent IDs + positions + hardware
//   assign <mac> <x> <y> (conductor) set a node's position by MAC; saved+broadcast
//   reserve-id <mac>     (conductor JSON RPC) reserve/return permanent ID
//   forget <mac>         (conductor) clear position; permanent ID remains
//   group <mac> <1..8>  (conductor) assign any inventoried node to a show group
//   leds <mac> <16|32|64> (conductor) set a board's active RGBW emitter count
//   role <conductor|performer|relay> set this node's role and save to NVS
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
  PatternConfig pattern_config = beaconRenderPattern(b, g_id.group_id);
  const char* role_name = isConductor() ? "CONDUCTOR" :
                          (isRelay() ? "RELAY" : "PERFORMER");
  Serial.printf("role=%s  id=%u  mac=%s  x=%.2f  y=%.2f  group=%u  leds=%u\n",
                role_name, g_id.id,
                macStr(g_mac, mac), g_id.x, g_id.y,
                (unsigned)(g_id.group_id + 1), (unsigned)g_id.led_count);
  Serial.printf("  firmware: v%s  proto=%u  build=%08lx%s\n", FIRMWARE_VERSION,
                PROTO_VERSION,
                (unsigned long)(uint32_t)FIRMWARE_BUILD_ID,
                FIRMWARE_BUILD_DIRTY ? " dirty" : "");
  Serial.printf("  pattern=%u  bri=%u  params=[%u %u %u %u]\n",
                pattern_config.pattern_id, pattern_config.brightness,
                pattern_config.params[0], pattern_config.params[1],
                pattern_config.params[2], pattern_config.params[3]);
  Serial.printf("  powersave=%s%s  dusk=%s%s\n", g_powersave ? "on" : "off",
                !isPerformer() ? " (infrastructure role: radio always on)" : "",
                g_dusk_on ? "on" : "off",
                !isPerformer() ? " (infrastructure role: never dusk-sleeps)" : "");
  if (!isConductor()) {
    ParentRoute route;
    portENTER_CRITICAL(&g_sync_mux);
    route = g_parent_route;
    portEXIT_CRITICAL(&g_sync_mux);
    char primary[18];
    char parent[18];
    if (route.valid) {
      Serial.printf("  route: primary=%s parent=%s hops=%u\n",
                    macStr(route.primary, primary), macStr(route.parent, parent),
                    route.hops);
    } else {
      Serial.println("  route: waiting for primary beacon");
    }
  }
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
    char via[18];
    Serial.printf("  [%u] %s  id=%u  role=%s route=%u via=%s  v%s  fw=%u  build=%08lx%s  last_seen=%lld ms ago\n", i,
                  macStr(snap.entries[i].mac, mac), snap.entries[i].id,
                  nodeRoleName(snap.entries[i].role), snap.entries[i].hops,
                  macStr(snap.entries[i].via, via),
                  snap.entries[i].version, snap.entries[i].fw,
                  (unsigned long)snap.entries[i].build,
                  snap.entries[i].dirty ? " dirty" : "",
                  (long long)((t - snap.entries[i].last_us) / 1000));
  }
}

// Conductor-only: print the authoritative board inventory and placement.
static void printTable() {
  if (!isConductor()) {
    Serial.println("(table lives on the conductor)");
    return;
  }
  Serial.printf("inventory: %u board(s), %u positioned\n", g_table.count,
                tablePositionedCount(g_table));
  for (uint8_t i = 0; i < g_table.count; i++) {
    char mac[18];
    const TableEntry& row = g_table.entries[i];
    if (tableHasPosition(row))
      Serial.printf("  [%u] %s  id=#%u  x=%.2f  y=%.2f  group=%u  leds=%u\n", i,
                    macStr(row.mac, mac), row.id, row.x, row.y,
                    (unsigned)(groupIdSafe(row.group_id) + 1),
                    (unsigned)ledCountSafe(row.led_count));
    else
      Serial.printf("  [%u] %s  id=#%u  unpositioned  group=%u  leds=%u\n", i,
                    macStr(row.mac, mac), row.id,
                    (unsigned)(groupIdSafe(row.group_id) + 1),
                    (unsigned)ledCountSafe(row.led_count));
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
    case patterns::FIRE_FLICKER: return "Fire Flicker";
    case patterns::FIRE2012: return "Fire2012";
    case patterns::WAVEFRONT: return "Wavefront";
    case patterns::POND_RIPPLE: return "Pond Ripple";
    case patterns::UPLOADED: return "Uploaded Pattern";
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

static void jsonReservedId(uint32_t request_id, uint16_t node_id,
                           bool created) {
  Serial.printf(
      "{\"id\":%lu,\"ok\":true,\"message\":\"%s\",\"node_id\":%u,"
      "\"created\":%s}\n",
      (unsigned long)request_id,
      created ? "permanent ID reserved" : "permanent ID already reserved",
      node_id, created ? "true" : "false");
}

static void saveBeaconSnapshot() {
  BeaconMsg snap;
  portENTER_CRITICAL(&g_sync_mux);
  snap = g_beacon;
  portEXIT_CRITICAL(&g_sync_mux);
  patternConfigSave(snap);
}

static void printPatternValueJson(const PatternConfig& p) {
  Serial.printf("{\"pattern\":\"%s\",\"brightness\":%u,"
                "\"params\":{\"p0\":%u,\"p1\":%u,\"p2\":%u,\"p3\":%u",
                patternName(p.pattern_id), p.brightness, p.params[0], p.params[1],
                p.params[2], p.params[3]);
  if (p.pattern_id == patterns::GLOW || p.pattern_id == patterns::PULSE) {
    Serial.printf(",\"hue\":%u,\"saturation\":%u", p.params[0],
                  p.params[1] ? p.params[1] : 100);
  } else {
    Serial.printf(",\"period\":%u", p.params[0]);
  }
  Serial.print("}}");
}

static void printPatternsJson(const BeaconMsg& b) {
  Serial.print("\"pattern\":");
  printPatternValueJson(beaconRenderPattern(b, 0));  // effective field state
  Serial.print(",\"patterns\":[");
  for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++) {
    if (group_id) Serial.print(",");
    Serial.printf("{\"group_id\":%u,\"config\":", group_id);
    printPatternValueJson(beaconPattern(b, group_id));
    Serial.print("}");
  }
  Serial.printf("],\"locator\":{\"enabled\":%s,\"brightness\":%u,"
                "\"slot_ms\":%u,\"bit_count\":%u,"
                "\"min_hamming_distance\":%u}",
                beaconLocatorActive(b) ? "true" : "false",
                b.locator.brightness, b.locator.slot_ms,
                b.locator.bit_count, b.locator.min_hamming_distance);
}

static void printUploadedProgramJson(const BeaconMsg& b, int64_t t) {
  uint8_t ready_count = 0;
  uint8_t seen_count = 0;
  bool ready = uploadedFleetReady(g_uploaded_target_id, t, &ready_count,
                                  &seen_count);
  uint8_t expected = tablePositionedCount(g_table);
  uint64_t active_id = 0;
  for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++) {
    const PatternConfig& pattern = b.patterns[group_id];
    if (pattern.pattern_id == patterns::UPLOADED) {
      active_id = patterns::uploadedPatternProgramId(pattern.params);
      break;
    }
  }
  Serial.printf("\"uploaded_program\":{\"vm_version\":%u,"
                "\"target_id\":%lu,\"target_tag\":%lu,"
                "\"target_label\":\"%016llx\","
                "\"active_id\":%lu,\"active_tag\":%lu,"
                "\"ready\":%s,\"ready_count\":%u,"
                "\"seen\":%u,\"expected\":%u}",
                UPLOADED_VM_VERSION,
                (unsigned long)(g_uploaded_target_id & 0xffffffffULL),
                (unsigned long)(g_uploaded_target_id >> 32),
                (unsigned long long)g_uploaded_target_id,
                (unsigned long)(active_id & 0xffffffffULL),
                (unsigned long)(active_id >> 32),
                ready ? "true" : "false", ready_count, seen_count, expected);
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

static void printOtaJson(uint8_t expected, uint8_t online, bool firmware_mixed,
                         int64_t t) {
  bool active = otaMaintenanceActive(t);
  uint8_t deferred = expected > online ? expected - online : 0;
  bool ready = online > 0;
  long timeout_s = active && g_ota_maintenance_until_us > t
                       ? (long)((g_ota_maintenance_until_us - t) / 1000000LL)
                       : 0;
  Serial.printf("\"ota\":{\"mode\":\"%s\",\"enabled\":%s,\"ready\":%s,"
                "\"ready_count\":%u,\"expected\":%u,\"missing\":%u,"
                "\"deferred\":%u,"
                "\"firmware_consistent\":%s,\"timeout_s\":%ld,\"blocked\":[",
                active ? "updating" : "ready", active ? "true" : "false",
                ready ? "true" : "false", online, expected, deferred,
                deferred,
                firmware_mixed ? "false" : "true", timeout_s);
  bool first = true;
  if (online == 0) {
    if (!first) Serial.print(",");
    Serial.print(expected == 0 ? "\"no registered performers\""
                               : "\"no performers online\"");
    first = false;
  }
  Serial.print("],\"nodes\":");
  printOtaStatusNodesJson(t);
  Serial.print("}");
}

static void otaUnicastPeerRelease() {
  if (!g_ota_unicast_peer.active) return;
  // ESP-NOW sends complete asynchronously. Keep the peer through the complete
  // repair stream and drain the final queued copies before reclaiming the one
  // temporary slot for another performer.
  delay(OTA_RADIO_SEND_DELAY_MS * 2);
  esp_now_del_peer(g_ota_unicast_peer.mac);
  otaPeerLeaseInit(g_ota_unicast_peer);
}

static void otaWriteAbort() {
  if (otaSessionOwnsLocalWriter(g_ota_session)) Update.abort();
  otaUnicastPeerRelease();
  g_ota_session = OTA_SESSION_IDLE;
  g_ota_write_size = 0;
  g_ota_write_written = 0;
  g_ota_write_crc = 0;
  g_ota_write_expected_crc = 0;
  g_ota_finalize_pending = false;
  otaCohortInit(g_ota_cohort);
}

static bool otaSessionActive() {
  return otaSessionIsActive(g_ota_session);
}

static void otaSetLocalStatus(uint8_t phase, uint8_t error, uint32_t offset,
                              uint32_t crc32) {
  if (isConductor()) return;
  OtaStatusMsg msg = {makeMsgHeader(MSG_OTA_STATUS),
                      {0}, phase, error, offset, crc32};
  memcpy(msg.mac, g_mac, 6);
  portENTER_CRITICAL(&g_ota_status_mux);
  g_ota_status_pending = msg;
  g_ota_status_pending_dirty = true;
  g_ota_status_due_us = now_us() +
      (int64_t)otaStatusDelayMs(g_id.id, g_mac) * 1000LL;
  g_ota_local_phase = phase;
  g_ota_local_error = error;
  portEXIT_CRITICAL(&g_ota_status_mux);
}

static void otaStatusReport(bool keep_pending) {
  if (isConductor() || !g_ota_status_pending_dirty || !g_radio_on) return;

  OtaStatusMsg msg;
  int64_t due_us;
  portENTER_CRITICAL(&g_ota_status_mux);
  msg = g_ota_status_pending;
  due_us = g_ota_status_due_us;
  portEXIT_CRITICAL(&g_ota_status_mux);
  if (!keep_pending && now_us() < due_us) return;

  uint8_t conductor_mac[6];
  uint8_t primary[6];
  if (!conductorPeerReady(conductor_mac, primary)) return;
  routeHeaderSet(msg.hdr, g_mac, primary);
  if (performerSend(conductor_mac, (const uint8_t*)&msg, sizeof(msg),
                    PERFORMER_TX_OTA_STATUS) && !keep_pending) {
    portENTER_CRITICAL(&g_ota_status_mux);
    g_ota_status_pending_dirty = false;
    portEXIT_CRITICAL(&g_ota_status_mux);
  }
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
  g_ota_session = otaSessionBegin(/*targeted=*/false);
  g_ota_write_size = msg.size;
  g_ota_write_written = 0;
  g_ota_write_crc = 0;
  g_ota_write_expected_crc = msg.crc32;
  otaSetLocalStatus(OTA_PHASE_BEGIN, OTA_ERR_NONE, 0, 0);
}

static void otaRadioChunk(const OtaChunkMsg& msg) {
  if (!otaSessionOwnsLocalWriter(g_ota_session)) return;
  switch (otaChunkDecision(g_ota_write_written, g_ota_write_size,
                           msg.offset, msg.n)) {
    case OTA_CHUNK_DUPLICATE:
      otaSetLocalStatus(g_ota_local_phase, g_ota_local_error,
                        g_ota_write_written, g_ota_write_crc);
      return;
    case OTA_CHUNK_OFFSET_MISMATCH:
      // Future chunks are harmless while a checkpoint is missing. Keep the OTA
      // partition open and report the exact byte needed so the conductor can
      // replay only this performer's gap.
      otaSetLocalStatus(OTA_PHASE_REPAIRING, OTA_ERR_OFFSET_MISMATCH,
                        g_ota_write_written, g_ota_write_crc);
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
  if (!otaSessionOwnsLocalWriter(g_ota_session)) return;
  if (g_ota_write_written != g_ota_write_size) {
    otaSetLocalStatus(OTA_PHASE_REPAIRING, OTA_ERR_INCOMPLETE,
                      g_ota_write_written, g_ota_write_crc);
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
  if (!otaSessionOwnsLocalWriter(g_ota_session)) return;
  if (!otaShouldFinalizeFlash(isConductor(), OTA_FINALIZE_ON_END)) {
    otaSessionStage(g_ota_session);
    return;
  }
  if (!Update.end(true)) {
    otaSetLocalStatus(OTA_PHASE_ERROR, OTA_ERR_END_FAILED,
                      g_ota_write_written, g_ota_write_crc);
    otaWriteAbort();
    return;
  }
  otaSessionStage(g_ota_session);
  otaSetLocalStatus(OTA_PHASE_STAGED, OTA_ERR_NONE,
                    g_ota_write_written, g_ota_write_crc);
}

static void otaSendRepeated(const uint8_t* data, size_t len, uint8_t copies,
                            uint8_t max_attempts) {
  // ESP_OK means only that ESP-NOW accepted the packet into its asynchronous
  // queue. Wait for the completion callback before counting a copy or queuing
  // another one; otherwise a burst can report several accepted copies while
  // putting few (or none) on air.
  delay(OTA_RADIO_SEND_DELAY_MS);
  uint8_t accepted = 0;
  for (uint8_t attempt = 0;
       accepted < copies && attempt < max_attempts;
       attempt++) {
    portENTER_CRITICAL(&g_ota_send_ack_mux);
    otaSendAckBegin(g_ota_send_ack, BROADCAST_ADDR);
    portEXIT_CRITICAL(&g_ota_send_ack_mux);
    if (esp_now_send(BROADCAST_ADDR, data, len) != ESP_OK) {
      portENTER_CRITICAL(&g_ota_send_ack_mux);
      otaSendAckInit(g_ota_send_ack);
      portEXIT_CRITICAL(&g_ota_send_ack_mux);
      delay(OTA_RADIO_SEND_DELAY_MS);
      continue;
    }

    int64_t deadline = now_us() +
        (int64_t)OTA_RADIO_UNICAST_ACK_TIMEOUT_MS * 1000LL;
    uint8_t state = OTA_SEND_ACK_PENDING;
    while (state == OTA_SEND_ACK_PENDING && now_us() < deadline) {
      delay(1);
      portENTER_CRITICAL(&g_ota_send_ack_mux);
      state = g_ota_send_ack.state;
      portEXIT_CRITICAL(&g_ota_send_ack_mux);
    }
    portENTER_CRITICAL(&g_ota_send_ack_mux);
    if (g_ota_send_ack.state == OTA_SEND_ACK_SUCCESS) accepted++;
    otaSendAckInit(g_ota_send_ack);
    portEXIT_CRITICAL(&g_ota_send_ack_mux);
    delay(OTA_RADIO_SEND_DELAY_MS);
  }
}

static void otaBroadcastBegin(uint32_t size, uint32_t crc32) {
  if (!isConductor()) return;
  OtaBeginMsg msg = {makeMsgHeader(MSG_OTA_BEGIN), size, crc32};
  routeHeaderSet(msg.hdr, g_mac, BROADCAST_ADDR);
  otaSendRepeated((const uint8_t*)&msg, sizeof(msg), OTA_RADIO_STRONG_COPIES,
                  OTA_RADIO_STRONG_MAX_ATTEMPTS);
}

static void otaBroadcastChunk(uint32_t offset, const uint8_t* data, uint8_t len,
                              bool strong) {
  if (!isConductor() || len == 0 || len > OTA_SERIAL_CHUNK_MAX) return;
  OtaChunkMsg msg = {makeMsgHeader(MSG_OTA_CHUNK), offset, len, {0}};
  routeHeaderSet(msg.hdr, g_mac, BROADCAST_ADDR);
  memcpy(msg.data, data, len);
  otaSendRepeated((const uint8_t*)&msg, offsetof(OtaChunkMsg, data) + len,
                  strong ? OTA_RADIO_STRONG_COPIES : OTA_RADIO_SEND_COPIES,
                  strong ? OTA_RADIO_STRONG_MAX_ATTEMPTS
                         : OTA_RADIO_SEND_MAX_ATTEMPTS);
  if (otaFlashSettleDue(offset, len)) delay(OTA_FLASH_SETTLE_MS);
}

static void otaBroadcastEnd() {
  if (!isConductor()) return;
  OtaEndMsg msg = {makeMsgHeader(MSG_OTA_END)};
  routeHeaderSet(msg.hdr, g_mac, BROADCAST_ADDR);
  otaSendRepeated((const uint8_t*)&msg, sizeof(msg), OTA_RADIO_STRONG_COPIES,
                  OTA_RADIO_STRONG_MAX_ATTEMPTS);
}

static bool otaUnicastRepeated(const uint8_t mac[6], const uint8_t* data,
                               size_t len) {
  uint8_t next_hop[6];
  memcpy(next_hop, mac, 6);
  portENTER_CRITICAL(&g_roster_mux);
  int route = rosterFind(g_roster, mac);
  if (route >= 0 && g_roster.entries[route].hops == 1 &&
      g_roster.entries[route].role != ROLE_RELAY) {
    memcpy(next_hop, g_roster.entries[route].via, 6);
  }
  portEXIT_CRITICAL(&g_roster_mux);
  if (g_ota_unicast_peer.active &&
      !otaPeerLeaseMatches(g_ota_unicast_peer, next_hop)) {
    otaUnicastPeerRelease();
  }
  if (!esp_now_is_peer_exist(next_hop)) {
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, next_hop, 6);
    peer.channel = WIFI_CHANNEL;
    peer.encrypt = false;
    esp_err_t add_error = esp_now_add_peer(&peer);
    if (add_error != ESP_OK && add_error != ESP_ERR_ESPNOW_EXIST) return false;
    if (add_error == ESP_OK) otaPeerLeaseSet(g_ota_unicast_peer, next_hop);
  }
  uint8_t accepted = 0;
  for (uint8_t attempt = 0;
       accepted < OTA_RADIO_REPAIR_COPIES &&
       attempt < OTA_RADIO_REPAIR_MAX_ATTEMPTS;
       attempt++) {
    portENTER_CRITICAL(&g_ota_send_ack_mux);
    otaSendAckBegin(g_ota_send_ack, next_hop);
    portEXIT_CRITICAL(&g_ota_send_ack_mux);
    if (esp_now_send(next_hop, data, len) != ESP_OK) {
      portENTER_CRITICAL(&g_ota_send_ack_mux);
      otaSendAckInit(g_ota_send_ack);
      portEXIT_CRITICAL(&g_ota_send_ack_mux);
      delay(OTA_RADIO_SEND_DELAY_MS);
      continue;
    }

    int64_t deadline = now_us() +
        (int64_t)OTA_RADIO_UNICAST_ACK_TIMEOUT_MS * 1000LL;
    uint8_t state = OTA_SEND_ACK_PENDING;
    while (state == OTA_SEND_ACK_PENDING && now_us() < deadline) {
      delay(1);
      portENTER_CRITICAL(&g_ota_send_ack_mux);
      state = g_ota_send_ack.state;
      portEXIT_CRITICAL(&g_ota_send_ack_mux);
    }
    portENTER_CRITICAL(&g_ota_send_ack_mux);
    if (g_ota_send_ack.state == OTA_SEND_ACK_SUCCESS) accepted++;
    otaSendAckInit(g_ota_send_ack);
    portEXIT_CRITICAL(&g_ota_send_ack_mux);
    delay(OTA_RADIO_SEND_DELAY_MS);
  }
  return accepted == OTA_RADIO_REPAIR_COPIES;
}

static bool otaTargetUsesRelay(const uint8_t mac[6], uint8_t type,
                               bool& receipt_supported) {
  bool relayed = false;
  receipt_supported = false;
  portENTER_CRITICAL(&g_roster_mux);
  int route = rosterFind(g_roster, mac);
  relayed = route >= 0 && g_roster.entries[route].hops == 1 &&
            g_roster.entries[route].role != ROLE_RELAY;
  if (relayed) {
    receipt_supported = relayRouteSupportsFrameReceipt(
        g_roster, mac, currentFirmwareVersion(PROTO_VERSION), type);
  }
  portEXIT_CRITICAL(&g_roster_mux);
  return relayed;
}

// Immediate ESP-NOW delivery to a next-hop relay is not enough: its bounded
// queue may still be draining earlier frames. For every targeted OTA frame,
// wait until the relay reports that all downstream copies have drained before
// advancing to the next target. This both proves the final hop and bounds relay
// queue occupancy regardless of cohort size.
static bool otaUnicastDelivered(const uint8_t mac[6], uint8_t type,
                                const uint8_t* data, size_t len) {
  bool receipt_supported = false;
  bool relayed = otaTargetUsesRelay(mac, type, receipt_supported);
  uint32_t frame_token = relayFrameReceiptToken(data, len);
  if (receipt_supported) {
    portENTER_CRITICAL(&g_ota_send_ack_mux);
    otaFrameAckBegin(g_ota_relay_delivery_ack, mac, type, frame_token);
    portEXIT_CRITICAL(&g_ota_send_ack_mux);
  }
  if (!otaUnicastRepeated(mac, data, len)) {
    if (receipt_supported) {
      portENTER_CRITICAL(&g_ota_send_ack_mux);
      otaFrameAckInit(g_ota_relay_delivery_ack);
      portEXIT_CRITICAL(&g_ota_send_ack_mux);
    }
    return false;
  }
  if (!relayed) return true;
  if (!receipt_supported) {
    // A v11 relay from before frame receipts still forwards the exact packet.
    // Pace by the maximum callback budget for every downstream copy so no
    // second logical frame can overrun its bounded queue.
    delay((OTA_RADIO_UNICAST_ACK_TIMEOUT_MS + OTA_RADIO_SEND_DELAY_MS) *
          relayTargetCopies(type));
    return true;
  }

  int64_t deadline = now_us() +
      (int64_t)OTA_RELAY_DELIVERY_TIMEOUT_MS * 1000LL;
  uint8_t state = OTA_SEND_ACK_PENDING;
  while (state == OTA_SEND_ACK_PENDING && now_us() < deadline) {
    delay(1);
    portENTER_CRITICAL(&g_ota_send_ack_mux);
    state = g_ota_relay_delivery_ack.state;
    portEXIT_CRITICAL(&g_ota_send_ack_mux);
  }
  portENTER_CRITICAL(&g_ota_send_ack_mux);
  bool delivered = g_ota_relay_delivery_ack.state == OTA_SEND_ACK_SUCCESS;
  otaFrameAckInit(g_ota_relay_delivery_ack);
  portEXIT_CRITICAL(&g_ota_send_ack_mux);
  return delivered;
}

static bool otaUnicastChunk(const uint8_t mac[6], uint32_t offset,
                            const uint8_t* data, uint8_t len) {
  if (!isConductor() || len == 0 || len > OTA_SERIAL_CHUNK_MAX) return false;
  OtaChunkMsg msg = {makeMsgHeader(MSG_OTA_CHUNK), offset, len, {0}};
  routeHeaderSet(msg.hdr, g_mac, mac);
  memcpy(msg.data, data, len);
  bool sent = otaUnicastDelivered(mac, MSG_OTA_CHUNK,
                                  (const uint8_t*)&msg,
                                  offsetof(OtaChunkMsg, data) + len);
  if (sent && otaFlashSettleDue(offset, len)) delay(OTA_FLASH_SETTLE_MS);
  return sent;
}

static bool otaUnicastBegin(const uint8_t mac[6], uint32_t size,
                            uint32_t crc32) {
  OtaBeginMsg msg = {makeMsgHeader(MSG_OTA_BEGIN), size, crc32};
  routeHeaderSet(msg.hdr, g_mac, mac);
  return otaUnicastDelivered(mac, MSG_OTA_BEGIN,
                             (const uint8_t*)&msg, sizeof(msg));
}

static bool otaUnicastEnd(const uint8_t mac[6]) {
  OtaEndMsg msg = {makeMsgHeader(MSG_OTA_END)};
  routeHeaderSet(msg.hdr, g_mac, mac);
  return otaUnicastDelivered(mac, MSG_OTA_END,
                             (const uint8_t*)&msg, sizeof(msg));
}

static bool otaUnicastActivate(const uint8_t mac[6]) {
  OtaActivateMsg msg = {makeMsgHeader(MSG_OTA_ACTIVATE)};
  routeHeaderSet(msg.hdr, g_mac, mac);
  return otaUnicastDelivered(mac, MSG_OTA_ACTIVATE,
                             (const uint8_t*)&msg, sizeof(msg));
}

static void handleOtaBegin(const SerialJsonCommand& cmd) {
  if (cmd.ota_size == 0) {
    jsonError(cmd.id, "bad ota size");
    return;
  }
  otaWriteAbort();
  OtaCohort cohort;
  int64_t t = now_us();
  portENTER_CRITICAL(&g_roster_mux);
  otaCohortSelectFresh(cohort, g_roster, g_mac, t, OTA_COHORT_FRESH_US);
  portEXIT_CRITICAL(&g_roster_mux);
  g_ota_cohort = cohort;
  if (g_ota_cohort.count == 0) {
    jsonError(cmd.id, "no performers online");
    return;
  }
  // Live OTA mode changes only the radio/power rendezvous. Pattern beacons and
  // rendering continue throughout the transfer.
  g_ota_maintenance = true;
  g_ota_maintenance_until_us = now_us() + OTA_WINDOW_US;
  // A retry of the same artifact must earn fresh acknowledgements. Otherwise a
  // recent complete row with the same size/CRC could satisfy this new cohort.
  portENTER_CRITICAL(&g_ota_status_mux);
  otaStatusInit(g_ota_status);
  portEXIT_CRITICAL(&g_ota_status_mux);
  if (!Update.begin(cmd.ota_size, U_FLASH)) {
    otaCohortInit(g_ota_cohort);
    jsonError(cmd.id, "ota begin failed");
    return;
  }
  g_ota_session = otaSessionBegin(/*targeted=*/false);
  g_ota_write_size = cmd.ota_size;
  g_ota_write_written = 0;
  g_ota_write_crc = 0;
  g_ota_write_expected_crc = cmd.ota_crc32;
  otaBroadcastBegin(cmd.ota_size, cmd.ota_crc32);
  Serial.printf("{\"id\":%lu,\"ok\":true,\"message\":\"ota write started\","
                "\"targets\":[", (unsigned long)cmd.id);
  for (uint8_t i = 0; i < g_ota_cohort.count; i++) {
    char mac[18];
    if (i) Serial.print(",");
    Serial.printf("\"%s\"", macStr(g_ota_cohort.macs[i], mac));
  }
  Serial.print("]}\n");
}

static void handleOtaBeginTargets(const SerialJsonCommand& cmd) {
  if (cmd.ota_size == 0 || cmd.ota_targets.count == 0) {
    jsonError(cmd.id, "bad targeted ota begin");
    return;
  }
  otaWriteAbort();
  OtaCohort cohort;
  int64_t t = now_us();
  bool selected;
  portENTER_CRITICAL(&g_roster_mux);
  selected = otaCohortSelectRequestedFresh(
      cohort, cmd.ota_targets, g_roster, g_mac, t, OTA_COHORT_FRESH_US);
  portEXIT_CRITICAL(&g_roster_mux);
  if (!selected) {
    jsonError(cmd.id, "targeted ota performer is not online");
    return;
  }
  g_ota_cohort = cohort;
  g_ota_maintenance = true;
  g_ota_maintenance_until_us = now_us() + OTA_WINDOW_US;
  portENTER_CRITICAL(&g_ota_status_mux);
  otaStatusInit(g_ota_status);
  portEXIT_CRITICAL(&g_ota_status_mux);
  g_ota_session = otaSessionBegin(/*targeted=*/true);
  g_ota_write_size = cmd.ota_size;
  g_ota_write_written = 0;
  g_ota_write_crc = 0;
  g_ota_write_expected_crc = cmd.ota_crc32;
  for (uint8_t i = 0; i < g_ota_cohort.count; i++) {
    if (!otaUnicastBegin(g_ota_cohort.macs[i], cmd.ota_size,
                         cmd.ota_crc32)) {
      otaWriteAbort();
      jsonError(cmd.id, "targeted ota begin send failed");
      return;
    }
  }
  Serial.printf("{\"id\":%lu,\"ok\":true,"
                "\"message\":\"targeted ota write started\","
                "\"targeted\":true,\"targets\":[",
                (unsigned long)cmd.id);
  for (uint8_t i = 0; i < g_ota_cohort.count; i++) {
    char mac[18];
    if (i) Serial.print(",");
    Serial.printf("\"%s\"", macStr(g_ota_cohort.macs[i], mac));
  }
  Serial.print("]}\n");
}

static void handleOtaChunk(const SerialJsonCommand& cmd) {
  if (!otaSessionIsWriting(g_ota_session)) {
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
  if (otaSessionIsTargeted(g_ota_session)) {
    for (uint8_t i = 0; i < g_ota_cohort.count; i++) {
      if (!otaUnicastChunk(g_ota_cohort.macs[i], cmd.ota_offset, bytes,
                           (uint8_t)len)) {
        jsonError(cmd.id, "targeted ota chunk send failed");
        return;
      }
    }
  } else {
    otaBroadcastChunk(cmd.ota_offset, bytes, (uint8_t)len);
    size_t written = Update.write(bytes, len);
    if (written != len) {
      otaWriteAbort();
      jsonError(cmd.id, "ota flash write failed");
      return;
    }
  }
  g_ota_write_crc = otaCrc32Update(g_ota_write_crc, bytes, len);
  g_ota_write_written += (uint32_t)len;
  jsonOk(cmd.id, "ota chunk written");
}

static void handleOtaRebroadcast(const SerialJsonCommand& cmd) {
  if (!otaSessionActive()) {
    jsonError(cmd.id, "ota write is not active");
    return;
  }
  uint8_t bytes[OTA_SERIAL_CHUNK_MAX];
  size_t len = 0;
  if (!otaHexDecode(cmd.ota_data_hex, bytes, sizeof(bytes), len) || len == 0) {
    jsonError(cmd.id, "bad ota rebroadcast data");
    return;
  }
  if (len != otaExpectedChunkLen(g_ota_write_size, cmd.ota_offset) ||
      cmd.ota_offset + len > g_ota_write_written) {
    jsonError(cmd.id, "ota rebroadcast range is invalid");
    return;
  }
  if (otaSessionIsTargeted(g_ota_session)) {
    for (uint8_t i = 0; i < g_ota_cohort.count; i++) {
      if (!otaUnicastChunk(g_ota_cohort.macs[i], cmd.ota_offset, bytes,
                           (uint8_t)len)) {
        jsonError(cmd.id, "targeted ota repair send failed");
        return;
      }
    }
  } else {
    otaBroadcastChunk(cmd.ota_offset, bytes, (uint8_t)len, /*strong=*/true);
  }
  jsonOk(cmd.id, "ota repair chunk rebroadcast");
}

static void handleOtaEnd(const SerialJsonCommand& cmd) {
  if (!otaSessionActive()) {
    jsonError(cmd.id, "ota write is not active");
    return;
  }
  if (otaSessionIsWriting(g_ota_session) &&
      g_ota_write_written != g_ota_write_size) {
    otaWriteAbort();
    jsonError(cmd.id, "ota image incomplete");
    return;
  }
  if (otaSessionIsWriting(g_ota_session) &&
      g_ota_write_crc != g_ota_write_expected_crc) {
    otaWriteAbort();
    jsonError(cmd.id, "ota crc mismatch");
    return;
  }
  if (otaSessionIsTargeted(g_ota_session)) {
    for (uint8_t i = 0; i < g_ota_cohort.count; i++) {
      if (!otaUnicastEnd(g_ota_cohort.macs[i])) {
        jsonError(cmd.id, "targeted ota end send failed");
        return;
      }
    }
    otaSessionStage(g_ota_session);
  } else {
    otaBroadcastEnd();
  }
  if (otaSessionOwnsLocalWriter(g_ota_session)) {
    bool finalize_on_end =
        otaShouldFinalizeFlash(isConductor(), OTA_FINALIZE_ON_END);
    if (finalize_on_end) {
      if (!Update.end(true)) {
        otaWriteAbort();
        jsonError(cmd.id, "ota finalize failed");
        return;
      }
    }
    otaSessionStage(g_ota_session, /*retain_local_writer=*/!finalize_on_end);
  }
  Serial.printf("{\"id\":%lu,\"ok\":true,\"message\":\"ota image staged\","
                "\"staged\":true,"
                "\"nodes\":", (unsigned long)cmd.id);
  printOtaStatusNodesJson(now_us());
  Serial.print("}\n");
}

static void handleOtaProgress(const SerialJsonCommand& cmd) {
  Serial.printf("{\"id\":%lu,\"ok\":true,\"active\":%s,"
                "\"staged\":%s,\"targeted\":%s,"
                "\"size\":%lu,\"written\":%lu,"
                "\"crc32\":%lu,\"targets\":[",
                (unsigned long)cmd.id,
                otaSessionIsWriting(g_ota_session) ? "true" : "false",
                otaSessionIsStaged(g_ota_session) ? "true" : "false",
                otaSessionIsTargeted(g_ota_session) ? "true" : "false",
                (unsigned long)g_ota_write_size,
                (unsigned long)g_ota_write_written,
                (unsigned long)g_ota_write_crc);
  for (uint8_t i = 0; i < g_ota_cohort.count; i++) {
    char mac[18];
    if (i) Serial.print(",");
    Serial.printf("\"%s\"", macStr(g_ota_cohort.macs[i], mac));
  }
  Serial.print("],\"nodes\":");
  printOtaStatusNodesJson(now_us());
  Serial.print("}\n");
}

static bool otaDecodeSerialChunk(const SerialJsonCommand& cmd,
                                 uint8_t bytes[OTA_SERIAL_CHUNK_MAX],
                                 size_t& len) {
  return otaHexDecode(cmd.ota_data_hex, bytes, OTA_SERIAL_CHUNK_MAX, len) &&
         len > 0;
}

static void handleOtaRepair(const SerialJsonCommand& cmd) {
  if (!otaSessionActive() ||
      !otaCohortContains(g_ota_cohort, cmd.mac)) {
    jsonError(cmd.id, "ota repair target is not active");
    return;
  }
  uint8_t bytes[OTA_SERIAL_CHUNK_MAX];
  size_t len = 0;
  if (!otaDecodeSerialChunk(cmd, bytes, len)) {
    jsonError(cmd.id, "bad ota repair data");
    return;
  }
  if (len != otaExpectedChunkLen(g_ota_write_size, cmd.ota_offset) ||
      cmd.ota_offset + len > g_ota_write_written) {
    jsonError(cmd.id, "ota repair range is invalid");
    return;
  }
  if (!otaUnicastChunk(cmd.mac, cmd.ota_offset, bytes, (uint8_t)len)) {
    jsonError(cmd.id, "ota repair send failed");
    return;
  }
  jsonOk(cmd.id, "ota repair chunk sent");
}

static void handleOtaRestart(const SerialJsonCommand& cmd) {
  if (!otaSessionActive() ||
      !otaCohortContains(g_ota_cohort, cmd.mac)) {
    jsonError(cmd.id, "ota restart target is not active");
    return;
  }
  if (!otaUnicastBegin(cmd.mac, g_ota_write_size, g_ota_write_expected_crc)) {
    jsonError(cmd.id, "ota restart send failed");
    return;
  }
  portENTER_CRITICAL(&g_ota_status_mux);
  otaStatusUpsert(g_ota_status, cmd.mac, OTA_PHASE_BEGIN, OTA_ERR_NONE,
                  0, 0, now_us());
  portEXIT_CRITICAL(&g_ota_status_mux);
  jsonOk(cmd.id, "ota performer restarted");
}

static void handleOtaProbe(const SerialJsonCommand& cmd) {
  if (!otaSessionActive()) {
    jsonError(cmd.id, "ota write is not active");
    return;
  }
  OtaQueryMsg msg = {makeMsgHeader(MSG_OTA_QUERY)};
  routeHeaderSet(msg.hdr, g_mac, BROADCAST_ADDR);
  otaSendRepeated((const uint8_t*)&msg, sizeof(msg), OTA_RADIO_STRONG_COPIES,
                  OTA_RADIO_STRONG_MAX_ATTEMPTS);
  jsonOk(cmd.id, "ota status requested");
}

static void handleOtaActivate(const SerialJsonCommand& cmd) {
  if (cmd.ota_self) {
    if (g_ota_session != OTA_SESSION_LOCAL_STAGED &&
        g_ota_session != OTA_SESSION_LOCAL_STAGED_WRITER) {
      jsonError(cmd.id, "conductor firmware is not staged");
      return;
    }
    if (otaSessionOwnsLocalWriter(g_ota_session) &&
        otaShouldFinalizeFlash(isConductor(), OTA_FINALIZE_ON_ACTIVATE)) {
      if (!Update.end(true)) {
        otaWriteAbort();
        jsonError(cmd.id, "conductor firmware finalize failed");
        return;
      }
      g_ota_session = OTA_SESSION_LOCAL_STAGED;
    }
    jsonOk(cmd.id, "activating conductor firmware");
    Serial.flush();
    delay(100);
    ESP.restart();
    return;
  }
  int status_index;
  OtaNodeStatusEntry status;
  portENTER_CRITICAL(&g_ota_status_mux);
  status_index = otaStatusFind(g_ota_status, cmd.mac);
  if (status_index >= 0) status = g_ota_status.entries[status_index];
  portEXIT_CRITICAL(&g_ota_status_mux);
  if (status_index < 0 ||
      !otaStatusEntryStaged(status, g_ota_write_size,
                            g_ota_write_expected_crc, now_us(),
                            OTA_STATUS_FRESH_US)) {
    jsonError(cmd.id, "performer firmware is not staged");
    return;
  }
  if (!otaUnicastActivate(cmd.mac)) {
    jsonError(cmd.id, "ota activation send failed");
    return;
  }
  portENTER_CRITICAL(&g_ota_status_mux);
  otaStatusUpsert(g_ota_status, cmd.mac, OTA_PHASE_ACTIVATING, OTA_ERR_NONE,
                  status.offset, status.crc32, now_us());
  portEXIT_CRITICAL(&g_ota_status_mux);
  jsonOk(cmd.id, "performer activation sent");
}

static void printLanternJson(const uint8_t mac_bytes[6], const char* label,
                             uint16_t node_id,
                             const char* status, int64_t last_seen_s, float x,
                             float y, bool has_position, uint8_t group_id,
                             uint8_t led_count,
                             const char* attention,
                             const RosterEntry* roster_entry,
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
    Serial.printf("\"x\":%.4f,\"y\":%.4f,\"position\":\"Set\","
                  "\"group_id\":%u,\"group\":\"Group %u\",",
                  x, y, groupIdSafe(group_id), groupIdSafe(group_id) + 1);
  } else {
    Serial.printf("\"x\":null,\"y\":null,\"position\":\"Missing\","
                  "\"group_id\":%u,\"group\":\"Group %u\",",
                  groupIdSafe(group_id), groupIdSafe(group_id) + 1);
  }
  Serial.printf("\"led_count\":%u,", (unsigned)ledCountSafe(led_count));
  if (roster_entry) {
    char via[18];
    Serial.printf("\"role\":\"%s\",\"route\":{\"hops\":%u,\"via\":\"%s\"},",
                  nodeRoleName(roster_entry->role), roster_entry->hops,
                  macStr(roster_entry->via, via));
  } else {
    Serial.print("\"role\":null,\"route\":null,");
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
  uint8_t online_performers = 0;
  bool firmware_mixed = false;
  uint8_t placed_total = tablePositionedCount(g_table);
  for (uint8_t i = 0; i < g_table.count; i++) {
    if (!tableHasPosition(g_table.entries[i])) continue;
    int r = rosterFind(g_state_roster_snapshot, g_table.entries[i].mac);
    if (r >= 0 && !otaSeenRecently(g_state_roster_snapshot.entries[r].last_us,
                                   t, OTA_COHORT_FRESH_US)) {
      r = -1;
    }
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
    if (!otaSeenRecently(g_state_roster_snapshot.entries[i].last_us, t,
                         OTA_COHORT_FRESH_US)) {
      continue;
    }
    online_performers++;
    FirmwareVersion fw = rosterEntryFirmware(g_state_roster_snapshot.entries[i]);
    int inventory = tableFind(g_table, g_state_roster_snapshot.entries[i].mac);
    bool placement_attention =
        inventory < 0 || !tableHasPosition(g_table.entries[inventory]);
    bool id_conflict = tableReportedIdConflict(
        g_table, g_state_roster_snapshot.entries[i].mac,
        g_state_roster_snapshot.entries[i].id);
    if (placement_attention || id_conflict) attention++;
    if (!firmwareSame(conductor_fw, fw)) firmware_mixed = true;
  }

  Serial.printf("{\"id\":%lu,\"ok\":true,\"state\":{", (unsigned long)id);
  PowerPolicy policy = isConductor() ? powerPolicySnapshot(t) : b.power;
  Serial.printf("\"conductor\":{\"connected\":true,\"uptime_s\":%.1f,"
                "\"seq\":%lu,\"wake\":%s,\"sync\":\"%s\","
                "\"firmware\":{\"version\":\"%s\",\"proto\":%u,\"build_id\":%lu,"
                "\"build_label\":\"%08lx\",\"dirty\":%s,"
                "\"features\":[\"pond_ripple\",\"uploaded_patterns_v1\"]}},",
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
                placed_alive, placed_total, attention, placed_total,
                firmware_mixed ? "false" : "true", firmware_matching,
                firmware_seen, placed_total,
                conductor_fw.version,
                (unsigned long)conductor_fw.build_id,
                conductor_fw.dirty ? "true" : "false");
  printPatternsJson(b);
  Serial.print(",");
  printUploadedProgramJson(b, t);
  Serial.printf(",\"blackout\":{\"restore_available\":%s},",
                g_blackout_state.restore_available ? "true" : "false");
  printPowerPolicyJson(policy);
  Serial.print(",");
  printOtaJson(g_table.count, online_performers, firmware_mixed, t);
  Serial.print(",\"lanterns\":[");
  bool first = true;
  for (uint8_t i = 0; i < g_table.count; i++) {
    const TableEntry& row = g_table.entries[i];
    int r = rosterFind(g_state_roster_snapshot, row.mac);
    if (r >= 0 && !otaSeenRecently(g_state_roster_snapshot.entries[r].last_us,
                                   t, OTA_COHORT_FRESH_US)) {
      r = -1;
    }
    char label[16];
    uint16_t node_id = row.id;
    uint16_t reported_id =
        r >= 0 ? g_state_roster_snapshot.entries[r].id : 0;
    bool id_conflict =
        r >= 0 && tableReportedIdConflict(g_table, row.mac, reported_id);
    if (!node_id && r >= 0 && !id_conflict) node_id = reported_id;
    if (node_id) snprintf(label, sizeof(label), "#%u", node_id);
    else snprintf(label, sizeof(label), "#?");
    if (!first) Serial.print(",");
    first = false;
    int64_t age_s = r >= 0 ? (t - g_state_roster_snapshot.entries[r].last_us) / 1000000 : -1;
    FirmwareVersion fw;
    FirmwareVersion* fw_ptr = nullptr;
    char attention_buf[48];
    const char* attention_text;
    if (id_conflict) {
      snprintf(attention_buf, sizeof(attention_buf),
               "ID conflict: reports #%u", reported_id);
      attention_text = attention_buf;
    } else {
      attention_text = tableHasPosition(row)
                           ? (r >= 0 ? "None" : "Not seen")
                           : "Needs position";
    }
    if (r >= 0) {
      fw = rosterEntryFirmware(g_state_roster_snapshot.entries[r]);
      fw_ptr = &fw;
      if (!firmwareSame(conductor_fw, fw) && !id_conflict)
        attention_text = "Firmware mismatch";
    }
    int p = powerTableFind(g_power_table, row.mac);
    printLanternJson(row.mac, label, node_id,
                     r >= 0 ? "alive" : "missing", age_s, row.x,
                     row.y, tableHasPosition(row), row.group_id,
                     row.led_count,
                     attention_text,
                     r >= 0 ? &g_state_roster_snapshot.entries[r] : nullptr,
                     fw_ptr,
                     p >= 0 ? &g_power_table.entries[p] : nullptr, t);
  }
  for (uint8_t i = 0; i < g_state_roster_snapshot.count; i++) {
    const RosterEntry& row = g_state_roster_snapshot.entries[i];
    if (!otaSeenRecently(row.last_us, t, OTA_COHORT_FRESH_US)) continue;
    if (tableFind(g_table, row.mac) >= 0) continue;
    char label[16];
    bool id_conflict = tableReportedIdConflict(g_table, row.mac, row.id);
    if (row.id && !id_conflict) snprintf(label, sizeof(label), "#%u", row.id);
    else snprintf(label, sizeof(label), "Unknown");
    if (!first) Serial.print(",");
    first = false;
    int64_t age_s = (t - row.last_us) / 1000000;
    FirmwareVersion fw = rosterEntryFirmware(row);
    char attention_buf[48];
    const char* attention_text;
    if (id_conflict) {
      snprintf(attention_buf, sizeof(attention_buf),
               "ID conflict: reports #%u", row.id);
      attention_text = attention_buf;
    } else {
      attention_text = firmwareSame(conductor_fw, fw) ? "Needs position"
                                                       : "Firmware mismatch";
    }
    int p = powerTableFind(g_power_table, row.mac);
    printLanternJson(row.mac, label, id_conflict ? 0 : row.id, "alive", age_s,
                     0.0f, 0.0f, false, 0, DEFAULT_LED_COUNT,
                     attention_text, &row, &fw,
                     p >= 0 ? &g_power_table.entries[p] : nullptr, t);
  }
  Serial.print("],\"events\":[]}}\n");
}

static void handleMachineCommand(const SerialJsonCommand& cmd) {
  if (cmd.kind == SJ_STATE) {
    printMachineState(cmd.id);
  } else if (cmd.kind == SJ_PROGRAM_PROGRESS) {
    BeaconMsg beacon;
    portENTER_CRITICAL(&g_sync_mux);
    beacon = g_beacon;
    portEXIT_CRITICAL(&g_sync_mux);
    Serial.printf("{\"id\":%lu,\"ok\":true,", (unsigned long)cmd.id);
    printUploadedProgramJson(beacon, now_us());
    Serial.println("}");
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
  } else if (cmd.kind == SJ_GROUP) {
    if (!isConductor()) {
      jsonError(cmd.id, "group is conductor-only");
    } else if (!cmd.has_group_id || !groupIdValid(cmd.group_id)) {
      jsonError(cmd.id, "invalid group");
    } else if (tableSetGroup(g_table, cmd.mac, cmd.group_id)) {
      tableSave();
      broadcastTable();
      jsonOk(cmd.id, "group changed");
    } else {
      jsonError(cmd.id, "inventory full");
    }
  } else if (cmd.kind == SJ_LED_COUNT) {
    if (!isConductor()) {
      jsonError(cmd.id, "led count is conductor-only");
    } else if (!cmd.has_led_count || !ledCountValid(cmd.led_count)) {
      jsonError(cmd.id, "invalid led count");
    } else if (tableSetLedCount(g_table, cmd.mac, cmd.led_count)) {
      tableSave();
      broadcastTable();
      jsonOk(cmd.id, "led count changed");
    } else {
      jsonError(cmd.id, "inventory full");
    }
  } else if (cmd.kind == SJ_FORGET) {
    if (!isConductor()) {
      jsonError(cmd.id, "forget is conductor-only");
    } else if (tableClearPosition(g_table, cmd.mac)) {
      tableSave();
      broadcastTable();
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
    int replacement = tableFind(g_table, cmd.new_mac);
    uint8_t group_id = 0;
    if (!tableLookup(g_table, cmd.old_mac, x, y)) {
      jsonError(cmd.id, "old lantern has no position");
    } else if (replacement >= 0 && tableHasPosition(g_table.entries[replacement])) {
      jsonError(cmd.id, "replacement lantern already has a position");
    } else if (!tableLookupGroup(g_table, cmd.old_mac, group_id) ||
               !tableSetWithGroup(g_table, cmd.new_mac, x, y, group_id)) {
      jsonError(cmd.id, "table full");
    } else {
      tableClearPosition(g_table, cmd.old_mac);
      tableSave();
      broadcastTable();
      jsonOk(cmd.id, "replaced");
    }
  } else if (cmd.kind == SJ_RESERVE_ID) {
    if (!isConductor()) {
      jsonError(cmd.id, "reserve_id is conductor-only");
    } else {
      TableReserveResult reserved = tableReserveDurably(
          g_table, cmd.mac, cmd.reported_id,
          [](const LayoutTable& table) { return tableSave(table); });
      if (reserved.status == TABLE_RESERVE_CONFLICT) {
        jsonError(cmd.id, "permanent ID conflict");
        return;
      }
      if (reserved.status == TABLE_RESERVE_FULL) {
        jsonError(cmd.id, "table full");
        return;
      }
      if (reserved.status == TABLE_RESERVE_SAVE_FAILED) {
        jsonError(cmd.id, "failed to persist permanent ID");
        return;
      }
      bool created = reserved.status == TABLE_RESERVE_CREATED;
      if (created) broadcastTable();
      jsonReservedId(cmd.id, reserved.id, created);
    }
  } else if (cmd.kind == SJ_PATTERN) {
    bool current_firmware_activation =
        patterns::patternNeedsCurrentFirmware(cmd.pattern_id);
    bool capability_off = current_firmware_activation && cmd.has_brightness &&
                          cmd.brightness == 0;
    if (current_firmware_activation && !capability_off &&
        (!isConductor() || !currentFirmwareFleetReady(now_us()))) {
      jsonError(cmd.id,
                "pattern requires current firmware on every placed lantern");
      return;
    }
    bool uploaded_activation = cmd.pattern_id == patterns::UPLOADED;
    bool uploaded_off = uploaded_activation && cmd.has_brightness &&
                        cmd.brightness == 0;
    uint16_t uploaded_params[4] = {0, 0, 0, 0};
    if (uploaded_activation) {
      uint16_t encoded[4] = {0, 0, 0, 0};
      patterns::uploadedPatternSetProgramId(encoded, g_uploaded_target_id);
      uint16_t requested_params[4];
      for (uint8_t i = 0; i < 4; i++)
        requested_params[i] = cmd.has_params[i] ? cmd.params[i] : encoded[i];
      uint64_t requested_id =
          patterns::uploadedPatternProgramId(requested_params);
      if (!isConductor()) {
        jsonError(cmd.id, "uploaded pattern activation is conductor-only");
        return;
      }
      if (!uploaded_off &&
          (!requested_id || requested_id != g_uploaded_target_id ||
           !uploadedFleetReady(requested_id, now_us()))) {
        jsonError(cmd.id,
                  "uploaded program is not verified on every placed lantern");
        return;
      }
      patterns::uploadedPatternSetProgramId(
          uploaded_params, uploaded_off ? requested_id : g_uploaded_target_id);
    }
    portENTER_CRITICAL(&g_sync_mux);
    uint8_t first = cmd.has_group_id ? cmd.group_id : 0;
    uint8_t end = cmd.has_group_id ? (uint8_t)(cmd.group_id + 1) : GROUP_COUNT;
    for (uint8_t group_id = first; group_id < end; group_id++) {
      PatternConfig& p = g_beacon.patterns[group_id];
      p.pattern_id = cmd.pattern_id;
      if (cmd.has_brightness) {
        p.brightness =
            cmd.brightness > MAX_BRIGHTNESS ? MAX_BRIGHTNESS : cmd.brightness;
      }
      if (uploaded_activation) {
        memcpy(p.params, uploaded_params, sizeof(p.params));
      } else {
        for (uint8_t i = 0; i < 4; i++)
          if (cmd.has_params[i]) p.params[i] = cmd.params[i];
      }
    }
    portEXIT_CRITICAL(&g_sync_mux);
    saveBeaconSnapshot();
    if (uploaded_activation) g_uploaded_verification_until_us = 0;
    jsonOk(cmd.id, "pattern changed");
  } else if (cmd.kind == SJ_PROGRAM_INSTALL) {
    if (!isConductor()) {
      jsonError(cmd.id, "program install is conductor-only");
    } else if (!uploadedProgramInstallLocal(cmd.uploaded_program)) {
      jsonError(cmd.id,
                "uploaded program invalid or no inactive staging slot remains");
    } else {
      portENTER_CRITICAL(&g_uploaded_status_mux);
      uploadedStatusInit(g_uploaded_status);
      portEXIT_CRITICAL(&g_uploaded_status_mux);
      g_uploaded_verification_until_us =
          now_us() + UPLOADED_VERIFICATION_WINDOW_US;
      uploadedProgramSend(BROADCAST_ADDR, cmd.uploaded_program);
      jsonOk(cmd.id, "uploaded program staged; waiting for fleet verification");
    }
  } else if (cmd.kind == SJ_LOCATOR) {
    if (!isConductor()) {
      jsonError(cmd.id, "locator is conductor-only");
    } else {
      portENTER_CRITICAL(&g_sync_mux);
      if (cmd.locator_enabled) {
        uint8_t brightness = cmd.brightness > MAX_BRIGHTNESS
                                 ? MAX_BRIGHTNESS
                                 : cmd.brightness;
        beaconLocatorSet(g_beacon, brightness, cmd.locator_slot_ms,
                         cmd.locator_bit_count,
                         cmd.locator_min_hamming_distance);
      } else {
        beaconLocatorClear(g_beacon);
      }
      portEXIT_CRITICAL(&g_sync_mux);
      jsonOk(cmd.id, cmd.locator_enabled ? "locator enabled"
                                         : "locator disabled");
    }
  } else if (cmd.kind == SJ_BLACKOUT) {
    bool captured;
    portENTER_CRITICAL(&g_sync_mux);
    captured = blackoutApply(g_blackout_state, g_beacon.patterns);
    portEXIT_CRITICAL(&g_sync_mux);
    // Persist the recovery point before the zeroed pattern snapshot. A power
    // loss between these writes can leave a harmless extra restore, never an
    // unrecoverable blackout.
    if (captured) blackoutStateSave();
    saveBeaconSnapshot();
    jsonOk(cmd.id, "blackout broadcast");
  } else if (cmd.kind == SJ_RESTORE_BLACKOUT) {
    bool restored;
    portENTER_CRITICAL(&g_sync_mux);
    restored = blackoutRestore(g_blackout_state, g_beacon.patterns);
    portEXIT_CRITICAL(&g_sync_mux);
    if (!restored) {
      jsonError(cmd.id, "no blackout to restore");
    } else {
      // Write the lit pattern first. If power drops before the blackout state
      // clears, restore remains safely repeatable after reboot.
      saveBeaconSnapshot();
      blackoutStateSave();
      jsonOk(cmd.id, "blackout restored");
    }
  } else if (cmd.kind == SJ_POWER_POLICY) {
    if (!isConductor()) {
      jsonError(cmd.id, "power policy is conductor-only");
    } else {
      powerPolicyApplyCommand(cmd);
      jsonOk(cmd.id, "power policy changed");
    }
  } else if (cmd.kind == SJ_OTA_MODE) {
    if (!isConductor()) {
      jsonError(cmd.id, "ota mode is conductor-only");
    } else {
      bool was_active = otaMaintenanceActive(now_us());
      if (!cmd.ota_enabled) otaWriteAbort();
      g_ota_maintenance = cmd.ota_enabled;
      g_ota_maintenance_until_us = cmd.ota_enabled ? now_us() + OTA_WINDOW_US : 0;
      if (cmd.ota_enabled && !was_active && !otaSessionActive()) {
        portENTER_CRITICAL(&g_ota_status_mux);
        otaStatusInit(g_ota_status);
        portEXIT_CRITICAL(&g_ota_status_mux);
      }
      jsonOk(cmd.id, cmd.ota_enabled ? "ota maintenance mode started"
                                      : "ota maintenance mode ended");
    }
  } else if (cmd.kind == SJ_OTA_BEGIN) {
    handleOtaBegin(cmd);
  } else if (cmd.kind == SJ_OTA_BEGIN_TARGETS) {
    handleOtaBeginTargets(cmd);
  } else if (cmd.kind == SJ_OTA_CHUNK) {
    handleOtaChunk(cmd);
  } else if (cmd.kind == SJ_OTA_REBROADCAST) {
    handleOtaRebroadcast(cmd);
  } else if (cmd.kind == SJ_OTA_END) {
    handleOtaEnd(cmd);
  } else if (cmd.kind == SJ_OTA_PROGRESS) {
    handleOtaProgress(cmd);
  } else if (cmd.kind == SJ_OTA_REPAIR) {
    handleOtaRepair(cmd);
  } else if (cmd.kind == SJ_OTA_RESTART) {
    handleOtaRestart(cmd);
  } else if (cmd.kind == SJ_OTA_PROBE) {
    handleOtaProbe(cmd);
  } else if (cmd.kind == SJ_OTA_ACTIVATE) {
    handleOtaActivate(cmd);
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
  } else if (!strcmp(cmd, "group")) {
    char* am = strtok(nullptr, " \t");
    char* ag = strtok(nullptr, " \t");
    uint8_t mac[6];
    int group_number = ag ? atoi(ag) : 0;
    if (!isConductor()) {
      Serial.println("? group is conductor-only");
    } else if (am && parseMac(am, mac) && group_number >= 1 &&
               group_number <= GROUP_COUNT) {
      if (tableSetGroup(g_table, mac, (uint8_t)(group_number - 1))) {
        tableSave();
        broadcastTable();
        printTable();
      } else {
        Serial.println("? unknown lantern");
      }
    } else {
      Serial.println("? group <mac> <1..8>");
    }
  } else if (!strcmp(cmd, "leds")) {
    char* am = strtok(nullptr, " \t");
    char* ac = strtok(nullptr, " \t");
    uint8_t mac[6];
    int led_count = ac ? atoi(ac) : 0;
    if (!isConductor()) {
      Serial.println("? leds is conductor-only");
    } else if (am && parseMac(am, mac) && ledCountInputValid(led_count)) {
      if (tableSetLedCount(g_table, mac, (uint8_t)led_count)) {
        tableSave();
        broadcastTable();
        printTable();
      } else {
        Serial.println("? unknown lantern");
      }
    } else {
      Serial.println("? leds <mac> <16|32|64>");
    }
  } else if (!strcmp(cmd, "forget")) {
    char* am = strtok(nullptr, " \t");
    uint8_t mac[6];
    if (!isConductor()) {
      Serial.println("? forget is conductor-only");
    } else if (am && parseMac(am, mac)) {
      if (tableClearPosition(g_table, mac)) {
        tableSave();
        broadcastTable();
        printTable();
      }
      else Serial.println("? no such mac in table");
    } else {
      Serial.println("? forget <mac>");
    }
  } else if (!strcmp(cmd, "role")) {
    char* a = strtok(nullptr, " \t");
    if (a) {
      if (!strcmp(a, "conductor") || !strcmp(a, "1")) g_role = ROLE_CONDUCTOR;
      else if (!strcmp(a, "performer") || !strcmp(a, "0")) g_role = ROLE_PERFORMER;
      else if (!strcmp(a, "relay") || !strcmp(a, "2")) g_role = ROLE_RELAY;
      else { Serial.println("? role conductor|performer|relay"); return; }
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
      portENTER_CRITICAL(&g_register_mux);
      registrationInit(g_register_schedule);
      portEXIT_CRITICAL(&g_register_mux);
      portENTER_CRITICAL(&g_performer_tx_mux);
      performerTxInit(g_performer_tx);
      portEXIT_CRITICAL(&g_performer_tx_mux);
      portENTER_CRITICAL(&g_sync_mux);
      parentRouteInit(g_parent_route);
      portEXIT_CRITICAL(&g_sync_mux);
      if (g_parent_peer.active) esp_now_del_peer(g_parent_peer.mac);
      otaPeerLeaseInit(g_parent_peer);
      portENTER_CRITICAL(&g_relay_mux);
      relayQueueInit(g_relay_queue);
      relayReceiptInit(g_relay_receipt);
      g_relay_send_pending = false;
      portEXIT_CRITICAL(&g_relay_mux);
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
      if (patterns::patternNeedsCurrentFirmware(v) &&
          (!isConductor() || !currentFirmwareFleetReady(now_us()))) {
        Serial.println("? pattern requires current firmware on every placed lantern");
        return;
      }
      if (v == patterns::UPLOADED &&
          (!isConductor() || !uploadedFleetReady(g_uploaded_target_id,
                                                 now_us()))) {
        Serial.println("? uploaded program is not verified on every placed lantern");
        return;
      }
      portENTER_CRITICAL(&g_sync_mux);
      for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++) {
        g_beacon.patterns[group_id].pattern_id = v;
        if (v == patterns::UPLOADED)
          patterns::uploadedPatternSetProgramId(
              g_beacon.patterns[group_id].params, g_uploaded_target_id);
      }
      BeaconMsg snap = g_beacon;
      portEXIT_CRITICAL(&g_sync_mux);
      patternConfigSave(snap);
      if (v == patterns::UPLOADED) g_uploaded_verification_until_us = 0;
      printInfo();
    }
  } else if (!strcmp(cmd, "bri")) {
    char* a = strtok(nullptr, " \t");
    if (a) {
      int v = atoi(a);
      if (v < 0) v = 0;
      if (v > MAX_BRIGHTNESS) v = MAX_BRIGHTNESS;  // never store above the cap
      BeaconMsg current;
      portENTER_CRITICAL(&g_sync_mux);
      current = g_beacon;
      portEXIT_CRITICAL(&g_sync_mux);
      bool firmware_ready = true;
      if (v > 0) firmware_ready = isConductor() &&
          currentFirmwareFleetReady(now_us());
      for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++) {
        const PatternConfig& pattern = current.patterns[group_id];
        if (!patterns::patternBrightnessRequiresReadiness(pattern.pattern_id,
                                                          (uint8_t)v))
          continue;
        if (!firmware_ready) {
          Serial.println("? brightness requires current firmware on every placed lantern");
          return;
        }
        if (pattern.pattern_id == patterns::UPLOADED &&
            !uploadedFleetReady(
                patterns::uploadedPatternProgramId(pattern.params), now_us())) {
          Serial.println("? brightness requires the uploaded program on every placed lantern");
          return;
        }
      }
      portENTER_CRITICAL(&g_sync_mux);
      for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++)
        g_beacon.patterns[group_id].brightness = (uint8_t)v;
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
        BeaconMsg current;
        portENTER_CRITICAL(&g_sync_mux);
        current = g_beacon;
        portEXIT_CRITICAL(&g_sync_mux);
        for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++) {
          if (!patterns::patternParamsMayChangeDirectly(
                  current.patterns[group_id].pattern_id)) {
            Serial.println("? uploaded program parameters are atomic; use program_install");
            return;
          }
        }
        portENTER_CRITICAL(&g_sync_mux);
        for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++)
          g_beacon.patterns[group_id].params[i] = v;
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
  static char buf[SERIAL_JSON_COMMAND_MAX];
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
  g_timer_wake = (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER);

  configLoad();
  g_policy_clock_set_us = now_us();
  patternConfigLoad();
  uploadedProgramsLoad();
  blackoutStateLoad();
  if (g_timer_wake && g_rtc_have_power_policy) {
    g_power_policy = g_rtc_power_policy;
    powerPolicySanitize(g_power_policy);
    g_policy_base_min = g_power_policy.current_min;
    g_policy_base_epoch_s = g_power_policy.current_epoch_s;
    g_policy_clock_set_us = now_us();
    g_beacon.power = g_power_policy;
  } else if (!g_timer_wake) {
    g_rtc_have_power_policy = false;
  }
  esp_read_mac(g_mac, ESP_MAC_WIFI_STA);  // stable identity, read from efuse
  rosterInit(g_roster);
  registrationInit(g_register_schedule);
  performerTxInit(g_performer_tx);
  parentRouteInit(g_parent_route);
  otaPeerLeaseInit(g_parent_peer);
  relayQueueInit(g_relay_queue);
  relayReceiptInit(g_relay_receipt);
  otaPeerLeaseInit(g_relay_peer);
  powerTableInit(g_power_table);
  otaStatusInit(g_ota_status);
  uploadedStatusInit(g_uploaded_status);
  uploadedStatusScheduleInit(g_uploaded_status_schedule);
  for (uint8_t group_id = 0; group_id < GROUP_COUNT; group_id++) {
    const PatternConfig& pattern = g_beacon.patterns[group_id];
    if (pattern.pattern_id != patterns::UPLOADED) continue;
    g_uploaded_status_requested_id =
        patterns::uploadedPatternProgramId(pattern.params);
    uploadedStatusScheduleRequest(g_uploaded_status_schedule, now_us(), g_mac);
    break;
  }
  otaCohortInit(g_ota_cohort);
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
  BootPlan plan = bootClassify(g_timer_wake, g_rtc_was_day, boot, BOOT_CFG);
  duskInit(g_dusk, plan.dusk_start_day, boot);
  g_dusk_earliest_us = plan.dusk_earliest_us;
  g_rtc_was_day = plan.rtc_day_flag;
  g_last_serial_us = plan.serial_seed_us;
  if (g_timer_wake)
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
    for (uint8_t i = 0; i < 3; i++) {
      otaStatusReport(/*keep_pending*/ true);
      delay(100);
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
  PatternConfig p = beaconRenderPattern(b, g_id.group_id);
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
    if (beaconLocatorActive(b) && t >= g_next_cal_roster_us) {
      broadcastCalibrationRoster();
      g_next_cal_roster_us = t + CAL_ROSTER_INTERVAL_US;
    }
    // Inventory adoption + row replies. A factory-flashed performer reports its
    // permanent number; the conductor records it once, then remains authoritative
    // and sends it back after any later erase. NVS writes stay out of the radio
    // callback. At worst an overflow retries on the next 10–12 s REGISTER.
    if (g_rowreq_n) {
      RegistrationRequest req[ROWREQ_MAX];
      uint8_t n;
      portENTER_CRITICAL(&g_roster_mux);
      n = g_rowreq_n;
      if (n) memcpy(req, g_rowreq, sizeof(RegistrationRequest) * n);
      g_rowreq_n = 0;
      portEXIT_CRITICAL(&g_roster_mux);
      bool inventory_changed = false;
      for (uint8_t i = 0; i < n; i++) {
        TableIdentityResult adopted =
            tableAdoptIdentity(g_table, req[i].mac, req[i].reported_id);
        if (adopted == TABLE_ID_ADOPTED) {
          inventory_changed = true;
          char mac[18];
          Serial.printf("[inventory] assigned %s permanent ID #%u\n",
                        macStr(req[i].mac, mac), req[i].reported_id);
        } else if (adopted == TABLE_ID_CONFLICT) {
          char mac[18];
          Serial.printf(
              "[inventory] ID CONFLICT from %s: reported #%u; "
              "keeping conductor assignment\n",
              macStr(req[i].mac, mac), req[i].reported_id);
        } else if (adopted == TABLE_ID_FULL) {
          Serial.println("[inventory] table full; could not retain reported ID");
        }
        int entry = tableFind(g_table, req[i].mac);
        bool reply = tableRowReplyWanted(
            req[i].mac_known, req[i].reported_id, req[i].reported_group,
            req[i].reported_led_count,
            entry >= 0, entry >= 0 ? g_table.entries[entry].id : 0,
            entry >= 0 ? g_table.entries[entry].group_id : 0,
            entry >= 0 ? g_table.entries[entry].led_count : DEFAULT_LED_COUNT);
        if (reply) {
          TableMsg m;
          size_t len = tableRowBuild(g_table, req[i].mac, m);
          if (len) {
            routeHeaderSet(m.hdr, g_mac, BROADCAST_ADDR);
            esp_now_send(BROADCAST_ADDR, (const uint8_t*)&m, len);
          }
        }

        bool positioned = entry >= 0 && tableHasPosition(g_table.entries[entry]);
        fallbackPatternsForMismatchedFirmware(req[i].mac, positioned, b);

        // During a verification window, repair the newly staged program. After
        // activation, repair the program assigned to this node's own group. A
        // single global target is insufficient because groups may intentionally
        // keep different uploaded programs active at the same time.
        bool verification_active =
            positioned && t < g_uploaded_verification_until_us;
        uint64_t assigned_active_id = 0;
        if (positioned) {
          const PatternConfig& assigned = beaconPattern(
              b, groupIdSafe(g_table.entries[entry].group_id));
          if (assigned.pattern_id == patterns::UPLOADED)
            assigned_active_id =
                patterns::uploadedPatternProgramId(assigned.params);
        }
        uint64_t target_id = uploadedRepairProgramId(
            g_uploaded_target_id, verification_active, assigned_active_id);
        if (target_id) {
          FirmwareVersion conductor_firmware =
              currentFirmwareVersion(PROTO_VERSION);
          FirmwareVersion roster_firmware = {};
          int64_t registered_us = 0;
          bool firmware_matches = false;
          portENTER_CRITICAL(&g_roster_mux);
          int roster_index = rosterFind(g_roster, req[i].mac);
          if (roster_index >= 0) {
            roster_firmware = rosterEntryFirmware(g_roster.entries[roster_index]);
            registered_us = g_roster.entries[roster_index].last_us;
            firmware_matches = firmwareSame(conductor_firmware,
                                            roster_firmware);
          }
          portEXIT_CRITICAL(&g_roster_mux);
          bool ready = false;
          bool after_registration = false;
          portENTER_CRITICAL(&g_uploaded_status_mux);
          int status = uploadedStatusFind(g_uploaded_status, req[i].mac);
          if (status >= 0) {
            const UploadedProgramStatusEntry& status_entry =
                g_uploaded_status.entries[status];
            ready = status_entry.vm_version == UPLOADED_VM_VERSION &&
                    status_entry.available &&
                    status_entry.requested_id == target_id &&
                    firmwareSame(status_entry.firmware, conductor_firmware) &&
                    firmwareSame(status_entry.firmware, roster_firmware);
            after_registration = status_entry.last_us >= registered_us;
          }
          portEXIT_CRITICAL(&g_uploaded_status_mux);
          bool target_active = assigned_active_id == target_id;
          UploadedRepairAction action = uploadedRepairAction(
              target_active, verification_active, firmware_matches, ready,
              after_registration);
          int slot = uploadedProgramFind(g_uploaded_programs, target_id);
          if (action == UPLOADED_REPAIR_INSTALL && slot >= 0)
            uploadedProgramSend(req[i].mac, g_uploaded_programs.slots[slot]);
          else if (action == UPLOADED_REPAIR_QUERY)
            uploadedProgramQuerySend(req[i].mac, target_id);
        }
      }
      if (inventory_changed) tableSave();
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
    bool wake_rendezvous = bootWakeRendezvousActive(
        g_timer_wake, s.beacons_rx, t, g_dusk_earliest_us);
    bool field_awake = powerPolicyForceAwake(policy);
    // Give a due registration its MAC-derived slot before dutyStep decides
    // whether this shared listen window may close. A pending slot/delivery is
    // the only registration state that extends the window.
    if (g_radio_on) maybeRegister(t);
    bool register_holds_radio = registrationHoldingRadio(t);
    bool program_holds_radio = false;
    portENTER_CRITICAL(&g_uploaded_pending_mux);
    program_holds_radio = uploadedProgramHoldsRadio(
        g_uploaded_install_pending_dirty, g_uploaded_status_schedule, t);
    portEXIT_CRITICAL(&g_uploaded_pending_mux);
    if ((otaSessionIsActive(g_ota_session) || isRelay()) && !g_radio_on)
      radioWake();
    if (field_awake && !g_radio_on) radioWake();
    if (isPerformer() && g_powersave && !otaSessionIsActive(g_ota_session) &&
        !field_awake && !register_holds_radio && !program_holds_radio) {
      DutyAction act = dutyStep(g_duty, currentDutyConfig(policy), t);
      if (act == DUTY_WAKE) radioWake();
      else if (act == DUTY_SLEEP) radioSleep();
    }
    // A duty wake above opens a fresh window; schedule its slot immediately.
    if (g_radio_on) maybeRegister(t);  // TX only when the radio is powered
    maybeInstallUploadedProgram();
    maybeUploadedStatusReport();
    maybePowerReport(t);   // no-op without the INA228; defers until radio-on
    maybeOtaStatusReport();
    maybeRelayDeliveryReceipt();
    drainRelayQueue();
    maybeAdoptTableAssignment();  // flush pending identity/position NVS adoption

    // Primary field sleep policy: when the broadcast schedule says LEDs are off,
    // clear the pixels and deep-sleep until the next check interval. A recent
    // serial session still wins, so a board on the bench stays reachable.
    if (isPerformer() && !wake_rendezvous &&
        powerPolicyShouldDeepSleep(policy) &&
        t - g_last_serial_us >= DUSK_SERIAL_GRACE_US) {
      duskEnterDeepSleep(powerPolicyDeepSleepUs(policy), policy);
    }

    // Lever 2: sample the light sensor at 1 Hz and deep-sleep through daylight.
    // Every gate here fails toward "awake" (see dusk.h): debounced day + boot
    // hold-off passed + no recent serial + no recent FIELD_AWAKE beacon. The
    // conductor never dusk-sleeps (it's the wall-powered clock anchor), and the
    // whole feature is off until `dusk on` (GPIO34 floats until wired).
    if (isPerformer() && g_dusk_on) {
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
  if (p.brightness > MAX_BRIGHTNESS) p.brightness = MAX_BRIGHTNESS;
  powerPolicySanitize(b.power);
  if (!powerPolicyLedsOn(b.power)) p.brightness = 0;

  // Conductor renders against its own clock; a performer against synced time
  // (which free-runs on the last offset when no beacon arrives).
  int64_t render_us = isConductor() ? t : syncedTime(s, t);
  uint16_t calibration_rank;
  portENTER_CRITICAL(&g_sync_mux);
  calibration_rank = g_calibration_rank;
  portEXIT_CRITICAL(&g_sync_mux);
  uint16_t render_node_id = (p.pattern_id == patterns::CALIBRATION && calibration_rank)
                                ? calibration_rank
                                : g_id.id;
  const UploadedProgram* uploaded_program = nullptr;
  bool uploaded_static = false;
  if (p.pattern_id == patterns::UPLOADED) {
    uint64_t program_id = patterns::uploadedPatternProgramId(p.params);
    int slot = uploadedProgramFindValidatedSlot(g_uploaded_programs, program_id);
    if (slot >= 0) {
      uploaded_program = &g_uploaded_programs.slots[slot];
      uploaded_static =
          !uploadedProgramUsesTimeValidated(*uploaded_program);
    } else {
      // This should be unreachable after the conductor's activation barrier.
      // Fail visibly and safely to a dim compiled Glow instead of blanking or
      // interpreting program-id words as pattern parameters.
      p.pattern_id = patterns::GLOW;
      p.params[0] = 40;
      p.params[1] = 100;
      p.params[2] = pmath::colorValuePack(128);
      p.params[3] = 0;
    }
  }
  // Static patterns (GLOW/SOLID) latch: pushing the identical frame at 60 Hz is
  // pure RMT + CPU waste, and it delays every Stage-B nap behind the CanShow()
  // wait. Re-render them only when the pattern changes, plus a ~1 Hz safety
  // refresh (self-heals a noise-glitched pixel). Animated patterns render every
  // pass as before.
  static PatternConfig last_shown = {};
  static uint8_t last_led_count = 0;
  static bool shown_once = false;
  static int64_t next_static_refresh = 0;
  bool pattern_changed = !shown_once ||
                        memcmp(&last_shown, &p, sizeof(p)) != 0 ||
                        last_led_count != g_id.led_count;
  bool pattern_static = uploaded_program ? uploaded_static
                                         : patterns::patternIsStatic(p.pattern_id);
  if (!pattern_static || pattern_changed ||
      t >= next_static_refresh) {
    if (uploaded_program) {
      patterns::renderUploaded(strip, *uploaded_program, render_us,
                               p.brightness, g_id.x, g_id.y,
                               ledCountSafe(g_id.led_count),
                               /*already_validated=*/true);
    } else {
      patterns::render(strip, p, render_us, g_id.x, g_id.y, render_node_id,
                       ledCountSafe(g_id.led_count));
    }
    strip.Show();
    last_shown = p;
    last_led_count = g_id.led_count;
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
  if (isPerformer() && g_powersave) {
    NapInputs in;
    in.now_us = now_us();
    in.synced_us = syncedTime(s, in.now_us);
    in.radio_on = g_radio_on;
    in.radio_change_at_us = g_duty.change_at_us;
    in.pattern_static = pattern_static;
    in.last_serial_us = g_last_serial_us;
    in.heartbeat_half_us = HEARTBEAT_LED ? HEARTBEAT_HALF_US : 0;
    nap = napPlan(NAP_CFG, in);
  }
  if (nap > 0) {
    // The RMT transfer from strip.Show() runs in the background — sleeping mid-
    // frame would truncate it and glitch the pixels, so wait for it to finish
    // (64 RGBW pixels ≈ 2.6 ms, a bounded wait). Same for the UART TX FIFO:
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
