#pragma once

#include <stdint.h>
#include <stddef.h>

#include "roster.h"

static constexpr uint16_t OTA_SERIAL_CHUNK_MAX = 128;
static constexpr uint8_t OTA_STATUS_MAX = 64;
// The first pass uses moderate redundancy for throughput. Single control
// packets and checkpoint rebroadcasts use the stronger setting: losing BEGIN
// is expensive, while a robust shared repair is still cheaper than replaying
// the same suffix separately to several performers.
static constexpr uint8_t OTA_RADIO_SEND_COPIES = 6;
static constexpr uint8_t OTA_RADIO_SEND_MAX_ATTEMPTS = 10;
static constexpr uint8_t OTA_RADIO_STRONG_COPIES = 8;
static constexpr uint8_t OTA_RADIO_STRONG_MAX_ATTEMPTS = 12;
static constexpr uint8_t OTA_RADIO_SEND_DELAY_MS = 4;
// Arduino Update buffers one flash sector, then performs a comparatively long
// erase/write while processing the packet that completes that sector. Give all
// receivers time to leave that critical section before the next unique chunk;
// otherwise tightly-spaced copies of that next chunk are lost together.
static constexpr uint16_t OTA_FLASH_SECTOR_BYTES = 4096;
static constexpr uint16_t OTA_FLASH_SETTLE_MS = 80;
// Targeted repair is unicast and waits for the ESP-NOW delivery callback, so
// one confirmed delivery is sufficient. A failed callback is retried here and
// any remaining gap is caught again by the next CRC checkpoint.
static constexpr uint8_t OTA_RADIO_REPAIR_COPIES = 1;
static constexpr uint8_t OTA_RADIO_REPAIR_MAX_ATTEMPTS = 4;
static constexpr uint16_t OTA_RADIO_UNICAST_ACK_TIMEOUT_MS = 100;
// Performer status reports are deterministically spread across this many
// millisecond slots. IDs are unique across the inventoried field, so the
// checkpoint query does not make every performer answer at once.
static constexpr uint16_t OTA_STATUS_SLOT_MS = 4;
static constexpr uint16_t OTA_STATUS_SLOT_COUNT = OTA_STATUS_MAX;

enum OtaStatusPhase : uint8_t {
  OTA_PHASE_IDLE = 0,
  OTA_PHASE_BEGIN = 1,
  OTA_PHASE_WRITING = 2,
  OTA_PHASE_COMPLETE = 3,
  OTA_PHASE_ERROR = 4,
  OTA_PHASE_REPAIRING = 5,
  OTA_PHASE_STAGED = 6,
  OTA_PHASE_ACTIVATING = 7,
};

enum OtaStatusError : uint8_t {
  OTA_ERR_NONE = 0,
  OTA_ERR_BEGIN_FAILED = 1,
  OTA_ERR_OFFSET_MISMATCH = 2,
  OTA_ERR_OVERFLOW = 3,
  OTA_ERR_WRITE_FAILED = 4,
  OTA_ERR_INCOMPLETE = 5,
  OTA_ERR_CRC_MISMATCH = 6,
  OTA_ERR_END_FAILED = 7,
};

enum OtaChunkDecision : uint8_t {
  OTA_CHUNK_ACCEPT = 0,
  OTA_CHUNK_DUPLICATE = 1,
  OTA_CHUNK_OFFSET_MISMATCH = 2,
  OTA_CHUNK_OVERFLOW = 3,
};

enum OtaFinalizeEvent : uint8_t {
  OTA_FINALIZE_ON_END = 0,
  OTA_FINALIZE_ON_ACTIVATE = 1,
};

// One node can either own a local Update writer or coordinate an exact remote
// cohort. Keeping those modes mutually exclusive makes the safety boundary
// testable on the host: a targeted session can never accidentally take the
// local-writer/broadcast branch through a stale combination of booleans.
enum OtaSessionMode : uint8_t {
  OTA_SESSION_IDLE = 0,
  OTA_SESSION_LOCAL_WRITING = 1,
  OTA_SESSION_LOCAL_STAGED = 2,
  OTA_SESSION_TARGETED_WRITING = 3,
  OTA_SESSION_TARGETED_STAGED = 4,
  OTA_SESSION_LOCAL_STAGED_WRITER = 5,
};

