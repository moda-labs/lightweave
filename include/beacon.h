// Wire protocol for the ESP-NOW messages nodes exchange.
//
// Every packet starts with a common routed MsgHeader. The
// receiver validates magic + version, then dispatches on `type` to the matching
// payload struct. The clock beacon (MSG_BEACON) is the hot path — broadcast a few
// times a second and followed by every performer — so it stays small. Everything
// else (REGISTER, and later ROSTER/TABLE) is occasional control traffic.
//
// All structs are packed so the wire layout is identical on every node
// regardless of compiler padding, and every message stays at or below the
// 250-byte ESP-NOW payload limit. The magic constant lives in config.h
// (BEACON_MAGIC); the version is bumped here whenever the layout changes.
#pragma once

#include <stdint.h>

#include "firmware_version.h"
#include "groups.h"
#include "ota_update.h"
#include "power_policy.h"
#include "powermon.h"  // PowerSample — MSG_POWER's payload IS the logic struct

// Bumped on any incompatible wire-layout change. Receivers reject a mismatch
// rather than misparse a packet from a node on different firmware. Also reported
// in REGISTER so the conductor can spot a straggler running stale firmware.
// v2: BeaconMsg grew `flags` (field-awake override for daytime deep-sleep).
// v3: RegisterMsg reports a build id + dirty flag for OTA version consistency.
// v4: RegisterMsg also reports the human firmware version string.
// v5: BeaconMsg carries runtime power policy (sleep intervals + LED schedule).
// v6: PowerPolicy carries UTC epoch seconds for aligned sleep/update rendezvous.
// v7: BeaconMsg added six bytes that once carried an experimental USB battery
//     keepalive. They remain reserved so v10's wire layout stays compatible.
// v8: TableRow carries a permanent numeric ID and optional-position flags.
// v9: TableRow and REGISTER carry group membership; BeaconMsg carries one
//     independent pattern config for each of eight lantern groups.
// v10: TableRow and REGISTER carry each board's 16/32/64 LED-count profile.
// v11: MsgHeader carries logical origin/destination plus a one-hop route count;
//      REGISTER reports performer/conductor/relay role.
// MSG_ROSTER was added without a protocol bump: it is a new optional message
// type, and older receivers safely ignore unknown types.
static constexpr uint8_t PROTO_VERSION = 11;

// A field has eight fixed group slots. Group ids are zero-based on the wire and
// in NVS (the operator UI labels them Group 1..8). Fixed slots avoid a second
// distributed naming/lifecycle protocol; the conductor remains authoritative
// for membership and the pattern in every slot.

// BeaconMsg.flags bits.
// FIELD_AWAKE: conductor-commanded override — "the field should be awake now,
// daylight or not". A dusk-sleeping performer checks for this at every resample
// rendezvous (it listens for a beacon before it may re-sleep), so setting the
// flag (`wake on` on the conductor) summons the whole field within one resample
// interval for a daytime test; clearing it lets the dusk logic resume. Sticky on
// the conductor (NVS) so a conductor reboot can't silently drop the override.
static constexpr uint8_t BEACON_FLAG_FIELD_AWAKE = 0x01;

enum MsgType : uint8_t {
  MSG_BEACON   = 0,  // conductor -> all: clock + pattern config (hot path)
  MSG_REGISTER = 1,  // performer -> conductor: announce my MAC + firmware
  MSG_ROSTER   = 2,  // conductor -> all: finalized roster        (Half 2)
  MSG_TABLE    = 3,  // conductor -> all: MAC->ID+position+group inventory
  MSG_ACK      = 4,  // relay -> primary: downstream delivery receipt
  MSG_POWER    = 5,  // performer -> conductor: INA228 energy telemetry
  MSG_OTA_BEGIN = 6, // conductor -> all: begin field firmware OTA
  MSG_OTA_CHUNK = 7, // conductor -> all: firmware OTA chunk
  MSG_OTA_END   = 8, // conductor -> all: finalize firmware OTA
  MSG_OTA_STATUS = 9, // performer -> conductor: OTA progress/error report
  MSG_OTA_QUERY = 10, // conductor -> performers: report current OTA checkpoint
  MSG_OTA_ACTIVATE = 11, // conductor -> performer: reboot staged image
};

