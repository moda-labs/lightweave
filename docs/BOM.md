# Production Bill of Materials — *Do Baskets Dream*

Parts list + cost model for the production field. Companion to
[`PROJECT_BRIEF.md`](PROJECT_BRIEF.md) (the hardware table / constraints) and the
`power-budget-go-no-go` measurements that justify the battery sizing.

**Scope:** 50 deployed lanterns + 5 spares (**55 nodes**) + 1 conductor + 1 admin host.
**Status:** first costed pass (2026-06-28), battery selection updated 2026-07-25.
Decisions locked except the enclosure (art-side), fleet charger, and the
level-shifter (recommended-optional). Verify every price at checkout — see the
price caveat at the bottom. **See the [Pilot batch](#pilot-batch-57-units--hardware-validation-before-the-full-order)
section for the near-term 5–7 unit order that precedes this full 55-node buy.**

---

## Locked decisions feeding this BOM

- **MCU → FireBeetle 2 ESP32-E** (not the DevKitC). Lower quiescent draw shrinks the
  CPU floor that is now the dominant night-power term; firmware already has a
  `firebeetle` build env. DevKitCs stay as bench boards.
- **LED → SK6812 **RGBW** warm-white** (Adafruit ring). RGBW (4-channel) is required —
  the GLOW scene and the entire measured power budget depend on the white channel.
- **Power → per-node 12 V LiFePO4 → buck → 5 V. Batteries, no solar.** Daytime
  deep-sleep (firmware Lever 2) is the calendar-life lever, not the battery.
- **Battery → KUNLUN model 1230, 12.8 V 30 Ah / 384 Wh.** At the measured
  ~11.7 Wh/night that's **~32 nights night-only** before derating — comfortably
  clears the 10-night target. The label specifies 14.4 V charge (14.6 V max),
  ≤20 A charge current, 0–45 °C charge temperature, and M8 terminals.
- **Admin host → Raspberry Pi Zero 2 W, headless.** Calibration CV runs on a laptop,
  not the field box, so no beefy Pi needed.

---

## Per-node BOM (performer)

Prices are recent-market estimates (Amazon strips live prices from automated
fetches; confirm at checkout). "Ext." = extended cost for the part actually consumed
per node (multipack unit price).

| # | Part | Selected product | Source / link | Unit est. | Per-node |
|---|---|---|---|---|---|
| 1 | MCU | FireBeetle 2 ESP32-E (DFR0654) | **DFRobot direct** ([product-2195](https://www.dfrobot.com/product-2195.html)) — $8.30 @10+ | $8.30 | $8.30 |
| 2 | LED ring | NeoPixel Ring 16× SK6812 **RGBW Warm White ~3000K** (#2854) | **Adafruit direct** ([2854](https://www.adafruit.com/product/2854)) — $10.76 @10–99, $9.56 @100+ | $10.76 | $10.76 |
| 3 | Buck 12→5 V | UCTRONICS 9–36 V→5 V 5 A, **fixed 5 V**, screw terminals (2-pack) | Amazon [B07XXWQ49N](https://www.amazon.com/dp/B07XXWQ49N) — ~$13/2pk | $6.50 | $6.50 |
| 4 | **Battery** | KUNLUN model **1230**, 12.8 V 30 Ah **384 Wh** LiFePO4, M8 terminals | eBay [357870398757](https://www.ebay.com/itm/357870398757) — $60.29 each at 4+ before tax (2026-07-25) | $60.29 +tax | $66.00 |
| 5 | Level shifter | 74AHCT125 DIP-14 (3.3→5 V data) — *recommended-optional* | Amazon 10-pk [B08GJF43N3](https://www.amazon.com/dp/B08GJF43N3) | ~$1.00 | $1.00 |
| 6 | Resistors | 470 Ω data + 47 k/10 k divider (from assortment) | Amazon kit [B07L851T3V](https://www.amazon.com/dp/B07L851T3V) — ~$14/1280pc | — | $0.20 |
| 7 | Bulk cap | 1000 µF 16 V radial across ring 5 V/GND | Amazon 10-pk [B07FYSD8TD](https://www.amazon.com/dp/B07FYSD8TD) | $0.70 | $0.70 |
| 8 | Dusk sensor | PT334-6C 5 mm phototransistor (analog, ADC1/GPIO34) | Amazon 20-pk [B00M1PMHO4](https://www.amazon.com/dp/B00M1PMHO4) | $0.35 | $0.35 |
| 9 | Carrier PCB | simple 2-layer node board (consolidates 5–8 + dividers) | JLCPCB @ qty | ~$2.00 | $2.00 |
| 10 | Connectors | M8 ring-lug battery lead; JST-SM pre-crimped pigtail for the LED ring | Source battery lead locally; JST-SM kit [B09SX68S6Y](https://www.amazon.com/dp/B09SX68S6Y) | — | $1.00 |
| 11 | Fuse | Inline ATC blade holder + fuse (12 V line) | Amazon kit [B07FQCBSJ5](https://www.amazon.com/dp/B07FQCBSJ5) — 10 holders +50 fuses | $1.60 | $1.60 |
| 12 | Switch | SPST mini toggle on/off | Amazon 8-pk [B09WQKTN2L](https://www.amazon.com/dp/B09WQKTN2L) | $1.10 | $1.10 |
| 13 | Misc | hookup wire, heatshrink, standoffs | — | — | ~$2.00 |
| 14 | Enclosure | basket / lantern + diffuser | **art-side, TBD** | TBD | TBD |

**Per-node electronics (excl. battery & enclosure): ≈ $35.51**
**Per-node with battery (excl. enclosure): ≈ $101.51**

The battery is ~65% of the costed per-node total. The next-largest levers are the
LED ring and the buck (the fixed-vs-adjustable choice, below).

---

## Shared infrastructure (one-time)

| Part | Selected product | Source / link | Est. |
|---|---|---|---|
| Conductor board | Lonely Binary ESP32-**WROOM-32UE**, external 2.4 GHz antenna **included** — max-range broadcast | Amazon [B0GR4Q23JV](https://www.amazon.com/dp/B0GR4Q23JV) | ~$15 |
| Conductor power | 1× KUNLUN model 1230 + UCTRONICS buck (or wall power — it beacons 4 Hz continuously, exempt from radio duty-cycle) | — | ~$75 |
| Fleet charging | One 14.4 V LiFePO4 CC/CV charger per battery, or an isolated-output multi-bank equivalent; ≤20 A per output | Product/circuit plan TBD; do not use a passive splitter from one shared charger | TBD |
| Admin host | Raspberry Pi **Zero 2 W**, headless (AP + web UI + USB-serial bridge) | kit [B0D5CYJ7V3](https://www.amazon.com/dp/B0D5CYJ7V3) + SD + 5 V PSU + micro-USB-OTG→USB-A | ~$45 all-in |
| Calibration | drone for the fly-over survey; CV runs on a laptop | rent/borrow — out of BOM | — |

### Fleet charging plan

Treat every loose battery as its own charging bank. Use either one safety-listed
14.4 V LiFePO4 CC/CV charger per battery or a multi-bank charger whose outputs are
electrically independent. A **5 A per-battery** target is the current recommendation:
it is far below the label's 20 A ceiling and should refill an empty 30 Ah pack in
roughly 6–7 hours including the CV tail. Do not connect loose batteries to a common
DC bus or passive charger splitter; different states of charge can cause
uncontrolled battery-to-battery equalization current.

For 55 batteries at 5 A, charger output is about 4.0 kW. Allowing for charger loss
is roughly 4.4 kW / 37 A at 120 V, so plan on **three dedicated 20 A circuits**
(or equivalent 240 V infrastructure), distributing charger nameplate input current
so each circuit stays within its continuous-load rating. Use listed rack PDUs, no
daisy-chained power strips, terminal covers, and a fuse in every positive charging
lead sized to protect that lead. Charge only in a dry, attended, noncombustible,
ventilated area with the packs outside their lantern enclosures and between 0–45 °C.

---

## Field roll-up (50 lanterns + 5 spares = 55 nodes, + conductor + admin host)

| Bucket | Qty | Est. total |
|---|---|---|
| Node electronics | 55 builds | ~$1,953 |
| Batteries | 55 | ~$3,630 |
| Conductor (board + power) | 1 | ~$90 |
| Admin host (Pi Zero 2 W) | 1 | ~$45 |
| **Subtotal — electronics + batteries** | | **≈ $5,720** |
| Enclosures / diffusers | 55 | **TBD (art-side)** |

Enclosures are the remaining unknown and could rival the battery line depending on
how the baskets are built — owned on the art side, left as a placeholder here.

---

## Pilot batch (5–7 units) — hardware validation before the full order

**Status: ORDERED 2026-07-03** — receipts in `receipts/`. Deltas from the table
below: 5 batteries / 4 RGBW rings bought (6th node uses already-owned bench
hardware); 2× INA228 (both Adafruit invoices — the first also had wrong RGB
rings; confirm that order's cancelled); JST-SM kit + fuse holders **not yet
ordered**; extras added (IP65 junction boxes ×5, grommets, sealant, 150 A power
analyzer). Details in `HANDOFF.md` → "Pilot batch: ORDERED". This section is a
historical receipt record; the production battery selection above now differs.

**Purpose:** bridge from "one bench-verified node" to the 55-node production buy.
Proves multi-node roster/sync behavior beyond 1–2 performers, gets one node
instrumented with the INA228 for a real continuous-Wh measurement (replacing
one-off ET900/DMM snapshots), and starts the Pi-based control-plane build (§5.2 in
`ARCHITECTURE.md`) in parallel with firmware work. Hand-built on **perfboard**, not
the fabbed carrier PCB — that's a 55-node-scale investment, not a 6-node one.

Sized for **6 nodes** (middle of the 5–7 range). Scale the per-node rows (1–4)
±1 trivially; every shared pack below (5, 7, 9, 11–13) already has spare headroom
across 5–7 units.

| # | Part | Qty to buy | Est. total | Notes |
|---|---|---|---|---|
| 1 | FireBeetle 2 ESP32-E | 6 | $49.80 | DFRobot direct, same as production pick |
| 2 | SK6812 RGBW ring | 6 | $64.56 | Adafruit direct |
| 3 | UCTRONICS buck 9–36V→5V | 3× 2-packs | $39.00 | covers exactly 6 |
| 4 | TalentCell LF120A1 battery | 6 | $288.00 | |
| 5 | Level shifter 74AHCT125 (optional) | 1× 10-pack | ~$10 | covers 6 + spares |
| 6 | Resistor assortment (data + divider) | 1 kit | ~$14 | reuse across many builds, not consumed by 6 |
| 7 | 1000µF 16V bulk cap | 1× 10-pack | ~$7 | covers 6 + spares |
| 8 | PT334-6C phototransistor | 1× 20-pack | ~$7 | covers 6 + spares |
| 9 | **Perfboard** (replaces the Carrier PCB line for this batch) | 1× 30-pack, T Tulead 5×7cm ([B07V9LSJLZ](https://www.amazon.com/Tulead-Perfboard-Prototyping-Circuit-Experiment/dp/B07V9LSJLZ)) | ~$8–13 (price unconfirmed — Amazon blocks the read) | covers all 6–7 + spares |
| 10 | JST-SM connector kit | 1 kit | ~$6 | covers 6 |
| 11 | Fuse holder + fuse | 1 kit (10 holders) | ~$16 | covers 6 + spares |
| 12 | SPST toggle switch | 1× 8-pack | ~$9 | covers 6, 2 spare |
| 13 | Misc (wire, heatshrink, standoffs, header pins) | — | ~$15 | |
| 14 | **INA228 power monitor** *(new)* | 1, Adafruit **#5832** ([adafruit.com/product/5832](https://www.adafruit.com/product/5832)) | **$14.95** (confirmed, manufacturer-direct) | goes on 1 reference node only — see `ARCHITECTURE.md` §4.2 |
| 15 | **Raspberry Pi 3 Model B+ starter kit** *(new)* | 1, CanaKit complete kit ([B0778CZ97B](https://www.amazon.com/CanaKit-Raspberry-Complete-Starter-Kit/dp/B0778CZ97B)) | ~$124.95 (CanaKit-direct price anchor; confirm at checkout) | admin-host / control-plane build (§5.2); picked over the Pi Zero 2 W production pick for reliable current stock — kit includes a case/HDMI cable/heatsinks not needed for headless use, so a bare board+PSU+SD bundle would run cheaper if you find one in stock |

**Estimated pilot total: ≈ $639** (6 nodes + INA228 + Pi, excl. tax/shipping/enclosure) —
node hardware ≈ $499 (electronics ≈ $211 incl. perfboard, batteries $288), + $14.95
INA228, + ~$125 Pi kit.

**Not included / deliberately deferred for this batch:**
- **Conductor hardware** — reuse the 3 already-owned bench DevKitC boards; no new
  purchase needed to prove out per-node builds + INA228 + Pi. The Lonely Binary
  external-antenna conductor board (~$15, see Shared infrastructure above) is worth
  adding only if this batch also needs a range test.
- **Enclosures** — this batch is a bench/indoor validation run, not a field test;
  enclosure design is still the open art-side track (see Open items below).

---

## Sourcing notes & open tradeoffs

- **FireBeetle is off-Amazon.** No dependable Amazon US listing — buy **direct from
  DFRobot** (~$8.30/board at 10+). Amazon fallback if you must stay on one cart: a
  plain ESP32 WROOM-32 DevKit 3-pack (~$5–6/board), but you lose the lower quiescent
  draw that made the FireBeetle the production pick.
- **LED & level-shifter are cheaper manufacturer-direct** than Amazon resellers
  (Adafruit $10.76 vs reseller markup; DFRobot/Adafruit beat third-party ASINs).
- **Buck — fixed vs. adjustable:** the UCTRONICS is genuinely fixed 5 V with screw
  terminals → **no per-unit calibration**. Cheap LM2596/MP1584 multipacks are
  ~$1/unit but each of 60 needs its trimpot set to 5.0 V and metered, with a
  knocked-off-voltage risk in the field. Recommendation: pay ~$300 more for the
  UCTRONICS and skip 60 calibrations.
- **Wrong-part traps to avoid:** Adafruit [B00KBXT9I0](https://www.amazon.com/dp/B00KBXT9I0)
  / #1463 is the **RGB** ring (no white channel); most Amazon "16-LED rings" are
  WS2812B RGB. Confirm "SK6812 **RGBW**" + a stated white temperature.
- **Pi Zero 2 W stock** is intermittently constrained (packaging, not the RAM
  shortage). Watch rpilocator.com; the Pi 3 B+ is the in-stock fallback (and adds
  real USB-A ports, skipping the OTG adapter) at ~$40–55.

## Open items before ordering

1. **Enclosure / diffuser** approach + per-unit cost (art-side).
2. **RF with buried electronics** — read `RF_ENCLOSURE.md` before production MCU
   purchase. Buried control boxes likely need external-antenna ESP32-UE boards,
   above-grade 2.4 GHz antennas, and 3 ft low-loss coax rather than the current
   DFR0654 onboard-antenna FireBeetles.
3. **Dust / weatherproofing** for BRC — sealed enclosure, gasketed battery door,
   conformal-coat on boards. Not yet costed; belongs here once the enclosure is known.
4. **Level shifter** — confirm whether to populate it on every node (reliability) or
   omit (the bench rig ran direct 3.3 V data through the 470 Ω resistor and worked).
5. **Carrier PCB** — design + fab the node board so 6–13 consolidate onto it; this
   is what makes a 60-unit build assemblable.

## Price caveat

Amazon renders prices via JavaScript and blocks automated price reads, so the dollar
figures above are recent typical-market estimates, **not** live quotes. Every ASIN,
title, and `/dp/` link was verified to resolve to a real listing. The two hard
anchors are the battery ($60.29 each at 4+ before tax on 2026-07-25) and the
manufacturer-direct LED/FireBeetle quantity pricing. Confirm all totals at checkout.
