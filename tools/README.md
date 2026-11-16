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

- There is no automatic QEMU heartbeat daemon. Failover is deliberately manual.
- After failover, the roles do not reverse. To return to `.104` primary and
  `.105` secondary, run `start` again.
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
`whitelist.txt`. Copy that directory into the guest and run it:

```sh
scp -r ferrumc 192.168.35.110:~/ferrumc
ssh 192.168.35.110 'cd ~/ferrumc && chmod +x ferrumc && ./ferrumc run'
```

`./ferrumc run` starts the server on `25565` (configurable in
`configs/config.toml`) and the dashboard on `9000`. The guest is reachable at
`192.168.35.110` and keeps that address across COLO failover.
