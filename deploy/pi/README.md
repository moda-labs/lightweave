# Raspberry Pi field deployment

This runbook packages the Lightweave control plane on a Raspberry Pi Zero 2 W.
It is the implementation guide for the stable contract in
[`docs/REMOTE_ADMIN.md`](../../docs/REMOTE_ADMIN.md). The human-owned rollout and
hardware proof remain in
[`plans/remote-administration.md`](../../plans/remote-administration.md), Phase 4.
After the first installation, normal updates follow the pull-based release
procedure in [`docs/RELEASING.md`](../../docs/RELEASING.md).

The production shape is deliberately narrow:

- Raspberry Pi OS Lite 64-bit Trixie, whose system Python is 3.13
- application and virtualenv under root-owned `/opt/lightweave`
- mutable state under `/var/lib/lightweave`
- one unprivileged, loopback-only Uvicorn worker
- one separately unprivileged `cloudflared` connector
- no inbound port forwarding and no Cloudflare Access policy

Never commit or log the application password hash or tunnel token. Do not place
the tunnel token in a command argument, shell history, application environment,
or the Lightweave checkout.

## 1. Prepare Raspberry Pi OS and Starlink

Use Raspberry Pi Imager to install the current **Raspberry Pi OS Lite (64-bit)
Trixie** image. In the Imager customization:

1. Set a unique hostname and administrative user.
2. Configure the Starlink Wi-Fi SSID and regulatory country.
3. Enable SSH only if it will be used from the Starlink LAN, preferably with a
   public key instead of password authentication.

The Pi is a normal Starlink Wi-Fi client; it does not need a public address,
dynamic DNS, router port forwarding, an Ethernet adapter, or an always-on
Basketnet access point. After first boot, verify the connection:

```bash
nmcli device status
nmcli connection show --active
ip address show wlan0
```

If Wi-Fi was not preconfigured, use NetworkManager's interactive prompt so the
passphrase is not placed in shell history:

```bash
sudo nmcli --ask device wifi connect 'REPLACE_WITH_STARLINK_SSID' ifname wlan0
```

Apply OS updates and install the base packages:

```bash
sudo apt-get update
sudo apt-get full-upgrade
sudo apt-get install ca-certificates curl git python3 python3-pip python3-venv
python3 --version
```

Stop if `python3 --version` is not Python 3.13.x. Reboot if the OS upgrade
installed a new kernel:

```bash
sudo systemctl reboot
```

Remote changes to `wlan0` are intentionally disabled in the field application.
Changing the Starlink connection is an on-site administrative operation.

## 2. Install the application

Create a locked-down service account. Membership in `dialout` is its only
supplementary privilege; do not give this account a password, login shell, or
sudo grant.

```bash
id --user lightweave >/dev/null 2>&1 ||
  sudo useradd --system --user-group --home-dir /nonexistent \
    --shell /usr/sbin/nologin lightweave
sudo usermod --append --groups dialout lightweave
```

Clone the repository, then check out an explicitly reviewed release tag or
commit. Replace `RELEASE_REF`; do not deploy a moving branch by accident.

```bash
sudo git clone https://github.com/underminedsk/lightweave.git /opt/lightweave
sudo git -C /opt/lightweave fetch --tags --prune
sudo git -C /opt/lightweave checkout --detach RELEASE_REF
sudo python3 -m venv /opt/lightweave/.venv
sudo /opt/lightweave/.venv/bin/python -m pip install --upgrade pip
sudo /opt/lightweave/.venv/bin/python -m pip install \
  --require-hashes --only-binary=:all: \
  --requirement /opt/lightweave/control/requirements.lock
sudo /opt/lightweave/.venv/bin/python -m pip check
sudo chown --recursive root:root /opt/lightweave
sudo chmod --recursive go-w /opt/lightweave
```

The direct dependencies are pinned in `control/requirements.txt`; the complete
transitive graph and accepted wheel hashes are committed in
`control/requirements.lock`. Keep the checkout root-owned: the service user only
needs to read and execute it.

## 3. Select the stable conductor serial device

Connect the conductor to the USB port that will be used in the field, then list
the persistent topology names:

```bash
ls -l /dev/serial/by-path/
```

Disconnect and reconnect the conductor to confirm which entry disappears and
returns. Use that complete `/dev/serial/by-path/...` path in `CONTROL_SERIAL_PORT`.
Keep the conductor on the same physical Pi/hub port because the by-path name
describes USB topology.

After the environment is installed, verify that the service account can open the
device:

```bash
sudo -u lightweave test -r /dev/serial/by-path/REPLACE_WITH_CONDUCTOR_PATH
sudo -u lightweave test -w /dev/serial/by-path/REPLACE_WITH_CONDUCTOR_PATH
```

## 4. Configure the application service

Generate the shared-password hash interactively from the deployed code:

```bash
cd /opt/lightweave
sudo -u lightweave .venv/bin/python -m control.auth hash-password
```

The command prompts twice without echoing the password. Copy the resulting
`scrypt$...` hash directly into the root-owned environment file; do not save it
in the repository or a shell command.

```bash
sudo install -d -o root -g root -m 0755 /etc/lightweave
sudo install -o root -g root -m 0600 \
  /opt/lightweave/deploy/pi/lightweave.env.example \
  /etc/lightweave/control.env
sudoedit /etc/lightweave/control.env
sudo install -o root -g root -m 0644 \
  /opt/lightweave/deploy/pi/lightweave-control.service \
  /etc/systemd/system/lightweave-control.service
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/lightweave-control.service
```

Replace all placeholders. `CONTROL_ALLOWED_ORIGINS` must be the exact public
HTTPS origin, with no path or trailing slash, for example
`https://control.example.com`. Keep:

```text
CONTROL_REQUIRE_HTTPS=true
CONTROL_ALLOW_NETWORK_CHANGES=false
CONTROL_DATA_DIR=/var/lib/lightweave
CONTROL_SERIAL_RESET_ON_OPEN=0
```

The unit requires `/etc/lightweave/control.env`; a missing file prevents startup.
`StateDirectory=lightweave` creates `/var/lib/lightweave` as mode 0700. Combined
with `ProtectSystem=strict`, that state directory is the service's only writable
persistent location. It contains `ota/`, `patterns/`, and `calibration/`.

Check the final ownership and permissions before starting:

```bash
sudo stat -c '%U:%G %a %n' /etc/lightweave/control.env /opt/lightweave
sudo systemctl enable --now lightweave-control.service
sudo systemctl status lightweave-control.service
sudo ss -ltnp 'sport = :8000'
```

Expected: the environment is `root:root 600`, the checkout is root-owned, and
Uvicorn listens only on `127.0.0.1:8000`, never `0.0.0.0:8000`.

## 5. Verify the private service boundary

Do this before creating a public hostname. Direct HTTP must not render the field
login because production requires an externally HTTPS request:

```bash
curl --include http://127.0.0.1:8000/login
curl --include http://127.0.0.1:8000/api/state
```

Also run the automated HTTPS `TestClient` coverage for valid login/logout,
session cookies, Origin enforcement, unauthenticated HTTP, and unauthenticated
WebSockets:

```bash
cd /opt/lightweave
sudo env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  -p no:cacheprovider \
  control/tests/test_auth.py control/tests/test_api.py \
  control/tests/test_remote_admin.py control/tests/test_remote_config.py \
  -k 'auth or origin or websocket'
```

Do not publish the route unless these checks pass. The tunnel publishes this
authenticated loopback service; Cloudflare Access is not part of this release.

## 6. Install the named Cloudflare Tunnel

Install `cloudflared` from Cloudflare's signed Debian repository:

```bash
sudo install -d -m 0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg |
  sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' |
  sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
sudo apt-get update
sudo apt-get install cloudflared
cloudflared --version
```

Version 2025.4.0 is the minimum because earlier releases do not support
`--token-file`. Verify the installed version:

```bash
test "$(cloudflared --version | awk '{print $3}')" != ""
dpkg --compare-versions "$(cloudflared --version | awk '{print $3}')" ge 2025.4.0
```

In the Cloudflare dashboard:

1. Go to **Networking > Tunnels** and create a named, remotely managed tunnel.
2. Add a published-application route for the chosen hostname.
3. Set its service URL to exactly `http://127.0.0.1:8000`.
4. Copy only the token value from the connector installation command into a
   password manager. Do not run the token-bearing command.
5. Do not add a Cloudflare Access policy.

Install the connector account, token file, and reviewed unit. Enter the token
through `sudoedit`, not a shell command:

```bash
id --user cloudflared >/dev/null 2>&1 ||
  sudo useradd --system --user-group --home-dir /nonexistent \
    --shell /usr/sbin/nologin cloudflared
sudo install -d -o root -g cloudflared -m 0750 /etc/cloudflared
sudo install -o root -g cloudflared -m 0640 /dev/null \
  /etc/cloudflared/lightweave.token
sudoedit /etc/cloudflared/lightweave.token
sudo install -o root -g root -m 0644 \
  /opt/lightweave/deploy/pi/cloudflared.service \
  /etc/systemd/system/cloudflared.service
sudo systemctl daemon-reload
sudo systemd-analyze verify \
  /etc/systemd/system/lightweave-control.service \
  /etc/systemd/system/cloudflared.service
sudo stat -c '%U:%G %a %n' /etc/cloudflared/lightweave.token
sudo systemctl enable --now cloudflared.service
sudo systemctl status cloudflared.service
```