typedef struct __attribute__((packed)) {
  uint32_t magic;    // BEACON_MAGIC — reject anything else
  uint8_t  version;  // PROTO_VERSION — reject a mismatch
  uint8_t  type;     // MsgType
  uint8_t  origin[6];       // logical sender, preserved across a relay
  uint8_t  destination[6];  // logical recipient or FF:FF:FF:FF:FF:FF
  uint8_t  hops;            // 0 direct, 1 relayed; larger values are rejected
} MsgHeader;

static_assert(sizeof(MsgHeader) == 19, "MsgHeader v11 wire layout changed");

// One group's pattern configuration. Kept as a separate packed wire type so a
// performer can select its own group without copying or interpreting the other
// seven configs. 8 * 12 B still leaves the full beacon well below ESP-NOW's
// 250-byte payload cap.
typedef struct __attribute__((packed)) {
  uint16_t  pattern_id;  // which pattern to render
  uint8_t   brightness;  // per-group brightness cap (0-255)
  uint8_t   palette_id;  // palette selector
  uint16_t  params[4];   // pattern-specific knobs for live tweaking
} PatternConfig;

// type = MSG_BEACON. The conductor's clock plus every group's pattern config.
// The sync hot path still consumes only epoch_us + seq and is untouched.
typedef struct __attribute__((packed)) {
  MsgHeader hdr;
  int64_t   epoch_us;    // conductor's esp_timer clock at send time
  PatternConfig patterns[GROUP_COUNT];
  uint8_t   flags;       // BEACON_FLAG_* bits (field-awake override, …)
  PowerPolicy power;      // runtime sleep/schedule config, broadcast not reflashed
  uint8_t   reserved_v7[6];  // retained to preserve the v10 wire layout
  uint32_t  seq;         // monotonic; for drop detection / logging
} BeaconMsg;

inline const PatternConfig& beaconPattern(const BeaconMsg& b,
                                          uint8_t group_id) {
  return b.patterns[groupIdSafe(group_id)];
}

static_assert(sizeof(BeaconMsg) == 149, "BeaconMsg v11 wire layout changed");
static_assert(sizeof(BeaconMsg) <= 250, "BeaconMsg exceeds ESP-NOW payload cap");

// type = MSG_ROSTER. During camera calibration the conductor broadcasts the
// sorted alive MAC roster in chunks. Each performer finds its own MAC and uses
// `base_rank + row_index + 1` as the dense, collision-free calibration identity.
// This lets brand-new id=0 nodes blink unique locator codes without serial
// provisioning. ESP-NOW caps payloads at 250 B; v11's routed header leaves room
// for 37 MACs in a 246 B packet.
static constexpr uint8_t ROSTER_MACS_PER_MSG = 37;

typedef struct __attribute__((packed)) {
  MsgHeader hdr;
  uint8_t   chunk;
  uint8_t   chunks;
  uint8_t   n;
  uint16_t  base_rank;  // zero-based rank of macs[0] in the full sorted roster
  uint8_t   macs[ROSTER_MACS_PER_MSG][6];
} RosterMsg;

// A relay emits this only after every queued copy of a targeted packet has
// completed downstream. The header origin is the logical child named by the
// original packet, while the immediate ESP-NOW sender remains the relay. This
// gives the primary end-to-end delivery evidence before it reboots that relay.
typedef struct __attribute__((packed)) {
  MsgHeader hdr;
  uint8_t   acked_type;
  uint8_t   delivered;
} AckMsg;

static_assert(sizeof(AckMsg) == 21, "AckMsg v11 wire layout changed");

inline uint16_t rosterMsgFindRank(const RosterMsg& msg, const uint8_t mac[6]) {
  for (uint8_t i = 0; i < msg.n && i < ROSTER_MACS_PER_MSG; i++) {
    bool match = true;
    for (uint8_t j = 0; j < 6; j++) {
      if (msg.macs[i][j] != mac[j]) {
        match = false;
        break;
      }
    }
    if (match) return (uint16_t)(msg.base_rank + i + 1);
  }
  return 0;
}

