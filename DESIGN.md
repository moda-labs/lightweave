# Do Baskets Dream Control Plane Design System

Status: first-pass design direction, 2026-07-04.

This is the UI source of truth for the Phase 1 control plane described in
`docs/CONTROLPLANE.md`: a local, offline, phone-first admin console for operating
a field of synchronized ESP32 LED lanterns from a Raspberry Pi access point.

## Memorable Thing

This should feel like **a field instrument you trust at 2am**.

Not a SaaS dashboard. Not a light-show toy. The operator is standing in a dark,
dusty field with limited attention, a phone screen, and real hardware state to
understand. The UI should be calm, precise, and hard to misread.

## Product Posture

- **Audience:** installation operators, builders, and agents/scripts using the same
  API.
- **Environment:** night use, phone on Pi hotspot, no internet, no router, unreliable
  USB/radio conditions.
- **Primary jobs:** know whether the field is alive, edit the layout table, tune the
  live pattern, control the soundtrack, reset/read power telemetry, and escape to
  raw serial when needed.
- **Trust rule:** every mutation must surface the conductor ack or failure. The UI
  never pretends a write landed.

## Aesthetic

**Industrial field instrument.** Dense, restrained, dark, readable. The visual
language should borrow from test equipment, radio consoles, and map tools: stable
grids, clear statuses, real labels, tabular numbers, and small live indicators.

Avoid:

- marketing-page composition
- generic card-heavy SaaS dashboards
- purple/blue gradient accents
- bubbly controls
- decorative backgrounds
- centered everything
- animations that compete with the actual field preview

## Palette

Use a dark-adapted palette with semantic color. Keep large surfaces near-black;
reserve saturated color for state, alerts, live nodes, and active controls.

| Token | Hex | Use |
|---|---:|---|
| `bg` | `#070908` | app background |
| `surface` | `#101411` | panels, sheets, toolbar surfaces |
| `surface-2` | `#171d19` | raised controls, selected rows |
| `line` | `#26302a` | dividers, map grid, control borders |
| `text` | `#e4e9e3` | primary text |
| `muted` | `#a7b0aa` | secondary labels |
| `dim` | `#69746d` | disabled/inactive text |
| `live` | `#54d67a` | alive/healthy nodes, successful ack |
| `sync` | `#5eb7ff` | locked sync, current clock/pattern state |
| `amber` | `#f0b35a` | warnings, unsaved changes, show warmth |
| `alert` | `#ff5d52` | stale/dead nodes, failed writes, blackout danger |
| `violet` | `#9f8cff` | rare calibration/identify accent only |

Color discipline:

- One semantic color per state. Do not invent new greens/reds by screen.
- Green means alive or acked. Red means dead, failed, or destructive.
- Amber means attention needed but recoverable.
- Cyan means sync/time/pattern, not generic decoration.
- Keep body text off pure white to preserve night vision.

## Typography

Use two families:

- **Body/UI:** `Geist` or `Source Sans 3`.
- **Data/logs:** `JetBrains Mono` or `IBM Plex Mono`.

Rules:

- MAC addresses, serial lines, Wh/W/mA values, seq numbers, and timestamps use the
  mono face with tabular numerals.
- Headings stay compact. This is an operator console, not a hero page.
- Avoid all-caps paragraphs. Short all-caps labels are acceptable for telemetry
  chips and section labels.

Suggested scale:

- Screen title: 22-26 px, semibold
- Section title: 15-17 px, semibold
- Body/control text: 14-16 px
- Dense metadata: 12-13 px
- Logs: 12 px mono

## Layout

The Overview health dashboard is the primary surface. Lantern Locations remains
the primary spatial operating surface one tap deeper; everything else supports
those two views.

Default to **progressive disclosure**. The first screen should answer the simple
operator questions without exposing every diagnostic:

- Is the field alive and synchronized?
- Is firmware consistent?
- What is playing?
- Is power behavior healthy over time?
- What needs attention next?

Detailed MACs, serial lines, power internals, table cross-checks, and raw logs are
still available, but they live one interaction deeper: node inspector, expandable
diagnostic panels, or the Operations tab. The UI should feel approachable to a non-technical
helper while still letting the builder get every useful byte when needed.

### Mobile

- Primary tabs begin with `Overview`, followed by `Lantern Locations`, `Node List`,
  `Patterns`, `Sound`, `Power`, `Operations`, and `Firmware`.
- `Overview` opens by default and keeps health metrics, attention items, current
  group patterns, and measured power history readable without horizontal scrolling.
- `Lantern Locations` and `Table` are peer multi-lantern views over the same
  roster/table data.
- `Lantern Locations` opens to the layout map with a compact status strip above it.
- `Table` shows the same lanterns as sortable/filterable rows for count checks,
  missing-node triage, and precise data review.
- Selecting a node opens a bottom sheet, not a separate page.
- The single-lantern detail sheet appears only in `Lantern Locations` and `Table`;
  hide it in global screens like `Patterns` and `Operations`.
- Risky actions require large, deliberate controls and clear result feedback.
- Avoid multi-column layouts below tablet width.

### Desktop/tablet

- Left rail or top status bar for global health.
- Center: selected multi-lantern view, either map or table.
- Right inspector is **collapsible** and appears when a node, pattern, or warning is
  selected. When nothing is selected, keep the screen map-first and show only a
  compact "field summary" panel.
- Logs/serial console can sit in a resizable bottom drawer.

