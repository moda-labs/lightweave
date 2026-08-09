# Do Baskets Dream — System Architecture & Design

Companion to [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md). The
brief is the original spec; this doc records the architectural decisions made
while building it. Status tags: **[done]** shipped, **[wip]** in progress,
**[planned]** designed, not yet built.

---

## 1. Design philosophy: parametric field, not pushed pixel frames

The conductor broadcasts compact **pattern configs** (which pattern + a few knobs for
each lantern group, plus the clock); every node selects its assigned group and
computes its output locally from **`f(x, y, t)`** using its stored position. Ring-aware patterns may additionally evaluate
**`f(x, y, pixel_index, t)`** for each local emitter. We deliberately do **not**
push per-node or per-pixel frames.

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
(default **performer**), set once over serial
(`role conductor|performer|relay`). One primary conductor per field; it is
typically a **ring-less, headless timekeeper** so all
visible rings are on performers.

- Conductor: the only authority; broadcasts the clock beacon + group pattern configs; renders against its own
  clock (if it has a ring).
- Relay: an always-on, one-hop transport node. It follows only a direct primary,
  forwards primary traffic downstream and performer replies upstream, renders
  against the primary clock if it has LEDs, and never self-promotes.
- Performer: locks an offset to the conductor's clock; renders against synced time;
  free-runs on missed beacons.

Rationale for runtime role: flashing different binaries per role was the single
biggest source of operational error during bring-up (wrong role on wrong board,
two conductors at once). One image + NVS role removes that entirely.

## 3. Identity vs. position — keep them separate

- **Machine identity = the ESP32 MAC address.** **[done]** Read at boot
  (`esp_read_mac`/`ESP_MAC_WIFI_STA`), shown in `info`, and reported in REGISTER so
  the conductor's roster is MAC-keyed. Globally unique, burned in, zero
  provisioning, no collisions. The MAC remains authoritative for routing.
- **Board number = permanent human identity.** **[done; 53-board inventory complete]**
  Each MAC receives one unique positive `id` that is printed on and follows the
  physical board, independent of field placement. The conductor retains the
  MAC-to-ID inventory while a board is offline and rehydrates erased performer
  NVS from the next registration. The factory watcher adopts an existing ID or
  atomically allocates the lowest unused number, verifies it on the board, and
  prints a prominent label prompt. Multi-board stations reserve IDs through the
  conductor before writing performer NVS, so two flashing hosts cannot allocate
  the same number from divergent local registries.
- **Position = `(x, y)`.** **[done, manual]** Per-deployment, changes whenever the
  field is re-laid. **Relative geometry only — no GPS / metric coords needed**;
  patterns (waves, ripples) care about relative positions, so normalized image
  coordinates from calibration are sufficient. Optional reference markers can set
  orientation/scale if a pattern must align to a real-world feature.

## 4. Pattern model **[done for field-space, 2-D, and first ring-local effect]**

Conductor broadcasts eight independent group pattern configs in the beacon, each
with `pattern_id`, `brightness`, `palette_id`, and `params[4]`, plus the shared
clock (`epoch_us`, `seq`). A performer renders only the config selected by its
cached table `group_id`.

- `params` are pattern-specific knobs for live tuning (e.g. sweep period in ms,
  wavelength ×100). The eight configs persist as the `patterns` NVS blob on the
  conductor so a power-cycle keeps every tuned look. **[done]** Group 1 is also
  mirrored to the legacy `pat`/`bri`/`p0`..`p3` keys for migration/repair.
- Patterns are `f(x, y, t)`:
  - `PULSE` — uniform breathing, all nodes in unison. **[done]**
  - `PALETTE_DRIFT` — smooth rainbow hue cycle; `params[0]` = cycle period in ms,
    `params[1]` = spatial hue offset (×100 cycles per x unit) so the rainbow can
    travel across the field or run in unison (0). **[done]**
  - `SWEEP` — traveling wave across `x`. **[done]** (1-D today.)
  - `SOLID` — every pixel full RGBW at `brightness`: the worst-case power draw, a
    bench rig for measuring the per-node LED ceiling (not a show pattern). **[done]**
  - `FIREFLY` — position-staggered meadow twinkle. **[done]**
  - `OCEAN_WAVE` — true 2-D summed wavefronts with tunable travel angle. **[done]**
  - `FIRE_FLICKER` — first ring-local pattern: deterministic billow plus coherent
    angular waves give every active LED distinct brightness and flame temperature.
    It remains clock/position-derived, so missed beacons do not freeze or desync
    the texture. **[done; hardware tuning pending]**
  - **[planned]** additional 2-D primitives such as a radial ripple from a center.

