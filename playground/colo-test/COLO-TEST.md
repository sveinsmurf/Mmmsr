# COLO Test — Setup Notes & Results

> **Note:** This test was set up and run by an AI agent (Codex). Results and steps below reflect what was actually executed.

## What goes on which machine

| Host | IP | Role | Notes |
|------|----|------|-------|
| `.103` (ezlab.min3) | 192.168.35.103 | **Primary** QEMU host | Runs the Primary VM (PVM), COLO proxy chardev servers (9001–9005), QMP on port 4444 |
| `.104` | 192.168.35.104 | Control/proxy node (reserved, not used in this test run) | Has `eno1` 10.0.0.2/24 direct link + USB NIC on `br0` |
| `.105` | 192.168.35.105 | **Secondary** QEMU host | Runs the Secondary VM (SVM), NBD server (9999) + incoming migration (9998), QMP on port 4445, `eno1` 10.0.0.1/24 direct link |

- Test VM IP: `192.168.35.110`, MAC `52:54:00:12:34:56`, user `svein` (passwordless sudo), SSH access via the `.103` `server` user's key.
- Tap interfaces: `tap1` on `.103` (master `vmbr0`), `tap0` on `.105` (master `br0`).
- Working dirs: `/mnt/vms/colo-test-primary` on `.103`, `/mnt/vms/colo-test-secondary` on `.105`.

## What was installed

**On `.103`:**
```bash
sudo pacman -Sy --needed libvirt qemu-tools socat nbd strace
```
(qemu-full / qemu-base already present, v11.1.2)

**On `.105`:**
```bash
sudo pacman -Sy --needed libisoburn
```
(qemu-full already present, v11.1.2 — needed for `xorriso` to build the cloud-init seed ISO)

## Configs changed

- `/etc/qemu/bridge.conf` on `.103`: set to `allow vmbr0` (required by `qemu-bridge-helper`; default was `allow virbr0`).
- `/etc/qemu/bridge.conf` on `.105`: set to `allow br0` (bridge name differs on this box).
- `/dev/net/tun` ownership set to group `server` on both hosts so the non-root `server` user can create taps.
- Created tap interfaces: `tap1` on `.103` (attached to `vmbr0`), `tap0` on `.105` (attached to `br0`).

## Disk images

1. Downloaded the Arch cloud image on `.103`:
   ```bash
   curl -LO https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2
   mv Arch-Linux-x86_64-cloudimg.qcow2 base.qcow2
   qemu-img resize base.qcow2 10G
   ```
2. Built a cloud-init `seed.iso` (`user-data` + `meta-data`) with `xorriso -as mkisofs -o seed.iso -V cidata -J -r user-data meta-data`.
   - **Gotcha:** the first seed used `$(head -1 ~/.ssh/authorized_keys)` inside a heredoc — the shell did not expand it, so the VM got a broken key and SSH failed. The seed must contain a **literal** public key.
3. Copied `base.qcow2` to `.105` via `scp` for the secondary side.
4. On `.105` created the COLO replication disks (same size as the primary image):
   ```bash
   qemu-img create -f qcow2 secondary-active.qcow2 10G
   qemu-img create -f qcow2 secondary-hidden.qcow2 10G
   ```

## QEMU commands

**Primary (`.103`)** — started with `-S` (paused) so the secondary can connect before the guest boots:
```bash
qemu-system-x86_64 \
  -enable-kvm -cpu qemu64,kvmclock=on -m 512 -smp 1 \
  -name primary \
  -qmp tcp:0.0.0.0:4444,server=on,wait=off \
  -chardev socket,id=mirror0,host=0.0.0.0,port=9003,server=on,wait=off \
  -chardev socket,id=compare1,host=0.0.0.0,port=9004,server=on,wait=off \
  -chardev socket,id=compare0,host=127.0.0.1,port=9001,server=on,wait=off \
  -chardev socket,id=compare0-0,host=127.0.0.1,port=9001 \
  -chardev socket,id=compare_out,host=127.0.0.1,port=9005,server=on,wait=off \
  -chardev socket,id=compare_out0,host=127.0.0.1,port=9005 \
  -object filter-mirror,id=m0,netdev=hn0,queue=tx,outdev=mirror0,vnet_hdr_support=on \
  -object filter-redirector,netdev=hn0,id=redire0,queue=rx,indev=compare_out,vnet_hdr_support=on \
  -object filter-redirector,netdev=hn0,id=redire1,queue=rx,outdev=compare0,vnet_hdr_support=on \
  -object iothread,id=iothread1 \
  -object colo-compare,id=comp0,primary_in=compare0-0,secondary_in=compare1,outdev=compare_out0,iothread=iothread1,vnet_hdr_support=on \
  -netdev tap,id=hn0,ifname=tap1,script=no,downscript=no,vhost=off \
  -device rtl8139,id=e0,netdev=hn0 \
  -drive if=ide,id=colo-disk0,driver=quorum,read-pattern=fifo,vote-threshold=1,children.0.file.filename=base.qcow2,children.0.driver=qcow2 \
  -display none -daemonize -pidfile /tmp/colo-primary.pid -serial file:/tmp/colo-primary.log -S
```