// type = MSG_REGISTER. A performer unicasts this to the conductor when it hears a
// beacon, so the conductor can build a roster keyed on the node's MAC (its stable
// identity). Sent periodically so a restarted conductor rebuilds the roster.
typedef struct __attribute__((packed)) {
  MsgHeader hdr;
  uint8_t   mac[6];  // sender's WiFi STA MAC — the node's stable identity
  uint16_t  id;      // human label (0 if unprovisioned)
  uint8_t   group_id;  // cached table group; lets conductor repair a missed edit
  uint8_t   led_count;  // cached 16/32/64 hardware profile
  uint8_t   role;     // ROLE_PERFORMER / ROLE_CONDUCTOR / ROLE_RELAY
  uint8_t   fw;      // sender's PROTO_VERSION (wire compatibility marker)
  uint32_t  build;   // sender's firmware build id (git-derived)
  uint8_t   dirty;   // sender was built from uncommitted firmware changes
  char      version[FIRMWARE_VERSION_MAX];  // human release version, NUL-padded
} RegisterMsg;

// One row of the conductor inventory on the wire. ID belongs to the physical
// board; position and show group are deployment-specific; LED count describes
// the board's attached emitter chain.
typedef struct __attribute__((packed)) {
  uint8_t  mac[6];
  uint16_t id;
  uint8_t  flags;
  uint8_t  group_id;
  uint8_t  led_count;
  float    x;
  float    y;
} TableRow;  // 19 bytes

// Rows per MSG_TABLE packet. ESP-NOW caps the payload at 250 B; the routed header
// plus chunk fields are 22 B, so (250 - 22) / 19 = 12 rows fit (250 B full).
static constexpr uint8_t TABLE_ROWS_PER_MSG = 12;

// type = MSG_TABLE. The conductor's authoritative inventory, broadcast in
// chunks. A node scans for its MAC and adopts + caches its permanent ID and
// optional position, group, and LED count. `chunk`/`chunks` describe one
// inventory round.
typedef struct __attribute__((packed)) {
  MsgHeader hdr;
  uint8_t   chunk;   // this chunk's index, 0..chunks-1
  uint8_t   chunks;  // total chunks in the table this round
  uint8_t   n;       // rows present in this packet (<= TABLE_ROWS_PER_MSG)
  TableRow  rows[TABLE_ROWS_PER_MSG];
} TableMsg;

// type = MSG_POWER. An INA228-instrumented performer (1–2 reference nodes, not
// the whole field) unicasts its hardware-accumulated energy/charge totals to the
// conductor on the existing REGISTER path, so any overnight sync test doubles as
// a fleet power audit. Sent occasionally (the accumulator integrates in hardware
// regardless); the conductor just logs it. Adding this type did NOT bump
// PROTO_VERSION: no existing layout changed, and a receiver without the handler
// ignores an unknown type via its dispatch default.
//
// The payload IS powermon.h's PowerSample, embedded — one field list, so sender
// and receiver can never drift out of positional lockstep. Wire-safe: all five
// members are 4-byte and naturally aligned, so PowerSample has no internal
// padding and the packed layout is byte-identical to spelling the fields out.
typedef struct __attribute__((packed)) {
  MsgHeader   hdr;
  uint8_t     mac[6];  // sender's MAC (also in recv-info; kept for the log)
  PowerSample s;       // energy_j / charge_c / bus_v / current_ma / elapsed_s
} PowerMsg;

typedef struct __attribute__((packed)) {
  MsgHeader hdr;
  uint32_t  size;
  uint32_t  crc32;
} OtaBeginMsg;

typedef struct __attribute__((packed)) {
  MsgHeader hdr;
  uint32_t  offset;
  uint8_t   n;
  uint8_t   data[OTA_SERIAL_CHUNK_MAX];
} OtaChunkMsg;

typedef struct __attribute__((packed)) {
  MsgHeader hdr;
} OtaEndMsg;

typedef struct __attribute__((packed)) {
  MsgHeader hdr;
} OtaQueryMsg;

typedef struct __attribute__((packed)) {
  MsgHeader hdr;
} OtaActivateMsg;

typedef struct __attribute__((packed)) {
  MsgHeader hdr;
  uint8_t   mac[6];
  uint8_t   phase;   // OtaStatusPhase
  uint8_t   error;   // OtaStatusError
  uint32_t  offset;  // bytes accepted so far
  uint32_t  crc32;   // running CRC32 at offset
} OtaStatusMsg;

static_assert(sizeof(TableMsg) == 250, "TableMsg must fit ESP-NOW v1 exactly");
static_assert(sizeof(TableMsg) <= 250, "TableMsg exceeds ESP-NOW payload cap");