inline bool otaSessionIsActive(OtaSessionMode mode) {
  return mode != OTA_SESSION_IDLE;
}

inline bool otaSessionIsWriting(OtaSessionMode mode) {
  return mode == OTA_SESSION_LOCAL_WRITING ||
         mode == OTA_SESSION_TARGETED_WRITING;
}

inline bool otaSessionIsStaged(OtaSessionMode mode) {
  return mode == OTA_SESSION_LOCAL_STAGED ||
         mode == OTA_SESSION_TARGETED_STAGED ||
         mode == OTA_SESSION_LOCAL_STAGED_WRITER;
}

inline bool otaSessionIsTargeted(OtaSessionMode mode) {
  return mode == OTA_SESSION_TARGETED_WRITING ||
         mode == OTA_SESSION_TARGETED_STAGED;
}

inline bool otaSessionOwnsLocalWriter(OtaSessionMode mode) {
  return mode == OTA_SESSION_LOCAL_WRITING ||
         mode == OTA_SESSION_LOCAL_STAGED_WRITER;
}

inline OtaSessionMode otaSessionBegin(bool targeted) {
  return targeted ? OTA_SESSION_TARGETED_WRITING : OTA_SESSION_LOCAL_WRITING;
}

inline bool otaSessionStage(OtaSessionMode& mode,
                            bool retain_local_writer = false) {
  if (mode == OTA_SESSION_LOCAL_WRITING) {
    mode = retain_local_writer ? OTA_SESSION_LOCAL_STAGED_WRITER
                               : OTA_SESSION_LOCAL_STAGED;
    return true;
  }
  if (mode == OTA_SESSION_TARGETED_WRITING) {
    mode = OTA_SESSION_TARGETED_STAGED;
    return true;
  }
  return false;
}

// Update.end() selects the newly written ESP32 partition for the next boot.
// Performers can do that as soon as their image verifies because activation
// follows immediately. The conductor must defer it until its explicit final
// activation so an incidental reset cannot install it ahead of the field.
inline bool otaShouldFinalizeFlash(bool is_conductor,
                                   OtaFinalizeEvent event) {
  return is_conductor
      ? event == OTA_FINALIZE_ON_ACTIVATE
      : event == OTA_FINALIZE_ON_END;
}

inline OtaChunkDecision otaChunkDecision(uint32_t written, uint32_t size,
                                         uint32_t offset, uint32_t len) {
  if (offset < written && offset + len <= written) return OTA_CHUNK_DUPLICATE;
  if (offset != written) return OTA_CHUNK_OFFSET_MISMATCH;
  if (written + len > size) return OTA_CHUNK_OVERFLOW;
  return OTA_CHUNK_ACCEPT;
}

inline uint16_t otaStatusSlot(uint16_t node_id, const uint8_t mac[6]) {
  if (node_id > 0) return (uint16_t)((node_id - 1) % OTA_STATUS_SLOT_COUNT);
  uint16_t hash = 0;
  for (uint8_t i = 0; i < 6; i++) hash = (uint16_t)((hash * 33U) ^ mac[i]);
  return (uint16_t)(hash % OTA_STATUS_SLOT_COUNT);
}

inline uint32_t otaStatusDelayMs(uint16_t node_id, const uint8_t mac[6]) {
  return (uint32_t)otaStatusSlot(node_id, mac) * OTA_STATUS_SLOT_MS;
}

inline uint16_t otaExpectedChunkLen(uint32_t size, uint32_t offset) {
  if (offset >= size) return 0;
  uint32_t remaining = size - offset;
  return remaining < OTA_SERIAL_CHUNK_MAX
      ? (uint16_t)remaining
      : OTA_SERIAL_CHUNK_MAX;
}

inline bool otaFlashSettleDue(uint32_t offset, uint16_t len) {
  return len > 0 && ((offset + len) % OTA_FLASH_SECTOR_BYTES) == 0;
}

struct OtaNodeStatusEntry {
  uint8_t mac[6];
  uint8_t phase;
  uint8_t error;
  uint32_t offset;
  uint32_t crc32;
  int64_t last_us;
};

struct OtaStatusTable {
  OtaNodeStatusEntry entries[OTA_STATUS_MAX];
  uint8_t count;
};

