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
