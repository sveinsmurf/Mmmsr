#!/usr/bin/env python3
"""COLO state agent: one tiny HTTP responder per host.

The workstation monitor polls these agents over plain HTTP, so the monitor
never needs SSH and the hosts do all the QMP work locally.

    tools/colo-agent.py --name 104 --qmp-port 4444 [--port 9100]

Endpoints:
    GET /state    JSON snapshot of the local QEMU
    GET /health   {"ok": true}

Run it as a systemd service; `tools/colo.py deploy-agent` installs it on both
hosts and enables it at boot. The agent is read-only: it only asks QMP
`query-migrate` and `query-status`, and reads the QEMU pid with pgrep.
"""

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CACHE_SECONDS = 0.2


def qmp_call(port, command, timeout=0.8):
    """Run one QMP command against the local QEMU, or return None."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return None
    try:
        sock.settimeout(timeout)
        stream = sock.makefile("rb")
        if not stream.readline():  # greeting
            return None
        sock.sendall(b'{"execute": "qmp_capabilities"}\n')
        while True:
            line = stream.readline()
            if not line:
                return None
            message = json.loads(line)
            if "return" in message:
                break
        sock.sendall(json.dumps({"execute": command}).encode() + b"\n")
        while True:
            line = stream.readline()
            if not line:
                return None
            message = json.loads(line)
            if "event" in message:
                continue
            if "return" in message:
                return message["return"]
            if "error" in message:
                return None
    except (OSError, ValueError):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def find_pid(qmp_port):
    """Return the QEMU pid whose QMP port matches, or None."""
    try:
        result = subprocess.run(
            ["pgrep", "-af", "qemu-system-x86_64"],
            text=True, capture_output=True, timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    needle = f"-qmp tcp:0.0.0.0:{qmp_port},"
    for line in result.stdout.splitlines():
        if needle in line:
            try:
                return int(line.split(None, 1)[0])
            except (ValueError, IndexError):
                return None
    return None


class Agent:
    def __init__(self, name, qmp_port):
        self.name = name
        self.qmp_port = qmp_port
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = 0.0

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            if self.cached is not None and now - self.cached_at < CACHE_SECONDS:
                return self.cached
        migrate = qmp_call(self.qmp_port, "query-migrate")
        colo = qmp_call(self.qmp_port, "query-colo-status")
        status = qmp_call(self.qmp_port, "query-status")
        pid = find_pid(self.qmp_port)
        data = {
            "host": self.name,
            "ok": True,
            "qemu": migrate is not None or status is not None,
            "pid": pid,
            "migrate": (migrate or {}).get("status"),
            "colo": (colo or {}).get("mode"),
            "has_socket": bool(migrate and "socket-address" in migrate),
            "running": bool(status and status.get("running")),
            "ts": time.time(),
        }
        with self.lock:
            self.cached = data
            self.cached_at = time.monotonic()
        return data


class Handler(BaseHTTPRequestHandler):
    agent = None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/state":
            body = json.dumps(self.agent.snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/health":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="host label, e.g. 104")
    parser.add_argument("--qmp-port", type=int, required=True, help="local QMP port")
    parser.add_argument("--port", type=int, default=9100, help="HTTP port (default 9100)")
    parser.add_argument("--bind", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    args = parser.parse_args(argv)

    Handler.agent = Agent(args.name, args.qmp_port)
    try:
        server = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as exc:
        print(f"cannot listen on {args.bind}:{args.port}: {exc}", file=sys.stderr)
        return 1
    print(f"colo-agent {args.name} on {args.bind}:{args.port} "
          f"(qmp {args.qmp_port})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
