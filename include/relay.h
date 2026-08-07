// Pure one-hop routing and relay queue logic.
//
// This module deliberately knows nothing about Arduino, ESP-NOW, NVS, or
// FreeRTOS. main.cpp owns radio peers and callbacks; these helpers own the
// quietly-wrong-able decisions: authority selection, parent stickiness, hop
// validation, next-hop learning, and bounded forwarding.
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "beacon.h"
#include "config.h"
#include "roster.h"

static constexpr uint8_t ROUTE_MAX_HOPS = 1;
static constexpr size_t RELAY_PACKET_MAX = 250;
static constexpr uint8_t RELAY_QUEUE_CAPACITY = 16;
static constexpr uint8_t RELAY_COPY_LIMIT = 8;

inline bool routeMacEqual(const uint8_t a[6], const uint8_t b[6]) {
  return memcmp(a, b, 6) == 0;
}

inline bool routeMacBroadcast(const uint8_t mac[6]) {
  for (uint8_t i = 0; i < 6; i++)
    if (mac[i] != 0xFF) return false;
  return true;
}

inline bool routeMacZero(const uint8_t mac[6]) {
  for (uint8_t i = 0; i < 6; i++)
    if (mac[i] != 0) return false;
  return true;
}

inline void routeHeaderSet(MsgHeader& hdr, const uint8_t origin[6],
                           const uint8_t destination[6], uint8_t hops = 0) {
  memcpy(hdr.origin, origin, 6);
  memcpy(hdr.destination, destination, 6);
  hdr.hops = hops;
}

inline bool routeHeaderBasicValid(const MsgHeader& hdr) {
  return hdr.magic == BEACON_MAGIC &&
         hdr.transport_version == TRANSPORT_VERSION &&
         hdr.hops <= ROUTE_MAX_HOPS && !routeMacZero(hdr.origin) &&
         !routeMacBroadcast(hdr.origin) && !routeMacZero(hdr.destination);
}

inline bool routeAddressedTo(const MsgHeader& hdr, const uint8_t local[6]) {
  return routeMacBroadcast(hdr.destination) ||
         routeMacEqual(hdr.destination, local);
}

struct ParentRoute {
  uint8_t primary[6];
  uint8_t parent[6];
  uint8_t hops;
  int64_t last_us;
  bool valid;
};

inline void parentRouteInit(ParentRoute& route) {
  memset(&route, 0, sizeof(route));
}

enum ParentDecision : uint8_t {
  PARENT_REJECT = 0,
  PARENT_ACCEPT_CURRENT,
  PARENT_ACCEPT_NEW,
  PARENT_ACCEPT_FAILOVER,
};

// Select a primary authority and physical parent from a beacon. A relay may
// learn only a direct primary (never another relay), which makes the topology
// one hop by construction. A performer may change physical parent only after
// the current one is stale, and only for the same logical primary.
inline ParentDecision parentRouteOnBeacon(
    ParentRoute& route, bool local_is_relay, const uint8_t local[6],
    const uint8_t transport_src[6], const MsgHeader& hdr, int64_t now_us,
    int64_t stale_us) {
  if (!routeHeaderBasicValid(hdr) || hdr.type != MSG_BEACON ||
      !routeAddressedTo(hdr, local) || !routeMacBroadcast(hdr.destination)) {
    return PARENT_REJECT;
  }
  if (local_is_relay &&
      (hdr.hops != 0 || !routeMacEqual(transport_src, hdr.origin))) {
    return PARENT_REJECT;
  }
  if (!local_is_relay) {
    if (hdr.hops > ROUTE_MAX_HOPS) return PARENT_REJECT;
    if (hdr.hops == 0 && !routeMacEqual(transport_src, hdr.origin))
      return PARENT_REJECT;
    if (hdr.hops == 1 && routeMacEqual(transport_src, hdr.origin))
      return PARENT_REJECT;
  }

  if (!route.valid) {
    memcpy(route.primary, hdr.origin, 6);
    memcpy(route.parent, transport_src, 6);
    route.hops = hdr.hops;
    route.last_us = now_us;
    route.valid = true;
    return PARENT_ACCEPT_NEW;
  }
  // Never switch logical conductors automatically. The primary remains a
  // deliberate single authority; only its physical path may fail over.
  if (!routeMacEqual(route.primary, hdr.origin)) return PARENT_REJECT;
  if (routeMacEqual(route.parent, transport_src) && route.hops == hdr.hops) {
    route.last_us = now_us;
    return PARENT_ACCEPT_CURRENT;
  }
  if (now_us - route.last_us < stale_us) return PARENT_REJECT;

  memcpy(route.parent, transport_src, 6);
  route.hops = hdr.hops;
  route.last_us = now_us;
  return PARENT_ACCEPT_FAILOVER;
}

