#!/usr/bin/env python3
"""Auto-rejoin daemon: keeps the COLO pair formed.

Runs on the designated primary (.104). The failover watchdogs keep the guest
alive when a host dies, but after a rebooted host comes back the pair is *not*
automatically re-formed: one side serves the guest as a lone standalone QEMU
and the other sits idle. That leaves a single point of failure, which is exactly
what made "reboot one box, then pull the other" drop the guest.

This daemon closes that gap. Every interval it checks whether both QMP
endpoints report `colo`. If the peer host is reachable but the pair is not
healthy for two consecutive checks, it re-forms the pair by running the same
sequence as `colo.py setup` + `colo.py start`.

COLO cannot live-resync a diverged pair, so re-forming performs a fresh
migration and the guest restarts. That restart happens only at rejoin, never on
the failover path (failover is seamless).

Usage:
    colo-rejoin.py [--colo-py /home/server/colo.py] [--peer-lan 192.168.35.105] \
                   [--primary-qmp 192.168.35.104:4444] \
                   [--secondary-qmp 192.168.35.105:4445] [--interval 15]
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


def migrate_status(host, port, timeout=4.0):
    """Return query-migrate status, or None if QMP is unreachable."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        stream = sock.makefile("rb")
        stream.readline()  # greeting

        def execute(command):
            sock.sendall((json.dumps({"execute": command}) + "\n").encode())
            while True:
                line = stream.readline()
                if not line:
                    raise RuntimeError("closed")
                message = json.loads(line)
                if "event" in message:
                    continue
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("return")

        execute("qmp_capabilities")
        status = execute("query-migrate").get("status")
        stream.close()
        sock.close()
        return status
    except (OSError, RuntimeError, socket.timeout):
        return None


def ping_alive(host, timeout=1):
    result = subprocess.run(
        ["ping", "-c", "1", "-W", str(timeout), host],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def ssh(host, command):
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, command],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
    )


def split_host_port(value):
    host, _, port = value.partition(":")
    return host, int(port)


def set_maintenance(hosts, on):
    action = "touch" if on else "rm -f"
    for host in hosts:
        try:
            ssh(host, f"{action} /tmp/colo-maintenance")
        except Exception as exc:
            log(f"maintenance {action} on {host} failed: {exc}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--colo-py", default="/home/server/colo.py")
    parser.add_argument("--peer-lan", default="192.168.35.105")
    parser.add_argument("--self-host", default="server@192.168.35.104")
    parser.add_argument("--peer-host", default="server@192.168.35.105")
    parser.add_argument("--primary-qmp", default="192.168.35.104:4444")
    parser.add_argument("--secondary-qmp", default="192.168.35.105:4445")
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--attempts", type=int, default=4,
                        help="how many times to retry forming the pair")
    parser.add_argument("--maintenance-file", default="/tmp/colo-maintenance",
                        help="while this file exists, the daemon will not re-form")
    parser.add_argument("--memory", type=int, default=2048)
    parser.add_argument("--smp", type=int, default=1)
    args = parser.parse_args(argv)

    p_host, p_port = split_host_port(args.primary_qmp)
    s_host, s_port = split_host_port(args.secondary_qmp)
    hosts = [args.self_host, args.peer_host]

    log(f"rejoin daemon started: peer={args.peer_lan} "
        f"primary={args.primary_qmp} secondary={args.secondary_qmp} "
        f"interval={args.interval}s")
    unhealthy = 0
    while True:
        try:
            time.sleep(args.interval)

            if os.path.exists(args.maintenance_file):
                unhealthy = 0
                continue

            if not ping_alive(args.peer_lan, timeout=1):
                # Peer host is down. The failover watchdog owns this case.
                unhealthy = 0
                continue

            p_status = migrate_status(p_host, p_port)
            s_status = migrate_status(s_host, s_port)
            if p_status == "colo" and s_status == "colo":
                unhealthy = 0
                continue

            unhealthy += 1
            log(f"pair not healthy (primary={p_status} secondary={s_status}); "
                f"check {unhealthy}")
            if unhealthy < 2:
                continue

            log("re-forming COLO pair (setup + start)")
            set_maintenance(hosts, True)
            started = time.time()
            ok = False
            for attempt in range(1, args.attempts + 1):
                for step in ("setup", "start"):
                    result = subprocess.run(
                        ["python3", args.colo_py, "--memory", str(args.memory),
                         "--smp", str(args.smp), step],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                        timeout=900,
                    )
                    if result.returncode != 0:
                        tail = "\n".join(result.stdout.strip().splitlines()[-6:])
                        log(f"{step} failed on attempt {attempt} "
                            f"(rc={result.returncode}):\n{tail}")
                        break
                else:
                    # both steps succeeded
                    ok = True
                    break
                # The known QMP greeting race makes start flaky right after a
                # reboot; tear down and try again.
                subprocess.run(["python3", args.colo_py, "cleanup"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=120)
                time.sleep(5)
            set_maintenance(hosts, False)
            if ok:
                log(f"re-formed pair in {time.time() - started:.1f}s")
            else:
                log(f"re-form gave up after {args.attempts} attempts")
            unhealthy = 0
        except Exception as exc:  # never let the daemon die
            log(f"rejoin loop error (continuing): {type(exc).__name__}: {exc}")
            try:
                set_maintenance(hosts, False)
            except Exception:
                pass
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