Expected token ownership and mode are `root:cloudflared 640`. The service command
is exactly:

```text
/usr/bin/cloudflared --no-autoupdate tunnel run --token-file /etc/cloudflared/lightweave.token
```

## 7. Force HTTPS for only the control hostname

Before sharing or otherwise exposing the hostname, create one host-specific
Cloudflare **Single Redirect** rule. For a host named `control.example.com`, use:

```text
(http.request.scheme eq "http" and http.host eq "control.example.com")
```

Redirect to the same hostname and path with an `https` scheme, return 301, and
preserve the query string. Test both `http://control.example.com` and
`https://control.example.com`.

Do not enable zone-wide **Always Use HTTPS** for this rollout: it affects every
hostname in the zone rather than only the Lightweave control host.

## 8. Production checks

From an operator browser outside the Starlink LAN:

1. Confirm HTTP redirects to the exact HTTPS hostname.
2. Confirm an unauthenticated API request is denied and `/login` is the only
   unauthenticated page.
3. Log in, confirm live WebSocket updates, log out, and confirm the prior session
   no longer works.
4. Confirm Wi-Fi and hotspot mutation controls are disabled.
5. Confirm `/var/lib/lightweave/{ota,patterns,calibration}` contains mutable
   state and `/opt/lightweave` remains unchanged.

On the Pi:

```bash
systemctl is-active lightweave-control cloudflared
sudo ss -ltnp 'sport = :8000'
sudo journalctl -u lightweave-control -u cloudflared --since today
sudo systemctl show lightweave-control \
  -p User -p Group -p SupplementaryGroups -p ReadWritePaths
```

## 9. Logs and routine recovery

Inspect current and prior-boot logs:

```bash
sudo journalctl -u lightweave-control -f
sudo journalctl -u cloudflared -f
sudo journalctl -u lightweave-control -u cloudflared -b
sudo journalctl -u lightweave-control -u cloudflared -b -1
```

Common recovery sequence:

```bash
systemctl is-active NetworkManager
nmcli connection show --active
systemctl is-active lightweave-control cloudflared
sudo systemctl restart lightweave-control
sudo systemctl restart cloudflared
```

Loss of Starlink, Cloudflare, or the Pi removes remote administration but does
not stop the conductor or performers from running their stored show. If remote
access does not recover, visit the installation and use a directly attached
console or SSH from the Starlink LAN. From that local session, check `wlan0`, the
stable serial symlink, both units, and their journals. Do not attempt risky Wi-Fi,
tunnel, or password changes unless a physical recovery path is available.

An interrupted OTA task is not resumed after an application restart. The staged
artifact remains under `/var/lib/lightweave/ota`; use live firmware consistency
and the existing OTA recovery workflow before starting a new installation.

## 10. Password-hash rotation

Generate a replacement hash interactively:

```bash
cd /opt/lightweave
sudo -u lightweave .venv/bin/python -m control.auth hash-password
sudoedit /etc/lightweave/control.env
sudo systemctl restart lightweave-control
sudo systemctl status lightweave-control
```

Replace only `CONTROL_PASSWORD_HASH`, then restart. A restart invalidates every
existing browser session. Verify old credentials fail and the new password can
log in. Keep neither hash nor plaintext in tickets, chat, logs, or source control.

## 11. Tunnel-token and connector rotation

Anyone holding the token can start another connector for this tunnel. Rotate it
regularly and immediately after suspected disclosure.

For routine rotation during a maintenance window:

1. In **Networking > Tunnels**, select the Lightweave tunnel and rotate/refresh
   its token.
2. Copy the new token value into a password manager without running the displayed
   token-bearing install command.
3. Replace `/etc/cloudflared/lightweave.token` using `sudoedit`; retain
   `root:cloudflared` ownership and mode 0640.
4. Restart `cloudflared.service` and confirm the Pi connector becomes healthy.
5. In the tunnel connector list, delete stale connectors and verify exactly the
   expected Pi connector remains active.

With one connector, expect a brief remote-administration interruption. It does
not affect the show.

For a compromised token, rotate it first, then use the Cloudflare dashboard to
disconnect/delete **all** existing connectors so a process holding the old token
cannot remain attached. Replace the Pi token file locally, restart the service,
and verify that exactly one newly authenticated Pi connector becomes active.
Never delete the named tunnel itself when the intent is to delete its connectors.

