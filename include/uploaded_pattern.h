// Bounded, stateless uploaded-pattern virtual machine.
//
// This module is deliberately dependency-free. Uploaded programs are data, not
// native code: there are no jumps, loops, allocations, or hardware calls. A
// validator proves stack safety and the instruction budget before a program can
// be persisted or rendered. Existing compiled patterns never execute this VM.
#pragma once

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "firmware_version.h"

static constexpr uint8_t UPLOADED_VM_VERSION = 1;
static constexpr uint8_t UPLOADED_PROGRAM_MAX_BYTES = 192;
static constexpr uint8_t UPLOADED_PROGRAM_MAX_INSTRUCTIONS = 64;
static constexpr uint16_t UPLOADED_PROGRAM_MAX_EXECUTION_COST = 128;
static constexpr uint8_t UPLOADED_STACK_MAX = 16;
// Eight groups may each keep one uploaded program active; the ninth slot is the
// inactive staging buffer for the next atomic distribution.
static constexpr uint8_t UPLOADED_PROGRAM_SLOTS = 9;
static constexpr uint8_t UPLOADED_STATUS_MAX = 64;
static constexpr int64_t UPLOADED_STATUS_HOLD_US = 2000000LL;
static constexpr int64_t UPLOADED_STATUS_INITIAL_SPREAD_US = 500000LL;
static constexpr int64_t UPLOADED_STATUS_RETRY_MAX_US = 800000LL;

enum UploadedOpcode : uint8_t {
  UOP_CONST = 1,  // little-endian IEEE-754 float immediate
  UOP_X = 2,
  UOP_Y = 3,
  UOP_TIME = 4,   // seconds, wrapped every 4096 s for stable float precision
  UOP_PIXEL = 5,  // normalized [0,1) position in the local emitter chain

  UOP_ADD = 16,
  UOP_SUB = 17,
  UOP_MUL = 18,
  UOP_DIV = 19,
  UOP_MIN = 20,
  UOP_MAX = 21,
  UOP_POW = 22,

  UOP_SIN = 32,   // input is cycles: sin(2*pi*x)
  UOP_COS = 33,
  UOP_ABS = 34,
  UOP_FRACT = 35,
  UOP_CLAMP01 = 36,
  UOP_NEG = 37,
  UOP_SQRT = 38,
  UOP_HASH = 39,  // deterministic quantized scalar hash in [0,1]
  UOP_FLOOR = 40,

  UOP_MIX = 48,        // a, b, t -> a + (b-a)*clamp01(t)
  UOP_SMOOTHSTEP = 49, // edge0, edge1, x -> smooth Hermite [0,1]
};

enum UploadedRepairAction : uint8_t {
  UPLOADED_REPAIR_NONE = 0,
  UPLOADED_REPAIR_QUERY,
  UPLOADED_REPAIR_INSTALL,
};

struct UploadedProgram {
  uint64_t id;
  uint8_t version;
  uint8_t length;
  uint8_t data[UPLOADED_PROGRAM_MAX_BYTES];
};

struct UploadedProgramSlots {
  UploadedProgram slots[UPLOADED_PROGRAM_SLOTS];
};

struct UploadedVmOutput {
  float hue;         // cycles; wraps at render time
  float saturation;  // [0,1]
  float value;       // perceptual sRGB [0,1]
  float intensity;   // [0,1]
  bool ok;
};

struct UploadedProgramStatusEntry {
  uint8_t mac[6];
  uint8_t vm_version;
  uint8_t available;
  uint64_t requested_id;
  FirmwareVersion firmware;
  int64_t last_us;
};

struct UploadedProgramStatusTable {
  UploadedProgramStatusEntry entries[UPLOADED_STATUS_MAX];
  uint8_t count;
};

struct UploadedStatusTxSchedule {
  bool pending;
  uint8_t failures;
  int64_t next_us;
  int64_t hold_until_us;
};

inline float uploadedClamp01(float value) {
  if (!isfinite(value)) return 0.0f;
  if (value < 0.0f) return 0.0f;
  if (value > 1.0f) return 1.0f;
  return value;
}

