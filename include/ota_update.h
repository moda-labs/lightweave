#pragma once

#include <stdint.h>
#include <stddef.h>

#include "roster.h"

static constexpr uint16_t OTA_SERIAL_CHUNK_MAX = 128;
static constexpr uint8_t OTA_STATUS_MAX = 64;
// ESP-NOW broadcast has no per-recipient acknowledgement. Spread redundant
// copies across a wider window so a short RF-loss burst cannot erase an entire
// OTA chunk and permanently desynchronize the receiver's write offset.
static constexpr uint8_t OTA_RADIO_SEND_COPIES = 8;
static constexpr uint8_t OTA_RADIO_SEND_MAX_ATTEMPTS = 24;
static constexpr uint8_t OTA_RADIO_SEND_DELAY_MS = 4;
static constexpr uint8_t OTA_RADIO_REPAIR_COPIES = 4;
static constexpr uint8_t OTA_RADIO_REPAIR_MAX_ATTEMPTS = 12;
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