// The performers required to acknowledge one specific OTA install. The
// conductor freezes this set from fresh, placed roster entries at ota_begin;
// layout rows that are offline at that moment are deferred to a later run.
struct OtaCohort {
  uint8_t macs[OTA_STATUS_MAX][6];
  uint8_t count;
};

struct OtaPeerLease {
  uint8_t mac[6];
  bool active;
};

enum OtaSendAckState : uint8_t {
  OTA_SEND_ACK_IDLE = 0,
  OTA_SEND_ACK_PENDING = 1,
  OTA_SEND_ACK_SUCCESS = 2,
  OTA_SEND_ACK_FAILED = 3,
};

struct OtaSendAck {
  uint8_t mac[6];
  uint8_t state;
};

struct OtaFrameAckWait {
  uint8_t mac[6];
  uint8_t type;
  uint32_t token;
  uint8_t state;
};

static_assert(sizeof(OtaCohort) <= 512,
              "OTA cohort must remain safe for the ESP32 loop-task stack");

inline void otaCohortInit(OtaCohort& c) { c.count = 0; }

inline void otaPeerLeaseInit(OtaPeerLease& lease) {
  memset(lease.mac, 0, sizeof(lease.mac));
  lease.active = false;
}

inline bool otaPeerLeaseMatches(const OtaPeerLease& lease,
                                const uint8_t mac[6]) {
  return lease.active && memcmp(lease.mac, mac, 6) == 0;
}

inline void otaPeerLeaseSet(OtaPeerLease& lease, const uint8_t mac[6]) {
  memcpy(lease.mac, mac, 6);
  lease.active = true;
}

inline void otaSendAckInit(OtaSendAck& ack) {
  memset(ack.mac, 0, sizeof(ack.mac));
  ack.state = OTA_SEND_ACK_IDLE;
}

inline void otaSendAckBegin(OtaSendAck& ack, const uint8_t mac[6]) {
  memcpy(ack.mac, mac, 6);
  ack.state = OTA_SEND_ACK_PENDING;
}

inline bool otaSendAckComplete(OtaSendAck& ack, const uint8_t mac[6],
                               bool success) {
  if (ack.state != OTA_SEND_ACK_PENDING || memcmp(ack.mac, mac, 6) != 0)
    return false;
  ack.state = success ? OTA_SEND_ACK_SUCCESS : OTA_SEND_ACK_FAILED;
  return true;
}

inline void otaFrameAckInit(OtaFrameAckWait& ack) {
  memset(&ack, 0, sizeof(ack));
  ack.state = OTA_SEND_ACK_IDLE;
}

inline void otaFrameAckBegin(OtaFrameAckWait& ack, const uint8_t mac[6],
                             uint8_t type, uint32_t token) {
  memcpy(ack.mac, mac, 6);
  ack.type = type;
  ack.token = token;
  ack.state = OTA_SEND_ACK_PENDING;
}

inline bool otaFrameAckComplete(OtaFrameAckWait& ack, const uint8_t mac[6],
                                uint8_t type, uint32_t token, bool success) {
  if (ack.state != OTA_SEND_ACK_PENDING || memcmp(ack.mac, mac, 6) != 0 ||
      ack.type != type || ack.token != token) return false;
  ack.state = success ? OTA_SEND_ACK_SUCCESS : OTA_SEND_ACK_FAILED;
  return true;
}

inline int otaCohortFind(const OtaCohort& c, const uint8_t mac[6]) {
  for (int i = 0; i < c.count; i++) {
    bool same = true;
    for (uint8_t j = 0; j < 6; j++) {
      if (c.macs[i][j] != mac[j]) {
        same = false;
        break;
      }
    }
    if (same) return i;
  }
  return -1;
}

inline bool otaCohortContains(const OtaCohort& c, const uint8_t mac[6]) {
  return otaCohortFind(c, mac) >= 0;
}

inline bool otaCohortAdd(OtaCohort& c, const uint8_t mac[6]) {
  if (otaCohortContains(c, mac)) return true;
  if (c.count >= OTA_STATUS_MAX) return false;
  for (uint8_t j = 0; j < 6; j++) c.macs[c.count][j] = mac[j];
  c.count++;
  return true;
}

inline bool otaSeenRecently(int64_t last_us, int64_t now_us,
                            int64_t max_age_us) {
  if (last_us <= 0 || now_us < last_us || max_age_us < 0) return false;
  return now_us - last_us <= max_age_us;
}

