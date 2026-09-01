# Remote PR testing on the Raspberry Pi

This runbook tests an exact pull-request commit on the field Pi through
Cloudflare SSH without replacing `/opt/lightweave`, changing the production
release pointer, sharing production state, or opening another public route.

The test control plane runs with a mock conductor in a transient systemd unit.
It listens only on Pi loopback port `8001`; an SSH tunnel exposes it only to the
operator computer. The production control plane remains on port `8000`.

Use [`SSH_ACCESS.md`](SSH_ACCESS.md) to configure the `basket-pi-remote` alias.
Normal immutable releases and production promotion remain governed by
[`RELEASING.md`](RELEASING.md).

## Safety rules

- Test the PR's exact 40-character commit, never a moving branch name.
- Keep the checkout and mutable state under PR-specific `/var/tmp` paths.
- Use `CONTROL_CONDUCTOR=mock`; do not compete with production for conductor
  serial access.
- Bind Uvicorn to `127.0.0.1`, never `0.0.0.0`.
- Do not source `/etc/lightweave/control.env` or copy production secrets into
  the test process.
- Do not edit `/opt/lightweave`, GitOps records, systemd unit files, or the
  production channel.
- Stop before a high-visibility show unless the test is explicitly authorized.
- Always run the cleanup section, even after a failed test.

This workflow can share host hardware such as the audio device. If the released
production control plane already owns that hardware, stop and schedule a brief
maintenance window instead of running both players at once.

## 1. Resolve and review the exact PR commit

Run these commands on the operator computer. Replace `54` with the PR number.

```bash
PR_NUMBER=54
PR_COMMIT="$(gh pr view "$PR_NUMBER" --json headRefOid -q .headRefOid)"
test "${#PR_COMMIT}" -eq 40
printf 'PR #%s commit %s\n' "$PR_NUMBER" "$PR_COMMIT"
gh pr checks "$PR_NUMBER"
```

Do not continue if required checks are failing or if the displayed commit is
not the commit intended for the hardware test.

## 2. Check the Pi without changing it

Confirm Cloudflare SSH access and inspect the production boundary:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=20 basket-pi-remote '
  hostname
  systemctl is-active lightweave-control lightweave-gitops.timer
  sudo -n git -C /opt/lightweave rev-parse HEAD
  df -h /var/tmp
  ss -ltn | grep -E ":800[01] " || true
'
```

For an audio change, also inspect the decoder, account permissions, and ALSA
devices:

```bash
ssh basket-pi-remote '
  command -v git-lfs
  command -v mpg123 || true
  id -nG lightweave
  aplay -l
'
```

The expected analog output on the current Pi is card 0, device 0, selected as
`hw:0,0`. Re-check `aplay -l` after hardware or OS changes instead of assuming
that identifier is permanent.

If an audio PR requires missing prerequisites, install only the declared
packages and group access:

```bash
ssh basket-pi-remote '
  sudo apt-get update
  sudo apt-get install -y git-lfs mpg123
  sudo usermod --append --groups audio lightweave
  command -v mpg123
  getent group audio
'
```

These package and group changes persist after the test. Keep them when the
feature will ship. Do not automatically uninstall shared production
prerequisites during cleanup.

## 3. Create an isolated checkout

Open an interactive remote shell:

```bash
ssh basket-pi-remote
```

Set the same PR number and commit printed in step 1. The validation guards make
the later cleanup refuse unexpected paths.

```bash
PR_NUMBER=54
PR_COMMIT=REPLACE_WITH_FULL_40_CHARACTER_SHA

case "$PR_NUMBER" in
  ''|*[!0-9]*) echo "invalid PR number" >&2; exit 1 ;;
esac
case "$PR_COMMIT" in
  ''|*[!0-9a-f]*) echo "invalid PR commit" >&2; exit 1 ;;
esac
test "${#PR_COMMIT}" -eq 40

