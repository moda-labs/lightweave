# Flashing & Bring-up Runbook

Everything we learned getting the first three boards flashed and synced. Read
this before flashing a new board — most of the pain came from non-obvious
gotchas, all captured here.

## TL;DR — flashing a brand-new board

```bash
export PATH="/opt/homebrew/bin:$PATH"

# 1. Find the port
pio device list                      # CP2102: usbserial; FireBeetle: wchusbserial

# 2. ONE-TIME full erase (new boards ship with factory firmware — see below)
~/.platformio/packages/tool-esptoolpy/esptool.py --port /dev/cu.usbserial-XXXX erase_flash

# 3. Flash the one firmware image (role is set at runtime, not at build)
pio run -e devkitc -t upload --upload-port /dev/cu.usbserial-XXXX

# 4. Provision + watch it run
pio device monitor -p /dev/cu.usbserial-XXXX -b 115200   # Ctrl-A then K to quit
#   then type:  role performer   (or 'role conductor' for the one conductor)
#               id 1 / pos 0 0   as needed
```

Every node runs the **same image**; role lives in NVS (default performer) and is
set over serial with `role conductor|performer`. A healthy performer prints
`LOCKED ... gaps=0` within ~1 s; a conductor prints `[conductor] ... seq=` climbing
once per second.

---

## The gotchas (in the order they bit us)

### 1. New boards ship with factory ESP-AT firmware — erase first
Our DOIT ESP32 DevKit V1 boards came preloaded with **ESP-AT firmware**
(you'll see `at_customize` / `ota_0` / `ota_1` partitions and
`Bin version(Wroom32)` in the boot log). A normal `upload` writes our app but
leaves leftover flash state that **scrambles the clock**, with these symptoms:

- Serial is **garbage at 115200** but **clean at 74880 baud**
- The GPIO2 heartbeat **won't blink** (or blinks at the wrong rate)
- Sync is flaky / `gaps` piles up

**Fix:** a one-time full chip erase before flashing:
```bash
~/.platformio/packages/tool-esptoolpy/esptool.py --port /dev/cu.usbserial-XXXX erase_flash
```
Then flash normally. This is a per-board, one-time step.

> Note: the boards are genuinely identical hardware — confirmed
> `Crystal is 40MHz`, ESP32-D0WD-V3, via
> `esptool.py --port <PORT> flash_id`. The 74880 baud was leftover flash, NOT a
> 26 MHz crystal. If a board ever acts "wrong," `erase_flash` first.

### 2. All boards enumerate as the same serial — names shuffle on replug
Every board reports USB serial `0001` (`VID:PID=10C4:EA60 SER=0001`), so macOS
can't give them stable names. When two are plugged in you'll see e.g.
`/dev/cu.usbserial-0001` and `/dev/cu.usbserial-7`, and **which physical board
is which name changes every time you replug.**

Consequences:
- **Flash deterministically by the current port**, not by which board you think
  it is. Read the port first (`pio device monitor` or the snippet below) to see
  its current role, then flash.
- When flashing several boards, do them **one at a time** and **label each with
  tape** (C, P1, P2) right after flashing.
- A successful "swap" isn't guaranteed — if a board didn't actually get
  reconnected, you can flash the same one twice and leave another untouched
  (this is how we ended up with a board still on factory firmware).

### 3. Exactly ONE conductor
Role is a runtime NVS value (set over serial with `role conductor|performer`),
not a build flag — every node runs the same image. There must be **only one
conductor** powered at a time.

Symptom of two conductors: a performer shows **`gaps ≈ rx`** (almost every
beacon counted as a gap) and its `offset`/`seq` jump between two very different
values — it's ping-ponging between two clocks. Fix: power down the extra
conductor. (With one conductor, a locked performer holds `gaps=0` and a steady
offset.)

### 3.5 Don't leave a USB power meter (ET900) inline while flashing/debugging
A pass-through USB power meter in the cable between the Mac and the board will
**corrupt the serial** (garbled at *every* baud — bit-level mangling of D+/D−, not
a baud mismatch) and can **brown out the radio-init current spike**, producing an
`rst:ets …` boot loop. Symptoms mimic gotcha #1, but `erase_flash` won't fix it —
removing the meter does. Flash and debug with the board plugged **straight** into
the Mac; measure power on the **12 V battery side** with a DMM in series instead
(that reading is authoritative anyway). This cost a session before we spotted it.