Every node hard-clamps rendered brightness to `MAX_BRIGHTNESS` (config.h, **192**),
so no pattern or broadcast config can exceed the tested 16-ring brightness
ceiling regardless of what is authored. It was set from the worst-case bench
measurement (solid white at 255 drew 0.76 A @ 5 V); the externally powered
32/64-emitter variants retain the same brightness clamp but are not covered by
that battery-budget measurement. See power management below.

Pure pattern math lives in `include/pattern_math.h` (host-unit-tested); the
LED-library binding is in `include/patterns.h`.

### 4.1 Lantern groups **[done; hardware verification pending]**

The field has eight fixed group slots, encoded as IDs 0–7. The control plane
stores an optional human-readable alias for each slot, so operators see labels
such as **Group 1 · Box lanterns** or **Group 3 · Bikes** everywhere groups are
selected. Aliases are control-plane metadata: changing one does not alter the
radio protocol, membership, or a performer's NVS. Each inventoried MAC belongs to exactly one group;
unpositioned rows can be grouped before layout. Membership is
independent of coordinates, survives clearing a position, and is cached in the
performer's NVS beside position. Each group has its own persisted
`PatternConfig`, so changing Group 3 does not disturb any other group.

All eight configs travel in the existing 4 Hz beacon (136 B total, under the
250 B ESP-NOW limit), rather than multiplying radio packets by the number of
groups. This preserves constant beacon cadence, radio duty-cycle behavior, and
free-running through packet loss. A performer reports its cached group in
REGISTER; if it missed a group-edit table broadcast, the conductor detects the
mismatch and sends the targeted row reply during that known-open radio window.

Protocol v9 migration is fail-preserving: existing layout rows become Group 1,
and the old field-wide pattern is copied into all eight slots. The upgrade alone
therefore causes no visible show change.

#### 4.1.1 Mixed LED counts **[done; hardware verification pending]**

Each physical board has a conductor-authoritative emitter profile of **16, 32,
or 64 RGBW LEDs**, independent of position and group. Existing v9 inventory and
performer NVS migrate to 16. The one firmware image allocates a 64-pixel
NeoPixelBus chain, renders only the configured active prefix, and explicitly
clears the remainder; a 16- or 32-pixel physical chain simply discards the later
wire bits. Ring-local math normalizes by the active count, so Fire Flicker uses
all configured emitters without changing beacon content or cadence.

The profile belongs to the board: moving or forgetting it preserves the count,
and replacing a positioned board does not copy the old count onto the spare.
Performers cache the value in NVS and report it in REGISTER; a mismatch triggers
the same targeted inventory-row repair used for IDs and groups. Protocol v10 adds
one `led_count` byte to REGISTER and `TableRow`. The persisted `TableEntry` blob
does not grow because the new value consumes v9's remaining padding byte.

The existing global `MAX_BRIGHTNESS=192` safety clamp still applies to every
profile. No additional per-node current cap is imposed for the externally
powered 32/64-emitter variants; their supply and wiring remain a hardware concern.
This profile records count, not physical topology: uniform field patterns work
on any one-wire arrangement, while Fire Flicker currently treats the active
sequence as circular. A future strip/grid topology field is only needed when an
effect must understand ends, rows, orientation, or custom local coordinates.

### 4.2 Show program (pattern scheduling) **[planned]**

Beyond a single live pattern, the conductor holds a **show program** in NVS — a
schedule of *what plays when* (e.g. pattern A for a while, then B; calmer/dimmer
late; brightness ramps). The conductor walks the program against its clock and
**broadcasts the current pattern each beacon**; nodes render whatever arrives and
stay dumb. So scheduling lives entirely on the conductor and needs no per-node
logic. Open considerations: smooth **transitions/crossfades** between patterns
(would need a blend factor in the pattern), and the schedule's **time base**
(uptime vs. dusk-relative once the LDR lands in Milestone 3, vs. a set wall-clock).

