# One-hop conductor relays

> **Status:** Reviewing
> **Created:** 2026-08-06
>
> Markers: `[ ]` idle | `[wip]` in progress | `[x]` done | `[f]` failed/blocked

## Purpose

Extend ESP-NOW coverage across the playa without creating a second authority.
The Raspberry Pi remains attached to one primary conductor. Any number of
always-on relay boards may repeat traffic for performers that cannot reach the
primary directly, while every show, inventory, power, calibration, and OTA
decision remains owned by the primary.

## Problem

The current protocol binds logical identity to the immediate ESP-NOW sender.
That works only for one radio hop:

- performers learn the most recent beacon sender as the conductor;
- the conductor rejects REGISTER and OTA status payloads whose embedded MAC is
  not the immediate sender;
- targeted OTA repair and activation packets contain no logical destination;
- the control plane activates performers in MAC order, which could reboot a
  relay before the performers that depend on it.

Blindly retransmitting packets would therefore lose origin identity, make
targeted repair impossible, and risk cutting off staged performers during
activation.

## Solution

Protocol v11 adds routing metadata to the common packed message header:

- logical origin MAC;
- logical destination MAC, using broadcast for field-wide messages;
- hop count, limited to one relay hop.

The largest existing table packet becomes exactly 250 bytes, the ESP-NOW v1
payload limit, so no table payload or chunk count changes. The calibration
roster carries 37 rather than 39 MACs per chunk. All
message types use the same routed header, keeping OTA packets inside the same
transport boundary as beacons and inventory traffic.

Roles become performer, conductor, and relay. A relay:

- accepts only a direct hop-zero primary beacon as its upstream authority;
- stays radio-on and free-runs the last show state if upstream disappears;
- processes broadcast traffic for itself and queues a hop-one copy downstream;
- forwards child-originated unicast traffic upstream without changing its
  logical origin;
- forwards primary-originated targeted traffic only to the named child;
- never forwards a hop-one packet, preventing loops and general mesh behavior.

Performers select the first valid parent transport for a primary origin and keep
it while fresh. They may fail over after a bounded stale interval, but do not
flip between overlapping direct and relayed beacons. Replies go to the selected
parent while retaining the performer as logical origin and the primary as
logical destination.

The primary records the fresh next hop learned from routed REGISTER traffic.
Targeted OTA repairs and activations go to that next hop while retaining the
performer destination. The machine state exposes each online node's role and
route. The control plane activates ordinary performers before relay nodes, then
activates the primary last. With one-hop routing, that is the complete dependency
order. For a relayed activation, the relay sends a delivery receipt only after
its queued downstream copies finish; the primary does not acknowledge the serial
activation command before that end-to-end boundary is proven.

Relay forwarding is queued outside the ESP-NOW receive callback. Queueing,
parent selection, route learning, hop validation, and relay activation ordering
live in dependency-free modules with native tests. Existing OTA offset, CRC,
checkpoint, retry-window, and independent-completion behavior remains unchanged.

## Scope

In scope:

- one authoritative primary and zero or more one-hop relays;
- runtime `role relay` provisioning in the common firmware image;
- routed transport for every current protocol-v10 message type;
- direct and relayed performer coexistence;
- sticky parent selection with stale failover;
- relay route diagnostics in serial machine state;
- performer-before-relay activation order;
- host tests, control tests, device builds, and operator documentation.

Out of scope:

- multi-hop mesh routing;
- relay election or conductor promotion;
- dynamic channel selection;
- ESP-NOW encryption or new application authentication;
- changing the OTA staging, repair, CRC, or retry policy;
- automatic RF placement or antenna selection.

## Relevant files

- `include/beacon.h`: common routed wire header and protocol-v11 layouts.
- `include/relay.h`: pure parent, route, forwarding, and queue decisions.
- `include/roster.h`: role and next-hop observations retained by the primary.
- `src/main.cpp`: NVS role, ESP-NOW glue, forwarding queue, and diagnostics.
- `control/app.py`: relay-safe activation ordering.
- `control/tests/test_api.py`: rolling activation behavior.
- `test/test_logic/test_main.cpp`: native routing and wire-layout coverage.
- `docs/ARCHITECTURE.md`, `docs/HANDOFF.md`, `README.md`: stable design and
  operator workflow.

## Acceptance criteria

- A direct performer and a performer behind a relay follow the same primary
  beacon and appear under their own MACs in the primary roster.
- A performer in overlapping coverage keeps one parent while it remains fresh
  and fails over only after the parent timeout.
- A relay forwards each supported downstream and upstream message class at most
  one hop and rejects looped, wrong-origin, or wrong-destination traffic.
- Broadcast table, calibration, power, and OTA messages remain within 250 bytes.
- Targeted OTA begin/chunk/query/activate traffic uses the learned next hop while
  OTA status remains attributed to the performer.
- Automatic and explicit staged activation order is performers, then relays,
  then the primary.
- A relay losing upstream continues rendering the last synchronized show state
  and never self-promotes.
- `pio test -e native`, the control test suite, and all firmware environments
  pass.

## Proof plan

- Native tests for all parent/route state transitions, hop validation, queue
  capacity and deduplication, message sizes, and role parsing.
- Control tests with mixed performer/relay targets proving dependency order and
  preserving ordinary MAC ordering within each role class.
- Device compilation for `devkitc`, `firebeetle`, and `field`.
- Manual code-path audit for every `MsgType`, every `esp_now_send` call, and all
  transport-source identity checks.
- Hardware proof remains a post-merge field gate: primary plus one relay, one
  direct performer, one shielded/out-of-primary-range performer, show controls,
  registration, table repair, power telemetry, OTA repair, and activation.

## Release hygiene

This feature intentionally bumps only `PROTO_VERSION` to 11. It does not bump
the product version or edit the changelog. Primary and performers move to v11
in one coordinated staged migration: the v10 primary dispatches every performer
activation, then activates itself, and the control plane verifies the field
after v11 connectivity returns. New relay boards require direct USB bootstrap.

```checklist
- [x] Create `feat/conductor-relays` from fresh `origin/main` in a dedicated worktree. (verify: `git status --short --branch`)
- [x] Record the protocol-v11 architecture, scope, acceptance criteria, proof plan, and rollout seam in this living plan. (verify: plan review against the Ready rubric)
- [x] Add dependency-free routed-header, parent-selection, relay-forwarding, and primary-route logic. (verify: focused native tests)
- [x] Integrate the relay runtime role, NVS provisioning, forwarding queue, and one-hop ESP-NOW sends. (verify: device builds and message-path audit)
- [x] Route all current upstream and downstream message types without changing OTA state semantics. (verify: MsgType/send-site matrix and native tests)
- [x] Expose node role and next hop in roster/machine state and activate performers before relays. (verify: control API tests)
- [x] Update architecture, handoff, README, and protocol comments with deployment and bootstrap instructions. (verify: documentation cross-check)
- [x] Run native tests, the full control suite, and all firmware builds. (verify: 180 native tests, 446 control tests, and three device builds)
- [x] Perform the required adversarial review, fix confirmed findings, and rerun affected proof. (verify: all confirmed findings fixed; affected native/device/static proof green)
- [ ] Commit implementation, push the feature branch, and open a pull request without merging. (verify: PR URL and clean worktree)
```
