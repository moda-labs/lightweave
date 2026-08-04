// Fixed lantern-group identifiers shared by the layout table, wire protocol,
// serial commands, and host tests. Dependency-free by design.
#pragma once

#include <stdint.h>

// IDs are zero-based internally; the operator UI labels them Group 1..8.
static constexpr uint8_t GROUP_COUNT = 8;

inline bool groupIdValid(uint8_t group_id) { return group_id < GROUP_COUNT; }

inline uint8_t groupIdSafe(uint8_t group_id) {
  return groupIdValid(group_id) ? group_id : 0;
}