inline void otaCohortSelectFresh(OtaCohort& cohort, const Roster& roster,
                                 const uint8_t self_mac[6], int64_t now_us,
                                 int64_t max_age_us) {
  otaCohortInit(cohort);
  for (uint8_t i = 0; i < roster.count; i++) {
    const RosterEntry& entry = roster.entries[i];
    if (memcmp(entry.mac, self_mac, 6) != 0 &&
        otaSeenRecently(entry.last_us, now_us, max_age_us)) {
      otaCohortAdd(cohort, entry.mac);
    }
  }
}

// Freeze an operator-selected subset, but only if every requested target is a
// fresh roster member. Returning false rejects the entire request so a typo or
// stale route cannot silently shrink a safety-critical OTA cohort.
inline bool otaCohortSelectRequestedFresh(
    OtaCohort& cohort, const OtaCohort& requested, const Roster& roster,
    const uint8_t self_mac[6], int64_t now_us, int64_t max_age_us) {
  otaCohortInit(cohort);
  if (requested.count == 0) return false;
  for (uint8_t i = 0; i < requested.count; i++) {
    const uint8_t* mac = requested.macs[i];
    int roster_index = rosterFind(roster, mac);
    if (memcmp(mac, self_mac, 6) == 0 || roster_index < 0 ||
        !otaSeenRecently(roster.entries[roster_index].last_us, now_us,
                         max_age_us) ||
        !otaCohortAdd(cohort, mac)) {
      otaCohortInit(cohort);
      return false;
    }
  }
  return cohort.count == requested.count;
}

inline void otaStatusInit(OtaStatusTable& t) { t.count = 0; }

inline int otaStatusFind(const OtaStatusTable& t, const uint8_t mac[6]) {
  for (int i = 0; i < t.count; i++) {
    bool same = true;
    for (uint8_t j = 0; j < 6; j++) {
      if (t.entries[i].mac[j] != mac[j]) {
        same = false;
        break;
      }
    }
    if (same) return i;
  }
  return -1;
}

inline bool otaStatusUpsert(OtaStatusTable& t, const uint8_t mac[6],
                            uint8_t phase, uint8_t error, uint32_t offset,
                            uint32_t crc32, int64_t last_us) {
  int i = otaStatusFind(t, mac);
  if (i < 0) {
    if (t.count >= OTA_STATUS_MAX) return false;
    i = t.count++;
  }
  for (uint8_t j = 0; j < 6; j++) t.entries[i].mac[j] = mac[j];
  t.entries[i].phase = phase;
  t.entries[i].error = error;
  t.entries[i].offset = offset;
  t.entries[i].crc32 = crc32;
  t.entries[i].last_us = last_us;
  return true;
}

inline bool otaStatusEntryComplete(const OtaNodeStatusEntry& e,
                                   uint32_t expected_size,
                                   uint32_t expected_crc32,
                                   int64_t now_us,
                                   int64_t max_age_us) {
  if (e.phase != OTA_PHASE_COMPLETE || e.error != OTA_ERR_NONE) return false;
  if (e.offset != expected_size || e.crc32 != expected_crc32) return false;
  if (max_age_us <= 0) return true;
  if (e.last_us <= 0 || now_us < e.last_us) return false;
  return (now_us - e.last_us) <= max_age_us;
}

inline bool otaStatusEntryStaged(const OtaNodeStatusEntry& e,
                                 uint32_t expected_size,
                                 uint32_t expected_crc32,
                                 int64_t now_us,
                                 int64_t max_age_us) {
  if (e.phase != OTA_PHASE_STAGED && e.phase != OTA_PHASE_ACTIVATING &&
      e.phase != OTA_PHASE_COMPLETE) {
    return false;
  }
  if (e.error != OTA_ERR_NONE || e.offset != expected_size ||
      e.crc32 != expected_crc32) {
    return false;
  }
  if (max_age_us <= 0) return true;
  return otaSeenRecently(e.last_us, now_us, max_age_us);
}