### 4.3 Power instrumentation — INA228 **[done — firmware/UI; awaiting chip verification]**

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
   plane reports average draw per metered performer and estimates battery SOC
   from the representative samples.
4. **Future field diagnostic (optional):** expose the current Wh reading over BLE
   for a phone-app spot-check, independent of the conductor link.

SOC is energy-counter based, not voltage-curve based: the KUNLUN model 1230 pack
default is **384 Wh**, and the control plane computes `100% - Wh used since full /
capacity`. Voltage is used only as a full-charge anchor: a sample at or above the
configured full-voltage threshold (default **14.4 V**) resets that node's SOC
anchor to 100%. Operators can also click **Sync to 100%** per metered node after
charging. This is a representative-sample tool for sizing Milestone 3's power
levers (§8.1, Lever 2 below), not a requirement to install INA228 on every node.

## 5. Node inventory and layout — conductor-authoritative **[done]**

The conductor holds `MAC → permanent ID + optional (x,y) + group + LED count` and broadcasts it.
Each node finds its own MAC, adopts its assignment, and **caches it in NVS**.
Board identity survives rearrangement because clearing or moving a position does
not remove its inventory row or change its number.

- **The conductor is authoritative and stores the table in its own NVS.** The
  field runs with **no laptop present** — the conductor is the coordination point
  for the table just as it is for the clock and the pattern config.
- Resilient: a node needs to hear the table only once, then survives on the cache.
- Cheap: 19 B/node on the wire (`TableRow`) gives 12 rows per `MSG_TABLE` packet.
  Steady-state rebroadcast is a slow backstop
  (`TABLE_INTERVAL_US`, 60 s — positions are static and cached in NVS); the
  moments that actually need the table travel out of band: `assign`/`group`/`leds`
  broadcast the full table immediately, and a REGISTER from a node that is **new
  to the roster, unprovisioned (id 0), reporting a conflicting ID, or reporting
  a stale group or LED count** gets an immediate **single-row reply** (28 B). The row reply
  is the delivery guarantee under radio duty-cycling: a
  REGISTER is the one moment the conductor provably knows that node's radio is
  on (TX is gated on radio-up). After radio delivery, the sender holds that
  listen window for at most another 750 ms and releases it as soon as its row
  arrives. This covers the primary→relay→child return queue instead of playing
  the ~13%-per-broadcast lottery; a missed reply is retried for free by the
  node's next REGISTER (10–12 s). Steady state
  (all nodes known + provisioned + profile-matched) costs zero table traffic beyond
  the backstop.

Implementation: the table logic is the dependency-free, host-tested
`include/table.h` (unique-ID adoption, optional placement, legacy migration); the wire side -
chunk math, receive-side length validation, own-row scan, and the row-reply
decision + builder (`tableRowReplyWanted`/`tableRowBuild`) — is the equally
pure, host-tested `include/table_wire.h`; `main.cpp` owns the NVS
blob, the radio calls, the reply queue (stash in the recv callback, drain in
`loop()` — same shape as the power-report queue), and the node-side adoption. The conductor edits it
over serial — `assign <mac> <x> <y>`, `group <mac> <1..8>`,
`leds <mac> <16|32|64>`, `table`,
`forget <mac>` — and pushes the change immediately.
REGISTER reports are reconciled in `loop()` so a factory number is persisted
without writing NVS inside the radio callback. A node stashes its row in the
recv callback and applies +
`identitySave()`s it from `loop()` (no flash write in the callback). Verified on
hardware: a node adopts a position set only on the conductor, with no serial to
that node, and keeps it across a reboot. Manual `pos x y` over serial remains as a
fallback/override for tests and stragglers.

### 5.1 Node replacement **[done]**

A dead lamp's spare has a new MAC and keeps its own printed number, so
replacement is a **table edit**, not a
re-calibration — because positions are MAC-keyed and the table is
conductor-authoritative. Physically drop the spare into the dead one's spot, then
transfer the position from the old MAC to the new one:

1. Spare boots as a performer (default), registers, and shows in the `roster`.
2. Operator runs `replace <oldMAC> <newMAC>` through the API/UI. The conductor
   atomically transfers position and group, then clears the old board's placement.
