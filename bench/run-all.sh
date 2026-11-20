#!/usr/bin/env bash
# 一口气跑：装机 -> 记环境 -> 做共享快照 -> 各实验按要紧的先后跑。哪一步出错记下来，接着跑下一步。
# 用法：run-all.sh            全部
#       run-all.sh setup      只装机（宿主、下载、内核、根盘、快照）
#       run-all.sh exp boot disk ...   只跑这几个实验
set -uo pipefail
cd "$(dirname "$0")"
. ./env.sh
need_root
mkdir -p "$BENCH_RESULTS"
command -v tb >/dev/null && tb busy 8h 'berth 真机实验在跑' >/dev/null 2>&1 || true
EXPS_DEFAULT="boot disk netplay h28 wall cmdnet agent clock gvlimits density build reclaim usage smt"
status=0
step() {
  local name=$1; shift
  log ">>> $name"
  local t0=$(date +%s)
  if "$@" >> "$BENCH_RESULTS/run-$name.log" 2>&1; then
    log "<<< $name 成了（$(( $(date +%s) - t0 )) 秒）"; echo "$name ok $(( $(date +%s) - t0 ))" >> "$BENCH_RESULTS/steps.txt"
  else
    log "<<< $name 失败（$(( $(date +%s) - t0 )) 秒），看 run-$name.log"; echo "$name FAIL $(( $(date +%s) - t0 ))" >> "$BENCH_RESULTS/steps.txt"; status=1
  fi
}
setup() {
  step host bash setup/host.sh
  grep -q "^host ok" "$BENCH_RESULTS/steps.txt" || { log "宿主没装好，停"; return 1; }
  step fetch bash setup/fetch.sh
  step kernel bash setup/kernel.sh
  step rootfs bash setup/rootfs.sh
  step env python3 bench.py env
  step snapshots python3 bench.py snapshots
}
case "${1:-all}" in
  setup) setup ;;
  exp) shift; for e in "${@:-$EXPS_DEFAULT}"; do step "$e" python3 bench.py "$e"; done ;;
  all)
    setup
    grep -q "^snapshots ok" "$BENCH_RESULTS/steps.txt" || die "装机或共用快照没成，不跑实验（看 steps.txt）"
    for e in $EXPS_DEFAULT; do step "$e" python3 bench.py "$e"; done ;;
  *) die "用法：run-all.sh [setup|exp 实验...|all]" ;;
esac
cat "$BENCH_RESULTS/steps.txt"
exit $status