inline bool otaStatusEntryAtCheckpoint(const OtaNodeStatusEntry& e,
                                       uint32_t expected_offset,
                                       uint32_t expected_crc32,
                                       int64_t now_us,
                                       int64_t max_age_us) {
  if (e.phase == OTA_PHASE_ERROR) return false;
  if (e.offset != expected_offset || e.crc32 != expected_crc32) return false;
  if (max_age_us <= 0) return true;
  return otaSeenRecently(e.last_us, now_us, max_age_us);
}

inline bool otaStatusCompleteForMac(const OtaStatusTable& t,
                                    const uint8_t mac[6],
                                    uint32_t expected_size,
                                    uint32_t expected_crc32,
                                    int64_t now_us,
                                    int64_t max_age_us) {
  int i = otaStatusFind(t, mac);
  if (i < 0) return false;
  return otaStatusEntryComplete(t.entries[i], expected_size, expected_crc32,
                                now_us, max_age_us);
}

inline bool otaCohortComplete(const OtaStatusTable& status,
                              const OtaCohort& cohort,
                              uint32_t expected_size,
                              uint32_t expected_crc32,
                              int64_t now_us,
                              int64_t max_age_us) {
  if (cohort.count == 0) return false;
  for (uint8_t i = 0; i < cohort.count; i++) {
    if (!otaStatusCompleteForMac(status, cohort.macs[i], expected_size,
                                 expected_crc32, now_us, max_age_us)) {
      return false;
    }
  }
  return true;
}

inline bool otaCohortStaged(const OtaStatusTable& status,
                            const OtaCohort& cohort,
                            uint32_t expected_size,
                            uint32_t expected_crc32,
                            int64_t now_us,
                            int64_t max_age_us) {
  if (cohort.count == 0) return false;
  for (uint8_t i = 0; i < cohort.count; i++) {
    int status_index = otaStatusFind(status, cohort.macs[i]);
    if (status_index < 0 ||
        !otaStatusEntryStaged(status.entries[status_index], expected_size,
                              expected_crc32, now_us, max_age_us)) {
      return false;
    }
  }
  return true;
}

inline const char* otaPhaseName(uint8_t phase) {
  switch (phase) {
    case OTA_PHASE_BEGIN: return "begin";
    case OTA_PHASE_WRITING: return "writing";
    case OTA_PHASE_REPAIRING: return "repairing";
    case OTA_PHASE_STAGED: return "staged";
    case OTA_PHASE_ACTIVATING: return "activating";
    case OTA_PHASE_COMPLETE: return "complete";
    case OTA_PHASE_ERROR: return "failed";
    default: return "idle";
  }
}

inline const char* otaErrorName(uint8_t error) {
  switch (error) {
    case OTA_ERR_BEGIN_FAILED: return "begin failed";
    case OTA_ERR_OFFSET_MISMATCH: return "chunk offset mismatch";
    case OTA_ERR_OVERFLOW: return "chunk exceeds image size";
    case OTA_ERR_WRITE_FAILED: return "flash write failed";
    case OTA_ERR_INCOMPLETE: return "image incomplete";
    case OTA_ERR_CRC_MISMATCH: return "crc mismatch";
    case OTA_ERR_END_FAILED: return "finalize failed";
    default: return "none";
  }
}

inline uint32_t otaCrc32Update(uint32_t crc, const uint8_t* data, size_t len) {
  crc = ~crc;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; bit++) {
      uint32_t mask = 0 - (crc & 1U);
      crc = (crc >> 1) ^ (0xEDB88320UL & mask);
    }
  }
  return ~crc;
}

inline int8_t otaHexNibble(char c) {
  if (c >= '0' && c <= '9') return (int8_t)(c - '0');
  if (c >= 'a' && c <= 'f') return (int8_t)(c - 'a' + 10);
  if (c >= 'A' && c <= 'F') return (int8_t)(c - 'A' + 10);
  return -1;
}

inline bool otaHexDecode(const char* hex, uint8_t* out, size_t out_cap,
                         size_t& out_len) {
  out_len = 0;
  size_t n = 0;
  while (hex[n]) n++;
  if ((n % 2) != 0) return false;
  if (n / 2 > out_cap) return false;
  for (size_t i = 0; i < n; i += 2) {
    int8_t hi = otaHexNibble(hex[i]);
    int8_t lo = otaHexNibble(hex[i + 1]);
    if (hi < 0 || lo < 0) return false;
    out[out_len++] = (uint8_t)((hi << 4) | lo);
  }
  return true;
}