3. Conductor rebroadcasts; the spare caches its own ID plus `(x,y,group)` and
   joins the field.

The old board keeps its permanent number and group in inventory with no position;
the replacement receives the same group as part of the transfer. Numbers follow
boards, never locations.

No drone, no re-fly — a single swap is one command. Getting the new MAC: read it
from the spare's serial `info`, or label spares with their MAC, or let the
conductor surface "new unknown MAC seen." Only worth a full re-calibration if many
nodes move at once.

### 5.2 Control plane - operator/admin interface **[done; Pi rollout pending]**

The conductor stores the authoritative **routing table** (§5) *and* **show
program** (§4.2) in NVS and runs the field with **no laptop present**. The laptop
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

Operator mutations follow a **desired-state / eventual-convergence** contract.
The Pi waits only for the conductor to accept and persist the requested state;
it never waits for every performer to receive it. The conductor immediately
includes that state in its recurring beacons, while performer contact and health
appear later in periodic snapshots. Repeated beacons provide eventual convergence;
any future per-performer desired-state generation ACK is observability only, never
an operator-command gate. Full fleet snapshots are cached
across browser sessions and polled on a relaxed cadence because their UART cost
grows with fleet size; they are observability, not part of the command ACK path.

**Not a runtime dependency:** unplug the admin host and the conductor + field
continue on their stored table and program.

**Deployment:** the admin host is a **Raspberry Pi** cabled to the conductor over
USB. The reviewed remote shape makes the Pi a normal Starlink Wi-Fi client,
publishes only a loopback Uvicorn listener through a named Cloudflare Tunnel, and
protects HTTP and WebSocket traffic with one application session boundary. The
Pi can also run calibration CV (§6) on-site. It is a permanent convenience but
**stays non-essential to runtime** - the authoritative table and active field
configuration live in conductor NVS, so the field survives loss of the Pi,
Starlink, Cloudflare, or the browser.

```
browser --HTTPS/tunnel--> Pi (web UI + CV + serial bridge) --USB--> conductor --ESP-NOW--> field
```

**[done; hardware throughput proof pending] USB flashing station:** a separate
same-host provisioner daemon discovers FireBeetles automatically and runs a
bounded pool of checksum-pinned serial flashes
(five by default, configurable to ten). The control plane proxies a narrow API
over a private Unix socket and publishes progress on its existing WebSocket; the
daemon owns jobs, so browser refreshes and control-plane request lifetimes do not
own flash subprocesses. Job history and optional slot configuration persist locally.
Factory erase is a distinct 15-minute session, the configured conductor path is
excluded before probing, and firmware role verification refuses any conductor.
Permanent-ID reservation crosses a scoped credential to the control plane and
then the conductor's NVS inventory. Pi packaging uses a hardened systemd unit;
macOS uses a per-user LaunchAgent. A laptop station can point that reservation
call at the Pi's HTTPS authority while all flash bytes remain local to the USB
host. USB discovery also starts a read-only serial inspection while the station
is idle. The UI shows the board's permanent ID, role, firmware version, build,
wire protocol, production target, and a server-computed Current/Update needed
assessment before the operator authorizes any write. Unreadable or factory
boards remain Version unknown, and detected conductors are excluded from the
performer job queue.

An internet-connected deployment variant using Starlink Wi-Fi, a named
Cloudflare Tunnel, and application-authenticated browser sessions is specified
in [`REMOTE_ADMIN.md`](REMOTE_ADMIN.md). It preserves the same non-dependency
rule: loss of the internet-facing admin path does not affect conductor or
performer runtime.

**[done; Pi field proof pending] Pull-based deployment:** production releases
bind an immutable Git tag/full commit, separate control and firmware notes, and
one checksum-verified canonical field binary in a release manifest. A reviewed
channel file on `main` selects exactly one manifest by URL and SHA-256. Each Pi
polls that channel outbound, backs up state, deploys the detached commit, and
requires a local service health check or automatically restores the prior code,
deployment record, and untouched commit-specific Python environment. Release
state is root-owned outside the app-writable data directory, application and
release-tooling dependencies are transitively hash-locked, firmware build inputs
are exactly pinned, and a shared operation
lock defers Pi deployment during field OTA. The promoted control-plane release
stages its checksum-matched companion firmware, and the control plane reconciles
that desired image onto online performers by default. A persistent show-safety
toggle disables new automatic work and pauses an automatic transfer at its next
command boundary; manual recovery remains available. See
[`RELEASING.md`](RELEASING.md).

