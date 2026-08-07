// One-at-a-time performer unicast ownership.
//
// ESP-NOW's send callback identifies only the destination MAC, not the payload.
// REGISTER, OTA status, and power telemetry all target the same conductor, so a
// bare "register pending" flag can consume the callback for the wrong packet.
// Serializing performer unicasts and recording their purpose makes the callback
// unambiguous. Conductors use their existing OTA delivery tracker instead.
#pragma once

#include <stdint.h>
#include <string.h>

enum PerformerTxPurpose : uint8_t {
  PERFORMER_TX_NONE = 0,
  PERFORMER_TX_REGISTER,
  PERFORMER_TX_POWER,
  PERFORMER_TX_OTA_STATUS,
  PERFORMER_TX_RELAY_ACK,
};

struct PerformerTxState {
  uint8_t mac[6];
  uint8_t purpose;
  bool pending;
};

struct PerformerTxCompletion {
  uint8_t purpose;
  bool matched;
  bool delivered;
};

inline void performerTxInit(PerformerTxState& state) {
  memset(state.mac, 0, sizeof(state.mac));
  state.purpose = PERFORMER_TX_NONE;
  state.pending = false;
}

inline bool performerTxAvailable(const PerformerTxState& state) {
  return !state.pending;
}

inline bool performerTxBegin(PerformerTxState& state, const uint8_t mac[6],
                             uint8_t purpose) {
  if (state.pending || purpose == PERFORMER_TX_NONE) return false;
  memcpy(state.mac, mac, sizeof(state.mac));
  state.purpose = purpose;
  state.pending = true;
  return true;
}

inline bool performerTxCancel(PerformerTxState& state, const uint8_t mac[6],
                              uint8_t purpose) {
  if (!state.pending || state.purpose != purpose ||
      memcmp(state.mac, mac, sizeof(state.mac)) != 0) {
    return false;
  }
  performerTxInit(state);
  return true;
}

inline PerformerTxCompletion performerTxComplete(PerformerTxState& state,
                                                  const uint8_t mac[6],
                                                  bool delivered) {
  PerformerTxCompletion result = {PERFORMER_TX_NONE, false, delivered};
  if (!state.pending || memcmp(state.mac, mac, sizeof(state.mac)) != 0)
    return result;
  result.purpose = state.purpose;
  result.matched = true;
  performerTxInit(state);
  return result;
}
