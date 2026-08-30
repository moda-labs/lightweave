#!/bin/sh
set -eu

repo=/opt/lightweave
if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' "install-gitops.sh must run as root" >&2
  exit 1
fi
if [ ! -f "$repo/deploy/pi/gitops_reconcile.py" ]; then
  printf '%s\n' "Lightweave is not installed at $repo" >&2
  exit 1
fi

install -d -o root -g root -m 0755 /etc/lightweave /usr/local/lib/lightweave
install -d -o root -g root -m 0700 /var/backups/lightweave
if [ -L /var/lib/lightweave-gitops ]; then
  printf '%s\n' "/var/lib/lightweave-gitops must not be a symlink" >&2
  exit 1
fi
install -d -o root -g lightweave -m 0750 /var/lib/lightweave-gitops
if [ -L /var/lib/lightweave-gitops/firmware-ota.lock ]; then
  printf '%s\n' "firmware OTA lock must not be a symlink" >&2
  exit 1
fi
if [ ! -e /var/lib/lightweave-gitops/firmware-ota.lock ]; then
  install -o root -g lightweave -m 0640 /dev/null \
    /var/lib/lightweave-gitops/firmware-ota.lock
elif [ ! -f /var/lib/lightweave-gitops/firmware-ota.lock ]; then
  printf '%s\n' "firmware OTA lock must be a regular file" >&2
  exit 1
else
  chown root:lightweave /var/lib/lightweave-gitops/firmware-ota.lock
  chmod 0640 /var/lib/lightweave-gitops/firmware-ota.lock
fi
running_commit=$(git -C "$repo" rev-parse HEAD)
case "$running_commit" in
  *[!0-9a-f]*|'')
    printf '%s\n' "cannot determine the installed full Git commit" >&2
    exit 1
    ;;
esac
if [ "${#running_commit}" -ne 40 ]; then
  printf '%s\n' "installed Git commit is not a full SHA" >&2
  exit 1
fi
printf '%s\n' "$running_commit" > /var/lib/lightweave-gitops/running-commit
chown root:lightweave /var/lib/lightweave-gitops/running-commit
chmod 0640 /var/lib/lightweave-gitops/running-commit
install -d -o root -g root -m 0755 "$repo/.venvs"
release_venv="$repo/.venvs/$running_commit"
if [ -d "$repo/.venv" ] && [ ! -L "$repo/.venv" ]; then
  if [ -e "$release_venv" ]; then
    printf '%s\n' "release environment already exists: $release_venv" >&2
    exit 1
  fi
  mv "$repo/.venv" "$release_venv"
fi
if [ ! -x "$release_venv/bin/python" ]; then
  printf '%s\n' "missing initial release environment: $release_venv" >&2
  exit 1
fi
"$release_venv/bin/python" -m pip check
ln -sfn "$release_venv" "$repo/.venv.new"
mv -Tf "$repo/.venv.new" "$repo/.venv"
{
  printf '%s\n' '.venv' '.venvs/'
} >> "$repo/.git/info/exclude"
if [ ! -f /etc/lightweave/gitops.env ]; then
  install -o root -g root -m 0644 \
    "$repo/deploy/pi/lightweave-gitops.env.example" \
    /etc/lightweave/gitops.env
fi
install -o root -g root -m 0755 \
  "$repo/deploy/pi/gitops_reconcile.py" \
  /usr/local/lib/lightweave/gitops_reconcile.py
install -o root -g root -m 0644 \
  "$repo/deploy/pi/lightweave-control.service" \
  /etc/systemd/system/lightweave-control.service
install -o root -g root -m 0644 \
  "$repo/deploy/pi/lightweave-provisioner.service" \
  /etc/systemd/system/lightweave-provisioner.service
install -o root -g root -m 0644 \
  "$repo/deploy/pi/lightweave-solix.service" \
  /etc/systemd/system/lightweave-solix.service
install -o root -g root -m 0644 \
  "$repo/deploy/pi/lightweave-gitops-recovery.service" \
  /etc/systemd/system/lightweave-gitops-recovery.service
install -o root -g root -m 0644 \
  "$repo/deploy/pi/lightweave-gitops.service" \
  /etc/systemd/system/lightweave-gitops.service
install -o root -g root -m 0644 \
  "$repo/deploy/pi/lightweave-gitops.timer" \
  /etc/systemd/system/lightweave-gitops.timer
systemctl daemon-reload
systemd-analyze verify \
  /etc/systemd/system/lightweave-control.service \
  /etc/systemd/system/lightweave-provisioner.service \
  /etc/systemd/system/lightweave-solix.service \
  /etc/systemd/system/lightweave-gitops-recovery.service \
  /etc/systemd/system/lightweave-gitops.service \
  /etc/systemd/system/lightweave-gitops.timer
systemctl restart lightweave-control.service
health_commit=
attempt=0
while [ "$attempt" -lt 30 ]; do
  health_commit=$(
    curl --fail --silent http://127.0.0.1:8000/api/health 2>/dev/null |
      python3 -c 'import json, sys; print(json.load(sys.stdin).get("commit") or "")' \
        2>/dev/null || true
  )
  [ "$health_commit" = "$running_commit" ] && break
  attempt=$((attempt + 1))
  sleep 1
done
if [ "$health_commit" != "$running_commit" ]; then
  printf '%s\n' "control health does not report the installed commit" >&2
  exit 1
fi
systemctl enable --now lightweave-gitops.timer
systemctl start lightweave-gitops.service
systemctl --no-pager status lightweave-gitops.timer
