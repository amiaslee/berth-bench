# berth 实验的公共设置：版本钉死、路径、快慢模式。各个 setup 脚本 source 它。
# 版本 2026-11-20 定：各家当天最新的正式版；内核取最新的长期支持版。

# 装好的东西放这里（程序、镜像、快照）；结果放 /srv/bench/results
export BENCH_ROOT="${BENCH_ROOT:-/opt/bench}"
export BENCH_SRV="${BENCH_SRV:-/srv/bench}"
export BENCH_RESULTS="${BENCH_RESULTS:-$BENCH_SRV/results}"
export BENCH_POOL="${BENCH_POOL:-bench}"
# BENCH_QUICK=1：干跑，每组只跑一两次，只看脚本对不对
export BENCH_QUICK="${BENCH_QUICK:-0}"

FC_VER=v1.18.0
CH_VER=v55.0
GVISOR_REL=20261116
KATA_VER=4.4.0
NODE_VER=v26.13.1
NODE_SHA256=5c063e46bab0fbc1c17cc445d7ff129cbdcd0471be6ed83b6ab864f8143bc864
ZIG_VER=0.17.1
ZIG_SHA256=3581863e3cbcfd0fce0efc682ce7e9e93da96668a977791d356a26d2a7e9b17a
REDIS_VER=8.10.3
SQLITE_PATH=2026/sqlite-amalgamation-3540100.zip
KERNEL_VER=6.18.64
KERNEL_SHA256=020d45510c539972e6f14c5c71566878d3b7ef56fb082b3e09134f2ebaade562
UBUNTU_SUITE=noble
# NetPlay 验收用的 Godot 版本（netplay 2026-11-20 第二轮答复）
GODOT_VER=4.7.2
GODOT_SHA512=9aa00f7a605200940bce3027a567b782f49bd8e940dd06ae9e987bd65aee1b1467edd56ed84fcdcbdd44354bf613bdbb4e5d2913e925850368e150c59ed54c65

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export BENCH_DIR

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "错误: $*"; exit 1; }
need_root() { [ "$(id -u)" = 0 ] || die "要 root"; }
# 下载并核对：fetch <网址> <存成的文件> [sha256]
fetch() {
  local url=$1 out=$2 sum=${3:-}
  if [ ! -s "$out" ]; then
    curl -fL --retry 5 --retry-delay 3 -sS -o "$out.part" "$url" && mv "$out.part" "$out"
  fi
  if [ -n "$sum" ]; then
    echo "$sum  $out" | sha256sum -c --quiet - || { rm -f "$out"; die "校验不过: $url"; }
  fi
  printf '%s  %s  %s\n' "$(sha256sum "$out" | cut -d' ' -f1)" "$(basename "$out")" "$url" >> "$BENCH_ROOT/dl/SOURCES.txt"
}
