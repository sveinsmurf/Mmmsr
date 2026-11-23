# COLO control tools

`colo.py` prepares and controls the two-node COLO lab:

- `192.168.35.104` is the primary QEMU host and uses `tap2` on `br0`.
- `192.168.35.105` is the secondary QEMU host and uses `tap0` on `br0`.
- `10.0.0.2` and `10.0.0.1` are the direct-link addresses used by COLO replication.
- The guest uses `192.168.35.110` and MAC `52:54:00:12:34:56`.

Those are the original roles. After a failover the live pair can be reversed;
the role is whatever QMP reports (`query-colo-status`), not the host address.

The script must be run from a workstation with passwordless SSH access to both
hosts as the `server` user. It also expects passwordless sudo on both hosts for
bridge/TAP setup.

## Commands

```sh
tools/colo.py setup
tools/colo.py start
tools/colo.py status
tools/colo.py failover
tools/colo.py resume-replication [--survivor auto|primary|secondary]
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
unless the base image is manually preserved. `resume-replication` ignores those
flags and adopts the surviving QEMU's live RAM/vCPU shape, because migration
requires an identical machine.

`resume-replication` is the non-disruptive rejoin. It keeps the surviving guest
running, starts the returning host as a fresh secondary, resyncs the survivor's
disk into it with `drive-mirror`, then re-forms the pair with a COLO migration.
It implements QEMU's "Primary resume replication" and "Secondary resume
replication" sequences, so it works in either direction: after a primary
failover (survivor has the primary topology) or after a secondary promotion
(survivor still has the `f1`/`f2`/`rew0` rewriter, which is removed and replaced
with the primary filters inserted before `rew0`). `--survivor` defaults to
`auto`, which picks the host whose QEMU is running; pass `primary`/`secondary`
explicitly when both or neither are running.

`start` and `resume-replication` set the COLO checkpoint interval to 500 ms
(`--checkpoint-delay`, default `500` ms). COLO withholds guest output until the
next checkpoint; QEMU's 20 s default is longer than Minecraft's client timeout,
so players saw "LAN server" entries that then timed out. The 500 ms interval keeps
the same guest running while staying inside the client timeout. On this pair a
500 ms interval measured a steady ~0.8 s Minecraft status ping.

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
- Re-forming the pair with `setup` + `start` restarts the guest. Use
  `resume-replication` instead to keep the guest running; it only pauses it for
  the COLO RAM transfer (about 5 s for the 1 GiB guest here), which is well
  inside Minecraft's connection timeout.
- COLO's checkpoint delay defaults to 20 s in QEMU. Always set it to 500 ms on a
  live Minecraft server; otherwise a reachable server still looks like a
  timeout to clients. The tools now do this automatically.
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

### Guest SSH password

The guest is not exposed to the internet. SSH password login is used because the
cloud-init SSH-key path has been unreliable in this lab.

- user: `svein`
- password: `123454321`

This password is intentionally weak and must never be used on a host reachable
from the internet. It exists only so you can `ssh svein@192.168.35.110` from
the private LAN.

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

Role detection uses `query-colo-status`, not `query-status.running`. A healthy
COLO secondary is *paused* while it waits for the primary's checkpoints, so
`running: false` is normal and must not be treated as "no usable guest". An
earlier version gated promotion on `running`, which left `.105` stuck as a
paused primary when `.104` went down. The gate is removed.

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
consecutive checks, it works out which QEMU is still running (the survivor) and
runs `colo.py resume-replication --survivor <role>`. Mid-migration states
(`setup`, `inmigrate`, `active`, `postmigrate`) are treated as healthy in-flight
rejoins and skipped, so the daemon cannot latch onto a paused secondary during a
transition. That keeps the surviving guest running while the returning host is
resynced and re-COLOed. It sets
`/tmp/colo-maintenance` on both hosts while it works so the watchdogs do not
interfere.

The old `setup` + `start` sequence is kept only as a last-resort fallback for the
case where **both** QEMUs are gone (no survivor); that path does reboot the
guest. Whenever one host is still serving the guest, the daemon uses the
non-disruptive path. If both QEMUs are alive but the pair is not COLO (a split
brain), the daemon refuses to act rather than rebooting a guest that is still
running.

Measured on 2026-11-19: `pull .105 -> .104 primary-failover -> .105 returns ->
daemon auto-rejoin -> pull .104 -> .105 resumes -> daemon auto-rejoin` kept the
guest connected throughout. Each automatic rejoin froze the guest for ~5 s (the
COLO RAM transfer) and neither rejoin rebooted the guest. The returning host's
disk resync (~20 s for the ~1 GiB used image) runs while the guest stays live.

Reboot test on 2026-11-20 (`reboot .105`, wait for rejoin, then `reboot .104`):
the first run exposed the paused-secondary gate and `.105` failed to promote;
after the fix, `.105` promoted in **0.356 s** when `.104` went down. The guest
stayed reachable throughout, but the Minecraft client saw a ~2 s connection
drop on each switch and had to reconnect. The guest itself came back healthy.

Control:

```sh
ssh server@192.168.35.104 'sudo systemctl status colo-rejoin.service'
ssh server@192.168.35.104 'sudo systemctl stop colo-rejoin.service'   # pause
```

## Live monitor

`tools/colo-monitor.py` serves an animated page of the pair. The guest (`110`)
is a square floating in space; whichever host is primary holds it. When that host
dies it dims and falls asleep, and the square drifts over to the other host.

The monitor polls each host's **state agent** over HTTP, so it never SSHes and a
dead host cannot stall the page. Deploy the agents once:

```sh
python3 tools/colo.py deploy-agent
```

That installs `tools/colo-agent.py` as `colo-agent.service` on both hosts
(enabled, so it survives reboots) and serves `GET /state` on port 9100. Then run
the monitor on the workstation:

```sh
python3 tools/colo-monitor.py            # http://127.0.0.1:8080/
python3 tools/colo-monitor.py --port 8080 --bind 0.0.0.0   # reachable over Tailscale
```

It binds all interfaces by default, so `http://<tailscale-ip>:8080/` works from
another machine. Pass `--bind 127.0.0.1` to keep it local-only.

