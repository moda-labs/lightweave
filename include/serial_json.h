// Machine serial protocol parser for the control plane.
//
// The human CLI in main.cpp remains line-oriented text. Lines that begin with
// JSON are parsed here into a tiny command struct so the Python control plane can
// drive the same firmware over USB serial. This is not a general JSON parser; it
// accepts the compact request shape emitted by control.adapters.JsonLineSerialConductor.
#pragma once

#include <ctype.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "macaddr.h"
#include "groups.h"
#include "led_profile.h"
#include "ota_update.h"
#include "pattern_ids.h"

// Worst-case targeted begin is 64 quoted MACs (20 bytes each including comma)
// plus bounded uint32 fields and JSON keys. Keep the UART accumulator large
// enough for the full OTA_STATUS_MAX cohort without putting it on loopTask's
// stack (pollSerialCommands owns a static buffer).
static constexpr uint16_t SERIAL_JSON_COMMAND_MAX = 1536;
static_assert(SERIAL_JSON_COMMAND_MAX >= 128 + OTA_STATUS_MAX * 20,
              "serial command buffer cannot hold a full targeted OTA cohort");

enum SerialJsonKind {
  SJ_NONE = 0,
  SJ_STATE,
  SJ_IDENTIFY,
  SJ_ASSIGN,
  SJ_GROUP,
  SJ_LED_COUNT,
  SJ_FORGET,
  SJ_REPLACE,
  SJ_RESERVE_ID,
  SJ_PATTERN,
  SJ_BLACKOUT,
  SJ_RESTORE_BLACKOUT,
  SJ_POWER_POLICY,
  SJ_OTA_MODE,
  SJ_OTA_BEGIN,
  SJ_OTA_BEGIN_TARGETS,
  SJ_OTA_CHUNK,
  SJ_OTA_REBROADCAST,
  SJ_OTA_END,
  SJ_OTA_PROGRESS,
  SJ_OTA_REPAIR,
  SJ_OTA_RESTART,
  SJ_OTA_PROBE,
  SJ_OTA_ACTIVATE,
};

struct SerialJsonCommand {
  uint32_t id = 0;
  SerialJsonKind kind = SJ_NONE;
  uint8_t mac[6] = {0};
  uint8_t old_mac[6] = {0};
  uint8_t new_mac[6] = {0};
  uint16_t reported_id = 0;
  float x = 0.0f;
  float y = 0.0f;
  bool has_group_id = false;
  uint8_t group_id = 0;
  bool has_led_count = false;
  uint8_t led_count = DEFAULT_LED_COUNT;
  uint16_t pattern_id = patterns::GLOW;
  uint8_t brightness = 48;
  bool has_brightness = false;
  bool has_params[4] = {false, false, false, false};
  uint16_t params[4] = {0, 0, 0, 0};
  bool has_light_sleep_check_s = false;
  bool has_deep_sleep_check_min = false;
  bool has_led_on_start_min = false;
  bool has_led_on_end_min = false;
  bool has_schedule_enabled = false;
  bool has_force_awake = false;
  bool has_force_sleep = false;
  bool has_current_min = false;
  bool has_current_epoch_s = false;
  uint16_t light_sleep_check_s = 4;
  uint16_t deep_sleep_check_min = 15;
  uint16_t led_on_start_min = 20 * 60;
  uint16_t led_on_end_min = 6 * 60;
  bool schedule_enabled = false;
  bool force_awake = false;
  bool force_sleep = false;
  uint16_t current_min = 12 * 60;
  uint32_t current_epoch_s = 0;
  bool has_ota_enabled = false;
  bool ota_enabled = false;
  uint32_t ota_size = 0;
  uint32_t ota_crc32 = 0;
  uint32_t ota_offset = 0;
  OtaCohort ota_targets = {};
  char ota_data_hex[OTA_SERIAL_CHUNK_MAX * 2 + 1] = {0};
  bool ota_self = false;
};

inline bool serialJsonLooksLike(const char* line) {
  while (*line && isspace((unsigned char)*line)) line++;
  return *line == '{';
}

inline const char* sjKey(const char* json, const char* key) {
  char needle[32];
  snprintf(needle, sizeof(needle), "\"%s\"", key);
  const char* p = json;
  while ((p = strstr(p, needle)) != nullptr) {
    p += strlen(needle);
    while (*p && isspace((unsigned char)*p)) p++;
    if (*p == ':') {
      p++;
      while (*p && isspace((unsigned char)*p)) p++;
      return p;
    }
  }
  return nullptr;
}

