# Remote administration

This document records the stable production architecture and operator contract for remote administration.
Execution status and implementation gates live in [`plans/remote-administration.md`](../plans/remote-administration.md).
Installation, upgrade, rollback, and recovery commands live in
[`deploy/pi/README.md`](../deploy/pi/README.md).
The release-authoring and production-promotion procedure lives in
[`RELEASING.md`](RELEASING.md).

## Runtime boundary

Remote administration is optional infrastructure, not part of the show runtime.
The conductor remains authoritative for layout and show configuration, and performers continue rendering if the Pi, Starlink, Cloudflare, or an operator browser disappears.

```text
operator browser
    |
    | HTTPS + application login
    v
control.example.com
    |
    | named Cloudflare Tunnel
    v
cloudflared on Raspberry Pi
    |
    | http://127.0.0.1:8000
    v
FastAPI control plane
    |
    | USB serial JSON
    v
conductor ESP32 --ESP-NOW--> field
```

## Internet ingress

The Raspberry Pi Zero 2 W joins Starlink Wi-Fi as a normal client.
A named Cloudflare Tunnel maps one public hostname to `http://127.0.0.1:8000`.
The tunnel connects outbound from the Pi, so deployment requires no public Pi address, dynamic DNS, or router port forwarding.

Uvicorn binds only to `127.0.0.1:8000`.
It must never bind to `0.0.0.0` in the field because that would expose the control plane directly to other Starlink LAN clients.

Cloudflare Access is not part of this release.
Authentication is enforced by the FastAPI application so the same session boundary protects browser pages, APIs, previews, uploads, documentation, and WebSockets.

## Authentication contract

The field service requires one shared operator password represented by a salted slow hash in root-owned deployment configuration.
The plaintext password and deployment hash must never be logged or committed.
There is no user database or separate password service.

Successful login creates an opaque process-local session cookie named `__Host-lightweave_session`.
The cookie is `Secure`, `HttpOnly`, `SameSite=Strict`, scoped to `Path=/`, has no `Domain`, and has a bounded lifetime.
Logout, password rotation, service restart, and expiry invalidate sessions.
Failed logins are rate-limited.

Only the login page, its explicitly allowlisted assets, session status, login endpoint,
and the health endpoint are public. The health response intentionally exposes
only `ok`, the running release version, and the exact Git commit so the root
reconciler can verify a deployment; it contains no operator or field state.
Every other HTTP route requires a live session.
Unauthenticated WebSockets are denied before acceptance, and authenticated sockets close when their session logs out or expires.

Every authenticated operator has the same permissions, including field-wide OTA.
This shared-credential model does not provide per-person attribution, and the first release does not add an action-audit system.

## Browser and request protections

The field service fails closed unless it has an exact allowed HTTPS origin, HTTPS enforcement, and authentication configuration.
Mutating browser requests must present an allowed `Origin`.
The WebSocket handshake uses the same origin allowlist.
Authenticated non-browser clients may omit `Origin`.
Permissive CORS is not enabled.

Responses prevent framing with `Content-Security-Policy: frame-ancestors 'none'` and `X-Frame-Options: DENY`.
Externally HTTPS responses include HSTS.
Uvicorn runs with proxy-header processing disabled.
The application trusts Cloudflare forwarding headers only when the unchanged socket peer is loopback.

## Remote operation behavior

Field OTA runs as a server-owned background job rather than depending on one long-lived browser request.
The start request performs bounded preflight and returns `202 Accepted`.
`GET /api/operations/ota-install` remains the authoritative progress and terminal-result endpoint.
A second start returns `409 Conflict`.

While OTA owns the conductor, other serial-backed operations fail promptly with `423 Locked` instead of queuing and executing after the operator believes they failed.
The staged artifact persists, but the in-memory OTA job does not survive a Pi process restart.
An interrupted update returns to the existing staged-artifact and live-firmware-consistency recovery workflow.

Routine serial-backed operator writes return when the conductor has accepted
and persisted the requested desired state; performer delivery is observed later
through periodic fleet snapshots. Full snapshots use a separate longer serial
budget and are shared across browser sessions. Sleeping the field pauses an
active OTA at a safe command boundary, verifies that OTA maintenance ended, and
then sends the force-sleep policy. Automatic reconciliation stays suppressed
while that override is active.

