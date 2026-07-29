# Remote administration

> **Status:** Building
> **Tracking issue:** underminedsk/lightweave#3 · **Created:** 2026-07-22 · **Last amended:** 2026-07-23 (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Give authorized operators a stable, browser-accessible control-plane URL over
Starlink without making the Raspberry Pi, internet connection, or cloud service
part of the show runtime. The simplest whole solution is a named Cloudflare
Tunnel plus one shared-password session gate in the existing FastAPI service;
building custom public ingress, user management, or firmware networking would
cost more and weaken the installation's existing offline resilience.

## Problem

The control plane already exposes the complete operator surface over HTTP and
WebSocket, and the real adapter already bridges those requests to the conductor
over structured USB serial (`control/app.py:create_app`,
`control/adapters.py:JsonLineSerialConductor`). However, production Pi packaging
is still absent: there is no `deploy/pi/` service definition or runbook, and the
documented launch command binds Uvicorn only for local development
(`control/README.md`).

Current behaviors need explicit handling before publishing the service:

- The FastAPI control plane has no authentication. Every UI, API, and WebSocket
  action is available to any client that can reach the listener.

- `control/app.py:install_ota_artifact` holds one HTTP request open for the
  complete field transfer, while `control/static/app.js:pollOtaInstallWhile`
  ties browser polling to that promise. A roughly 6-minute bench transfer was
  operator-observed during planning, longer than Cloudflare's normal 125-second
  proxy read timeout.
- The OTA handler holds `app.state.conductor_lock` for the transfer. Every normal
  serial-backed route enters that same lock through `conductor_call`, so a command
  submitted during OTA can wait past the proxy timeout and execute later after
  the operator believes it failed.
- `POST /api/network/wifi` and `POST /api/network/hotspot` are available whenever
  NetworkManager is present. On the single-radio Pi Zero 2 W deployment, either
  operation can disconnect the Starlink client and strand remote administration.
- The FastAPI app has no cross-origin mutation check, and `/ws` accepts a browser
  connection without validating its `Origin`. Once the application issues an
  authenticated browser session, it must still reject requests initiated by an
  unrelated site.
- `create_app` constructs the OTA, pattern, and calibration stores under relative
  `.control_*` paths. A field service therefore needs an explicit writable state
  directory separate from the read-only application checkout.

The ESP32 side does not need internet support. The conductor already persists
field configuration in NVS and performers continue rendering through loss of the
Pi or upstream connectivity (`docs/ARCHITECTURE.md` sections 5.2 and 8).

## Solution

Run the Pi Zero 2 W as a normal client of Starlink Wi-Fi. Install a remotely
managed named `cloudflared` tunnel that maps one public hostname to
`http://127.0.0.1:8000`. The application verifies one salted password hash from
the required root-owned service environment, then issues a random, process-local,
expiring browser session. There is no user database or separate password service.
Uvicorn remains loopback-only, so the public route has no Pi IP, dynamic DNS,
router port-forward, or direct Starlink-LAN origin to bypass.

Within the existing control plane:

- Gate the UI, HTTP API, and WebSocket behind the same application session.
  Store only an encoded slow password hash in `CONTROL_PASSWORD_HASH`; never
  store or log the plaintext password or commit a deployment hash to the repo.
- Issue an opaque random `__Host-lightweave_session` cookie with `Secure`,
  `HttpOnly`, `SameSite=Strict`, `Path=/`, and a bounded lifetime. Keep sessions
  in process memory, rate-limit failed logins, support logout, and invalidate all
  sessions on service restart or password rotation.
- Convert field OTA to one server-owned `asyncio.Task`. The POST performs bounded
  preflight, reserves one job, and returns `202`; GET remains the authoritative
  status/result surface. During that reservation, fail other serial-backed work
  promptly instead of letting it queue behind the transfer. Preserve the existing
  serial lock, chunk retry, alignment, readiness, and full-field verification
  logic.
- Add deployment settings for allowed browser origins and whether network
  mutations are enabled. The field service disables network mutation; bench
  development can retain the existing behavior.
