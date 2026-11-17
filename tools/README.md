# COLO control tools

`colo.py` prepares and controls the two-node COLO lab:

- `192.168.35.104` is the primary QEMU host and uses `tap2` on `br0`.
- `192.168.35.105` is the secondary QEMU host and uses `tap0` on `br0`.
- `10.0.0.2` and `10.0.0.1` are the direct-link addresses used by COLO replication.
- The guest uses `192.168.35.110` and MAC `52:54:00:12:34:56`.

The script must be run from a workstation with passwordless SSH access to both
hosts as the `server` user. It also expects passwordless sudo on both hosts for
bridge/TAP setup.

## Commands

```sh
tools/colo.py setup
tools/colo.py start
tools/colo.py status
tools/colo.py failover
tools/colo.py cleanup --reset-disks
```

`setup` downloads/resizes the guest image, builds the cloud-init seed, copies the
image to `.105` over the direct link, recreates the secondary replication disks,
and creates the two TAP interfaces. Existing QEMU VMs are not killed by `setup`.

`start` runs the required order:

1. stop the stale secondary and recreate its replication disks,
2. start the secondary with NBD and incoming migration,
3. stop the stale primary and start it paused,
4. add the secondary's NBD backing node to the primary,
5. migrate the primary and wait until both sides report `colo`.

`--memory` and `--smp` can be placed before any subcommand. `start` defaults to
1 GiB RAM and one vCPU; a restart therefore resets the guest to cloud-init state
unless the base image is manually preserved.

`failover` promotes the secondary after the primary heartbeat is lost. It stops
the secondary's NBD server, sends `x-colo-lost-heartbeat` to the secondary, waits
for `query-migrate` to report `completed`, and stops the old primary if it is
still listening. Promote the secondary **before** killing the primary; a primary
that disappears without promotion leaves the guest frozen in COLO mode. A
verified direct-link run received 23/23 pings across the handoff with a 0.201 s
maximum gap, so the guest remained reachable throughout.

## Important limitations

- Automatic failover is provided by `colo-watchdog.py` (see below), not by QEMU
  itself. QEMU has no heartbeat daemon; the watchdog supplies the missing
  heartbeat.
- Failover does not reverse roles. Whichever host recovered keeps serving the
  guest. Re-forming a `.104`-primary / `.105`-secondary pair is the job of
  `colo-rejoin.py`.
- Re-forming the pair restarts the guest (COLO cannot live-resync a diverged
  pair). The failover path itself is seamless; only the rejoin restarts.
- `cleanup --reset-disks` only recreates replication disks while QEMU is stopped.
- A successful migration leaves the original primary's QEMU process in a
  post-migration state; use `cleanup` before starting a fresh pair.

## Guest server (FerrumC)

