// The node inventory on the wire: chunking, validation, and broadcast cadence.
//
// table.h is the pure MAC->ID+optional-position+group store; beacon.h is the
// packet layout.
// This header is everything in between — the logic that used to live inline in
// main.cpp's broadcastTable()/onRecv() where it was host-unreachable:
//   - sender side: how many MSG_TABLE chunks a table needs, and filling one
//     chunk (header, indices, rows, exact wire length);
//   - receiver side: validating a chunk's length against its declared row
//     count BEFORE trusting it, and scanning it for this node's own row;
//   - targeted delivery: deciding when a REGISTER earns an immediate
//     single-row reply, and building that reply (see the section comment).
// Dependency-free beyond sibling pure headers; main.cpp owns the radio calls.
#pragma once

#include <stddef.h>  // offsetof
#include <stdint.h>
#include <string.h>

#include "beacon.h"
#include "config.h"  // BEACON_MAGIC
#include "table.h"

// ---- Sender side ---------------------------------------------------------

// Exact wire length of a MSG_TABLE packet carrying n rows.
inline size_t tableMsgWireLen(uint8_t n) {
  return offsetof(TableMsg, rows) + (size_t)n * sizeof(TableRow);
}

// Number of MSG_TABLE chunks needed to carry `count` rows (0 for an empty
// table — nothing to advertise).
inline uint8_t tableChunkCount(uint8_t count) {
  return (uint8_t)((count + TABLE_ROWS_PER_MSG - 1) / TABLE_ROWS_PER_MSG);
}

// Fill chunk `c` of the table into `m` (full header + chunk indices + rows).
// Returns the wire length to send, or 0 when `c` is out of range (which
// includes every c for an empty table).
inline size_t tableChunkBuild(const LayoutTable& t, uint8_t c, TableMsg& m) {
  uint8_t chunks = tableChunkCount(t.count);
  if (c >= chunks) return 0;
  m.hdr.magic = BEACON_MAGIC;
  m.hdr.version = PROTO_VERSION;
  m.hdr.type = MSG_TABLE;
  m.chunk = c;
  m.chunks = chunks;
  uint8_t start = (uint8_t)(c * TABLE_ROWS_PER_MSG);
  uint8_t n = (uint8_t)(t.count - start);
  if (n > TABLE_ROWS_PER_MSG) n = TABLE_ROWS_PER_MSG;
  m.n = n;
  for (uint8_t i = 0; i < n; i++) {
    memcpy(m.rows[i].mac, t.entries[start + i].mac, 6);
    m.rows[i].id = t.entries[start + i].id;
    m.rows[i].flags = t.entries[start + i].flags;
    m.rows[i].group_id = groupIdSafe(t.entries[start + i].group_id);
    m.rows[i].x = t.entries[start + i].x;
    m.rows[i].y = t.entries[start + i].y;
  }
  return tableMsgWireLen(n);
}

// ---- Receiver side ---------------------------------------------------------

// Validation step 1, BEFORE copying the packet into a TableMsg: is the length
// inside the struct's bounds at all (header + counts present, no overrun)?
inline bool tableMsgLenPlausible(int len) {
  return len >= (int)offsetof(TableMsg, rows) && len <= (int)sizeof(TableMsg);
}

// Validation step 2, after the copy: the declared row count must be in range
// and match the wire length exactly — rejects truncation, padding, and an `n`
// that would overrun rows[].
inline bool tableMsgLenValid(int len, uint8_t n) {
  if (n > TABLE_ROWS_PER_MSG) return false;
  return len == (int)tableMsgWireLen(n);
}

struct TableAssignment {
  uint16_t id;
  float x;
  float y;
  bool has_position;
  uint8_t group_id;
};

enum TableIdentityDecision : uint8_t {
  TABLE_ID_KEEP_LOCAL = 0,
  TABLE_ID_ADOPT_AUTHORITY,
  TABLE_ID_AUTHORITY_CONFLICT,
};