- Validate present `Origin` headers on mutating HTTP requests and the WebSocket
  handshake without enabling CORS or breaking authenticated non-browser clients
  that send no `Origin`.
- Prevent the UI from being framed by another site so an authenticated operator
  cannot be clickjacked into a destructive same-origin action.
- Store mutable control-plane data under `/var/lib/lightweave`, separate from the
  read-only checkout under `/opt/lightweave`.
- Add a hardened systemd unit, non-secret environment example, and Pi runbook.
  Cloudflare owns the tunnel service and token separately from the FastAPI
  password configuration.

Alternatives considered:

- A tailnet was rejected for this operator path because it requires every browser
  device to install and join a private network; it remains viable for SSH or a
  future private-only deployment.
- A DNS A record, dynamic DNS, and router port forwarding were rejected because
  the tunnel removes the public-IP and Starlink-router dependency entirely.
- A second Wi-Fi adapter or Ethernet uplink was rejected because the accepted
  operating model allows a physical visit when upstream connectivity fails; an
  always-on Basketnet AP is not a first-release requirement.
- Cloudflare Access was rejected for the first release because the operator chose
  one shared application password over individual identities or an external
  identity policy.
- Browser Basic Auth at a reverse proxy was rejected because an application login
  and same-origin session cookie cover HTTP and WebSocket consistently without a
  second web service.
- A literal password or deployment hash in Python source was rejected because it
  couples credential rotation to a code change and publishes the offline-cracking
  target to every source checkout. The existing required root-owned environment
  file is configuration, not a separate password store.

## Relevant files

### Existing (verified 2026-07-22)

- `control/app.py` - owns all HTTP/WS routes, lifecycle tasks, network mutations,
  conductor serialization, OTA orchestration, and in-memory OTA job state.
- `control/static/app.js` - starts OTA, polls OTA state, renders network controls,
  and reconnects the same-origin WebSocket.
- `control/static/index.html` - contains the Operations controls for Wi-Fi,
  hotspot, and OTA actions.
- `control/tests/test_api.py` - covers HTTP/WS behavior, NetworkManager command
  dispatch, and the OTA success/retry/recovery matrix.
- `control/serial_transport.py` - opens the conductor without intentional
  DTR/RTS reset and reconnects after serial failure.
- `control/ota_store.py` - validates and persists the staged artifact before an
  install begins.
- `control/pattern_store.py` - persists saved patterns under a configurable
  directory.
- `control/calibration.py` - persists calibration state under a configurable
  directory.
- `control/requirements.txt` - pins the Python dependencies that must install on
  the Pi Zero 2 W's Raspberry Pi OS Python version.
- `control/README.md` - documents the current loopback development launch and
  real-serial environment.
- `docs/CONTROLPLANE.md` - records the API contract and OTA correctness model.
- `docs/ARCHITECTURE.md` - records the conductor-authoritative, Pi-optional
  runtime boundary.
- `docs/HANDOFF.md` - records test counts, hardware state, and outstanding Pi
  packaging.

### New

- `plans/remote-administration.md` - the single living execution artifact for
  this initiative.
- `control/auth.py` - dependency-free password-hash verification, login
  throttling, and process-local session lifecycle logic.
- `control/tests/test_auth.py` - deterministic unit coverage for password parsing,
  verification outcomes, throttling, expiry, logout, and restart.
- `control/static/login.html`, `control/static/login.js`, and
  `control/static/login.css` - the only unauthenticated browser surface and its
  explicitly allowlisted assets.
- `docs/REMOTE_ADMIN.md` - stable architecture and operator guidance derived
  from this plan; it contains no phase markers or duplicate execution status.
- `deploy/pi/lightweave-control.service` - boots the loopback FastAPI process as
  an unprivileged service and restarts it after failure.
- `deploy/pi/cloudflared.service` - pins the tunnel token-file invocation and
  restart behavior installed as `cloudflared.service`.
- `deploy/pi/lightweave.env.example` - defines the non-secret deployment contract
  for serial, data, origin, HTTPS, password-hash, and network-mutation settings.
