# Do Baskets Dream — Firmware Project Brief

A Burning Man art installation: a field of 50–60 LED lanterns that play coordinated
patterns (slow pulses, palette drifts) that sweep across the physical field. Each
lantern is an **independent, battery-powered node**. There is no wiring between
nodes — coordination is wireless via **ESP-NOW**. This brief is the spec for the
node firmware; build it in PlatformIO.

---

## Goal of this phase

Get a **conductor + 2 performers** showing synchronized pulses over ESP-NOW, powered
by USB, before any battery/enclosure work. Sync is the hard part and the real risk —
prove it first, in isolation.

---

## Hardware (per node)

| Function | Part | Connection |
|---|---|---|
| MCU | ESP32-WROOM-32 — comparing **DevKitC** vs **FireBeetle 2 ESP32-E** (same chip, code runs unchanged on both) | — |
| LEDs | 16× **SK6812 RGBW** ring | data on **GPIO18**, through a 470Ω series resistor |
| LED power cap | 1000µF across the ring's 5V/GND | at the ring |
| Dusk sensor | LDR + 10kΩ divider | **GPIO34** (ADC1) |
| Battery sense (coarse) | 47kΩ/10kΩ divider off the 12V line | **GPIO35** (ADC1) — cheap voltage-only backup |
| Power monitor (precise) | **INA228** breakout (15mΩ shunt on-board, hardware energy/charge accumulation) | I2C: VCC→3.3V, GND→GND, SDA/SCL→ESP32 I2C pins; wired in series between battery+ and buck input |
| Power | KUNLUN model 1230, 12.8 V 30 Ah (384 Wh) LiFePO4 → buck → 5V | M8 terminals; 5V to ESP32 VIN + ring; common ground |

**Hard pin/ADC constraint:** the LDR and battery sense **must** use ADC1 pins
(GPIO 32–39). ADC2 pins stop working whenever the WiFi/ESP-NOW radio is active —
this is a silent failure, not an error. GPIO34 and GPIO35 are input-only ADC1 pins.

---

## Architecture

**Roles.** One **conductor** broadcasts sync beacons; all others are **performers**.
Role is selectable via a build flag or an NVS value (default performer). The conductor
can be a headless 3rd ESP32 on USB, or a performer that also originates the beacon.

**Transport.** ESP-NOW broadcast (peer = `FF:FF:FF:FF:FF:FF`), `WIFI_STA` mode, a
**fixed WiFi channel** set explicitly on every node (`esp_wifi_set_channel`) so they
all agree without scanning.

**Clock model (this is the resilience core).** The conductor periodically broadcasts
its clock. Performers compute an offset and render against the synced time. Crucially,
a performer that **misses a beacon keeps free-running on its last known offset** and
re-locks on the next beacon — so a dropped packet causes at most slight drift, never a
blackout. Use **`esp_timer_get_time()` (64-bit microseconds)**, not `millis()`, to
avoid 32-bit wraparound over a multi-day run.

**Beacon packet (sketch — refine as needed):**
```c
typedef struct {
  uint32_t magic;        // identify our packets
  int64_t  epoch_us;     // conductor's clock at send time
  uint16_t pattern_id;
  uint8_t  brightness;   // global cap
  uint8_t  palette_id;
  uint16_t params[4];    // pattern-specific knobs for live tweaking
  uint32_t seq;          // for drop detection / logging
} Beacon;
```

**Position-aware rendering.** Each node stores its **node ID + (x,y) coordinate** in
NVS (non-volatile flash). Patterns render as **`f(x, y, t)`** so a pulse can physically
travel across the field. A node cold-boots, reads its (x,y) from NVS, hears a beacon
within ~1–2s, locks the clock, and resumes — so battery swaps look like a single blink.

**LED library.** Use **NeoPixelBus** (handles SK6812 RGBW cleanly). FastLED is fine if
preferred but its RGBW support is rougher.

**Power discipline (load-bearing for the battery budget — not optional).**
- Enable **WiFi modem-sleep** so the radio naps between beacons. Leaving it in
  continuous receive roughly doubles ESP32 draw (~0.25W → ~0.55W) and would push a
  node past its 10-night battery budget.
- Run the CPU at **~160MHz** (16 pixels need nothing more); saves ~15–30mA.
- **Deep-sleep through the day**, wake at dusk (LDR or RTC timer).
- Target: ESP32 ≈ 0.25W active at night.

**OTA (later phase).** On-demand WiFi AP (button or scheduled window) → ArduinoOTA.
Don't depend on field-wide WiFi.

