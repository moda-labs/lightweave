# Do Baskets Dream — System Architecture & Design

Companion to [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md). The
brief is the original spec; this doc records the architectural decisions made
while building it. Status tags: **[done]** shipped, **[wip]** in progress,
**[planned]** designed, not yet built.

---

## 1. Design philosophy: parametric field, not addressed pixels

The conductor broadcasts a compact **pattern config** (which pattern + a few knobs + the
clock); every node computes its own color locally from **`f(x, y, t)`** using its
stored position. We deliberately do **not** push per-node frames.

Why this is the right core:
- **Resilience.** A node that misses a beacon keeps evaluating `f(x,y,t)` against
  the synced clock — it free-runs, never blanks. This is the brief's hard
  requirement, and it only works because content is a function, not pushed data.
- **Scale.** One small broadcast drives 5 nodes or 500. Bandwidth is constant.
- **Power.** Minimal radio traffic fits the modem-sleep / battery budget.

Trade-off: patterns must be expressible as smooth functions of position, time, and
a handful of parameters. That covers pulses, waves, ripples, and drifts — the
installation's whole vocabulary. Arbitrary imagery/text is explicitly out of
scope (it would break resilience and the power budget).

## 2. Roles **[done]**

Every board runs **one identical firmware image**. Role is a runtime value in NVS
(default **performer**), set once over serial (`role conductor|performer`). One
conductor per field; it is typically a **ring-less, headless timekeeper** so all
visible rings are on performers.

- Conductor: broadcasts the clock beacon + pattern config; renders against its own
  clock (if it has a ring).
- Performer: locks an offset to the conductor's clock; renders against synced time;
  free-runs on missed beacons.

Rationale for runtime role: flashing different binaries per role was the single
biggest source of operational error during bring-up (wrong role on wrong board,
two conductors at once). One image + NVS role removes that entirely.

## 3. Identity vs. position — keep them separate

- **Identity = the ESP32 MAC address.** **[done]** Read at boot
  (`esp_read_mac`/`ESP_MAC_WIFI_STA`), shown in `info`, and reported in REGISTER so
  the conductor's roster is MAC-keyed. Globally unique, burned in, zero
  provisioning, no collisions. The friendly `id` persists as a human label, but the
  MAC is authoritative. (Layout table keying on MAC is Half 2.)
- **Position = `(x, y)`.** **[done, manual]** Per-deployment, changes whenever the
  field is re-laid. **Relative geometry only — no GPS / metric coords needed**;
  patterns (waves, ripples) care about relative positions, so normalized image
  coordinates from calibration are sufficient. Optional reference markers can set
  orientation/scale if a pattern must align to a real-world feature.

## 4. Pattern model **[done for 1-D; 2-D planned]**

Conductor broadcasts the pattern in the beacon: `pattern_id`, `brightness`,
`palette_id`, `params[4]`, plus the clock (`epoch_us`, `seq`).

- `params` are pattern-specific knobs for live tuning (e.g. sweep period in ms,
  wavelength ×100). Persisted in NVS on the conductor so a power-cycle keeps the
  tuned look. **[done]** (every node persists/restores the pattern; keys
  `pat`/`bri`/`p0`..`p3` in the `"node"` namespace.)
- Patterns are `f(x, y, t)`:
  - `PULSE` — uniform breathing, all nodes in unison. **[done]**
  - `PALETTE_DRIFT` — smooth rainbow hue cycle; `params[0]` = cycle period in ms,
    `params[1]` = spatial hue offset (×100 cycles per x unit) so the rainbow can
    travel across the field or run in unison (0). **[done]**
  - `SWEEP` — traveling wave across `x`. **[done]** (1-D today.)
  - `SOLID` — every pixel full RGBW at `brightness`: the worst-case power draw, a
    bench rig for measuring the per-node LED ceiling (not a show pattern). **[done]**
  - **[planned]** true 2-D: plane wave at an arbitrary **angle**, **radial ripple**
    from a center point. `params` encode direction/center.