inline bool sjString(const char* json, const char* key, char* out, size_t out_len) {
  if (!out_len) return false;
  const char* p = sjKey(json, key);
  if (!p || *p != '"') return false;
  p++;
  size_t n = 0;
  while (*p && *p != '"') {
    if (n + 1 >= out_len) return false;
    out[n++] = *p++;
  }
  if (*p != '"') return false;
  out[n] = '\0';
  return true;
}

inline bool sjFloat(const char* json, const char* key, float& out) {
  const char* p = sjKey(json, key);
  if (!p) return false;
  char* end = nullptr;
  out = strtof(p, &end);
  return end && end != p;
}

inline bool sjUint(const char* json, const char* key, uint32_t& out) {
  const char* p = sjKey(json, key);
  if (!p) return false;
  char* end = nullptr;
  unsigned long v = strtoul(p, &end, 10);
  if (!end || end == p) return false;
  out = (uint32_t)v;
  return true;
}

inline bool sjBool(const char* json, const char* key, bool& out) {
  const char* p = sjKey(json, key);
  if (!p) return false;
  if (!strncmp(p, "true", 4)) {
    out = true;
    return true;
  }
  if (!strncmp(p, "false", 5)) {
    out = false;
    return true;
  }
  uint32_t v = 0;
  if (sjUint(json, key, v)) {
    out = v != 0;
    return true;
  }
  return false;
}

inline void sjLowerCompact(const char* in, char* out, size_t out_len) {
  size_t n = 0;
  for (; *in && n + 1 < out_len; in++) {
    unsigned char c = (unsigned char)*in;
    if (c == ' ' || c == '_' || c == '-') continue;
    out[n++] = (char)tolower(c);
  }
  out[n] = '\0';
}

inline bool serialJsonPatternId(const char* value, uint16_t& out) {
  char norm[32];
  sjLowerCompact(value, norm, sizeof(norm));
  if (!strcmp(norm, "pulse")) out = patterns::PULSE;
  else if (!strcmp(norm, "palettedrift")) out = patterns::PALETTE_DRIFT;
  else if (!strcmp(norm, "sweep")) out = patterns::SWEEP;
  else if (!strcmp(norm, "solid")) out = patterns::SOLID;
  else if (!strcmp(norm, "glow")) out = patterns::GLOW;
  else if (!strcmp(norm, "firefly")) out = patterns::FIREFLY;
  else if (!strcmp(norm, "oceanwave")) out = patterns::OCEAN_WAVE;
  else if (!strcmp(norm, "fireflicker")) out = patterns::FIRE_FLICKER;
  else if (!strcmp(norm, "fire2012")) out = patterns::FIRE2012;
  else if (!strcmp(norm, "wavefront")) out = patterns::WAVEFRONT;
  else if (!strcmp(norm, "white")) out = patterns::WHITE;
  else if (!strcmp(norm, "calibration")) out = patterns::CALIBRATION;
  else {
    char* end = nullptr;
    unsigned long v = strtoul(value, &end, 10);
    if (!end || end == value || *end != '\0' || v > 65535) return false;
    out = (uint16_t)v;
  }
  return true;
}

inline bool sjMac(const char* json, const char* key, uint8_t out[6]) {
  char text[18];
  return sjString(json, key, text, sizeof(text)) && parseMac(text, out);
}

// Parse the deliberately narrow JSON array emitted by the control adapter.
// Keeping this here makes exact-cohort validation host-testable without pulling
// a general-purpose JSON allocator into the firmware.
inline bool sjMacCohort(const char* json, const char* key, OtaCohort& out) {
  otaCohortInit(out);
  const char* p = sjKey(json, key);
  if (!p || *p != '[') return false;
  p++;
  while (*p && isspace((unsigned char)*p)) p++;
  if (*p == ']') return false;

  while (*p) {
    while (*p && isspace((unsigned char)*p)) p++;
    if (*p != '"') return false;
    p++;
    char text[18];
    size_t n = 0;
    while (*p && *p != '"') {
      if (n + 1 >= sizeof(text)) return false;
      text[n++] = *p++;
    }
    if (*p != '"') return false;
    p++;
    text[n] = '\0';
    uint8_t mac[6];
    if (!parseMac(text, mac) || otaCohortContains(out, mac) ||
        !otaCohortAdd(out, mac)) return false;
    while (*p && isspace((unsigned char)*p)) p++;
    if (*p == ']') return out.count > 0;
    if (*p != ',') return false;
    p++;
  }
  return false;
}

