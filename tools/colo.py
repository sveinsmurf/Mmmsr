#!/usr/bin/env python3
"""Set up and control a QEMU COLO cluster using .104 as primary and .105 as secondary.

Run from a workstation that has passwordless SSH access to both hosts:

    tools/colo.py setup
    tools/colo.py start
    tools/colo.py status
    tools/colo.py failover
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


class CommandError(RuntimeError):
    pass


class QMPClient:
    def __init__(self, host, port, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.stream = self.sock.makefile("rb")
        greeting = json.loads(self.stream.readline())
        if "QMP" not in greeting:
            raise CommandError(f"unexpected QMP greeting: {greeting}")
        self.execute("qmp_capabilities")

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


def wait_for_port(host, port, description, timeout=30):
    print(f"Waiting for {description} on {host}:{port}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if remote_port_open(host, port):
            return
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


def stop_qemu(host, pidfile, name):
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
pkill -f '^qemu-system-x86_64 .* -name {name} ' 2>/dev/null || true
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
runcmd:
  - chown -R svein:svein /home/svein
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


def sync_image(args):
    print("Copying the guest image to the secondary over the direct link")
    remote(PRIMARY_HOST, f"test -f '{PRIMARY_DIR}/base.qcow2'")
    remote(SECONDARY_HOST, f"mkdir -p '{SECONDARY_DIR}'", sudo=True)
    remote(SECONDARY_HOST,
           f"chown server:server '{SECONDARY_DIR}'", sudo=True)
    remote(
        PRIMARY_HOST,
        f"scp -q '{PRIMARY_DIR}/base.qcow2' "
        f"server@{SECONDARY_DIRECT_IP}:'{SECONDARY_DIR}/base.qcow2'",
        timeout=900,
    )
    remote(
        PRIMARY_HOST,
        f"scp -q '{PRIMARY_DIR}/seed.iso' "
        f"server@{SECONDARY_DIRECT_IP}:'{SECONDARY_DIR}/seed.iso'",
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


def reset_secondary_disks():
    print("Resetting secondary COLO replication disks")
    script = f"""
qemu-img create -f qcow2 '{SECONDARY_DIR}/secondary-active.qcow2' {IMAGE_SIZE}
qemu-img create -f qcow2 '{SECONDARY_DIR}/secondary-hidden.qcow2' {IMAGE_SIZE}
chown server:server '{SECONDARY_DIR}/secondary-active.qcow2' '{SECONDARY_DIR}/secondary-hidden.qcow2'
"""
    remote(SECONDARY_HOST, script, sudo=True)


def qemu_launch_options(args):
    if args.debug:
        return "-display none -daemonize -D /tmp/qemu-debug.log"
    return "-display none -daemonize"


def start_secondary_qemu(args):
    qemu = f"""qemu-system-x86_64 \\
  -enable-kvm -cpu qemu64,kvmclock=on -m {args.memory} -smp {args.smp} \\
  -name colo-secondary \\
  -qmp tcp:0.0.0.0:{SECONDARY_QMP},server=on,wait=off \\
  -chardev socket,id=red0,host={PRIMARY_DIRECT_IP},port={PROXY_TX_PORT},reconnect-ms=1000 \\
  -chardev socket,id=red1,host={PRIMARY_DIRECT_IP},port={PROXY_COMPARE_PORT},reconnect-ms=1000 \\
  -object filter-redirector,id=f1,netdev=hn0,queue=tx,indev=red0,vnet_hdr_support=on \\
  -object filter-redirector,id=f2,netdev=hn0,queue=rx,outdev=red1,vnet_hdr_support=on \\
  -object filter-rewriter,id=rew0,netdev=hn0,queue=all,vnet_hdr_support=on \\
  -netdev tap,id=hn0,ifname=tap0,script=no,downscript=no,vhost=off \\
  -device rtl8139,id=e0,netdev=hn0 \\
  -drive if=none,id=parent0,file.filename={SECONDARY_DIR}/base.qcow2,driver=qcow2 \\
  -drive if=none,id=childs0,driver=replication,mode=secondary,file.driver=qcow2,top-id=colo-disk0,file.file.filename={SECONDARY_DIR}/secondary-active.qcow2,file.backing.driver=qcow2,file.backing.file.filename={SECONDARY_DIR}/secondary-hidden.qcow2,file.backing.backing=parent0 \\
  -drive if=ide,id=colo-disk0,driver=quorum,read-pattern=fifo,vote-threshold=1,children.0=childs0 \\
  -drive file={SECONDARY_DIR}/seed.iso,if=virtio,media=cdrom \\
  -incoming tcp:0.0.0.0:{INCOMING_PORT} \\
  -serial file:/tmp/colo-secondary.log {qemu_launch_options(args)}
"""
    remote(SECONDARY_HOST, qemu)


def start_primary_qemu(args):
    qemu = f"""qemu-system-x86_64 \\
  -enable-kvm -cpu qemu64,kvmclock=on -m {args.memory} -smp {args.smp} \\
  -name colo-primary \\
  -qmp tcp:0.0.0.0:{PRIMARY_QMP},server=on,wait=off \\
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
  -netdev tap,id=hn0,ifname=tap2,script=no,downscript=no,vhost=off \\
  -device rtl8139,id=e0,netdev=hn0 \\
  -drive if=ide,id=colo-disk0,driver=quorum,read-pattern=fifo,vote-threshold=1,children.0.file.filename={PRIMARY_DIR}/base.qcow2,children.0.driver=qcow2 \\
  -drive file={PRIMARY_DIR}/seed.iso,if=virtio,media=cdrom \\
  -serial file:/tmp/colo-primary.log -S {qemu_launch_options(args)}
"""
    remote(PRIMARY_HOST, qemu)


def setup(args):
    print("Verifying direct and LAN SSH connectivity")
    remote(PRIMARY_HOST, ":")
    remote(SECONDARY_HOST, ":")
    remote(PRIMARY_HOST, f"ping -c 1 {SECONDARY_DIRECT_IP}")
    stop_qemu(PRIMARY_HOST, "/tmp/colo-primary.pid", "colo-primary")
    stop_qemu(SECONDARY_HOST, "/tmp/colo-secondary.pid", "colo-secondary")
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
    stop_qemu(SECONDARY_HOST, "/tmp/colo-secondary.pid", "colo-secondary")
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
    stop_qemu(PRIMARY_HOST, "/tmp/colo-primary.pid", "colo-primary")
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
        stop_qemu(PRIMARY_HOST, "/tmp/colo-primary.pid", "colo-primary")
    print(f"Failover complete; guest is now served by {SECONDARY_IP}")


def cleanup(args):
    stop_qemu(PRIMARY_HOST, "/tmp/colo-primary.pid", "colo-primary")
    stop_qemu(SECONDARY_HOST, "/tmp/colo-secondary.pid", "colo-secondary")
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