**Secondary (`.105`)** — started with `-incoming` waiting for migration:
```bash
qemu-system-x86_64 \
  -enable-kvm -cpu qemu64,kvmclock=on -m 512 -smp 1 \
  -name secondary \
  -qmp tcp:0.0.0.0:4445,server=on,wait=off \
  -chardev socket,id=red0,host=192.168.35.103,port=9003,reconnect-ms=1000 \
  -chardev socket,id=red1,host=192.168.35.103,port=9004,reconnect-ms=1000 \
  -object filter-redirector,id=f1,netdev=hn0,queue=tx,indev=red0,vnet_hdr_support=on \
  -object filter-redirector,id=f2,netdev=hn0,queue=rx,outdev=red1,vnet_hdr_support=on \
  -object filter-rewriter,id=rew0,netdev=hn0,queue=all,vnet_hdr_support=on \
  -netdev tap,id=hn0,ifname=tap0,script=no,downscript=no,vhost=off \
  -device rtl8139,id=e0,netdev=hn0 \
  -drive if=none,id=parent0,file.filename=/mnt/vms/colo-test-secondary/base.qcow2,driver=qcow2 \
  -drive if=none,id=childs0,driver=replication,mode=secondary,file.driver=qcow2,top-id=colo-disk0,file.file.filename=/mnt/vms/colo-test-secondary/secondary-active.qcow2,file.backing.driver=qcow2,file.backing.file.filename=/mnt/vms/colo-test-secondary/secondary-hidden.qcow2,file.backing.backing=parent0 \
  -drive if=ide,id=colo-disk0,driver=quorum,read-pattern=fifo,vote-threshold=1,children.0=childs0 \
  -drive file=seed.iso,if=virtio,media=cdrom \
  -incoming tcp:0.0.0.0:9998 \
  -display none -daemonize -pidfile /tmp/colo-secondary.pid -serial file:/tmp/colo-secondary.log
```

## QMP startup sequence (order matters!)

**1. Secondary QMP first** (port 4445):
```json
{"execute":"qmp_capabilities"}
{"execute": "migrate-set-capabilities", "arguments": {"capabilities": [ {"capability": "return-path", "state": true}, {"capability": "x-colo", "state": true} ]}}
{"execute": "nbd-server-start", "arguments": {"addr": {"type": "inet", "data": {"host": "0.0.0.0", "port": "9999"}}}}
{"execute": "nbd-server-add", "arguments": {"device": "parent0", "writable": true}}
```

**2. Then Primary QMP** (port 4444):
```json
{"execute":"qmp_capabilities"}
{"execute": "blockdev-add", "arguments": {"driver": "nbd", "node-name": "nbd0", "server": {"type": "inet", "host": "192.168.35.105", "port": "9999"}, "export": "parent0", "detect-zeroes": "on"}}
{"execute": "x-blockdev-change", "arguments": {"parent": "colo-disk0", "node": "nbd0"}}
{"execute": "migrate-set-capabilities", "arguments": {"capabilities": [ {"capability": "return-path", "state": true}, {"capability": "x-colo", "state": true} ]}}
{"execute": "migrate", "arguments": {"uri": "tcp:192.168.35.105:9998"}}
```

Both sides then report `query-migrate: status: colo` when lock-stepping is active.

Optional tuning:
```json
{"execute": "migrate-set-parameters", "arguments": {"x-checkpoint-delay": 2000}}
```

## Results / numbers

| Metric | Result |
|--------|--------|
| Direct link `.103 ↔ .105` bandwidth | **941 Mbit/s** both directions (iperf3, 4 parallel streams, 5s) |
| COLO state | Both sides report `query-migrate: status: colo` — lock-stepping confirmed |
| Failover method | Manual: `x-colo-lost-heartbeat` via QMP (no heartbeat daemon exists) |
| Failover ping gap | **~24 lost pings ≈ 5 seconds** (50 pings at 0.2s interval, 48% loss during failover) |
| Ping RTT inside COLO | ~0.5–1.3 ms |
| Promoted VM clock drift | ~113 seconds vs. primary's last checkpoint |

## Gotchas encountered

- The **heartbeat daemon is not implemented** in QEMU — failover must be triggered manually via QMP `x-colo-lost-heartbeat`.
- Primary `chardev ... server=on,wait=on` (the doc's `compare1`) **blocks the main loop** until a client connects, and QMP hangs/never accepts connections while blocked. Use `wait=off` (or start the secondary before the primary finishes) to avoid a deadlock.
- `qemu-bridge-helper` requires the bridge to be allowed in `/etc/qemu/bridge.conf`, and the helper must be able to write to `/dev/net/tun`. Using an explicit pre-created tap (`ifname=tapX,script=no,downscript=no`) as the `server` user avoids "access denied by acl file" issues.
- `qemu-system` daemons put their cwd at `/` — use **absolute paths** for images and `chmod 666` the QMP/pid/log files created by root, otherwise QMP is inaccessible to non-root users.
- The secondary's replicated BTRFS root went read-only after the first crash (`Transaction aborted, errno=-5`) — the hidden/active COLO disks must be wiped/recreated before retrying replication.
- The canonical QEMU COLO docs use the same image on both hosts, and note **COLO uses double the guest ram size on the secondary side**.
- There should be only one NBD client per primary disk; `nbd-server-start`/`nbd-server-add` on the secondary must run **before** `migrate` on the primary.
- `hostname` is not installed on the mini PCs (Arch minimal) — use `uname`/`cat /etc/hostname` instead.

## What was NOT done

- The dirty-page measurement (`tools/dirtyrate.py`) was abandoned: pagemap reads are extremely slow on these CPUs even for a 16 MB process (~113s per scan), and the soft-dirty tool never got a usable dirty-rate number. The tool exists in `/home/svein/Mmmsr/tools/` but was not used for this result.
- No Minecraft server was installed in the test VMs — this test used a plain Arch cloud image as a neutral workload (ping/SSH), not a game server.
