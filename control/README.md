# Control Plane

Local web control plane for Do Baskets Dream. It defaults to a mock conductor
for UI/API work and can talk to a real conductor over newline-delimited JSON on
USB serial.

## Run

Use Python 3.13 or 3.12. Python 3.14 is too new for the pinned FastAPI/Pydantic
dependency stack today.

```bash
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r control/requirements.lock
.venv/bin/python -m uvicorn control.app:app --reload --host 127.0.0.1 --port 8000
```

Real conductor on the same laptop, exposed only on loopback:

```bash
CONTROL_CONDUCTOR=local-serial \
CONTROL_SERIAL_PORT=/dev/cu.usbserial-XXXX \
CONTROL_DATA_DIR="$PWD/.control-data" \
.venv/bin/python -m uvicorn control.app:app --host 127.0.0.1 --port 8000 \
  --workers 1 --no-proxy-headers
```

`local-serial` is deliberately narrower than mock mode: HTTP and WebSocket
requests must arrive directly on a loopback socket, use a loopback Host, and
carry no forwarding headers. This lets the same process act as the permanent-ID
authority for the local USB flashing station without making that unauthenticated
bench UI reachable through a LAN bind or reverse proxy.

Remote/production conductor:

```bash
export CONTROL_PASSWORD_HASH="$(
  .venv/bin/python -m control.auth hash-password
)"
CONTROL_CONDUCTOR=serial \
CONTROL_SERIAL_PORT=/dev/cu.usbserial-XXXX \
CONTROL_SERIAL_TIMEOUT_S=8.0 \
CONTROL_SERIAL_STATE_TIMEOUT_S=30.0 \
CONTROL_STATE_POLL_INTERVAL_S=15.0 \
CONTROL_DATA_DIR=/tmp/lightweave-control \
CONTROL_ALLOWED_ORIGINS=https://control.example.com \
CONTROL_REQUIRE_HTTPS=true \
CONTROL_ALLOW_NETWORK_CHANGES=false \
CONTROL_PASSWORD_HASH="$CONTROL_PASSWORD_HASH" \
.venv/bin/python -m uvicorn control.app:app --host 127.0.0.1 --port 8000 \
  --workers 1 --no-proxy-headers
```

Remote `serial` mode deliberately requires the complete authenticated HTTPS deployment
contract, even when a conductor adapter is injected. Use the named tunnel or a
reviewed local HTTPS proxy in front of this loopback listener; do not weaken the
serial-mode boundary for bench convenience. The production environment file and
service command are documented in [`deploy/pi/`](../deploy/pi/README.md).

By default the serial transport deasserts DTR/RTS after opening so peeking at a
running conductor does not intentionally reset it. Set
`CONTROL_SERIAL_RESET_ON_OPEN=1` only when you want normal serial-open reset
behavior.

Ordinary serial commands default to an 8-second timeout. Full state snapshots
have a separate 30-second budget because a populated 128-board response is
roughly 50 KiB at 115200 baud. The browser and WebSocket clients share recent
snapshots on a 15-second cadence instead of forcing a new inventory read after
each accepted mutation. If a response times out mid-frame, the transport
discards the rest of that newline-delimited frame before accepting a later
response, so one slow snapshot cannot corrupt the next request.

Open:

- Local UI: <http://127.0.0.1:8000/>
- OpenAPI: <http://127.0.0.1:8000/docs>

Production remote administration uses a loopback-only service behind a named
Cloudflare Tunnel and the control plane's shared-password session boundary.
See [`docs/REMOTE_ADMIN.md`](../docs/REMOTE_ADMIN.md) for the stable deployment
contract and [`plans/remote-administration.md`](../plans/remote-administration.md)
for implementation status.

## Test

```bash
.venv/bin/python -m pytest control/tests
pio test -e native
```

## Current Scope

- FastAPI app with HTTP + WebSocket state updates
- Mock conductor adapter and pyserial-backed JSON-line conductor adapter
- API-backed Map/Node List/Patterns/Operations UI shell
- Shared lantern detail sheet
- Actions for identify, assign, forget, replace, pattern changes, and blackout
- Desired-state acknowledgements that return after conductor persistence while
  performer convergence is observed by later fleet snapshots
- Reversible per-group Off/On controls and safe field sleep that pauses active
  OTA before leaving maintenance mode
- Shared-password sessions that protect HTTP, previews, uploads, and WebSockets
- Durable background field OTA with checkpoint repair, a six-hour retry window,
  per-performer verified staging and independent one-action activation, and
  normal show controls available throughout the transfer
- Persistent stores rooted by `CONTROL_DATA_DIR`