Before changing the checkout, the reconciler persists a root-only transaction
containing the prior commit, environment pointer, deployment record, and stable
runtime snapshots. An interrupted invocation recovers and health-checks that
state before reading desired state again. A recovery-only root oneshot is a
required predecessor of control at boot and restores the prior filesystem state
without creating a service-start dependency cycle. The no-op path likewise requires the
checkout, environment link, commit marker, staged firmware, and live health
response to agree.

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

- **[done]** Stable transport v11 common header
  `MsgHeader {magic, transport_version, type, origin, destination, hops}` on
  every packet.
  Receivers validate the logical addresses and one-hop limit before dispatching
  on `type`. A relay increments `hops` but preserves logical origin and
  destination, so normal and OTA traffic share one routing boundary.
  Types: `MSG_BEACON` (hot path), `MSG_REGISTER`, `MSG_TABLE` (live);
  `MSG_ROSTER`/`MSG_ACK` reserved. `MsgHeader.transport_version` is the stable
  `TRANSPORT_VERSION`, rejected on mismatch; it is intentionally independent of
  the application `PROTO_VERSION` reported by each node. This separation lets a
  v11 relay forward a future application generation's registration and OTA
  traffic without understanding or running that generation itself.
- **[done]** `MSG_BEACON` (clock + pattern) broadcast on a fixed channel to
  `FF:FF:FF:FF:FF:FF`, `WIFI_STA`. The hot path (sync.h) reads `epoch_us`+`seq`.
- **[done]** Bidirectional ESP-NOW: a performer learns one logical primary and a
  sticky physical parent from beacons, adds the parent as a peer, and unicasts
  `MSG_REGISTER {mac, id, group_id, led_count, role, fw, build, dirty, version}` every
  10–12 s; the conductor builds a MAC-keyed roster (`roster` serial command).
  A performer may change physical parent only after 12 seconds without a beacon,
  and only to another direct or one-hop path for the same primary. A relay learns
  only a hop-zero primary, which prevents relay chains by construction. The
  roster retains role, hop count, and immediate next hop for targeted replies.
  Shared radio sleep windows do not create a return-traffic herd: after a window
  opens, each expired performer selects a stable MAC-derived slot across 500 ms,
  holds the radio through delivery plus a bounded 750 ms table-repair return
  window (released early by its row), and uses 100 ms–2 s bounded backoff when
  the ESP-NOW unicast delivery callback fails. Performer
  unicasts are serialized by purpose so a power or OTA-status callback cannot be
  mistaken for REGISTER completion. `fw` is application
  compatibility (`PROTO_VERSION`); `version` + `build` + `dirty` are the OTA
  safety marker that catches same-protocol stale firmware.
- **[done]** `MSG_TABLE`: the conductor broadcasts inventory and layout in chunks
  (`TableRow` ×12/packet); nodes adopt their permanent ID, optional position,
  group, and 16/32/64 LED profile. `chunk`/`chunks` fields let a
  receiver tell how much it has seen.
- **[done]** `MSG_POWER`: an INA228-instrumented performer unicasts its
  hardware-accumulated energy/charge to the conductor (§4.3), reusing the
  REGISTER unicast path. Added without a PROTO_VERSION bump — no existing
  layout changed, and receivers ignore unknown types via the dispatch default.
- **[done]** `MSG_OTA_BEGIN`, `MSG_OTA_CHUNK`, `MSG_OTA_END`: conductor sends a
  staged firmware image during background OTA. A normal release transition
  broadcasts to the online field and writes the conductor locally. Once the
  conductor already has the exact immutable release, reconciliation freezes
  only the stale routed-v11 MACs and sends the same byte-stable messages to
  their logical destinations. Relays forward those packets; non-target nodes do
  not open a writer, receive firmware chunks, or reboot. Receivers accept the
  image only after size and CRC checks pass.
