# Control Plane

Local web control plane for Do Baskets Dream. It defaults to a mock conductor
for UI/API work and can talk to a real conductor over newline-delimited JSON on
USB serial.

## Run

Use Python 3.13 or 3.12. Python 3.14 is too new for the pinned FastAPI/Pydantic
dependency stack today.

```bash
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/python -m pip install -r control/requirements.txt
.venv/bin/python -m uvicorn control.app:app --reload --host 127.0.0.1 --port 8000
```

Real conductor:

```bash
CONTROL_CONDUCTOR=serial \
CONTROL_SERIAL_PORT=/dev/cu.usbserial-XXXX \
.venv/bin/python -m uvicorn control.app:app --host 127.0.0.1 --port 8000
```

By default the serial transport deasserts DTR/RTS after opening so peeking at a
running conductor does not intentionally reset it. Set
`CONTROL_SERIAL_RESET_ON_OPEN=1` only when you want normal serial-open reset
behavior.

Open:

- UI: <http://127.0.0.1:8000/>
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