**Getting the reading out (readout path).** The INA228 accumulates in hardware, but that
number still needs to reach a human:
- **Bench/bring-up:** print `readEnergy() / 3600.0` (Wh) to serial while USB-tethered.
  Fine for validating the sensor itself, not for overnight battery-only runs.
- **Single-night validation:** let the node run untethered on battery overnight, then
  reconnect USB the next morning and read the live value off serial — the accumulator
  kept counting all night regardless of whether anything was listening. Plugging in USB
  typically adds power alongside the battery rather than replacing it, so this usually
  doesn't reset the accumulator — but don't disconnect the battery first, since any power
  gap zeroes the registers.
- **Fleet-scale (build this in before the multi-node tests):** have each performer send
  its accumulated Wh back to the conductor periodically as a small ESP-NOW return packet
  (ESP-NOW supports bidirectional unicast once nodes know each other as peers — this is a
  firmware addition, not new hardware). The conductor, kept on USB, logs every node's
  energy reading to serial automatically. This turns every overnight sync test into a
  full-fleet power audit with no need to physically visit each lantern.
- **Future field diagnostic (optional):** expose the current Wh reading over BLE so any
  deployed lantern's draw can be spot-checked with a phone app, independent of the
  conductor link.

**How the INA228 accumulation works.** Unlike the INA219, the INA228 has real hardware
ENERGY and CHARGE accumulation registers — a background digital engine integrates
continuously while the device is in **continuous conversion mode** (the library default),
independent of when firmware happens to check in. This eliminates the polling-rate/aliasing
question entirely — no firmware integration math needed:
- Init once via `Adafruit_INA228` library (STEMMA QT, no soldering needed).
- To read a night's total: just call `readEnergy()` (Joules — divide by 3600 for Wh) once,
  whenever convenient (once at wake, once at the end of the night, or periodically for
  logging — the number is the same either way since it's a true continuous integral).
- Call `resetAccumulators()` at the start of each night's run for a clean "Wh consumed
  last night" figure, same reasoning as before — don't bother persisting a cross-day total.
- **Must stay in continuous mode, not triggered mode** — in triggered mode the
  accumulation registers are invalid, since the device doesn't track elapsed time.
- Bonus: `readCharge()` gives Ah for free as a cross-check against the Wh figure.
- Same telemetry idea applies: fold the nightly energy reading into the ESP-NOW beacon so
  the conductor collects every node's overnight draw automatically during the sync test.
- This is a validation-only component — not intended for all 60 field units, just the
  1–2 instrumented prototype/reference nodes.

---

## Milestones (in order)

1. **Sync proof (do this first):** 1 conductor + 2 performers, USB-powered, no battery,
   no LEDs-in-enclosure. All three show a synchronized slow pulse on their rings.
   **Print sync status to serial** (offset, last-beacon age, seq gaps) so the boards can
   be range-walked to find where sync drops.
2. Add the (x,y) NVS identity + a pattern that visibly sweeps across nodes by position.
3. Add power management (modem-sleep, 160MHz, dusk deep-sleep) and the LDR/battery ADC.
4. Move to battery power; validate runtime/energy against the budget below.
5. OTA via on-demand AP; enclosure.

**Starter patterns:** (a) slow brightness pulse, (b) palette drift. Both keyed to
synced time + (x,y).

---

## Tooling

- **PlatformIO**, Arduino framework.
- Two board environments: `esp32dev` (DevKitC) and the FireBeetle 2 ESP32-E.
- USB-serial driver may be needed (CP2102 or CH340) — and the USB cable must be a
  **data** cable, not charge-only.
- Deps: NeoPixelBus; ESP-NOW via `esp_now.h` (built in).
- Role flag (conductor/performer) exposed as a build flag for easy reflashing.

---

## Design targets / context (so firmware respects the physical design)

- Brightness tier: **gentle** (slow fades, low brightness) — fits the aesthetic and the
  battery budget. ~0.7W LEDs.
- Per-node nightly energy ≈ **11 Wh** (10h run) → the 384 Wh pack gives roughly
  **32–35 nights** before derating, comfortably clearing the 10-night target.
  Keeping the ESP32 efficient protects cold-weather, aging, and incomplete-charge
  margin.
- Field: ~50–60 nodes; ESP-NOW single-hop from a centrally-placed (ideally elevated)
  conductor. Enable ESP-NOW long-range mode + max TX power if range testing demands it.

## Don't-break list

- ADC1 pins only for sensing (radio kills ADC2).
- Common ground between ESP32 and ring or the data line is invalid.
- Modem-sleep stays on — it's a battery-budget requirement, not a nicety.
- Free-run-on-missed-beacon behavior must hold (no blanking when a packet drops).
- 64-bit microsecond time base (no 32-bit millis wrap over the event).