// A conductor can rehydrate an erased board, but it may never silently relabel
// a board that already carries a different non-zero permanent number.
inline TableIdentityDecision tableIdentityDecision(uint16_t local_id,
                                                   uint16_t authoritative_id) {
  if (authoritative_id == 0 || authoritative_id == local_id)
    return TABLE_ID_KEEP_LOCAL;
  if (local_id == 0) return TABLE_ID_ADOPT_AUTHORITY;
  return TABLE_ID_AUTHORITY_CONFLICT;
}

// Scan a validated chunk for this node's own inventory row. Reads packed
// members by value, never by reference.
inline bool tableMsgFindRow(const TableMsg& m, const uint8_t mac[6],
                            TableAssignment& assignment) {
  uint8_t n = m.n > TABLE_ROWS_PER_MSG ? TABLE_ROWS_PER_MSG : m.n;
  for (uint8_t i = 0; i < n; i++) {
    if (memcmp(m.rows[i].mac, mac, 6) == 0) {
      assignment.id = m.rows[i].id;
      assignment.x = m.rows[i].x;
      assignment.y = m.rows[i].y;
      assignment.has_position = (m.rows[i].flags & TABLE_FLAG_POSITIONED) != 0;
      assignment.group_id = groupIdSafe(m.rows[i].group_id);
      return true;
    }
  }
  return false;
}

// ---- Targeted row reply -----------------------------------------------------
// The steady-state rebroadcast is slow (TABLE_INTERVAL_US, 60 s) because nodes
// cache their position in NVS — but a node that LACKS its position (first join,
// erase_flash recovery) must not wait out that cadence through a ~13% radio
// duty cycle. A REGISTER is the one moment the conductor provably knows the
// sender's radio is on RIGHT NOW (TX is gated on radio-up), so the fix is a
// targeted reply: broadcast just that node's row (27 B) immediately. Any
// single broadcast the node missed is retried for free by its next REGISTER
// (10 s cadence), so delivery needs no scheduler, no burst flag, and no
// rate-limit machinery.

// Should a REGISTER trigger a row reply? Reply when the sender is new to the
// roster (first join since conductor boot, or a REGISTER dropped by a full
// roster — mac_known is computed BEFORE the upsert, so a full roster can't
// mask a new node), is unprovisioned, or disagrees with the conductor's
// permanent ID or group. Provisioned known nodes that agree get no reply, so
// steady state costs zero targeted traffic.
inline bool tableRowReplyWanted(bool mac_known, uint16_t reported_id,
                                uint8_t reported_group,
                                bool have_authoritative,
                                uint16_t authoritative_id,
                                uint8_t authoritative_group) {
  return !mac_known || reported_id == 0 ||
         (have_authoritative &&
          (reported_id != authoritative_id ||
           groupIdSafe(reported_group) != groupIdSafe(authoritative_group)));
}

// Fill `m` with a single-row MSG_TABLE carrying this MAC's permanent ID and
// optional position. Returns 0 only when the MAC is not inventoried.
// Receivers treat it exactly like a full-table chunk: scan for their own MAC.
inline size_t tableRowBuild(const LayoutTable& t, const uint8_t mac[6],
                            TableMsg& m) {
  int i = tableFind(t, mac);
  if (i < 0) return 0;
  m.hdr.magic = BEACON_MAGIC;
  m.hdr.version = PROTO_VERSION;
  m.hdr.type = MSG_TABLE;
  m.chunk = 0;
  m.chunks = 1;
  m.n = 1;
  memcpy(m.rows[0].mac, mac, 6);
  m.rows[0].id = t.entries[i].id;
  m.rows[0].flags = t.entries[i].flags;
  m.rows[0].group_id = groupIdSafe(t.entries[i].group_id);
  m.rows[0].x = t.entries[i].x;
  m.rows[0].y = t.entries[i].y;
  return tableMsgWireLen(1);
}
