// The conductor's authoritative node inventory and optional field placement.
//
// A MAC address is the immutable machine identity. `id` is the permanent number
// written on the physical board, while (x,y) is deployment-specific and may be
// cleared or moved without changing that number. The conductor persists this
// table and broadcasts it so performers can recover both identity and position
// after erased NVS.
#pragma once

#include <stdint.h>
#include <string.h>

#include "groups.h"
#include "led_profile.h"

// The live field is 60 nodes, but identities survive board replacement. Keep
// enough conductor-side history for a full second fleet without exhausting the
// inventory. Protocol-v7 used exactly 64 rows; its migration shape stays fixed
// separately below.
static constexpr uint8_t TABLE_MAX = 128;
static constexpr uint8_t LEGACY_TABLE_MAX = 64;
static constexpr uint8_t TABLE_FLAG_POSITIONED = 1 << 0;

struct TableEntry {
  uint8_t  mac[6];
  uint16_t id;
  float    x;
  float    y;
  uint8_t  flags;
  uint8_t  group_id;
  uint8_t  led_count;
};

// v9 reused one v8 trailing padding byte for group_id; v10 uses the next one for
// led_count. The table blob size and every pre-existing field offset stay fixed.
// Firmware still stamps an NVS schema version so old padding is never interpreted
// as configuration.
static_assert(sizeof(TableEntry) == 20, "TableEntry NVS layout changed");

struct LayoutTable {
  TableEntry entries[TABLE_MAX];
  uint8_t    count;
};

// Exact protocol-v7 NVS shape, retained only for one-way migration.
struct LegacyTableEntry {
  uint8_t mac[6];
  float x;
  float y;
};

struct LegacyLayoutTable {
  LegacyTableEntry entries[LEGACY_TABLE_MAX];
  uint8_t count;
};

enum TableIdentityResult : uint8_t {
  TABLE_ID_UNCHANGED = 0,
  TABLE_ID_ADOPTED,
  TABLE_ID_CONFLICT,
  TABLE_ID_FULL,
};

enum TableReserveStatus : uint8_t {
  TABLE_RESERVE_EXISTING = 0,
  TABLE_RESERVE_CREATED,
  TABLE_RESERVE_CONFLICT,
  TABLE_RESERVE_FULL,
  TABLE_RESERVE_SAVE_FAILED,
};

struct TableReserveResult {
  TableReserveStatus status;
  uint16_t id;
};

inline void tableInit(LayoutTable& t) { memset(&t, 0, sizeof(t)); }

inline int tableFind(const LayoutTable& t, const uint8_t mac[6]) {
  for (int i = 0; i < t.count; i++)
    if (memcmp(t.entries[i].mac, mac, 6) == 0) return i;
  return -1;
}

inline int tableFindId(const LayoutTable& t, uint16_t id) {
  if (id == 0) return -1;
  for (int i = 0; i < t.count; i++)
    if (t.entries[i].id == id) return i;
  return -1;
}

inline bool tableHasPosition(const TableEntry& entry) {
  return (entry.flags & TABLE_FLAG_POSITIONED) != 0;
}

inline uint8_t tablePositionedCount(const LayoutTable& t) {
  uint8_t count = 0;
  for (uint8_t i = 0; i < t.count; i++)
    if (tableHasPosition(t.entries[i])) count++;
  return count;
}

inline bool tableValid(const LayoutTable& t) {
  if (t.count > TABLE_MAX) return false;
  for (uint8_t i = 0; i < t.count; i++) {
    if (t.entries[i].flags & (uint8_t)~TABLE_FLAG_POSITIONED) return false;
    if (!groupIdValid(t.entries[i].group_id)) return false;
    if (!ledCountValid(t.entries[i].led_count)) return false;
    for (uint8_t j = 0; j < i; j++) {
      if (memcmp(t.entries[i].mac, t.entries[j].mac, 6) == 0) return false;
      if (t.entries[i].id != 0 && t.entries[i].id == t.entries[j].id)
        return false;
    }
  }
  return true;
}