- `deploy/pi/README.md` - installs and verifies Raspberry Pi OS dependencies,
  Starlink Wi-Fi, stable serial naming, systemd, Cloudflare Tunnel, login, logs,
  upgrades, and physical recovery.

## Questionables

- **Q:** Which browser front-door authentication should the first release use?
  Options: (a) Cloudflare Access with an explicit operator email allowlist and
  one-time PIN or existing identity provider / (b) a single shared Basic Auth
  password at a loopback reverse proxy behind the tunnel. Recommendation: (a)
  because it adds no application password store, supports individual revocation,
  and records operator login identity while remaining usable from any browser.
  **Decision (2026-07-23, Zach):** Use one shared password verified by the backend,
  with no separate password database or service. Implement this as a salted hash
  in required deployment configuration plus a secure application session so HTTP
  and WebSocket share one login.

- **Q:** What authorization boundary should remote firmware installation use?
  Options: (a) treat every allowlisted control-plane operator as a fully trusted
  unsigned-firmware publisher / (b) put OTA paths behind a narrower Access policy
  with stronger identity/MFA and a short session / (c) add release-signature
  verification on the Pi / (d) disable remote OTA and keep installation local-only.
  Recommendation: (b) for the first release because it is small, separates routine
  show control from arbitrary firmware execution, and keeps signature verification
  available if unsigned-firmware residual risk is unacceptable.
  **Decision (2026-07-23, Zach):** Every operator who can authenticate to the web
  UI may install firmware. Firmware signatures and a narrower OTA role are out of
  scope for this trust model.

- **Q:** Does the first release require an action audit? A shared credential cannot
  identify which person acted. Options: (a) keep no mutation audit / (b) append a
  local log containing session identifier, action, result, and timestamp.
  Recommendation: (a) for the stated minimal trust model; standard service logs
  still retain failures, but they are not presented as operator attribution.
  **Decision (2026-07-23, Zach):** Add no action-audit feature. Normal service
  logging remains operational diagnostics only.

- **Q:** Must OTA job state survive a Pi process restart? Options: (a) keep the
  task process-local and treat a mid-install restart as an explicit recovery state
  using the persisted staged artifact plus live firmware consistency / (b) persist
  a small accepted/terminal job journal and reconcile a prior nonterminal job at
  startup. Recommendation: (a) for the minimal first release because the existing
  updater already treats mixed firmware as recovery and does not support safely
  resuming an arbitrary in-flight serial call; the plan must not claim restart
  survival if this option is chosen.
  **Decision (2026-07-23, Zach):** Keep OTA job state process-local and use the
  persisted artifact plus live firmware consistency for interruption recovery.

## Phases

### Phase 1 - Authenticate and lock the public request boundary `[x]`

- [x] Add dependency-free `control/auth.py` using `hashlib.scrypt` with
  `n=131072`, `r=8`, `p=1`, `dklen=32`, `maxmem=268435456`, a random 16-byte
  salt, at most 1024 UTF-8 password bytes, and `hmac.compare_digest`. Encode only
  `scrypt$n=131072,r=8,p=1$<unpadded-base64url-salt>$<unpadded-base64url-digest>`
  and reject every other algorithm, parameter set, salt/digest length, padding,
  or malformed encoding at startup.
- [x] Implement `python -m control.auth hash-password` with two matching
  `getpass` prompts and a 12-character generation minimum. Never accept a
  password on argv/stdin, use a fast general-purpose hash, log the password/hash,
  or commit a deployment hash.
- [x] Add an explicit auth-manager dependency to `create_app`. The module-level
  app requires `CONTROL_PASSWORD_HASH` whenever `CONTROL_CONDUCTOR=serial`; the
  default mock app and tests inject a disabled/test manager. An injected conductor
  never disables auth implicitly, and no environment flag can disable auth in
  serial mode.
- [x] Add `GET /login`, `POST /api/auth/login`, `GET /api/auth/session`, and
  authenticated `POST /api/auth/logout`. Login accepts only
  `{"password":"..."}`, returns generic JSON `401` on bad credentials, or creates
  a 256-bit opaque session with a 12-hour absolute lifetime and sets
  `__Host-lightweave_session` as `Secure`, `HttpOnly`, `SameSite=Strict`,
  `Path=/`, with no `Domain`. Logout deletes the session, clears the cookie with
  matching attributes, and returns `204`.
