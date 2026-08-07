# Agent onboarding

Firmware for **Do Baskets Dream** — a synchronized ESP32 LED-lantern installation.
One conductor broadcasts a clock + pattern config over ESP-NOW; every performer
renders `f(x, y, t)` locally and free-runs through dropped beacons. One firmware
image runs on every node; role is a runtime NVS value.

## Read these first, in order, at the start of every new session

1. **[`docs/PROJECT_BRIEF.md`](docs/PROJECT_BRIEF.md)** —
   the original spec: what we're building and the hard constraints.
2. **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)** — the design decisions and how
   the pieces fit together (with `[done]`/`[planned]` status tags).
3. **[`docs/HANDOFF.md`](docs/HANDOFF.md)** — the "you are here" doc: a running work
   log of what's done and **the next task to pick up**. Start work from here.

After those three you'll know what exists, why it's built that way, and what to do
next. Build/flash/test commands and the read-serial-without-resetting trick are in
`docs/HANDOFF.md` (Quick reference) and `docs/FLASHING.md`.

## Pattern authoring workflow

When asked to create, compare, review, save, or broadcast show patterns through the
control plane, read **[`skills/CREATING_PATTERNS.md`](skills/CREATING_PATTERNS.md)**.
It documents the API-only loop for draft review, PNG/JSON previews, saved pattern
CRUD, automated scoring, and the guarded live broadcast step.

## Release workflow

When asked to **cut**, **publish**, or **create** a release, read
**[`docs/RELEASING.md`](docs/RELEASING.md)** and complete the entire production
release workflow as one user action. Do not stop after publishing the GitHub
release. A release is complete only after its exact immutable manifest is
promoted through `deploy/channels/production.json`, that promotion is merged,
and the production pointer is verified. The original release request authorizes
both reviewed PRs and their merges once their required checks pass; ask again
only when a failure or policy gate needs user judgment.

The Pi automatically polls the promoted production pointer, deploys the control
plane, and stages the matching firmware. With **Automatic updates** enabled (the
default), the control plane then reconciles online mismatched boards in the
background; each performer installs independently and the conductor activates
last. Before a high-visibility show, use the persistent Automatic updates switch
to pause that behavior at a safe command boundary.

## All code should be unit tested

Write tests for new logic — don't ship untested code. The project's pattern: keep
the real, silently-failing-able logic in **dependency-free headers** (no Arduino, no
ESP-NOW, no globals) — like `sync.h`, `pattern_math.h`, `roster.h`, `table.h` — so it
runs on the host, and put only the thin hardware glue in `src/main.cpp`. Add cases to
`test/test_logic/` and keep **`pio test -e native` green** (it gates every change).
When new logic lands in `main.cpp` and isn't host-reachable, that's the signal to
extract it into a pure module so it *can* be tested.

## Before uploading any firmware

**Read [`docs/FLASHING.md`](docs/FLASHING.md) first.** Flashing has non-obvious
gotchas that will waste a session if skipped — new boards need a one-time
`erase_flash`, all boards enumerate as the same USB serial so port names shuffle on
replug (flash by current port, not by which board you think it is), and you must use
a data USB cable. Don't run `pio run -t upload` until you've read it.