inline bool routeFromCurrentParent(const ParentRoute& route,
                                   const uint8_t transport_src[6],
                                   const MsgHeader& hdr,
                                   const uint8_t local[6]) {
  return route.valid && routeHeaderBasicValid(hdr) &&
         routeAddressedTo(hdr, local) &&
         routeMacEqual(hdr.origin, route.primary) &&
         routeMacEqual(transport_src, route.parent) &&
         hdr.hops == route.hops;
}

inline bool routeFromCurrentParentAnyDestination(
    const ParentRoute& route, const uint8_t transport_src[6],
    const MsgHeader& hdr) {
  return route.valid && routeHeaderBasicValid(hdr) &&
         routeMacEqual(hdr.origin, route.primary) &&
         routeMacEqual(transport_src, route.parent) &&
         hdr.hops == route.hops;
}

inline bool routeChildUplinkValid(const ParentRoute& route,
                                  const uint8_t transport_src[6],
                                  const MsgHeader& hdr) {
  return route.valid && routeHeaderBasicValid(hdr) && hdr.hops == 0 &&
         routeMacEqual(hdr.origin, transport_src) &&
         routeMacEqual(hdr.destination, route.primary);
}

inline bool routePrimaryReceiveValid(const uint8_t primary[6],
                                     const uint8_t transport_src[6],
                                     bool transport_is_relay,
                                     const MsgHeader& hdr) {
  if (!routeHeaderBasicValid(hdr) ||
      !routeMacEqual(hdr.destination, primary)) return false;
  if (hdr.hops == 0) return routeMacEqual(hdr.origin, transport_src);
  return transport_is_relay && !routeMacEqual(hdr.origin, transport_src);
}

enum RelayPeerKind : uint8_t {
  RELAY_PEER_BROADCAST = 0,
  RELAY_PEER_UPSTREAM,
  RELAY_PEER_DOWNSTREAM,
};

// The upstream parent is a permanent peer owned by conductorPeerReady(). Only
// downstream children may occupy the relay's rotating one-peer lease.
inline RelayPeerKind relayPeerKind(const ParentRoute& route,
                                   const uint8_t destination[6]) {
  if (routeMacBroadcast(destination)) return RELAY_PEER_BROADCAST;
  if (route.valid && routeMacEqual(route.parent, destination))
    return RELAY_PEER_UPSTREAM;
  return RELAY_PEER_DOWNSTREAM;
}

struct RelayFrame {
  uint8_t data[RELAY_PACKET_MAX];
  uint8_t transport_destination[6];
  uint16_t len;
  uint8_t copies;
  int64_t received_us;
  bool delivered;
};

struct RelayCompletion {
  uint8_t destination[6];
  uint8_t type;
  uint32_t token;
  bool delivered;
};

struct RelayReceipt {
  uint8_t destination[6];
  uint8_t type;
  uint32_t token;
  bool delivered;
  bool pending;
};

inline void relayReceiptInit(RelayReceipt& receipt) {
  memset(&receipt, 0, sizeof(receipt));
}

