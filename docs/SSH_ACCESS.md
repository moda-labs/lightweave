# Raspberry Pi SSH access

This guide covers administrative shell access to the field Raspberry Pi. It is
separate from the Lightweave web control plane and its application password.

## Current connection details

The deployed Pi currently uses:

```text
hostname: baskets.local
user:     baskets
SSH:      port 22, public-key authentication available
```

`baskets.local` is an mDNS name. It works only when the operator computer is on
the same LAN as the Pi, such as the home Wi-Fi during development or the
Starlink LAN in the field. Its DHCP address can change, so do not record the
current numeric IP as the normal connection method.

The current Ed25519 SSH host-key fingerprint, recorded on 2026-08-01, is:

```text
SHA256:DaAl/Ix4RDHi/5srsInGCGSiJflWIW+hDyCSrBbmq4E
```

If the Pi is reimaged, its host key will change. Update this record only after
verifying the replacement fingerprint from a physically attached console or an
already trusted administrative session.

## Connect from the same LAN

From a terminal on an operator computer:

```bash
ssh baskets@baskets.local
```

For a non-interactive connectivity check that will never prompt for a password:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=8 baskets@baskets.local \
  'hostname; uptime'
```

On the first connection from a new computer, compare the displayed host-key
fingerprint with the trusted value above before accepting it. If OpenSSH warns
that the key changed, stop and verify the Pi locally. Do not blindly remove the
old `known_hosts` entry.

An optional local alias makes the command easier to remember. Add this to
`~/.ssh/config` on the operator computer:

```sshconfig
Host basket-pi
  HostName baskets.local
  User baskets
  IdentityFile ~/.ssh/REPLACE_WITH_OPERATOR_PRIVATE_KEY
  IdentitiesOnly yes
```

Then connect with:

```bash
ssh basket-pi
```

The private key stays on the operator computer and must never be copied into
this repository.

## Add an operator computer

Prefer one key per operator computer so a lost key can be removed without
affecting every administrator. Generate an Ed25519 key on the new computer if it
does not already have a dedicated key:

```bash
ssh-keygen -t ed25519 -a 64 -f ~/.ssh/lightweave_baskets
```

From a computer that already has trusted access, append the new computer's
**public** `.pub` key to `/home/baskets/.ssh/authorized_keys` on the Pi. Keep the
permissions exact:

```bash
ssh baskets@baskets.local
install -d -m 700 ~/.ssh
editor ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Keep the existing session open and test the new key from a second terminal:

```bash
ssh -i ~/.ssh/lightweave_baskets \
  -o IdentitiesOnly=yes \
  baskets@baskets.local 'hostname; id'
```

Only remove an old key after the replacement succeeds. Never paste a private
key into `authorized_keys`, chat, a ticket, or the repository.

## Routine checks

Once connected, these read-only checks cover the common failure boundaries:

```bash
hostname
uptime
nmcli connection show --active
systemctl is-active lightweave-control cloudflared lightweave-gitops.timer
curl -fsS http://127.0.0.1:8000/api/health
sudo journalctl -u lightweave-control -u cloudflared \
  -u lightweave-gitops.service -n 100 --no-pager
```

The public control service can also be checked without SSH:

```bash
curl -fsS https://control.basketbrain.dev/api/health
```

The application intentionally listens on Pi loopback only. A failed connection
to `baskets.local:8000` from a laptop is expected; use the public HTTPS hostname
or run the loopback health command through SSH.

## If `baskets.local` is not found

1. Confirm the Pi and operator computer are on the same Wi-Fi and client
   isolation is not enabled.
2. Wait a minute after power-on, then retry `ping baskets.local`.
3. Find the Pi's DHCP lease in the router or Starlink client list and connect to
   that temporary address with `ssh baskets@ADDRESS`.
4. After connecting, check `hostname`, `systemctl is-active avahi-daemon`,
   `nmcli connection show --active`, and `ip address show wlan0`.
5. If the Pi is absent from the router, use a physical console or inspect the SD
   card; changing Wi-Fi remotely can remove the only recovery path.

## Internet SSH is not currently enabled

`https://control.basketbrain.dev` reaches only the loopback HTTP application
through Cloudflare Tunnel. It does not expose port 22, and `baskets.local` does
not resolve across the internet. The current production recovery contract is a
physical console or SSH from the Starlink LAN.

Do not add router port forwarding or expose port 22 publicly. If unattended
internet shell access becomes a requirement, provision and review a separate
private access path first. Suitable designs are:

- a private mesh VPN such as Tailscale on the Pi and approved operator devices;
- a separate Cloudflare Tunnel SSH hostname protected by Cloudflare Access,
  with `cloudflared` installed on operator computers.

Keep remote shell access independent of the public control hostname, require
device/user authentication, retain LAN SSH as the recovery path, and verify the
show continues while that access layer is disconnected.

## Hardening and key rotation

The current Pi accepts both public-key and password SSH authentication. After at
least two operator keys and a physical recovery path have been tested, disable
password authentication and root login in an `sshd_config.d` drop-in. Validate
with `sudo sshd -t`, keep the existing session open, reload SSH, and prove a new
key-only session works before closing the old one.

To revoke a computer, remove only its public-key line from
`~/.ssh/authorized_keys`, then test that an approved key still works. SSH key
changes do not require restarting the Lightweave control service and do not
affect the running show.