The single-radio Pi must not change its own field network remotely.
`CONTROL_ALLOW_NETWORK_CHANGES` defaults off in field and serial deployment, and a missing or malformed field setting fails startup.
Bench development enables network mutation only through explicit development configuration or injected test dependencies.

## Service and data layout

Application code and the Python virtual environment live under read-only `/opt/lightweave`.
Mutable OTA, pattern, and calibration data live under `/var/lib/lightweave`;
root-owned release records and staged release firmware live under
`/var/lib/lightweave-gitops` and are only group-readable by the service.
The FastAPI service runs as an unprivileged `lightweave` user with serial access and write access only to its state directory.

`cloudflared` runs as a separate unprivileged service.
Its tunnel token is stored outside the application environment in a restricted root-owned file.
Tunnel credentials and application password configuration remain separate.

## Pull-based releases

The Pi polls an HTTPS production-channel document from GitHub every five minutes.
That document points to one hash-pinned, immutable release manifest. The
reconciler verifies the approved repository, exact tag and full commit, downloads
and verifies the canonical firmware, backs up mutable state, deploys the control
plane, and rolls code back automatically if the loopback health check fails.
The health check must report the exact promoted commit. A shared operation lock
defers reconciliation while a manual ESP32 OTA is active.

Reconciliation changes only the Pi software and staged firmware artifact. It
never starts ESP32 OTA. An authenticated operator still chooses a maintenance
window and starts field OTA against the frozen online performer cohort; offline
rows remain deferred for a later run. The Operations UI reports control-plane
and field-firmware deployment states separately and shows their separate release
notes.

The field service environment includes:

```text
CONTROL_CONDUCTOR=serial
CONTROL_SERIAL_PORT=/dev/serial/by-path/<conductor-path>
CONTROL_SERIAL_RESET_ON_OPEN=0
CONTROL_SERIAL_TIMEOUT_S=8.0
CONTROL_SERIAL_STATE_TIMEOUT_S=30.0
CONTROL_STATE_POLL_INTERVAL_S=15.0
CONTROL_DATA_DIR=/var/lib/lightweave
CONTROL_ALLOWED_ORIGINS=https://control.example.com
CONTROL_REQUIRE_HTTPS=true
CONTROL_ALLOW_NETWORK_CHANGES=false
CONTROL_PASSWORD_HASH=<generated-scrypt-hash>
```

## Failure and recovery

Loss of Starlink or Cloudflare removes remote administration but does not affect the running show.
A Pi or FastAPI restart invalidates browser sessions and reconnects to the conductor after service recovery.
A USB flap is handled by service restart and serial reconnection.

Physical recovery uses a directly attached console or SSH from the Starlink LAN
during an on-site visit. Connection details, key enrollment, and the explicit
internet-SSH boundary are in [`SSH_ACCESS.md`](SSH_ACCESS.md).
Changing `wlan0`, tunnel credentials, or the application password is an administrative operation that may require physical access if performed incorrectly.

Password rotation replaces the stored hash and restarts the FastAPI service.
Tunnel-token rotation follows Cloudflare's connector rotation procedure and verifies that only the expected connector remains active.

## Production exposure checklist

Before publishing the hostname:

1. Confirm Uvicorn listens only on loopback.
2. Confirm unauthenticated HTTP and WebSocket requests are denied.
3. Confirm login, logout, expiry, origin checks, anti-framing headers, and HTTPS enforcement.
4. Confirm Wi-Fi and hotspot mutations are disabled.
5. Confirm mutable data is written under `/var/lib/lightweave`, not the checkout.
6. Confirm the tunnel token and password hash are outside the repository with restricted permissions.
7. Disconnect Starlink and verify the conductor and performers continue their stored show.
8. Reconnect Starlink and verify the tunnel, browser session flow, and WebSocket recover.
9. Start a bench OTA, close the initiating browser, reconnect, and verify the same background job reaches a correct terminal state.
