#!/usr/bin/env python3
"""Live COLO monitor: an animated scene of the pair and the guest.

The guest (192.168.35.110) is a square floating in space. Whichever host is
currently primary holds it. When that host dies it dims and falls asleep and the
square drifts over to the other host, which catches it.

The monitor polls the two hosts' state agents over HTTP (see colo-agent.py and
`tools/colo.py deploy-agent`), so it never needs SSH and a dead host cannot stall
the page.

    tools/colo-monitor.py [--port 8080] [--interval 1.0] [--bind 0.0.0.0]

Open http://127.0.0.1:8080/ on the workstation, or over Tailscale at
http://<tailscale-ip>:8080/ since it binds all interfaces by default.
"""

import argparse
import errno
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOSTS = {
    "104": {"ip": "192.168.35.104", "agent": "http://192.168.35.104:9100",
            "ssh": "server@192.168.35.104", "qmp": 4444},
    "105": {"ip": "192.168.35.105", "agent": "http://192.168.35.105:9100",
            "ssh": "server@192.168.35.105", "qmp": 4445},
}
GUEST_IP = "192.168.35.110"
GUEST_PORTS = (25565, 9000)
DOWN_ROLES = {"down", "asleep"}
SERVING_ROLES = {"primary", "standalone"}

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mmmsr COLO</title>
<style>
  :root {
    --bg: #070b14;
    --panel: #111827;
    --line: #1f2a3d;
    --fg: #e6edf6;
    --dim: #7d8ba1;
    --ok: #35d07f;
    --warn: #f0b429;
    --bad: #ef4a5a;
    --accent: #6ea8fe;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--fg); overflow: hidden;
    font: 14px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  #scene { position: fixed; inset: 0; width: 100%; height: 100%; display: block; }
  .overlay {
    position: fixed; inset: 0; pointer-events: none;
    display: flex; flex-direction: column; justify-content: space-between;
    padding: 20px 24px;
  }
  .top { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
  .title { font-size: 13px; letter-spacing: .18em; text-transform: uppercase; color: var(--dim); }
  .title b { color: var(--fg); letter-spacing: .1em; }
  .pill {
    display: inline-flex; align-items: center; gap: 8px;
    border: 1px solid var(--line); border-radius: 999px;
    background: rgba(17, 24, 39, .78); padding: 7px 14px;
    font-size: 12px; letter-spacing: .12em; text-transform: uppercase;
    backdrop-filter: blur(6px);
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dim); }
  .dot.ok { background: var(--ok); box-shadow: 0 0 10px var(--ok); }
  .dot.warn { background: var(--warn); box-shadow: 0 0 10px var(--warn); }
  .dot.bad { background: var(--bad); box-shadow: 0 0 10px var(--bad); }
  .bottom { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; }
  .log {
    pointer-events: auto; max-width: 460px; max-height: 22vh; overflow: hidden;
    border: 1px solid var(--line); border-radius: 10px;
    background: rgba(17, 24, 39, .78); padding: 10px 12px;
    font-size: 11.5px; line-height: 1.7; backdrop-filter: blur(6px);
  }
  .log div { color: var(--dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .log div:first-child { color: var(--fg); }
  .log time { color: #4d5b70; }
  .foot { color: var(--dim); font-size: 11px; letter-spacing: .08em; text-align: right; }
</style>
</head>
<body>
<canvas id="scene"></canvas>
<div class="overlay">
  <div class="top">
    <div class="title">Mmmsr <b>COLO</b></div>
    <div class="pill"><span class="dot" id="dot"></span><span id="pair">connecting</span></div>
  </div>
  <div class="bottom">
    <div class="log" id="log"></div>
    <div class="foot" id="foot"></div>
  </div>
</div>
<script>
const canvas = document.getElementById('scene');
const ctx = canvas.getContext('2d');

// roundRect is recent; fall back to a plain rect on older browsers.
if (!ctx.roundRect) {
  CanvasRenderingContext2D.prototype.roundRect = function (x, y, w, h) {
    this.rect(x, y, w, h);
    return this;
  };
}
const dotEl = document.getElementById('dot');
const pairEl = document.getElementById('pair');
const logEl = document.getElementById('log');
const footEl = document.getElementById('foot');

const NODES = {
  '104': { label: '104', host: 'ezlab.min4' },
  '105': { label: '105', host: 'ezlab.min5' },
};

const state = {
  pair: 'connecting',
  hosts: {},
  guest: { up: false, ping: null, ports: {} },
  role: null,          // '104' | '105' | null  -- who is holding the square
  asleep: {},          // key -> true while it is dimmed
  updated: 0,
};

// ---- animation state -------------------------------------------------------
const anim = {
  t: 0,
  square: { x: 0, y: 0, vx: 0, vy: 0, glow: 0 },
  targets: {},
  sleeping: {},
  history: [],
};

const COLORS = {
  '104': '#6ea8fe',
  '105': '#c084fc',
};

// Colour for a role badge. Kept next to COLORS so drawNode can use it.
const ROLE_COLOR = {
  primary: '#6ee7a8',
  secondary: '#6ea8fe',
  standalone: '#f0b429',
  down: '#ef4a5a',
  asleep: '#ef4a5a',
  'no agent': '#ef4a5a',
  unreachable: '#ef4a5a',
  unknown: '#94a3b8',
};

function roleColor(role) {
  return ROLE_COLOR[role] || '#94a3b8';
}

function resize() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(innerWidth * dpr);
  canvas.height = Math.floor(innerHeight * dpr);
  canvas.style.width = innerWidth + 'px';
  canvas.style.height = innerHeight + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
addEventListener('resize', resize);
resize();

const NODE_BASE = 92;   // pod size in design units
const NODE_SCALE = 4;   // how much bigger than the original design

function nodeSize() {
  // 4x the original pod, but never so wide that the pair leaves the screen.
  return Math.min(NODE_BASE * NODE_SCALE, innerWidth * 0.40);
}

function nodePositions() {
  const w = innerWidth, h = innerHeight;
  const size = nodeSize();
  const pad = Math.max(size / 2 + 28, Math.min(w * 0.18, 220));
  const y = Math.max(size / 2 + 90, Math.min(h * 0.56, h - size / 2 - 130));
  return {
    '104': { x: pad, y },
    '105': { x: w - pad, y },
  };
}

// ---- drawing ---------------------------------------------------------------
function drawStars(t) {
  const w = innerWidth, h = innerHeight;
  ctx.save();
  for (let i = 0; i < 140; i++) {
    const sx = (i * 977) % w;
    const sy = (i * 613) % h;
    const tw = 0.35 + 0.65 * Math.abs(Math.sin(t * 0.0006 + i));
    ctx.globalAlpha = 0.10 + tw * 0.35;
    ctx.fillStyle = '#cfe0ff';
    ctx.fillRect(sx, sy, i % 7 === 0 ? 2 : 1, i % 7 === 0 ? 2 : 1);
  }
  ctx.restore();
}

function drawNode(key, pos, info, t) {
  const asleep = !!anim.sleeping[key];
  const color = COLORS[key];
  const size = NODE_BASE;        // design units; scaled up below
  const real = nodeSize();       // actual pixels
  const k = real / size;
  const cx = pos.x, cy = pos.y;
  const bob = asleep ? 0 : Math.sin(t * 0.0011 + (key === '104' ? 0 : 1.7)) * 4;

  // Halo (real pixels)
  const halo = ctx.createRadialGradient(cx, cy, real * 0.2, cx, cy, real * 1.7);
  halo.addColorStop(0, asleep ? 'rgba(120,130,150,0.20)' : color + '44');
  halo.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = halo;
  ctx.beginPath();
  ctx.arc(cx, cy, real * 1.7, 0, Math.PI * 2);
  ctx.fill();

  // Pod body, drawn in design units and scaled to fit.
  ctx.save();
  ctx.translate(cx, cy + bob);
  ctx.scale(k, k);

  ctx.beginPath();
  ctx.roundRect(-size / 2, -size / 2, size, size, 18);
  ctx.fillStyle = asleep ? 'rgba(26,32,45,0.75)' : 'rgba(22,30,46,0.9)';
  ctx.fill();
  ctx.lineWidth = asleep ? 1 : 2;
  ctx.strokeStyle = asleep ? 'rgba(120,130,150,0.35)' : color;
  ctx.globalAlpha = asleep ? 0.55 : 1;
  ctx.stroke();
  ctx.globalAlpha = 1;

  // Inner screen
  ctx.beginPath();
  ctx.roundRect(-size / 2 + 12, -size / 2 + 12, size - 24, size - 30, 10);
  ctx.fillStyle = asleep ? 'rgba(15,20,30,0.9)' : 'rgba(10,16,28,0.9)';
  ctx.fill();

  // LED strip
  const led = asleep ? 'rgba(120,130,150,0.35)' : color;
  ctx.fillStyle = led;
  for (let i = 0; i < 4; i++) {
    const on = asleep ? false : ((Math.floor(t * 0.004) + i) % 4 === 0);
    ctx.globalAlpha = on ? 1 : 0.18;
    ctx.fillRect(-size / 2 + 20 + i * 15, size / 2 - 16, 9, 3);
  }
  ctx.globalAlpha = 1;

  // Label
  ctx.fillStyle = asleep ? 'rgba(150,160,178,0.6)' : '#dbe6f7';
  ctx.font = '600 13px ui-monospace, Menlo, Consolas, monospace';
  ctx.textAlign = 'center';
  ctx.fillText(key, 0, -size / 2 + 30);
  ctx.font = '10px ui-monospace, Menlo, Consolas, monospace';
  ctx.fillStyle = asleep ? 'rgba(130,140,158,0.5)' : 'rgba(160,175,196,0.7)';
  ctx.fillText(NODES[key].host, 0, size / 2 - 26);

  // Status line under the pod
  ctx.textAlign = 'center';
  const role = info && info.role ? info.role : 'down';
  ctx.fillStyle = asleep ? 'rgba(130,140,158,0.55)' : roleColor(role);
  ctx.font = '600 11px ui-monospace, Menlo, Consolas, monospace';
  const label = asleep ? 'ASLEEP' : ROLE_LABEL[role] || role.toUpperCase();
  ctx.fillText(label, 0, size / 2 + 20);

  // Sleeping Zs
  if (asleep) {
    ctx.fillStyle = 'rgba(170,182,205,0.75)';
    ctx.font = '600 15px ui-monospace, Menlo, Consolas, monospace';
    for (let i = 0; i < 3; i++) {
      const phase = ((t * 0.0006) + i * 0.33) % 1;
      ctx.globalAlpha = Math.sin(phase * Math.PI) * 0.9;
      ctx.fillText('z', size * 0.28 + phase * 34, -size * 0.42 - phase * 46);
    }
    ctx.globalAlpha = 1;
  }
  ctx.restore();
}

function drawSquare(t) {
  const s = anim.square;
  const size = 34;
  const pulse = 1 + Math.sin(t * 0.003) * 0.04;

  // Trail
  for (let i = 0; i < anim.history.length; i++) {
    const p = anim.history[i];
    const age = (anim.history.length - i) / anim.history.length;
    ctx.globalAlpha = age * 0.20;
    ctx.fillStyle = '#8fd3ff';
    const sz = size * (0.35 + age * 0.45);
    ctx.fillRect(p.x - sz / 2, p.y - sz / 2, sz, sz);
  }
  ctx.globalAlpha = 1;

  // Glow
  const glow = ctx.createRadialGradient(s.x, s.y, 4, s.x, s.y, size * 2.6);
  glow.addColorStop(0, 'rgba(143,211,255,0.55)');
  glow.addColorStop(1, 'rgba(143,211,255,0)');
  ctx.fillStyle = glow;
  ctx.beginPath();
  ctx.arc(s.x, s.y, size * 2.6, 0, Math.PI * 2);
  ctx.fill();

  // The guest square
  ctx.save();
  ctx.translate(s.x, s.y);
  ctx.rotate(Math.sin(t * 0.0012) * 0.06);
  ctx.scale(pulse, pulse);
  ctx.beginPath();
  ctx.roundRect(-size / 2, -size / 2, size, size, 7);
  ctx.fillStyle = '#8fd3ff';
  ctx.shadowColor = 'rgba(143,211,255,0.9)';
  ctx.shadowBlur = 22;
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.lineWidth = 2;
  ctx.strokeStyle = 'rgba(225,245,255,0.95)';
  ctx.stroke();
  ctx.restore();

  // Label
  ctx.fillStyle = 'rgba(225,245,255,0.92)';
  ctx.font = '600 11px ui-monospace, Menlo, Consolas, monospace';
  ctx.textAlign = 'center';
  ctx.fillText('110', s.x, s.y + size / 2 + 22);

  // Tether to the holder
  const holder = state.role;
  if (holder && anim.targets[holder]) {
    const tp = anim.targets[holder];
    ctx.save();
    ctx.strokeStyle = 'rgba(143,211,255,0.28)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 7]);
    ctx.lineDashOffset = -t * 0.02;
    ctx.beginPath();
    ctx.moveTo(tp.x, tp.y - nodeSize() / 2 - 4);
    ctx.lineTo(s.x, s.y);
    ctx.stroke();
    ctx.restore();
  }
}

function loop(t) {
  anim.t = t;
  const w = innerWidth, h = innerHeight;

  // Background
  const sky = ctx.createLinearGradient(0, 0, 0, h);
  sky.addColorStop(0, '#070b14');
  sky.addColorStop(0.55, '#0a1120');
  sky.addColorStop(1, '#060a12');
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, w, h);

  drawStars(t);

  const positions = nodePositions();
  anim.targets = positions;

  for (const key of ['104', '105']) {
    drawNode(key, positions[key], state.hosts[key], t);
  }

  // Physics: the square is held by the primary, or floats to the new holder.
  const holder = state.role;
  let tx, ty;
  if (holder) {
    const p = positions[holder];
    tx = p.x;
    ty = p.y - nodeSize() / 2 - 40;
  } else {
    tx = w / 2;
    ty = h * 0.34;
  }
  const s = anim.square;
  const k = holder ? 0.022 : 0.004;
  const damp = holder ? 0.86 : 0.985;
  s.vx = (s.vx + (tx - s.x) * k) * damp;
  s.vy = (s.vy + (ty - s.y) * k) * damp;
  s.x += s.vx;
  s.y += s.vy;

  anim.history.push({ x: s.x, y: s.y });
  if (anim.history.length > 18) anim.history.shift();

  drawSquare(t);

  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);

// ---- UI --------------------------------------------------------------------
const ROLE_LABEL = {
  primary: 'PRIMARY', secondary: 'SECONDARY', standalone: 'SERVING',
  down: 'ASLEEP', asleep: 'ASLEEP', 'no agent': 'NO AGENT',
  unreachable: 'UNREACHABLE', unknown: 'UNKNOWN',
};

function log(msg) {
  const row = document.createElement('div');
  const stamp = new Date().toLocaleTimeString();
  row.innerHTML = '<time>' + stamp + '</time> ' + msg;
  logEl.prepend(row);
  while (logEl.children.length > 8) logEl.lastChild.remove();
}

function setSleeping(hosts) {
  for (const key of ['104', '105']) {
    const info = hosts[key];
    const asleep = !info || info.role === 'down' || info.role === 'asleep' ||
                   info.role === 'no agent' || info.role === 'unknown';
    if (anim.sleeping[key] !== asleep) {
      anim.sleeping[key] = asleep;
      log('.' + key + (asleep ? ' fell asleep' : ' woke up'));
    }
  }
}

function render(next) {
  const previousRole = state.role;
  state.pair = next.pair;
  state.hosts = next.hosts;
  state.guest = next.guest;
  state.role = next.role;
  state.updated = next.updated;

  setSleeping(next.hosts);

  pairEl.textContent = next.pair;
  dotEl.className = 'dot ' + (next.pair === 'COLO pair healthy' ? 'ok'
    : next.role ? 'warn' : 'bad');

  if (previousRole !== next.role) {
    if (next.role) log('110 is held by .' + next.role);
    else log('110 is floating free');
  }

  const g = next.guest;
  const ports = g.ports && g.ports.up;
  footEl.textContent = [
    '110 ' + (g.up ? 'up' : 'unreachable'),
    ports ? 'mc+dash' : '',
    g.ping != null ? Math.round(g.ping) + 'ms' : '',
    'updated ' + new Date().toLocaleTimeString(),
  ].filter(Boolean).join('  ·  ');
}

async function poll() {
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    render(await res.json());
  } catch (err) {
    pairEl.textContent = 'monitor offline';
    dotEl.className = 'dot bad';
  }
  setTimeout(poll, 1000);
}
poll();
</script>
</body>
</html>
"""


def role_of(info):
    """Map an agent snapshot to a scene role."""
    if not info or not info.get("ok"):
        return "no agent"
    if not info.get("qemu"):
        return "down"
    migrate = info.get("migrate")
    if migrate == "colo":
        # query-colo-status is authoritative. The socket-address heuristic is
        # wrong for a pair whose primary was originally started as secondary.
        colo = info.get("colo")
        if colo in ("primary", "secondary"):
            return colo
        return "secondary" if info.get("has_socket") else "primary"
    if migrate in ("failed", "cancelled"):
        return "down"
    if info.get("running"):
        return "standalone"
    return "unknown"


def pair_name(hosts):
    role_list = [role_of(hosts.get("104")), role_of(hosts.get("105"))]
    roles = set(role_list)
    dead = {"down", "no agent"}
    serving = {"primary", "standalone"}
    if roles <= dead:
        return "both hosts down"
    if sum(role in serving for role in role_list) >= 2:
        return "split brain"
    if roles == {"primary", "secondary"}:
        return "COLO pair healthy"
    if "primary" in roles or "standalone" in roles:
        if roles & dead:
            return "failover"
        if "unknown" in roles or "secondary" in roles:
            return "rejoining"
        return "serving alone"
    if "secondary" in roles:
        # A secondary whose peer is gone is waiting for the promotion to land.
        if roles & dead:
            return "failover"
        return "secondary without primary"
    if "unknown" in roles:
        return "rejoining"
    return "settling"


def fetch_agent(url, timeout):
    try:
        with urllib.request.urlopen(url + "/state", timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


SSH_PROBE = r'''
import json, socket, subprocess
port = %d
def qmp(cmd):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=0.8)
        s.settimeout(0.8)
        f = s.makefile("rb")
        f.readline()
        s.sendall(b'{"execute": "qmp_capabilities"}\n')
        while True:
            line = f.readline()
            if not line: return None
            m = json.loads(line)
            if "return" in m: break
        s.sendall(json.dumps({"execute": cmd}).encode() + b"\n")
        while True:
            line = f.readline()
            if not line: return None
            m = json.loads(line)
            if "event" in m: continue
            if "return" in m: return m["return"]
            if "error" in m: return None
    except Exception:
        return None
    finally:
        try: s.close()
        except Exception: pass
mig = qmp("query-migrate")
st = qmp("query-status")
pid = None
try:
    out = subprocess.run(["pgrep", "-af", "qemu-system-x86_64"],
                         capture_output=True, text=True, timeout=2).stdout
    needle = "-qmp tcp:0.0.0.0:%%d," %% port
    for line in out.splitlines():
        if needle in line:
            pid = int(line.split()[0]); break
except Exception:
    pass
print(json.dumps({
    "ok": True, "qemu": mig is not None or st is not None, "pid": pid,
    "migrate": (mig or {}).get("status"),
    "has_socket": bool(mig and "socket-address" in mig),
    "running": bool(st and st.get("running")),
}))
'''


def fetch_ssh(host, qmp_port, timeout):
    """Fallback probe over SSH when the host has no agent installed."""
    import subprocess
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
             "-o", "LogLevel=ERROR", host, "python3", "-", str(qmp_port)],
            input=SSH_PROBE % qmp_port, text=True,
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def tcp_probe(host, port, timeout):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ping_guest(host, timeout):
    import subprocess
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout)), host],
            text=True, capture_output=True, timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if "time=" in line:
            try:
                return float(line.split("time=")[1].split()[0])
            except (ValueError, IndexError):
                return None
    return None


class Monitor:
    def __init__(self, interval):
        self.interval = interval
        self.lock = threading.Lock()
        self.snapshot = {"pair": "connecting", "hosts": {}, "role": None,
                         "guest": {"up": False, "ping": None, "ports": {}},
                         "updated": 0}
        self.pool = ThreadPoolExecutor(max_workers=4)
        # Guest probes get their own pool: a slow SSH fallback to a host must
        # not delay the guest ping/TCP check, or the page reports a false
        # "port down" while the server is actually healthy.
        self.guest_pool = ThreadPoolExecutor(max_workers=2)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        while True:
            started = time.monotonic()
            # Fire every probe at once so the poll costs one round trip, not
            # the sum of them. A dead host must not delay the live one.
            futures = {
                key: self.pool.submit(self._probe, key, cfg)
                for key, cfg in HOSTS.items()
            }
            ping_future = self.guest_pool.submit(ping_guest, GUEST_IP, 1.0)
            # COLO withholds guest output until the next checkpoint, so a
            # healthy guest can take over a second to answer a SYN. A 0.5 s
            # timeout produced false "port down" readings on a live server.
            port_future = self.guest_pool.submit(
                tcp_probe, GUEST_IP, GUEST_PORTS[0], 2.0)
            hosts = {}
            for key, future in futures.items():
                try:
                    hosts[key] = future.result()
                except Exception:
                    hosts[key] = None
            roles = {key: role_of(hosts.get(key)) for key in HOSTS}
            role = None
            for key in ("104", "105"):
                if roles[key] in SERVING_ROLES:
                    role = key
                    break
            try:
                ping = ping_future.result()
            except Exception:
                ping = None
            try:
                port_open = port_future.result()
            except Exception:
                port_open = False
            guest = {
                "up": ping is not None or port_open,
                "ping": ping,
                "ports": {"up": port_open},
            }
            with self.lock:
                self.snapshot = {
                    "pair": pair_name(hosts),
                    "hosts": {k: self._decorate(k, hosts.get(k), roles[k])
                              for k in HOSTS},
                    "role": role,
                    "guest": guest,
                    "updated": time.time(),
                }
            elapsed = time.monotonic() - started
            time.sleep(max(0.05, self.interval - elapsed))

    def _probe(self, key, cfg):
        """Ask the agent first; fall back to SSH if it isn't installed."""
        info = fetch_agent(cfg["agent"], 1.2)
        if info is not None:
            info["via"] = "agent"
            return info
        info = fetch_ssh(cfg["ssh"], cfg["qmp"], 3.0)
        if info is not None:
            info["via"] = "ssh"
        return info

    @staticmethod
    def _decorate(key, info, role):
        if not info:
            return {"role": "unreachable", "migrate": None, "running": False,
                    "pid": None, "host": HOSTS[key]["ip"], "via": None}
        return {
            "role": role,
            "migrate": info.get("migrate"),
            "running": info.get("running"),
            "pid": info.get("pid"),
            "host": HOSTS[key]["ip"],
            "via": info.get("via"),
        }

    def get(self):
        with self.lock:
            return dict(self.snapshot)


class Handler(BaseHTTPRequestHandler):
    monitor = None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            body = json.dumps(self.monitor.get()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--bind", default="0.0.0.0",
                        help="listen address (default 0.0.0.0, so Tailscale works)")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)

    monitor = Monitor(args.interval)
    monitor.start()
    Handler.monitor = monitor
    try:
        server = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"port {args.port} is already in use. Something else owns it:",
                  file=sys.stderr)
            print(f"  ss -ltnp 'sport = :{args.port}'      # find the pid",
                  file=sys.stderr)
            print(f"  pkill -f colo-monitor                # or stop that process",
                  file=sys.stderr)
            print(f"  python3 tools/colo-monitor.py --port {args.port + 1}",
                  file=sys.stderr)
        else:
            print(f"cannot listen on {args.bind}:{args.port}: {exc}",
                  file=sys.stderr)
        return 1
    print(f"COLO monitor on http://{args.bind}:{args.port}/", flush=True)
    if args.bind in ("0.0.0.0", "::"):
        print(f"  reachable over Tailscale at http://<tailscale-ip>:{args.port}/",
              flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