- **[done]** `MSG_OTA_STATUS`: performers report begin/writing/complete/error
  status with offset and prefix CRC, and the API filters stale statuses.
- **[done]** `MSG_OTA_QUERY` and `MSG_OTA_ACTIVATE`: the conductor requests
  deterministically time-slotted status replies and activates a fully staged
  performer. These message types were additive in v10; protocol v11 routes them
  without changing their OTA payload semantics.
- **Compatibility contract:** `MsgHeader`, `BeaconMsg`, `RegisterMsg`, and every
  `MSG_OTA_*` layout form the v11 migration plane and remain byte-stable across
  application protocol revisions. Application evolution is additive (new
  message types) or is coordinated while continuing to carry clock, routing,
  registration, and OTA over transport v11. Upgrade order is performers, relays,
  then primary after the field-wide staging barrier, so every intermediate fleet
  can still route and report OTA state.
- **[planned]** `MSG_ACK` + richer machine Pi↔conductor serial (lands with the Pi
  UI).
- Time base: 64-bit `esp_timer` microseconds throughout (no 32-bit `millis` wrap).

### 7.1 OTA policy, transfer, and recovery **[done; scale hardware verification pending]**

OTA automatically reconciles the companion firmware by default. A new release
remains a field update, but later drift is repaired as an exact stale-node
cohort when the primary already runs that immutable release. It runs in the
background: beacons and normal serial control commands continue between chunks,
so pattern, blackout, and power operations remain available. The system must never offer
arbitrary operator-selected firmware versions as a normal workflow: the control
plane derives the target set from trusted release identity and live state.
Mixed firmware can still happen after a failed update, but it is treated as an
automatic reconciliation/recovery state.

The foundation is in place: device builds get a release version from `VERSION`,
a git-derived 32-bit build id, and dirty flag via `scripts/firmware_build_id.py`;
performers report that identity in REGISTER; the conductor exposes
conductor/per-node firmware in machine state; the control plane shows field
firmware consistency in Operations, links build hashes to GitHub commits, and
flags `Firmware mismatch` in the Node List.

The transfer path stages a `.bin` artifact and streams it over machine serial.
The full-field path uses `ota_begin`/`ota_chunk`/`ota_end`: the conductor writes
its own OTA partition and broadcasts the same chunk stream. The selective path
uses `ota_begin_targets` with an exact MAC array. Firmware atomically validates
that every requested MAC is fresh in the roster, opens no conductor writer, and
routes begin/chunk/end only to that frozen cohort. The existing checkpoint,
repair, staged-CRC, relay-last, and activation machinery is shared by both
paths. The durable journal records the selective mode and original MAC set
before transfer begins; resume can remove targets proven installed but never
downgrades or expands that set into a full-field job. Install status exposes
`cohort_mode` as `selective` or `full-field`.
The UI shows broadcast, repair, staging, and activation phases per board.
This was hardware-verified on the 3-board bench on 2026-07-06, including a
same-protocol mixed-firmware recovery that restored performer #1 from
`0.3.0-mismatch` to `0.3.0`. The scale-hardened path now checkpoints every 256
chunks. Each performer reports its exact written offset and prefix CRC in a
deterministic status slot. Two or more valid laggards receive one shared suffix
rebroadcast from the earliest missing offset; a lone laggard receives only its
missing suffix by unicast. CRC divergence or a fatal flash error restarts and
replays only that performer. Normal chunks use six callback-confirmed broadcast
transmissions, while control packets and shared repairs use eight; the conductor
also pauses after each 4 KiB boundary so performer flash writes cannot consume
the next unique packet. Targeted repairs require one delivery-callback-confirmed
unicast; checkpoints and the final full-image CRC provide the durable proof.
Transient serial and radio failures retry with bounded backoff until success or
the durable six-hour retry deadline. Starting staging again after that deadline
opens a fresh window and reconciles the conductor plus each performer from its
verified offset/CRC. Duplicate chunks and finalization are idempotent.

