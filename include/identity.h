// Node identity: a stable ID and (x,y) position in the field.
//
// Persisted in NVS (flash) so a node cold-boots, reads who/where it is, hears a
// beacon, and resumes — a battery swap looks like a single blink. The NVS I/O
// itself lives in main.cpp (Arduino Preferences); this header is just the
// dependency-free data + defaults so it can be reasoned about and tested.
#pragma once

#include <stdint.h>

struct NodeIdentity {
  uint16_t id;  // unique per node; 0 = unprovisioned
  float    x;   // field coordinate (arbitrary units, e.g. meters)
  float    y;
  uint8_t  group_id;  // zero-based show group; cached from conductor table
};

// True once a node has been given an ID over serial (see provisioning in main).
inline bool identityProvisioned(const NodeIdentity& n) { return n.id != 0; }