inline bool serialJsonParse(const char* json, SerialJsonCommand& cmd,
                            const char*& error) {
  cmd = SerialJsonCommand{};
  uint32_t id = 0;
  if (!sjUint(json, "id", id)) {
    error = "missing id";
    return false;
  }
  cmd.id = id;

  char verb[20];
  if (!sjString(json, "cmd", verb, sizeof(verb))) {
    error = "missing cmd";
    return false;
  }
  char norm[24];
  sjLowerCompact(verb, norm, sizeof(norm));

  if (!strcmp(norm, "state")) {
    cmd.kind = SJ_STATE;
  } else if (!strcmp(norm, "identify")) {
    cmd.kind = SJ_IDENTIFY;
    if (!sjMac(json, "mac", cmd.mac)) {
      error = "bad mac";
      return false;
    }
  } else if (!strcmp(norm, "assign")) {
    cmd.kind = SJ_ASSIGN;
    if (!sjMac(json, "mac", cmd.mac) || !sjFloat(json, "x", cmd.x) ||
        !sjFloat(json, "y", cmd.y)) {
      error = "bad assign";
      return false;
    }
  } else if (!strcmp(norm, "group")) {
    cmd.kind = SJ_GROUP;
    uint32_t group_id = 0;
    if (!sjMac(json, "mac", cmd.mac) || !sjUint(json, "group_id", group_id) ||
        group_id >= GROUP_COUNT) {
      error = "bad group";
      return false;
    }
    cmd.has_group_id = true;
    cmd.group_id = (uint8_t)group_id;
  } else if (!strcmp(norm, "ledcount")) {
    cmd.kind = SJ_LED_COUNT;
    uint32_t led_count = 0;
    if (!sjMac(json, "mac", cmd.mac) || !sjUint(json, "led_count", led_count) ||
        led_count > 255 || !ledCountValid((uint8_t)led_count)) {
      error = "bad led count";
      return false;
    }
    cmd.has_led_count = true;
    cmd.led_count = (uint8_t)led_count;
  } else if (!strcmp(norm, "forget")) {
    cmd.kind = SJ_FORGET;
    if (!sjMac(json, "mac", cmd.mac)) {
      error = "bad mac";
      return false;
    }
  } else if (!strcmp(norm, "replace")) {
    cmd.kind = SJ_REPLACE;
    if (!sjMac(json, "old_mac", cmd.old_mac) ||
        !sjMac(json, "new_mac", cmd.new_mac)) {
      error = "bad replace";
      return false;
    }
  } else if (!strcmp(norm, "reserveid")) {
    cmd.kind = SJ_RESERVE_ID;
    if (!sjMac(json, "mac", cmd.mac)) {
      error = "bad mac";
      return false;
    }
    uint32_t reported_id = 0;
    if (sjUint(json, "reported_id", reported_id)) {
      if (reported_id > 65535) {
        error = "bad reported id";
        return false;
      }
      cmd.reported_id = (uint16_t)reported_id;
    }
  } else if (!strcmp(norm, "pattern")) {
    cmd.kind = SJ_PATTERN;
    char pattern[32];
    if (!sjString(json, "pattern", pattern, sizeof(pattern)) ||
        !serialJsonPatternId(pattern, cmd.pattern_id)) {
      error = "bad pattern";
      return false;
    }
    uint32_t brightness = 0;
    if (sjUint(json, "brightness", brightness)) {
      cmd.has_brightness = true;
      cmd.brightness = (uint8_t)(brightness > 255 ? 255 : brightness);
    }
    uint32_t group_id = 0;
    if (sjUint(json, "group_id", group_id)) {
      if (group_id >= GROUP_COUNT) {
        error = "bad pattern group";
        return false;
      }
      cmd.has_group_id = true;
      cmd.group_id = (uint8_t)group_id;
    }
    uint32_t v = 0;
    if (sjUint(json, "period", v)) {
      cmd.has_params[0] = true;
      cmd.params[0] = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "hue", v)) {
      cmd.has_params[0] = true;
      cmd.params[0] = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "saturation", v)) {
      cmd.has_params[1] = true;
      cmd.params[1] = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "spatial", v)) {
      cmd.has_params[1] = true;
      cmd.params[1] = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "p0", v)) {
      cmd.has_params[0] = true;
      cmd.params[0] = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "p1", v)) {
      cmd.has_params[1] = true;
      cmd.params[1] = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "p2", v)) {
      cmd.has_params[2] = true;
      cmd.params[2] = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "p3", v)) {
      cmd.has_params[3] = true;
      cmd.params[3] = (uint16_t)(v > 65535 ? 65535 : v);
    }
  } else if (!strcmp(norm, "blackout")) {
    cmd.kind = SJ_BLACKOUT;
  } else if (!strcmp(norm, "restoreblackout")) {
    cmd.kind = SJ_RESTORE_BLACKOUT;
  } else if (!strcmp(norm, "powerpolicy")) {
    cmd.kind = SJ_POWER_POLICY;
    uint32_t v = 0;
    bool b = false;
    if (sjUint(json, "light_sleep_check_s", v)) {
      cmd.has_light_sleep_check_s = true;
      cmd.light_sleep_check_s = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "deep_sleep_check_min", v)) {
      cmd.has_deep_sleep_check_min = true;
      cmd.deep_sleep_check_min = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "led_on_start_min", v)) {
      cmd.has_led_on_start_min = true;
      cmd.led_on_start_min = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "led_on_end_min", v)) {
      cmd.has_led_on_end_min = true;
      cmd.led_on_end_min = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "current_min", v)) {
      cmd.has_current_min = true;
      cmd.current_min = (uint16_t)(v > 65535 ? 65535 : v);
    }
    if (sjUint(json, "current_epoch_s", v)) {
      cmd.has_current_epoch_s = true;
      cmd.current_epoch_s = v;
    }
    if (sjBool(json, "schedule_enabled", b)) {
      cmd.has_schedule_enabled = true;
      cmd.schedule_enabled = b;
    }
    if (sjBool(json, "force_awake", b)) {
      cmd.has_force_awake = true;
      cmd.force_awake = b;
    }
    if (sjBool(json, "force_sleep", b)) {
      cmd.has_force_sleep = true;
      cmd.force_sleep = b;
    }
  } else if (!strcmp(norm, "otamode")) {
    cmd.kind = SJ_OTA_MODE;
    bool b = false;
    if (!sjBool(json, "enabled", b)) {
      error = "bad ota mode";
      return false;
    }
    cmd.has_ota_enabled = true;
    cmd.ota_enabled = b;
  } else if (!strcmp(norm, "otabegin")) {
    cmd.kind = SJ_OTA_BEGIN;
    if (!sjUint(json, "size", cmd.ota_size) ||
        !sjUint(json, "crc32", cmd.ota_crc32)) {
      error = "bad ota begin";
      return false;
    }
  } else if (!strcmp(norm, "otabegintargets")) {
    cmd.kind = SJ_OTA_BEGIN_TARGETS;
    if (!sjUint(json, "size", cmd.ota_size) ||
        !sjUint(json, "crc32", cmd.ota_crc32) ||
        !sjMacCohort(json, "targets", cmd.ota_targets)) {
      error = "bad targeted ota begin";
      return false;
    }
  } else if (!strcmp(norm, "otachunk")) {
    cmd.kind = SJ_OTA_CHUNK;
    if (!sjUint(json, "offset", cmd.ota_offset) ||
        !sjString(json, "data", cmd.ota_data_hex, sizeof(cmd.ota_data_hex))) {
      error = "bad ota chunk";
      return false;
    }
  } else if (!strcmp(norm, "otarebroadcast")) {
    cmd.kind = SJ_OTA_REBROADCAST;
    if (!sjUint(json, "offset", cmd.ota_offset) ||
        !sjString(json, "data", cmd.ota_data_hex, sizeof(cmd.ota_data_hex))) {
      error = "bad ota rebroadcast";
      return false;
    }
  } else if (!strcmp(norm, "otaend")) {
    cmd.kind = SJ_OTA_END;
  } else if (!strcmp(norm, "otaprogress")) {
    cmd.kind = SJ_OTA_PROGRESS;
  } else if (!strcmp(norm, "otarepair")) {
    cmd.kind = SJ_OTA_REPAIR;
    if (!sjMac(json, "mac", cmd.mac) ||
        !sjUint(json, "offset", cmd.ota_offset) ||
        !sjString(json, "data", cmd.ota_data_hex, sizeof(cmd.ota_data_hex))) {
      error = "bad ota repair";
      return false;
    }
  } else if (!strcmp(norm, "otarestart")) {
    cmd.kind = SJ_OTA_RESTART;
    if (!sjMac(json, "mac", cmd.mac)) {
      error = "bad ota restart";
      return false;
    }
  } else if (!strcmp(norm, "otaprobe")) {
    cmd.kind = SJ_OTA_PROBE;
  } else if (!strcmp(norm, "otaactivate")) {
    cmd.kind = SJ_OTA_ACTIVATE;
    bool self = false;
    if (sjBool(json, "conductor", self) && self) {
      cmd.ota_self = true;
    } else if (!sjMac(json, "mac", cmd.mac)) {
      error = "bad ota activate";
      return false;
    }
  } else {
    error = "unknown cmd";
    return false;
  }
  error = nullptr;
  return true;
}