inline uint32_t uploadedCrc32Update(uint32_t crc, const uint8_t* data,
                                    size_t len) {
  crc = ~crc;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; bit++) {
      uint32_t mask = 0 - (crc & 1U);
      crc = (crc >> 1) ^ (0xEDB88320UL & mask);
    }
  }
  return ~crc;
}

inline uint32_t uploadedLoad32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
         ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

inline uint32_t uploadedRotateRight32(uint32_t value, uint8_t count) {
  return (value >> count) | (value << (32 - count));
}

inline void uploadedBlake2sCompress(uint32_t state[8], const uint8_t block[64],
                                    uint64_t bytes, bool last) {
  static constexpr uint32_t iv[8] = {
      0x6A09E667U, 0xBB67AE85U, 0x3C6EF372U, 0xA54FF53AU,
      0x510E527FU, 0x9B05688CU, 0x1F83D9ABU, 0x5BE0CD19U};
  static constexpr uint8_t sigma[10][16] = {
      {0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15},
      {14,10,4,8,9,15,13,6,1,12,0,2,11,7,5,3},
      {11,8,12,0,5,2,15,13,10,14,3,6,7,1,9,4},
      {7,9,3,1,13,12,11,14,2,6,5,10,4,0,15,8},
      {9,0,5,7,2,4,10,15,14,1,11,12,6,8,3,13},
      {2,12,6,10,0,11,8,3,4,13,7,5,15,14,1,9},
      {12,5,1,15,14,13,4,10,0,7,6,3,9,2,8,11},
      {13,11,7,14,12,1,3,9,5,0,15,4,8,6,2,10},
      {6,15,14,9,11,3,0,8,12,2,13,7,1,4,10,5},
      {10,2,8,4,7,6,1,5,15,11,9,14,3,12,13,0}};
  uint32_t m[16];
  uint32_t v[16];
  for (uint8_t i = 0; i < 16; i++) m[i] = uploadedLoad32(block + i * 4);
  for (uint8_t i = 0; i < 8; i++) {
    v[i] = state[i];
    v[i + 8] = iv[i];
  }
  v[12] ^= (uint32_t)bytes;
  v[13] ^= (uint32_t)(bytes >> 32);
  if (last) v[14] = ~v[14];
#define UPLOADED_G(a,b,c,d,x,y) do { \
  v[a] = v[a] + v[b] + m[x]; v[d] = uploadedRotateRight32(v[d] ^ v[a], 16); \
  v[c] += v[d]; v[b] = uploadedRotateRight32(v[b] ^ v[c], 12); \
  v[a] = v[a] + v[b] + m[y]; v[d] = uploadedRotateRight32(v[d] ^ v[a], 8); \
  v[c] += v[d]; v[b] = uploadedRotateRight32(v[b] ^ v[c], 7); \
} while (0)
  for (uint8_t r = 0; r < 10; r++) {
    UPLOADED_G(0,4,8,12,sigma[r][0],sigma[r][1]);
    UPLOADED_G(1,5,9,13,sigma[r][2],sigma[r][3]);
    UPLOADED_G(2,6,10,14,sigma[r][4],sigma[r][5]);
    UPLOADED_G(3,7,11,15,sigma[r][6],sigma[r][7]);
    UPLOADED_G(0,5,10,15,sigma[r][8],sigma[r][9]);
    UPLOADED_G(1,6,11,12,sigma[r][10],sigma[r][11]);
    UPLOADED_G(2,7,8,13,sigma[r][12],sigma[r][13]);
    UPLOADED_G(3,4,9,14,sigma[r][14],sigma[r][15]);
  }
#undef UPLOADED_G
  for (uint8_t i = 0; i < 8; i++) state[i] ^= v[i] ^ v[i + 8];
}

inline uint64_t uploadedProgramIdentity(uint8_t version, const uint8_t* data,
                                        uint8_t length) {
  static constexpr uint32_t iv[8] = {
      0x6A09E667U, 0xBB67AE85U, 0x3C6EF372U, 0xA54FF53AU,
      0x510E527FU, 0x9B05688CU, 0x1F83D9ABU, 0x5BE0CD19U};
  uint32_t state[8];
  memcpy(state, iv, sizeof(state));
  state[0] ^= 0x01010008U;  // unkeyed BLAKE2s, 8-byte output
  uint8_t input[UPLOADED_PROGRAM_MAX_BYTES + 2] = {version, length};
  memcpy(input + 2, data, length);
  size_t total = (size_t)length + 2;
  size_t offset = 0;
  while (total - offset > 64) {
    uploadedBlake2sCompress(state, input + offset, offset + 64, false);
    offset += 64;
  }
  uint8_t last[64] = {0};
  memcpy(last, input + offset, total - offset);
  uploadedBlake2sCompress(state, last, total, true);
  return (uint64_t)state[0] | ((uint64_t)state[1] << 32);
}

