# Build Handoff — start here

The "you are here, do this next" doc for a session picking up the build cold.
Design rationale lives in [`ARCHITECTURE.md`](ARCHITECTURE.md); this is state +
next steps only.

**Read order:** this doc → `ARCHITECTURE.md` → `README.md` →
[`FLASHING.md`](FLASHING.md) → [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md).

**Repo:** https://github.com/moda-labs/lightweave · `pio test -e native`
(**231 pass**) and the full control suite are green; all three device envs
(`devkitc` / `firebeetle` / canonical `field`) build clean.

Latest in `feat/radial-ripple` (2026-08-30): `POND_RIPPLE` adds concentric,
center-selectable outward waves
without changing the stable v11 transport layout; the control plane blocks its
broadcast until every placed lantern is online on the exact current firmware.
`OCEAN_WAVE` now retains a 22% perceptual deep-water floor plus a one-PWM
quantization guard, so a non-black Ocean config never drops fully dark between
crests at low nonzero brightness. `UPLOADED` preserves every existing pattern
option and adds a versioned, loop-free 192-byte expression VM. The control plane
preflights exact full-fleet firmware, stages into a slot that cannot overwrite any
of eight active group programs, waits for exact acknowledgements from every placed
node, and only then activates the full 64-bit BLAKE2s program identity. Every acknowledgement includes the
reporter's exact firmware identity and must be newer than its latest registration,
so a rollback or reboot cannot reuse stale readiness. A mixed rollout has no
uploaded target, sends no program traffic, and cannot broadcast the new ID;
built-in rendering and the stable v11 beacon remain unchanged. If firmware becomes
mixed after activation, the conductor reverts every new-only group to compiled
Glow; zero brightness remains available as a safe off command. Status responses
are deterministically spread across 500 ms and use bounded retry backoff inside
a two-second response window, and the control plane uses a compact progress poll.
Repair
for a staged but inactive target ends after a 60-second verification window;
active targets are repaired only when a node registers. Firmware and preview
implementations share host/control regression coverage. The interpreter enforces
a weighted execution budget in addition to its instruction and stack limits.
The current field build uses 74,816 B RAM (22.8%) and 905,605 B flash (69.1%).
Bench nodes #56 (conductor), #57, and #58 run integrated firmware build
`be36f8aa`. Mixed old/current fleets rejected both new-only patterns without
changing the active show while legacy Glow and Ocean remained usable; the full
7,128-chunk OTA plus repeated state snapshots completed without a stack panic;
Pond Ripple and Ocean rendered on the reconciled fleet; and uploaded program
`e5d021f64150da90` reached the exact 2/2 readiness barrier. Performer #57 and the
conductor were reset independently and recovered their roles, positions,
firmware, show state, and uploaded-program persistence. The control plane was
restarted twice, and saved uploaded-pattern CRUD plus rebroadcast recovery
remained healthy. A final selective OTA updated only stale performer #58 from
`5c05ebe9` to `be36f8aa`: its full-image CRC matched, activation was verified
from post-reboot firmware identity, and the operation completed with zero repair
chunks. The resulting fleet reports 2/2 exact firmware matches, no attention
flags, and recovery ready. The saved uploaded program then activated at 2/2
readiness on the integrated image before the bench was restored to Ocean Wave at
brightness 48 with params `[9000, 64612, 65069, 205]`.

The operator UI now presents the VM-backed option as **Custom Pattern** and
provides guided controls for traveling waves, center ripples, whole-field
pulses, per-lantern shimmer, and steady glow. Color, timing, spacing, direction,
ripple center, and dim/bright levels compile into the same bounded program
format; JSON is retained only as a collapsed **Advanced source** escape hatch
for existing or hand-authored programs. The stable firmware/API identifier
remains `Uploaded Pattern`. Browser QA covered desktop and mobile layouts,
guided save/reload/validation/activation, and lossless editing of a legacy JSON
program. The generated center ripple reached the exact 2/2 bench readiness
barrier before the bench was restored to the Ocean Wave state above.

Also present from v0.9.4: `WAVEFRONT` remains pattern 11, `FIRE2012` remains
pattern 10, while new IDs are allocated after them (`POND_RIPPLE` 12 and
`UPLOADED` 13). Firefly retains its deterministic three-beat chorus. All ten
USB-connected performers were last read back on protocol 11; no pattern ID from
that release may be reused by later firmware.

Also merged from `main` (2026-08-30): the control plane now owns a simple
Pi-hosted soundtrack player. It discovers MP3s in `sound/`, autoplays
`baskets-soundscape-v4.mp3` in a continuous loop, and preserves the selected
track plus paused state across service restarts. Overview shows the soundtrack
and current minute; the new Sound tab supports pause/resume, restart, and track
selection. Playback uses `mpg123` through the system ALSA output, with an
optional `CONTROL_AUDIO_DEVICE` override. The service has audio-group access,
the Pi runbook installs `mpg123` and Git LFS, and MP3 assets are marked for LFS.
Focused player/API tests and JavaScript/Python syntax checks are green; physical
audio-jack playback on the production Pi remains to be verified.

Also in this feature branch (2026-08-10): the lantern locator is now one
temporary field-wide beacon override instead of a calibration pattern copied
into all eight group slots. It takes precedence over group rendering while
active, leaves every saved group pattern untouched, and starts/stops with one
atomic serial command. This fixes partial shutdowns where Group 1 resumed before
Group 2. INA228 plausibility now validates instantaneous `V × I` rather than
dividing the sensor's retained lifetime energy by fresh ESP32 uptime after a
reboot; the Pi continues to calculate recent draw from consecutive Wh deltas.
Basketbrain now also appends every distinct INA228 report to a durable SQLite
time series under `CONTROL_DATA_DIR/power/`, exposes it through
`GET /api/power/history`, and restores recent Wh-delta anchors after a Pi or
process restart. Replayed latest samples are deduplicated; accumulator resets
start a new energy session, while ESP32 reboots with increasing lifetime Wh stay
in the same session. The web app now opens on an Overview dashboard with field,
sync, firmware, battery, attention, and active-group health. Its power-over-time
widget consumes the durable history in 1-hour through 7-day ranges and charts a
separate 15-minute Wh-delta trace for each meter without bridging bad readings,
stale gaps, or accumulator sessions. Native logic, the full control suite,
JavaScript/Python syntax checks, and all three firmware builds are green. The
Overview also has an animated Expected field canvas reconstructed from the same
pattern math, effective per-group configs, conductor uptime, positions, LED
profiles, field power, and locator override used by the installation. Missing
or mismatched positioned lanterns keep live health outlines, and Fire Flicker
keeps a representative pixel ring. The canvas remains a read-only dashboard;
its explicit Manage locations action opens the placement view. Before any
positions exist it truthfully reports the number awaiting placement. The
placement tab is now named Lantern Locations to
distinguish it from this live field rendering. That tab supports guarded
single-key shortcuts for Locate (L), Move (M), Place (P), Replace (R), Details
(D), and Forget (F), while ignoring typing targets and modified shortcuts. A
small palette-matched SVG lantern favicon now identifies the control plane in
browser tabs. These changes are included in the current local bench build; the
production release remains pending the live pattern check.

Latest in this feature branch (2026-08-08): control mutations now return as soon
as the conductor accepts and persists desired state; field health is reported
later by the state ticker. The old mutation path synchronously fetched
a full fleet snapshot before responding, and the browser fetched another one,
which made successful commands look failed or take 6–14 seconds. Recent snapshots
are now shared across tabs, the full-state cadence is 15 seconds instead of 5,
and state reads have a separate 30-second serial budget. Routine show, group,
placement, and power mutations update
the browser optimistically instead of forcing another fleet read. Group Off is
now an Off/On toggle that remembers the previous brightness. Sleep field pauses a
running OTA at a safe boundary, exits OTA maintenance mode, then applies
force-sleep; direct API sleep refuses a still-running OTA, and automatic
reconciliation stays paused while the sleep override is active. Field power and
sleep scheduling now share one canonical three-state mode: Sleep on schedule,
Always on, or Off. Mode transitions normalize all three firmware flags, while
editing schedule settings leaves the active mode unchanged. This is a
control-plane-only working-tree change and has not been released to the Pi yet.

Latest in this feature branch (2026-08-07): **automatic OTA reconciliation now
targets only stale nodes once the primary already runs the exact immutable
release.** The Pi computes the mismatch set, sends it with the additive
`ota_begin_targets` serial command, and records `cohort_mode=selective`. Firmware
rejects the whole request unless every MAC is fresh in the primary roster, then
uses the existing logical-destination v11 OTA packets for begin/chunk/end, so a
target behind a relay remains reachable. Already-current performers and relays
do not open an OTA writer, receive firmware chunks, activate, or reboot; an
already-current primary does not write or reboot either. The same checkpoint,
repair, CRC/staged barrier, relay-last ordering, pause/resume, and eventual
consistency logic remains in force for the exact cohort. The journal persists
that cohort before radio transfer starts and never downgrades it to full-field
after a Pi/conductor restart. Relayed targeted OTA waits for a downstream
delivery receipt per logical frame, bounding the relay queue even for a 64-node
cohort. The additive receipt carries a frame token, so a delayed chunk-N receipt
cannot acknowledge chunk N+1. When a stale relay and its stale children are
both discovered, reconciliation updates the directly reachable relay first and
defers its children to the next automatic pass; conservative pacing remains the
compatibility fallback. Selective repair stays per-node instead of replaying the
full cohort.
Manual artifacts
without immutable build identity and an outdated primary use the explicit
`full-field` fallback. A pre-v11 node returning after the primary is already v11
fails closed with the one-time USB-station action because it cannot parse routed
packets. Therefore the release introducing this command performs one normal
whole-field upgrade; subsequent routed-v11 single-node drift is selective
without another radio protocol bump. Native parser/cohort tests,
control integration tests for one stale node and the compatibility fallback,
and the canonical field build cover the software boundary; hardware proof
through a relay is still owed.

Latest in this feature branch (2026-08-06): **one-hop conductor relays are
code-complete; hardware verification is pending.** Protocol v11 gives every
packet a logical origin, logical destination, and hop count. The Pi-attached
primary remains the only authority. A board provisioned with `role relay` learns
only a direct primary, stays radio-on, forwards broadcasts downstream and child
unicasts upstream, and never forwards a hop-one packet or promotes itself.
Performers keep a sticky direct/relay parent for the same primary and fail over
only after a 12-second stale timeout. The primary roster retains node role and
next hop, so targeted OTA repair and activation traverse relays without losing
performer identity. After REGISTER delivery, a duty-cycled child holds its radio
for the bounded table-repair return path and releases it early when its row
arrives. Rolling OTA activates verified performers independently,
but no relay is eligible to reboot until every non-relay target has activated;
the primary waits for each relay to acknowledge downstream activation delivery,
then relays activate and the conductor remains last. Native routing tests,
control activation-order tests, and device builds cover the software boundary.
Field proof still needs one primary, one relay, one direct performer, and one
performer shielded from the primary.

**Protocol-v11 rollout seam:** v10 and v11 reject each other. The control plane
detects a v10 primary and changes activation semantics for this one transition:
it first stages and verifies the entire reachable cohort, dispatches every
performer activation while the v10 primary can still address them, activates
the primary last, and verifies the field only after v11 registrations return.
Do not manually switch the primary first. Provision/direct-flash new relay
boards after that coordinated migration. Any performer not reachable during
the final v10 transfer needs USB provisioning before it can join the v11 field.