- [x] Enforce a 2 KiB login-request limit in ASGI receive handling before JSON
  parsing, including missing, falsified, or chunked `Content-Length`; return `413`,
  count the attempt, and never invoke scrypt for oversized or malformed bodies.
- [x] Reserve attempts atomically before verification. Allow five failed attempts
  per canonical client IP in a rolling five-minute window, with no attacker-
  triggered global lockout. Run scrypt through `asyncio.to_thread` behind one
  nonblocking verification slot; return `429` without hashing when that slot or
  the client limit is occupied. This bounds scrypt memory to one 128 MiB job and
  keeps the event loop/WebSocket responsive.
- [x] Start Uvicorn with `--no-proxy-headers`. Trust a strictly parsed
  `CF-Connecting-IP` and exact `X-Forwarded-Proto` only when the unchanged socket
  peer is loopback; otherwise use the peer IP and disregard forwarded headers.
  With `CONTROL_REQUIRE_HTTPS=true` required in serial mode, refuse to render or
  accept login unless the trusted external scheme is `https`.
- [x] Default-deny the application boundary. Public routes are exactly
  `GET /login`, `GET /static/login.js`, `GET /static/login.css`,
  `GET /api/auth/session` (boolean only), and `POST /api/auth/login`. Every other
  HTTP route, including `/`, ordinary `/static/*`, `/api/*`, `/preview*`,
  `/review*`, `/docs`, `/redoc`, and `/openapi.json`, requires a live session;
  APIs return JSON `401`, while browser pages use a relative `303 /login`. Deny
  unauthenticated `/ws` before acceptance.
- [x] Associate each accepted WebSocket with its session. Close its sockets
  immediately on logout and from a lifecycle expiry reaper at the 12-hour
  deadline; revalidate before every publish so an expired session receives no
  later state. The main UI treats any `401` or authenticated socket closure as a
  transition to `/login`, not a toast/reconnect loop.

- [x] Add strict environment parsing for `CONTROL_ALLOWED_ORIGINS`,
  `CONTROL_ALLOW_NETWORK_CHANGES`, and `CONTROL_REQUIRE_HTTPS` in
  `control/app.py`. Parse the comma-separated origin list into exact
  `scheme://host[:port]` values, reject malformed values, and accept only explicit
  true/false spellings for booleans.
- [x] Make field/serial startup fail closed: require at least one allowed origin,
  require HTTPS, default network mutation off, and reject a missing or malformed
  required field setting. Preserve current mock/bench behavior only through
  explicit injected dependencies or development configuration.
- [x] Reject malformed, `null`, and unapproved `Origin` values on `POST`, `PUT`,
  `PATCH`, and `DELETE`; continue allowing non-browser clients that omit `Origin`
  and do not enable permissive CORS.
- [x] Validate the WebSocket `Origin` before `accept()` and deny an unapproved
  browser origin without exposing an accepted socket.
- [x] Emit `Content-Security-Policy: frame-ancestors 'none'` and
  `X-Frame-Options: DENY` on UI/API responses, plus
  `Strict-Transport-Security: max-age=31536000` on externally HTTPS responses.
- [x] Include `allow_changes` in `GET /api/network/wifi`, return `403` from both
  network mutation endpoints when disabled, and hide or disable their UI actions.
- [x] Add unit/API tests for the exact hash format and bounds, correct/incorrect
  passwords, malformed/missing field hash, wire-level body limits, generic
  failures, per-client throttling, concurrent verification, cookie flags, expiry,
  logout, service restart, trusted/untrusted proxy headers, HTTPS enforcement,
  and authenticated/unauthenticated HTTP and WebSocket access.
- [x] Add a registered-route inventory regression proving the public allowlist is
  exact, and tests proving logout and clock-driven expiry close an already-open
  authenticated WebSocket before any later publish. Cover every Origin, strict
  boolean, clickjacking/HSTS header, and network-mutation case.