inline uint64_t uploadedProgramId(uint8_t version, const uint8_t* data,
                                  uint8_t length) {
  return uploadedProgramIdentity(version, data, length);
}

inline uint64_t uploadedProgramId(const UploadedProgram& program) {
  return uploadedProgramIdentity(program.version, program.data, program.length);
}

inline uint8_t uploadedOpcodeExecutionCost(uint8_t opcode) {
  if (opcode == UOP_POW) return 16;
  if (opcode == UOP_SIN || opcode == UOP_COS) return 8;
  if (opcode == UOP_SQRT) return 4;
  if (opcode == UOP_HASH) return 3;
  return 1;
}

inline bool uploadedOpcodeUsesTime(uint8_t opcode) {
  return opcode == UOP_TIME;
}

inline bool uploadedProgramInspect(const UploadedProgram& program,
                                   bool* uses_time = nullptr,
                                   uint8_t* instruction_count = nullptr,
                                   uint16_t* execution_cost = nullptr) {
  if (uses_time) *uses_time = false;
  if (instruction_count) *instruction_count = 0;
  if (execution_cost) *execution_cost = 0;
  if (program.version != UPLOADED_VM_VERSION || program.id == 0 ||
      program.length == 0 || program.length > UPLOADED_PROGRAM_MAX_BYTES ||
      uploadedProgramId(program) != program.id) {
    return false;
  }

  uint8_t depth = 0;
  uint8_t instructions = 0;
  uint16_t cost = 0;
  size_t offset = 0;
  while (offset < program.length) {
    if (++instructions > UPLOADED_PROGRAM_MAX_INSTRUCTIONS) return false;
    uint8_t opcode = program.data[offset++];
    cost += uploadedOpcodeExecutionCost(opcode);
    if (cost > UPLOADED_PROGRAM_MAX_EXECUTION_COST) return false;
    if (uploadedOpcodeUsesTime(opcode) && uses_time) *uses_time = true;
    if (opcode == UOP_CONST) {
      if (offset + sizeof(float) > program.length || depth >= UPLOADED_STACK_MAX)
        return false;
      float value = 0.0f;
      memcpy(&value, program.data + offset, sizeof(value));
      if (!isfinite(value)) return false;
      offset += sizeof(float);
      depth++;
      continue;
    }
    if (opcode == UOP_X || opcode == UOP_Y || opcode == UOP_TIME ||
        opcode == UOP_PIXEL) {
      if (depth >= UPLOADED_STACK_MAX) return false;
      depth++;
      continue;
    }
    if (opcode >= UOP_ADD && opcode <= UOP_POW) {
      if (depth < 2) return false;
      depth--;
      continue;
    }
    if (opcode >= UOP_SIN && opcode <= UOP_FLOOR) {
      if (depth < 1) return false;
      continue;
    }
    if (opcode == UOP_MIX || opcode == UOP_SMOOTHSTEP) {
      if (depth < 3) return false;
      depth -= 2;
      continue;
    }
    return false;
  }
  if (instruction_count) *instruction_count = instructions;
  if (execution_cost) *execution_cost = cost;
  // Programs leave H, S, V, and intensity on the stack, in that order.
  return depth == 4;
}

inline bool uploadedProgramValid(const UploadedProgram& program) {
  return uploadedProgramInspect(program);
}

// Slots are validated at load/install boundaries. Render-time callers can read
// this metadata without recomputing the content hash or revalidating the stack.
inline bool uploadedProgramUsesTimeValidated(const UploadedProgram& program) {
  size_t offset = 0;
  while (offset < program.length) {
    uint8_t opcode = program.data[offset++];
    if (opcode == UOP_TIME) return true;
    if (opcode == UOP_CONST) offset += sizeof(float);
  }
  return false;
}

