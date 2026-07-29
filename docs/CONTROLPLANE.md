# Control plane — features & phasing

Status: **prototype in progress (2026-07-05).** Companion to
[`ARCHITECTURE.md`](ARCHITECTURE.md) §5.2 (control plane) and §7 (wire
protocol). This doc enumerates *what* the control plane does, what the
current HTTP/API contract is, and how the serial adapter talks to the
conductor without changing the UI/API surface.

## Deployment shape

```
phone / laptop / agent --HTTP+WS--> API server (FastAPI) --USB serial--> conductor --ESP-NOW--> field
                                    (laptop in dev,
                                     Pi + its own AP in the field)
```

The Pi is a **deployment target, not a dependency** — dev runs the identical
server on a laptop with the bench conductor on USB. Pi-specific work is pure
config: AP hotspot, mDNS (`baskets.local`), systemd unit, serial device
naming.

## Architecture principles

1. **API-first.** The web UI is a pure client of the same HTTP + WebSocket
   API, with the OpenAPI schema served at `/docs` (FastAPI gives this for
   free). **Anything the UI can do, an agent can do over HTTP** — no
   UI-only paths, no server-rendered actions. This makes the field
   scriptable/agent-drivable from day one.
2. **The conductor stays authoritative.** Table, pattern, and (later) show
   program live in conductor NVS. The server holds only derived/history
   data (power logs, event log). Unplug the server and the field keeps
   running — same rule as today.
3. **Structured machine serial, human CLI preserved.** Newline-delimited
   JSON with request ids and explicit ok/error acks (`{"id":1,"cmd":...}` →
   `{"id":1,"ok":...}`). Machine lines are distinguishable by the leading
   `{`; the existing human CLI and diag lines coexist on the same port.
   Protocol logic lives in a dependency-free, host-tested header per
   project convention.
4. **Honest write semantics.** Every mutation surfaces its serial-protocol
   ack or failure. Conductor unplugged → the UI/API says so loudly, never
   pretends.
5. **Fully offline.** All assets local; nothing assumes internet. The real
   client is a phone on the Pi's AP on the playa.

## Current HTTP API contract

The implemented prototype is FastAPI + a pure static client. The UI uses only
these endpoints, so agents can drive the same workflows without a browser.

### State

- `GET /api/state` → full control-plane snapshot.
- `GET /api/lanterns` → `state.lanterns` only.
- `WS /ws` → pushes `{"type":"state","state":...}` snapshots and `error`
  events.

Snapshot shape:

```json
{
  "conductor": {
    "connected": true,
    "uptime_s": 12.3,
    "seq": 184221,
    "wake": true,
    "sync": "locked",
    "firmware": {
      "version": "0.3.0",
      "proto": 7,
      "build_id": 3225866068,
      "build_label": "c046bf54",
      "dirty": false
    }
  },
  "summary": {
    "alive": 8,
    "total": 9,
    "attention": 2,
    "table_rows": 9,
    "firmware": {
      "consistent": true,
      "matching": 8,
      "seen": 8,
      "expected": 9,
      "version": "0.3.0",
      "build_label": "c046bf54",
      "dirty": false
    }
  },
  "pattern": {
    "pattern": "Glow",
    "brightness": 48,
    "params": {"hue": 40, "saturation": 100}
  },
  "power": {
    "light_sleep_check_s": 4,
    "deep_sleep_check_min": 15,
    "led_on_start_min": 1200,
    "led_on_end_min": 360,
    "current_min": 720,
    "current_epoch_s": 0,
    "schedule_enabled": false,
    "force_awake": true,
    "force_sleep": false,
    "leds_on": true
  },
  "lanterns": [
    {
      "mac": "8C:94:DF:8F:71:50",
      "label": "#0",
      "status": "alive",
      "last_seen_s": 4,
      "last_seen_label": "4s ago",
      "x": 0.54,
      "y": 0.47,
      "position": "Set",
      "attention": "None",
      "firmware": {"version": "0.3.0", "proto": 7, "build_id": 3225866068, "build_label": "c046bf54", "dirty": false},
      "power": {"wh": 0.38, "avg_w": 0.71, "last_report_label": "4s ago"},
      "updated_at": 1720123456.0
    }
  ],
  "power_monitor": {
    "battery_capacity_wh": 384.0,
    "full_voltage": 14.4,
    "sample_count": 2,
    "usable_sample_count": 2,
    "estimated_node_soc_percent": 99.7,
    "estimated_field_avg_w": 6.6,
    "samples": []
  },
  "events": [{"ts": 1720123456.0, "message": "mock conductor started"}]
}
```