Each performer finalizes independently and activates as soon as its own image is
full-size/full-CRC staged; there is no fleet-wide verification barrier. The
normal UI and automatic reconciler use one operation: upload, verify, activate
each ready performer, verify its re-registration, keep repairing laggards, then
activate a staged conductor last on a full-field run. Selective reconciliation
leaves the already-current conductor running. One performer's failure therefore
cannot block a different verified performer. With relays, verified leaf
performers still activate independently, but every targeted relay remains
online until all non-relay targets have activated; targeted relays then
activate, followed by the conductor only when that full-field run staged it. The
stage-only and explicit-activation API
routes remain recovery tools. The durable install journal and checksum-pinned
artifact survive browser disconnects and service restarts. The control plane
recognizes already-installed members of a partially migrated legacy cohort so
they cannot deadlock a resumed update. Offline inventory rows are
deferred and automatically catch up when they later check in. Operators can turn
automatic updates off before a show; doing so prevents new background work and
pauses an automatic transfer at its next safe command boundary.

`Update.end()` also selects the newly written ESP32 partition for the next
boot, so the conductor deliberately defers that call until its final explicit
activation. An incidental conductor reset during performer repair therefore
returns to the current conductor image instead of silently installing the
staged image early. Performers finalize on their own successful END because
their activation follows immediately and independently.

The first rollout has one explicit bootstrap seam: the USB-attached conductor
must be direct-flashed once because the previously deployed firmware cannot
serve the new repair/probe/activation serial RPCs. Control-plane preflight
detects that legacy command set before `ota_begin` and reports the required
action. After that, existing v10 performers can transition through OTA; unknown
additive message types remain safe during the mixed-version pass.

Selective reconciliation has a similar safe bootstrap rule without another
radio protocol bump. Routed v11 already supports logical-MAC-addressed OTA for
targeted repair, so v0.9.1 receivers and relays understand the targeted radio
packets. The primary-side `ota_begin_targets` command is additive, however: the
release introducing it uses the full-field path because the primary is not yet
on the desired immutable build. After that one rollout, a stale routed-v11
performer or relay is the only node written and activated. An outdated primary
or a manually uploaded artifact without immutable build identity deliberately
falls back to the full-field path. A pre-v11 node returning after its primary
has already migrated cannot understand routed packets at all, so reconciliation
fails closed with the one-time USB-station action instead of rewriting the
current field pointlessly.

The control plane derives a Recovery summary from live state and the durable last
install attempt. It classifies missing lanterns, same-protocol mixed firmware,
and failed OTA nodes into one operator action surface. Rerunning the same staged
artifact resumes or repairs the field; no maintenance-window reset is required.

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
- NVS caches (role, position, group, and all group pattern configs) survive power cycles / battery swaps.

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

The UI presents power as one three-state mode: **Sleep on schedule**, **Always
on**, or **Off**. Every transition writes `schedule_enabled`, `force_awake`, and
`force_sleep` together so stale overrides cannot overlap. Sleep field selects
Off, Wake field selects Always on, and enabling the sleep schedule selects the
scheduled mode; disabling the schedule also returns to Always on. Editing the
schedule times or check intervals does not change the active mode.
The old photodiode/dusk path remains off by default as a fallback/experiment; it is
not required for the main installation behavior.

### 8.3 One-hop relay coverage **[done; hardware verification pending]**

Protocol v11 separates logical packet identity from the immediate ESP-NOW radio
sender. Performers in overlapping primary/relay coverage keep one parent while
it remains fresh, preventing route flapping. If that parent disappears, they may
select another direct or one-hop path carrying the same primary identity after a
12-second timeout. They never adopt a different conductor identity without a
reboot or explicit reprovisioning.

Relays stay radio-on and ignore the performer sleep schedule. Forwarding is
queued outside the receive callback, bounded to one in-flight send and 16 queued
logical packets, and never forwards a packet already at hop one. Repeated OTA
copies collapse into a bounded repeat count. A forwarded beacon advances its
epoch by queue residence time so relay-zone clocks do not inherit a fixed relay
delay. If upstream disappears, the relay and its performers continue rendering
the last show state; the relay does not become a conductor.