inline float uploadedHash(float value) {
  if (!isfinite(value)) return 0.0f;
  // Avoid undefined float-to-integer overflow for valid programs that create a
  // very large finite intermediate. Modulo 2^32 matches the Python preview.
  double wrapped = fmod(floor((double)value * 4096.0), 4294967296.0);
  if (wrapped < 0.0) wrapped += 4294967296.0;
  uint32_t h = (uint32_t)wrapped;
  h ^= h >> 16;
  h *= 0x7feb352dU;
  h ^= h >> 15;
  h *= 0x846ca68bU;
  h ^= h >> 16;
  return (float)(h & 0x00FFFFFFU) / 16777215.0f;
}

inline UploadedVmOutput uploadedProgramRun(const UploadedProgram& program,
                                            int64_t synced_us, float x, float y,
                                            uint16_t pixel_index,
                                            uint16_t pixel_count,
                                            bool already_validated = false) {
  UploadedVmOutput failed = {0.0f, 0.0f, 0.0f, 0.0f, false};
  if (!already_validated && !uploadedProgramValid(program)) return failed;
  float stack[UPLOADED_STACK_MAX] = {0};
  uint8_t depth = 0;
  size_t offset = 0;
  static constexpr float kTau = 6.2831853071795864769f;
  while (offset < program.length) {
    uint8_t opcode = program.data[offset++];
    if (opcode == UOP_CONST) {
      memcpy(&stack[depth++], program.data + offset, sizeof(float));
      offset += sizeof(float);
      continue;
    }
    if (opcode == UOP_X) { stack[depth++] = x; continue; }
    if (opcode == UOP_Y) { stack[depth++] = y; continue; }
    if (opcode == UOP_TIME) {
      double seconds = (double)synced_us / 1000000.0;
      stack[depth++] = (float)fmod(seconds, 4096.0);
      continue;
    }
    if (opcode == UOP_PIXEL) {
      stack[depth++] = pixel_count
          ? (float)(pixel_index % pixel_count) / (float)pixel_count
          : 0.0f;
      continue;
    }

    if (opcode >= UOP_ADD && opcode <= UOP_POW) {
      float b = stack[--depth];
      float a = stack[depth - 1];
      float result = 0.0f;
      switch (opcode) {
        case UOP_ADD: result = a + b; break;
        case UOP_SUB: result = a - b; break;
        case UOP_MUL: result = a * b; break;
        case UOP_DIV: result = fabsf(b) < 1e-6f ? 0.0f : a / b; break;
        case UOP_MIN: result = a < b ? a : b; break;
        case UOP_MAX: result = a > b ? a : b; break;
        case UOP_POW:
          result = a < 0.0f ? 0.0f : powf(a, b);
          break;
      }
      stack[depth - 1] = isfinite(result) ? result : 0.0f;
      continue;
    }

    if (opcode >= UOP_SIN && opcode <= UOP_FLOOR) {
      float a = stack[depth - 1];
      float result = 0.0f;
      switch (opcode) {
        case UOP_SIN: result = sinf(kTau * a); break;
        case UOP_COS: result = cosf(kTau * a); break;
        case UOP_ABS: result = fabsf(a); break;
        case UOP_FRACT: result = a - floorf(a); break;
        case UOP_CLAMP01: result = uploadedClamp01(a); break;
        case UOP_NEG: result = -a; break;
        case UOP_SQRT: result = a <= 0.0f ? 0.0f : sqrtf(a); break;
        case UOP_HASH: result = uploadedHash(a); break;
        case UOP_FLOOR: result = floorf(a); break;
      }
      stack[depth - 1] = isfinite(result) ? result : 0.0f;
      continue;
    }

    float c = stack[--depth];
    float b = stack[--depth];
    float a = stack[depth - 1];
    if (opcode == UOP_MIX) {
      float t = uploadedClamp01(c);
      float result = a + (b - a) * t;
      stack[depth - 1] = isfinite(result) ? result : 0.0f;
    } else {  // UOP_SMOOTHSTEP; validation rejected every other opcode.
      float width = b - a;
      float t = fabsf(width) < 1e-6f ? (c >= b ? 1.0f : 0.0f)
                                      : uploadedClamp01((c - a) / width);
      float result = t * t * (3.0f - 2.0f * t);
      stack[depth - 1] = isfinite(result) ? result : 0.0f;
    }
  }

  float hue = isfinite(stack[0]) ? stack[0] - floorf(stack[0]) : 0.0f;
  return {hue, uploadedClamp01(stack[1]), uploadedClamp01(stack[2]),
          uploadedClamp01(stack[3]), true};
}

