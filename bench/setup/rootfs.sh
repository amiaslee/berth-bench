#!/usr/bin/env bash
# 做"干净系统"：所有环境共用的只读根（目录给容器用，ext4 镜像给虚拟机用）；
# 再做"这个人的盘"的模板：编译测试的源码和 npm 缓存都放进去，每个环境从它克隆一份。
set -euo pipefail
. "$(dirname "$0")/../env.sh"
need_root
R="$BENCH_ROOT/rootfs"; D="$BENCH_ROOT/dl"
STORAGE=$(python3 -I -c "import json;print(json.load(open('$BENCH_ROOT/state.json'))['storage'])")

if [ ! -x "$R/usr/bin/socat" ]; then
  log "debootstrap $UBUNTU_SUITE"
  rm -rf "$R"
  debootstrap --variant=minbase --components=main,universe "$UBUNTU_SUITE" "$R" http://archive.ubuntu.com/ubuntu >/dev/null
  cp /etc/resolv.conf "$R/etc/resolv.conf"
  printf '#!/bin/sh\nexit 101\n' > "$R/usr/sbin/policy-rc.d"; chmod 755 "$R/usr/sbin/policy-rc.d"
  mount -t proc proc "$R/proc"
  trap 'umount "$R/proc" 2>/dev/null || true' EXIT
  chroot "$R" apt-get update -qq
  chroot "$R" env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    socat python3 build-essential pkg-config git ca-certificates curl xz-utils zstd procps iproute2 \
    util-linux e2fsprogs coreutils bubblewrap >/dev/null
  chroot "$R" apt-get clean
  umount "$R/proc"; trap - EXIT
  rm -rf "$R/var/lib/apt/lists"/* "$R/var/cache/apt"/*
fi

log "Node、Zig、berth 客机程序放进根"
mkdir -p "$R/usr/local" "$R/opt" "$R/usr/local/lib/berth" "$R/work" "$R/data"  # /data：虚拟机里的 gVisor 沙箱把作品盘挂到这里，只读根上要先有挂载点
tar -xJf "$D/node.tar.xz" -C "$R/usr/local" --strip-components=1 --exclude='*/README.md' --exclude='*/CHANGELOG.md' --exclude='*/LICENSE'
rm -rf "$R/opt/zig"; mkdir -p "$R/opt/zig"; tar -xJf "$D/zig.tar.xz" -C "$R/opt/zig" --strip-components=1
ln -sf /opt/zig/zig "$R/usr/local/bin/zig"
install -m 755 "$BENCH_DIR/guest/berth-init" "$R/sbin/berth-init"
install -m 644 "$BENCH_DIR"/guest/{server.js,alloc.py,burn.py,send.py,rtt.py} "$R/usr/local/lib/berth/"
[ -f "$BENCH_DIR/guest/alloc_until.py" ] && install -m 644 "$BENCH_DIR/guest/alloc_until.py" "$R/usr/local/lib/berth/"
install -m 755 "$BENCH_DIR/guest/gv.sh" "$R/usr/local/lib/berth/gv.sh"
# Pages 那条路要在虚拟机里起 gVisor：把同一个 runsc 放进客机
rm -rf "$R/usr/local/bin/gvisor-bin"; cp -a "$BENCH_ROOT/bin/runsc" "$BENCH_ROOT/bin/gvisor-bin" "$R/usr/local/bin/"
# NetPlay 的规则容器跑 Godot 无界面版
rm -rf "$D/godot" && mkdir -p "$D/godot" && unzip -q "$D/godot.zip" -d "$D/godot"
install -m 755 "$(find "$D/godot" -type f -name 'Godot_*' | head -1)" "$R/usr/local/bin/godot"
chroot "$R" /usr/local/bin/node --version
chroot "$R" /usr/local/bin/zig version
chroot "$R" /usr/bin/env HOME=/tmp /usr/local/bin/godot --headless --version || log "godot --version 没跑通（看上面的输出）"

log "共用的只读根盘镜像（ext4）"
mb=$(du -sm "$R" | cut -f1); size=$(( mb * 13 / 10 + 256 ))
rm -f "$BENCH_ROOT/images/base.ext4"
mkfs.ext4 -q -F -L berthroot -E lazy_itable_init=0,lazy_journal_init=0 -d "$R" "$BENCH_ROOT/images/base.ext4" "${size}M"
log "根盘 ${size}MB（内容 ${mb}MB）"