Lantern status values currently used by the prototype:

- `alive` — awake and usable.
- `missing` — expected/positioned but stale or not seen.
- `retired` — old MAC after a replacement; keep for audit, never offer as
  a replacement spare.

Lantern attention can also be `Firmware mismatch` when a registered node has
the same wire protocol but a different release version, build id, or dirty flag
than the conductor.
`summary.firmware` is the OTA safety invariant: manual field-wide OTA should not
start unless every reachable node is on the expected build, or the UI explicitly
keeps the operator in a recovery flow.
`recovery` is the operator-facing classification derived from state and the last
firmware install attempt. It reports one of `ready`, `missing_nodes`,
`mixed_firmware`, or `ota_failed`, plus the affected lanterns and the next
action to take.

`summary.alive / summary.total` means healthy placed lanterns over placed
lanterns in the field table. Unpositioned live lanterns are available substrate
or spares: they have `x: null`, `y: null`, and `position: "Missing"`, appear in
the tray and Node List, and do not count in the field denominator until placed.
The map renders only positioned lanterns.

### Mutations

- `POST /api/lanterns/{mac}/identify`
  -> make one physical lantern visibly identify itself; the UI labels this
  action **Locate** in the selected-lantern sheet and Node List rows.
- `POST /api/lanterns/{mac}/assign` with `{"x":0.25,"y":0.75}`
- `POST /api/lanterns/{mac}/forget`
- `POST /api/lanterns/replace` with `{"old_mac":"...","new_mac":"..."}`
- `GET /api/patterns` -> saved pattern configs.
- `POST /api/patterns` with
  `{"name":"Amber Glow","pattern":"Glow","brightness":48,"params":{"hue":40,"saturation":100}}`
  -> create a saved pattern config.
- `GET /api/patterns/{id}` -> one saved pattern config.
- `PUT /api/patterns/{id}` with the same body as create -> replace a saved
  pattern config.
- `DELETE /api/patterns/{id}` -> delete a saved pattern config.
- `GET /api/patterns/{id}/preview?t=0` -> PNG preview of that saved pattern
  config on the current positioned lantern layout.
- `GET /api/patterns/{id}/preview.json?t=0` -> per-lantern RGBW/RGB/luma
  samples and aggregate metrics for that saved pattern config.
- `GET /api/patterns/{id}/preview/frames.json?duration_ms=8000&fps=4` ->
  frame sequence for motion/rhythm review, with per-frame samples and aggregate
  sequence metrics.
- `GET /api/patterns/{id}/review?duration_ms=8000&fps=4` -> automated review
  of the saved pattern config: pass/reject, score, issues, recommendations, and
  sequence metrics.
- `POST /api/patterns/{id}/broadcast` -> broadcast that saved pattern config
  through the existing conductor pattern command and push a state update.
- `POST /api/show/pattern` with
  `{"pattern":"Sweep","brightness":64,"params":{"period":8000,"spatial":300}}`
- `POST /api/show/blackout`
- `POST /api/operations/power-policy` with the runtime sleep/check policy.
- `POST /api/operations/power-monitor` with
  `{"battery_capacity_wh":384.0,"full_voltage":14.4}` -> update the SOC
  estimate capacity and full-charge voltage anchor.
- `POST /api/lanterns/{mac}/power-sync-full` -> manually anchor that metered
  node's current Wh reading as 100% SOC.
- `POST /api/operations/ota-mode` with `{"enabled":true}` or
  `{"enabled":false}`.
- `GET /api/operations/ota-artifact` -> current staged firmware metadata.
- `PUT /api/operations/ota-artifact?filename=firmware.bin` with raw
  `application/octet-stream` firmware bytes -> stage a `.bin` artifact.
- `GET /api/operations/ota-install` -> current/last install progress.
- `POST /api/operations/ota-install` -> stream the staged artifact to the
  conductor and field during an OTA maintenance window.