The agent is read-only: it runs QMP `query-migrate` / `query-status` and `pgrep`
locally and returns JSON. The monitor polls both agents plus a ping to the guest
once a second and drives the scene from that.

The scene also shows the pair state (`COLO pair healthy`, `failover`,
`rejoining`, `split brain`, `both hosts down`) and a role-change log. It is a
workstation tool: nothing is installed on the workstation beyond Python.

## Troubleshooting

**Minecraft says "connection reset" but the guest pings and the dashboard is up.**
Capture at the guest's game port first. If the client sends FIN/RST and the guest
sends nothing, the guest's application layer is not answering; the network and
COLO are fine. A capture during a failover shows this as a burst of late
SYN-ACKs, not a server RST. If the guest's TCP stack still accepts connections
but Minecraft handshakes time out, `ferrumc.service` inside the guest is wedged
and needs a restart. The guest seed does not enable `sshd`, so use the QEMU
console or add SSH to the seed. `colo.py` does not restart the guest's server.

**The monitor shows the wrong primary.** The agent reports the role from QMP
`query-colo-status`, not from the migration socket. If a host shows a stale
role, redeploy the agents with `python3 tools/colo.py deploy-agent` and restart
`tools/colo-monitor.py`.

**A host refuses to promote after the peer dies.** Check that the deployed
watchdog is the fixed version: a healthy COLO secondary is paused, so promotion
must key off `query-colo-status`, not `query-status.running`. Re-copy
`tools/colo-watchdog.py` to both hosts and restart the service.

## Deployment summary

Both watchdogs and the rejoin daemon run as systemd services and are enabled at
boot. The watchdogs need `CAP_NET_RAW` (gratuitous ARP) and `CAP_NET_ADMIN`
(`ss -K` socket reset):

- `.104`: `colo-watchdog.service` (`--role auto --peer-lan 192.168.35.105
  --peer-direct 10.0.0.1 --local-qmp 4444`) and `colo-rejoin.service`.
- `.105`: `colo-watchdog.service` (`--role auto --peer-lan 192.168.35.104
  --peer-direct 10.0.0.2 --local-qmp 4445`).