- [x] Preserve the existing test suite by injecting the disabled/test auth manager
  explicitly. Use an HTTPS `TestClient` for auth cookie tests and pass its session
  cookie explicitly to `websocket_connect`; Starlette's default `ws://testserver`
  test handshake does not carry the Secure cookie.

**Validation gate** - do not exit this phase until every line passes; if a
command fails, fix the cause and re-run.

- [x] `.venv/bin/python -m pytest control/tests/test_auth.py control/tests/test_api.py -k 'auth or origin or websocket or wifi or hotspot'`
- [x] `.venv/bin/python -m pytest control/tests`
- [x] With a mock Uvicorn server, a configured same-origin browser can load state
  and WebSocket updates through an HTTPS `TestClient`, while a foreign Origin
  cannot mutate or read `/ws`; a mock HTTP request proves field login is rejected.
  Public-hostname browser proof belongs exclusively to Phase 4.

### Phase 2 - Detach OTA from the browser request `[x]`

- [x] Split `POST /api/operations/ota-install` into bounded synchronous preflight
  and a worker coroutine. Missing artifact, invalid job snapshot, and immediate
  readiness failures remain synchronous `400`/`503`; artifact reads, maintenance
  settle waits, transfer, reboot, and verification run only in the worker.
- [x] Under one OTA-start lock, atomically re-check that no job is active, publish
  the accepted in-memory state, retain a strong reference to the worker task, and
  then return `202 {"install": <accepted snapshot>}`. A concurrent start returns
  `409` and never invokes the adapter.
- [x] Make the worker own every terminal transition and consume its exception.
  Success records `running=false`, `complete=true`, message, and `completed_at`;
  any post-acceptance failure records `running=false`, `complete=false`, error,
  and `completed_at`. `GET /api/operations/ota-install` remains `200` and is the
  authoritative terminal-result surface.
- [x] Reserve conductor access for the OTA worker without allowing ordinary
  calls to queue. While reserved, return `423 Locked` immediately from every
  other serial-backed route, skip ticker serial polls, and reject artifact staging
  or OTA-mode changes. The OTA worker's own readiness and verification calls must
  use an explicit internal path that cannot deadlock on its reservation.
- [x] Apply the OTA-availability precondition before any handler mutates local
  state and then calls or publishes through the conductor. In particular,
  `POST /api/operations/power-monitor` must return `423` without changing its
  in-memory configuration. Tests prove both adapter calls and local state remain
  unchanged for every rejected route.
- [x] Disable serial-backed actions and artifact/OTA mode controls in the UI while
  the job is active; continue polling `GET /api/operations/ota-install` and render
  the explicit busy response if another browser has already reserved the
  conductor. Session status, logout, static UI, and OTA install status remain
  available during the reservation.
- [x] Keep OTA job state process-local. Cancel and await the task on graceful
  shutdown, mark the in-memory job interrupted, and document that an abrupt
  process restart returns to the existing persisted-artifact/live-firmware
  recovery flow rather than restoring or resuming the job.
- [x] Change the UI to start once and poll GET until a terminal state independent
  of the POST connection; surface the recorded terminal message/error. Preserve
  HTTP status in the API helper so `401`, `423`, and ordinary failures are
  distinguishable.
- [x] Make a fresh authenticated page opened mid-install treat `/api/state` `423`
  as OTA-busy, fetch and poll the install endpoint, render progress, and disable
  serial actions instead of aborting refresh before OTA status loads.
- [x] Run every OTA test inside a lifespan-managed `with TestClient(app)` block
  and use one bounded `wait_for_ota_terminal()` helper so the detached task is not
  canceled when the per-request portal closes. Preserve all retry, alignment,
  missing-node, node-failure, finalize-timeout, and post-reboot assertions.
- [x] Add regressions for immediate `202` before maintenance settling, duplicate
  `409`, initiator disconnect, task exception capture, shutdown interruption, and
  all terminal GET fields. For each OTA-busy route, assert `423` and prove its
  adapter method was never called and its local state did not change. Add a fresh-
  page mid-OTA regression or browser gate.

**Validation gate**