inline bool relayReceiptSupportsType(uint8_t type) {
  return type == MSG_OTA_BEGIN || type == MSG_OTA_CHUNK ||
         type == MSG_OTA_END || type == MSG_OTA_ACTIVATE;
}

inline uint32_t relayFrameReceiptToken(const uint8_t* data, size_t len) {
  if (!data || len < sizeof(MsgHeader)) return 0;
  MsgHeader hdr;
  memcpy(&hdr, data, sizeof(hdr));
  uint32_t token = otaCrc32Update(0, &hdr.type, sizeof(hdr.type));
  return otaCrc32Update(token, data + sizeof(MsgHeader),
                        len - sizeof(MsgHeader));
}

// Activation receipts existed in the first routed-v11 release. Receipts for
// every targeted OTA frame are additive, so use them only when the immediate
// relay runs the same image as the primary. Older v11 relays remain compatible
// through bounded pacing in the hardware glue.
inline bool relayRouteSupportsFrameReceipt(
    const Roster& roster, const uint8_t target[6],
    const FirmwareVersion& primary_firmware, uint8_t type) {
  int target_index = rosterFind(roster, target);
  if (target_index < 0 || roster.entries[target_index].hops != 1) return false;
  if (type == MSG_OTA_ACTIVATE) return true;
  int relay_index = rosterFind(roster, roster.entries[target_index].via);
  if (relay_index < 0 || roster.entries[relay_index].role != ROLE_RELAY)
    return false;
  return firmwareSame(primary_firmware,
                      rosterEntryFirmware(roster.entries[relay_index]));
}

inline bool relayReceiptSchedule(RelayReceipt& receipt,
                                 const RelayCompletion& completion) {
  if (!relayReceiptSupportsType(completion.type) ||
      routeMacBroadcast(completion.destination)) return false;
  if (receipt.pending &&
      (!routeMacEqual(receipt.destination, completion.destination) ||
       receipt.type != completion.type || receipt.token != completion.token))
    return false;
  memcpy(receipt.destination, completion.destination, 6);
  receipt.type = completion.type;
  receipt.token = completion.token;
  receipt.delivered = receipt.delivered || completion.delivered;
  receipt.pending = true;
  return true;
}

inline void relayReceiptSendResult(RelayReceipt& receipt, bool delivered) {
  if (receipt.pending && delivered) relayReceiptInit(receipt);
}

struct RelayQueue {
  RelayFrame frames[RELAY_QUEUE_CAPACITY];
  uint8_t head;
  uint8_t count;
  uint32_t dropped;
};

inline void relayQueueInit(RelayQueue& queue) {
  memset(&queue, 0, sizeof(queue));
}

inline RelayFrame* relayQueueAt(RelayQueue& queue, uint8_t offset) {
  if (offset >= queue.count) return nullptr;
  return &queue.frames[(uint8_t)((queue.head + offset) % RELAY_QUEUE_CAPACITY)];
}

inline RelayFrame* relayQueueFront(RelayQueue& queue) {
  return relayQueueAt(queue, 0);
}

