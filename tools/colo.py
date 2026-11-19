#!/usr/bin/env python3
"""Set up and control a QEMU COLO cluster using .104 as primary and .105 as secondary.

Run from a workstation that has passwordless SSH access to both hosts:

    tools/colo.py setup
    tools/colo.py start
    tools/colo.py status
    tools/colo.py failover
    tools/colo.py resume-replication
    tools/colo.py cleanup

The setup downloads the Arch cloud image, builds a cloud-init seed, and copies the
image to .105 over the direct 10.0.0.0/24 link. The QEMU guest is configured with
192.168.35.110 and MAC 52:54:00:12:34:56.
"""

import argparse
import hashlib
import json
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path


PRIMARY_HOST = "server@192.168.35.104"
SECONDARY_HOST = "server@192.168.35.105"
PRIMARY_IP = "192.168.35.104"
SECONDARY_IP = "192.168.35.105"
PRIMARY_DIRECT_IP = "10.0.0.2"
SECONDARY_DIRECT_IP = "10.0.0.1"

PRIMARY_DIR = "/mnt/vms/colo-primary"
SECONDARY_DIR = "/mnt/vms/colo-secondary"
PRIMARY_QMP = 4444
SECONDARY_QMP = 4445
PROXY_TX_PORT = 9003
PROXY_COMPARE_PORT = 9004
LOCAL_COMPARE_IN_PORT = 9001
LOCAL_COMPARE_OUT_PORT = 9005
NBD_PORT = 9999
INCOMING_PORT = 9998

VM_IP = "192.168.35.110"
VM_MAC = "52:54:00:12:34:56"
IMAGE_SIZE = "10G"
IMAGE_SIZE_BYTES = 10 * 1024 * 1024 * 1024


class HostSpec:
    """A COLO host: SSH target plus the resources that are fixed per box.

    The QMP port, TAP and image directory belong to the physical host, not to
    the COLO role, so they stay correct after a role swap.
    """

    def __init__(self, ssh_host, lan_ip, direct_ip, directory, qmp_port, tap):
        self.ssh_host = ssh_host
        self.lan_ip = lan_ip
        self.direct_ip = direct_ip
        self.directory = directory
        self.qmp_port = qmp_port
        self.tap = tap

    def peer(self):
        return SECONDARY if self is PRIMARY else PRIMARY


PRIMARY = HostSpec(PRIMARY_HOST, PRIMARY_IP, PRIMARY_DIRECT_IP, PRIMARY_DIR, PRIMARY_QMP, "tap2")
SECONDARY = HostSpec(SECONDARY_HOST, SECONDARY_IP, SECONDARY_DIRECT_IP, SECONDARY_DIR, SECONDARY_QMP, "tap0")


class CommandError(RuntimeError):
    pass


class QMPClient:
    def __init__(self, host, port, timeout=5.0, retries=10):
        # The QMP greeting is sent as soon as the socket is accepted, but QEMU
        # can be slow to accept right after start-up, so retry the connect +
        # greeting instead of failing the whole run on a transient race.
        last_error = None
        for attempt in range(retries):
            try:
                self.sock = socket.create_connection((host, port), timeout=timeout)
                self.sock.settimeout(timeout)
                self.stream = self.sock.makefile("rb")
                line = self.stream.readline()
                if not line:
                    raise CommandError("empty QMP greeting")
                greeting = json.loads(line)
                if "QMP" not in greeting:
                    raise CommandError(f"unexpected QMP greeting: {greeting}")
                self.execute("qmp_capabilities")
                return
            except (OSError, ValueError, CommandError) as exc:
                last_error = exc
                try:
                    self.sock.close()
                except Exception:
                    pass
                time.sleep(0.5)
        raise CommandError(f"QMP connect to {host}:{port} failed: {last_error}")

    def _read_message(self):
        while True:
            line = self.stream.readline()
            if not line:
                raise CommandError("QMP connection closed")
            message = json.loads(line)
            if "event" not in message and (
                    "return" in message or "error" in message):
                return message

    def _wait_for_result(self):
        while True:
            message = self._read_message()
            if "return" in message:
                return message["return"]
            if "error" in message:
                raise CommandError(message["error"])

    def execute(self, command, arguments=None):
        request = {"execute": command}
        if arguments is not None:
            request["arguments"] = arguments
        payload = json.dumps(request) + "\n"
        self.sock.sendall(payload.encode())
        return self._wait_for_result()

    def close(self):
        self.stream.close()
        self.sock.close()


def remote(host, script, sudo=False, check=True, timeout=900):
    if sudo:
        script = f"sudo -n bash -lc {shlex.quote(script)}"
    command = ["ssh", "-o", "BatchMode=yes", host, script]
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode != 0:
        raise CommandError(
            f"remote command failed on {host} ({result.returncode}):\n"
            f"{result.stdout}{result.stderr}"
        )
    return result.stdout.strip(), result.stderr.strip()


