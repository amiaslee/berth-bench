#!/usr/bin/env bash
# 下载并核对四种隔离要用的程序，和编译测试要用的源码。官方给了校验和的照校验；没给的记下我们算的。
set -euo pipefail
. "$(dirname "$0")/../env.sh"
need_root
D="$BENCH_ROOT/dl"; B="$BENCH_ROOT/bin"; mkdir -p "$D" "$B"
cd "$D"

# 四种隔离用借机那天官方最新的正式版（berth 主会话 2026-11-20 转 Pages 的提醒）：装前查一次发布页，
# 查不到才退回 env.sh 里钉的版本。BENCH_PIN=1 时一律用钉的版本（复现旧结果用）。
if [ "${BENCH_PIN:-0}" != 1 ]; then
  gh_latest() { curl -fsS --retry 3 "https://api.github.com/repos/$1/releases/latest" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])'; }
  v=$(gh_latest firecracker-microvm/firecracker 2>/dev/null) && FC_VER=$v
  v=$(gh_latest cloud-hypervisor/cloud-hypervisor 2>/dev/null) && CH_VER=$v
  v=$(gh_latest kata-containers/kata-containers 2>/dev/null) && KATA_VER=$v
  # gVisor 的正式版目录是 releases/release/<日期>[.<序号>]，挑最新的、带 x86_64 包的那一个
  v=$(curl -fsS --retry 3 "https://storage.googleapis.com/storage/v1/b/gvisor/o?prefix=releases/release/2&delimiter=/" \
      | python3 -I -c '
import json, re, sys
ps = [p.split("/")[2] for p in json.load(sys.stdin).get("prefixes", [])]
ps = [p for p in ps if re.fullmatch(r"\d{8}(\.\d+)?", p)]
key = lambda p: (p.split(".")[0], int(p.split(".")[1]) if "." in p else -1)
print(sorted(ps, key=key)[-1])' 2>/dev/null) && GVISOR_REL=$v
fi
log "这次装的版本：Firecracker $FC_VER、Cloud Hypervisor $CH_VER、gVisor $GVISOR_REL、Kata $KATA_VER"

log "Firecracker $FC_VER"
fetch "https://github.com/firecracker-microvm/firecracker/releases/download/$FC_VER/firecracker-$FC_VER-x86_64.tgz.sha256.txt" fc.sha256.txt
fetch "https://github.com/firecracker-microvm/firecracker/releases/download/$FC_VER/firecracker-$FC_VER-x86_64.tgz" fc.tgz "$(awk '{print $1}' fc.sha256.txt)"
tar -xzf fc.tgz
install -m 755 "release-$FC_VER-x86_64/firecracker-$FC_VER-x86_64" "$B/firecracker"

log "Cloud Hypervisor $CH_VER（官方没给校验和，记下我们算的）"
fetch "https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/$CH_VER/cloud-hypervisor-static" ch
fetch "https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/$CH_VER/ch-remote-static" ch-remote
install -m 755 ch "$B/cloud-hypervisor"; install -m 755 ch-remote "$B/ch-remote"

log "gVisor $GVISOR_REL"
G="https://storage.googleapis.com/gvisor/releases/release/$GVISOR_REL/x86_64"
fetch "$G/gvisor.tar.zstd.sha512" gvisor.sha512
fetch "$G/gvisor.tar.zstd" gvisor.tar.zstd
echo "$(awk '{print $1}' gvisor.sha512)  gvisor.tar.zstd" | sha512sum -c --quiet - || die "gVisor 校验不过"
mkdir -p gvisor && tar --zstd -xf gvisor.tar.zstd -C gvisor
# 新版 runsc 要它旁边的 gvisor-bin/（sentry、gofer 等），整个目录一起放
gsrc=$(dirname "$(find gvisor -type f -name runsc | head -1)")
rm -rf "$B/gvisor-bin"; cp -a "$gsrc/runsc" "$gsrc/gvisor-bin" "$B/"
[ -x "$B/gvisor-bin/gvisor_sentry" ] || die "gVisor 的 gvisor-bin 没放好"

log "Kata $KATA_VER（约 1GB）"
fetch "https://github.com/kata-containers/kata-containers/releases/download/$KATA_VER/kata-static-$KATA_VER-amd64.tar.zst" kata-static.tar.zst
[ -x /opt/kata/runtime-rs/bin/containerd-shim-kata-v2 ] || tar --zstd -xf kata-static.tar.zst -C /
ln -sf /opt/kata/runtime-rs/bin/containerd-shim-kata-v2 /usr/local/bin/containerd-shim-kata-v2

log "Node $NODE_VER、Zig $ZIG_VER、Redis $REDIS_VER、SQLite"
fetch "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz" node.tar.xz "$NODE_SHA256"
fetch "https://ziglang.org/download/$ZIG_VER/zig-x86_64-linux-$ZIG_VER.tar.xz" zig.tar.xz "$ZIG_SHA256"
fetch "https://github.com/redis/redis/archive/refs/tags/$REDIS_VER.tar.gz" redis.tar.gz
fetch "https://www.sqlite.org/$SQLITE_PATH" sqlite.zip
fetch "https://github.com/godotengine/godot/releases/download/$GODOT_VER-stable/Godot_v$GODOT_VER-stable_linux.x86_64.zip" godot.zip
echo "$GODOT_SHA512  godot.zip" | sha512sum -c --quiet - || die "Godot 校验不过"

if [ ! -s "$BENCH_ROOT/guest/vmlinux" ]; then
  log "客机内核源码 $KERNEL_VER"
  fetch "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$KERNEL_VER.tar.xz" linux.tar.xz "$KERNEL_SHA256"
fi

{ echo "装的版本（借机那天查的官方最新）：firecracker=$FC_VER cloud-hypervisor=$CH_VER gvisor=$GVISOR_REL kata=$KATA_VER guest-kernel=$KERNEL_VER"
  echo "firecracker: $("$B/firecracker" --version 2>&1 | head -1)"
  echo "cloud-hypervisor: $("$B/cloud-hypervisor" --version 2>&1 | head -1)"
  echo "runsc: $("$B/runsc" --version 2>&1 | head -2 | tr '\n' ' ')"
  echo "kata: $(/opt/kata/runtime-rs/bin/containerd-shim-kata-v2 --version 2>&1 | head -1)"
  echo "kata-clh: $(/opt/kata/bin/cloud-hypervisor --version 2>&1 | head -1)"
  echo "containerd: $(containerd --version 2>&1)"
  echo "runc: $(runc --version 2>&1 | head -1)"
} > "$BENCH_ROOT/versions.txt"
cat "$BENCH_ROOT/versions.txt"
log "下载完了"
