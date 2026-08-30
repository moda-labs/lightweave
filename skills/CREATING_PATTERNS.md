# Creating Patterns

Use this guide when an agent is asked to author, compare, review, save, or
broadcast show patterns for Do Baskets Dream.

This is a control-plane workflow for the current compiled pattern vocabulary:
`Pulse`, `Glow`, `Sweep`, `Wavefront`, `Palette Drift`, `Firefly`, `Ocean Wave`,
`Pond Ripple`, `Fire Flicker`, and `Fire2012`. The separate `Uploaded Pattern`
option accepts bounded JSON
expressions for new `f(x,y,pixel,t)` looks without another firmware update. It
does not load native code or change compiled pattern functions; new C++ built-ins
still belong in `include/pattern_math.h` with host tests.

## Preconditions

Start from the repository root.

Read the project onboarding docs first if this is a new session:

1. `docs/PROJECT_BRIEF.md`
2. `docs/ARCHITECTURE.md`
3. `docs/HANDOFF.md`

Run or verify the control-plane server:

```bash
PYTHONPATH=. uvicorn control.app:create_app --factory --host 127.0.0.1 --port 8000
```

Use `http://127.0.0.1:8000` as the base URL unless the user gives a different
control-plane host.

## Rules

- Prefer API-only iteration before browser/UI work.
- Never broadcast a candidate with `rating: reject`.
- Do not use `SOLID` for show authoring. It is a bench power-test pattern only.
- Treat high brightness as power/glare risk. Ask or explain before broadcasting
  values above 128.
- Keep saved pattern names human-readable and specific.
- Use the real positioned lantern layout from the control plane; do not invent a
  fake layout unless explicitly working offline.
- Broadcasting is a live field mutation. Only broadcast when the user asks to run
  a pattern live or clearly approves the candidate.

## Pattern Parameters

`Glow`

```json
{"pattern":"Glow","brightness":48,"params":{"hue":40,"saturation":100}}
```

`Pulse`

```json
{"pattern":"Pulse","brightness":48,"params":{"hue":40,"saturation":100}}
```

`Sweep`

```json
{"pattern":"Sweep","brightness":64,"params":{"period":8000,"spatial":300}}
```

For `Sweep`, `spatial` is the wavelength in hundredths of a coordinate unit
because it maps to firmware `params[1]`.

`Wavefront`

```json
{"pattern":"Wavefront","brightness":64,"params":{"p0":6000,"p1":58396,"p2":65024,"p3":200}}
```

`Wavefront` is a single soft band with darkness ahead of and behind it. It
enters one side of the normalized 2-D field, crosses once, exits the opposite
side, then repeats. Its params are positional:

- `p0` = complete crossing period in ms, default 6000.
- `p1` = packed saturation + band width: top 6 bits hold saturation (0-63),
  low 10 bits hold width ×100. Default UI saturation 90 + width 28 packs to
  `58396`.
- `p2` = packed value + angle: bit 15 marks the packed format, bits 9-14 hold
  value (0-63), and low 9 bits hold angle in degrees. Default value 255 + angle
  0 packs to `65024`. Angle 0 moves left-to-right; image-space coordinates put
  90 top-to-bottom and 270 bottom-to-top.
- `p3` = hue in degrees, default 200.

Preview/review also accept friendly `period`, `front_width`, `angle`, and `hue`
query params. Live broadcasts should use the packed positional form above.

`Palette Drift`

```json
{"pattern":"Palette Drift","brightness":48,"params":{"period":8000,"spatial":100}}
```

For `Palette Drift`, `spatial` is hue offset in hundredths of a cycle per x unit.

`Firefly`

```json
{"pattern":"Firefly","brightness":56,"params":{"p0":7000,"p1":58,"p2":65508,"p3":37461}}
```

`Firefly` ("hotaru") uses **positional** params. Most of the time each node has
its own deterministic but irregular flash: start, duration, amplitude, shimmer,
and occasional skipped flashes vary by node and time window. It periodically
crossfades into exactly three shared beats, then disperses again. Its knobs
would collide on the shared `hue`/`period` aliases, so send them as `p0..p3`:

- `p0` = full cycle period in ms (flash + dark gap), default 7000.
- `p1` = hue in degrees (0-359), default 58 (warm gold-green, like a real firefly).
- `p2` = packed value + scatter: bit 15 marker, bits 7-14 sRGB value, and bits
  0-6 scatter (0-100). High scatter means fully irregular solos; zero retains
  the legacy regular/unison flash. Default value 255 + scatter 100 packs to
  `65508`.
- `p3` = packed chorus recurrence + saturation: bit 15 marker, bits 7-14 hold
  recurrence seconds, and bits 0-6 hold saturation (0-100). Default 36 seconds
  + saturation 85 packs to `37461`. The chorus itself is always three 1.35 s
  beats.