TEST_ROOT="/var/tmp/lightweave-pr-${PR_NUMBER}"
TEST_STATE="/var/tmp/lightweave-pr-${PR_NUMBER}-state"
TEST_UNIT="lightweave-pr${PR_NUMBER}-test"
TEST_PORT=8001

case "$TEST_ROOT" in
  /var/tmp/lightweave-pr-[0-9]*) ;;
  *) echo "unsafe test root" >&2; exit 1 ;;
esac
case "$TEST_STATE" in
  /var/tmp/lightweave-pr-[0-9]*-state) ;;
  *) echo "unsafe test state" >&2; exit 1 ;;
esac

test ! -e "$TEST_ROOT"
test ! -e "$TEST_STATE"
```

Clone without automatically downloading every Git LFS object, then fetch the
GitHub PR ref and prove that it matches the reviewed commit:

```bash
GIT_LFS_SKIP_SMUDGE=1 git clone --no-checkout \
  https://github.com/moda-labs/lightweave.git "$TEST_ROOT"
git -C "$TEST_ROOT" fetch --depth=1 origin \
  "refs/pull/${PR_NUMBER}/head"
test "$(git -C "$TEST_ROOT" rev-parse FETCH_HEAD)" = "$PR_COMMIT"
GIT_LFS_SKIP_SMUDGE=1 git -C "$TEST_ROOT" checkout --detach "$PR_COMMIT"
test "$(git -C "$TEST_ROOT" rev-parse HEAD)" = "$PR_COMMIT"
```

Pull only the large assets needed by the feature. For soundtrack testing:

```bash
git -C "$TEST_ROOT" lfs pull --include='sound/*.mp3'
git -C "$TEST_ROOT" lfs ls-files
ls -lh "$TEST_ROOT"/sound/*.mp3
```

If Cloudflare SSH disconnects during the transfer, reconnect, restore the PR and
path variables above, verify the exact paths again, and rerun `git lfs pull`. The
operation resumes safely. A real MP3 is tens or hundreds of megabytes; a file
around 130 bytes is still an LFS pointer.

## 4. Select an isolated Python environment

Reusing the production virtual environment is safe only when the PR's locked
Python graph is byte-for-byte identical:

```bash
if cmp -s /opt/lightweave/control/requirements.lock \
    "$TEST_ROOT/control/requirements.lock"; then
  TEST_PYTHON=/opt/lightweave/.venv/bin/python
else
  python3 -m venv "$TEST_ROOT/.venv"
  "$TEST_ROOT/.venv/bin/python" -m pip install \
    --require-hashes --only-binary=:all: \
    --find-links "$TEST_ROOT/control/wheels" \
    --requirement "$TEST_ROOT/control/requirements.lock"
  "$TEST_ROOT/.venv/bin/python" -m pip check
  TEST_PYTHON="$TEST_ROOT/.venv/bin/python"
fi
test -x "$TEST_PYTHON"
```

The second path takes longer on a Pi Zero 2 W, but it tests dependency changes
without mutating the production environment.

## 5. Run feature-specific preflight checks

For soundtrack changes, run the PR's muted decoder/output check as the real
service account. This opens the configured ALSA output without emitting the
track:

```bash
sudo -u lightweave env \
  PYTHONPATH="$TEST_ROOT" \
  CONTROL_AUDIO_DIR="$TEST_ROOT/sound" \
  CONTROL_AUDIO_DEVICE=hw:0,0 \
  "$TEST_PYTHON" -m control.audio_player check
```

No output and exit status zero means the default LFS payload decoded and the
service account opened the configured output. A silent preflight proves the
software and ALSA path, not speaker wiring or audible volume; a person must
confirm those at the installation.

## 6. Start the isolated control plane

Refuse an already-running test with the same name, create separate mutable
state, and launch a transient service. `RuntimeMaxSec` stops the process after
two hours even if the SSH session is lost.

```bash
if systemctl is-active --quiet "$TEST_UNIT"; then
  echo "$TEST_UNIT is already running" >&2
  exit 1
fi

sudo install -d -o lightweave -g lightweave -m 0700 "$TEST_STATE"
sudo systemd-run \
  --unit="$TEST_UNIT" \
  --collect \
  --property=User=lightweave \
  --property=Group=lightweave \
  --property=SupplementaryGroups='dialout audio' \
  --property=WorkingDirectory="$TEST_ROOT" \
  --property=RuntimeMaxSec=2h \
  --setenv=PYTHONPATH="$TEST_ROOT" \
  --setenv=CONTROL_CONDUCTOR=mock \
  --setenv=CONTROL_DATA_DIR="$TEST_STATE" \
  --setenv=CONTROL_ALLOW_NETWORK_CHANGES=false \
  --setenv=CONTROL_REQUIRE_HTTPS=false \
  --setenv=CONTROL_AUDIO_DIR="$TEST_ROOT/sound" \
  --setenv=CONTROL_AUDIO_DEVICE=hw:0,0 \
  "$TEST_PYTHON" -m uvicorn control.app:app \
    --host 127.0.0.1 --port "$TEST_PORT" --workers 1 --no-proxy-headers
```

The Pi may need several seconds to import the application. Poll instead of
treating the first refused connection as a failure:

```bash
ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS "http://127.0.0.1:${TEST_PORT}/api/health"; then
    ready=1
    break
  fi
  sleep 2
done
test "$ready" = 1

systemctl status "$TEST_UNIT" --no-pager --full
curl -fsS "http://127.0.0.1:${TEST_PORT}/api/audio"
```

If the tested PR has no audio feature, omit the two `CONTROL_AUDIO_*` settings
and replace the audio API check with the relevant endpoint.

## 7. Open the test UI through SSH

Keep the remote shell open. In a second terminal on the operator computer, run:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 18001:127.0.0.1:8001 basket-pi-remote
```

Then open <http://127.0.0.1:18001>. Closing that SSH command removes local
browser access; it does not stop the transient service. No Cloudflare HTTP route
or firewall change is required.

The test process uses a mock conductor and intentionally has no production
login configuration. Its unauthenticated HTTP listener remains reachable only
through Pi loopback and the authenticated SSH tunnel.

## 8. Exercise the feature

For the soundtrack player, verify:

1. Overview names **Baskets Soundscape V4**, reports a changing position, and
   says the loop is playing.
2. Sound lists every MP3 with the expected duration.
3. Pause freezes the displayed position for at least two polling intervals.
4. Selecting another track while paused keeps it paused and resets position.
5. Play starts the selected track and position advances.
6. Restart returns position to the beginning.
7. Selecting Soundscape V4 restores the intended default.
8. An operator at the installation confirms the analog jack is audible and the
   mixer/amplifier level is suitable.

The same controls can be checked directly through the tunnel:

```bash
curl -fsS http://127.0.0.1:18001/api/audio
curl -fsS -X POST http://127.0.0.1:18001/api/audio/pause
curl -fsS -X POST \
  -H 'Content-Type: application/json' \
  --data '{"track_id":"baskets-final-boat.mp3"}' \
  http://127.0.0.1:18001/api/audio/select
curl -fsS -X POST http://127.0.0.1:18001/api/audio/play
curl -fsS -X POST http://127.0.0.1:18001/api/audio/restart
```

Restore V4 before completing the test:

```bash
curl -fsS -X POST \
  -H 'Content-Type: application/json' \
  --data '{"track_id":"baskets-soundscape-v4.mp3"}' \
  http://127.0.0.1:18001/api/audio/select
```

### Persistence check

Pause the player, restart only the transient service, and confirm it remains
paused:

```bash
curl -fsS -X POST http://127.0.0.1:18001/api/audio/pause
ssh basket-pi-remote 'sudo systemctl restart lightweave-pr54-test'
```

Wait for port 18001 to recover, then:

```bash
curl -fsS http://127.0.0.1:18001/api/audio
curl -fsS -X POST http://127.0.0.1:18001/api/audio/play
```

Use the actual test unit name if the PR number is not 54.

## 9. Capture final evidence

On the Pi, confirm the test has no warnings and production stayed healthy:

```bash
sudo journalctl -u "$TEST_UNIT" --since '15 minutes ago' \
  --priority=warning --no-pager
systemctl is-active lightweave-control lightweave-gitops.timer
curl -fsS http://127.0.0.1:8000/api/health
curl -fsS "http://127.0.0.1:${TEST_PORT}/api/health"
git -C "$TEST_ROOT" rev-parse HEAD
```

Record the exact commit, checks performed, backend errors, and whether audible
hardware output was confirmed. Do not claim the jack passed solely because the
process remained alive.

## 10. Clean up

Stop the SSH tunnel with `Ctrl-C`. In the Pi shell, re-establish and validate
the identifiers before deleting anything:

```bash
PR_NUMBER=54
TEST_ROOT="/var/tmp/lightweave-pr-${PR_NUMBER}"
TEST_STATE="/var/tmp/lightweave-pr-${PR_NUMBER}-state"
TEST_UNIT="lightweave-pr${PR_NUMBER}-test"

case "$PR_NUMBER" in
  ''|*[!0-9]*) echo "invalid PR number" >&2; exit 1 ;;
esac
case "$TEST_ROOT" in
  /var/tmp/lightweave-pr-[0-9]*) ;;
  *) echo "unsafe test root" >&2; exit 1 ;;
esac
case "$TEST_STATE" in
  /var/tmp/lightweave-pr-[0-9]*-state) ;;
  *) echo "unsafe test state" >&2; exit 1 ;;
esac

sudo systemctl stop "$TEST_UNIT" 2>/dev/null || true
sudo rm -rf -- "$TEST_ROOT" "$TEST_STATE"

test ! -e "$TEST_ROOT"
test ! -e "$TEST_STATE"
systemctl is-active lightweave-control lightweave-gitops.timer
```

This removes only the PR checkout, isolated state, and transient service. It
does not change the production checkout, production data, release pointer,
installed packages, or account groups.

## Troubleshooting

### `ModuleNotFoundError: No module named 'control'`

The Python process is not resolving code from the PR checkout. Set
`PYTHONPATH="$TEST_ROOT"` and ensure systemd's `WorkingDirectory` is the same
exact checkout.

### The first health check refuses the connection

Pi Zero startup can take several seconds. Use the bounded polling loop and then
inspect:

```bash
systemctl status "$TEST_UNIT" --no-pager --full
sudo journalctl -u "$TEST_UNIT" -n 100 --no-pager
```

### MP3 files are tiny or the API says they are LFS pointers

Resume the scoped LFS download:

```bash
git -C "$TEST_ROOT" lfs pull --include='sound/*.mp3'
ls -lh "$TEST_ROOT"/sound/*.mp3
```

### ALSA reports a busy or missing device

Re-run `aplay -l`, verify `CONTROL_AUDIO_DEVICE`, and inspect current owners:

```bash
aplay -l
sudo fuser -v /dev/snd/* || true
```

Do not stop an unknown process. If the production control plane owns the audio
device, schedule a maintenance window or test after stopping the isolated
player.

### Git reports dubious ownership for `/opt/lightweave`

The production checkout is intentionally root-owned. Use `sudo git -C
/opt/lightweave ...` for read-only inspection. Do not add a global
`safe.directory` exception and do not modify the checkout.

### Port 8001 or local port 18001 is already occupied

Inspect the owner before choosing another port:

```bash
ss -ltnp | grep ':8001 ' || true
lsof -nP -iTCP:18001 -sTCP:LISTEN || true
```

Keep the Pi listener on `127.0.0.1`, and use the same adjusted port consistently
in the transient service, SSH forward, and test commands.

## Passing criteria

A remote Pi test passes when the exact reviewed commit is running, the intended
hardware or API path succeeds, the browser flow works through loopback
forwarding, restart behavior is correct, the transient unit has no relevant
warnings, production remains healthy, and cleanup removes every temporary
resource. Hardware output that cannot be observed remotely remains an explicit
human verification item.