log "用户盘模板：源码、npm 缓存"
W="$BENCH_ROOT/work-tmpl"; rm -rf "$W"; mkdir -p "$W"
cp -a "$BENCH_DIR/workloads/webapp" "$W/webapp"; rm -rf "$W/webapp/node_modules" "$W/webapp/dist"
mkdir -p "$W/redis" && tar -xzf "$D/redis.tar.gz" -C "$W/redis" --strip-components=1
mkdir -p "$W/sqlite" && unzip -q -j "$D/sqlite.zip" -d "$W/sqlite"
mkdir -p "$W/.npm" "$W/.zig-cache"
cp -a "$BENCH_DIR/workloads/godot" "$W/godot"
# 在干净系统里联网装一遍依赖，只为填满 npm 缓存；装完删掉 node_modules，测试时离线从缓存装
mount -t proc proc "$R/proc"; mount --bind "$W" "$R/work"
trap 'umount "$R/work" "$R/proc" 2>/dev/null || true' EXIT
chroot "$R" /bin/sh -c 'cd /work/webapp && HOME=/tmp npm ci --cache /work/.npm --no-audit --no-fund --loglevel=error && npm run build >/dev/null && rm -rf node_modules dist'
umount "$R/work" "$R/proc"; trap - EXIT
echo "berth-user-disk" > "$W/.berth-marker"
# 模板内容的指纹：恢复快照换上盘以后在客机里重算一遍，核"客机没读到占位盘的旧缓存"
(cd "$W" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1) > "$BENCH_ROOT/images/work.digest"
du -sh "$W" "$W/.npm"

log "用户盘模板写成 10G 的盘（$STORAGE）"
if [ "$STORAGE" = zfs ]; then
  P=$(python3 -I -c "import json;print(json.load(open('$BENCH_ROOT/state.json'))['pool'])")
  zfs destroy -r "$P/tmpl/work" 2>/dev/null || true
  zfs create -V 10G -o volmode=dev "$P/tmpl/work"
  for i in $(seq 1 500); do [ -b "/dev/zvol/$P/tmpl/work" ] && break; sleep 0.02; done
  mkfs.ext4 -q -F -L berthwork -E lazy_itable_init=0,lazy_journal_init=0 -d "$W" "/dev/zvol/$P/tmpl/work"
  zfs snapshot "$P/tmpl/work@v1"
  zfs get -Hp used,referenced,logicalreferenced,compressratio "$P/tmpl/work"
  # H8b：同样内容的加密模板盘（ZFS 原生加密，测试用的随机密钥，只在这台测试机上）
  if [ ! -f "$BENCH_ROOT/enc.key" ]; then head -c 32 /dev/urandom > "$BENCH_ROOT/enc.key"; chmod 600 "$BENCH_ROOT/enc.key"; fi
  zfs destroy -r "$P/enc" 2>/dev/null || true
  zfs create -o encryption=aes-256-gcm -o keyformat=raw -o "keylocation=file://$BENCH_ROOT/enc.key" "$P/enc"
  zfs create "$P/enc/users"
  zfs create -V 10G -o volmode=dev "$P/enc/tmpl"
  for i in $(seq 1 500); do [ -b "/dev/zvol/$P/enc/tmpl" ] && break; sleep 0.02; done
  e2image -ra -p "/dev/zvol/$P/tmpl/work" "/dev/zvol/$P/enc/tmpl"
  zfs snapshot "$P/enc/tmpl@v1"
else
  rm -f "$BENCH_ROOT/images/work.img"
  truncate -s 10G "$BENCH_ROOT/images/work.img"
  mkfs.ext4 -q -F -L berthwork -E lazy_itable_init=0,lazy_journal_init=0 -d "$W" "$BENCH_ROOT/images/work.img"
fi
# 虚拟机快照里"这个人的盘"那个槽位的占位盘：跟用户盘一样大，空的稀疏文件
truncate -s 10G "$BENCH_ROOT/images/placeholder.img"
sha256sum "$BENCH_ROOT/images/base.ext4" > "$BENCH_ROOT/images/SHA256SUMS"
log "根和用户盘模板做好了"