Every node hard-clamps rendered brightness to `MAX_BRIGHTNESS` (config.h, **192**),
so no pattern or broadcast config can exceed the per-node power budget regardless of what is
authored. Set from the worst-case bench measurement (solid white at 255 drew
0.76 A @ 5 V); see power management below.

Pure pattern math lives in `include/pattern_math.h` (host-unit-tested); the
LED-library binding is in `include/patterns.h`.

### 4.1 Show program (pattern scheduling) **[planned]**

Beyond a single live pattern, the conductor holds a **show program** in NVS — a
schedule of *what plays when* (e.g. pattern A for a while, then B; calmer/dimmer
late; brightness ramps). The conductor walks the program against its clock and
**broadcasts the current pattern each beacon**; nodes render whatever arrives and
stay dumb. So scheduling lives entirely on the conductor and needs no per-node
logic. Open considerations: smooth **transitions/crossfades** between patterns
(would need a blend factor in the pattern), and the schedule's **time base**
(uptime vs. dusk-relative once the LDR lands in Milestone 3, vs. a set wall-clock).

### 4.2 Power instrumentation — INA228 **[done — firmware/UI; awaiting chip verification]**

Firmware landed 2026-07-04, built ahead of the pilot-batch chips (arriving with
the Monday order) so their arrival is pure hardware verification: I2C probe at
boot (one image everywhere — a node without the chip skips telemetry silently,
`skipReset=true` so a serial monitor's DTR reset can't wipe the night's
accumulated Wh), pure logic in the host-tested `include/powermon.h`
(conversions, plausibility gate, radio-aware report scheduler), a `MSG_POWER`
unicast on the existing REGISTER path (no PROTO_VERSION bump — new type only),
ungated conductor-side logging, conductor retention of the latest per-MAC sample
for machine JSON, and `power` / `power reset` serial commands.

A precision power monitor (**INA228** breakout, 15 mΩ on-board shunt) wired in series
between battery+ and the buck input, on **1–2 instrumented reference nodes only** —
not all 60 field units. Unlike the INA219, it has real hardware **energy/charge
accumulation registers**: a background digital engine integrates continuously in
**continuous conversion mode**, so there's no firmware polling-rate/aliasing math —
`readEnergy()` (÷3600 for Wh) is a true continuous integral regardless of when it's
called. `resetAccumulators()` at the start of a night's run gives a clean per-night
Wh figure; `readCharge()` gives Ah as a free cross-check. Must stay in continuous
mode — triggered mode invalidates the accumulators since the device stops tracking
elapsed time.

**Readout path**, cheapest to most capable:
1. **Bench/USB-tethered:** print `readEnergy()` to serial — validates the sensor,
   not useful for a battery-only overnight run.
2. **Single-night validation:** run untethered overnight, reconnect USB the next
   morning and read the accumulator (plugging in USB adds power alongside the
   battery rather than replacing it, so this doesn't reset the reading — but never
   disconnect the battery first, since any power gap zeroes the registers).
3. **Fleet-scale:** each performer returns its
   accumulated Wh to the conductor as a small ESP-NOW unicast, piggybacking on the
   existing bidirectional-ESP-NOW path used by `MSG_REGISTER` (§7). The conductor
   logs every report and exposes the latest sample in `/api/state`; the control
   plane rolls sparse reference nodes into an estimated field draw and per-node
   battery SOC.
4. **Future field diagnostic (optional):** expose the current Wh reading over BLE
   for a phone-app spot-check, independent of the conductor link.

SOC is energy-counter based, not voltage-curve based: the KUNLUN model 1230 pack
default is **384 Wh**, and the control plane computes `100% - Wh used since full /
capacity`. Voltage is used only as a full-charge anchor: a sample at or above the
configured full-voltage threshold (default **14.4 V**) resets that node's SOC
anchor to 100%. Operators can also click **Sync to 100%** per metered node after
charging. This is a representative-sample tool for sizing Milestone 3's power
levers (§8.1, Lever 2 below), not a requirement to install INA228 on every node.

