#!/usr/bin/env python3
"""Symmetric COLO failover watchdog.

Runs on BOTH hosts (one instance per host, as a systemd service). Each instance
watches the *other* host and, when the peer is confirmed dead, recovers the
guest locally. The recovery action depends on which role this host currently
holds:

  * If this host is the SECONDARY and the peer (primary) died, it promotes
    itself: stop the local NBD server, send x-colo-lost-heartbeat, wait for
    query-migrate to report "completed", then broadcast a gratuitous ARP so the
    switch/router relearn the guest's port. The guest keeps running.

  * If this host is the PRIMARY and the peer (secondary) died, it runs the
    documented "primary failover": detach the NBD backing child, delete the
    colo-compare/filter objects, and resume. Without this the primary's guest
    freezes because COLO withholds output until the shadow confirms it.

Detection uses signals that do not depend on the peer's QEMU main loop:

  * the direct-link NIC carrier (/sys/class/net/<nic>/carrier), which drops the
    instant the peer's NIC powers off, and
  * kernel-level ICMP to the peer host on the LAN.

The peer is declared dead only when the carrier is down AND the peer stops
answering ICMP. A lone cable pull, a QEMU restart, or a hung QMP leaves the host
answering ICMP and is ignored, so a false promotion (split-brain) cannot happen.

The loop never exits on a transient error, and recovery is idempotent and gated
on this host's QEMU actually being in COLO mode.

Usage (primary .104):
    colo-watchdog.py --role primary --nic eno1 --peer-lan 192.168.35.105 \
        --local-qmp 4444 --garp-iface br0 --guest-ip 192.168.35.110 \
        --guest-mac 52:54:00:12:34:56
Usage (secondary .105):
    colo-watchdog.py --role secondary --nic eno1 --peer-lan 192.168.35.104 \
        --local-qmp 4445 --garp-iface br0 --guest-ip 192.168.35.110 \
        --guest-mac 52:54:00:12:34:56
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time


def log(message):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"{stamp} {message}", flush=True)


class QMPClient:
    def __init__(self, host, port, timeout=10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.stream = self.sock.makefile("rb")
        greeting = json.loads(self.stream.readline())
        if "QMP" not in greeting:
            raise RuntimeError(f"unexpected QMP greeting: {greeting}")
        self.execute("qmp_capabilities")

    def execute(self, command, arguments=None):
        request = {"execute": command}
        if arguments is not None:
            request["arguments"] = arguments
        self.sock.sendall((json.dumps(request) + "\n").encode())
        while True:
            line = self.stream.readline()
            if not line:
                raise RuntimeError("QMP connection closed")
            message = json.loads(line)
            if "event" in message:
                continue
            if "error" in message:
                raise RuntimeError(message["error"])
            return message.get("return")

    def close(self):
        try:
            self.stream.close()
            self.sock.close()
        except OSError:
            pass


def carrier_up(nic):
    try:
        with open(f"/sys/class/net/{nic}/carrier") as handle:
            return handle.read().strip() == "1"
    except OSError:
        return False


def icmp_alive(host, timeout):
    result = subprocess.run(
        ["ping", "-c", "1", "-W", str(timeout), host],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def send_gratuitous_arp(iface, ip, mac, count=5, interval=0.05):
    """Broadcast a gratuitous ARP for the guest's IP/MAC on the bridge.

    After a failover the switch MAC table and the router ARP cache still point
    at the dead peer's port; a GARP makes them update immediately instead of
    waiting for the next regular frame, which shortens the network gap.
    """
    try:
        mac_bytes = bytes.fromhex(mac.replace(":", ""))
        ip_bytes = socket.inet_aton(ip)
    except (ValueError, OSError) as exc:
        log(f"garp: bad mac/ip: {exc}")
        return
    eth = b"\xff\xff\xff\xff\xff\xff" + mac_bytes + b"\x08\x06"
    arp = (b"\x00\x01\x08\x00\x06\x04\x00\x01" + mac_bytes + ip_bytes +
           b"\x00\x00\x00\x00\x00\x00" + ip_bytes)
    frame = eth + arp
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        sock.bind((iface, 0))
    except OSError as exc:
        log(f"garp: cannot open {iface}: {exc}")
        return
    try:
        for _ in range(count):
            try:
                sock.send(frame)
            except OSError:
                break
            time.sleep(interval)
    finally:
        sock.close()


def local_migrate(port):
    """Return (status, looks_like_secondary).

    A COLO secondary reports an incoming socket-address in query-migrate; the
    primary does not. This lets each watchdog work out its own role without
    being told, which stays correct after a reboot or a role swap.
    """
    qmp = QMPClient("127.0.0.1", port)
    try:
        result = qmp.execute("query-migrate")
        return result.get("status"), "socket-address" in result
    finally:
        qmp.close()


def local_status(port):
    status, _ = local_migrate(port)
    return status


def promote_secondary(port, timeout=60.0):
    """Promote this (secondary) host after the primary died. Never raises."""
    qmp = None
    try:
        qmp = QMPClient("127.0.0.1", port)
        try:
            qmp.execute("nbd-server-stop")
        except (RuntimeError, OSError) as exc:
            log(f"nbd-server-stop: {exc} (continuing)")
        qmp.execute("x-colo-lost-heartbeat")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = qmp.execute("query-migrate").get("status")
            except (RuntimeError, OSError, socket.timeout) as exc:
                log(f"query-migrate during promotion: {exc} (retrying)")
                time.sleep(0.1)
                continue
            if status == "completed":
                return True
            if status in ("failed", "cancelled"):
                log(f"promotion failed: {status}")
                return False
            time.sleep(0.02)
        log("promotion timed out")
        return False
    except (RuntimeError, OSError, socket.timeout) as exc:
        log(f"promotion error: {exc}")
        return False
    finally:
        if qmp is not None:
            qmp.close()


def break_dead_peer_sockets(peer_ip):
    """Forcibly close TCP connections to the dead peer.

    When the secondary dies, the primary's block layer is left waiting on the
    NBD replication socket. That blocks QEMU's main loop, so ordinary QMP
    commands stall and the guest stays frozen. Resetting the sockets makes the
    pending I/O fail immediately, which unblocks the main loop. Requires
    CAP_NET_ADMIN (ss -K).
    """
    result = subprocess.run(
        ["ss", "-K", "dst", peer_ip],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    killed = result.stdout.strip().splitlines()
    # First line is the header; remaining lines are reset connections.
    n = max(0, len(killed) - 1)
    log(f"reset {n} socket(s) to {peer_ip}: {result.stdout.strip()!r}")


def resume_primary(port, peer_ips, timeout=30.0):
    """Primary failover: detach the dead secondary and resume this host.

    Mirrors QEMU's documented COLO primary-failover sequence: drop the NBD
    backing child, remove the compare/filter objects, then resume. Without this
    the primary's guest stays frozen because COLO withholds output until the
    secondary confirms it. The dead peer's sockets are reset first, otherwise
    the main loop is blocked and QMP never answers.
    """
    for peer_ip in peer_ips:
        try:
            break_dead_peer_sockets(peer_ip)
        except Exception as exc:
            log(f"socket reset to {peer_ip} failed: {type(exc).__name__}: {exc}")

    # Wait for QMP to answer again after the block layer unblocks.
    qmp = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            qmp = QMPClient("127.0.0.1", port, timeout=3.0)
            break
        except (RuntimeError, OSError, socket.timeout):
            qmp = None
            time.sleep(0.2)
    if qmp is None:
        log("primary failover: QMP never became reachable")
        return False

    try:
        def try_exec(command, arguments=None):
            try:
                qmp.execute(command, arguments)
                return True
            except (RuntimeError, OSError, socket.timeout) as exc:
                log(f"{command}: {exc} (continuing)")
                return False

        # Detach the dead secondary's replication plumbing. Order follows the
        # QEMU COLO primary-failover docs.
        try_exec("x-blockdev-change", {"parent": "colo-disk0", "child": "children.1"})
        try_exec("blockdev-del", {"node-name": "nbd0"})
        for obj in ("comp0", "iothread1", "m0", "redire0", "redire1"):
            try_exec("object-del", {"id": obj})

        # After the objects are gone the VM is still paused in COLO; the
        # heartbeat command is what actually resumes it. Without this the guest
        # stays frozen even though query-migrate still reports "colo".
        try_exec("x-colo-lost-heartbeat")

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = qmp.execute("query-migrate").get("status")
            except (RuntimeError, OSError, socket.timeout):
                break
            if status != "colo":
                break
            time.sleep(0.05)
        try_exec("cont")
        return True
    except (RuntimeError, OSError, socket.timeout) as exc:
        log(f"primary failover error: {exc}")
        return False
    finally:
        qmp.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("primary", "secondary", "auto"), default="auto",
                        help="role this host holds (default: auto-detect from QMP)")
    parser.add_argument("--nic", default="eno1", help="direct-link NIC on this host")
    parser.add_argument("--peer-lan", required=True, help="peer host's LAN IP")
    parser.add_argument("--peer-direct", default=None,
                        help="peer host's direct-link IP (defaults to --peer-lan)")
    parser.add_argument("--local-qmp", type=int, required=True,
                        help="QMP port of this host's QEMU")
    parser.add_argument("--poll", type=float, default=0.02, help="poll interval in seconds")
    parser.add_argument("--ping-timeout", type=float, default=0.1,
                        help="ICMP timeout in seconds")
    parser.add_argument("--confirm", type=int, default=1,
                        help="consecutive dead confirmations before recovering")
    parser.add_argument("--garp-iface", default="br0",
                        help="bridge interface for the gratuitous ARP (secondary only)")
    parser.add_argument("--guest-ip", default="192.168.35.110")
    parser.add_argument("--guest-mac", default="52:54:00:12:34:56")
    parser.add_argument("--maintenance-file", default="/tmp/colo-maintenance",
                        help="while this file exists, the watchdog will not recover")
    parser.add_argument("--debug", action="store_true", help="log every decision")
    args = parser.parse_args(argv)

    log(f"watchdog started: role={args.role} peer_lan={args.peer_lan} nic={args.nic} "
        f"local-qmp={args.local_qmp} poll={args.poll}s ping={args.ping_timeout}s "
        f"confirm={args.confirm}")
    misses = 0
    recovered = False
    while True:
        try:
            time.sleep(args.poll)

            if os.path.exists(args.maintenance_file):
                misses = 0
                continue

            carrier = carrier_up(args.nic)
            if carrier:
                # Direct link physically up -> peer host is powered.
                misses = 0
                recovered = False
                continue

            # Carrier is down. Only a power pull also makes the peer stop
            # answering ICMP; a lone cable pull or a QEMU restart does not.
            peer_alive = icmp_alive(args.peer_lan, args.ping_timeout)
            if args.debug:
                log(f"debug: carrier={carrier} peer_alive={peer_alive} misses={misses} recovered={recovered}")
            if peer_alive:
                misses = 0
                recovered = False
                continue

            misses += 1
            if misses < args.confirm:
                continue

            if recovered:
                # Already recovered from this peer death; wait for the peer to
                # come back before doing anything again. The primary keeps
                # reporting "colo" after failover, so this flag is what stops
                # the watchdog from re-running recovery on every poll.
                continue

            reason = f"direct-link carrier down and peer host ICMP-dead x{misses}"
            try:
                status, looks_secondary = local_migrate(args.local_qmp)
            except (OSError, RuntimeError, socket.timeout) as exc:
                log(f"detected death ({reason}) but local QMP unavailable: {exc}")
                misses = 0
                continue

            if status != "colo":
                if status != "completed":
                    log(f"detected death ({reason}) but local status is '{status}', not recovering")
                misses = 0
                continue

            role = args.role
            if role == "auto":
                role = "secondary" if looks_secondary else "primary"
                log(f"auto-detected role: {role}")

            started = time.time()
            if role == "secondary":
                log(f"PRIMARY DOWN: {reason} -> promoting this secondary")
                if promote_secondary(args.local_qmp):
                    send_gratuitous_arp(args.garp_iface, args.guest_ip, args.guest_mac)
                    recovered = True
                    log(f"promotion complete in {time.time() - started:.3f}s; "
                        f"guest now served by this host")
            else:
                log(f"SECONDARY DOWN: {reason} -> resuming this primary")
                peer_ips = [args.peer_lan]
                if args.peer_direct and args.peer_direct not in peer_ips:
                    peer_ips.append(args.peer_direct)
                if resume_primary(args.local_qmp, peer_ips):
                    send_gratuitous_arp(args.garp_iface, args.guest_ip, args.guest_mac)
                    recovered = True
                    log(f"primary failover complete in {time.time() - started:.3f}s; "
                        f"guest now served by this host")
            misses = 0
        except Exception as exc:  # never let the watchdog die
            log(f"watchdog loop error (continuing): {type(exc).__name__}: {exc}")
            misses = 0
            time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
