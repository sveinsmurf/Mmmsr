# COLO Test #2 — Direct Link (`.104` ↔ `.105`)

> **Note:** This test was set up and run by an AI agent (Codex). Results below reflect what was actually executed, including the parts that failed.

## Motivation

Test #1 (see `COLO-TEST.md`) ran the COLO pair on `.103` (primary) ↔ `.105` (secondary) over the **switched LAN**. The direct gigabit link between `.104` (`eno1`, 10.0.0.2/24) and `.105` (`eno1`, 10.0.0.1/24) was left unused — even though COLO's replication traffic (checkpoints + packet comparison stream) is exactly the kind of traffic that benefits from a dedicated low-latency link. This test moved the primary to `.104` so the COLO control path rides the direct link.

## What goes on which machine

| Host | IP | Role | Notes |
|------|----|------|-------|
| `.104` | 192.168.35.104 | **Primary** QEMU host | QMP on TCP 4444, COLO proxy chardev servers (9003/9004), guest tap `tap2` on `br0`. Old test1 VM was killed first to free the host. |
| `.105` | 192.168.35.105 | **Secondary** QEMU host | QMP on TCP 4445, NBD server (9999), `-incoming tcp:0.0.0.0:9998`, guest tap `tap0` on `br0` |

- Test VM IP: `192.168.35.110` (MAC `52:54:00:12:34:56`), 512 MB RAM, 1 vCPU, same `base.qcow2` on both hosts (root password SSH enabled + `server@.103` key injected via nbd mount).
- COLO chardev sockets now point at **`10.0.0.2` (direct link)** instead of the LAN IP used in test #1.

## Setup differences from test #1

1. Killed the old test1 VM on `.104`, copied its cloud image to `/mnt/vms/colo-test-primary/base.qcow2`, resized to 10G.
2. Injected SSH keys by mounting the image over `qemu-nbd` (`/dev/nbd0p3` → `/mnt/vm-root`), writing `~/.ssh/authorized_keys` for `root` and `svein`, setting `PermitRootLogin yes`.
3. Copied the fixed image to `.105` over the direct link (`scp server@10.0.0.2:...`).
4. Recreated `secondary-active.qcow2` / `secondary-hidden.qcow2` fresh on `.105`.
5. Secondary chardevs: `red0`/`red1` → `host=10.0.0.2` (direct link) instead of `192.168.35.103`.
6. Both QEMUs run as `server` with explicit `ifname=tapN,script=no,downscript=no` (no bridge helper — it kept failing the ACL check even with `allow vmbr0` in `/etc/qemu/bridge.conf`).
7. QMP moved to **TCP sockets** (4444/4445) — the unix QMP socket repeatedly refused or hung connections, and TCP QMP worked reliably.
8. Primary `compare1` chardev changed to `wait=off` (the doc's `wait=on` blocks the main loop and freezes QMP — see gotchas in `COLO-TEST.md`).

## What worked

- Primary on `.104`, secondary on `.105` over the direct link: both sides reached `query-migrate: status: colo` (lock-stepping active), same as test #1.
- `red0` (port 9003, mirrored guest TX packets) connected successfully across the direct link.
- The image copy over the direct link was fast (1.3 GB copied without issue).

## What failed

- **The comparison channel (`red1` → primary port 9004) never established a healthy connection.** It sat in `CLOSE-WAIT` on the secondary side. Result: no packet comparison, no checkpoints driving the SVM.
- **The secondary VM froze** at guest uptime ~141 s (serial log stopped growing) — in COLO the SVM only makes progress when checkpoints arrive from the primary.
- **The VM was never network-reachable in this run** (0 of 100 pings succeeded, `Destination Host Unreachable` from the start) — with the compare channel dead, COLO's proxy withholds all outbound packets, exactly as designed for a broken proxy.
- Manual failover (`x-colo-lost-heartbeat`) returned `VM is not in COLO mode` after the primary died; a `system_reset` of the secondary produced a guest with a **read-only BTRFS root** (`Transaction aborted, errno=-5`) — same block-replication artifact as test #1.

## Measured numbers

### Link latency (measured this run)

| Path | min RTT | avg RTT | max RTT |
|------|---------|---------|---------|
| **Direct link** `.104 → .105` (10.0.0.x) | 0.162 ms | **0.285 ms** | 0.636 ms |
| **Direct link** `.105 → .104` | 0.127 ms | **0.208 ms** | 0.257 ms |
| **Switched LAN** `.104 → .105` (via switch) | 0.452 ms | 0.504 ms | 0.552 ms |
| **Switched LAN** `.105 → .103` (test #1 path) | 0.413 ms | 0.470 ms | 0.517 ms |

**The direct link is ~2.2× lower average latency** than the switched LAN path (0.21–0.29 ms vs 0.47–0.50 ms), with slightly higher jitter in one direction.

### COLO performance delta

| Metric | Test #1 (switched LAN) | Test #2 (direct link) |
|--------|------------------------|------------------------|
| Reached `colo` state | ✅ | ✅ |
| VM reachable during COLO | ✅ (~0.5–1.3 ms RTT) | ❌ (0/100 pings — proxy channel broken) |
| Failover ping gap | ~24 lost pings ≈ 5 s | unmeasurable (VM never pingable) |
| Post-failover VM state | running, pingable | BTRFS read-only, required `system_reset` |

**Honest conclusion: no COLO performance improvement could be measured this run.** The direct link's raw latency advantage is real and significant (2.2×), but this run's COLO setup failed at the proxy-comparison layer for reasons unrelated to link latency (a half-closed chardev connection that `reconnect-ms=1000` failed to recover). The switched-LAN run in test #1 was the *more* successful COLO run. To actually measure whether the direct link improves COLO (checkpoint frequency, compare latency, failover gap), the run needs to be repeated with a verified-healthy 9004 channel before killing anything.

### Likely cause of the broken 9004 channel

The secondary's `red1` chardev connected to the primary's port 9004 during an earlier primary incarnation; when the primary QEMU was killed and restarted between setup steps, the secondary's `reconnect-ms=1000` did not re-establish the connection (it stayed `CLOSE-WAIT` while `red0`/9003 did reconnect). Both chardev connections must be verified `ESTAB` on **both** sides before issuing `migrate` — a `ss -tnp | grep 9003/9004` check on each host should be added as an explicit step.

## New gotchas (beyond `COLO-TEST.md`)

- **Verify all four chardev connections are `ESTAB` bidirectionally** (9003 and 9004 on both hosts) before starting migration. A half-open comparison channel silently freezes the SVM and withholds all guest network output.
- **Prefer TCP QMP** (`-qmp tcp:...`) over unix sockets on these boxes — the unix socket intermittently refused or hung connections.
- **`qemu-bridge-helper` ACL checks failed even with a correct `bridge.conf`** — using a pre-created tap with `ifname=` and running QEMU as the tap-owning user is the reliable path.
- **The old test1 VM on `.104` must be killed and its image lock released** (`qemu-nbd --disconnect`) before reusing the host or mounting its disk.
- Don't mount a qcow2 that a running QEMU holds, even with `--read-only` — the write lock blocks `qemu-nbd --connect` regardless.