Preview/review also accept the friendly names (`period`, `hue`, `scatter`,
`saturation`, `chorus`) as a convenience, but a **live broadcast must send `p0..p3`** so the
firmware places them in the right slots.

`Ocean Wave`

```json
{"pattern":"Ocean Wave","brightness":64,"params":{"p0":9000,"p1":100,"p2":45,"p3":205}}
```

`Ocean Wave` is a soft 2-D swell of light rolling across the field — a sum of
three traveling sine wavefronts (dispersion-detuned so it never quite repeats),
dim deep blue in the troughs with foam-capped cyan-white crests. A non-black
Ocean config always retains at least the minimum visible PWM level between
crests instead of switching its LEDs fully off. Also **positional**:

- `p0` = primary swell period in ms (a crest crosses the field), default 9000.
- `p1` = wavelength ×100 (coord units); ~100 (=1.0) keeps 1-2 crests on the field
  for a calm swell. Shorter = more, tighter waves. Default 100.
- `p2` = travel direction in degrees. A diagonal (≈45) avoids row-by-row
  "chase"; straight axis angles read mechanical. Default 45.
- `p3` = base (mid-water) hue in degrees; the ramp runs indigo→azure→cyan around
  it. Default 205 (ocean blue). ~180-220 reads as water.

`Fire Flicker`

```json
{"pattern":"Fire Flicker","brightness":56,"params":{"p0":1200,"p1":24,"p2":65493,"p3":95}}
```

`Fire Flicker` is the first ring-addressable built-in pattern. Each 16-pixel
ring gets a shared billow plus coherent angular flame waves, so neighboring LEDs
move like flame tongues rather than independent noise. Brighter pixels shift
toward yellow and dimmer pixels toward red. Its params are positional:

- `p0` = primary flicker timescale in ms, default 1200.
- `p1` = middle flame hue in degrees, default 24 (orange).
- `p2` = packed value + texture: bit 15 marker, bits 7-14 sRGB value, and bits
  0-6 texture depth (0-100). Default UI value 255 + texture 85 packs to `65493`.
- `p3` = saturation percent, default 95.

Preview/review accept friendly `period`, `hue`, `texture`, and `saturation`
query params. Live broadcasts and saved candidates should use `p0..p3`; the
friendly `period` and `hue` aliases both map to wire slot 0 and therefore cannot
represent this pattern safely on a live conductor.

`Fire2012`

```json
{"pattern":"Fire2012","brightness":56,"params":{"p0":30,"p1":55,"p2":120,"p3":0}}
```

`Fire2012` is the classic one-dimensional FastLED heat-cell effect adapted to
Lightweave's synchronized runtime. Every active emitter is a heat cell: cells
cool, heat diffuses upward through increasing pixel indexes, sparks ignite near
pixel zero, and temperature maps black→red→yellow→white. Random draws are
deterministic from absolute time plus lantern identity, so performers remain
reproducible across preview, restart, and dropped-beacon free-running.

- `p0` = fixed simulation frames per second, 10-60; default 30.
- `p1` = cooling, 20-100; lower gives taller flames, default 55.
- `p2` = sparking chance out of 255, 50-200; higher gives a more active fire,
  default 120.
- `p3` = reserved; send 0.

Preview/review also accept the friendly names `speed`, `cooling`, and
`sparking`. A live broadcast or saved candidate should use `p0..p3`.

`Pond Ripple`

```json
{"pattern":"Pond Ripple","brightness":64,"params":{"p0":6000,"p1":50,"p2":500,"p3":500}}
```

`Pond Ripple` emits repeating concentric cyan-water crests from a selected field
point. Its controls are positional on the live wire:

- `p0` = period between rings in ms, default 6000.
- `p1` = wavelength ×100 in normalized field-coordinate units, default 50.
- `p2` = center X ×1000, default 500 (the field midpoint).
- `p3` = center Y ×1000, default 500 (the field midpoint).

Preview/review also accept `period`, `wavelength`, `center_x`, and `center_y`.
Live broadcasts and saved candidates should use `p0..p3`.

## Uploaded Pattern Programs

An uploaded program has four outputs: hue in cycles, saturation, perceptual
value, and intensity. Each output may be a finite number, an input (`x`, `y`,
`time`, or normalized `pixel`), or an operation object. Firmware supports
`add`, `sub`, `mul`, `div`, `min`, `max`, `pow`, `sin`, `cos`, `abs`, `fract`,
`clamp`, `neg`, `sqrt`, `hash`, `floor`, `mix`, and `smoothstep`; the compiler
also expands the friendly `wave` and `distance` operations.

```json
{
  "hue": 0.56,
  "saturation": 0.9,
  "value": {
    "op": "mix",
    "args": [0.2, 1.0, {
      "op": "wave",
      "args": [{"op": "add", "args": ["x", {"op": "mul", "args": ["time", 0.12]}]}]
    }]
  },
  "intensity": 1.0
}
```

