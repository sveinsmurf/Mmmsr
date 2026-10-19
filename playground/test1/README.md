# First test on .104

1. Installed libisoburn
2. Working in ~/colo
3. Get disk image ´´´curl -LO https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2
mv Arch-Linux-x86_64-cloudimg.qcow2 primary.qcow2
qemu-img resize primary.qcow2 10G´´´
4. Cloud-init seed ´´´cat > user-data <<EOF
#cloud-config
users:
  - name: svein
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $(head -1 ~/.ssh/authorized_keys)
EOF

cat > network-config <<'EOF'
version: 2
ethernets:
  nic:
    match:
      name: "e*"
    addresses: [192.168.35.110/24]
    routes:
      - to: default
        via: 192.168.35.1
    nameservers:
      addresses: [192.168.35.1]
EOF

echo "instance-id: colo-test" > meta-data
xorriso -as mkisofs -o seed.iso -V cidata -J -r user-data meta-data network-config´´´
5. Start the VM ´´´qemu-system-x86_64 -enable-kvm -cpu qemu64,kvmclock=on -m 1024 -smp 1 \
  -netdev tap,id=hn0,vhost=off,helper=/usr/lib/qemu/qemu-bridge-helper \
  -device rtl8139,id=e0,netdev=hn0 \
  -drive file=primary.qcow2,if=ide \
  -cdrom seed.iso \
  -display none -serial file:vm.log -daemonize -pidfile vm.pid´´´