### 4. Use a DATA USB cable, install the correct driver
- Charge-only cables won't enumerate a port — if `pio device list` shows nothing
  new, suspect the cable first.
- The DevKitC boards use a **CP2102** USB-UART chip. If no port appears, install
  the CP2102 (Silicon Labs) driver.
- The FireBeetle 2 ESP32-E used here has a **WCH CH340** USB-UART chip
  (`VID:PID=1A86:7522`) and needs the WCH driver described below.

#### FireBeetle WCH driver setup on macOS 15

The FireBeetle can appear in **System Information → USB** as `USB Serial`
(`VID:PID=1A86:7522`) while `pio device list` still shows no port. That means the
Mac sees the USB device, but the WCH serial driver is missing or installed but
not enabled.

1. Install the WCH driver and complete the macOS package installer, including
   administrator authentication:

   ```bash
   brew install --cask wch-ch34x-usb-serial-driver
   ```

2. Open **System Settings → General → Login Items & Extensions**, scroll to
   **Extensions**, click the info button next to **Driver Extensions**, and enable
   **WCH**, **CH34xVCPDriver**, or **QinHeng**. On macOS 15 this toggle is under
   **General**, not **Privacy & Security**.
3. Restart if macOS requests it, then unplug and reconnect the FireBeetle.
4. Verify that the extension is enabled and the serial port now exists:

   ```bash
   systemextensionsctl list | grep -i CH34
   pio device list
   ```

   A working setup reports `[activated enabled]` and exposes a port such as
   `/dev/cu.wchusbserial2130`. `[activated waiting for user]` means the Driver
   Extensions toggle still needs to be enabled.

Before erasing or flashing, confirm the new WCH port identifies a different ESP32
from every connected node:

```bash
~/.platformio/packages/tool-esptoolpy/esptool.py \
  --port /dev/cu.wchusbserialXXXX chip_id
```

Then use the `firebeetle` environment and that exact port for the upload:

```bash
~/.platformio/packages/tool-esptoolpy/esptool.py \
  --port /dev/cu.wchusbserialXXXX erase_flash
pio run -e firebeetle -t upload --upload-port /dev/cu.wchusbserialXXXX
```

---

## Reading serial without resetting the board