inline void uploadedProgramSlotsInit(UploadedProgramSlots& slots) {
  memset(&slots, 0, sizeof(slots));
}

inline int uploadedProgramFind(const UploadedProgramSlots& slots, uint64_t id) {
  if (!id) return -1;
  for (uint8_t i = 0; i < UPLOADED_PROGRAM_SLOTS; i++)
    if (slots.slots[i].id == id && uploadedProgramValid(slots.slots[i])) return i;
  return -1;
}

// Hot render-path lookup. Slots are fully validated when loaded or installed;
// re-running BLAKE2s for every pixel frame would turn identity hardening into a
// permanent CPU tax. Control/distribution paths keep using the validating find.
inline int uploadedProgramFindValidatedSlot(const UploadedProgramSlots& slots,
                                            uint64_t id) {
  if (!id) return -1;
  for (uint8_t i = 0; i < UPLOADED_PROGRAM_SLOTS; i++)
    if (slots.slots[i].id == id) return i;
  return -1;
}

// Install into the inactive slot. The active program is never overwritten, so
// a partially distributed replacement cannot disturb the currently running
// uploaded look.
inline int uploadedProgramInstallSlot(const UploadedProgramSlots& slots,
                                      const UploadedProgram& program,
                                      const uint64_t* active_ids,
                                      uint8_t active_count) {
  int existing = uploadedProgramFind(slots, program.id);
  if (existing >= 0) return existing;
  for (uint8_t i = 0; i < UPLOADED_PROGRAM_SLOTS; i++) {
    bool active = false;
    for (uint8_t j = 0; j < active_count; j++)
      active = active ||
          (active_ids[j] != 0 && slots.slots[i].id == active_ids[j]);
    if (!active) return i;
  }
  return -1;
}

inline bool uploadedProgramInstallPreserving(UploadedProgramSlots& slots,
                                             const UploadedProgram& program,
                                             const uint64_t* active_ids,
                                             uint8_t active_count,
                                             uint8_t* changed_slot) {
  if (!uploadedProgramValid(program)) return false;
  int slot = uploadedProgramInstallSlot(slots, program, active_ids, active_count);
  if (slot < 0) return false;
  bool changed = slots.slots[slot].id != program.id;
  slots.slots[slot] = program;
  if (changed_slot) *changed_slot = changed ? (uint8_t)slot : UINT8_MAX;
  return true;
}

inline bool uploadedProgramInstall(UploadedProgramSlots& slots,
                                   const UploadedProgram& program,
                                   uint64_t active_id, uint8_t* changed_slot) {
  return uploadedProgramInstallPreserving(
      slots, program, &active_id, active_id == 0 ? 0 : 1, changed_slot);
}

inline void uploadedStatusInit(UploadedProgramStatusTable& table) {
  memset(&table, 0, sizeof(table));
}

inline int uploadedStatusFind(const UploadedProgramStatusTable& table,
                              const uint8_t mac[6]) {
  for (uint8_t i = 0; i < table.count; i++)
    if (memcmp(table.entries[i].mac, mac, 6) == 0) return i;
  return -1;
}

inline bool uploadedStatusUpsert(UploadedProgramStatusTable& table,
                                 const uint8_t mac[6], uint8_t vm_version,
                                 uint64_t requested_id, bool available,
                                 const FirmwareVersion& firmware,
                                 int64_t now_us) {
  int index = uploadedStatusFind(table, mac);
  if (index < 0) {
    if (table.count >= UPLOADED_STATUS_MAX) return false;
    index = table.count++;
  }
  UploadedProgramStatusEntry& entry = table.entries[index];
  memcpy(entry.mac, mac, 6);
  entry.vm_version = vm_version;
  entry.requested_id = requested_id;
  entry.available = available ? 1 : 0;
  entry.firmware = firmware;
  entry.last_us = now_us;
  return true;
}