Latest in this branch (2026-08-06): **release v0.8.0 packages the running
control plane and field firmware as one release by default.** Its checksum-verified companion
firmware becomes the desired image automatically; online mismatches and old
performers that check in later are reconciled without another operator action.
The Firmware tab has a persistent **Automatic updates** show-safety toggle,
enabled by default. Turning it off prevents new automatic updates and requests a
safe pause of an active automatic transfer; explicit manual recovery remains
available. Known-release selection and manual binary upload are collapsed under
Development / recovery. The conductor continues normal beacons and the control plane
interleaves show commands between 128-byte OTA chunks. Every 256 chunks it probes
the frozen online cohort. Two or more valid laggards receive one shared suffix
rebroadcast; a lone laggard receives MAC-addressed unicast, while a bad prefix
CRC or fatal flash error restarts only that board. Normal chunks use six
callback-confirmed radio transmissions; control packets and shared repairs use
eight. An 80 ms pause at each 4 KiB flash boundary prevents a receiver's sector
write from swallowing the next chunk. A targeted repair uses one
delivery-confirmed copy, because the checkpoint and final CRC barriers own
reliability. Serial and radio recovery retry with bounded backoff for up to six
hours. The normal UI is one operation: each performer installs and reboots as
soon as its own full image and CRC are verified, without waiting for the rest of
the field; laggards keep repairing independently and the conductor activates last. The
stage-only and separate activation routes remain recovery APIs. The UI
exposes broadcast/repair/staging/activation phases and leaves pattern, blackout,
and power controls available. Install state is journaled beside the
checksum-pinned artifact, resumes after control-service restart without changing
requested intent, and supports an explicit pause/resume from the verified
conductor prefix. Native logic, control API fault injection, rolling-order,
persistence, and canonical field-build checks are green. Full 53-board hardware
verification is still required before calling the scale behavior field-proven.
The local USB-attached conductor has been direct-flashed with the new
repair/probe/activation and shared-rebroadcast RPCs while preserving its NVS.
Any still-legacy conductor needs that same one-time bootstrap before its first
live OTA; preflight detects the old command set before `ota_begin`.
The conductor now also defers `Update.end()` itself until conductor-last
activation; merely verifying its local image no longer changes the partition
that an incidental reset will boot.

Also in this branch: **performer registration no longer forms a synchronized
return-traffic herd.** A 17-board local field received conductor beacons but only
nine initially appeared in the roster; power-cycling five immediately made all
five register, confirming that their immediate/10-second check-ins were
phase-locked. REGISTER now uses a dependency-free, host-tested scheduler: expired
deadlines are spread into stable MAC-derived slots over 500 ms inside the next
shared radio window, successful delivery returns to a jittered 10–12-second
cadence, and failed ESP-NOW delivery retries with 100 ms–2 s bounded backoff.
Performer unicasts are serialized by purpose so REGISTER cannot consume a power
or OTA-status callback. The UI header separately shows the current conductor
contact count, and every group dropdown shows `online / total` membership.

Hardware acceptance on 2026-08-05 found and fixed three real-device-only edges:
`ota_begin` copied the 3,080-byte roster onto the ESP32's 8 KiB loop-task stack
(leaving 64 bytes and crashing the Wi-Fi heap spinlock), repair sends treated an
ESP-NOW queue acceptance as radio delivery, and deployed `0.5.1` performers could
not answer the new checkpoint query. The conductor now snapshots a bounded
385-byte cohort, waits for ESP-NOW unicast delivery callbacks, and defers a
query-incompatible laggard to at most one full delivery-confirmed replay at the
final staging barrier instead of repeatedly replaying prefixes. A clean
`a0e58bfc` artifact (`881984` bytes, CRC32 `3998627663`) completed OTA to
performer #23 in 709 seconds while Fire Flicker remained controllable; the final
operation reported all 6,891 chunks, matching CRC, post-reboot `complete`, and
both conductor and #23 on clean `v0.7.1` build `a0e58bfc`. The local field now
reports 17 performers radio-online after directly updating two protocol-v8
stragglers; #23 is intentionally offline. The full 18-performer acceptance run
therefore remains pending.

The next scale acceptance run must exercise independent completion explicitly:
healthy performers should install as soon as they verify, a board that missed
`OTA_BEGIN` must receive a targeted writer restart before shared suffix replay,
and that laggard must not hold already-verified performers behind a fleet-wide
barrier. Record total duration and repair counts, and verify all performers plus
the conductor return on the expected clean build. Repeat for #23 when it is
online to close the requested 18-performer gate. The UI progress regression is
covered: a partial row such as 224 KiB / 862 KiB cannot render 100%, and the
green check requires full size plus the expected CRC (or confirmed installed
firmware).

Latest on main (2026-08-04): **the multi-board USB flashing station is built.**
The control plane now has a Firmware screen backed by a separate same-host
provisioner daemon. It detects FireBeetles on macOS and Linux and automatically
queues every connected board, flashing five boards in parallel by default
(configurable to ten). Optional USB-topology slot labels persist across port-name
shuffles and service restarts. Progress streams over the existing WebSocket and
retry is available for connected failures. Factory erase requires an explicit
15-minute session. Production
mode fails closed on blank boards, the configured conductor path is excluded
before probing, and role verification refuses any conductor. Permanent-ID
reservation now goes through the conductor's canonical NVS inventory before the
performer is written and read back. Packaging includes a hardened Pi systemd
unit, GitOps deployment integration, a macOS LaunchAgent installer, and a pinned
flash runtime bundled inside the checksum-pinned serial artifact. The browser
workflow was exercised at desktop and 375 px mobile widths without
console/network errors. Hardware throughput proof with a real powered hub and
5-10 simultaneous FireBeetles remains the next gate.
The v0.7.0 tag workflow publishes a serial ZIP using flash-plan schema 2 with
its usable local flashing runtime. Older v0.6.0 artifacts fail station preflight
clearly rather than starting board jobs without that runtime.
The schema-2 build gate now executes the bundled flashing runtime, the station and GitOps deployment share an operation lock, USB path reuse fails closed, production health includes the daemon, and conductor ID reservations roll back unless NVS persistence succeeds.
The station now inspects connected USB boards even while idle. The Firmware UI
shows permanent ID, role, current version/build/protocol, the production target,
and Current/Update needed before flashing is armed. Unknown boards remain
explicitly unknown until a station run, and detected conductors are excluded.

The one-time inventory/tagging pass is complete: **53 boards are registered**.
No performer firmware migration is required for the station itself; only the
conductor needs the new `reserve_id` serial RPC before a station may allocate a
new permanent number.

Also landed on main (2026-08-04): **groups have persistent operator-facing names, and
the failed USB battery keepalive experiment is removed.** The Patterns screen
can name any fixed group slot (for example, `Box lanterns`, `Lotus lanterns`, or
`Bikes`); the resulting `Group N · Name` label appears in pattern controls, node
details, plus the node detail and node-list assignment dropdowns. Names live only in the control
plane's data directory, so IDs 0–7, performer membership, NVS, and protocol v10
stay unchanged. Clearing a name restores the numbered label. The obsolete
keepalive UI/API/serial/NVS/render behavior is gone. Its six beacon bytes remain
reserved and zeroed to keep the v10 wire layout compatible with the currently
flashed bench nodes.

Previous latest (2026-08-04): **protocol-v10 groups and mixed LED counts are
bench-verified, and global blackout is reversible.** The live three-board bench
runs one conductor plus performer #23 in Group 1 on a 16-pixel ring and performer
#24 in Group 2 on a 64-pixel strip. Group 1 ran White while Group 2 simultaneously
ran Fire Flicker. Both performers retained group, LED count, and build identity
across direct flashes; the 64-pixel chain also visibly honored live 64 → 32 → 16
→ 64 profile changes and restored 64 from NVS after reboot. The Patterns UI now
distinguishes `Turn off Group N` from `Blackout all groups`. A global blackout
captures all eight brightness values in a separate NVS recovery record before
saving zeroes; repeated blackout does not overwrite that record, and `Restore
all groups` recovers it. Hardware proof blacked out both bench groups, hard-reset
the conductor, confirmed the recovery record and zeroes survived boot, then
restored Group 1 White/24 and Group 2 Fire Flicker/24. A targeted Group 1 off
through the real HTTPS API left Group 2 at 24 and Group 1 was returned to 24.

Previous latest (2026-08-04): **mixed 16/32/64 LED counts are code-complete.**
Each inventoried board carries an LED-count
hardware profile independent of its group and placement. The control plane can
change it from the lantern detail sheet or inline Node List dropdown, including
before placement. One firmware image allocates a 64-emitter RGBW chain, renders
only the configured active prefix, clears inactive pixels, and normalizes
ring-local effects by the active count. Existing inventory and performer NVS
migrate to 16. The profile stays with the physical board during move, forget,
and replacement. Protocol v10 adds the profile to REGISTER and `MSG_TABLE`, so
every board must be updated together.

Previous latest (2026-08-03): **eight independent lantern groups are
code-complete; bench verification is pending.** Each inventoried MAC now carries
a Group 1–8 membership alongside its permanent board ID and optional position in
the conductor-authoritative inventory. The existing 4 Hz beacon carries all
eight persisted pattern configs in one packet, so every group can run a
different pattern without extra packet cadence or losing free-run behavior. The
UI assigns membership from the lantern detail sheet or an inline Node List
dropdown, including before placement, and targets live or saved patterns with a
group selector. Clearing a position preserves membership. Global blackout,
power policy, and calibration
still span the field, while calibration restores every group's prior look
afterward. Protocol v9 migrated existing v8 rows to Group 1 and copied the old
field-wide look to all slots, but the wire change means every board must still be
updated together. No boards were flashed in this session.

Previous latest (2026-08-03): **the v0.5.1 hotfix makes the permanent-ID release
deploy reliably on the Pi.** The first v0.5.0 production reconcile exposed that
fresh commit-specific Python environments retained `0700` mode from their
temporary build directory, so the unprivileged control service could not execute
them. Automatic rollback restored healthy v0.4.0. The reconciler now publishes
fresh environments with traversable permissions and repairs a complete existing
environment before reuse. v0.5.1 rebuilds the same protocol-v8 firmware from the
reviewed hotfix commit.

The v0.5.0 feature release adds permanent numeric IDs
backed by their immutable MAC addresses. The conductor persists every learned
`MAC → ID` inventory row independently of field position, shows the ID while a
board is offline, rejects duplicate/conflicting reports, and sends the
authoritative ID plus optional position back to an erased performer. Existing
v7 position-only NVS tables migrate without losing coordinates; the wire change
bumps the field to protocol v8. The FireBeetle watcher upgrades its existing
device registry in place, adopts a valid on-board ID or allocates the lowest
unused positive number, writes and reads it back over serial, and prints a large
`BOARD #n - LABEL THIS BOARD` banner even when firmware is already current. The
Pi-attached conductor has been direct-flashed to protocol v8 with its NVS
inventory preserved. Performer inventory/tagging remains pending the promoted
v0.5.1 artifact; after publication, refresh the conductor from that exact
artifact and flash all performers together for the v8 protocol change. Before
inventorying, rerun the documented
`firebeetle_autoflash.py install --factory` command: the LaunchAgent runs a
copied script and does not gain this behavior from firmware promotion alone.
The existing `devices.json` is preserved; its first safe migration may require
one scan of every previously known board before any unnumbered board is assigned.