inline int tableEnsure(LayoutTable& t, const uint8_t mac[6]) {
  int i = tableFind(t, mac);
  if (i >= 0) return i;
  if (t.count >= TABLE_MAX) return -1;
  i = t.count++;
  memset(&t.entries[i], 0, sizeof(t.entries[i]));
  memcpy(t.entries[i].mac, mac, 6);
  t.entries[i].led_count = DEFAULT_LED_COUNT;
  return i;
}

// Learn a non-zero ID reported by its board. Existing conductor assignments and
// IDs owned by another MAC are immutable: conflicts fail closed instead of
// silently relabeling physical hardware.
inline TableIdentityResult tableAdoptIdentity(LayoutTable& t,
                                              const uint8_t mac[6],
                                              uint16_t id) {
  if (id == 0) return TABLE_ID_UNCHANGED;
  int own = tableFind(t, mac);
  if (own >= 0 && t.entries[own].id == id) return TABLE_ID_UNCHANGED;
  if ((own >= 0 && t.entries[own].id != 0) || tableFindId(t, id) >= 0)
    return TABLE_ID_CONFLICT;
  if (own < 0) {
    own = tableEnsure(t, mac);
    if (own < 0) return TABLE_ID_FULL;
  }
  t.entries[own].id = id;
  return TABLE_ID_ADOPTED;
}

// Reserve the lowest unused permanent ID for a MAC. The conductor is the only
// allocator, so a Pi station and a laptop station can never independently hand
// the same physical number to different boards.
inline TableReserveResult tableReserveIdentity(LayoutTable& t,
                                               const uint8_t mac[6]) {
  int own = tableFind(t, mac);
  if (own >= 0 && t.entries[own].id != 0)
    return {TABLE_RESERVE_EXISTING, t.entries[own].id};

  uint16_t id = 1;
  while (tableFindId(t, id) >= 0) id++;

  if (own < 0) {
    own = tableEnsure(t, mac);
    if (own < 0) return {TABLE_RESERVE_FULL, 0};
  }
  t.entries[own].id = id;
  return {TABLE_RESERVE_CREATED, id};
}

inline bool tableSet(LayoutTable& t, const uint8_t mac[6], float x, float y) {
  int i = tableEnsure(t, mac);
  if (i < 0) return false;
  t.entries[i].x = x;
  t.entries[i].y = y;
  t.entries[i].flags |= TABLE_FLAG_POSITIONED;
  return true;
}

inline bool tableMigrateLegacy(const LegacyLayoutTable& legacy,
                               LayoutTable& current) {
  tableInit(current);
  if (legacy.count > LEGACY_TABLE_MAX) return false;
  for (uint8_t i = 0; i < legacy.count; i++) {
    if (!tableSet(current, legacy.entries[i].mac, legacy.entries[i].x,
                  legacy.entries[i].y)) {
      tableInit(current);
      return false;
    }
  }
  return true;
}

// True when a live report disagrees with this inventory or claims an ID already
// owned by another MAC. Used by the conductor UI to make conflicts explicit
// instead of rendering two boards with the same physical label.
inline bool tableReportedIdConflict(const LayoutTable& t,
                                    const uint8_t mac[6], uint16_t reported_id) {
  if (reported_id == 0) return false;
  int own = tableFind(t, mac);
  if (own >= 0 && t.entries[own].id != 0 &&
      t.entries[own].id != reported_id) {
    return true;
  }
  int owner = tableFindId(t, reported_id);
  return owner >= 0 && memcmp(t.entries[owner].mac, mac, 6) != 0;
}

// Insert/update a complete row. Position-only edits above deliberately preserve
// the current group; this variant is used by replacement/migration paths that
// need to set membership atomically with the position.
inline bool tableSetWithGroup(LayoutTable& t, const uint8_t mac[6], float x,
                              float y, uint8_t group_id) {
  if (group_id >= GROUP_COUNT) return false;
  if (!tableSet(t, mac, x, y)) return false;
  t.entries[tableFind(t, mac)].group_id = group_id;
  return true;
}