The primary learns each online node's immediate next hop from REGISTER. Every
targeted OTA begin, chunk, end, repair, and activation uses that next hop while
retaining the performer as logical destination. After all queued copies of each
logical frame finish downstream, a current relay returns the additive
`MSG_OTA_FRAME_ACK` with a content/offset-derived token; the primary does not
advance to another target until the exact token arrives. Delayed receipts for a
previous chunk therefore cannot acknowledge the next chunk. This prevents a
large cohort from overflowing the 16-frame relay queue. Activation retains the
original v11 `MSG_ACK`, preventing a protocol migration from rebooting a relay
while a child's activation is still queued.
For a routed-v11 relay from before per-frame receipts, the primary detects the
different firmware identity and conservatively paces each logical frame by the
downstream callback budget; activation keeps using its original v11 receipt.
If that stale relay and stale children appear in the same reconciliation, the
control plane updates the directly reachable relay first and defers those
children to the next automatic pass, avoiding a multi-hour paced cohort. This
preserves the additive compatibility path without a protocol bump.
Selective checkpoint repair is per-node rather than replaying the cohort.
Offset, prefix CRC, and full-image CRC remain the durable staging proof, and
post-reboot REGISTER remains the final installed-image proof.

The v10-to-v11 rollout is a coordinated protocol migration, not the ordinary
same-protocol rolling activation. Because a v11 performer becomes invisible to
the still-v10 primary as soon as it reboots, the control plane requires every
reachable node to reach the staged barrier, dispatches all performer
activations through v10, then activates the primary. Only the post-reboot v11
firmware identities count as completion. Durable attempted/dispatched markers
make this sequence resumable across a control-service restart.

## 9. Milestone mapping

| Milestone | Status |
|---|---|
| 1 — sync proof (conductor + performers) | ✅ done, hardware-verified |
| 2 — NVS identity + position-aware sweep | ✅ done, hardware-verified |
| Refactor — symmetric runtime role + NVS pattern persistence + rainbow drift pattern | ✅ done, hardware-verified |
| Protocol foundation, Half 1 — typed header, MAC identity, bidirectional ESP-NOW, registration + roster | ✅ done, hardware-verified |
| Protocol foundation, Half 2 — MAC→ID+optional-position/group/LED-profile inventory broadcast + NVS cache (`assign`/`group`/`leds`/`table`/`forget`) | ✅ v7 position path hardware-verified; v8 ID, v9 group, and v10 LED-profile paths pending bench verification |
| Lantern groups — eight independent pattern slots + MAC group membership | ✅ code-complete + host/control tested; hardware verification pending |
| Mixed LED counts — per-board 16/32/64 active-emitter profile | ✅ code-complete + host/control tested; hardware verification pending |
| Control plane - structured machine Pi↔conductor serial (bulk table/show-program) | ✅ UI/API, authenticated remote boundary, and Pi packaging done; physical Pi/tunnel rollout pending |
| Auto-calibration — register / roster / blink + laptop CV | 📐 planned |
| 3 — power management (radio duty-cycle, schedule deep-sleep, optional LDR fallback, INA228 energy monitor) | 🛠 in progress — Lever 1 Stage A (performer radio duty-cycle) ✅ done + host-tested + hardware-verified + measured (85→~55 mA @ 12V); Stage B (CPU light-sleep between work, `napsched.h`) ✅ hardware-verified on bench 2026-07-03 (power re-measure owed); schedule-driven deep sleep ✅ code-complete + host-tested + UI/API built, hardware verification owed; photodiode dusk sensing is now optional/fallback; INA228 instrumentation (§4.3) ✅ firmware done + host-tested (`powermon.h`, `MSG_POWER`), awaiting the chip |
| 4 — battery power + ET900 draw measurement (go/no-go) | 📐 planned |
| 5 — OTA + enclosure | 🛠 OTA transfer/recovery done and 3-board bench-verified; enclosure/RF still planned |
| One-hop relay coverage | ✅ code-complete + host/control tested; field hardware verification pending |

## 10. Resolved & open decisions

Resolved:
- **Master table & show program: conductor-authoritative, stored in conductor
  NVS.** Field runs laptop-free; the laptop is a transient editor only (§5, §5.2).

Open:
- 2-D pattern parameter encoding (how to pack angle/center into `params[4]`).
- Pattern transitions/crossfades, and the show-program time base (uptime vs.
  dusk-relative vs. wall-clock) (§4.2).
- Admin UI form: local web UI (current lean) vs. CLI-first.
- Temporal-coded calibration as a later speed upgrade (§6).
