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
};

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

inline bool tableLookup(const LayoutTable& t, const uint8_t mac[6], float& x,
                        float& y) {
  int i = tableFind(t, mac);
  if (i < 0 || !tableHasPosition(t.entries[i])) return false;
  x = t.entries[i].x;
  y = t.entries[i].y;
  return true;
}

// Forget only deployment placement. The permanent MAC/ID inventory survives.
inline bool tableClearPosition(LayoutTable& t, const uint8_t mac[6]) {
  int i = tableFind(t, mac);
  if (i < 0 || !tableHasPosition(t.entries[i])) return false;
  t.entries[i].flags &= (uint8_t)~TABLE_FLAG_POSITIONED;
  t.entries[i].x = 0.0f;
  t.entries[i].y = 0.0f;
  return true;
}

inline bool tableRemove(LayoutTable& t, const uint8_t mac[6]) {
  int i = tableFind(t, mac);
  if (i < 0) return false;
  t.entries[i] = t.entries[--t.count];
  memset(&t.entries[t.count], 0, sizeof(t.entries[t.count]));
  return true;
}
