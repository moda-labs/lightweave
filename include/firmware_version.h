#pragma once

#include <stdint.h>
#include <string.h>

static constexpr uint8_t FIRMWARE_VERSION_MAX = 16;

#ifndef FIRMWARE_BUILD_ID
#define FIRMWARE_BUILD_ID 0x00000000u
#endif

#ifndef FIRMWARE_BUILD_DIRTY
#define FIRMWARE_BUILD_DIRTY 1
#endif

#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "0.0.0"
#endif

struct FirmwareVersion {
  uint8_t  proto;
  uint32_t build_id;
  uint8_t  dirty;
  char     version[FIRMWARE_VERSION_MAX];
};

inline void firmwareCopyVersion(char out[FIRMWARE_VERSION_MAX], const char* version) {
  memset(out, 0, FIRMWARE_VERSION_MAX);
  if (!version) return;
  strncpy(out, version, FIRMWARE_VERSION_MAX - 1);
}

inline FirmwareVersion currentFirmwareVersion(uint8_t proto) {
  FirmwareVersion v = {proto, (uint32_t)FIRMWARE_BUILD_ID, (uint8_t)FIRMWARE_BUILD_DIRTY, {0}};
  firmwareCopyVersion(v.version, FIRMWARE_VERSION);
  return v;
}

inline bool firmwareSame(const FirmwareVersion& a, const FirmwareVersion& b) {
  return a.proto == b.proto && a.build_id == b.build_id && a.dirty == b.dirty &&
         strncmp(a.version, b.version, FIRMWARE_VERSION_MAX) == 0;
}

inline bool firmwareFleetConsistent(const FirmwareVersion& expected,
                                    const FirmwareVersion* seen,
                                    uint8_t count) {
  for (uint8_t i = 0; i < count; i++) {
    if (!firmwareSame(expected, seen[i])) return false;
  }
  return true;
}

inline bool firmwareFleetReady(uint8_t expected, uint8_t seen,
                               uint8_t matching) {
  return expected > 0 && seen == expected && matching == expected;
}