Previous latest (2026-08-01): **the production release now also drives batch
FireBeetle provisioning.** Tag CI builds a deterministic, manifest-bound serial
ZIP containing the bootloader, partition table, OTA boot helper, field firmware,
and verified flash plan. The checked-in macOS LaunchAgent watcher follows only
the reviewed production channel, caches the last verified bundle, filters for
the fleet's WCH + ESP32/40 MHz/4 MB signature, preserves valid Lightweave NVS,
erases factory/unrecognized boards once, and verifies the promoted clean build
plus role/ID/position after upload. See `FLASHING.md`.

Previous latest (2026-08-01): **pull-based Pi releases and separate deployed-change
visibility are implemented for v0.4.0.** A reviewed `production.json` pointer
selects one hash-pinned immutable release manifest; each Pi polls outbound,
verifies the repository/tag/commit and firmware hashes, backs up state, deploys
the control plane into a fresh commit-specific Python environment, health-checks
it, and atomically restores the untouched prior environment/code/record on
failure or on the next timer run after an interrupted deployment. Root-owned
durable rollback state, hash-locked application and release-tooling
dependencies, exact-commit health checks, and a shared OTA/deployment lock close
the privileged update boundary. A
successful deployment stages the release firmware without starting OTA, and a
timer firing during OTA safely defers. Operations shows web-control and field-firmware versions, sync state, and
separate changelogs from `RELEASES.json`. Tag CI publishes the canonical field
binary plus manifest through a resumable, byte-verified draft; the same release
action then promotes that exact manifest through a second reviewed PR. Offline
placed performers keep the firmware card explicitly
deferred until all layout rows verify. The complete procedure is
[`RELEASING.md`](RELEASING.md). Physical installation and health of the Pi timer
remain operator-owned proof steps; release agents own production-channel
promotion and pointer verification.

Previous latest (2026-08-01): **manual OTA now treats the online field as a
frozen required cohort instead of requiring every layout row to be present.**
The conductor considers a performer online after a registration within the
last 30 seconds, snapshots all fresh placed MACs at `ota_begin`, returns that
target list to the control plane, and requires fresh exact-size/exact-CRC
completion from every target before rebooting. Offline placed rows are shown as
deferred and do not block the run; a later manual maintenance run catches them
up. This is still one field-wide ESP-NOW broadcast, not selected-node or
opportunistic OTA. The API persists the target/deferred lists and verifies only
the frozen target set after a final-ack timeout. Hardware rollout is still
pending; the existing conductor firmware predates cohorts, so the first remote
upgrade must temporarily narrow and then restore the saved layout table (or use
a direct USB flash) before later updates gain native partial-online behavior.

Previous latest (2026-07-31): **the first ring-addressable show pattern is
code-complete and ready for bench tuning.** `FIRE_FLICKER` (`pattern 9`) keeps
the compact clock+config broadcast but evaluates
`f(x,y,pixel_index,t)` locally, giving all 16 LEDs a deterministic shared
billow plus coherent angular flame texture. Bright pixels lean yellow and dim
pixels lean red; speed, base color, sRGB value, and texture depth are tunable.
The control plane exposes Fire in the pattern picker, packs the positional wire
params, renders individual ring emitters in PNG previews, and includes all 16
RGBW samples plus intra-ring contrast in JSON previews/reviews. Offline review
on the 9-node mock layout rated the default candidate `strong` / 100 with
temporal luma range 10.943 and max ring contrast 0.2088 at brightness 56.
The USB-attached board (`30:76:F5:93:67:3C`, ID 1) was flashed with the
`devkitc` bench image from dirty build `a425d7eb` on 2026-07-31. It reported and
preserved a CONDUCTOR role and its post-flash beacon sequence advanced normally,
but the older hardware record below identifies this MAC as performer #2 and the
conductor as `8C:94:DF:57:7F:14`. The operator subsequently confirmed that a
second conductor is powered in range on the Raspberry Pi alongside one
battery-powered performer. With two conductors beaconing, the performer follows
whichever beacon most recently overwrote its learned conductor MAC; that
explains why a 20+ second check found no performers in the laptop conductor's
roster. Its placement table is also empty, so no OTA was started; the temporary
field-awake override was restored to off. Power down one conductor before OTA
so readiness and completion status have one authoritative controller. No
performers were flashed, and nothing was saved or broadcast. Native tests are 142/142, control
tests 215/215, and the then-current firmware environments built. Hardware verification
still owed: establish the intended conductor/performer identities, flash the
same build to all bench performers before broadcasting Fire, then tune
period/texture/color against the actual diffuser and ring orientation.

Previous latest (2026-07-29): **remote-administration phases 1–3 are
code-complete; physical rollout remains deliberately unclaimed.** Serial mode
now fails closed behind a strict shared-password session boundary that protects
HTTP, previews, uploads, OpenAPI, and WebSockets. Login uses the canonical
resource-bounded scrypt format generated by `python -m control.auth
hash-password`; sessions are process-local, expire after 12 hours, and close
their sockets on logout/expiry. Exact Origin, trusted-loopback proxy, HTTPS,
anti-framing, HSTS, and field network-mutation rules are covered by tests.
Field OTA now returns `202` after bounded preflight, runs as one server-owned
task, exposes authoritative progress through GET, and returns immediate
`423 Locked` for competing serial work instead of queuing it behind the
transfer. Mutable OTA/pattern/calibration/audio state is rooted by
`CONTROL_DATA_DIR`. Pi Zero 2 W packaging and the complete Starlink +
Cloudflare Tunnel runbook are under [`deploy/pi/`](../deploy/pi/README.md);
stable architecture is in [`REMOTE_ADMIN.md`](REMOTE_ADMIN.md). The next owner
must perform the human/account/hardware rollout gates in phase 4 of
[`plans/remote-administration.md`](../plans/remote-administration.md), including
installed systemd verification and the browser-disconnect/restart OTA drills,
before claiming field deployment.

Previous latest (2026-07-08):
**computer-vision calibration has a first usable API/UI scaffold, a synthetic
proof harness, and a live calibration-mode toggle** while hardware power testing
waits on more boards/INA228 modules.
The control plane can upload calibration still images (`.jpg`, `.jpeg`, `.png`,
`.webp`), detect bright LED blobs, decode an ordered multi-photo on/off identity
sequence into bit values/tracks, and propose reviewable `(mac,x,y)` assignments
by mapping decoded codes onto the current alive roster or an explicit roster. It
can also render a synthetic ordered PNG sequence from a known layout and run that
sequence through the exact same upload/detect/decode/propose path. Synthetic and
video workflows use an explicit blink-code plan with default Hamming distance 3,
so a missed bit should surface as missing/extra instead of silently assigning the
wrong MAC. Firmware now has a `Calibration` pattern that generates the same
Hamming-spaced code sequence from node IDs (`params`: slot ms, bit count, first
code, min Hamming distance), and the control plane exposes it as a simple
Operations → Lantern Locations **Play/Stop lantern locator pattern** toggle. Video/photo
analysis is a separate action: the normal visible path is select a video and
click **Analyze video**; developer controls (Plan codes, Upload frames, Extract
video, Refresh frames, Simulate, Propose layout, threshold/min-area/timing) live
inside Advanced. The review UI shows proposed lantern locations directly over
the captured frame; the normal path does not show the raw PNG manifest or dump
every ignored extra track. The analyzer now scales the minimum blob area for
browser-extracted video frames and uses code-aware temporal contrast for those
video frames, so constant glare, cables, and non-node bright objects do not win
just because they are bright. It still tries every cyclic bit alignment for the
planned code map so the video does not have to start exactly on slot 0, accepts
only tracks whose blink signature matches the planned lantern codes, and reports
non-matching lights as extras. Duplicate matching codes are accepted only when
one track is clearly stronger; comparable duplicates remain ambiguous. Synthetic
simulation supports deterministic jitter, dim LEDs, glare blobs, missing frames,
and perspective warp. This is intentionally non-destructive: it does **not**
write the conductor layout table yet. Live bench
OTA on 2026-07-08 staged `.pio/build/devkitc/firmware.bin` (`862992` bytes /
`6743` chunks / sha256
`98daf0fb6e42435bfa577d81b6642d61fa8f3a9986bba4b39780d493f42b83c4` / crc32
`4291516488`), streamed in `378 s`, returned `ota install complete; rebooting`,
and post-reboot `/api/state` showed both performers alive and firmware-consistent
on build `6fb35676`, `dirty=true`. The live calibration toggle then started
successfully with a 2-node plan (`001` and `110`) and stopped back to the prior
pattern. The existing per-node identify command is now exposed as **Locate** in
both the selected-lantern sheet and each Node List row, so an operator can make
one physical lantern visibly identify itself from either view. Regression test
`test_real_two_node_video_auto_aligns_and_ignores_extra_lights` extracts the
fixture `control/tests/fixtures/2_nodes_calibration.mov`, recovers both bench
nodes from the first three 1 Hz frames with `alignment_offset=2`, and leaves
unplanned bright objects as extra tracks. Regression test
`test_real_two_node_video_with_extra_lights_uses_temporal_code_signal` covers
`control/tests/fixtures/2_nodes_calibration_extra_lights.mov`, the phone clip
that previously assigned glare/cable locations; it now resolves the upper/right
`001` node around `(0.69,0.34)` and the lower `110` node around `(0.48,0.65)`.

Previous latest (2026-07-07):
**OTA finalization is now status-gated before conductor reboot and
hardware-verified on the 3-board bench** — after
`ota_end`, the conductor broadcasts the finalize command, then waits up to 30 s
for every placed performer (excluding the conductor's own MAC if it is in the
layout table) to report a fresh `complete` status with the exact staged image
size and CRC. If any performer does not verify, the conductor returns
`ota performers did not complete` with the current per-node OTA status table and
does **not** finalize/reboot itself, leaving the API/UI with concrete failed
node rows and a retryable maintenance state instead of silently declaring
success. The Python serial adapter now preserves structured fields on failed
acks, and the API stores failed-finalize node details in
`/api/operations/ota-install`. Live drill: all three DevKitC boards were first
USB-flashed to local dirty build `b00f872b`, then the real serial-backed API
staged `.pio/build/devkitc/firmware.bin` (`862432` bytes / `6738` chunks /
sha256 `295bbeb997e19ecb411c789351df5c8cdb6d6620d4d413bfa6985449f78d0d42` /
crc32 `3357813144`), entered maintenance with `2 / 2` ready, streamed the image
in `369.7 s`, returned `ota install complete; rebooting`, and
`/api/operations/ota-install` recorded both performers at terminal `complete`
with `offset=862432` and matching crc32. Final post-reboot `/api/state` showed
`summary.alive=2`, `summary.total=2`, `attention=0`,
`summary.firmware.consistent=true`, `build_label=b00f872b`, `dirty=true`, and
OTA back to idle with both complete status rows retained.