- `POST /api/operations/calibration-mode` with `{"enabled":true}` or
  `{"enabled":false}` -> start/stop the live LED calibration blink sequence.
  This is only light control: it does not upload media or write layout
  coordinates. Starting stores the previous pattern, switches the field to the
  firmware `Calibration` pattern, and returns the generated Hamming-spaced
  code plan. Stopping restores the previous pattern when available.
- `GET /api/calibration/frames` -> uploaded calibration image frames.
- `PUT /api/calibration/frames?filename=anchor.png` with raw image bytes
  (`.jpg`, `.jpeg`, `.png`, or `.webp`) -> store one calibration frame.
- `POST /api/calibration/frames/{frame_id}/detect` with
  `{"threshold":180,"min_area":4}` -> run first-pass bright-blob detection on
  that frame and return normalized candidate `(x,y)` points plus pixel-space
  centroids/bounds. This is the image-sequence/CV scaffold; it does not push a
  layout table yet.
- `POST /api/calibration/decode` with
  `{"frame_ids":["..."],"threshold":180,"min_area":4,"max_distance":0.035}` ->
  decode an ordered image sequence by matching bright blobs across frames and
  turning per-frame presence into a bit string/value per tracked blob. This is
  the first-pass path for multi-photo identity coding.
- `POST /api/calibration/code-plan` with
  `{"first_code":1,"bit_count":null,"min_hamming_distance":3}` -> generate the
  blink-code plan for the current alive roster (or explicit `roster_macs`). The
  default synthetic/phone-video path uses codes separated by Hamming distance 3
  so a missed bit should show up as missing/extra rather than silently assigning
  the wrong MAC.
- `POST /api/calibration/propose-layout` with
  `{"frame_ids":["..."],"code_map":[{"mac":"...","code":1,"bits":"001"}],"threshold":180,"min_area":4,"max_distance":0.035}` ->
  decode the ordered image sequence, map decoded values through either an
  explicit `code_map` or sequential `roster_macs`/`first_code`, and return
  proposed `(mac,x,y)` assignments plus missing, ambiguous, and extra detections.
  Browser-extracted video frames use code-aware temporal contrast against the
  planned blink codes, which suppresses constant glare/cables/non-node lights
  before falling back to the static bright-blob tracker if temporal scoring
  cannot fully resolve the plan. If both `code_map` and `roster_macs` are
  omitted, the API uses the current alive lantern roster with sequential values.
  This endpoint does not write the conductor layout table.
- `POST /api/calibration/apply-proposal` with
  `{"assignments":[{"mac":"...","x":0.25,"y":0.75,"code":1,"bits":"001"}],"missing":[],"ambiguous":[]}` ->
  save reviewed confident proposal assignments through the normal lantern
  assignment path. Missing and ambiguous rows are skipped and returned in the
  response summary; failed assignment writes are reported per MAC.
- `POST /api/calibration/simulate` with
  `{"width":960,"height":720,"first_code":1,"bit_count":null,"blob_radius":5,"jitter_px":0,"glare_count":0,"missing_frames":[],"perspective":0}` ->
  render an ordered synthetic calibration image sequence from the current
  positioned alive roster (or explicit `nodes` with `mac/x/y`), upload those
  frames into the same calibration store, and run the normal proposal path with
  the generated code map. This is the synthetic proof harness for the
  multi-photo identity workflow. The Operations UI labels this workflow
  **Lantern Locations**. The normal path is intentionally two separate actions:
  play/stop the lantern locator pattern to make the lights blink,
  then select a local phone video and click **Analyze video** to extract still
  frames in the browser and upload them through the same image frame API. The
  UI shows the proposal over the captured frame and keeps ignored extras as a
  compact count, not a raw track list. No server-side video codec is required.
  The proposal step is code-map-aware:
  video-sized frames get a scaled blob-area floor, all cyclic bit alignments are
  scored so capture does not have to begin on the first locator slot, tracks
  matching planned lantern codes become candidate assignments, and other bright
  lights remain extra tracks instead of being assigned as lanterns.