// Enqueue a hop-zero packet as hop one. Identical queued copies collapse into
// one frame with a bounded repeat count, preventing OTA's strong broadcast
// copies from exhausting RAM while retaining their delivery redundancy.
inline bool relayQueuePush(RelayQueue& queue, const uint8_t* data, size_t len,
                           const uint8_t transport_destination[6],
                           int64_t received_us, uint8_t copies = 1) {
  if (!data || len < sizeof(MsgHeader) || len > RELAY_PACKET_MAX || copies == 0)
    return false;
  MsgHeader hdr;
  memcpy(&hdr, data, sizeof(hdr));
  if (!routeHeaderBasicValid(hdr) || hdr.hops != 0) return false;

  uint8_t routed[RELAY_PACKET_MAX];
  memcpy(routed, data, len);
  MsgHeader routed_header;
  memcpy(&routed_header, routed, sizeof(routed_header));
  routed_header.hops = 1;
  memcpy(routed, &routed_header, sizeof(routed_header));

  for (uint8_t i = 0; i < queue.count; i++) {
    RelayFrame* frame = relayQueueAt(queue, i);
    if (frame->len == len &&
        routeMacEqual(frame->transport_destination, transport_destination) &&
        memcmp(frame->data, routed, len) == 0) {
      uint16_t total = (uint16_t)frame->copies + copies;
      frame->copies = total > RELAY_COPY_LIMIT ? RELAY_COPY_LIMIT : total;
      return true;
    }
  }

  if (queue.count >= RELAY_QUEUE_CAPACITY) {
    queue.dropped++;
    return false;
  }
  uint8_t index = (uint8_t)((queue.head + queue.count) % RELAY_QUEUE_CAPACITY);
  RelayFrame& frame = queue.frames[index];
  memcpy(frame.data, routed, len);
  memcpy(frame.transport_destination, transport_destination, 6);
  frame.len = (uint16_t)len;
  frame.copies = copies > RELAY_COPY_LIMIT ? RELAY_COPY_LIMIT : copies;
  frame.received_us = received_us;
  frame.delivered = false;
  queue.count++;
  return true;
}

inline bool relayQueueCompleteCopy(RelayQueue& queue, bool delivered,
                                   RelayCompletion& completion) {
  memset(&completion, 0, sizeof(completion));
  RelayFrame* frame = relayQueueFront(queue);
  if (!frame) return false;
  frame->delivered = frame->delivered || delivered;
  if (frame->copies > 1) {
    frame->copies--;
    return false;
  }
  MsgHeader hdr;
  memcpy(&hdr, frame->data, sizeof(hdr));
  memcpy(completion.destination, hdr.destination, 6);
  completion.type = hdr.type;
  completion.token = relayFrameReceiptToken(frame->data, frame->len);
  completion.delivered = frame->delivered;
  memset(frame, 0, sizeof(*frame));
  queue.head = (uint8_t)((queue.head + 1) % RELAY_QUEUE_CAPACITY);
  queue.count--;
  return true;
}

inline void relayQueuePopCopy(RelayQueue& queue) {
  RelayCompletion ignored;
  relayQueueCompleteCopy(queue, false, ignored);
}

// A forwarded beacon carries the primary's time at the relay receive instant.
// Advance it by the bounded queue residence time so relay-zone performers do
// not inherit a systematic extra clock offset.
inline bool relayFramePrepare(const RelayFrame& frame, int64_t send_us,
                              uint8_t out[RELAY_PACKET_MAX]) {
  if (frame.len < sizeof(MsgHeader) || frame.len > RELAY_PACKET_MAX) return false;
  memcpy(out, frame.data, frame.len);
  MsgHeader hdr;
  memcpy(&hdr, out, sizeof(hdr));
  if (hdr.type == MSG_BEACON) {
    if (frame.len != sizeof(BeaconMsg)) return false;
    BeaconMsg beacon;
    memcpy(&beacon, out, sizeof(beacon));
    if (send_us > frame.received_us)
      beacon.epoch_us += send_us - frame.received_us;
    memcpy(out, &beacon, sizeof(beacon));
  }
  return true;
}

inline uint8_t relayTargetCopies(uint8_t type) {
  switch (type) {
    case MSG_OTA_ACTIVATE:
    case MSG_OTA_BEGIN:
    case MSG_OTA_END:
      return 4;
    case MSG_OTA_CHUNK:
      return 2;
    default:
      return 1;
  }
}

inline const char* nodeRoleName(uint8_t role) {
  if (role == ROLE_CONDUCTOR) return "conductor";
  if (role == ROLE_RELAY) return "relay";
  return "performer";
}

inline uint8_t nodeRoleSafe(uint8_t role) {
  return role <= ROLE_RELAY ? role : ROLE_PERFORMER;
}