def local(args, check=True):
    result = subprocess.run(args, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise CommandError(
            f"local command failed ({result.returncode}): {' '.join(args)}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result.stdout.strip(), result.stderr.strip()


def remote_port_open(host, port):
    try:
        socket.create_connection((host, port), timeout=1).close()
        return True
    except OSError:
        return False


def wait_for_port(host, port, description, timeout=60):
    print(f"Waiting for {description} on {host}:{port}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if remote_port_open(host, port):
            # The socket can accept before QEMU's QMP greeting is ready, so
            # confirm the greeting too. This removes the intermittent
            # "timed out waiting for ... QMP" race after a restart.
            try:
                qmp = QMPClient(host, port, timeout=5.0, retries=1)
                qmp.close()
                return
            except (OSError, CommandError):
                pass
        time.sleep(0.5)
    raise CommandError(f"timed out waiting for {description} on {host}:{port}")


def established_colo_channels(host):
    output, _ = remote(
        host,
        "ss -Htn state established | grep -E ':(9003|9004)\\b' || true",
    )
    return [line for line in output.splitlines() if line.strip()]


def wait_for_colo_channels(timeout=30):
    print("Waiting for both COLO proxy channels to establish")
    deadline = time.time() + timeout
    while time.time() < deadline:
        channels = {
            host: established_colo_channels(host)
            for host in (PRIMARY_HOST, SECONDARY_HOST)
        }
        if all(len(lines) >= 2 for lines in channels.values()):
            return
        time.sleep(1)
    details = "\n".join(
        f"{host}:\n" + ("\n".join(lines) or "  no established channels")
        for host, lines in channels.items()
    )
    raise CommandError(f"COLO proxy channels did not establish within {timeout}s\n{details}")


def stop_qemu(host, pidfile, qmp_port):
    """Stop the COLO QEMU on a host, identified by its QMP port.

    The QEMU ``-name`` label records the role at start-up, so it goes stale
    after a role swap (the same process may be primary or secondary). The QMP
    port is fixed per physical host, so match on that instead.
    """
    script = f"""
if [ -f '{pidfile}' ]; then
    pid=$(cat '{pidfile}' 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.1
        done
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f '{pidfile}'
fi
pkill -f '^qemu-system-x86_64 .* -qmp tcp:0.0.0.0:{qmp_port},' 2>/dev/null || true
"""
    remote(host, script)


def ensure_tap(host, tap):
    script = f"""
if ! ip link show '{tap}' >/dev/null 2>&1; then
    ip tuntap add dev '{tap}' mode tap user server
fi
ip link set '{tap}' master br0 2>/dev/null || true
ip link set '{tap}' up
"""
    remote(host, script, sudo=True)


def seed_files(public_keys):
    keys = "\n".join(f"      - {key}" for key in public_keys)
    user_data = f"""#cloud-config
users:
  - name: svein
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
{keys}
ssh_pwauth: false
write_files:
  - path: /etc/systemd/system/ferrumc.service
    permissions: '0644'
    content: |
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
runcmd:
  - chown -R svein:svein /home/svein
  - [systemctl, daemon-reload]
  - [sh, -c, 'if [ -x /home/svein/ferrumc/ferrumc ]; then systemctl enable --now ferrumc.service; fi']
"""
    network_config = f"""version: 2
ethernets:
  nic:
    match:
      macaddress: "{VM_MAC}"
    addresses:
      - {VM_IP}/24
    routes:
      - to: default
        via: 192.168.35.1
    nameservers:
      addresses:
        - 192.168.35.1
"""
    digest = hashlib.sha256((user_data + network_config).encode()).hexdigest()[:12]
    return user_data, network_config, f"instance-id: colo-mmmsr-{digest}\n"


def build_seed(args):
    print("Building cloud-init seed on the primary host")
    public_key, _ = remote(PRIMARY_HOST, "head -n1 ~/.ssh/authorized_keys")
    if not public_key:
        raise CommandError("no public key found for server@" + PRIMARY_IP)
    public_keys = [public_key]
    try:
        local_key = Path.home().joinpath(".ssh/id_ed25519.pub").read_text().strip()
    except OSError:
        local_key = ""
    if local_key and local_key not in public_keys:
        public_keys.append(local_key)
    user_data, network_config, meta_data = seed_files(public_keys)
    script = f"""
mkdir -p '{PRIMARY_DIR}/seed'
cat > '{PRIMARY_DIR}/seed/user-data' <<'MMMSR_EOF'
{user_data}MMMSR_EOF
cat > '{PRIMARY_DIR}/seed/network-config' <<'MMMSR_EOF'
{network_config}MMMSR_EOF
cat > '{PRIMARY_DIR}/seed/meta-data' <<'MMMSR_EOF'
{meta_data}MMMSR_EOF
cd '{PRIMARY_DIR}/seed'
xorriso -as mkisofs -o ../seed.iso -V cidata -J -r user-data meta-data network-config
"""
    remote(PRIMARY_HOST, script, sudo=True)


def sync_image(args, src=PRIMARY, dst=SECONDARY):
    """Copy base.qcow2 + seed.iso from the surviving host to the returning one.

    When a host comes back after a power pull its copy of the image is stale
    (or gone), so the disk the replication resync will target must be refreshed
    from the host that kept serving the guest.
    """
    print(f"Copying the guest image from {src.lan_ip} to {dst.lan_ip} over the direct link")
    remote(src.ssh_host, f"test -f '{src.directory}/base.qcow2'")
    remote(dst.ssh_host, f"mkdir -p '{dst.directory}'", sudo=True)
    remote(dst.ssh_host, f"chown server:server '{dst.directory}'", sudo=True)
    remote(
        src.ssh_host,
        f"scp -q '{src.directory}/base.qcow2' "
        f"server@{dst.direct_ip}:'{dst.directory}/base.qcow2'",
        timeout=900,
    )
    remote(
        src.ssh_host,
        f"scp -q '{src.directory}/seed.iso' "
        f"server@{dst.direct_ip}:'{dst.directory}/seed.iso'",
    )


def ensure_primary_image(_args):
    script = f"""
mkdir -p '{PRIMARY_DIR}'
if [ ! -f '{PRIMARY_DIR}/base.qcow2' ]; then
    curl -fL --retry 3 https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2 -o '{PRIMARY_DIR}/base.qcow2'
fi
current_size=$(qemu-img info --output=json -f qcow2 '{PRIMARY_DIR}/base.qcow2' | sed -n 's/.*"virtual-size": *\\([0-9]*\\).*/\\1/p')
if [ "$current_size" != '{IMAGE_SIZE_BYTES}' ]; then
    qemu-img resize '{PRIMARY_DIR}/base.qcow2' {IMAGE_SIZE}
fi
chown -R server:server '{PRIMARY_DIR}'
"""
    remote(PRIMARY_HOST, script, sudo=True, timeout=1800)


def reset_secondary_disks(spec=SECONDARY):
    print(f"Resetting COLO replication disks on {spec.lan_ip}")
    script = f"""
qemu-img create -f qcow2 '{spec.directory}/secondary-active.qcow2' {IMAGE_SIZE}
qemu-img create -f qcow2 '{spec.directory}/secondary-hidden.qcow2' {IMAGE_SIZE}
chown server:server '{spec.directory}/secondary-active.qcow2' '{spec.directory}/secondary-hidden.qcow2'
"""
    remote(spec.ssh_host, script, sudo=True)


def qemu_launch_options(args):
    if args.debug:
        return "-display none -daemonize -D /tmp/qemu-debug.log"
    return "-display none -daemonize"


def secondary_qemu_command(args, spec):
    """QEMU command line for a host running as the COLO secondary.

    ``spec`` is the HostSpec of the host that will run as secondary; its direct
    link peer (the primary) is where the red0/red1 chardevs connect.
    """
    peer = spec.peer()
    return f"""qemu-system-x86_64 \\
  -enable-kvm -cpu qemu64,kvmclock=on -m {args.memory} -smp {args.smp} \\
  -name colo-secondary \\
  -qmp tcp:0.0.0.0:{spec.qmp_port},server=on,wait=off \\
  -chardev socket,id=red0,host={peer.direct_ip},port={PROXY_TX_PORT},reconnect-ms=1000 \\
  -chardev socket,id=red1,host={peer.direct_ip},port={PROXY_COMPARE_PORT},reconnect-ms=1000 \\
  -object filter-redirector,id=f1,netdev=hn0,queue=tx,indev=red0,vnet_hdr_support=on \\
  -object filter-redirector,id=f2,netdev=hn0,queue=rx,outdev=red1,vnet_hdr_support=on \\
  -object filter-rewriter,id=rew0,netdev=hn0,queue=all,vnet_hdr_support=on \\
  -netdev tap,id=hn0,ifname={spec.tap},script=no,downscript=no,vhost=off \\
  -device rtl8139,id=e0,netdev=hn0 \\
  -drive if=none,id=parent0,file.filename={spec.directory}/base.qcow2,driver=qcow2 \\
  -drive if=none,id=childs0,driver=replication,mode=secondary,file.driver=qcow2,top-id=colo-disk0,file.file.filename={spec.directory}/secondary-active.qcow2,file.backing.driver=qcow2,file.backing.file.filename={spec.directory}/secondary-hidden.qcow2,file.backing.backing=parent0 \\
  -drive if=ide,id=colo-disk0,driver=quorum,read-pattern=fifo,vote-threshold=1,children.0=childs0 \\
  -drive file={spec.directory}/seed.iso,if=virtio,media=cdrom \\
  -incoming tcp:0.0.0.0:{INCOMING_PORT} \\
  -serial file:/tmp/colo-secondary.log {qemu_launch_options(args)}
"""


def primary_qemu_command(args, spec):
    """QEMU command line for a host running as the COLO primary."""
    return f"""qemu-system-x86_64 \\
  -enable-kvm -cpu qemu64,kvmclock=on -m {args.memory} -smp {args.smp} \\
  -name colo-primary \\
  -qmp tcp:0.0.0.0:{spec.qmp_port},server=on,wait=off \\
  -chardev socket,id=mirror0,host=0.0.0.0,port={PROXY_TX_PORT},server=on,wait=off \\
  -chardev socket,id=compare1,host=0.0.0.0,port={PROXY_COMPARE_PORT},server=on,wait=off \\
  -chardev socket,id=compare0,host=127.0.0.1,port={LOCAL_COMPARE_IN_PORT},server=on,wait=off \\
  -chardev socket,id=compare0-0,host=127.0.0.1,port={LOCAL_COMPARE_IN_PORT} \\
  -chardev socket,id=compare_out,host=127.0.0.1,port={LOCAL_COMPARE_OUT_PORT},server=on,wait=off \\
  -chardev socket,id=compare_out0,host=127.0.0.1,port={LOCAL_COMPARE_OUT_PORT} \\
  -object filter-mirror,id=m0,netdev=hn0,queue=tx,outdev=mirror0,vnet_hdr_support=on \\
  -object filter-redirector,netdev=hn0,id=redire0,queue=rx,indev=compare_out,vnet_hdr_support=on \\
  -object filter-redirector,netdev=hn0,id=redire1,queue=rx,outdev=compare0,vnet_hdr_support=on \\
  -object iothread,id=iothread1 \\
  -object colo-compare,id=comp0,primary_in=compare0-0,secondary_in=compare1,outdev=compare_out0,iothread=iothread1,vnet_hdr_support=on \\
  -netdev tap,id=hn0,ifname={spec.tap},script=no,downscript=no,vhost=off \\
  -device rtl8139,id=e0,netdev=hn0 \\
  -drive if=ide,id=colo-disk0,driver=quorum,read-pattern=fifo,vote-threshold=1,children.0.file.filename={spec.directory}/base.qcow2,children.0.driver=qcow2 \\
  -drive file={spec.directory}/seed.iso,if=virtio,media=cdrom \\
  -serial file:/tmp/colo-primary.log -S {qemu_launch_options(args)}
"""


def start_secondary_qemu(args, spec=SECONDARY):
    remote(spec.ssh_host, secondary_qemu_command(args, spec))


def start_primary_qemu(args, spec=PRIMARY):
    remote(spec.ssh_host, primary_qemu_command(args, spec))


def setup(args):
    print("Verifying direct and LAN SSH connectivity")
    remote(PRIMARY_HOST, ":")
    remote(SECONDARY_HOST, ":")
    remote(PRIMARY_HOST, f"ping -c 1 {SECONDARY_DIRECT_IP}")
    stop_qemu(PRIMARY_HOST, "/tmp/colo-primary.pid", PRIMARY_QMP)
    stop_qemu(SECONDARY_HOST, "/tmp/colo-secondary.pid", SECONDARY_QMP)
    ensure_primary_image(args)
    if not args.no_seed:
        build_seed(args)
    if not args.no_image_sync:
        sync_image(args)
    reset_secondary_disks()
    ensure_tap(PRIMARY_HOST, "tap2")
    ensure_tap(SECONDARY_HOST, "tap0")
    for host in (PRIMARY_HOST, SECONDARY_HOST):
        remote(host, "grep -q '^allow br0$' /etc/qemu/bridge.conf 2>/dev/null "
                     "|| echo 'allow br0' > /etc/qemu/bridge.conf", sudo=True)
        remote(host, "chmod 666 /dev/net/tun", sudo=True)
    remote(PRIMARY_HOST, f"chown -R server:server '{PRIMARY_DIR}'", sudo=True)
    remote(SECONDARY_HOST, f"chown -R server:server '{SECONDARY_DIR}'", sudo=True)
    print("Setup complete")


def start_secondary(args):
    print("Stopping any stale secondary QEMU")
    stop_qemu(SECONDARY_HOST, "/tmp/colo-secondary.pid", SECONDARY_QMP)
    ensure_tap(SECONDARY_HOST, "tap0")
    start_secondary_qemu(args)
    wait_for_port(SECONDARY_IP, SECONDARY_QMP, "secondary QMP")
    qmp = QMPClient(SECONDARY_IP, SECONDARY_QMP)
    try:
        qmp.execute(
            "migrate-set-capabilities",
            {"capabilities": [
                {"capability": "return-path", "state": True},
                {"capability": "x-colo", "state": True},
            ]},
        )
        qmp.execute("nbd-server-start", {
            "addr": {"type": "inet", "data": {"host": "0.0.0.0", "port": str(NBD_PORT)}}
        })
        qmp.execute("nbd-server-add", {"device": "parent0", "writable": True})
    finally:
        qmp.close()
    print("Secondary is ready and waiting for incoming COLO migration")


def start_primary(args):
    print("Stopping any stale primary QEMU")
    stop_qemu(PRIMARY_HOST, "/tmp/colo-primary.pid", PRIMARY_QMP)
    ensure_tap(PRIMARY_HOST, "tap2")
    start_primary_qemu(args)
    wait_for_port(PRIMARY_IP, PRIMARY_QMP, "primary QMP")
    print("Primary is paused and ready for COLO migration")


def migrate(args):
    wait_for_colo_channels()
    print("Attaching the secondary's NBD backing image to the primary")
    qmp = QMPClient(PRIMARY_IP, PRIMARY_QMP)
    try:
        qmp.execute("blockdev-add", {
            "driver": "nbd",
            "node-name": "nbd0",
            "server": {"type": "inet", "host": SECONDARY_IP, "port": str(NBD_PORT)},
            "export": "parent0",
            "detect-zeroes": "on",
        })
        qmp.execute("x-blockdev-change", {"parent": "colo-disk0", "node": "nbd0"})
        qmp.execute(
            "migrate-set-capabilities",
            {"capabilities": [
                {"capability": "return-path", "state": True},
                {"capability": "x-colo", "state": True},
            ]},
        )
        qmp.execute("migrate", {"uri": f"tcp:{SECONDARY_IP}:{INCOMING_PORT}"})
        deadline = time.time() + args.timeout
        while True:
            result = qmp.execute("query-migrate")
            status = result.get("status")
            print(f"migration status: {status}")
            if status == "colo":
                break
            if status in ("failed", "cancelled"):
                raise CommandError(f"migration did not complete: {result}")
            if time.time() >= deadline:
                raise CommandError(f"migration timed out: {result}")
            time.sleep(1)
        if args.checkpoint_delay is not None:
            qmp.execute("migrate-set-parameters", {
                "x-checkpoint-delay": args.checkpoint_delay
            })
    finally:
        qmp.close()
    print("COLO lock-stepping is active")


def qmp_migrate_status(host, port, timeout=4.0):
    """Return the query-migrate dict, or None if QMP is unreachable."""
    try:
        qmp = QMPClient(host, port, timeout=timeout, retries=1)
    except (OSError, CommandError):
        return None
    try:
        return qmp.execute("query-migrate")
    finally:
        qmp.close()


def qmp_is_running(host, port):
    try:
        qmp = QMPClient(host, port, retries=1)
    except (OSError, CommandError):
        return None
    try:
        return bool(qmp.execute("query-status").get("running"))
    finally:
        qmp.close()


def chardev_labels(qmp):
    return {c["label"] for c in qmp.execute("query-chardev")}


def object_ids(qmp):
    return {o["name"] for o in qmp.execute("qom-list", {"path": "/objects"})}


def primary_chardevs():
    """The chardevs a COLO primary needs, as (id, backend) pairs."""
    def inet(host, port, server):
        return {"type": "socket", "data": {
            "addr": {"type": "inet", "data": {"host": host, "port": str(port)}},
            "server": server,
        }}
    return [
        ("mirror0", inet("0.0.0.0", PROXY_TX_PORT, True)),
        ("compare1", inet("0.0.0.0", PROXY_COMPARE_PORT, True)),
        ("compare0", inet("127.0.0.1", LOCAL_COMPARE_IN_PORT, True)),
        ("compare0-0", inet("127.0.0.1", LOCAL_COMPARE_IN_PORT, False)),
        ("compare_out", inet("127.0.0.1", LOCAL_COMPARE_OUT_PORT, True)),
        ("compare_out0", inet("127.0.0.1", LOCAL_COMPARE_OUT_PORT, False)),
    ]


def primary_filter_objects(insert_before=None):
    """The objects a COLO primary needs.

    When the host previously ran as a secondary it still has its filter-rewriter
    (``rew0``); the docs require the new filters be inserted *before* it, so pass
    ``insert_before="rew0"`` in that case.
    """
    place = {}
    if insert_before:
        place = {"insert": "before", "position": f"id={insert_before}"}
    return [
        {"qom-type": "filter-mirror", "id": "m0", "netdev": "hn0",
         "queue": "tx", "outdev": "mirror0", "vnet_hdr_support": True, **place},
        {"qom-type": "filter-redirector", "id": "redire0", "netdev": "hn0",
         "queue": "rx", "indev": "compare_out", "vnet_hdr_support": True, **place},
        {"qom-type": "filter-redirector", "id": "redire1", "netdev": "hn0",
         "queue": "rx", "outdev": "compare0", "vnet_hdr_support": True, **place},
        {"qom-type": "iothread", "id": "iothread1"},
        {"qom-type": "colo-compare", "id": "comp0", "primary_in": "compare0-0",
         "secondary_in": "compare1", "outdev": "compare_out0",
         "iothread": "iothread1", "vnet_hdr_support": True},
    ]


def drop_secondary_leftovers(qmp):
    """Remove the objects/chardevs a promoted secondary still carries.

    After a secondary promotes it keeps its own redirectors (f1/f2) and the
    red0/red1 chardevs that pointed at the dead primary. They must go before the
    primary filters are added, or the tx/rx chains run through a dead socket.
    """
    present = object_ids(qmp)
    for obj in ("f2", "f1"):
        if obj in present:
            print(f"  object-del {obj}")
            qmp.execute("object-del", {"id": obj})
    labels = chardev_labels(qmp)
    for cid in ("red1", "red0"):
        if cid in labels:
            print(f"  chardev-remove {cid}")
            qmp.execute("chardev-remove", {"id": cid})


def add_primary_colo_plumbing(qmp, insert_before=None):
    """Hot-add the COLO chardevs and objects to a running primary.

    Only what is missing is added, so this is safe to re-run and works whether
    the host was originally the primary (everything already present) or is a
    promoted secondary that still carries its own rewriter.
    """
    labels = chardev_labels(qmp)
    for cid, backend in primary_chardevs():
        if cid in labels:
            continue
        print(f"  chardev-add {cid}")
        qmp.execute("chardev-add", {"id": cid, "backend": backend})

    present = object_ids(qmp)
    for obj in primary_filter_objects(insert_before):
        if obj["id"] in present:
            continue
        print(f"  object-add {obj['id']}")
        qmp.execute("object-add", obj)


def stop_all_colo_qemu(spec):
    """Stop the COLO QEMU on a host, whatever role it was started as."""
    for pidfile in ("/tmp/colo-primary.pid", "/tmp/colo-secondary.pid"):
        stop_qemu(spec.ssh_host, pidfile, spec.qmp_port)


def start_returning_secondary(args, returner, survivor):
    """Start the returning host as the COLO secondary and ready its NBD export."""
    ensure_tap(returner.ssh_host, returner.tap)
    remote(returner.ssh_host, "chmod 666 /dev/net/tun", sudo=True)
    remote(returner.ssh_host,
           "grep -q '^allow br0$' /etc/qemu/bridge.conf 2>/dev/null "
           "|| echo 'allow br0' > /etc/qemu/bridge.conf", sudo=True)
    start_secondary_qemu(args, returner)
    wait_for_port(returner.lan_ip, returner.qmp_port, "returning secondary QMP")
    qmp = QMPClient(returner.lan_ip, returner.qmp_port)
    try:
        qmp.execute("migrate-set-capabilities", {"capabilities": [
            {"capability": "return-path", "state": True},
            {"capability": "x-colo", "state": True},
        ]})
        qmp.execute("nbd-server-start", {
            "addr": {"type": "inet", "data": {"host": "0.0.0.0", "port": str(NBD_PORT)}}
        })
        qmp.execute("nbd-server-add", {"device": "parent0", "writable": True})
    finally:
        qmp.close()
    print("Returning host is up as the secondary and waiting for the resync")


def find_mirror_job(qmp, job_id):
    jobs = qmp.execute("query-block-jobs")
    return next((j for j in jobs
                 if j.get("device") == job_id or j.get("id") == job_id), None)


def wait_for_mirror(qmp, job_id, timeout):
    """Wait until the mirror has copied everything into the NBD target.

    The job uses the default flags, so reaching ``ready`` leaves the device
    untouched (the pivot only happens on ``block-job-complete``). The caller
    cancels it after the guest is stopped, which aborts the pivot and lets the
    default ``auto-dismiss`` remove the job's ``mirror_top`` node so that
    ``x-blockdev-change`` can add the NBD child.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        job = find_mirror_job(qmp, job_id)
        if job is None:
            return
        status = job.get("status")
        if status in ("ready", "concluded"):
            return
        if status in ("null",) or job.get("error"):
            raise CommandError(f"resync job ended badly: {job}")
        progress = (job.get("offset", 0), job.get("len", 0), status)
        if progress != last:
            print(f"  resync {progress[0]}/{progress[1]} ({status})")
            last = progress
        time.sleep(0.5)
    raise CommandError(f"resync did not finish within {timeout}s")


def finish_mirror_job(qmp, job_id):
    """Cancel the mirror job so it leaves the graph without pivoting.

    ``block-job-cancel`` on a *ready* mirror aborts it instead of completing the
    pivot, which is what we want: the device is switched to the replicated NBD
    child explicitly afterwards. The default ``auto-dismiss`` then removes the
    job's ``mirror_top`` node; otherwise ``x-blockdev-change`` cannot add the
    NBD child.
    """
    if find_mirror_job(qmp, job_id) is None:
        return
    print("  block-job-cancel resync (do not pivot)")
    try:
        qmp.execute("block-job-cancel", {"device": job_id})
    except CommandError as exc:
        print(f"  block-job-cancel: {exc} (continuing)")

    deadline = time.time() + 15
    while time.time() < deadline:
        if find_mirror_job(qmp, job_id) is None:
            return
        time.sleep(0.05)
    raise CommandError("resync job did not dismiss after cancel")


def wait_for_colo(qmp, timeout):
    """Wait until query-migrate reports ``colo``; returns the final status."""
    deadline = time.time() + timeout
    while True:
        result = qmp.execute("query-migrate")
        status = result.get("status")
        print(f"migration status: {status}")
        if status == "colo":
            return status
        if status in ("failed", "cancelled"):
            raise CommandError(f"COLO migration failed: {result}")
        if time.time() >= deadline:
            raise CommandError(f"COLO migration timed out: {result}")
        time.sleep(0.5)


def resume_replication_on_survivor(args, survivor, returner):
    """Primary resume replication: re-COLO with the survivor as primary.

    Implements the QEMU "Primary resume replication" sequence (and the
    "Secondary resume replication" variant when the survivor was a promoted
    secondary): resync the survivor's disk into the new secondary with
    drive-mirror, stop the guest only across the disk switch-over, hot-add the
    COLO plumbing, then run a COLO migration. The guest is never rebooted, so
    connected players stay connected.
    """
    qmp = QMPClient(survivor.lan_ip, survivor.qmp_port)
    stopped = False
    started = time.time()
    last = started

    def step(message):
        nonlocal last
        now = time.time()
        print(f"  [{now - started:6.2f}s +{now - last:5.2f}s] {message}", flush=True)
        last = now

    try:
        status = qmp.execute("query-migrate").get("status")
        if status == "colo":
            print(f"{survivor.lan_ip} is already in COLO mode; nothing to resume")
            return

        print(f"Resyncing the disk to {returner.lan_ip} with drive-mirror")
        qmp.execute("drive-mirror", {
            "device": "colo-disk0",
            "job-id": "resync",
            "target": f"nbd://{returner.direct_ip}:{NBD_PORT}/parent0",
            "mode": "existing",
            "format": "raw",
            "sync": "full",
        })
        wait_for_mirror(qmp, "resync", args.timeout)
        step("disk resynced")

        # The guest is frozen only across the switch-over: the local copy and
        # the mirrored copy are identical at this point.
        print("Freezing the guest briefly and switching to the replicated disk")
        qmp.execute("stop")
        stopped = True
        finish_mirror_job(qmp, "resync")
        step("mirror job finished")

        qmp.execute("blockdev-add", {
            "driver": "nbd",
            "node-name": "nbd0",
            "server": {"type": "inet", "host": returner.direct_ip, "port": str(NBD_PORT)},
            "export": "parent0",
            "detect-zeroes": "on",
        })
        qmp.execute("x-blockdev-change", {"parent": "colo-disk0", "node": "nbd0"})
        step("NBD child attached")

        # A host that was serving as the secondary keeps its filter-rewriter
        # (``rew0``); the docs require the new filters be inserted in front of
        # it. A host that ran primary-failover has no rewriter.
        insert_before = "rew0" if "rew0" in object_ids(qmp) else None
        if insert_before:
            print("  surviving host was a promoted secondary; dropping its leftovers")
            drop_secondary_leftovers(qmp)
        add_primary_colo_plumbing(qmp, insert_before=insert_before)
        step("COLO plumbing added")

        qmp.execute("migrate-set-capabilities", {"capabilities": [
            {"capability": "return-path", "state": True},
            {"capability": "x-colo", "state": True},
        ]})
        # COLO's initial migration stops the guest and copies all RAM, then
        # restarts it once the first checkpoint completes; resuming mid-flight
        # is forbidden by QEMU because it can desync the checkpoint. So the
        # guest stays paused from the disk switch-over until COLO is live.
        qmp.execute("migrate", {"uri": f"tcp:{returner.direct_ip}:{INCOMING_PORT}"})
        step("migrate started")
        wait_for_colo(qmp, args.timeout)
        step("COLO live")
        running = qmp_is_running(survivor.lan_ip, survivor.qmp_port)
        if running is not True:
            print("  resuming guest after resume migration")
            try:
                qmp.execute("cont")
            except CommandError as exc:
                print(f"  cont: {exc} (COLO usually resumes it)")
            running = qmp_is_running(survivor.lan_ip, survivor.qmp_port)
        # Only disarm the unfreeze safety net once the guest is really running.
        stopped = running is not True
        step("guest running" if running is True else "guest still paused")
    finally:
        if stopped:
            # Never leave the guest frozen because a step failed mid-way.
            try:
                if qmp_is_running(survivor.lan_ip, survivor.qmp_port) is not True:
                    print("  resume failed; unfreezing the guest")
                    try:
                        qmp.execute("cont")
                    except CommandError as exc:
                        print(f"  cont: {exc}")
            except (OSError, CommandError):
                pass
        qmp.close()
    print(f"COLO resumed: {survivor.lan_ip} primary, {returner.lan_ip} secondary")


def resolve_survivor(args):
    """Work out which host kept serving the guest and which is returning."""
    p_status = qmp_migrate_status(PRIMARY_IP, PRIMARY_QMP)
    s_status = qmp_migrate_status(SECONDARY_IP, SECONDARY_QMP)
    choice = args.survivor
    if choice in ("primary", PRIMARY_IP):
        return PRIMARY, SECONDARY
    if choice in ("secondary", SECONDARY_IP):
        return SECONDARY, PRIMARY
    # auto
    if p_status is None and s_status is None:
        raise CommandError("neither host answers QMP; nothing to resume")
    if s_status is None:
        return PRIMARY, SECONDARY
    if p_status is None:
        return SECONDARY, PRIMARY
    p_run = qmp_is_running(PRIMARY_IP, PRIMARY_QMP)
    s_run = qmp_is_running(SECONDARY_IP, SECONDARY_QMP)
    if p_run and not s_run:
        return PRIMARY, SECONDARY
    if s_run and not p_run:
        return SECONDARY, PRIMARY
    raise CommandError(
        f"cannot tell which host is the survivor (primary={p_status}, "
        f"secondary={s_status}); pass --survivor")


def adopt_survivor_shape(args, survivor):
    """Match the returning QEMU's RAM/vCPU to the running survivor.

    Migration requires identical memory (and vCPU count); the survivor is
    already running, so its live shape wins over whatever was passed on the
    command line.
    """
    qmp = QMPClient(survivor.lan_ip, survivor.qmp_port)
    try:
        memory = qmp.execute("query-memory-size-summary")["base-memory"]
        cpus = len(qmp.execute("query-cpus-fast"))
    finally:
        qmp.close()
    args.memory = memory // (1024 * 1024)
    args.smp = cpus
    print(f"Matching the survivor's shape: {args.memory} MiB, {args.smp} vCPU(s)")


def prepare_returning_disks(survivor, returner):
    """Ready the returning host's disks for a COLO resync.

    The guest's live disk (the survivor's ``base.qcow2``) is being written to by
    a running QEMU, so it must not be copied out from under it. Instead the
    returning host gets a fresh, correctly sized ``base.qcow2`` plus fresh
    replication disks, and ``drive-mirror`` fills them from the survivor. Only
    the small, read-only ``seed.iso`` is copied.
    """
    print(f"Preparing replication disks on {returner.lan_ip}")
    remote(returner.ssh_host, f"mkdir -p '{returner.directory}'", sudo=True)
    # The direct link is only trusted in one direction after setup, so a
    # host-to-host scp over it can fail host-key verification. Publish the
    # returning host's key on the survivor first.
    remote(
        survivor.ssh_host,
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/known_hosts && "
        f"ssh-keyscan -T 5 {returner.direct_ip} 2>/dev/null "
        ">> ~/.ssh/known_hosts && "
        "sort -u -o ~/.ssh/known_hosts ~/.ssh/known_hosts",
    )
    remote(survivor.ssh_host,
           f"scp -q -o BatchMode=yes '{survivor.directory}/seed.iso' "
           f"server@{returner.direct_ip}:'{returner.directory}/seed.iso'")
    script = f"""
qemu-img create -f qcow2 '{returner.directory}/base.qcow2' {IMAGE_SIZE}
chown server:server '{returner.directory}/base.qcow2' '{returner.directory}/seed.iso'
"""
    remote(returner.ssh_host, script, sudo=True)
    reset_secondary_disks(returner)


def resume_replication(args):
    """Non-disruptive rejoin: keep the guest running, re-COLO the pair.

    The surviving host keeps serving the guest; the returning host is started
    as a fresh secondary, the survivor's disk is resynced into it with
    drive-mirror, and the pair is re-formed with a COLO migration. The guest is
    frozen only for the disk switch-over, so connected players are not dropped.
    """
    survivor, returner = resolve_survivor(args)
    print(f"Resuming COLO: survivor {survivor.lan_ip} -> returning {returner.lan_ip}")

    # Any stale QEMU on the returning host must go before its disks are
    # recreated, or the image files stay locked.
    print(f"Stopping any stale QEMU on the returning host {returner.lan_ip}")
    stop_all_colo_qemu(returner)
    adopt_survivor_shape(args, survivor)
    prepare_returning_disks(survivor, returner)
    start_returning_secondary(args, returner, survivor)
    resume_replication_on_survivor(args, survivor, returner)
    print("Resume complete")


def status(args):
    for name, ip, port in (
        ("primary", PRIMARY_IP, PRIMARY_QMP),
        ("secondary", SECONDARY_IP, SECONDARY_QMP),
    ):
        try:
            qmp = QMPClient(ip, port)
            try:
                result = qmp.execute("query-migrate")
                print(f"{name}: {json.dumps(result, sort_keys=True)}")
            finally:
                qmp.close()
        except (OSError, CommandError) as exc:
            print(f"{name}: unavailable ({exc})")
    for host, label in ((PRIMARY_HOST, "primary"), (SECONDARY_HOST, "secondary")):
        output, _ = remote(host, "ss -tnp | grep -E ':(9003|9004)\\b' || true")
        print(f"{label} proxy connections:")
        print(output or "  none")
    print(f"guest ping: {'ok' if vm_reachable() else 'not reachable'}")


def vm_reachable():
    try:
        result = subprocess.run(
            ["ping", "-c", "2", "-W", "1", VM_IP],
            text=True,
            capture_output=True,
        )
        return result.returncode == 0
    except CommandError:
        return False


def failover(args):
    print("Promoting the secondary after the primary heartbeat was lost")
    qmp = QMPClient(SECONDARY_IP, SECONDARY_QMP)
    try:
        qmp.execute("nbd-server-stop")
        qmp.execute("x-colo-lost-heartbeat")
        deadline = time.time() + args.timeout
        while True:
            result = qmp.execute("query-migrate")
            status = result.get("status")
            print(f"secondary migration status: {status}")
            if status == "completed":
                break
            if status in ("failed", "cancelled"):
                raise CommandError(f"secondary promotion failed: {result}")
            if time.time() >= deadline:
                raise CommandError(f"secondary promotion timed out: {result}")
            time.sleep(0.2)
    finally:
        qmp.close()
    if remote_port_open(PRIMARY_IP, PRIMARY_QMP):
        print("Stopping the old primary QEMU")
        stop_qemu(PRIMARY_HOST, "/tmp/colo-primary.pid", PRIMARY_QMP)
    print(f"Failover complete; guest is now served by {SECONDARY_IP}")


def cleanup(args):
    stop_qemu(PRIMARY_HOST, "/tmp/colo-primary.pid", PRIMARY_QMP)
    stop_qemu(SECONDARY_HOST, "/tmp/colo-secondary.pid", SECONDARY_QMP)
    if args.reset_disks:
        reset_secondary_disks()
    print("Cleanup complete")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory", type=int, default=1024, help="guest RAM in MiB")
    parser.add_argument("--smp", type=int, default=1, help="guest vCPUs")
    parser.add_argument("--debug", action="store_true",
                        help="run QEMU in the foreground and show its stderr")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser("setup", help="prepare both hosts")
    setup_parser.add_argument("--no-seed", action="store_true", help="do not rebuild seed.iso")
    setup_parser.add_argument("--no-image-sync", action="store_true",
                              help="do not copy base.qcow2 to .105")
    setup_parser.set_defaults(func=setup)

    start_secondary_parser = subparsers.add_parser(
        "start-secondary", help="start the secondary VM and NBD server")
    start_secondary_parser.set_defaults(func=start_secondary)

    start_primary_parser = subparsers.add_parser(
        "start-primary", help="start the paused primary VM")
    start_primary_parser.set_defaults(func=start_primary)

    start_parser = subparsers.add_parser("start", help="start both sides in COLO order")
    start_parser.set_defaults(func=lambda args: (start_secondary(args), start_primary(args), migrate(args)))

    migrate_parser = subparsers.add_parser("migrate", help="migrate primary into secondary")
    migrate_parser.add_argument("--timeout", type=int, default=180)
    migrate_parser.add_argument("--checkpoint-delay", type=int, default=None)
    migrate_parser.set_defaults(func=migrate)

    status_parser = subparsers.add_parser("status", help="show COLO status")
    status_parser.set_defaults(func=status)

    failover_parser = subparsers.add_parser("failover", help="promote the secondary")
    failover_parser.add_argument("--timeout", type=int, default=30)
    failover_parser.set_defaults(func=failover)

    resume_parser = subparsers.add_parser(
        "resume-replication",
        help="non-disruptive rejoin: re-COLO the pair without restarting the guest")
    resume_parser.add_argument("--timeout", type=int, default=300)
    resume_parser.add_argument(
        "--survivor", default="auto",
        help="which host kept serving the guest: auto, primary, secondary, "
             "or an IP address (default: auto)")
    resume_parser.set_defaults(func=resume_replication)

    cleanup_parser = subparsers.add_parser("cleanup", help="stop both QEMUs")
    cleanup_parser.add_argument("--reset-disks", action="store_true",
                                help="recreate secondary replication disks")
    cleanup_parser.set_defaults(func=cleanup)

    args = parser.parse_args(argv)
    if args.command == "start":
        args.timeout = 180
        args.checkpoint_delay = None
    return args


def main():
    args = parse_args()
    try:
        args.func(args)
    except (CommandError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