- [x] `.venv/bin/python -m pytest control/tests/test_api.py -k ota`
- [x] `.venv/bin/python -m pytest control/tests`
- [x] Start an OTA against the mock server, terminate the initiating browser
  request, reconnect, and observe the same job reach one terminal result through
  GET without a second `ota_begin`; opening a second fresh page mid-transfer also
  reaches that job despite `/api/state` returning `423`.

### Phase 3 - Package the Pi and tunnel deployment `[x]`

- [x] Add `CONTROL_DATA_DIR` and construct `OtaArtifactStore`, `PatternStore`, and
  `CalibrationStore` at `<dir>/ota`, `<dir>/patterns`, and `<dir>/calibration`;
  explicit injected stores override those defaults. Test exact paths and
  persistence across app restart so production never requires a writable checkout.
- [x] Target the current Raspberry Pi OS Lite 64-bit Trixie image for Pi Zero 2 W
  in the runbook. Document Python 3.13 virtualenv setup and keep every dependency
  pinned; actual ARM64 installation and reboot proof belongs to Phase 4.
- [x] Add `deploy/pi/lightweave-control.service` for code and virtualenv under
  `/opt/lightweave`, state under `/var/lib/lightweave` via
  `StateDirectory=lightweave`, `StateDirectoryMode=0700`, and a required
  root-owned mode-0600
  `EnvironmentFile=/etc/lightweave/control.env`.
- [x] Run exactly one loopback-bound Uvicorn worker as unprivileged user
  `lightweave` with `dialout` group access, `Restart=on-failure`, `RestartSec=5`,
  `TimeoutStopSec=180`, `NoNewPrivileges=true`, `UMask=0077`,
  `ProtectSystem=strict`, and no sudo grant. Limit writes to the state directory.
  Use exact `ExecStart=/opt/lightweave/.venv/bin/uvicorn control.app:app --host
  127.0.0.1 --port 8000 --workers 1 --no-proxy-headers`.
- [x] Add `deploy/pi/lightweave.env.example` with
  `CONTROL_CONDUCTOR=serial`, a `/dev/serial/by-path` conductor path,
  `CONTROL_SERIAL_RESET_ON_OPEN=0`, `CONTROL_DATA_DIR=/var/lib/lightweave`, exact
  HTTPS field origin, `CONTROL_REQUIRE_HTTPS=true`, a placeholder
  `CONTROL_PASSWORD_HASH`, and disabled network mutation; do not commit the
  deployment hash, tunnel credentials, or tokens.
- [x] Add `deploy/pi/README.md` with install/upgrade/rollback commands, Starlink
  client setup, stable serial discovery, Cloudflare named-tunnel route setup,
  password-hash generation and rotation, service/log inspection, and recovery
  through a local console or SSH from the Starlink LAN during a physical visit.
  Rotation replaces the hash and restarts the service, invalidating every session.
- [x] Before publishing the Cloudflare route, verify locally that unauthenticated
  HTTP and WebSocket requests are denied and, through an HTTPS `TestClient`, that
  valid login/logout works. The tunnel publishes only the authenticated loopback
  service; no Cloudflare Access policy is part of this release.
- [x] Require `cloudflared >= 2025.4.0`. Store the remotely managed tunnel token
  at `/etc/cloudflared/lightweave.token`, owned `root:cloudflared` mode 0640, and
  install the reviewed service as `cloudflared.service` with exact
  `ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel run --token-file
  /etc/cloudflared/lightweave.token`. Run as `User=cloudflared`,
  `Group=cloudflared` with `Restart=on-failure`, `RestartSec=5`,
  `NoNewPrivileges=true`, `ProtectSystem=strict`, `ProtectHome=true`, and
  `PrivateTmp=true`. Never place the token in argv, shell history, or the
  application environment.
- [x] Document that rollout must create a host-specific Cloudflare HTTP-to-HTTPS
  redirect for `$CONTROL_HOST` before exposing the route; zone-wide Always Use
  HTTPS is not part of this release. Include tunnel token routine/compromise
  rotation, connector deletion, and verification that only the expected connector
  is active; actual account configuration and proof belong to Phase 4.
