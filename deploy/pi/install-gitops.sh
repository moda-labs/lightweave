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
install -d -o root -g lightweave -m 0750 /var/lib/lightweave-gitops
install -d -o lightweave -g lightweave -m 0700 /var/lib/lightweave/operations
if [ ! -e /var/lib/lightweave/operations/firmware-ota.lock ]; then
  install -o lightweave -g lightweave -m 0600 /dev/null \
    /var/lib/lightweave/operations/firmware-ota.lock
else
  chown lightweave:lightweave /var/lib/lightweave/operations/firmware-ota.lock
  chmod 0600 /var/lib/lightweave/operations/firmware-ota.lock
fi
if [ ! -f /etc/lightweave/gitops.env ]; then
  install -o root -g root -m 0644 \
    "$repo/deploy/pi/lightweave-gitops.env.example" \
    /etc/lightweave/gitops.env
fi
install -o root -g root -m 0755 \
  "$repo/deploy/pi/gitops_reconcile.py" \
  /usr/local/lib/lightweave/gitops_reconcile.py
install -o root -g root -m 0644 \
  "$repo/deploy/pi/lightweave-gitops.service" \
  /etc/systemd/system/lightweave-gitops.service
install -o root -g root -m 0644 \
  "$repo/deploy/pi/lightweave-gitops.timer" \
  /etc/systemd/system/lightweave-gitops.timer
systemctl daemon-reload
systemd-analyze verify \
  /etc/systemd/system/lightweave-gitops.service \
  /etc/systemd/system/lightweave-gitops.timer
systemctl enable --now lightweave-gitops.timer
systemctl start lightweave-gitops.service
systemctl --no-pager status lightweave-gitops.timer
