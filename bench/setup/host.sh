#!/usr/bin/env bash
# 装宿主：系统包、KVM、存储池、cgroup、挑钉核用的 CPU，写 state.json。要 root；重复跑不出错。
set -euo pipefail
. "$(dirname "$0")/../env.sh"
need_root
mkdir -p "$BENCH_ROOT"/{bin,dl,images,guest,snap,users} "$BENCH_SRV"/{vms,mnt,results,bundles} /run/vm
: > "$BENCH_ROOT/dl/SOURCES.txt.tmp" 2>/dev/null || true

log "装系统包"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  socat jq python3 build-essential bc flex bison libelf-dev libssl-dev dwarves cpio \
  debootstrap e2fsprogs xz-utils zstd unzip curl ca-certificates git \
  iproute2 nftables containerd runc numactl sysstat util-linux kmod pciutils acl >/dev/null
# ZFS：先装用户态；内核模块 Ubuntu 的内核自带，云上某些内核要另装 modules-extra
apt-get install -y -qq --no-install-recommends zfsutils-linux >/dev/null || true
modprobe zfs 2>/dev/null || { apt-get install -y -qq "linux-modules-extra-$(uname -r)" >/dev/null 2>&1 || true; modprobe zfs 2>/dev/null || true; }

log "KVM"
modprobe kvm_intel 2>/dev/null || modprobe kvm_amd 2>/dev/null || true
[ -e /dev/kvm ] || die "没有 /dev/kvm：这台机器不能跑虚拟机"
chmod 666 /dev/kvm
[ -e /dev/userfaultfd ] && chmod 600 /dev/userfaultfd || true
systemctl enable --now containerd >/dev/null 2>&1 || systemctl start containerd
ulimit -n 1048576 || true
sysctl -qw fs.inotify.max_user_instances=8192 fs.file-max=2097152 kernel.pid_max=4194304 || true

log "内存：关交换，护住 sshd、testbox 的代理不被 OOM 杀"
swapoff -a || true
# 只护登录和 testbox 的代理；containerd 不护（它起的 Kata 虚拟机会继承它的分数）
for p in $(pgrep -x sshd) $(pgrep -f tb-agent); do echo -1000 > /proc/$p/oom_score_adj 2>/dev/null || true; done

log "cgroup：开控制器，建 bench 这一层"
for c in cpuset cpu io memory pids; do echo "+$c" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true; done
mkdir -p /sys/fs/cgroup/bench
for c in cpuset cpu io memory pids; do echo "+$c" > /sys/fs/cgroup/bench/cgroup.subtree_control 2>/dev/null || true; done

log "存储：优先拿一块没用的整盘建 ZFS 池"
storage=file; pool_dev=""
if grep -q '^zfs ' /proc/modules && command -v zpool >/dev/null; then
  if zpool list -H "$BENCH_POOL" >/dev/null 2>&1; then
    storage=zfs; pool_dev=$(zpool list -vHP "$BENCH_POOL" | awk 'NR==2{print $1}')
  else
    # 没分区、没挂载、不是交换、没被别的设备占着、至少 20G 的整盘
    for d in $(lsblk -dnpo NAME,TYPE | awk '$2=="disk"{print $1}'); do
      [ "$(lsblk -no NAME "$d" | wc -l)" = 1 ] || continue
      [ -z "$(lsblk -no MOUNTPOINTS "$d" | tr -d '[:space:]')" ] || continue
      grep -q "^$d " /proc/swaps && continue
      [ -z "$(ls /sys/block/"$(basename "$d")"/holders 2>/dev/null)" ] || continue
      sz=$(lsblk -bdno SIZE "$d"); [ "$sz" -ge $((20*1024*1024*1024)) ] || continue
      blkid -p "$d" >/dev/null 2>&1 && continue   # 上面有文件系统或 RAID 标记，不碰
      pool_dev=$d; break
    done
    if [ -n "$pool_dev" ]; then
      # 盘够大时先切出 24G 当"原生块设备"，给 H8（ZFS 卷对比原生块设备的构建速度）做对照；其余建池
      if [ "$(lsblk -bdno SIZE "$pool_dev")" -ge $((100*1024*1024*1024)) ]; then
        printf ',24G\n,\n' | sfdisk -q "$pool_dev"
        partprobe "$pool_dev" 2>/dev/null || true; udevadm settle || true
        raw_part=$(lsblk -lnpo NAME,TYPE "$pool_dev" | awk '$2=="part"{print $1}' | sed -n 1p)
        zfs_part=$(lsblk -lnpo NAME,TYPE "$pool_dev" | awk '$2=="part"{print $1}' | sed -n 2p)
        echo "$raw_part" > "$BENCH_ROOT/raw_dev"
        pool_dev=$zfs_part
      fi
      zpool create -f -o ashift=12 -O compression=lz4 -O atime=off -O xattr=sa "$BENCH_POOL" "$pool_dev"
      storage=zfs
    else
      log "没有空闲整盘：用根盘上的文件建池（盘的数字要打折看）"
      truncate -s 80G /var/lib/bench-pool.img
      zpool create -f -o ashift=12 -O compression=lz4 -O atime=off -O xattr=sa "$BENCH_POOL" /var/lib/bench-pool.img
      storage=zfs; pool_dev=/var/lib/bench-pool.img
    fi
  fi
  # ZFS 的 ARC 默认能占到一半内存，密度实验会把它当可用内存；限到 2 GiB（冷读 2026-11-20 指出）
  echo $((2*1024*1024*1024)) > /sys/module/zfs/parameters/zfs_arc_max 2>/dev/null || true
  for ds in tmpl users recv comp; do zfs list -H "$BENCH_POOL/$ds" >/dev/null 2>&1 || zfs create "$BENCH_POOL/$ds"; done
else
  log "没有 ZFS 模块：用户盘改用稀疏文件（只适合干跑）"
fi

log "挑钉核的 CPU：避开 0 号核，挑两个不同的物理核的第一个线程"
raw_dev=$(cat "$BENCH_ROOT/raw_dev" 2>/dev/null || true)
python3 -I - "$storage" "$pool_dev" "$raw_dev" > "$BENCH_ROOT/state.json" <<'EOF'
import glob, json, os, sys
cores = {}
for p in sorted(glob.glob('/sys/devices/system/cpu/cpu[0-9]*/topology/thread_siblings_list')):
    cpu = int(p.split('/')[5][3:])
    sib = open(p).read().strip()
    cores.setdefault(sib, []).append(cpu)
groups = sorted((sorted(v) for v in cores.values()), key=lambda v: v[0])
usable = [g for g in groups if 0 not in g] or groups
first = [g[0] for g in usable]
pin2 = first[-2:] if len(first) >= 2 else first
pin1 = pin2[-1:]
smt = open('/sys/devices/system/cpu/smt/control').read().strip() if os.path.exists('/sys/devices/system/cpu/smt/control') else 'unknown'
print(json.dumps({
    'storage': sys.argv[1], 'pool': os.environ.get('BENCH_POOL', 'bench'), 'pool_dev': sys.argv[2], 'raw_dev': sys.argv[3],
    'pin2': ','.join(map(str, pin2)), 'pin1': ','.join(map(str, pin1)),
    'core_groups': groups, 'smt': smt, 'ncpu': os.cpu_count(),
}, indent=1))
EOF
cat "$BENCH_ROOT/state.json"
log "宿主装好了"