- [x] Create `docs/REMOTE_ADMIN.md` as stable architecture and operator guidance
  that links this plan for execution status and contains no phase/status copy.
- [x] Update `control/README.md`, `docs/CONTROLPLANE.md`, and
  `docs/ARCHITECTURE.md` to point to the deployed shape without claiming hardware
  verification before it occurs.

**Validation gate**

- [x] `git diff --check`
- [x] `.venv/bin/python -m pytest control/tests`
- [x] `pio test -e native`
- [x] A controlled app restart preserves staged OTA artifact, saved patterns, and
  calibration beneath `/var/lib/lightweave`; `/opt/lightweave` remains read-only.
- [x] Static review confirms both service units use the exact commands, required
  environment/token files, least-privilege settings, and restart behavior above;
  installed-unit and reboot proof belongs to Phase 4.

### Phase 4 - Human-owned field rollout and recovery proof

This phase is a rollout gate owned by an operator with the Cloudflare account,
Starlink installation, Pi, and 3-board bench. It begins only after the code lane
lands; the builder supplies the runbook and records evidence but does not invent
account credentials or claim physical verification.

- [ ] Install the documented Raspberry Pi OS Lite 64-bit Trixie image on the Pi
  Zero 2 W, create the Python 3.13 virtualenv, install every pinned requirement,
  and run the complete control test suite.
- [ ] Create the named tunnel route and its required host-specific HTTP-to-HTTPS
  redirect before sharing the hostname; install the token file without exposing
  it in shell history and verify only the expected connector is active.
- [ ] Verify both installed unit files with `systemd-analyze verify`, confirm
  `cloudflared --version` is at least 2025.4.0, and reboot to prove FastAPI and the
  tunnel return without login.
- [ ] Remotely change a harmless pattern and verify the serial acknowledgement
  plus performer update.
- [ ] Disconnect Starlink and verify the field continues its stored pattern and
  power policy without Pi/internet participation.
- [ ] Restore Starlink and verify `cloudflared`, the page, and `/ws` recover
  without restarting the conductor.
- [ ] On the 3-board bench, stage firmware, enter maintenance, start OTA, close
  the browser after transfer begins, reconnect, and verify the same job completes
  with every expected performer firmware-consistent.
- [ ] Verify a logged-out browser cannot stage or install firmware and any
  logged-in session can; retain only redacted evidence and never record the
  password.
- [ ] Restart the Pi process during a bench OTA and verify the documented
  persisted-artifact/live-firmware recovery flow without claiming job resumption.
- [ ] Confirm the field deployment cannot remotely join Wi-Fi or start hotspot
  mode, and record the deployed hostname and service verification in
  `docs/HANDOFF.md` without storing credentials.

**Validation gate**

- [ ] `curl -sS -o /dev/null -D - http://$CONTROL_HOST/login` redirects to HTTPS
  before rendering login, and the same command over HTTPS includes one-year HSTS.
- [ ] `curl -sS -o /dev/null -w '%{http_code}' https://$CONTROL_HOST/api/state`
  returns `401` without a session;
  a browser with a valid password session receives the UI, `/api/state`, and `/ws`
  updates; wrong-password and expired sessions receive no control data.
- [ ] From the Starlink LAN,
  `! curl --connect-timeout 3 http://$PI_STARLINK_IP:8000/api/state` confirms the
  loopback-only origin is not directly reachable.
- [ ] `systemctl is-active lightweave-control cloudflared` reports both active
  after a Pi reboot and after Starlink reconnection.
- [ ] `systemd-analyze verify /etc/systemd/system/lightweave-control.service
  /etc/systemd/system/cloudflared.service` passes on the deployed Pi.
- [ ] The browser-disconnect OTA drill records one install, all expected nodes at
  the staged image size/CRC, and post-reboot firmware consistency.
- [ ] `.venv/bin/python -m pytest control/tests && pio test -e native`

## Proof of work

The implementation PR must keep the complete control test suite and native
firmware logic suite green. New regressions prove the default-deny HTTP/WS auth
boundary, password resource bounds, proxy/HTTPS rules, origin enforcement,
network gating, and every transition in the OTA task lifecycle. Existing API
tests receive explicit test auth, and existing OTA tests retain their behavioral
assertions after the endpoint changes; converting them to polling is not
permission to reduce coverage.