- `GET /preview?pattern=Glow&brightness=48&hue=40&t=0` -> PNG preview of the
  simulated field using the current positioned lantern layout. Also accepts
  `params` as a JSON object query string and direct aliases (`hue`,
  `saturation`, `period`, `spatial`, `wavelength`) for agent-friendly calls.
- `GET /preview.json?pattern=Glow&brightness=48&hue=40&t=0` -> machine-readable
  preview data for agent scoring: positioned lantern samples plus `count`,
  `lit_count`, luma range, average luma, and contrast metrics.
- `GET /preview/frames.json?pattern=Sweep&duration_ms=8000&fps=4` -> frame
  sequence for a draft pattern without saving it first.
- `GET /review?pattern=Sweep&duration_ms=8000&fps=4` -> automated review for a
  draft pattern without saving it first.

Every mutation returns an ack:

```json
{"ok": true, "message": "assigned #2"}
```

Adapter errors such as serial timeout surface as HTTP `503`. Command-level
errors such as unknown MAC or invalid replacement return `404` with the
adapter's `error` text. Rejected pattern changes return `400`; the UI should
only treat a pattern change as saved after a successful ack.
Pattern library CRUD is server-side control-plane state, persisted under
`.control_patterns/patterns.json`; broadcasting a saved pattern is still a
separate `POST /api/show/pattern` operation.

## Adapter contract

FastAPI depends on `control.adapters.ConductorAdapter`, not on the mock
implementation directly. The app defaults to `MockConductor`; set
`CONTROL_CONDUCTOR=serial` and `CONTROL_SERIAL_PORT=/dev/cu.usbserial-XXXX`
to use the pyserial-backed conductor. The required methods are:

```python
snapshot() -> dict
lanterns() -> list[dict]
tick() -> None
identify(mac) -> ack
assign(mac, x, y) -> ack
forget(mac) -> ack
replace(old_mac, new_mac) -> ack
update_pattern(pattern, brightness, params) -> ack
blackout() -> ack
update_power_policy(policy) -> ack
set_ota_mode(enabled) -> ack
ota_begin(size, crc32) -> ack
ota_chunk(offset, data) -> ack
ota_end() -> ack
```

`MockConductor` implements this contract for UI development.
`JsonLineSerialConductor` implements the same contract over newline-delimited
JSON and is tested with a fake transport. The serial transport deasserts
DTR/RTS by default after opening so a running conductor is not intentionally
reset just because the web server connected. FastAPI serial calls run through
a serialized `asyncio.to_thread` helper so a blocking serial read does not pin
the event loop.

### Machine serial protocol

Requests are one compact JSON object per line:

```json
{"id":1,"cmd":"state"}
{"id":2,"cmd":"assign","mac":"8C:94:DF:57:7F:14","x":0.25,"y":0.75}
{"id":3,"cmd":"forget","mac":"8C:94:DF:57:7F:14"}
{"id":4,"cmd":"replace","old_mac":"A0:B7:65:11:44:91","new_mac":"8C:94:DF:57:7F:14"}
{"id":5,"cmd":"pattern","pattern":"Sweep","brightness":64,"params":{"period":8000,"spatial":300}}
{"id":6,"cmd":"blackout"}
```

Responses echo the request id:

```json
{"id":1,"ok":true,"state":{...}}
{"id":2,"ok":true,"message":"assigned #2"}
{"id":3,"ok":false,"error":"unknown lantern"}
```

The adapter ignores human CLI/diagnostic lines and malformed JSON until it
sees a valid JSON response with the matching `id`. If no matching response
arrives before the timeout, the adapter raises `SerialProtocolError`, which
the API exposes as HTTP `503`.

Firmware support lives beside the human CLI: lines beginning with `{` enter
the machine parser in `include/serial_json.h`, while `info`, `table`, `assign`,
and the other text commands keep working. The firmware `state` response derives
lanterns from the conductor's persistent layout table plus the live roster:
positioned table rows show on the map, roster-only MACs show as "Needs
position", and table rows not currently registered show as "Not seen".

## Features

### 1. Field dashboard (read-only)

- Roster view: MAC, friendly id, last-seen age; stale (silent > ~30 s) and
  expected-but-never-seen nodes flagged.
- Count check at a glance: "58 of 60 healthy".
- Conductor status: beacon seq, current pattern, table row count, `wake`
  flag, uptime.
