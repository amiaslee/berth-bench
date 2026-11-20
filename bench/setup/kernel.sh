#!/usr/bin/env bash
# 编一个 Firecracker 和 Cloud Hypervisor 都能起的客机内核（同一个内核，两家才好比）。
# 已有 $BENCH_ROOT/guest/vmlinux（比如干跑时编好送来的）就跳过。
set -euo pipefail
. "$(dirname "$0")/../env.sh"
need_root
OUT="$BENCH_ROOT/guest"
if [ -s "$OUT/vmlinux" ]; then log "已有客机内核，跳过编译"; exit 0; fi
W="$BENCH_ROOT/build"; mkdir -p "$W" "$OUT"
cd "$W"
[ -d "linux-$KERNEL_VER" ] || tar -xf "$BENCH_ROOT/dl/linux.tar.xz"
cd "linux-$KERNEL_VER"
make -s x86_64_defconfig
make -s kvm_guest.config
c=./scripts/config
# Firecracker：virtio-mmio，命令行给设备；Cloud Hypervisor：PVH 直启、virtio-pci、ACPI 热插拔
$c -e VIRTIO -e VIRTIO_MMIO -e VIRTIO_MMIO_CMDLINE_DEVICES -e VIRTIO_PCI -e VIRTIO_BLK -e VIRTIO_NET \
   -e VIRTIO_CONSOLE -e VIRTIO_BALLOON -e PAGE_REPORTING -e VIRTIO_MEM -e MEMORY_HOTPLUG -e MEMORY_HOTREMOVE \
   -e VSOCKETS -e VIRTIO_VSOCKETS -e HW_RANDOM_VIRTIO -e PVH -e KVM_GUEST -e PARAVIRT \
   -e ACPI -e PCI -e HOTPLUG_PCI -e HOTPLUG_PCI_ACPI -e PCIEPORTBUS -e HOTPLUG_PCI_PCIE \
   -e EXT4_FS -e TMPFS -e DEVTMPFS -e DEVTMPFS_MOUNT -e OVERLAY_FS -e CGROUPS -e MEMCG -e BLK_CGROUP \
   -e SERIAL_8250 -e SERIAL_8250_CONSOLE -e UNIX -e INET -e IPV6 -e TUN -e VETH -e BRIDGE \
   -e FUSE_FS -e SECCOMP -e USER_NS -e NAMESPACES
# 跟测试无关的大块关掉，编得快些
$c -d DRM -d SOUND -d WLAN -d WIRELESS -d CFG80211 -d USB_SUPPORT -d MEDIA_SUPPORT -d INPUT_TOUCHSCREEN \
   -d NETWORK_FILESYSTEMS -d MODULES -d DEBUG_INFO -d DEBUG_INFO_BTF -d WERROR
make -s olddefconfig
for o in VIRTIO_MMIO_CMDLINE_DEVICES PVH VIRTIO_VSOCKETS VIRTIO_BALLOON PAGE_REPORTING HOTPLUG_PCI_ACPI EXT4_FS; do
  grep -q "^CONFIG_$o=y" .config || die "内核配置里 $o 没开"
done
log "编内核（$(nproc) 线程）"
t0=$(date +%s)
make -s -j"$(nproc)" vmlinux
log "内核编完，用了 $(( $(date +%s) - t0 )) 秒"
install -m 644 vmlinux "$OUT/vmlinux"
install -m 644 .config "$OUT/config-$KERNEL_VER"
sha256sum "$OUT/vmlinux"