Previous latest on `main` (2026-07-06):
**manual field-wide OTA is end-to-end hardware-verified and retry-hardened on the 3-board bench** —
Operations can enter a maintenance window, stage a `.bin`, stream it over USB
serial to the conductor, fan it out over ESP-NOW to performers, show chunk
progress, retry dropped serial chunk ACKs, and reboot the field onto the staged
image. Latest verified artifact: clean build `a11fffec`, `860944` bytes /
`6727` chunks / sha256
`c621ea7eeb366bdad0204a4c3a787b7bf453f82bd457553a60d25e55fe182da2`; the
2026-07-06 live API drill staged that image, entered maintenance with `2 / 2`
ready, recovered serial chunk timeouts via retry, streamed all chunks in about
467 s, returned `ota install complete; rebooting`, and post-reboot `/api/state`
showed conductor and both performers on `0.3.0` build `a11fffec`,
`dirty=false`, `summary.alive=2`, `summary.total=2`, `attention=0`, and
`summary.firmware.consistent=true`. `/api/operations/ota-install` reported
`chunks_sent=6727`, `bytes_per_s≈1843`, `eta_s=0`, and both performers at
terminal `complete` with `offset=860944` and `crc32=723916971`. Previous clean
build drill: `ba705b46`, `860928` bytes / `6726` chunks / sha256
`106cfda591acc64ece0c9c1272d4570e7b0b8e418520ac3faedc22e26f05dbee`, about
400 s. Negative safety drill: direct-flashed performer #2 to same-protocol build
`9821db52`; `/api/state` reported `Firmware mismatch` and
`recovery.status=mixed_firmware`, and reinstalling the clean staged `a11fffec`
image restored both performers. That restore exposed a final `ota_end` serial
ack timeout after all chunks had landed and the field had actually rebooted
cleanly; the API now handles that shape by forcing post-reboot verification
instead of leaving the install failed. A follow-up restore also proved that a
periodic `ota_progress` serial timeout during the stream is non-fatal: the API
records `last_progress_error`, keeps transferring chunks, and relies on the next
poll or post-reboot verification. Previous verified recovery artifact:
`860944` bytes / `6727` chunks / sha256
`906fc37a03fa2c1afe97c1a35ba4f8153e295df0de5672232312d2fb7e9c1568`; the final
live run intentionally recovered one same-protocol mismatched performer
(`#1`, `0.3.0-mismatch`) by staging the normal `0.3.0` image, entering
maintenance, streaming all chunks, and returning `ota install complete;
rebooting`. Post-reboot state showed `summary.alive=2`, `summary.total=2`,
`attention=0`, and `summary.firmware.consistent=true`; `install.nodes` reported
both performers at `phase=complete`, `offset=860944`, `crc32=3411679313`.
Root cause of the prior failed recovery path was a pair of OTA correctness bugs:
200-byte serial chunks were too close to the 512-byte firmware command buffer, so
a truncated-but-even hex payload could decode as a shorter chunk and advance the
firmware writer by a non-chunk length; separately, the API could report success
from a nonempty node list instead of requiring every expected performer to verify.
Fixes now use 128-byte chunks, require exact decoded length for the current
offset, remove the invalid per-128-byte `Update.progress()` invariant, retry
explicit chunk-length mismatch responses, wait for maintenance beacons to settle
before `ota_begin`, and require all expected placed performers to report complete
or verify from post-reboot field firmware consistency. Performers also report
begin/writing/complete OTA status so future failures expose their offset/error.
Previous latest: **12 V power monitoring is code-complete in firmware/API/UI** —
the conductor retains the latest `MSG_POWER` sample per metered MAC and exposes it
in `/api/state`; the dedicated Power tab shows top-line SOC and average draw per
metered performer, defaulting to the 384 Wh KUNLUN model 1230 pack. SOC is based
on Wh used since a full-charge anchor, not a voltage curve: voltage at or above
the configured full threshold (default 14.4 V) can auto-anchor a metered node to
100%, and each metered node has a manual **Sync to 100%** button after charging.
Previous latest: **runtime power schedule is
code-complete** — Operations can set light-sleep/radio check interval,
deep-sleep check interval, LED-on window, and force-awake override. The conductor
persists that `PowerPolicy` and broadcasts it in every beacon; performers apply
it without further firmware changes. UTC epoch anchoring for sleep checks bumps
`VERSION` to `0.3.0` and `PROTO_VERSION` to 6, so all boards must be reflashed together before the live
bench can use it. Photodiodes are now optional/fallback, not the main sleep
strategy. Previous latest: **release version display for OTA
safety is code-complete** — firmware reports `VERSION` in addition to protocol,
git-derived build id, and dirty flag; the Operations/detail UI shows the human
version and links the commit hash to GitHub. Previous latest: **OTA
safety foundation hardware-verified** — all three bench boards were flashed to
`PROTO_VERSION 3` build `c046bf54` with dirty=false; `/api/state` reports
`summary.firmware.consistent=true`, `matching=2`, `expected=2`, and the
Operations tab shows `2 / 2 on this build`. REGISTER carries protocol +
git-derived build id + dirty flag in that verified build, and the machine state
exposes conductor/per-node firmware identity so mixed firmware is visible before
OTA transfer exists. Previous latest: **real control plane on the bench** —
the FastAPI UI/API can now talk to the conductor over USB serial using
newline-delimited JSON while preserving the human CLI. Hardware-verified with
one conductor + two performers: `/api/state` sees both performers, map placement
works, and Pattern tab changes ack through the real conductor. The Pattern tab
now has per-pattern controls: Pulse/Glow use hue, Sweep uses period+wavelength,
and Palette Drift uses period+spatial spread. Same-session review fixes moved
blocking serial calls off the FastAPI event loop and cleaned WebSocket disconnect
handling. Previous latest: **review-debt paydown** — the
host-unreachable logic extracted out of `main.cpp` into pure tested headers
(`macaddr.h`, `table_wire.h`, `bootplan.h`, `patternBootSafe`), the `field` build
(`-D HEARTBEAT_LED=0`), and the table rebroadcast stretched 5 s → 60 s
steady-state with **targeted single-row replies to needy REGISTERs** (the
initial new-MAC-burst design was replaced the same day after an 8-angle
adversarial review confirmed three delivery holes in it — see "Self
code-review" below). Before that: **INA228
power-telemetry firmware (code-complete, host-tested, 8-angle-reviewed — built
ahead of the chip, see "INA228 power telemetry" below)**,
Stage-A radio duty-cycle (measured), **Stage-B CPU light-sleep
(hardware-verified on bench 2026-07-03)**, **Lever-2 daytime deep-sleep
(code-complete, default off, awaiting the pilot phototransistors)**, a
full-repo adversarial self-review with all 5 correctness findings fixed, the
production BOM, and the **pilot-batch order placed 2026-07-03** (most parts
arrive Mon Jul 6, batteries Jul 10 — see "Pilot batch: ORDERED" below).

## ▶ Next session: pick up here (updated 2026-08-30)

Priority order:
1. **Hardware-verify and live-tune the new patterns on the ten-board line:**
   first distribute one uploaded expression through a relay, confirm every
   placed node acknowledges the exact program and firmware identity, and measure
   frame timing with the VM active. Then run Pond Ripple from the center and an
   off-center point, including a mixed-firmware negative check that leaves the
   current built-in show untouched. Continue with Wavefront at
   angle 0 and confirm the band travels left-to-right in physical board order.
   Then evaluate Firefly's irregular solo behavior and three-beat chorus, and
   Fire2012's speed/cooling/sparking against the actual ring orientation and
   diffuser. Keep Automatic OTA disabled during the show check. If the looks pass,
   proceed with the normal reviewed release workflow; otherwise tune defaults and
   repeat before release.
2. **Finish the remaining protocol-v10 repair checks:** simultaneous Group 1/2
   patterns, 16/64 physical chains, live 16/32/64 profiles, profile NVS restore,
   reversible global blackout across conductor reboot, and selected-group off
   are hardware-verified. Still change one membership and one LED count while a
   performer radio is asleep, then confirm its next REGISTER triggers the
   targeted row repair and persists across performer reboot. Test a physical
   32-pixel chain when one is available; 32 is currently verified as the active
   prefix of the 64-pixel strip.
3. **Fire Flicker bench tuning:** after the field shares the current protocol-v10
   build, select Fire at brightness 56 / period 1200 ms /
   hue 24 / saturation 95 / texture 85, and evaluate it through the physical
   diffuser. Tune for organic neighboring-pixel motion without a visible chase;
   check draw on the INA228 reference node before adopting a brighter default.
4. **Remote-administration rollout (human-owned):** follow
   [`deploy/pi/README.md`](../deploy/pi/README.md) and phase 4 of the remote
   administration plan. This needs the Pi, Starlink, Cloudflare account, final
   hostname, and 3-board bench. Do not record the shared password, hash, or
   tunnel token in this repository.
5. **CV calibration apply workflow:** the phone-video proof is now good enough
   on two real clips, including one with large glare/cable false positives.
   Add a guarded "apply proposal" endpoint/UI that writes assignments through
   the existing `/api/lanterns/{mac}/assign` path only after the operator
   reviews the image overlay and any missing/ambiguous rows.
6. **Synthetic hardening follow-up only when needed:** run Simulate with jitter,
   dim LEDs, glare, missing frames, and perspective values that approximate the
   phone capture. Tests already cover clean recovery, noisy recovery, and
   missing-frame alias prevention; add cases only when real media exposes a new
   failure mode.
7. **Drone/field media validation:** when a drone clip exists, run it through
   the same Lantern Locations flow and add a fixture if it exposes a new failure
   mode. Temporal code scoring should ignore constant extra lights; only extra
   lights blinking with the same planned code should remain ambiguous.
8. **Hardware-verify scale-hardened OTA:** first run the requested 18-performer
   field once all 18 are radio-visible. Confirm checkpoint status collection,
   targeted suffix repair, the explicit `18 / 18 staged` barrier with no reboots,
   one-action rolling activation, and live show control under real ESP-NOW
   contention. Record total duration, repair counts, and any nodes requiring a
   targeted restart. Later expand the same proof across the 53-board inventory.
9. **Optional negative OTA-safety check:** if useful, intentionally flash one
   performer with a same-v8 but different build and confirm it appears as
   `Firmware mismatch`; restore all boards to one build afterward. Any
   protocol-mismatched board vanishes from the roster due to the version gate.
10. **When parts are in hand:** phototransistors are no longer required for the
   main sleep strategy. Treat them as optional/fallback only. Wire INA228 on one
   reference node (SDA→21, SCL→22, chip in series between
   battery+ and buck input) → run the INA228 bench checklist below → first
   real Wh integral; first flash of the `firebeetle` env on real FireBeetle
   hardware. **Plus one new 2-minute check:** with the conductor up and a
   table row assigned, `erase_flash` + reflash a performer and confirm it
   re-adopts its position within ~10 s of registering (the new single-row
   `[table]` reply; code-reviewed + host-tested but the radio path itself
   isn't hardware-verified yet).
11. **User task, anytime (needs hands + DMM):** re-measure the 12 V
   battery-side draw with naps running, **USB disconnected** (USB backfeeds the
   5 V rail and corrupts the reading) — quantifies the Stage-B win vs the old
   51 mA rest / 55 mA avg numbers. Same scene for apples-to-apples: amber GLOW
   @ bri 48.
12. **Hardware topic to revisit:** buried battery/control boxes will likely make
   onboard 2.4 GHz antennas unreliable. Read `docs/RF_ENCLOSURE.md` before buying
   more MCUs or committing to enclosure geometry. The pilot FireBeetles already
   ordered are DFR0654-F onboard-antenna boards; buried-box deployment likely
   needs external-antenna ESP32-UE boards plus above-grade antennas.

**Battery selection update (2026-07-25):** production now uses the KUNLUN model
1230 12.8 V 30 Ah / 384 Wh LiFePO4 pack (eBay item `357870398757`), replacing the
TalentCell LF120A1 as the plan of record. The control-plane defaults are 384 Wh
capacity and 14.4 V full-charge anchoring. Pack label limits: 14.4 V charge
(14.6 V max), ≤20 A charging, 0–45 °C charging temperature, and M8 terminals.
The older 12 Ah/138 Wh measurements and Amazon receipt below remain as historical
bench/pilot records. The production BOM contains the fleet charging topology and
revised cost roll-up.

---

## Where the build is right now

**Current implementation** (hardware verification status is called out per item):
- **One firmware image for every node** (`src/main.cpp`); role is a runtime NVS
  value (default performer), set over serial. Build envs: `devkitc`, `firebeetle`,
  `native`, plus one canonical cross-board `field` image with
  `-D HEARTBEAT_LED=0` (the onboard blink is invisible inside an opaque lantern,
  burns LED current, and caps every Stage-B nap at 500 ms). Bench flashes keep
  the heartbeat; flash `field` for deployment.
- **Sync:** conductor broadcasts a clock beacon; performers lock an offset and
  render against synced time; **free-run on missed beacon** (no blackout), re-lock
  on return. Verified: `LOCKED`, stable offset ~±100 µs, `gaps=0`.
- **Patterns** (`f(x,y,t)`): eight independent group configs travel together in
  each beacon; a performer selects its cached group and free-runs that look.
  Built-ins include `PULSE` (uniform breathing), `PALETTE_DRIFT` (smooth
  rainbow hue cycle; `params[0]`=period ms, `params[1]`=spatial hue offset ×100 so
  the rainbow travels or runs in unison), `SWEEP` (1-D traveling wave),
  `WAVEFRONT` (one directional 2-D band), `GLOW` (steady color), `FIREFLY`
  (irregular solos plus a periodic three-beat chorus), `OCEAN_WAVE` (summed 2-D
  wavefronts), `WHITE` (the dedicated white channel), and
  `SOLID` (`pattern 3`: every pixel full RGBW — the worst-case power draw, for
  bench-measuring the LED ceiling). Conductor broadcasts the pattern
  (`pattern_id`/`brightness`/`params[4]`); performers render it. Every node
  hard-clamps brightness to `MAX_BRIGHTNESS` (config.h, **192**) so no pattern can
  exceed the per-node power budget (see the worst-case measurement below).
  `FIRE_FLICKER` extends the local model to `f(x,y,pixel_index,t)` and is the
  first pattern to render distinct values across the ring; it appends ID 9.
  `FIRE2012` (ID 10) adds deterministic heat cells across the active emitter
  sequence, `WAVEFRONT` is ID 11, `POND_RIPPLE` is ID 12, and the bounded
  data-driven `UPLOADED` option is ID 13. Uploaded programs use a separate
  capability-gated distribution path; all earlier renderers stay unchanged.
- **NVS identity:** `id` + `(x,y)` + group persist across reboot; position/group
  are adopted from the conductor table.
- **Group pattern configs persist** too: all eight
  `pattern_id`/`brightness`/`params` slots survive a conductor power-cycle in
  the `patterns` blob; Group 1 remains mirrored to the legacy keys. Global
  blackout snapshots all eight brightness values separately before persisting
  zeroes, so Restore remains available across conductor reboot and repeated
  blackout cannot overwrite the recovery point.
- **Control plane serial bridge + UI** (hardware-verified 2026-07-05): FastAPI
  serves the static operator UI and HTTP/WS API; `JsonLineSerialConductor`
  talks to the conductor over pyserial with request ids and ok/error acks.
  Mutations currently implemented: identify ack, assign/place, group assignment,
  forget, replace, per-group pattern changes/off, reversible field-wide blackout,
  power policy changes, OTA maintenance
  mode enter/exit, firmware artifact staging, and field-wide OTA install.
  Serial calls are serialized and run
  off the FastAPI event loop, so one serial timeout does not block unrelated
  async work. The UI has Overview, Lantern Locations, Node List, Patterns, Sound,
  Power, Operations, and Firmware views; location-map zoom/pan,
  drag-to-move/place, unpositioned tray, single bottom-sheet actions,
  per-pattern controls, field firmware consistency display, Recovery card, and
  the OTA updater card are all wired. Hardware-verified 2026-07-05: after flashing
  all three bench boards, `/api/state.ota` reported maintenance `ready=true`,
  `ready_count=2`, `expected=2`, no blockers; repeated full OTA installs streamed
  all chunks and returned the field to idle with both performers healthy and
  firmware-consistent. The latest run streamed `6727 / 6727` chunks and restored
  performer #1 from `0.3.0-mismatch` to `0.3.0`; both performers reported
  terminal `complete` status at the full image offset. Recovery dry-run follow-up
  2026-07-06: old performer firmware aborted on repeated already-written OTA
  chunks; `otaChunkDecision()` now makes those duplicate chunks idempotent, OTA
  maintenance beacons keep performer radios awake until the window ends, and
  pyserial writes are bounded with no unbounded `flush()`. Follow-up
  investigation found the actual unsafe partial-write case: a truncated but
  even-length hex payload could decode as a shorter chunk, advance the firmware
  writer by a non-chunk length, and leave the API believing the full chunk had
  landed. The firmware now requires exact decoded chunk length at the current
  offset, and the API refuses to mark an install complete unless all expected
  placed performers report complete or verify from post-reboot field state.
- **Protocol foundation Half 1** (hardware-verified): typed message header
  `{magic, version, type}` with type dispatch; **MAC identity** read at boot and
  shown in `info`; **bidirectional ESP-NOW** — performers unicast `REGISTER`
  every 10–12 s and the conductor builds a **MAC-keyed roster** (`roster` command).
  Sync hot path unchanged (still `LOCKED gaps` flat after the rework).
- **Protocol foundation Half 2** (v7 position, v8 ID, v9 group, and v10
  LED-profile paths hardware-verified on the current bench): **conductor-authoritative
  `MAC→permanent ID + optional (x,y) + group + LED count` inventory** in NVS, broadcast as
  chunked `MSG_TABLE`; a node finds its row, adopts its number, placement, and
  hardware profile, and caches them (survives reboot, no laptop needed). Conductor edits it
  with `assign <mac> <x> <y>` / `group <mac> <1..8>` /
  `leds <mac> <16|32|64>` / `table` /
  `forget <mac>`.
  Verified: a node took a position set only on the conductor, no serial to it.
  Re-adoption hardware check 2026-07-06: performer #2 was erased/reflashed after
  its table row existed; it registered as `#?` but re-adopted `(0.6115, 0.4646)`
  from the conductor's single-row table reply within the normal roster window.
  Protocol v8 extends that same reply to restore `#2` automatically.
- **GPIO2 heartbeat** blinks on the synced beat (zero-wiring sync check).
- **Serial commands:** `info`, `roster` / `table` / `assign` / `group` / `leds` / `forget`
  (conductor), `role conductor|performer|relay`, `id <n>`, `pos <x> <y>`,
  `pattern <n>`, `bri <n>`, `param <i> <v>`, `powersave on|off`,
  `dusk on|off` (performer; daytime deep-sleep, default off),
  `wake on|off` (conductor; FIELD_AWAKE beacon flag, summons dusk-sleeping
  nodes; same force-awake bit as the Operations override),
  `power` / `power reset` (INA228 nodes; print / zero the energy
  accumulators). Note diag output is gated: it prints only within **5 min of
  serial input** — hit Enter in a monitor to revive a quiet node (see
  FLASHING.md). Exception: the conductor's `[power]` telemetry log is
  deliberately ungated (it's the overnight audit trail).
- **Application protocol and stable transport are both v11** (`PROTO_VERSION 11`,
  `TRANSPORT_VERSION 11`); the common header preserves
  logical origin/destination across at most one relay hop; REGISTER reports role
  and the primary retains each node's immediate next hop. `MSG_TABLE` includes permanent
  board ID, optional-position flags, group ID, and 16/32/64 LED count; BEACON includes eight group
  `PatternConfig`s and runtime `PowerPolicy` with UTC epoch seconds. Six bytes
  from the retired v7 keepalive experiment remain reserved to preserve the v10
  layout; REGISTER includes board/group/LED-count/role identity
  plus release version, protocol, build id, and dirty flag
  for OTA version consistency; OTA begin/chunk/end messages carry
  the staged firmware image during manual maintenance updates).
  Header validation is deliberately tied only to `TRANSPORT_VERSION`; REGISTER
  separately reports `PROTO_VERSION`. This keeps the routed OTA migration plane
  byte-stable so existing v11 relays can carry future application revisions and
  a staged field can activate performers, relays, then the primary without USB
  reflashing. The initial v10→v11 transition still requires the one-time station
  migration because v10 predates the routed header. A same-protocol stale
  version/build is reported as `Firmware mismatch`.
- **Host unit tests** (`test/test_logic/`) and control tests: sync
  core, pattern math, roster, layout table, radio duty-cycle, nap scheduler (Stage B), dusk detector +
  fail-awake gates (Lever 2), pattern static-ids + boot-guard, glow warm-hue
  color, power telemetry (conversions / plausibility gate / report scheduler),
  MAC text parsing, table wire (chunking / length validation / own-row scan /
  row-reply decision + builder), firmware version consistency, OTA CRC/hex
  parsing, power-policy schedule math, and boot classification.

**Hardware-verified (2026-06-28) — Milestone 3, Lever 1, Stage A (performer radio
duty-cycle):** a performer powers the radio **down** between brief listen windows
and keeps rendering from the synced clock, attacking the RX-dominated night draw.
Logic is the dependency-free `include/powersave.h` (5 host tests): a state machine
that holds the radio ON to acquire the first beacon, then cycles `DUTY_LISTEN_US`
ON (600 ms, spans ~2 of the 4 Hz beacons) / `DUTY_OFF_US` OFF (4 s) — ~13% radio
duty. `main.cpp` does the teardown/bring-up (`radioSleep`/`radioWake`:
`esp_wifi_stop`/`start`, re-adding the broadcast peer + recv-cb and re-flagging the
conductor unicast peer on each wake), gates TX on the radio being up, and feeds
caught-beacon events back to the scheduler. Conductor is exempt (gated on
`role == performer`). Toggle live with `powersave on|off` (persisted, NVS key
`ps`; default ON). The `[duty]` diag line reports `radio=ON/off` + `windows`/
`missed`.

**Bench result (conductor + duty-cycling performer, both DevKitC on USB):** radio
cycles ~0.6 s ON / ~4 s OFF as designed; **0% missed windows in steady state**
(every window catches a beacon and re-locks); the node free-runs the render across
each OFF with no blackout; wake reliably rebuilds the peer table (rx climbs across
sleeps); and the performer still appears in the conductor's `roster` (~10 s
cadence, mildly stretched because TX only happens during a window). The only
misses seen were transient, while the conductor itself was being reset during
setup. Note `gaps` increments ~once per wake (the first beacon after a 4 s sleep
has skipped seq numbers) — that's expected with duty-cycling and benign; the
`missed` counter is the meaningful health metric now, not `gaps`.

**SOLID boot-guard (same change):** a node never *boots* into `SOLID` (pattern 3,
full-white worst case) — `patternConfigLoad` falls a persisted SOLID back to
`SWEEP`, so a power-cycle can't leave a node draining the battery on all four
channels. `pattern 3` still works live for a deliberate on-bench measurement.

**Power result — MEASURED (2026-06-28, 12 V battery-side DMM, one DevKitC performer
locked to a beaconing conductor, steady-amber `GLOW` @ bri 48):**
- Radio **off** (rest, ~87% of the cycle): **51 mA @ 12 V**; radio **on** (the
  ~600 ms listen window, ~13%): **85 mA**. powersave-on **average ≈ 55 mA (~0.74 W
  @ 13.4 V)**; powersave-off pins the radio at the **85 mA** level (~1.14 W).
- Radio RX term = 85 − 51 ≈ **34 mA**; duty-cycling pays it only ~13% of the time
  → **saves ~30 mA @ 12 V (~0.4 W), ~35% of node draw.** The always-on 85 mA
  matches the original go/no-go's ~83 mA baseline (rigs agree).
- **Battery life (138 Wh, 10 h/night): ~12 → ~19 nights (~1.5×);** 24/7 calendar
  ~5 → ~7.8 days.
- **Why ~1.5× and not more:** the saving is a *fixed* ~30 mA radio term, but the
  ~51 mA rest floor (LEDs + CPU) now dominates, so the duty-cycle % scales inversely
  with LED load — a bigger win on dim shows. **Radio is no longer the dominant
  term;** the next levers are LED brightness, Stage B (CPU light-sleep to cut the
  floor), and Lever 2 (daytime deep-sleep).
- A *dim/5 V-side* sanity run under pulsing-white earlier showed the radio blip
  clearly (off-floor 0.05 A vs on 0.09–0.17 A); consistent with the above.

Measurement gotcha confirmed: do the battery reading with **USB disconnected**
(USB backfeeds 5 V into the DevKit and corrupts the 12 V draw) — powersave persists
in NVS, so set the mode over serial, then unplug and read. And never leave a USB
power meter inline on the data path (corrupts the UART / browns out radio init —
cost us a session; see FLASHING.md).

**A new show pattern landed alongside this:** `GLOW` (`pattern 4`) — a steady solid
color at a fixed hue, no time term, so the field holds one calm color with a *flat*
(non-pulsing) draw. `params[0]` = hue degrees (30 orange / 40 amber / 50 yellow),
`params[1]` = saturation %. Used as the realistic-conservative power-test scene; also
a genuine warm/gentle show pattern. Host test covers the warm-hue color math (39
tests now).

**INA228 power telemetry — CODE-COMPLETE 2026-07-04, host-tested,
both device envs build — NOT hardware-verified (chip arrives with
Monday's order).** ARCHITECTURE §4.3. Reviewed same day (8-angle /code-review,
10 verified candidates → 4 fixes applied: avg-W plausibility bound, time-gate
before the spinlock in maybePowerReport, PowerSample embedded in PowerMsg,
conductor self-log on the tested scheduler). What landed:
- `include/powermon.h` (pure, 7 host tests): J→Wh / C→mAh / avg-W conversions,
  a plausibility gate (NaN/inf/orders-of-magnitude nonsense gets logged but
  flagged `** IMPLAUSIBLE`, never trusted into the budget — including the
  reboot-inflated avg-W case: accumulator survives an ESP32 reset while the
  elapsed anchor restarts, so a whole night's Joules over seconds would read
  as kW; the gate bounds avg at 50 W), and the report scheduler (fires only
  while the radio is up + conductor peer exists, catches up with exactly ONE
  report after a long radio-off span — no bursts).
- Wire: `MSG_POWER` unicast performer→conductor on the existing REGISTER path;
  the payload IS the embedded `PowerSample` struct (one field list, sender and
  receiver can't drift; byte-identical layout — all members 4-byte aligned).
  **No PROTO_VERSION bump** — new type only; v2 receivers without the handler
  ignore it via the dispatch default. The peer-add logic is now shared
  (`conductorPeerReady`).
- Glue (`main.cpp`): I2C probe at boot (`Wire` on SDA 21 / SCL 22; a node
  without the chip fails the probe in ~ms and stays silent — one image
  everywhere). **`begin(..., skipReset=true)` is load-bearing:** the chip stays
  battery-powered across an ESP32 reset, and the lib's default begin()
  hardware-resets it — which would wipe the night's Wh the moment a serial
  monitor's DTR auto-reset hits. Zeroing is only ever explicit (`power reset`).
  Continuous conversion mode is set explicitly (triggered mode invalidates the
  hardware accumulators). Conductor drains reports from a spinlocked queue and
  logs `[power] <mac> E=… Wh avg=… W Q=… mAh V=… I=… (elapsed)` — ungated by
  the serial-activity window (it IS the overnight audit log). A conductor
  carrying the chip logs its own line on the same 60 s cadence.
- Caveat (documented in the code): `elapsed_s` restarts on reboot while the
  chip keeps accumulating, so after an unplanned mid-night reboot the energy
  total is still right but avg-W overstates until the next `power reset`.
- Overnight flow: `power reset` at dusk → run untethered → morning: reconnect
  USB and read with the **no-reset pyserial trick** (FLASHING.md) *or* just
  read the conductor's scrollback; a DTR reset no longer zeroes the chip, only
  the elapsed anchor.
- Control plane (2026-07-06): `/api/state` includes `power_monitor` summary from
  sparse reference-node samples. Operators can configure battery capacity
  (default 384 Wh) and the full-voltage threshold (default 14.4 V), see
  average draw per metered performer and top-line SOC in Power, and click **Sync to 100%** per
  metered node after charging to anchor that node's current Wh as full.

**INA228 bench checklist (Monday, chip in hand):** (1) wire VCC→3V3, GND→GND,
SDA→21, SCL→22, shunt in series battery+ → buck input; (2) boot log shows
`[power] INA228 found`; `info` shows `ina228=yes`; (3) `power` prints a sane
line (V≈13.4, I≈55 mA on the GLOW scene); (4) cross-check E against the DMM/
ET900 over ~30 min; (5) confirm a `[power]` report lands in the conductor log
~every 60 s with powersave on (deferred through radio-off spans); (6) DTR-reset
the node mid-run and confirm E survives (only elapsed resets); (7) `power
reset` then verify E climbs from 0.

**Code layout:** `include/` — `config.h` (pins/constants), `beacon.h` (wire
packets), `sync.h` (clock core, tested), `pattern_math.h` (pure pattern fns,
tested), `patterns.h` (LED binding), `roster.h` + `table.h` (pure, tested),
`relay.h` (one-hop authority, parent, validation, and queue logic, pure and tested),
`table_wire.h` (table chunking / validation / broadcast cadence, pure, tested),
`powersave.h` (radio duty-cycle schedule, pure, tested), `powermon.h` (INA228
telemetry logic, pure, tested), `macaddr.h` (MAC text parse/format, pure,
tested), `bootplan.h` (Lever-2 boot classification, pure, tested), `identity.h`
(NodeIdentity). `src/main.cpp`
is the on-device glue. NVS namespace is `"node"` (keys: `id`, `x`, `y`, `role`,
`pat`/`bri`/`p0`..`p3` for the pattern, `table` blob on the conductor).

**Not built or not verified yet:** physical Pi/Starlink/Cloudflare rollout,
auto-calibration field proof, show program / scheduling, real identify blink
over ESP-NOW, and any larger-fleet OTA reliability mechanism shown necessary by
scale testing. Pi packaging, authenticated remote administration, detached OTA,
structured machine serial, and the dev laptop UI/API are built. The manual OTA
transfer path remains bench-verified; remote browser-disconnect and Pi-restart
drills remain phase-4 work. INA228 telemetry firmware is built but still awaits
the physical chip.

## Hardware state

- **As of 2026-08-10 (local control plane on `http://127.0.0.1:8000`):** ten
  USB-connected performers, permanent IDs 1–10, and the USB-attached conductor
  all run `0.9.3-dev`, protocol 11, clean build `b1e7d434`. Serial readback
  verified every role and ID after flashing the conductor last. Live state
  reports all 10 performers alive and firmware-consistent (`matching=10`,
  `seen=10`). Automatic OTA is disabled; the interrupted prior install remains
  paused while the patterns are tested.
- **As of 2026-08-04 (live HTTPS API on `https://127.0.0.1:8000`):**
  - `/dev/cu.usbserial-0001`, `8C:94:DF:8F:71:50` — conductor, protocol 10,
    build `143a14a8` during the verified WIP flash.
  - `/dev/cu.wchusbserial110`, `68:FE:71:A6:32:4C` — performer #23, Group 1,
    16 LEDs, White at brightness 24.
  - `/dev/cu.wchusbserial1220`, `68:FE:71:A6:30:84` — performer #24, Group 2,
    64 LEDs, Fire Flicker at brightness 24.
  - Both performers were alive, locked, and reported protocol 10 / build
    `143a14a8` after the reversible-blackout reboot test. The macOS automatic
    flasher LaunchAgent remains uninstalled so it cannot contend for these
    serial ports during bench work.
- **Historical 2026-07 bench:** 3× DOIT ESP32 DevKit V1 on the unified image;
  rings on 2 of them. LED data
  on **GPIO13 (`D13`)**, USB 5V (no 12 V / buck yet).
- **As of 2026-07-06 (live API checked on `http://127.0.0.1:8001` after recovery hardening):**
  - `/dev/cu.usbserial-7`, `8C:94:DF:57:7F:14` — **CONDUCTOR**, serial-backed
    API server currently attached here.
  - `8C:94:DF:8F:71:50` — performer, label `#1`, positioned at approximately
    `(0.2249, 0.7570)`.
  - `30:76:F5:93:67:3C` — performer, label `#2`, positioned at approximately
    `(0.8076, 0.4122)`.
  - Current live state when this doc was updated after the successful
    mixed-firmware recovery run: `summary.alive=2`,
    `summary.total=2`, `attention=0`, firmware `version=0.3.0`,
    `build_label=ed2e397f`, `dirty=true`, `summary.firmware.consistent=true`
    with `2 / 2` performers matching, and `recovery.status=ready`. Performer #1
    was restored to `powersave on` after the OTA debugging run. The staged OTA
    artifact is persisted in `.control_ota/` and survives API restarts.
- Uploads were unstable at the previous high serial rate after long OTA
  sessions, so `platformio.ini` currently sets `upload_speed = 115200`. It is
  slow but reliable on the bench.
- Port names still shuffle because all boards report the same USB serial —
  re-check each board with `info` rather than trusting labels.
- **Gotcha:** factory boards ship with ESP-AT firmware and need a one-time
  `esptool.py --port <P> erase_flash` before our image runs right. Boards report
  the same USB serial, so port names shuffle on replug — flash by current port.
  Full details in `FLASHING.md`.

**Power — battery go/no-go MEASURED (2026-06-28): GO (nighttime).** Wired the real
battery → buck → node chain (12 Ah LiFePO4 at 13.43 V → UCTRONICS 9–36V→5V buck →
DevKit performer + pulsing ring) and measured the **12V input** current with a DMM
in series. **Loaded draw ~83 mA → ~1.11 W total, converter included**; buck idle
(no board) 8.7 mA ≈ 0.12 W; **converter efficiency ~77%** (load = 0.855 W full-hour
ET900 integral ÷ 1.11 W input; UCTRONICS is fine — keep it). **~11 Wh/night →
138 Wh ÷ 1.11 W ≈ ~12 nights** at 10 h/night, clears the 10-night target. The
5V-side ET900 reading was the *load only* (0.855 W); this 12V number is
authoritative. Caveats: (1) **calendar life still needs
daytime deep-sleep** — at 24/7 it's ~5 days, under a 10-night event. The photodiode
sets the duty cycle: at BRC (BM 2026 = Aug 30–Sep 7, ~40.8 °N) darkness is ~11 h
sunset→sunrise, so a dusk-tripped LDR runs **~10–10.5 h on / ~13.5 h asleep** —
the 10 h/night assumption holds; use 10.5 h for the post-M3 recompute (math in the
`power-budget-go-no-go` memory); (2) radio
likely dominates the draw — **modem-sleep is ineffective in connectionless ESP-NOW**
(`WIFI_PS_MIN_MODEM` set, CPU 160 MHz, but no AP/DTIM so RX stays on); the real
lever is **scheduled light-sleep between beacons** (synced clock enables it), the
Milestone-3 power item; (3) **FireBeetle** would draw less still. Full math in the
`power-budget-go-no-go` memory.

**Worst-case measured (2026-06-28, ET900 @ 5V):** `SOLID` (`pattern 3`) at
`bri 255` — every pixel full RGBW white — drew **0.76 A @ 5V = 3.8 W → ~4.6 W @ 12V
→ ~3 nights sustained** (~4× the colored show, since white lights all 4 channels).
**Battery GO holds** — even pathological all-white never fails in one night, and
0.76 A is well inside the buck/USB limits (so the cap is policy, not safety).
Decision: **`MAX_BRIGHTNESS = 192`** keeps worst case ~3.8 nights while barely
dimming real shows. Watts are fine; the gating issue is *hours* → daytime sleep.

## Quick reference

```bash
export PATH="/opt/homebrew/bin:$PATH"
pio test -e native                                  # 206 host tests
pio run -e devkitc                                  # build
pio run -e devkitc -t upload --upload-port /dev/cu.usbserial-XXXX
pio device monitor -p /dev/cu.usbserial-XXXX        # provision + watch
```
Reading serial without resetting the board: see the pyserial snippet in
`FLASHING.md` (opening the port auto-resets; wait ~2 s before typing commands).
Raspberry Pi shell access and recovery: see `SSH_ACCESS.md`; the current LAN
command is `ssh baskets@baskets.local`.

---

**⚠ `main.cpp` global-ordering pitfall (for future edits):** NVS helpers must be
defined *below* the global they touch. `patternConfigLoad/Save` sit *after*
`g_beacon`; `tableLoad/Save` need `g_table`, which is declared up top with the
other config globals for exactly this reason. Don't move these loads into
`configLoad()` or forward-declare the globals — a prior attempt broke the build.

## Pilot batch: ORDERED 2026-07-03 (receipts in `receipts/`)

The **`docs/BOM.md` → "Pilot batch (5–7 units)"** order went out 2026-07-03,
across three carts (~$732 incl. tax/shipping):
- **DFRobot** — 6× FireBeetle 2 ESP32-E, $57 (invoice `366209`).
- **Adafruit** — 4× SK6812 **RGBW** rings (PID 2855, Natural White) + **2×
  INA228** total across two invoices (`3705934`, `3705946`). The first invoice
  was a mis-order (**RGB** PID 1463 rings) corrected 33 min later — ⚠ **confirm
  the RGB order was cancelled/refunded.** 2 INA228s is fine (plan is 1–2
  instrumented reference nodes).
- **Amazon** (`111-8959596-4536221`, $600.40) — 5× TalentCell LF120A1 batteries
  (arriving Jul 10), 3× buck 2-packs, CanaKit Pi 3 B+ kit, preloaded Pi OS SD
  card, 74AHCT125 10-pack, perfboard 30-pack, PT334-6C phototransistors,
  resistor kit, 1000 µF caps, toggle switches, plus beyond-BOM extras: 5× IP65
  junction boxes, grommet kit, silicone sealant, and a 150 A inline power
  analyzer. Most of it arriving Mon Jul 6.

**Follow-ups from the receipts:**
- ⚠ **The battery line is on Subscribe & Save ("every 2 weeks") — cancel the
  subscription after the first delivery** or it re-ships 5 batteries (~$220)
  every two weeks.
- Ordered **5 batteries / 4 RGBW rings**, not 6 — the 6th node is covered by
  already-owned bench hardware (2 rings are mounted on the DevKitC boards).
- **Not ordered anywhere: JST-SM connector kit and fuse holders + fuses** (BOM
  pilot rows 10–11) — add to a future cart before field wiring.

## Milestone 3 detail — power management (Stage A measured, Stage B verified, Lever 2 awaiting sensors; next-session order is at the top of this doc)

**Lever 1 Stage A (performer radio duty-cycle) is done, hardware-verified, measured,
and pushed** (`main` @ `5089d33`) — see the bench result near the top. It cut node
draw ~35% (85 → ~55 mA @ 12 V, ~12 → ~19 nights). **The radio is no longer the
dominant term.**

**Power budget now breaks into three roughly co-equal terms** (measured @ 5 V on a
DevKitC): **CPU + board ~50 mA** (incl. the DevKit's power LED + CP2102 USB chip),
**LEDs ~50 mA** (amber `GLOW` @ bri 48), **radio RX ~70 mA when on** (now paid only
~13% of the time). So the remaining levers, roughly in impact order:
1. **CPU floor** — the largest *constant* draw now. Attacked by Stage B and, more
   powerfully, by **scheduled-wake + deep-sleep for static scenes** (next section).
2. **LED brightness** — pure policy knob.
3. **Daytime deep-sleep (Lever 2)** — the calendar-life fix (24/7 is still ~5 days).

⏳ **Quick measurement still owed:** the CPU floor above was read *through USB*, which
includes CP2102 + power-LED overhead absent on battery. Re-measure `bri 0` rest on
the **12 V battery rig, USB disconnected** for the true MCU floor before sizing the
sleep work. (FireBeetle, the M4 candidate, has a lower quiescent draw and shrinks
this floor further.)

**INA228 precision power monitor: firmware/API/UI DONE (2026-07-06)** (see the
"INA228 power telemetry" section above, `PROJECT_BRIEF.md` readout-path
section, and `ARCHITECTURE.md` §4.3) — an I2C breakout with hardware
energy/charge accumulation, wired in series between battery+ and the buck
input on 1–2 reference nodes. It replaces one-off ET900/DMM snapshots with a
true continuous Wh integral per night, and instrumented performers report
accumulated Wh to the conductor over ESP-NOW (`MSG_POWER`) so every overnight
sync test doubles as a fleet-wide power audit. The Power UI rolls those sparse
samples into average performer draw and SOC using the configured battery
capacity, with auto/manual full-charge anchoring. Awaiting the physical chip
(Monday's order) for hardware verification — the bench checklist is in the
section above.

### Lever 1 (do first): radio off between beacons — performer-only

**Why it works:** a performer free-runs `f(x,y,t)` from the synced clock, so it does
*not* need continuous RX — only periodic beacons for clock-drift correction and
pattern/table updates. So turn the radio **off** most of the time and wake it briefly
to resync. This attacks the dominant RX term (modem-sleep can't, see power note).
**Conductor is exempt** — it must beacon every 250 ms (TX), and is typically
wall-powered; gate all of this on `role == performer`.

**Staged plan:**
- **Stage A — radio duty-cycle, CPU stays on. ✅ DONE + host-tested +
  hardware-verified + measured + pushed.** `include/powersave.h` + glue in
  `main.cpp`; `powersave on|off` toggles it live (NVS `ps`, default on); `[duty]`
  diag. Implementation notes preserved in §8.1 / the commit. The one thing *not*
  worth doing: widening `DUTY_OFF_US` (4 s → 8 s/60 s) — at 13% duty we already
  removed ~30 of the ~34 mA radio term, so a 60 s wake saves only ~4 mA more while
  multiplying pattern-update latency. Diminishing returns; leave it at 4 s.
- **Stage B — cut the CPU floor (now the biggest constant term). ✅ HARDWARE-VERIFIED
  (2026-07-03 bench, conductor + 2 performers, all on protocol v2): naps run
  ~2/s on GLOW (heartbeat-capped ~0.5 s each), measured slept time tracks wall
  clock (~87–93% of each radio-off span asleep) — `esp_timer` IS compensated
  across light sleep, the known risk is retired; sync stays `LOCKED` with
  `missed=0` across hundreds of naps; UART wake + serial grace + diag gating
  all behave as designed. One glue fix found on the bench: light sleep releases
  the UART0 TX pad and sprayed a junk byte per transition at the host —
  `gpio_hold_en(GPIO_NUM_1)` across the sleep eliminates it (verified 0 junk
  bytes post-fix). Power re-measure on the 12 V rig still owed. ⚠ Watch item:
  the conductor board (8C:94:DF:57:7F:14, the one that needed today's
  `erase_flash` for the FLASHING.md clock-scramble) once flooded serial with
  diag lines at full baud with `seq` racing (time-gated timers firing every
  loop) — cleared by reset, not reproducible after retrying the same command
  sequence; if it recurs, suspect this board's hardware first.** What landed: `include/napsched.h`
  (pure `napPlan()` — how long may the CPU light-sleep right now; 9 host tests)
  + `include/pattern_ids.h` (PatternId enum extracted from Arduino-bound
  patterns.h so `patternIsStatic` is host-reachable) + glue in `main.cpp`
  (replaces the `delay(16)` loop tail). Behavior: naps happen only on a
  performer with `powersave on` and **only while the radio is already off**
  (never into a listen window); a nap ends at the earliest of the next radio
  duty transition, the next ~30 fps frame (animated patterns only — GLOW/SOLID
  and uploaded programs without a time input skip this and sleep clear through),
  the next heartbeat edge (keeps the GPIO2
  sync blink square), or a 1 s safety cap. Serial safety: any serial byte holds
  naps off for 30 s, a UART wakeup (typing at a sleeping node — hit Enter once,
  then type) does the same, and boot seeds the grace so a fresh flash has a
  provisioning window. Glue details: waits for the RMT LED transfer +
  `Serial.flush()` before sleeping (sleep truncates both), knobs in config.h
  (`NAP_*`, `SERIAL_NAP_GRACE_US`).
  **Bench checklist for the next hardware session:** (1) `[nap]` diag —
  `slept` should track wall time (measured via esp_timer delta; **slept≈0 with a
  climbing nap count means esp_timer is NOT compensated across light sleep**,
  the known Stage-B risk — fix would be adding slept RTC time back); (2) sync
  must stay `LOCKED`, `missed=0`, offset stable across naps; (3) heartbeat still
  square @ 1 Hz; (4) serial: confirm Enter-then-type works on a napping node;
  (5) re-measure the 12 V USB-disconnected draw on GLOW — expect the ~50 mA
  CPU floor to drop meaningfully; compare `powersave on` vs `off`.
  Original design notes (kept for context) — two flavors, gated on whether the
  current scene is animated:
  - **Animated patterns (pulse/sweep/drift):** CPU must re-render ~20–30 Hz, so the
    play is **light-sleep between rendered frames**. SK6812 latch their last color,
    so LEDs hold during the nap. Harder part: verify whether `esp_timer`/systimer
    advances across `esp_light_sleep_start()` — if not, add the slept RTC duration
    back or synced time drifts.
  - **Static scenes (`GLOW` and any constant `f`):** the LEDs hold with the MCU
    fully asleep, so the node can **deep/light-sleep for the whole inter-wake
    interval** — attacking the radio *and* CPU floor at once, down toward LED-only
    draw. This is the **"scheduled-wake" protocol idea** (below) and is the single
    biggest remaining win for calm shows.

**Scheduled-wake + deep-sleep protocol (design thread, not yet built):** because
every node shares the synced clock, instead of each performer waking on its own
~4.6 s timer, have them **all wake at a shared wall-clock boundary** (e.g. each
synced-time minute), the conductor **bursts the current pattern during that window**,
nodes catch it and sleep again. No "subscribe" handshake is needed — ESP-NOW is
broadcast and the shared clock *is* the coordination. The real prize isn't more
radio savings (Stage A already got those) but that a known next-wake time lets a
node **deep-sleep between windows on static scenes** (LEDs latched), and it makes
Lever 2's deep-sleep/rejoin coherent. Costs to design around: pattern/show updates
land up to one wake-interval late (fine for a programmed show, painful for live
tuning), and each window gets more critical (miss → longer free-run + delayed
update; mitigate with a longer burst + the conductor repeating it).

**Drift budget (still holds):** free-running tens of seconds on the last offset is
<~5 ms relative drift (crystals ~tens of ppm) — invisible on the slow patterns, so
minute-scale wake intervals are fine for *timekeeping*; the limiter is update
latency, not sync.

### Lever 2 (then): schedule-driven deep-sleep — calendar life

**Primary path as of 2026-07-05:** the conductor broadcasts runtime
`PowerPolicy` in every v6 beacon. Operations sets the radio/light-sleep check
interval, deep-sleep check interval, LED-on window, and force-awake override.
Performers clear LEDs and deep-sleep outside the window, then wake on the
configured check cadence to hear whether the schedule/override changed. This
makes the photodiodes redundant for the main installation path.

Operations now has one-click **Sleep field**, **Wake field**, and **Follow
schedule** controls. Forced sleep preserves the saved LED window; forced wake summons sleeping performers on their
next configured check; Follow schedule clears both overrides. Serial equivalents
on the conductor are `sleep on|off` and `wake on|off`.

**Field incident 2026-07-16 — timer wake could miss recovery:** the INA228 node
did not return after **Wake field**. Conductor-only diagnosis set the sticky wake
override and observed zero registrations across multiple one-minute sleep checks.
Root cause: a timer wake restored the prior off/sleep policy from RTC memory, and
the schedule path could immediately deep-sleep again before receiving a fresh
beacon. Timer wakes now enforce a bounded 10-second rendezvous: the radio remains
in initial acquisition until a beacon arrives, and schedule/forced sleep cannot
re-enter deep sleep before that beacon or the rendezvous deadline. Regression
coverage is in `test_timer_wake_rendezvous_blocks_sleep_until_beacon_or_deadline`.
Nodes already stranded on the affected firmware need one physical battery power
cycle (USB is not required) while the conductor's wake override is on; once they
register, update the field firmware before relying on remote sleep/wake again.

The photodiode dusk detector below remains off by default as an optional
fallback/experiment, not the plan of record.

**🛠 CODE-COMPLETE (2026-07-03), host-tested (57 native tests green), both device
envs build — NOT hardware-verified (needs the pilot phototransistors, arriving);
DEFAULT OFF (`dusk on|off`, NVS `dusk`) because GPIO34 floats until the sensor
is wired.** What landed: `include/dusk.h` (pure debounced day/night detector
with hysteresis + the deep-sleep gate; 9 host tests) + glue in `main.cpp` +
config knobs (`DUSK_*`; thresholds are placeholders to bench-calibrate).

**Design principle: FAIL AWAKE** — every ambiguous case resolves to staying
awake; the only possible failure mode is battery drain, never an unreachable
field. Four independent layers guarantee daytime testability:
1. **`wake on|off` (conductor, NVS-sticky)** sets `BEACON_FLAG_FIELD_AWAKE` in
   every beacon. A dusk-sleeping node wakes every **15 min** (`DUSK_RESAMPLE_US`)
   and listens for a beacon before it may re-sleep — a flagged beacon pins it
   awake (60 s TTL, continuously refreshed). Summon latency for the whole field:
   ≤ one resample interval. Historical note: this originally grew the wire format
   to `PROTO_VERSION 2`; the current protocol is **v10**, and every protocol bump
   still means reflashing every board together.
2. **Any power-cycle boots awake** (cold boot starts in "night", won't dusk-sleep
   for 10 min, 60 s light debounce on top). Per-lantern physical override via the
   battery toggle switch — no firmware bug can remove it.
3. **`dusk` is default OFF** — nothing sleeps until deliberately enabled after
   the pilot proves it (INA228 watching).
4. **Fail-awake invariants:** deep sleep is only entered via `duskEnterDeepSleep()`
   which arms the RTC wake timer atomically (`esp_deep_sleep`); implausible light
   readings (outside `[20, 3100] mV` — floating/broken sensor) read as *night*;
   serial traffic holds sleep off 5 min; all host-tested.

Mechanics: 1 Hz ADC sampling; flip day↔night only after 60 s continuously past
the far hysteresis threshold (clouds/headlights/flashlights reset the stretch);
timer wakes start in "day" via an RTC-memory flag and re-sleep in ~10 s if still
bright; dusk arrival flips to night and the show starts. `[dusk]` diag line +
light/vbat in `info` (VBAT divider read landed too — reported only, no cutoff
policy yet). Daytime cost at 15-min cadence ≈ ~1% duty → ~0.15 Wh/day, noise.
**Bench checklist:** calibrate `DUSK_DAY_MV`/`DUSK_NIGHT_MV` (+ polarity
`DUSK_DAY_ABOVE`) against the real divider; verify deep-sleep current (~10 µA
class); verify a full sleep→timer-wake→re-sleep cycle and `wake on` summoning;
verify a cold boot never sleeps for 10 min.

Original design notes: fixes the 24/7 ~5-day problem by sleeping through daylight. **Light sensor on
`PIN_LDR` = GPIO34 (ADC1 — ADC2 dies with the radio, already reserved in config.h).**
User leans toward a **photodiode/phototransistor** (faster than an LDR; an LDR in a
divider also works for a coarse threshold — pick by what's on hand). Below a light
threshold for a debounce → `esp_deep_sleep` (~10 µA), waking on an RTC timer to
re-sample (e.g. every ~30 min) or sleeping a fixed span until expected dusk. Add the
**battery ADC on `PIN_VBAT` = GPIO35** (divider) to report voltage + low-batt cutoff.

### Self code-review (2026-07-03) — fixes landed + known debt

A full-repo adversarial review (8 finder angles) ran after Stage B + Lever 2.
**Fixed + tested + committed:** (1) the stale-RTC-day trap — a dusk node
timer-waking after sunset re-slept every 15 min all night; `duskShouldSleep`
now refuses while live samples disagree with the day state (`d.cand != d.day`,
host-tested); (2) re-issued `powersave on` during a radio-off span stranded the
radio off permanently (now radioWake()s first; espnowStart also tolerates
double-init); (3) serial `pattern`/`bri`/`param` raced the recv callback's
`g_beacon` overwrite (now mutate+snapshot under `g_sync_mux`, NVS-save from the
snapshot); (4) role switches left a stale duty schedule (role command now
re-inits the duty machine); (5) `missed_windows` overcounted (beacon credit now
runs before `dutyStep` in loop()); (6) static patterns no longer re-render at
60 Hz (pattern-change detection + 1 Hz safety refresh); (7) diag prints only
within 5 min of serial activity — **hit Enter on a monitor to revive a quiet
node's diag** (headless nodes no longer burn ~13 ms/s of UART drain).

**Debt PAID 2026-07-04 (the review-debt session):**
- ✅ **Field build env** `field` (one canonical ESP32 profile +
  `-D HEARTBEAT_LED=0`; see "Build envs" near the top and
  FLASHING.md's env table).
- ✅ **Inventory rebroadcast stretched** 5 s → 60 s steady-state backstop
  (`TABLE_INTERVAL_US`); targeted delivery is a **single-row MSG_TABLE reply**
  to any REGISTER from a node that is new to the roster, unprovisioned, or in
  conflict with the conductor's permanent ID or group. It is sent while that
  node's radio is provably up, retried by its next REGISTER, and costs zero
  targeted traffic in steady state. `assign` and `group` still broadcast the
  full inventory immediately.
- ✅ **Host-unreachable logic extracted + tested** (83 total):
  `parseMac`/`macStr` → `macaddr.h` (parseMac rejects trailing garbage — a
  pasted EUI-64 must not silently truncate to its prefix MAC); `broadcastTable`
  chunk math, MSG_TABLE length validation, own-row scan, and the row-reply
  decision/builder → `table_wire.h`; the Lever-2 boot classification →
  `bootplan.h` (the seed now pre-expires `max(dusk, nap)` serial grace, so the
  old unlabeled `DUSK_SERIAL_GRACE_US > SERIAL_NAP_GRACE_US` invariant is
  *gone*, not just labeled); the SOLID boot-guard → `patternBootSafe` in
  `pattern_ids.h`.

**Same-day adversarial review of the paydown (8 finder angles + 1-vote
verify, 10 findings, 7 CONFIRMED) — all fixed the same day:** the first
cadence design (burst on new-MAC, inferred from a roster count change) had
three confirmed delivery holes — a reflashed node (known MAC, wiped NVS) never
got a burst and waited ~5-8 min for its position; a full roster (never pruned,
64 slots vs 60 nodes + swaps) silently suppressed the burst; and a burst
deferred by the 2 s rate limit could fire into the requester's radio-off gap
with no retry. The row-reply design above replaced it (simpler AND covers all
three: reply keyed on pre-upsert known-ness + id, not on count inference; no
hold-off to miss). Also fixed from the review: `parseMac` trailing-garbage
acceptance (empirically confirmed — `forget <EUI-64 paste>` would have
operated on the wrong lantern), the `role` round-trip resuming a stale table
schedule (now file-scope `g_next_table_us`, zeroed on role change), FLASHING.md
missing the field envs, and the field envs copy-pasting instead of
`extends`-ing the bench envs.

**Known debt (deliberate, not yet done):**
- Dead wire artifacts: `palette_id` unused, `MSG_ROSTER`/`MSG_ACK` unsent,
  `TableMsg.chunk/chunks` written but never read. The stable v11 migration plane
  keeps existing layouts byte-compatible; retire or supersede fields through
  additive message types in a deliberate application protocol revision. Roster
  firmware reporting is no longer dead: current REGISTER includes `fw` + release
  version + build id + dirty flag for OTA consistency checks, and BEACON includes
  runtime `PowerPolicy`.

### After Milestone 3
Milestone 4 — battery enclosure + final go/no-go on the **FireBeetle** (lower draw
than the DevKit). Then the non-power tracks still open: remote-control physical
rollout, auto-calibration (§6), and show program (§4.2). The Pi UI/control plane,
machine serial, and remote packaging are built. Milestone 5 - OTA + enclosure.

## Project memory (loaded automatically in this dir)

- `at-firmware-erase-flash` — erase new boards first; serial-port shuffle.
- `power-budget-go-no-go` — ET900 measurement plan, ~11 Wh/night target.
- `design-discussion-style` — in design mode, recommend in prose (no question widgets).