## 5. Layout table — conductor-authoritative `MAC → (x,y)` **[done]**

The conductor holds the authoritative field map and broadcasts it; each node finds
its own MAC, adopts its `(x,y)`, and **caches it in NVS**. Edit one table to
re-arrange the whole field.

- **The conductor is authoritative and stores the table in its own NVS.** The
  field runs with **no laptop present** — the conductor is the coordination point
  for the table just as it is for the clock and the pattern config.
- Resilient: a node needs to hear the table only once, then survives on the cache.
- Cheap: 14 B/node on the wire (`TableRow`) → ~840 B for 60 nodes → ~4 `MSG_TABLE`
  packets (17 rows each). Steady-state rebroadcast is a slow backstop
  (`TABLE_INTERVAL_US`, 60 s — positions are static and cached in NVS); the
  moments that actually need the table travel out of band: `assign` broadcasts
  the full table immediately, and a REGISTER from a node that is **new to the
  roster or unprovisioned (id 0)** gets an immediate **single-row reply**
  (23 B). The row reply is the delivery guarantee under radio duty-cycling: a
  REGISTER is the one moment the conductor provably knows that node's radio is
  on (TX is gated on radio-up), so the reply lands inside the sender's open
  listen window instead of playing the ~13%-per-broadcast lottery, and a missed
  reply is retried for free by the node's next REGISTER (10 s). Steady state
  (all nodes known + provisioned) costs zero table traffic beyond the backstop.

Implementation: the table logic is the dependency-free, host-tested
`include/table.h` (`tableSet`/`tableLookup`/`tableRemove`); the wire side —
chunk math, receive-side length validation, own-row scan, and the row-reply
decision + builder (`tableRowReplyWanted`/`tableRowBuild`) — is the equally
pure, host-tested `include/table_wire.h`; `main.cpp` owns the NVS
blob, the radio calls, the reply queue (stash in the recv callback, drain in
`loop()` — same shape as the power-report queue), and the node-side adoption. The conductor edits it
over serial — `assign <mac> <x> <y>`, `table`, `forget <mac>` — and pushes the
change immediately. A node stashes its row in the recv callback and applies +
`identitySave()`s it from `loop()` (no flash write in the callback). Verified on
hardware: a node adopts a position set only on the conductor, with no serial to
that node, and keeps it across a reboot. Manual `pos x y` over serial remains as a
fallback/override for tests and stragglers.

### 5.1 Node replacement **[done via assign + forget]**

A dead lamp's spare has a new MAC, so replacement is a **table edit**, not a
re-calibration — because positions are MAC-keyed and the table is
conductor-authoritative. Physically drop the spare into the dead one's spot, then
transfer the position from the old MAC to the new one:

1. Spare boots as a performer (default), registers, and shows in the `roster`.
2. Operator runs `assign <newMAC> <x> <y>` then `forget <oldMAC>` on the conductor
   (a single `replace <oldMAC> <newMAC>` convenience command is a possible later
   nicety, but the two existing commands already cover it).
3. Conductor rebroadcasts; the spare caches its `(x,y)` to NVS and joins the field.

No drone, no re-fly — a single swap is one command. Getting the new MAC: read it
from the spare's serial `info`, or label spares with their MAC, or let the
conductor surface "new unknown MAC seen." Only worth a full re-calibration if many
nodes move at once.

### 5.2 Control plane — operator/admin interface **[planned]**

The conductor stores the authoritative **routing table** (§5) *and* **show
program** (§4.1) in NVS and runs the field with **no laptop present**. The laptop
is a **transient admin tool**, plugged into the conductor over USB serial only
when you want to *change something*:

- **Routing table edits:** reposition nodes (new `(x,y)`), or replace a node
  (§5.1) — i.e. change *where* the light is.
- **Show program edits:** pattern selection, appearance (brightness, sweep
  speed/wavelength, palette), and schedule (what runs when) — i.e. change *what*
  the light does.