## 12. Upgrade

After the first `v0.4.0` installation, enroll the Pi in the production release
channel:

```bash
sudo /opt/lightweave/deploy/pi/install-gitops.sh
sudo systemctl status lightweave-gitops.timer
sudo journalctl -u lightweave-gitops.service -n 100 --no-pager
```

The installer creates root-owned release state under
`/var/lib/lightweave-gitops`, installs the root-owned shared OTA/deployment lock, copies the
reconciler outside the mutable Git checkout, verifies the systemd units, enables
the five-minute timer, and performs one immediate check. Normal upgrades are
then authorized by merging a reviewed channel
promotion as documented in
[`docs/RELEASING.md`](../../docs/RELEASING.md). Do not pull or edit the detached
checkout by hand during normal operation.

The commands below are retained only as an on-site emergency procedure for a Pi
that cannot run the GitOps reconciler.

Choose and record a reviewed target commit. Back up state and retain the current
commit for rollback:

```bash
git -C /opt/lightweave rev-parse HEAD
sudo systemctl stop lightweave-control
sudo install -d -o root -g root -m 0700 /var/backups/lightweave
sudo tar -C /var/lib -czf \
  "/var/backups/lightweave/pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ).tgz" \
  lightweave
sudo git -C /opt/lightweave fetch --tags --prune
sudo git -C /opt/lightweave checkout --detach NEW_RELEASE_REF
sudo /opt/lightweave/.venv/bin/python -m pip install \
  --require-hashes --only-binary=:all: \
  --requirement /opt/lightweave/control/requirements.lock
sudo /opt/lightweave/.venv/bin/python -m pip check
sudo chown --recursive root:root /opt/lightweave
sudo chmod --recursive go-w /opt/lightweave
sudo install -o root -g root -m 0644 \
  /opt/lightweave/deploy/pi/lightweave-control.service \
  /etc/systemd/system/lightweave-control.service
sudo install -o root -g root -m 0644 \
  /opt/lightweave/deploy/pi/cloudflared.service \
  /etc/systemd/system/cloudflared.service
sudo systemctl daemon-reload
sudo systemd-analyze verify \
  /etc/systemd/system/lightweave-control.service \
  /etc/systemd/system/cloudflared.service
sudo systemctl start lightweave-control
sudo systemctl restart cloudflared
sudo systemctl status lightweave-control
sudo systemctl status cloudflared
```

Run the release's tests before or during a maintenance window, and verify login,
WebSocket state, serial connectivity, persisted OTA/pattern/calibration data, and
both journals before declaring the upgrade complete.

Upgrade `cloudflared` separately through its package repository:

```bash
sudo apt-get update
sudo apt-get install --only-upgrade cloudflared
cloudflared --version
sudo systemctl restart cloudflared
sudo systemctl status cloudflared
```

Re-check that the version is at least 2025.4.0 and that only the expected
connector is active.

## 13. Emergency manual rollback

Normal unhealthy deployments roll back automatically, and an intentional
rollback is performed by promoting an older immutable release. Use this manual
procedure only for on-site recovery when the reconciler itself cannot run.

Use the exact commit recorded before the upgrade:

```bash
sudo systemctl stop lightweave-control
sudo git -C /opt/lightweave checkout --detach PREVIOUS_RELEASE_COMMIT
sudo /opt/lightweave/.venv/bin/python -m pip install \
  --require-hashes --only-binary=:all: \
  --requirement /opt/lightweave/control/requirements.lock
sudo /opt/lightweave/.venv/bin/python -m pip check
sudo chown --recursive root:root /opt/lightweave
sudo chmod --recursive go-w /opt/lightweave
sudo install -o root -g root -m 0644 \
  /opt/lightweave/deploy/pi/lightweave-control.service \
  /etc/systemd/system/lightweave-control.service
sudo install -o root -g root -m 0644 \
  /opt/lightweave/deploy/pi/cloudflared.service \
  /etc/systemd/system/cloudflared.service
sudo systemctl daemon-reload
sudo systemd-analyze verify \
  /etc/systemd/system/lightweave-control.service \
  /etc/systemd/system/cloudflared.service
sudo systemctl start lightweave-control
sudo systemctl restart cloudflared
sudo systemctl status lightweave-control
sudo systemctl status cloudflared
```

Do not restore `/var/lib/lightweave` blindly. First check the release notes and
application logs for state-format compatibility. If restoration is required,
preserve the failed state separately and restore the pre-upgrade archive during
a maintenance window. Re-run the production checks after rollback.