No new browser-test framework is required for this narrow vanilla-JS change.
Local proof uses HTTPS `TestClient` plus mock Uvicorn requests for password
login/logout, session expiry, WebSocket reconnection, disabled serial actions
during OTA, hidden network actions, and detached OTA polling. Public-hostname
browser and 3-board interruption proof belong exclusively to Phase 4. The Pi
service gate is executed on Raspberry Pi OS because macOS cannot validate the
installed systemd runtime.

## Lane map

| Lane | Dispatch issue | Phases | One-line scope | Marker mode | Status |
|---|---|---|---|---|---|
| Implementation lane | #6 | 1-3 | Control-plane contract, tests, and Pi deployment artifacts | solo | ready |

**Lanes:** One solo lane owns the OTA API/UI contract, authentication and
deployment safety settings, systemd contract, documentation, and all phase 1-3
gates. Splitting those coupled changes would duplicate integration verification
rather than shorten wall-clock time. Phase 4 is a subsequent human-owned rollout
gate, not a parallel builder lane.

## Amendments

- **2026-07-22** (Codex, Moda Create): Created the required living plan artifact
  and verified its code, test, and deployment context against the clean base.
- **2026-07-22** (Codex, Moda Review): Folded verified security, OTA concurrency,
  test-lifecycle, persistent-state, systemd, tunnel-token, and rollout findings
  into the plan; retained four policy choices for operator resolution and moved
  status to In review.
- **2026-07-23** (Zach, decisions relayed by Codex): Selected one shared backend
  password, authorized every logged-in operator for OTA, and selected process-local
  OTA state with explicit interruption recovery; declined action auditing.
- **2026-07-23** (Codex, Moda Review): Re-ran the Ready rubric with every
  questionable resolved, concrete phase gates, verified file/behavior claims, and
  one coherent code lane; approved the plan for merge and split.
- **2026-07-23** (Codex, Moda Split): Split the approved plan into one solo
  implementation lane, #6; retained phase 4 as a human-owned rollout gate.
- **2026-07-23** (Codex, Moda Ready re-review): Application-owned authentication
  exposed verified HTTPS, route-inventory, WebSocket-expiry, password-resource,
  proxy-header, test-harness, OTA-reload, and deployment ambiguities. Folded the
  mechanical fixes into an amendment and returned the plan to In review.
- **2026-07-23** (Codex, Moda Amendment review): Red-team and integration review
  returned LANDABLE, and the implementer Ready rubric returned READY after the
  shared-password contracts were made exact; restored Approved/ready status.

## Notes

- Cloudflare Tunnel setup:
  https://developers.cloudflare.com/tunnel/setup/
- Cloudflare proxy read timeout:
  https://developers.cloudflare.com/fundamentals/reference/connection-limits/
- Cloudflare Tunnel supports WebSockets:
  https://developers.cloudflare.com/cloudflare-one/faq/cloudflare-tunnels-faq/
- OWASP password storage guidance:
  https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- MDN secure cookie guidance:
  https://developer.mozilla.org/en-US/docs/Web/Security/Practical_implementation_guides/Cookies
- Cloudflare host-specific Single Redirects:
  https://developers.cloudflare.com/rules/url-forwarding/single-redirects/
- Cloudflare visitor headers:
  https://developers.cloudflare.com/fundamentals/reference/http-headers/
- Cloudflare tunnel run parameters:
  https://developers.cloudflare.com/tunnel/advanced/run-parameters/
- Uvicorn proxy-header settings:
  https://www.uvicorn.org/settings/
- Current Raspberry Pi OS images:
  https://www.raspberrypi.com/software/operating-systems/
- The domain and final hostname are deployment parameters, not source-controlled
  decisions. Use `control.example.com` only as a redacted example in repository
  files.
- Tunnel creation, DNS, password selection, Starlink credentials, and the final
  hardware acceptance drill require human-owned accounts or physical access;
  builders document and verify them but never commit their secrets.