- Cross-checks: in roster but not table (unpositioned); in table but not
  roster (dead lantern?).

### 2. Layout map (the reason a web UI exists)

- 2-D field map of table `(x,y)` positions with roster liveness overlaid.
- Drag to reposition; click to add/edit; `forget` to remove.
- Replace-node flow (§5.1): pick dead node + spare → one action does
  `assign` + `forget`.
- **Identify:** click a dot → that physical lantern blinks so it can be
  found in a dark field of 60. ⚠ Needs a small new ESP-NOW unicast message
  (no PROTO_VERSION bump); the one Phase-1 firmware addition beyond the
  machine serial protocol.
- Table import/export as JSON — backup before edits; the socket the
  calibration CV output (§6) plugs into later.

### 3. Live show control

- Pattern picker (PULSE / PALETTE_DRIFT / SWEEP / GLOW; SOLID behind a
  bench-only flag).
- Brightness slider + per-pattern param controls with human labels:
  Pulse/Glow expose hue, Sweep exposes period + wavelength, and Palette Drift
  exposes period + spatial spread. The Change Pattern button is disabled until
  the visible draft differs from the live conductor state.
- Pattern preview: the browser renders `f(x,y,t)` live on the map *before*
  broadcasting (JS port of the pure `pattern_math.h`). Cheap because the
  math is pure and host-tested; turns knob-tuning into instant feedback.
  Also the backbone of the agent-assisted pattern-authoring workflow (see
  the "Pattern authoring" section), including its headless
  `GET /preview` PNG render endpoint. **[done for PNG still frames]**
- Blackout button (bri 0).

### 4. Power & energy

- Live power panel: the conductor's `[power]` lines parsed into structured
  V / I / avg-W / Wh-tonight per instrumented node.
- Operations power schedule: set radio/light-sleep check interval, deep-sleep
  check interval, and LED-on window. The Field power controls provide one-click
  **Sleep field**, **Wake field**, and **Follow schedule** overrides without
  rewriting the saved schedule. These are runtime-broadcast in beacons, so
  changing them does not require another firmware update.
- Nightly history persisted server-side (sqlite or CSV) — trend across the
  event, not just tonight.
- `power reset` button for the dusk ritual.
- Later (protocol v3 bundle): VBAT in REGISTER → fleet-wide battery health
  on the dashboard.

### 5. Show program (firmware doesn't exist yet — see §4.1)