inline void uploadedStatusInvalidate(UploadedProgramStatusTable& table,
                                     const uint8_t mac[6]) {
  int index = uploadedStatusFind(table, mac);
  if (index < 0) return;
  table.entries[index].available = 0;
  table.entries[index].last_us = 0;
}

inline bool uploadedStatusReady(const UploadedProgramStatusTable& table,
                                const uint8_t mac[6], uint64_t requested_id,
                                const FirmwareVersion& firmware,
                                int64_t not_before_us = 0) {
  int index = uploadedStatusFind(table, mac);
  if (index < 0) return false;
  const UploadedProgramStatusEntry& entry = table.entries[index];
  return entry.vm_version == UPLOADED_VM_VERSION && entry.available &&
         entry.requested_id == requested_id &&
         firmwareSame(entry.firmware, firmware) &&
         entry.last_us > 0 && entry.last_us >= not_before_us;
}

inline uint32_t uploadedStatusMacHash(const uint8_t mac[6]) {
  uint32_t hash = 2166136261U;
  for (uint8_t i = 0; i < 6; i++) {
    hash ^= mac[i];
    hash *= 16777619U;
  }
  return hash;
}

inline void uploadedStatusScheduleInit(UploadedStatusTxSchedule& schedule) {
  memset(&schedule, 0, sizeof(schedule));
}

inline void uploadedStatusScheduleRequest(UploadedStatusTxSchedule& schedule,
                                          int64_t now_us,
                                          const uint8_t mac[6]) {
  schedule.pending = true;
  schedule.failures = 0;
  schedule.next_us = now_us +
      (uploadedStatusMacHash(mac) % (UPLOADED_STATUS_INITIAL_SPREAD_US + 1));
  schedule.hold_until_us = now_us + UPLOADED_STATUS_HOLD_US;
}

inline bool uploadedStatusScheduleDue(const UploadedStatusTxSchedule& schedule,
                                      int64_t now_us) {
  return schedule.pending && now_us >= schedule.next_us &&
         now_us < schedule.hold_until_us;
}

inline bool uploadedStatusScheduleExpired(
    const UploadedStatusTxSchedule& schedule, int64_t now_us) {
  return schedule.pending && schedule.hold_until_us > 0 &&
         now_us >= schedule.hold_until_us;
}

inline void uploadedStatusScheduleResult(UploadedStatusTxSchedule& schedule,
                                         int64_t now_us,
                                         const uint8_t mac[6],
                                         bool delivered) {
  if (delivered) {
    schedule.pending = false;
    schedule.failures = 0;
    schedule.next_us = 0;
    schedule.hold_until_us = 0;
    return;
  }
  if (schedule.failures < 255) schedule.failures++;
  uint8_t shift = schedule.failures > 4 ? 4 : schedule.failures;
  int64_t delay_us = 50000LL << shift;
  if (delay_us > UPLOADED_STATUS_RETRY_MAX_US)
    delay_us = UPLOADED_STATUS_RETRY_MAX_US;
  delay_us += uploadedStatusMacHash(mac) % 50001U;
  schedule.next_us = now_us + delay_us;
}

inline bool uploadedProgramHoldsRadio(bool install_pending,
                                      const UploadedStatusTxSchedule& schedule,
                                      int64_t now_us) {
  return install_pending ||
         (schedule.pending && now_us < schedule.hold_until_us);
}

inline UploadedRepairAction uploadedRepairAction(
    bool target_active, bool verification_active, bool firmware_matches,
    bool status_ready, bool status_after_registration) {
  if ((!target_active && !verification_active) || !firmware_matches)
    return UPLOADED_REPAIR_NONE;
  if (status_ready)
    return !status_after_registration ? UPLOADED_REPAIR_QUERY
                                      : UPLOADED_REPAIR_NONE;
  return UPLOADED_REPAIR_INSTALL;
}

inline uint64_t uploadedRepairProgramId(uint64_t staged_id,
                                        bool verification_active,
                                        uint64_t assigned_active_id) {
  return verification_active ? staged_id : assigned_active_id;
}