The workload under test is [FerrumC](https://github.com/ferrumc-rs/ferrumc), a
Rust Minecraft server. Download the release for the x86_64 Linux guest:

```sh
curl -LO https://github.com/ferrumc-rs/ferrumc/releases/latest/download/ferrumc-v0.1.0-rc2-x86_64-unknown-linux-musl.tar.gz
curl -LO https://github.com/ferrumc-rs/ferrumc/releases/latest/download/ferrumc-v0.1.0-rc2-x86_64-unknown-linux-musl.tar.gz.sha256
sha256sum -c ferrumc-v0.1.0-rc2-x86_64-unknown-linux-musl.tar.gz.sha256
tar -xzf ferrumc-v0.1.0-rc2-x86_64-unknown-linux-musl.tar.gz
```

Use the `latest/download/<asset>` URL form so the links keep working across
releases; the `v0.1.0-rc2` in the filename is the current latest tag. On the
releases page the relevant assets are:

- `ferrumc-v0.1.0-rc2-x86_64-unknown-linux-musl.tar.gz` — the static x86_64 Linux
  binary for the COLO guest (this is the one to use on the mini-PCs).
- `ferrumc-v0.1.0-rc2-x86_64-unknown-linux-musl.tar.gz.sha256` — checksum.
- `ferrumc-v0.1.0-rc2-aarch64-unknown-linux-musl.tar.gz` — ARM64 Linux, not used
  here.
- `ferrumc-v0.1.0-rc2-aarch64-apple-darwin.tar.gz` — Apple Silicon macOS, not
  used here.
- `ferrumc-v0.1.0-rc2-x86_64-pc-windows-msvc.zip` — Windows, not used here.

The archive contains the `ferrumc` binary plus `configs/`, `world/`, and
`whitelist.txt`. Copy that directory into the guest:

```sh
scp -r ferrumc 192.168.35.110:~/ferrumc
```

The guest image is provisioned by `colo.py` with a `ferrumc.service` systemd
unit (written by the cloud-init seed) that starts the server automatically:

```ini
[Unit]
Description=FerrumC Minecraft server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=svein
WorkingDirectory=/home/svein/ferrumc
ExecStart=/home/svein/ferrumc/ferrumc run
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

`ferrumc run` serves Minecraft on `25565` (configurable in `configs/config.toml`)
and the dashboard on `9000`. The guest is reachable at `192.168.35.110` and keeps
that address across COLO failover. Start it by hand with
`ssh 192.168.35.110 'sudo systemctl start ferrumc'`.

## Automatic failover (either direction)

`colo-watchdog.py` runs on **both** hosts as `colo-watchdog.service` (enabled,
`Restart=always`). Each instance watches the *other* host and, when the peer is
confirmed dead, recovers the guest locally. The recovery action depends on the
role the host currently holds, which the watchdog auto-detects from QMP:

- **This host is the secondary and the primary died** -> promote: stop the local
  NBD server, send `x-colo-lost-heartbeat`, wait for `query-migrate: completed`,
  then broadcast a gratuitous ARP so the switch/router relearn the guest port.
  The guest keeps running.
- **This host is the primary and the secondary died** -> primary failover:
  reset the dead peer's TCP sockets (`ss -K`; the dead NBD link otherwise blocks
  QEMU's main loop and QMP stops answering), detach the NBD child, delete the
  `colo-compare`/filter objects, send `x-colo-lost-heartbeat`, then `cont`.

Detection uses two signals that do not depend on the peer's QEMU main loop:

- the direct-link NIC carrier (`/sys/class/net/eno1/carrier`), which drops the
  instant the peer's NIC powers off, and
- kernel-level ICMP to the peer host on the LAN.

The peer is declared dead only when the carrier is down **and** the peer stops
answering ICMP. A lone cable pull, a QEMU restart, or a hung QMP leaves the host
answering ICMP and is ignored, so a false promotion (split-brain) cannot happen.
After recovering, the watchdog stays quiet until the peer returns (so a primary
that still reports `colo` after failover is not re-processed on every poll).

Measured on 2026-11-17 (50 pings/s at the guest, peer QEMU killed and both its
links dropped):

| Direction | Recovery action | Recovery time |
|-----------|-----------------|---------------|
| `.104` dies, `.105` promotes | `x-colo-lost-heartbeat` | ~0.35 s |
| `.105` dies, `.104` resumes | socket reset + primary failover | ~0.33 s |

Control (per host):

```sh
ssh server@192.168.35.104 'sudo systemctl status colo-watchdog.service'
# planned maintenance (restart the pair) - suppress recovery on both hosts:
ssh server@192.168.35.104 'touch /tmp/colo-maintenance'
ssh server@192.168.35.105 'touch /tmp/colo-maintenance'
ssh server@192.168.35.104 'rm -f /tmp/colo-maintenance'
ssh server@192.168.35.105 'rm -f /tmp/colo-maintenance'
```

## Automatic rejoin

`colo-rejoin.py` runs on the designated primary (`.104`) as
`colo-rejoin.service`. The watchdog keeps the guest alive when a host dies, but
after a rebooted host comes back the pair is *not* automatically re-formed: one
side serves the guest as a lone standalone QEMU and the other sits idle, which
is a single point of failure. This is exactly what made "reboot one box, then
pull the other" drop the guest.

The daemon polls both QMP endpoints every `--interval` seconds. If the peer host
is reachable but the pair is not healthy (`colo` on both sides) for two
consecutive checks, it re-forms the pair by running `colo.py setup` then
`colo.py start`, retrying through the QMP greeting race. It sets
`/tmp/colo-maintenance` on both hosts while it works so the watchdogs do not
interfere.

Re-forming performs a fresh migration, so the **guest restarts** at rejoin. That
is inherent to COLO; there is no live re-sync of a diverged pair. Failover never
restarts the guest.

Measured on 2026-11-17: a host that was power-pulled and returned was re-joined
into a healthy `colo` pair automatically in ~68 s (including the guest reboot).

Control:

```sh
ssh server@192.168.35.104 'sudo systemctl status colo-rejoin.service'
ssh server@192.168.35.104 'sudo systemctl stop colo-rejoin.service'   # pause
```

## Deployment summary

Both watchdogs and the rejoin daemon run as systemd services and are enabled at
boot. The watchdogs need `CAP_NET_RAW` (gratuitous ARP) and `CAP_NET_ADMIN`
(`ss -K` socket reset):

- `.104`: `colo-watchdog.service` (`--role auto --peer-lan 192.168.35.105
  --peer-direct 10.0.0.1 --local-qmp 4444`) and `colo-rejoin.service`.
- `.105`: `colo-watchdog.service` (`--role auto --peer-lan 192.168.35.104
  --peer-direct 10.0.0.2 --local-qmp 4445`).