`pio device monitor` is fine for watching, but **opening the port toggles
DTR/RTS and resets the ESP32**. To peek without resetting (e.g. to check a
running board's role/offset), deassert the control lines after opening. The
platformio-bundled Python has `pyserial`:

```bash
/opt/homebrew/Cellar/platformio/*/libexec/bin/python - <<'EOF'
import time, serial
s = serial.Serial('/dev/cu.usbserial-XXXX', 115200, timeout=1)
time.sleep(0.4); s.setDTR(False); s.setRTS(False)   # don't reset
for _ in range(8):
    print(s.readline().decode(errors='replace').strip())
s.close()
EOF
```

### If a typed command seems to get ignored right after opening serial

Opening the CP2102 port can still race the ESP32 boot/reset path: the first
command you write may land while the bootloader/app is still coming up, so it
gets lost even though later output looks normal. This showed up while setting a
performer's friendly ID after flashing: `id 1` appeared to do nothing until we
waited for boot to settle, sent a blank newline to wake the CLI, then sent the
command and verified with `info`.

Reliable scripted shape:

```python
s = serial.Serial('/dev/cu.usbserial-XXXX', 115200, timeout=0.25)
s.setDTR(False); s.setRTS(False)
time.sleep(1.0)
s.reset_input_buffer()
s.write(b'\n'); s.flush()       # wake/clear the line-oriented CLI
time.sleep(0.2)
s.write(b'id 1\n'); s.flush()
time.sleep(0.5)
s.write(b'info\n'); s.flush()   # verify the command actually persisted
```

## Reading the diagnostics

> **Quiet node?** Diag lines only print within **5 minutes of serial input**
> (battery nodes shouldn't burn UART time printing to nobody). **Hit Enter** in
> the monitor to revive them for another 5 minutes.

**Conductor:**
```
[conductor] t=1338048 us  seq=4  pat=0  bri=48
```
`seq` climbing ~4/sec = beaconing normally.

**Performer:**
```
[performer] LOCKED  offset=6803499 us  last_beacon=83 ms ago  rx=16  gaps=0  rej=0  seq=42
```
| Field | Healthy | Trouble |
|---|---|---|
| state | `LOCKED` within ~1 s | never locks → no conductor / wrong channel / needs erase |
| `offset` | stable, glides on correction | jumping between two values → two conductors |
| `last_beacon` | < 250 ms | climbing → not receiving |
| `rx` | climbing ~4/sec | `0` → no beacons heard |
| `gaps` | `0` (or rare) | `≈ rx` → two conductors |
| `rej` | `0` (or rare) | climbing → late/bogus beacons gated out (congestion, or a rebooting conductor before it re-locks) |

`FREE-RUN` with `offset=0, rx=0` just means no conductor is in range — the node
keeps rendering off its local clock (this is the intended no-blackout behavior),
and a flashed board still blinks its GPIO2 LED.

---

## Build environments

| Env | Target |
|---|---|
| `devkitc` | DOIT ESP32 DevKit V1 (`esp32dev`) — bench (heartbeat LED on) |
| `firebeetle` | FireBeetle 2 ESP32-E — bench (heartbeat LED on) |
| `field` | Canonical DevKit/FireBeetle **deployment + OTA** image (`-D HEARTBEAT_LED=0`) |
| `native` | host unit tests (`pio test -e native`) |

One field image across both board types; **role is set at runtime** over serial
(`role conductor|performer`, default performer). Multi-board setup = flash the
same field image everywhere, then set exactly one node to `role conductor`.

**Bench vs field:** `field` is the one artifact used on both board types, with
the onboard heartbeat LED compiled out — inside an opaque lantern it is
invisible, burns LED current all night, and caps every Stage-B nap at 500 ms. Flash `field`
onto anything that ships to the field; keep the bench envs for desk work (the
GPIO2 blink is the zero-wiring sync check).

---

## Batch FireBeetle auto-flashing (macOS)

Production releases publish the OTA binary plus a hash-pinned serial ZIP with
the bootloader, partition table, OTA boot helper, canonical field firmware, and
exact flash plan. The watcher follows the reviewed GitHub production channel;
it never builds the local checkout, so local edits cannot change provisioning.
If GitHub is temporarily unavailable it retains the last verified cache.

After a release is promoted:

```bash
python3 scripts/firebeetle_autoflash.py install --factory
tail -f ~/Library/Logs/lightweave-firebeetle-autoflash.log
```

`--factory` is the explicit authorization to erase a previously unseen board
that still has no valid Lightweave identity after a non-destructive ROM reset.
Omit it for an update-only station; unrecognized hardware then fails closed
without any flash write. A factory erase is recorded as pending before the
command and as known only after it succeeds, so an interrupted write never
silently skips or repeats an ambiguous erase.

Plug FireBeetles in one at a time with a direct data cable. The watcher accepts
the WCH `1A86:7522` port, confirms ESP32-D0WD-V3/40 MHz/4 MB through the ROM,
skips the current clean build, preserves Lightweave NVS, and full-erases only a
previously unseen board in explicit factory mode after both normal and post-reset
`info` attempts fail. It verifies the production build and preserved
role/ID/position after flashing.

The USB/chip signature is a strong fleet check, not a unique board-model ID; do
not attach unrelated CH340 ESP32 hardware while enabled. A failure is retried
after unplug/replug. If the log reports an ambiguous factory erase, physically
confirm the ROM MAC belongs to the factory board, then authorize one retry:

```bash
python3 scripts/firebeetle_autoflash.py retry-factory C0:CD:D6:C8:03:E0
```

Disable the watcher while retaining its cache with:

```bash
python3 scripts/firebeetle_autoflash.py uninstall
```