Validate and preview against the positioned inventory without changing the field:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/uploaded-patterns/preview \
  -H 'content-type: application/json' \
  -d '{"name":"Blue wave","brightness":56,"program":{"hue":0.56,"saturation":0.9,"value":{"op":"wave","args":[{"op":"add","args":["x",{"op":"mul","args":["time",0.12]}]}]},"intensity":1}}'
```

An editor draft can be broadcast without saving it as a named pattern:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/uploaded-patterns/broadcast \
  -H 'content-type: application/json' \
  -d '{"name":"Blue wave","brightness":56,"program":{"hue":0.56,"saturation":0.9,"value":{"op":"wave","args":[{"op":"add","args":["x",{"op":"mul","args":["time",0.12]}]}]},"intensity":1}}'
```

To retain it, save the same payload with `POST /api/uploaded-patterns`, list it
with `GET /api/uploaded-patterns`, then broadcast the returned ID only after user
approval:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/uploaded-patterns/blue-wave/broadcast
```

Broadcast is transactional from the show's perspective: it first requires every
placed lantern online on the exact interpreter-capable firmware, distributes and
verifies the exact 64-bit BLAKE2s-addressed program everywhere, and only then changes the
selected pattern. A timeout or mixed fleet leaves the existing pattern active.
The conductor repeats this barrier independently, so bypassing the UI cannot
activate a partially distributed program. Readiness reports include exact firmware
identity and must postdate the node's latest registration, so a reboot or rollback
cannot reuse a stale acknowledgement.

## Draft Review Loop

1. Choose a candidate pattern, brightness, and params.
2. Get automated review:

```bash
curl -sS 'http://127.0.0.1:8000/review?pattern=Sweep&brightness=64&period=8000&spatial=300&duration_ms=8000&fps=4'
```

3. Inspect frame metrics if the pattern is temporal:

```bash
curl -sS 'http://127.0.0.1:8000/preview/frames.json?pattern=Sweep&brightness=64&period=8000&spatial=300&duration_ms=8000&fps=4'
```

4. Generate a still PNG if visual feedback is useful:

```bash
curl -sS -o preview.png 'http://127.0.0.1:8000/preview?pattern=Sweep&brightness=64&period=8000&spatial=300&t=1200'
```

5. Iterate until the review is acceptable and the metrics fit the user's intent.

Useful review fields:

- `ok`: false means do not broadcast.
- `rating`: `strong`, `usable`, `needs_review`, or `reject`.
- `score`: 0-100.
- `issues`: specific blockers or warnings.
- `recommendations`: concrete next adjustments.
- `metrics.avg_luma_mean`: overall brightness across the sampled window.
- `metrics.max_contrast`: spatial variation.
- `metrics.temporal_luma_range`: motion/change over time.

## Save Candidate

Save only candidates worth reusing or broadcasting:

```bash
curl -sS -X POST http://127.0.0.1:8000/api/patterns \
  -H 'content-type: application/json' \
  -d '{"name":"Slow Sweep","pattern":"Sweep","brightness":64,"params":{"period":8000,"spatial":300}}'
```

List saved patterns:

```bash
curl -sS http://127.0.0.1:8000/api/patterns
```

## Review Saved Pattern

Use the saved pattern id from create/list responses.

```bash
curl -sS 'http://127.0.0.1:8000/api/patterns/slow-sweep/review?duration_ms=8000&fps=4'
```

Other saved-pattern preview endpoints:

```bash
curl -sS -o preview.png 'http://127.0.0.1:8000/api/patterns/slow-sweep/preview?t=1200'
curl -sS 'http://127.0.0.1:8000/api/patterns/slow-sweep/preview.json?t=1200'
curl -sS 'http://127.0.0.1:8000/api/patterns/slow-sweep/preview/frames.json?duration_ms=8000&fps=4'
```

## Broadcast Saved Pattern

Broadcast only after review passes and the user approves live execution:

```bash
curl -sS -X POST 'http://127.0.0.1:8000/api/patterns/slow-sweep/broadcast'
```

Confirm live state after broadcasting:

```bash
curl -sS http://127.0.0.1:8000/api/state
```

## UI Workflow

Open `http://127.0.0.1:8000`, then use the Patterns tab.

- Tune the draft controls.
- `Save draft` stores the current draft in the pattern library.
- Saved pattern actions:
  - `Preview`: PNG still frame.
  - `Frames`: JSON frame sequence.
  - `Review`: automated score and recommendations.
  - `Broadcast`: live field mutation.
  - `Delete`: remove stale candidates.

## Verification

After changing this workflow or related code, run:

```bash
PYTHONPATH=. pytest control/tests
pio test -e native
python -m compileall control
node --check control/static/app.js
```

If a hardware conductor is attached, also smoke test one saved pattern end to end:

1. Save candidate.
2. Review saved candidate.
3. Broadcast saved candidate.
4. Confirm `/api/state.pattern`.