- Schedule editor: what plays when; brightness ramps ("calmer/dimmer after
  1 am").
- Forces the open §4.1 decisions: time base (dusk-relative vs wall-clock)
  and crossfades.
- Simulate the schedule against the map preview.

### 6. Calibration wizard (§6)

- Enter-calibration mode, capture orchestration, CV upload/review,
  empty-slot report, push table. Integrates via the map's import/export
  socket; CV itself runs on the Pi/laptop.

### 7. Ops & escape hatches

- Field firmware panel: conductor release version, GitHub-linked commit hash,
  dirty flag, and consistency count (`N / total on this build`). Mixed firmware
  is red and also appears in the Node List as `Firmware mismatch`.
- Power schedule panel: authoritative place for LED on/off hours and
  force-awake debugging. Schedule-driven sleep is the primary field behavior;
  photodiode dusk sensing is optional/fallback, not required for the main path.
- Power monitoring panel: sparse INA228 reference nodes report Wh/avg-W/voltage
  through `MSG_POWER`; Operations estimates whole-field draw and per-node SOC
  from the configured battery capacity. Voltage is used only to detect/full-anchor
  a charged pack, and each metered node has a manual **Sync to 100%** action.
- Raw serial console passthrough to the conductor CLI (the playa will
  produce a situation the UI didn't anticipate).
- Event log: registrations, table edits, errors — timestamped, server-side.
- Conductor config backup/restore (table + pattern + program as one JSON
  blob).
- Server self-health: serial connect/reconnect state, auto-reconnect on
  USB flap.

### 8. OTA (Milestone 5)

- Manual maintenance-mode OTA only. No autonomous/opportunistic updates.
- Field-wide firmware updates only. No selected-node OTA; mixed firmware is a
  recovery state, not a supported operating mode.
- Built foundation: REGISTER reports release version + protocol + build id +
  dirty flag, the control-plane state exposes per-node and conductor firmware
  versions, and Operations shows version consistency with linked commits.
- Built updater: Operations can enter/exit a 15-minute
  maintenance window over the same machine serial protocol; state reports
  `ota.mode`, readiness count, expected count, missing placed lanterns,
  firmware consistency, blockers, and timeout.
- Firmware artifacts are staged through the API/UI, persisted under
  `.control_ota/`, validated for `.bin` extension, size, CRC32, and sha256, and
  chunked at 128 bytes for the serial/ESP-NOW path.
- Install flow: the API sends `ota_begin`, then every `ota_chunk`, then
  `ota_end` over USB serial to the conductor. The conductor writes its own OTA
  partition and broadcasts the same begin/chunk/end packets over ESP-NOW. Each
  performer writes the image into its own OTA partition and reboots after a
  successful size/CRC/end check. The UI polls install progress and shows chunk
  counts, elapsed time, transfer rate, and ETA while the long request streams.
  The conductor's `ota_progress` response includes its own write offset plus
  performer status rows; the API samples it every 64 chunks so the UI can show
  node progress during the transfer, and any performer-reported failure aborts
  before `ota_end`. Serial chunk timeouts and retryable conductor chunk NACKs
  are retried; duplicate already-written chunks are idempotent on both conductor
  and performers. Firmware rejects any decoded chunk whose length does not
  exactly match the expected full/tail chunk length at the current offset, so a
  truncated serial command cannot advance the flash writer to a non-chunk
  boundary. The API also polls conductor `ota_progress` to recover after
  retryable chunk timeouts/NACKs, but rejects unsafe mid-chunk resume offsets.
  A timeout from a periodic progress poll is recorded as `last_progress_error`
  and does not abort the stream; the next successful poll or post-reboot
  verification is authoritative.
  Pyserial writes use `write_timeout=2.0` and do not call unbounded `flush()`
  after every line.
- Hardware-verified 2026-07-06 on the 3-board bench: staged `firmware.bin`
  (`860944` bytes, `6727` chunks, sha256
  `906fc37a03fa2c1afe97c1a35ba4f8153e295df0de5672232312d2fb7e9c1568`),
  entered maintenance with `2 / 2 ready`, streamed all chunks, received
  `ota install complete; rebooting`, and post-reboot `/api/state` showed
  `summary.alive=2`, `summary.total=2`, `attention=0`, and
  `summary.firmware.consistent=true`. `install.nodes` reported both performers
  at terminal `complete` status with `offset=860944` and `crc32=3411679313`;
  stale OTA status is ignored.
- Clean-build drill 2026-07-06: staged `firmware-devkitc-ba705b4.bin`
  (`860928` bytes, `6726` chunks, sha256
  `106cfda591acc64ece0c9c1272d4570e7b0b8e418520ac3faedc22e26f05dbee`) against
  the same 3-board bench. The transfer completed in about 400 s, both
  performers reported terminal `complete` status at `offset=860928` and
  `crc32=84077347`, and post-reboot state showed conductor + both performers on
  `0.3.0` build `ba705b46`, `dirty=false`, with recovery `ready`.
- Status-polling firmware drill 2026-07-06: staged
  `firmware-devkitc-a11fffe.bin` (`860944` bytes, `6727` chunks, sha256
  `c621ea7eeb366bdad0204a4c3a787b7bf453f82bd457553a60d25e55fe182da2`).
  The API recovered serial chunk timeouts during the stream, completed in about
  467 s, and post-reboot state showed conductor + both performers on `0.3.0`
  build `a11fffec`, `dirty=false`, recovery `ready`. Final install status
  reported both performers `complete` at `offset=860944`, `crc32=723916971`.
- Negative safety drill 2026-07-06: direct-flashed performer #2 to same-protocol
  build `9821db52`. `/api/state` reported `summary.firmware.consistent=false`,
  `attention=1`, `recovery.status=mixed_firmware`, and the mismatched MAC in
  `recovery.mismatched`. Reinstalling the clean staged `a11fffec` image restored
  both performers. That restore exposed a final serial `ota_end` ack timeout
  after all chunks had landed and the field had actually rebooted cleanly; the
  API now treats that exact shape as post-reboot verification instead of a hard
  failure, and still fails if all expected performers do not verify.
- Follow-up restore 2026-07-06: the clean staged `a11fffec` image restored the
  mixed field again after a periodic `ota_progress` serial timeout during the
  stream. The API kept transferring, returned `ota install complete; rebooting`,
  and post-reboot `/api/state` showed both performers on `a11fffec` with
  `summary.firmware.consistent=true`.
- Recovery flow: `/api/state.recovery` classifies missing placed lanterns, mixed
  firmware, and failed OTA nodes into one Operations card. A failed install from
  `/api/operations/ota-install` also drives that card so the operator sees the
  reset-and-rerun path immediately. Same-protocol mixed firmware is allowed to
  enter maintenance install as its recovery action once all placed nodes are
  present. A 2026-07-06 recovery dry-run found that old performer firmware
  aborted on repeated already-written OTA chunks; the fixed receiver now treats
  those duplicates as no-ops and OTA maintenance keeps performer radios awake.
  A subsequent fixed-receiver recovery rerun exposed the unsafe partial-write
  case above and a false-success API path; the API now requires all expected
  placed performers to report complete or verify from post-reboot field firmware
  consistency before returning success. The latest mixed-firmware recovery run
  intentionally restored performer #1 from `0.3.0-mismatch` to `0.3.0`.
  Missing placed lanterns now block install while listing the missing lantern in
  Recovery, and post-reboot verification failures synthesize per-node failed OTA
  rows for any expected performer that did not verify. Remaining reliability work
  before trusting a 60-node deployment: decide whether field scale needs explicit
  performer ACK/retry beyond the current status reporting.

### Cross-cutting

- **Phone-first, dark theme** — night use with dark-adapted eyes; big
  touch targets; possibly a red-shifted night mode. (Visual design session
  pending — see Open items.)
- Fully offline (principle 5).
- Honest write semantics (principle 4).

## Phasing

| Phase | Scope | Firmware work |
|---|---|---|
| **1** | Machine serial protocol; API server + OpenAPI; dashboard, layout map, live show control, power panel, ops console | Serial protocol header + glue; **identify-node** message |
| **2** | Show program: conductor schedule in NVS + editor UI | Show-program feature (§4.1 decisions land here) |
| **3** | Calibration wizard | Calibration mode messages (§6) |
| **M5** | OTA UI | Manual maintenance-mode OTA over serial + ESP-NOW |

Everything in Phase 1 is testable end-to-end on the 3 bench DevKitC boards —
no Pi, no new parts.

## Wire-protocol implications (enumerated up front, none urgent)

- **Phase 1:** `MSG_IDENTIFY` unicast (new type only — no PROTO_VERSION
  bump, same precedent as `MSG_POWER`).
- **Protocol v3 bundle (later, one deliberate bump):** VBAT + sync-health
  fields in REGISTER, riding the same bump that clears the known dead-field
  debt (`palette_id`, `MSG_ROSTER`/`MSG_ACK`, `TableMsg.chunk/chunks`).
  Nothing else in this doc touches the radio.

## Pattern authoring — an agent-assisted workflow, not a UI feature

Agent workflow guide: [`skills/CREATING_PATTERNS.md`](../skills/CREATING_PATTERNS.md).

Pattern authoring should optimize for this operator goal:

> Design patterns in the Pi/brain, distribute them over the control plane, and keep
> performer nodes as simple and low-power as possible.

The performer should not be the creative computer. The Pi/server can spend CPU on
pattern design, layout-aware compilation, previews, and agent-assisted iteration.
The performer should spend as little CPU as practical: maintain synced time, replay
compact local instructions, interpolate colors, and drive the LEDs.

There are three pattern-delivery tiers:

1. **Built-in patterns (current firmware):** conductor broadcasts `pattern_id` +
   params; performers evaluate known C++ patterns locally. Tiny, live-tunable,
   resilient, and still the right path for simple primitives like GLOW/SWEEP.
2. **Precomputed per-node clips (target creative layer):** the Pi/brain knows
   `MAC -> (x,y)`, evaluates a desired `f(x,y,t)` offline, then sends each node a
   compact replayable artifact: start epoch, loop duration, keyframes/curve samples,
   interpolation mode, brightness cap, and hash/version. A clip may target the
   lantern as one virtual light **or** multiple emitters inside the lantern.
   Performers do not need to understand the source pattern math; they just replay
   their own clip against the synced clock. This preserves no-live-streaming
   resilience while minimizing node "thinking" and CPU time.

   Do not bake "16-LED ring" into the control-plane model. The current hardware is a
   16-pixel ring, but future nodes may splay those pixels into a different 2-D or
   3-D shape. Model each physical node as a container with an optional **emitter
   layout**: emitter count + local coordinates/orientation. Phase 1 can treat every
   node as one virtual emitter for simplicity, as long as the clip/protocol can later
   expand from `node -> 1 color` to `node -> N emitter colors` without redesigning
   the authoring model.
3. **Firmware OTA:** used only when the replay engine, protocol, or built-in
   primitives need to change. A new artistic look should not require reflashing if
   it can compile down to a replayable clip.

Important boundary: avoid live frame streaming. Once a clip is loaded and acked,
the field should keep running if the Pi disappears, and performers should free-run
through dropped beacons just like they do today.

Open engineering questions for the precomputed-clip layer:

- clip representation: color keyframes, palette-index keyframes, curves, or a mix
- spatial granularity: one color per node, per-emitter frames, or compressed local
  primitives that preserve future `1 -> N emitters` expansion without bloating clip
  size
- emitter layout schema: current default is one virtual emitter per node; later,
  support local emitter coordinates for the 16-pixel ring or a future 2-D/3-D LED
  arrangement
- sample rate and loop length that fit NVS/flash and radio transfer time
- chunking/acks/resume for distributing 60 unique clips
- whether clips live on performers, conductor, or both
- preview conformance: the browser preview and performer clip player need shared
  golden vectors

Until that layer exists, new patterns are still compiled firmware
(`pattern_math.h` stays the single home of `f(x,y,t)`), but the control plane is
what makes authoring fast and lets an agent do most of it:

1. **Author:** write the new `f(x,y,t)` in `pattern_math.h` (pure,
   dependency-free — an agent can iterate here freely) + host tests.
2. **Visualize before any flash:** the browser preview (§3) renders the
   proposed pattern on the *real* field layout. An agent drives this via
   browser control, or — cheaper — via a **headless preview endpoint**
   (`GET /preview?pattern=…&params=…&t=…` → PNG frame of the simulated
   field), plus `preview.json`, `preview/frames.json`, and `review` for
   machine-readable scoring. This makes the loop API-only: draft → still
   preview → frame-sequence metrics → automated review → save → broadcast.
   GIF loops remain a later extension; JSON frame sequences are implemented.
3. **Bench:** flash the 3 bench boards, tune params live through the show
   controls.
4. **Fleet:** pre-event reflash or manual maintenance-mode OTA.

**Keeping C++ and the JS preview in sync:** the preview is a second
implementation of the pattern math, which will drift. Plan: a shared
**golden-vector conformance file** — the host tests emit expected RGBW for
a grid of `(pattern, params, x, y, t)` samples; the JS preview's test suite
consumes the same file. (If drift still bites, `pattern_math.h` is
dependency-free and could compile to WASM for a single-source preview —
deferred until needed.)

**Reality check:** until the precomputed-clip layer exists, a new built-in
pattern still means a firmware rollout — now possible via manual maintenance
OTA, but still heavier than a normal show edit. The pattern *vocabulary* wants
to be locked pre-event, while the **pattern space (pattern × brightness ×
params) is the live authoring surface** on the playa. That's the division of
labor: agents + preview expand the vocabulary before the event; the UI tunes
patterns during it.

## Deliberately out of scope

- Auth beyond the Pi AP's WPA2 password.
- Multi-conductor support.
- On-device pattern interpretation (a bytecode/expression VM for patterns
  on the ESP32) — patterns stay compiled C++; the authoring workflow above
  covers the need without the complexity.

## Open items

- Visual design session (aesthetic, layout, dark/night theming) — next.
- Serial protocol schema — designed against this feature list, after the
  design session.
- Stack confirmation: Python + FastAPI + pyserial (recommended; pyserial
  already proven in this repo), UI framework TBD in the design session.