- It is also the host that runs the **calibration CV** (§6).

The conductor persists every pushed change to NVS, then executes/broadcasts it —
so the laptop can walk away and the field keeps running the new config. Likely
form: a **local web UI** (laptop server + browser) over a structured serial
command/response protocol (§7) — chosen because the table/map editor, live pattern
controls, and calibration wizard are far better visually than a CLI. The protocol
must support **bulk table transfer** (60 rows won't fit a typed line) and clean
acks/errors so a program can drive the conductor reliably.

**Not a runtime dependency:** unplug the admin host and the conductor + field
continue on their stored table and program.

**Deployment (no router/internet):** the admin host is a **Raspberry Pi** cabled
to the conductor over USB. The Pi runs as its **own Wi-Fi access point** (hotspot
via NetworkManager / RaspAP), so a phone joins the Pi's SSID directly and browses
the UI (`http://baskets.local` or the AP IP) — no router, no internet, works on the
playa. Set a WPA2 password. The Pi can also run the calibration CV (§6) on-site, so
no separate laptop is needed. The Pi is a permanent convenience but **stays
non-essential to runtime** — the authoritative table + show program live in the
conductor's NVS, so the field survives the Pi being removed or failing.

```
phone --WiFi--> Pi (AP + web UI + CV + serial bridge) --USB--> conductor --ESP-NOW--> field
```

An internet-connected deployment variant using Starlink Wi-Fi, a named
Cloudflare Tunnel, and application-authenticated browser sessions is specified
in [`REMOTE_ADMIN.md`](REMOTE_ADMIN.md). It preserves the same non-dependency
rule: loss of the internet-facing admin path does not affect conductor or
performer runtime.

## 6. Auto-calibration — drone + computer vision **[planned]**

Goal: build the `MAC → (x,y)` table by **survey**, not by hand (manual surveying of
60 lamps in a field is slow and inaccurate). Technique: temporal LED mapping — the
lamps blink in a known schedule, a drone films from above, and CV recovers each
lamp's position and associates it with a MAC.

The synced clock makes the capture **open-loop** — no live RF during the fly-over.

### Procedure
1. **Register (one-time, live).** Conductor broadcasts "enter calibration"; each
   node phones home once with its MAC. Conductor builds the roster (and thus the
   expected count).
2. **Freeze + distribute roster (live, must be reliable).** Conductor sorts MACs
   ascending, broadcasts the finalized roster + a calibration **start epoch**;
   nodes ACK. Each node computes its **rank** in the sorted list → its blink slot.
3. **Capture (open-loop, no live RF).** At the start epoch all lamps **flash
   together once** (a timeline anchor for the video), then each rank blinks in its
   **absolute synced-time slot** (`rank × slot_width`). Drone records a stable
   top-down clip. Sequential, one lamp lit at a time, with off-gaps between slots.
4. **Process (laptop).** CV detects each blink, converts its timestamp → slot →
   rank → MAC (via the roster), and records the blob's normalized `(x,y)`. Reports
   any **empty slots** = MACs that failed to map.
5. **Distribute (live).** Load `MAC → (x,y)` into the conductor; broadcast; nodes
   cache to NVS (§5).

### Why absolute time slots (not just activation order)
Pure ordering shifts the entire tail of the table if one lamp is dead/occluded.
Binding each rank to an absolute synced-time window + the start-flash anchor means
a missing lamp leaves an **empty slot** instead of mis-mapping everyone. The
register step gives the expected roster, so empty slots pinpoint exactly which MACs
need a manual `pos` fallback. (Optional periodic all-flash re-anchors long runs.)

### Notes & decisions
- **Blink encoding:** start **sequential** (bulletproof CV: find the one lit blob).
  Temporal binary coding (all blink, ~6 frames) is a future speed optimization that
  trades CV robustness for time.
- **Coordinates:** CV output is normalized pixel `(x,y)` — sufficient for relative
  patterns. Add reference markers only if real-world orientation matters.
- **Sync during capture:** leaving the (light-less) conductor beacon running keeps
  clocks tight; fully silent free-run is also fine (drift over ~60 s is sub-ms).
- **Off-board:** the laptop runs CV and feeds the table to the conductor over USB;
  the ESP32 never runs CV. Prototype the CV on a phone photo of 3 lamps before
  investing in the drone workflow.
- **Idempotent:** re-fly and re-calibrate at will.

## 7. Wire protocol **[partly done]**

- **[done]** Common header `MsgHeader {uint32 magic; uint8 version; uint8 type;}`
  on every packet; receiver validates magic+version then dispatches on `type`.
  Types: `MSG_BEACON` (hot path), `MSG_REGISTER`, `MSG_TABLE` (live);
  `MSG_ROSTER`/`MSG_ACK` reserved. `PROTO_VERSION` is rejected on mismatch.
- **[done]** `MSG_BEACON` (clock + pattern) broadcast on a fixed channel to
  `FF:FF:FF:FF:FF:FF`, `WIFI_STA`. The hot path (sync.h) reads `epoch_us`+`seq`.
- **[done]** Bidirectional ESP-NOW: a performer learns the conductor's MAC from the
  recv-info, adds it as a peer, and unicasts
  `MSG_REGISTER {mac, id, fw, build, dirty, version}` every 10 s; the conductor
  builds a MAC-keyed roster (`roster` serial command). `fw` is wire
  compatibility (`PROTO_VERSION`); `version` + `build` + `dirty` are the OTA
  safety marker that catches same-protocol stale firmware.
- **[done]** `MSG_TABLE`: the conductor broadcasts the layout table in chunks
  (`TableRow` ×17/packet); nodes adopt their own row. `chunk`/`chunks` fields let a
  receiver tell how much it has seen.
- **[done]** `MSG_POWER`: an INA228-instrumented performer unicasts its
  hardware-accumulated energy/charge to the conductor (§4.2), reusing the
  REGISTER unicast path. Added without a PROTO_VERSION bump — no existing
  layout changed, and receivers ignore unknown types via the dispatch default.
- **[done]** `MSG_OTA_BEGIN`, `MSG_OTA_CHUNK`, `MSG_OTA_END`: conductor
  broadcasts a staged firmware image during manual maintenance-mode OTA.
  Performers write the image into their OTA partition and accept it only after
  size and CRC checks pass. This is field-wide only; no selected-node firmware
  updates.
- **[done]** `MSG_OTA_STATUS`: performers report begin/writing/complete/error
  status with offset and CRC, and the API filters stale statuses. If no fresh
  terminal ACKs arrive after reboot, the API can record per-node completion from
  verified post-reboot field state.
- **[planned]** `MSG_ACK` + richer machine Pi↔conductor serial (lands with the Pi
  UI).
- Time base: 64-bit `esp_timer` microseconds throughout (no 32-bit `millis` wrap).

### 7.1 OTA policy, transfer, and recovery **[done; 3-board bench verified]**

OTA is manual maintenance-mode only and field-wide only. The system must never
offer selected-node firmware updates as a normal workflow. Mixed firmware can
still happen after a failed update, but it is treated as an error/recovery state.

The foundation is in place: device builds get a release version from `VERSION`,
a git-derived 32-bit build id, and dirty flag via `scripts/firmware_build_id.py`;
performers report that identity in REGISTER; the conductor exposes
conductor/per-node firmware in machine state; the control plane shows field
firmware consistency in Operations, links build hashes to GitHub commits, and
flags `Firmware mismatch` in the Node List.

The transfer path is also in place: the control plane stages a `.bin` artifact,
streams it over machine serial with `ota_begin`/`ota_chunk`/`ota_end`, the
conductor writes its own OTA partition, and the conductor broadcasts the same
chunk stream to performers via ESP-NOW. The UI shows install progress by chunk.
This was hardware-verified on the 3-board bench on 2026-07-06, including a
same-protocol mixed-firmware recovery that restored performer #1 from
`0.3.0-mismatch` to `0.3.0`. Serial chunk timeouts and retryable chunk NACKs are
retried, duplicate already-written chunks are idempotent on both conductor and
performers, and unsafe mid-chunk resume offsets are rejected instead of papering
over a partial write. The current serial/ESP-NOW chunk payload is 128 bytes for
command-buffer margin. Firmware requires each decoded chunk length to equal the
expected full/tail length at the current offset; this avoids the old failure mode
where a truncated but even-length hex command decoded as a shorter chunk and
advanced the flash writer to a non-chunk boundary. OTA maintenance beacons keep
performer radios awake for the window so duty cycling does not fight the updater.
Performer OTA status is freshness filtered; the API only reports install success
after every expected placed performer reports complete or verifies from live
post-reboot firmware consistency. Missing placed lanterns block install with a
Recovery row, and post-reboot verification failures synthesize per-node failed
OTA rows for expected performers that did not verify. A final `ota_end` serial
ACK timeout after all bytes land is treated as a post-reboot verification path,
not an immediate install failure; periodic `ota_progress` poll timeouts are
recorded and ignored while chunk transfer continues. Remaining deployment
hardening: decide whether a 60-node deployment needs explicit performer ACK/retry
beyond the current status reporting.

The control plane derives a Recovery summary from live state and the last install
attempt. It classifies missing placed lanterns, same-protocol mixed firmware, and
failed OTA nodes into one operator action surface. Mixed firmware is never a
normal running state: enter maintenance mode, wait for readiness, and reinstall
the staged firmware field-wide. Same-protocol mixed firmware is allowed to
proceed with maintenance install as the recovery action once all placed nodes are
present. Failed OTA installs instruct the operator to reset the maintenance
window and rerun the same staged firmware after nodes check back in.

## 8. Resilience model

- Missed beacon → free-run on last offset; re-lock on next. **[done]**
- Late/bogus beacon → **the offset is disciplined, not set.** A beacon whose implied
  offset jumps more than `SYNC_DEFAULT.gate_us` (100 ms) past the coasting clock is
  gated out (it's a congestion-delayed or stray packet, not real drift — which is
  sub-ms between beacons); trusted corrections *slew* at ≤2 ms/beacon so animations
  never step. A genuine conductor jump (reboot / master change) trips every node's
  gate together and force-re-locks after `relock_after` (8) rejects, so the field
  moves to the new timeline in lockstep rather than one node lurching at a time. This
  is what keeps a single delayed packet from yanking a node off the shared clock.
  Diagnostics: the performer status line's `rej=` counter. **[done]** (`sync.h`)
- Cold boot → read role/identity/position from NVS + MAC from efuse, lock within
  ~1–2 s, resume. **[done — role/pos/MAC; table-assigned position is cached to the
  same NVS pos keys, so it survives a reboot without re-hearing the table]**
- Calibration capture is open-loop (no live RF dependency during the fly-over).
- NVS caches (role, position, pattern config) survive power cycles / battery swaps.

### 8.1 Performer radio duty-cycle **[done — Stage A, hardware-verified]**

The free-run property (a performer renders `f(x,y,t)` from the synced clock without
needing live RF) is what lets us **power the radio off** most of the time. A
performer wakes the radio for a brief listen window every few seconds — just long
enough to catch a beacon and re-lock the clock (and pick up any pattern/table
change) — then sleeps it and keeps rendering from the coasting clock. This is the
right lever for the night draw because the radio is RX-dominated and **modem-sleep
is ineffective in connectionless ESP-NOW** (no AP/DTIM, so RX otherwise stays on).

Decisions: the schedule is pure, host-tested logic (`include/powersave.h`,
mirroring `sync.h`); the on-device glue (`main.cpp`) owns the teardown/bring-up,
which must **re-add the broadcast peer and recv callback on every wake** because
`esp_wifi_stop()`/`start()` drops the peer table. The cold-boot window is held open
until the first beacon is caught, so a battery swap still re-locks fast (the
"single blink" guarantee) before any sleeping begins. The **conductor is exempt**
(it must beacon at 4 Hz and is wall-powered) — gated on `role == performer`. A
runtime/NVS toggle (`powersave on|off`) exists so the night draw can be A/B'd on
the meter. Trade-off: a pattern/position change lands up to one OFF interval (~4 s)
late — acceptable for a slow art piece. The off interval is now also a runtime
power-policy field broadcast by the conductor, so the UI can tune it without a
firmware rebuild.

### 8.2 Schedule-driven deep sleep **[done in firmware/UI; hardware verification owed]**

The primary calendar-life policy is now conductor-authoritative schedule, not
photodiode sensing. The conductor persists a `PowerPolicy` and includes it in
every beacon: light-sleep/radio check interval, deep-sleep check interval,
LED-on start/end minutes, current minute-of-day, schedule-enabled, and
force-awake/force-sleep overrides. Performers apply that policy directly. Outside
the LED window, or under forced sleep, they clear LEDs and deep-sleep for the
configured check interval; inside the window they render normally. The Operations
UI sends the current local minute whenever the policy is saved, so the conductor
can keep evaluating the wall-clock schedule without NTP.

The `wake on|off` concept is now the same force-awake bit as the UI override.
It keeps boards on for debugging/field testing and wins over the schedule. The
one-click Field power controls preserve the schedule: Sleep field sets the
force-sleep bit, Wake field sets force-awake, and Follow schedule clears both.
old photodiode/dusk path remains off by default as a fallback/experiment; it is
not required for the main installation behavior.

## 9. Milestone mapping

| Milestone | Status |
|---|---|
| 1 — sync proof (conductor + performers) | ✅ done, hardware-verified |
| 2 — NVS identity + position-aware sweep | ✅ done, hardware-verified |
| Refactor — symmetric runtime role + NVS pattern persistence + rainbow drift pattern | ✅ done, hardware-verified |
| Protocol foundation, Half 1 — typed header, MAC identity, bidirectional ESP-NOW, registration + roster | ✅ done, hardware-verified |
| Protocol foundation, Half 2 — MAC→(x,y) layout table broadcast + NVS cache (`assign`/`table`/`forget`) | ✅ done, hardware-verified |
| Control plane — structured machine Pi↔conductor serial (bulk table/show-program) | ✅ done for dev laptop UI/API; Pi packaging still planned |
| Auto-calibration — register / roster / blink + laptop CV | 📐 planned |
| 3 — power management (radio duty-cycle, schedule deep-sleep, optional LDR fallback, INA228 energy monitor) | 🛠 in progress — Lever 1 Stage A (performer radio duty-cycle) ✅ done + host-tested + hardware-verified + measured (85→~55 mA @ 12V); Stage B (CPU light-sleep between work, `napsched.h`) ✅ hardware-verified on bench 2026-07-03 (power re-measure owed); schedule-driven deep sleep ✅ code-complete + host-tested + UI/API built, hardware verification owed; photodiode dusk sensing is now optional/fallback; INA228 instrumentation (§4.2) ✅ firmware done + host-tested (`powermon.h`, `MSG_POWER`), awaiting the chip |
| 4 — battery power + ET900 draw measurement (go/no-go) | 📐 planned |
| 5 — OTA + enclosure | 🛠 OTA transfer/recovery done and 3-board bench-verified; enclosure/RF still planned |

## 10. Resolved & open decisions

Resolved:
- **Master table & show program: conductor-authoritative, stored in conductor
  NVS.** Field runs laptop-free; the laptop is a transient editor only (§5, §5.2).

Open:
- 2-D pattern parameter encoding (how to pack angle/center into `params[4]`).
- Pattern transitions/crossfades, and the show-program time base (uptime vs.
  dusk-relative vs. wall-clock) (§4.1).
- Admin UI form: local web UI (current lean) vs. CLI-first.
- Temporal-coded calibration as a later speed upgrade (§6).