// Assign any inventoried lantern to a group without touching position.
inline bool tableSetGroup(LayoutTable& t, const uint8_t mac[6],
                          uint8_t group_id) {
  if (group_id >= GROUP_COUNT) return false;
  int i = tableEnsure(t, mac);
  if (i < 0) return false;
  t.entries[i].group_id = group_id;
  return true;
}

// Change the physical emitter count without touching identity, placement, or
// show group. Hardware profiles belong to boards, not field positions.
inline bool tableSetLedCount(LayoutTable& t, const uint8_t mac[6],
                             uint8_t led_count) {
  if (!ledCountValid(led_count)) return false;
  int i = tableEnsure(t, mac);
  if (i < 0) return false;
  t.entries[i].led_count = led_count;
  return true;
}

// Look up a node's position. Writes x,y and returns true when present.
inline bool tableLookup(const LayoutTable& t, const uint8_t mac[6], float& x,
                        float& y) {
  int i = tableFind(t, mac);
  if (i < 0 || !tableHasPosition(t.entries[i])) return false;
  x = t.entries[i].x;
  y = t.entries[i].y;
  return true;
}

// Forget only deployment placement. Permanent identity and group membership
// survive so a spare can be organized before it receives coordinates.
inline bool tableClearPosition(LayoutTable& t, const uint8_t mac[6]) {
  int i = tableFind(t, mac);
  if (i < 0 || !tableHasPosition(t.entries[i])) return false;
  t.entries[i].flags &= (uint8_t)~TABLE_FLAG_POSITIONED;
  t.entries[i].x = 0.0f;
  t.entries[i].y = 0.0f;
  return true;
}

inline bool tableLookupGroup(const LayoutTable& t, const uint8_t mac[6],
                             uint8_t& group_id) {
  int i = tableFind(t, mac);
  if (i < 0) return false;
  group_id = groupIdSafe(t.entries[i].group_id);
  return true;
}

inline bool tableLookupLedCount(const LayoutTable& t, const uint8_t mac[6],
                                uint8_t& led_count) {
  int i = tableFind(t, mac);
  if (i < 0) return false;
  led_count = ledCountSafe(t.entries[i].led_count);
  return true;
}

inline bool tableRemove(LayoutTable& t, const uint8_t mac[6]) {
  int i = tableFind(t, mac);
  if (i < 0) return false;
  t.entries[i] = t.entries[--t.count];
  memset(&t.entries[t.count], 0, sizeof(t.entries[t.count]));
  return true;
}

// Apply the complete reserve-id command and acknowledge a new identity only
// after its durable save succeeds. A failed save rolls the in-memory mutation
// back so the next request cannot mistake a volatile assignment for a durable
// one. The callback keeps persistence hardware out of this testable module.
template <typename SaveTable>
inline TableReserveResult tableReserveDurably(LayoutTable& t,
                                              const uint8_t mac[6],
                                              uint16_t reported_id,
                                              SaveTable save) {
  int own_before = tableFind(t, mac);
  TableReserveResult result = {TABLE_RESERVE_EXISTING, 0};
  if (reported_id != 0) {
    TableIdentityResult adopted = tableAdoptIdentity(t, mac, reported_id);
    if (adopted == TABLE_ID_CONFLICT)
      return {TABLE_RESERVE_CONFLICT, 0};
    if (adopted == TABLE_ID_FULL) return {TABLE_RESERVE_FULL, 0};
    int own = tableFind(t, mac);
    result = {adopted == TABLE_ID_ADOPTED ? TABLE_RESERVE_CREATED
                                         : TABLE_RESERVE_EXISTING,
              static_cast<uint16_t>(own >= 0 ? t.entries[own].id : 0)};
  } else {
    result = tableReserveIdentity(t, mac);
  }
  if (result.status != TABLE_RESERVE_CREATED || save(t)) return result;
  if (own_before >= 0) {
    t.entries[own_before].id = 0;
  } else {
    tableRemove(t, mac);
  }
  return {TABLE_RESERVE_SAVE_FAILED, 0};
}
