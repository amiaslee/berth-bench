#!/bin/sh
# 客机里起、停 gVisor 沙箱：Pages 那条路——开着的虚拟机里每个作品一个 gVisor 沙箱，挂上它的 /data。
# gv.sh up <名字> <数据目录>   起沙箱、在里面起 Node 服务、等它回话
# gv.sh start <名字> <数据目录> 只起沙箱
# gv.sh x <名字> <命令>         在沙箱里跑一条命令
# gv.sh down <名字>            扔掉
# gv.sh pause|resume <名字>
# gv.sh check <名字>           Node 服务回不回话
set -e
R="runsc --root /run/runsc --network=none --overlay2=none --platform=systrap --ignore-cgroups"
cmd=$1; id=$2
# GV_DEBUG=1：打开 gVisor 的调试日志，起不来时把日志尾巴打出来
[ -n "$GV_DEBUG" ] && R="$R --debug --debug-log=/run/gv/$id/log/"
# 作品的 Node 服务读 /data 上的标记文件回话；回对了才算醒（跟宿主上那几种同一个判法）
APP='setsid nohup node /usr/local/lib/berth/server.js /data/.berth-marker >/dev/null 2>&1 </dev/null &
i=0; while [ "$(curl -sf http://127.0.0.1:8080/ 2>/dev/null)" != berth-user-disk ]; do i=$((i+1)); [ $i -gt 30000 ] && exit 9; sleep 0.002; done'
case "$cmd" in
up|start)
  data=$3; b=/run/gv/$id; mkdir -p "$b"
  caps='"CAP_CHOWN","CAP_DAC_OVERRIDE","CAP_FSETID","CAP_FOWNER","CAP_SETGID","CAP_SETUID","CAP_NET_BIND_SERVICE","CAP_KILL"'
  cat > "$b/config.json" <<JSON
{"ociVersion":"1.0.2",
 "process":{"terminal":false,"user":{"uid":0,"gid":0},"args":["/bin/sleep","infinity"],
  "env":["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin","HOME=/root","LANG=C.UTF-8"],"cwd":"/",
  "capabilities":{"bounding":[$caps],"effective":[$caps],"permitted":[$caps]},"noNewPrivileges":true},
 "root":{"path":"/","readonly":true},"hostname":"$id",
 "mounts":[{"destination":"/proc","type":"proc","source":"proc"},
  {"destination":"/dev","type":"tmpfs","source":"tmpfs","options":["nosuid","mode=755"]},
  {"destination":"/tmp","type":"tmpfs","source":"tmpfs","options":["nosuid","nodev","mode=1777"]},
  {"destination":"/root","type":"tmpfs","source":"tmpfs","options":["nosuid","nodev","mode=700"]},
  {"destination":"/run","type":"tmpfs","source":"tmpfs","options":["nosuid","nodev","mode=755"]},
  {"destination":"/data","type":"bind","source":"$data","options":["rbind","rw"]}],
 "linux":{"namespaces":[{"type":"pid"},{"type":"ipc"},{"type":"uts"},{"type":"mount"},{"type":"network"}]}}
JSON
  $R run --detach --bundle "$b" "$id" </dev/null >/dev/null 2>"$b/err" || {
    tail -5 "$b/err"
    for f in "$b"/log/*; do [ -f "$f" ] && { echo "== $f"; tail -25 "$f"; }; done
    dmesg 2>/dev/null | tail -10
    exit 3; }
  if [ "$cmd" = up ]; then $R exec "$id" /bin/sh -c "$APP"; fi
  ;;
x) shift 2; $R exec "$id" /bin/sh -c "$*" ;;
down) $R kill "$id" KILL >/dev/null 2>&1 || true; $R delete -force "$id" >/dev/null 2>&1 || true; rm -rf "/run/gv/$id" ;;
pause) $R pause "$id" ;;
resume) $R resume "$id" ;;
check) $R exec "$id" curl -sf http://127.0.0.1:8080/ >/dev/null ;;
*) echo "gv.sh up|down|pause|resume|check" >&2; exit 2 ;;
esac