### Density

Use an 8 px spacing unit with tighter data surfaces:

- 4 px: internal gaps in dense rows
- 8 px: control gaps
- 12 px: panel padding on phone
- 16 px: panel padding on tablet/desktop
- 24 px: major screen regions

Cards are allowed only for repeated items or actual framed tools. Do not nest cards.

## Components

### Field Status Strip

Show the state the operator needs before touching anything:

- alive count, for example `58 / 60 alive`
- conductor connection
- current pattern
- wake flag
- one current attention item, if any

Use compact badges with semantic color and timestamps.

### Lantern Locations map

The map should look like a working field plot:

- dark grid, low contrast
- visible edge buffer around the field extents; normalized `(0,0)` and `(1,1)`
  should not sit directly on the screen edge
- node dots with liveness color
- selected node keeps its underlying liveness color and does not change size,
  position, or hit target; render selection as a separate cyan overlay ring so
  color continues to mean status
- unpositioned nodes in a side/bottom queue
- stale nodes dimmed or red-rimmed
- current pattern preview rendered directly on nodes when possible
- zoom controls with pinch-to-zoom on touch devices and wheel/trackpad zoom on
  desktop; click-hold-drag / one-finger drag pans the map like a familiar map app;
  map controls remain fixed while the field content scales

Node labels should appear on selection, hover, or zoom. Do not clutter the default
view with 60 always-visible MAC labels.

### Lantern Table

The table is the map's analytical twin, not an admin afterthought. It should show
the same multi-lantern state in a form that is better for scanning exact values.

Use it for:

- finding stale, missing, or unpositioned lanterns
- checking roster vs. table mismatches
- sorting by last seen, id, MAC, power report age, or position state
- batch review before/after calibration import

Rows should stay plain by default: lantern label, status, last seen, position, and
one attention reason. MAC, power telemetry, raw events, and destructive operations
belong in the lantern detail surface after selecting a row.

### Node Inspector

The node inspector is not permanent chrome. Treat it as a **single lantern detail
surface**:

- Selecting a lantern opens the detail surface directly below the map on mobile and
  tablet. On wide desktop it may become a side inspector, but it should keep the same
  content model and should never be required for the mobile experience.
- All single-lantern operations live here: identify, move/assign, replace, forget,
  power details, recent events, and technical identifiers.
- The default detail state is plain-language: "Lantern #12 is alive", last seen,
  position state, and the next obvious actions.
- Advanced sections expand in place: technical details, power telemetry, table row,
  raw events, replace/forget.
- Keep global actions out of the lantern detail surface unless they relate directly
  to the selected lantern.
- `Move` should arm direct manipulation mode: then click-drag or touch-drag the
  selected lantern on the map, release, and send the new normalized `(x,y)`
  assignment. Avoid raw coordinate prompts in the normal operator flow.

Patterns:

- MAC, friendly id, `(x,y)`
- last seen age
- sync/power details when available
- actions: identify, assign position, forget, replace

Destructive actions such as `forget` should be red-outline and require confirmation.

### Live Show Controls

Controls should be tactile and direct:

- Pattern picker as segmented control.
- Brightness as a slider with numeric value.
- Hue as named swatches plus a keyboard- and pointer-accessible color wheel;
  retain the native system color picker as a compact fallback.
- Pattern params use human labels: `sweep period`, `wavelength`, `spatial hue`,
  `saturation`.
- `SOLID` is hidden behind bench mode.
- `Blackout` is visually distinct, red, and always shows ack/failure.
- `Wake` is a toggle with conductor state and last ack.

The preview should update before broadcast; broadcast requires a clear action.

### Power Panel

Prioritize tonight's operational truth:

- Wh tonight
- average W
- battery/input voltage
- current mA
- last report age
- plausibility flag

Use small trends only after history exists. Do not fake charts with one sample.

### Event Log and Raw Serial

Logs use mono typography and stable row heights. Separate structured events from raw
serial passthrough. Raw serial is an escape hatch, so keep it available but visually
secondary to the API-backed controls.

## Motion

Motion is functional only:

- live nodes can breathe subtly
- stale nodes can fade, not blink
- ack/failure toasts slide in quickly and disappear after a readable interval
- panel transitions should be short and predictable

No decorative motion. The actual light pattern preview is the expressive element.

## Safe Choices

- Dark, data-dense operational UI because this is used at night and under pressure.
- A dedicated Lantern Locations surface because position editing needs an
  unmistakable home separate from the Overview field rendering.
- Mono typography for hardware identifiers and telemetry because mistakes in those
  values are expensive.

## Deliberate Risks

- **No generic dashboard cards as the dominant layout.** Overview uses only the
  few cards needed to summarize field health; Lantern Locations owns spatial work.
- **Warm amber as a major accent.** Most technical dashboards lean blue. Amber ties
  the UI to the lantern installation and preserves the nocturnal feel.
- **Visible hardware roughness.** MACs, serial status, stale ages, and acks stay
  present. This is less polished than hiding them, but it builds trust when the field
  is misbehaving.

## Implementation Notes

- Build the UI as a pure client of the FastAPI HTTP + WebSocket API.
- Keep all controls reachable and readable on a phone.
- Use local fonts/assets only; the Pi field deployment has no internet.
- Mirror C++ pattern math with a JS preview plus golden-vector conformance tests.
- Prefer clear disabled/error states over optimistic UI for conductor writes.
