// The conductor's node roster — who is in the field, keyed on MAC.
//
// Dependency-free (no Arduino, no ESP-NOW, no globals, no FreeRTOS) so the
// insert / update / overflow logic is host-testable in isolation, exactly like
// sync.h and pattern_math.h. main.cpp owns a single Roster and guards it with a
// spinlock (the recv callback writes it, loop() reads it) — the locking lives
// there, the logic lives here. The brief flags "silently-failing logic" as the
// real risk; the roster's dedup-by-MAC and the bound at ROSTER_MAX are exactly
// that kind of quietly-wrong-able code, so they get tests.
#pragma once

#include <stdint.h>
#include <string.h>

#include "firmware_version.h"
#include "config.h"

// Max nodes the conductor tracks. 60-node field + headroom; route metadata is
// retained per entry so targeted replies can select the immediate next hop.
static constexpr uint8_t ROSTER_MAX = 64;

struct RosterEntry {
  uint8_t  mac[6];
  uint16_t id;       // human label reported by the node (0 if unprovisioned)
  uint8_t  fw;       // node's PROTO_VERSION (application compatibility)
  uint32_t build;    // node's firmware build id (spot same-protocol stragglers)
  uint8_t  dirty;    // node was built from an uncommitted firmware tree
  uint8_t  role;     // runtime performer/conductor/relay role
  uint8_t  via[6];   // primary's immediate ESP-NOW next hop for this node
  uint8_t  hops;     // 0 direct, 1 through via relay
  char     version[FIRMWARE_VERSION_MAX];  // human release version
  int64_t  last_us;  // local time of this node's most recent REGISTER
};

struct Roster {
  RosterEntry entries[ROSTER_MAX];
  uint8_t     count;
};

inline void rosterInit(Roster& r) { r.count = 0; }

// Index of the entry with this MAC, or -1 if absent.
inline int rosterFind(const Roster& r, const uint8_t mac[6]) {
  for (int i = 0; i < r.count; i++)
    if (memcmp(r.entries[i].mac, mac, 6) == 0) return i;
  return -1;
}

// Insert a new node or refresh an existing one (matched by MAC). Idempotent per
// MAC: the same node re-registering updates its row in place, never duplicates.
// Returns false only when the roster is full AND the MAC is new — that
// registration is dropped rather than evicting a known node (a known MAC still
// updates even when full).
inline bool rosterUpsert(Roster& r, const uint8_t mac[6], uint16_t id, uint8_t fw,
                         uint32_t build, uint8_t dirty, const char* version,
                         int64_t t, uint8_t role = ROLE_PERFORMER,
                         const uint8_t via[6] = nullptr, uint8_t hops = 0) {
  int i = rosterFind(r, mac);
  if (i < 0) {
    if (r.count >= ROSTER_MAX) return false;
    i = r.count++;
  }
  memcpy(r.entries[i].mac, mac, 6);
  r.entries[i].id = id;
  r.entries[i].fw = fw;
  r.entries[i].build = build;
  r.entries[i].dirty = dirty;
  r.entries[i].role = role <= ROLE_RELAY ? role : ROLE_PERFORMER;
  memcpy(r.entries[i].via, via ? via : mac, 6);
  r.entries[i].hops = hops <= 1 ? hops : 1;
  firmwareCopyVersion(r.entries[i].version, version);
  r.entries[i].last_us = t;
  return true;
}

inline FirmwareVersion rosterEntryFirmware(const RosterEntry& e) {
  FirmwareVersion v = {e.fw, e.build, e.dirty, {0}};
  firmwareCopyVersion(v.version, e.version);
  return v;
}
