#!/usr/bin/env python3
"""berth 第一批真机实验：python3 bench.py <实验> 。实验和判据见 docs/research/experiments/2026-11-metal-round1/plan.md。

env        记下机器、内核、缓解、各程序版本
snapshots  做"干净系统刚开好机"的共享快照（Firecracker、Cloud Hypervisor 各两种形状）
boot       起机和醒来：冷启、共享快照恢复后换上这个人的盘、热插盘、gVisor、Kata、runc；内存里暂停；整机内存快照（旧做法）
disk       盘：挂上、写、卸下、换一台再挂；断电式的卸法；热插热拔；zfs 收发；压缩比
density    一台挤多少：Pages 形状（跑 Node 服务）和 dute 形状（空闲），放到内存剩 3 GiB 为止
build      编译真项目：npm ci + vite build、redis、zig cc sqlite；各隔离方式同样钉两个核
reclaim    内存回收：客机放掉 1 GiB 以后宿主收回多少、多快；气球；页缓存
usage      berth 在宿主上记的算力、内存、盘、流量跟环境里实际用的差多少
smt        超线程开和关，一台机器同时编译的总吞吐
netplay    同机两台环境之间一次来回的延迟；Godot 无界面版冷启动
cleanup    清掉起过的所有东西
每一次测量一行 JSON，失败的也记（ok=false），写在 $BENCH_RESULTS/<实验>.jsonl。
"""
import contextlib
import glob
import json
import os
import resource
import shlex
import shutil
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from benchlib import *  # noqa: E402,F403
import benchlib as B  # noqa: E402

PIN2 = B.STATE.get("pin2")
PIN1 = B.STATE.get("pin1")
GIB = 1 << 30
MIB = 1 << 20


def n(full, quick=1):
    return quick if B.QUICK else full


PARTS = set(filter(None, os.environ.get("BENCH_PARTS", "").split(",")))


def want(part):
    """BENCH_PARTS=a,b 时只跑这几段（干跑只重跑改到的，省 GitHub 的分钟数）；不设就全跑。"""
    return not PARTS or part in PARTS


FAILS = {}


def too_many(key):
    """同一种连着失败 3 次，后面的就不再试（快照坏了时不至于每次空等两分钟）。"""
    return FAILS.get(key, 0) >= 3


def guard(rec, base, fn, envs=(), key=None):
    """跑一次测量：成了记一行 ok=true；出错记 ok=false 和原因，收拾干净，接着跑下一次。"""
    if key is not None and too_many(key):
        return rec.put(ok=False, skipped=True, err="这一种已连着失败 3 次，跳过", **base)
    try:
        res = fn()
        if key is not None:
            FAILS[key] = 0
        return rec.put(ok=True, **base, **(res or {}))
    except Exception as e:  # noqa: BLE001
        extra = {}
        for env in envs:
            if isinstance(env, B.VMEnv):
                extra[f"vmm_log_{env.name}"] = env.vmm_log()
        log("失败", base, repr(e)[:400])
        if key is not None:
            FAILS[key] = FAILS.get(key, 0) + 1
        rec.put(ok=False, err=repr(e)[:1500], tb=traceback.format_exc()[-1500:], **base, **extra)
        for env in envs:
            with contextlib.suppress(Exception):
                env.stop(graceful=False)
        return None


def rec_ok(rec):
    return True


def drop_caches():
    sh("sync; echo 3 > /proc/sys/vm/drop_caches")


def app(path="/work/.berth-marker", expect="berth-user-disk"):
    """起作品的 Node 服务并等它第一次回对话：它每次都读一遍作品盘上的文件回内容，
    回出 expect 才算醒（证明作品的 /data 真挂上、读得到）；expect=None 时只要回话就算（没带盘的密度实验）。"""
    cond = (f'[ "$(curl -sf http://127.0.0.1:8080/ 2>/dev/null)" != {expect} ]' if expect
            else "! curl -sf http://127.0.0.1:8080/ >/dev/null 2>&1")
    return (f"a0=$(date +%s.%N); setsid nohup node /usr/local/lib/berth/server.js {path} >/dev/null 2>&1 </dev/null &\n"
            f"al=$(date +%s.%N); i=0; while {cond}; do i=$((i+1)); [ $i -gt 30000 ] && exit 9; sleep 0.002; done\n"
            'echo "A0=$a0 AL=$al AO=$(date +%s.%N)"')


def app_marks(out):
    """app() 打的点（环境里的钟）：主程序起来用了多久、起来到第一次回对话又多久。"""
    kv = dict(x.split("=", 1) for x in out.split() if x[:3] in ("A0=", "AL=", "AO="))
    try:
        return {"t_main_launch": float(kv["AL"]) - float(kv["A0"]), "t_main_to_ok": float(kv["AO"]) - float(kv["AL"])}
    except (KeyError, ValueError):
        return {}


APP = app()
APP_NODISK = app("/etc/os-release", None)
APP_CHECK = '[ "$(curl -sf http://127.0.0.1:8080/)" = berth-user-disk ]'
GV = "sh /usr/local/lib/berth/gv.sh"
GUEST_AVAIL = "awk '/MemAvailable/{print $2}' /proc/meminfo"


def gv_call(host, cmd, timeout=120):
    """在客机里跑一条要用 runsc 的命令。命令丢到后台、结果写进文件，宿主隔 10 毫秒去读一次：
    干跑 2026-11-20 看到客机里一跑 runsc，经 vsock 发命令的那个 shell 就断了（setsid 也没用），
    这样连接断了也拿得回结果。失败时把客机内核日志一起带回来。"""
    tag = os.urandom(4).hex()
    job = f"/run/gvj.{tag}"
    host.exec(f"setsid nohup sh -c {shlex.quote(f'{cmd} > {job}.out 2>&1; echo $? > {job}.rc')} </dev/null >/dev/null 2>&1 &", 30)
    end = now() + timeout
    code = None
    while now() < end:
        with contextlib.suppress(B.GuestErr, OSError):
            rc, out = host.exec(f"cat {job}.rc 2>/dev/null", 10)
            if out.strip().isdigit():
                code = int(out.strip())
                break
        time.sleep(0.01)
    out = ""
    with contextlib.suppress(B.GuestErr, OSError):
        _, out = host.exec(f"cat {job}.out 2>/dev/null; rm -f {job}.*", 10)
    if code is None or code != 0:
        diag = ""
        with contextlib.suppress(B.GuestErr, OSError):
            _, diag = host.exec("dmesg | tail -30", 10)
        raise RuntimeError(f"客机里的 runsc 命令{'超时' if code is None else f'退出 {code}'}：{out[-600:]}\n== dmesg\n{diag[-1500:]}")
    return out


def guest_avail_kb(env):
    rc, out = env.exec(GUEST_AVAIL, 30)
    return int(out.split()[-1]) if rc == 0 else None


# ---------------- env ----------------

def exp_env():
    def rd(p):
        with contextlib.suppress(OSError):
            return open(p).read().strip()
        return None

    info = {
        "uname": " ".join(os.uname()),
        "os_release": rd("/etc/os-release"),
        "cmdline": rd("/proc/cmdline"),
        "lscpu": sh(["lscpu", "-J"], check=False).stdout,
        "microcode": sh("grep -m1 microcode /proc/cpuinfo", check=False).stdout.strip(),
        "cpu_model": sh("grep -m1 'model name' /proc/cpuinfo", check=False).stdout.strip(),
        "vulnerabilities": {os.path.basename(p): rd(p) for p in glob.glob("/sys/devices/system/cpu/vulnerabilities/*")},
        "smt_control": rd("/sys/devices/system/cpu/smt/control"),
        "smt_active": rd("/sys/devices/system/cpu/smt/active"),
        "dmi": {k: rd(f"/sys/class/dmi/id/{k}") for k in ("sys_vendor", "product_name", "board_name", "bios_version")},
        "meminfo": B.meminfo(),
        "numa": sh(["numactl", "-H"], check=False).stdout,
        "lsblk": sh(["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MODEL,ROTA,TRAN,MOUNTPOINTS"], check=False).stdout,
        "zpool": sh(["zpool", "status"], check=False).stdout,
        "zfs_version": sh(["zfs", "version"], check=False).stdout,
        "sysctl": {k: rd(f"/proc/sys/{k.replace('.', '/')}") for k in
                   ("vm.overcommit_memory", "vm.swappiness", "kernel.numa_balancing", "vm.unprivileged_userfaultfd")},
        "thp": rd("/sys/kernel/mm/transparent_hugepage/enabled"),
        "ksm_run": rd("/sys/kernel/mm/ksm/run"),
        "swaps": rd("/proc/swaps"),
        "versions": rd(f"{B.ROOT}/versions.txt"),
        "state": B.STATE,
        "guest_kernel_sha256": sh(["sha256sum", B.KERNEL], check=False).stdout.split()[:1],
        "base_img": rd(f"{B.ROOT}/images/SHA256SUMS"),
        "base_img_bytes": os.path.getsize(B.BASE_IMG) if os.path.exists(B.BASE_IMG) else None,
        "kata_config": B.kata_config_src() if glob.glob("/opt/kata/share") else None,
        "quick": B.QUICK,
    }
    info["h12"] = h12_check(info)
    os.makedirs(B.RESULTS, exist_ok=True)
    with open(f"{B.RESULTS}/env.json", "w") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    for p in glob.glob(f"{B.ROOT}/guest/config-*"):
        shutil.copy(p, B.RESULTS)
    for p in (f"{B.ROOT}/dl/SOURCES.txt", f"{B.ROOT}/versions.txt"):
        if os.path.exists(p):
            shutil.copy(p, B.RESULTS)
    log("机器信息写进 env.json")


# ---------------- probe ----------------

PROBE = ("echo '== id'; id; uname -r; echo '== mounts'; grep -E ' (/|/work|/tmp|/data) ' /proc/self/mountinfo; "
         "echo '== net'; ip -br addr 2>&1; echo '== tmp'; echo hello > /tmp/probe.txt && echo TMP_WRITE_OK; "
         "echo '== tools'; for b in node python3 curl socat setsid godot runsc zig; do command -v $b >/dev/null && echo $b ok || echo $b MISSING; done")
PROBE2 = ("cat /tmp/probe.txt 2>&1 && echo TMP_PERSIST_OK; echo '== app'; "
          "setsid nohup node /usr/local/lib/berth/server.js >/tmp/node.log 2>&1 </dev/null & "
          "i=0; while ! curl -sf http://127.0.0.1:8080/ >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 200 ] && break; sleep 0.05; done; "
          "curl -sS -m 2 http://127.0.0.1:8080/ 2>&1; echo; echo '== node log'; cat /tmp/node.log 2>&1 | tail -5; "
          "echo '== ps'; ps -eo pid,comm 2>&1 | grep -E 'node|sleep' | head")


def exp_probe():
    """干跑先跑这一步：每种环境起一台、跑一遍探查，原样记下输出，好一次看清各种环境里缺什么。"""
    rec = Rec("probe")
    for kind in list(B.KINDS) + ["gvisor-in-vm"]:
        disk = Disk(f"pr-{kind}")
        env = make_env("ch-snap-cow" if kind == "gvisor-in-vm" else kind, f"pr-{kind}", shape="dute", disk=disk)

        def one():
            disk.create()
            env.start()
            res = {}
            if kind == "gvisor-in-vm":
                env.exec(f"cat > /work/p1.sh <<'EOF1'\n{PROBE}\nEOF1\ncat > /work/p2.sh <<'EOF2'\n{PROBE2}\nEOF2", 30)
                try:
                    out = gv_call(env, f"export GV_DEBUG=1; {GV} start pr /work && {GV} x pr 'sh /data/p1.sh'; {GV} x pr 'sh /data/p2.sh'")
                    rc = 0
                except RuntimeError as e:
                    rc, out = 1, str(e)
                res["probe"] = out[-3000:]
            else:
                rc, out = env.exec(PROBE, 60)
                res["probe"] = out[-2000:]
                rc2, out2 = env.exec(PROBE2, 60)
                res["probe2"] = out2[-2000:]
            res["rc"] = rc
            env.stop(graceful=True)
            disk.destroy()
            return res

        guard(rec, {"kind": kind}, one, [env])
        with contextlib.suppress(Exception):
            disk.destroy()


def h12_check(info):
    """H12：这台裸金属能开 KVM、读得出硬件漏洞状态、有没有本机 NVMe、能不能关嵌套虚拟化。
    关嵌套是重新加载 kvm 模块（nested=0）再改回原样，要在还没有虚拟机跑着的时候做（env 这一步排在共用快照之前）。"""
    r = {"kvm": os.path.exists("/dev/kvm"),
         "vuln_readable": bool(info["vulnerabilities"]) and all(v for v in info["vulnerabilities"].values())}
    disks = sh(["lsblk", "-dno", "NAME,TRAN,ROTA,MODEL"], check=False).stdout
    r["disks"] = disks
    r["nvme"] = any(l.split()[0].startswith("nvme") for l in disks.splitlines() if l.split())
    mod = "kvm_intel" if os.path.exists("/sys/module/kvm_intel") else "kvm_amd"
    par = f"/sys/module/{mod}/parameters/nested"
    before = open(par).read().strip() if os.path.exists(par) else None
    r["nested_before"] = before
    if before is not None:
        orig = "1" if before in ("Y", "1") else "0"
        errs = []
        try:
            for cmd in (f"modprobe -r {mod}", f"modprobe {mod} nested=0"):
                errs.append(sh(cmd, check=False).stderr)
            r["nested_off"] = open(par).read().strip() if os.path.exists(par) else None
        finally:
            # 不管上面成没成，一定装回原样；装回失败再试一次不带参数地装（冷读 2026-11-20：装不回 KVM 就没了）
            sh(f"modprobe -r {mod}", check=False)
            if sh(f"modprobe {mod} nested={orig}", check=False).returncode != 0:
                errs.append(sh(f"modprobe {mod}", check=False).stderr)
        r["nested_restored"] = open(par).read().strip() if os.path.exists(par) else None
        r["nested_toggle_err"] = "".join(errs).strip()[-300:]
        r["can_disable_nested"] = r.get("nested_off") in ("N", "0")
        if not os.path.exists("/dev/kvm"):
            raise SystemExit("关嵌套以后 KVM 没装回来，/dev/kvm 不在了：停下，别接着拍快照、跑实验")
        os.chmod("/dev/kvm", 0o666)
    return r


# ---------------- snapshots ----------------

def exp_snapshots():
    rec = Rec("snapshots")
    failed = []
    for cls in (B.FC, B.CH):
        for shape in ("dute", "pages"):
            out = f"{B.SNAP}/{cls.kind}-{shape}"
            env = cls(f"snap-{cls.kind}-{shape}", shape=shape, mode="cold")

            def one():
                shutil.rmtree(out, ignore_errors=True)
                env.start()
                # 丢掉客机缓存，尤其是"这个人的盘"槽位上那块占位盘的缓存，恢复后换盘才不会读到旧块
                # （块设备的 st_size 是 0，fincore 核不出来；真正的核验在 disk 实验里：换上盘后 e2fsck 加模板指纹）
                rc, o = env.exec("sync && echo 3 > /proc/sys/vm/drop_caches && blockdev --flushbufs /dev/vdb", 60)
                if rc:
                    raise RuntimeError(f"占位盘的客机缓存没丢掉，不拍: rc={rc} {o[-300:]}")
                t = now()
                env.snapshot(out)
                t_snap = now() - t
                env.stop(graceful=False)
                files = {os.path.basename(p): {"bytes": os.path.getsize(p), "disk_bytes": os.stat(p).st_blocks * 512}
                         for p in glob.glob(out + "/*")}
                return {"t_boot": env.t["t_ready"], "t_snap": t_snap, "files": files}

            if guard(rec, {"vmm": cls.kind, "shape": shape}, one, [env]) is None:
                failed.append(f"{cls.kind}-{shape}")
    if failed:
        raise SystemExit(f"共用快照没拍成：{' '.join(failed)}（后面恢复类的实验都会失败）")


# ---------------- boot ----------------

BOOT_DUTE = ["fc-cold", "fc-snap", "ch-cold", "ch-snap-copy", "ch-snap-cow", "ch-snap-uffd", "ch-snap-hotplug",
             "gvisor-systrap", "gvisor-kvm", "kata-clh", "runc"]
BOOT_PAGES = ["fc-snap", "ch-snap-cow", "gvisor-systrap", "kata-clh", "runc"]


def exp_boot():
    rec = Rec("boot")
    R = n(30, 2)
    # 1. dute 形状：从"产品说用户来了"到这个人的盘能读、Node 应用能回话；再扔掉
    for i in range(R if want("wake-dute") else 0):
        for kind in BOOT_DUTE:
            disk = Disk(f"b{i}-{kind}")
            env = make_env(kind, f"b{i}-{kind}", shape="dute", disk=disk)

            def one():
                t_disk = disk.create()
                drop_caches() if i == 0 else None
                t = env.start()
                mem = {}
                if i < 5:
                    time.sleep(5)
                    mem = env.host_mem()
                rc, out, t_app = env.timed_exec(APP, 120)
                if rc:
                    raise RuntimeError(f"应用没起来: {out[-300:]}")
                s = env.stop(graceful=True)
                t2 = now()
                disk.destroy()
                return {**t, "t_disk_prep": t_disk, "t_app": t_app, **{f"idle_{k}": v for k, v in mem.items()},
                        **s, "t_disk_destroy": now() - t2}

            guard(rec, {"part": "wake-dute", "kind": kind, "rep": i, "host_cache_cold": i == 0}, one, [env], key=("wake-dute", kind))
            with contextlib.suppress(Exception):
                disk.destroy()
    # 2. Pages 形状：从请求到作品的 Node 服务第一次回话，带作品的 /data（这个人的盘挂在 /work）。
    #    gVisor 这一行就是 Pages 今天"停着的 gVisor 容器连同盘留着，来人再起"的做法。
    for i in range(R if want("wake-pages") else 0):
        for kind in BOOT_PAGES:
            disk = Disk(f"p{i}-{kind}")
            env = make_env(kind, f"p{i}-{kind}", shape="pages", disk=disk, probe=False)

            def one():
                disk.create()
                if i == 0:
                    drop_caches()
                t0 = now()
                t = env.start()
                t_env = now() - t0
                rc, out = env.exec(APP, 120)
                if rc:
                    raise RuntimeError(f"应用没起来: {out[-300:]}")
                t_to_app = now() - t0
                time.sleep(1)
                mem = env.host_mem()
                s = env.stop(graceful=True)
                disk.destroy()
                # H7 要的三个时刻：环境起来（t_env）、主程序起来（t_env + t_main_launch）、应用连得上（t_to_app）
                return {**t, "t_env": t_env, **app_marks(out), "t_to_app": t_to_app,
                        **{f"app_{k}": v for k, v in mem.items()}, **s}

            guard(rec, {"part": "wake-pages", "kind": kind, "rep": i, "host_cache_cold": i == 0}, one, [env], key=("wake-pages", kind))
            with contextlib.suppress(Exception):
                disk.destroy()
    # 2a. 同时醒：4 个、8 个一起，每个量从请求到回话（Pages 要的顺带数，不算判据）
    import concurrent.futures as cf
    for burst, bshape, bkinds in ((4, "pages", ("gvisor-systrap", "fc-snap", "ch-snap-cow")),
                                  (8, "pages", ("gvisor-systrap", "fc-snap", "ch-snap-cow")),
                                  (n(10, 2), "dute", ("ch-snap-cow", "fc-snap"))):  # H2：10 台 dute 同时醒
        for i in range(n(5, 1) if want("wake-burst") else 0):
            for kind in bkinds:
                disks = [Disk(f"w{burst}{bshape[0]}-{i}-{j}-{kind}") for j in range(burst)]
                envs = [make_env(kind, f"w{burst}{bshape[0]}-{i}-{j}-{kind}", shape=bshape, disk=d, probe=False)
                        for j, d in enumerate(disks)]
                try:
                    for d in disks:
                        d.create()

                    t_batch = now()

                    def wake(env):
                        t0 = t_batch  # 整批同一个请求时刻，派发到线程的等待也算在里面
                        env.start()
                        if env.shape == "dute":  # H2 的终点是盘能读，跟单独醒一样
                            return now() - t0
                        rc, out = env.exec(APP, 120)
                        if rc:
                            raise RuntimeError(f"应用没起来: {out[-200:]}")
                        return now() - t0

                    with cf.ThreadPoolExecutor(burst) as ex:
                        futs = [ex.submit(wake, e) for e in envs]
                        for j, fu in enumerate(futs):
                            try:
                                rec.put(ok=True, part="wake-burst", kind=kind, burst=burst, shape=bshape, rep=i, j=j,
                                        t_to_app=fu.result())
                            except Exception as e:  # noqa: BLE001
                                rec.put(ok=False, part="wake-burst", kind=kind, burst=burst, shape=bshape, rep=i, j=j,
                                        err=repr(e)[:600])
                finally:
                    for e in envs:
                        with contextlib.suppress(Exception):
                            e.stop(graceful=False)
                    for d in disks:
                        with contextlib.suppress(Exception):
                            d.destroy()
    # 2b. Pages 那条路：开着的虚拟机里起一个 gVisor 沙箱，挂上作品的 /data，量到 Node 回话
    for host_kind in (("ch-snap-cow", "fc-snap") if want("gvisor-in-vm") else ()):
        disk = Disk(f"gvm-{host_kind}")
        host = make_env(host_kind, f"gvm-{host_kind}", shape="dute", disk=disk)
        try:
            disk.create()
            host.start()
            for i in range(R):
                def one():
                    host.exec(f"mkdir -p /work/d{i} && cp /work/.berth-marker /work/d{i}/", 30)  # 作品的数据本来就在
                    a0 = guest_avail_kb(host)
                    t0 = now()
                    gv_call(host, f"{GV} up g{i} /work/d{i}")
                    t_to_app = now() - t0
                    time.sleep(1)
                    a1 = guest_avail_kb(host)
                    t = now()
                    gv_call(host, f"{GV} down g{i}", 60)
                    return {"t_to_app": t_to_app, "t_down": now() - t, "guest_mem_kb": (a0 - a1) if a0 and a1 else None}

                guard(rec, {"part": "gvisor-in-vm", "kind": host_kind, "data": "dir", "rep": i}, one,
                      key=("gvisor-in-vm", host_kind, "dir"))
                with contextlib.suppress(Exception):
                    gv_call(host, f"{GV} down g{i}", 60)
        except Exception as e:  # noqa: BLE001
            rec.put(ok=False, part="gvisor-in-vm", kind=host_kind, err=repr(e)[:800], vmm_log=host.vmm_log())
        finally:
            host.stop(graceful=True)
            with contextlib.suppress(Exception):
                disk.destroy()
    #     作品的盘不在这台虚拟机里：先热插上来（Cloud Hypervisor），再起沙箱
    host = make_env("ch-snap-cow", "gvmh", shape="dute")
    try:
        if want("gvisor-in-vm"):
            host.start()
        for i in range(R if want("gvisor-in-vm") else 0):
            disk = Disk(f"gvmh{i}")

            def one():
                disk.create()
                t0 = now()
                host.hot_add(disk, path=f"/tmp/data{i}")
                try:
                    t_attach = now() - t0
                    gv_call(host, f"{GV} up g{i} /tmp/data{i}")
                    t_to_app = now() - t0
                finally:
                    with contextlib.suppress(Exception):
                        gv_call(host, f"{GV} down g{i}", 60)
                    host.hot_remove()  # 拔不干净就抛出去，这台宿主虚拟机后面的样本不可信
                disk.destroy()
                return {"t_attach": t_attach, "t_to_app": t_to_app, "dev": host.hot_dev}

            if guard(rec, {"part": "gvisor-in-vm", "kind": "ch-snap-cow", "data": "hotplug", "rep": i}, one,
                     key=("gvisor-in-vm", "hotplug")) is None or not rec_ok(rec):
                break
            with contextlib.suppress(Exception):
                disk.destroy()
    except Exception as e:  # noqa: BLE001
        rec.put(ok=False, part="gvisor-in-vm", kind="ch-snap-cow", data="hotplug", err=repr(e)[:800])
    finally:
        host.stop(graceful=False)
    # 2d. H27：Pages 发新版时作品后端停多久（gVisor 那一档、独占读写盘）。从旧的停写那一刻起，
    #     berth 的做法：旧的停写（SIGTERM，最多等 10 秒）→ 卸盘 → 挂盘 → 新的起来、作品回话；
    #     Pages 今天的做法（同机对照）：同样停旧的，不卸不挂，新容器直接用同一个挂载点起来到回话。
    for i in range(R if want("handover") else 0):
        for way in ("berth", "pages-today"):
            disk = Disk(f"ho{i}-{way}")
            # 容器的 1 号进程收到 SIGTERM 就退（sleep 当 1 号进程会忽略 SIGTERM，两种做法都得干等 10 秒）
            init = ["/bin/sh", "-c", "trap 'exit 0' TERM; sleep infinity & wait"]
            old = make_env("gvisor-systrap", f"hoa{i}-{way}", shape="pages", disk=disk, probe=False, init_args=init)
            box = {}

            def one():
                disk.create()
                old.start()
                rc, out = old.exec(APP, 120)
                if rc:
                    raise RuntimeError(f"旧版本没起来: {out[-300:]}")
                t0 = now()
                old.stop_writes(10.0)
                t_stopped = now() - t0
                res = {"t_stop_old": t_stopped}
                if way == "berth":
                    t = now()
                    B.host_umount(old.mnt)
                    res["t_umount"] = now() - t
                    new = make_env("gvisor-systrap", f"hob{i}-{way}", shape="pages", disk=disk, probe=False, init_args=init)
                else:
                    new = make_env("gvisor-systrap", f"hob{i}-{way}", shape="pages", probe=False, mnt=old.mnt, init_args=init)
                box["new"] = new
                new.start()
                rc, out = new.exec(APP, 120)
                if rc:
                    raise RuntimeError(f"新版本没起来: {out[-300:]}")
                res["handover_s"] = now() - t0
                res["t_mount"] = new.t.get("t_mount")
                new.stop(graceful=True)
                old.stop(graceful=False)  # 收掉旧的 cgroup；pages-today 那一路顺带卸掉旧挂载点
                disk.destroy()
                return res

            guard(rec, {"part": "handover", "kind": "gvisor-systrap", "way": way, "rep": i}, one, [old],
                  key=("handover", way))
            if box.get("new"):
                with contextlib.suppress(Exception):
                    box["new"].stop(graceful=False)
            with contextlib.suppress(Exception):
                disk.destroy()
    # 2c. gVisor 的检查点：跑着的作品连内存存到盘上、容器停下；来人时从检查点恢复
    for i in range(R if want("gvisor-ckpt") else 0):
        disk = Disk(f"ck{i}")
        env = make_env("gvisor-systrap", f"ck{i}", shape="pages", disk=disk)
        image = f"{B.SRV}/ckpt/ck{i}"

        def one():
            disk.create()
            env.start()
            rc, out = env.exec(APP, 120)
            if rc:
                raise RuntimeError(f"应用没起来: {out[-300:]}")
            shutil.rmtree(image, ignore_errors=True)
            t = now()
            env.checkpoint(image)
            t_ckpt = now() - t
            size = sum(os.stat(p).st_blocks * 512 for p in glob.glob(image + "/**", recursive=True) if os.path.isfile(p))
            if cold:
                drop_caches()
            t0 = now()
            env.restore(image)
            # 恢复以后作品可能还在后台装页，等它回话（最多 30 秒）
            rc, out = env.exec(f"i=0; while ! {APP_CHECK} 2>/dev/null; do i=$((i+1)); [ $i -gt 6000 ] && exit 9; sleep 0.005; done", 60)
            if rc:
                raise RuntimeError(f"恢复后应用不回话: {out[-300:]}")
            t_to_app = now() - t0
            env.stop(graceful=True)
            disk.destroy()
            shutil.rmtree(image, ignore_errors=True)
            return {"t_checkpoint": t_ckpt, "image_bytes": size, "t_to_app": t_to_app}

        cold = i % 2 == 1
        guard(rec, {"part": "gvisor-ckpt", "kind": "gvisor-systrap", "rep": i, "image_cache_cold": cold}, one, [env],
              key=("gvisor-ckpt",))
        with contextlib.suppress(Exception):
            disk.destroy()
        shutil.rmtree(image, ignore_errors=True)
    # 3. 刚闲下来先在内存里停一会儿：Pages 形状、跑着 Node，暂停，再恢复到 Node 回话
    for kind, pshape in (("fc-snap", "pages"), ("ch-snap-cow", "pages"), ("gvisor-systrap", "pages"),
                         ("fc-snap", "dute"), ("ch-snap-cow", "dute")) if want("pause") else ():  # dute 形状是 H3
        pdisk = Disk(f"pz-{kind}-{pshape}")
        env = make_env(kind, f"pz-{kind}-{pshape}", shape=pshape, disk=pdisk)
        try:
            pdisk.create()
            env.start()
            rc, out = env.exec(APP, 120)
            if rc:
                raise RuntimeError(f"应用没起来: {out[-300:]}")
            for i in range(R):
                def one():
                    t = now()
                    env.pause()
                    t_pause = now() - t
                    time.sleep(0.5)
                    t = now()
                    env.resume()
                    rc, out = env.exec(APP_CHECK, 30)
                    if rc:
                        raise RuntimeError(f"恢复后不回话: {out[-200:]}")
                    return {"t_pause": t_pause, "t_resume_to_app": now() - t}

                guard(rec, {"part": "pause", "kind": kind, "shape": pshape, "rep": i}, one, key=("pause", kind, pshape))
        except Exception as e:  # noqa: BLE001
            rec.put(ok=False, part="pause", kind=kind, shape=pshape, err=repr(e)[:800])
        finally:
            env.stop(graceful=False)
            with contextlib.suppress(Exception):
                pdisk.destroy()
    # 3b. H1：同一份共用快照连着恢复 100 次，全成功才算稳；顺带记虚拟机管理程序自己的常驻内存
    for kind, times in (("ch-snap-cow", 100), ("fc-snap", 30)):  # 原文 H1 只要 Cloud Hypervisor 100 次；Firecracker 30 次作对照
        for i in range(n(times, 3) if want("restore100") else 0):
            env = make_env(kind, f"r{i}-{kind}", shape="dute")

            def one():
                t = env.start()
                self_kb = env.vmm_self_kb()
                env.stop(graceful=False)
                return {**t, "vmm_self_kb": self_kb}

            guard(rec, {"part": "restore100", "kind": kind, "rep": i}, one, [env])
    # 3c. H1：按需补页（userfaultfd）恢复到一半，快照的内存文件坏了——管理程序是退出还是挂住；退成冷开机要多久
    for i in range(n(3, 1) if want("uffd-fault") else 0):
        copy = f"{B.SRV}/snapcopy/ch-dute-{i}"
        env = B.CH(f"uf{i}", shape="dute", mode="ondemand", snap=copy)
        fb = make_env("ch-cold", f"uf{i}-cold", shape="dute")

        def one():
            shutil.rmtree(copy, ignore_errors=True)
            shutil.copytree(f"{B.SNAP}/ch-dute", copy)
            env._prep_dir()
            t0 = now()
            env._boot()
            time.sleep(0.05)
            with open(f"{copy}/memory-ranges", "r+b") as mf:  # 恢复刚开始就把内存文件截断
                mf.truncate(0)
            outcome = "ready"
            try:
                B.wait_vsock_ready(env.uds, n(30, 10), env.proc)
                rc, _ = env.exec("echo 3 > /proc/sys/vm/drop_caches; cat /proc/meminfo >/dev/null", 20)
                outcome = "ready-and-ran" if rc == 0 else "ready-but-cmd-failed"
            except B.GuestErr:
                outcome = "vmm-exited"
            except (TimeoutError, OSError):
                outcome = "hung"
            t_detect = now() - t0
            env.stop(graceful=False)
            t = now()
            fb.start()
            t_fallback = now() - t
            fb.stop(graceful=False)
            shutil.rmtree(copy, ignore_errors=True)
            return {"outcome": outcome, "t_detect": t_detect, "t_fallback_cold": t_fallback, "vmm_log": env.vmm_log()[-400:]}

        guard(rec, {"part": "uffd-fault", "kind": "ch-snap-uffd", "rep": i}, one, [env, fb])
        shutil.rmtree(copy, ignore_errors=True)
    # 4. 旧做法对照：给每个人存一份整机内存快照（用过的环境，1 GiB 脏内存），再从它恢复
    for kind, restore_modes in (("fc", ("snap",)), ("ch", ("copy", "ondemand"))):
        for i in range(n(10, 1) if want("user-memsnap") else 0):
            for mode in restore_modes:
                disk = Disk(f"us{i}-{kind}-{mode}")
                usnap = f"{B.SRV}/usersnap/{kind}-{mode}-{i}"
                cls = B.FC if kind == "fc" else B.CH
                a = cls(f"usa{i}-{kind}-{mode}", shape="dute", disk=disk, mode="cold")
                b = cls(f"usb{i}-{kind}-{mode}", shape="dute", disk=disk, mode=mode, snap=usnap)

                def one():
                    disk.create()
                    a.start()
                    rc, out = a.exec("setsid nohup python3 /usr/local/lib/berth/alloc.py 1024 100000 /tmp/alloc.done "
                                     ">/dev/null 2>&1 </dev/null & i=0; while [ ! -f /tmp/alloc.done ]; do sleep 0.05; done", 300)
                    shutil.rmtree(usnap, ignore_errors=True)
                    t = now()
                    a.snapshot(usnap)
                    t_snap = now() - t
                    size = sum(os.stat(p).st_blocks * 512 for p in glob.glob(usnap + "/*"))
                    a.stop(graceful=False)  # 快照已含盘的状态；不卸盘，恢复的那台接着用
                    drop_caches()
                    b._prep_dir()
                    t0 = now()
                    b._boot()
                    B.wait_vsock_ready(b.uds, 120, b.proc)
                    t_ready = now() - t0
                    rc, out = b.exec("pgrep -f '[a]lloc.py' >/dev/null && cat /work/.berth-marker >/dev/null", 60)
                    t_usable = now() - t0
                    b.stop(graceful=True)
                    disk.destroy()
                    shutil.rmtree(usnap, ignore_errors=True)
                    return {"t_snap": t_snap, "snap_disk_bytes": size, "t_ready": t_ready, "t_usable": t_usable,
                            "procs_survived": rc == 0}

                guard(rec, {"part": "user-memsnap", "kind": kind, "mode": mode, "rep": i}, one, [a, b])
                with contextlib.suppress(Exception):
                    disk.destroy()
                shutil.rmtree(usnap, ignore_errors=True)


# ---------------- disk ----------------

WRITE_F1 = "head -c 268435456 /dev/urandom > /work/f1 && sync /work/f1 && sha256sum /work/f1 | cut -c1-64"
WRITE_F2 = "head -c 16777216 /dev/urandom > /work/f2 && sha256sum /work/f2 | cut -c1-64"
CHECK = "for f in f1 f2; do if [ -f /work/$f ]; then echo $f=$(sha256sum /work/$f | cut -c1-64); else echo $f=MISSING; fi; done"


def parse_check(out):
    d = dict(x.split("=", 1) for x in out.split() if x.startswith(("f1=", "f2=")))
    return d.get("f1", "MISSING"), d.get("f2", "MISSING")


def sha_ok(h):
    return len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def exp_disk():
    rec = Rec("disk")
    # 1. 换一台：A 写、正常扔掉（sync、卸载、杀进程），B 从共享快照起、换上同一块盘、核对
    for kind in ("ch-snap-cow", "fc-snap"):
        for i in range(n(10, 1) if want("move") else 0):
            disk = Disk(f"mv{i}-{kind}")
            a = make_env(kind, f"mva{i}-{kind}", disk=disk)
            b = make_env(kind, f"mvb{i}-{kind}", disk=disk)

            def one():
                disk.create()
                a.start()
                rc, out, t_write = a.timed_exec(WRITE_F1, 300)
                if rc:
                    raise RuntimeError(f"A 写盘失败（不算换盘丢数据）: {out[-300:]}")
                h1 = out.strip().split()[-1]
                rc, out2 = a.exec(WRITE_F2, 120)
                if rc:
                    raise RuntimeError(f"A 写盘失败: {out2[-300:]}")
                h2 = out2.strip().split()[-1]
                s = a.stop(graceful=True)
                if not (sha_ok(h1) and sha_ok(h2)):
                    raise RuntimeError(f"A 写完没拿到校验和: {h1!r} {h2!r}")
                b.start()
                rc, chk, t_check = b.timed_exec(CHECK, 300)
                g1, g2 = parse_check(chk)
                b.stop(graceful=True)
                disk.destroy()
                return {"t_attach_a": a.t["t_usable"], "t_write_256m_fsync": t_write, "t_detach": s["t_stop"],
                        "t_umount": s.get("t_umount"), "t_attach_b": b.t["t_usable"],
                        "f1_ok": g1 == h1, "f2_ok": g2 == h2, "h1": h1}

            guard(rec, {"part": "move", "kind": kind, "rep": i}, one, [a, b], key=("move", kind))
            with contextlib.suppress(Exception):
                disk.destroy()
    # 2. 机器突然没了：A 写完一个 fsync 过的、一个没 fsync 的，直接杀掉，B 换上同一块盘看剩什么
    for kind in ("ch-snap-cow", "fc-snap"):
        for i in range(n(3, 1) if want("crash") else 0):
            disk = Disk(f"cr{i}-{kind}")
            after = Disk(f"cr{i}-{kind}-after")
            a = make_env(kind, f"cra{i}-{kind}", disk=disk)
            b = make_env(kind, f"crb{i}-{kind}", disk=disk)

            def one():
                disk.create()
                a.start()
                rc, out = a.exec(WRITE_F1, 300)
                if rc:
                    raise RuntimeError(f"A 写盘失败（不算换盘丢数据）: {out[-300:]}")
                h1 = out.strip().split()[-1]
                rc, out2 = a.exec(WRITE_F2, 120)
                if rc:
                    raise RuntimeError(f"A 写盘失败: {out2[-300:]}")
                h2 = out2.strip().split()[-1]
                if not (sha_ok(h1) and sha_ok(h2)):
                    raise RuntimeError(f"A 写完没拿到校验和: {h1!r} {h2!r}")
                a.stop(graceful=False)
                if B.ZFS:
                    # 断电式：杀掉以后立刻拍 ZFS 快照、克隆给 B。还在宿主页缓存里没进 ZFS 的，这份里就没有
                    sh(["zfs", "snapshot", f"{disk.ds}@crash"])
                    sh(["zfs", "clone", "-o", "volmode=dev", f"{disk.ds}@crash", after.ds])
                    B.wait_path(after.dev, 30)
                    b.disk = after
                b.start()
                rc, chk = b.exec(CHECK, 300)
                g1, g2 = parse_check(chk)
                b.stop(graceful=True)
                after.destroy()
                disk.destroy()
                # 这一路是"宿主还活着、虚拟机管理程序崩了"：以杀掉那一刻 ZFS 已经收下的为准（冷读 2026-11-20：
                # zfs snapshot 会等事务组落盘，不等于宿主断电；宿主断电要另做故障试验）
                return {"t_attach_b_after_crash": b.t["t_usable"], "f1_ok": g1 == h1,
                        "f2_survived": g2 == h2, "f2_missing": g2 == "MISSING", "crash_model": "vmm-crash-host-alive"}

            guard(rec, {"part": "crash", "kind": kind, "rep": i}, one, [a, b])
            with contextlib.suppress(Exception):
                after.destroy()
            with contextlib.suppress(Exception):
                disk.destroy()
    # 2b. 槽位换盘读到的是不是这个人的盘：挂载前 e2fsck 只读查一遍，挂上后重算模板内容的指纹
    want_digest = open(f"{B.ROOT}/images/work.digest").read().strip() if os.path.exists(f"{B.ROOT}/images/work.digest") else None
    for kind in ("fc-snap", "ch-snap-copy", "ch-snap-cow", "ch-snap-uffd"):
        for i in range(n(2, 1) if want("integrity") else 0):
            disk = Disk(f"ig{i}-{kind}")
            env = make_env(kind, f"ig{i}-{kind}", disk=disk)

            def one():
                disk.create()
                token = os.urandom(16).hex()
                m = B.host_mount(disk.dev, f"tok-{disk.name}")
                with open(f"{m}/.berth-token", "w") as f:
                    f.write(token)
                B.host_umount(m)
                env._prep_dir()
                env._boot()
                B.wait_vsock_ready(env.uds, 120, env.proc)
                rc, out = env.exec("e2fsck -fn /dev/vdb >/tmp/fsck.log 2>&1; echo FSCK=$?; mount -o ro,noatime /dev/vdb /work && "
                                   "echo TOKEN=$(cat /work/.berth-token) && cd /work && find . -type f ! -name .berth-token -print0 "
                                   "| LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-64", 600)
                fsck = int(out.split("FSCK=")[1].split()[0]) if "FSCK=" in out else None
                got_token = out.split("TOKEN=")[1].split()[0] if "TOKEN=" in out else None
                got = out.strip().split()[-1]
                env.stop(graceful=True)
                disk.destroy()
                return {"fsck_rc": fsck, "digest_ok": want_digest is not None and got == want_digest, "token_ok": got_token == token,
                        "digest": got}

            guard(rec, {"part": "integrity", "kind": kind, "rep": i}, one, [env])
            with contextlib.suppress(Exception):
                disk.destroy()
    # 3. 热插热拔（Cloud Hypervisor）：两台都开着，盘从 A 拔下插到 B
    for i in range(n(10, 1) if want("hotplug") else 0):
        disk = Disk(f"hp{i}")
        a = make_env("ch-snap-cow", f"hpa{i}")
        b = make_env("ch-snap-cow", f"hpb{i}")

        def one():
            disk.create()
            a.start()
            b.start()
            t = now()
            a.hot_add(disk)
            t_add = now() - t
            rc, out = a.exec(WRITE_F1, 300)
            if rc:
                raise RuntimeError(f"A 写盘失败: {out[-300:]}")
            h1 = out.strip().split()[-1]
            if not sha_ok(h1):
                raise RuntimeError(f"A 写完没拿到校验和: {h1!r}")
            t = now()
            a.hot_remove()
            t_remove = now() - t
            t = now()
            b.hot_add(disk)
            t_add2 = now() - t
            rc, chk = b.exec(CHECK, 300)
            g1, _ = parse_check(chk)
            b.hot_remove()
            a.stop(graceful=False)
            b.stop(graceful=False)
            disk.destroy()
            return {"t_hot_add": t_add, "t_hot_remove": t_remove, "t_hot_add_other": t_add2, "f1_ok": g1 == h1}

        guard(rec, {"part": "hotplug", "kind": "ch", "rep": i}, one, [a, b])
        with contextlib.suppress(Exception):
            disk.destroy()
    if not B.ZFS:
        return
    # 3b. H23：每条命令结束时给工作盘打一个检查点（ZFS 快照）要多久——闲着、和环境里正在写的时候
    disk = Disk("snaplat")
    env = make_env("ch-snap-cow", "snaplat", disk=disk)

    def snaplat():
        disk.create()
        env.start()
        res = {}
        for label, bg in (("idle", None), ("busy", "setsid nohup dd if=/dev/urandom of=/work/bg bs=1M count=2048 conv=fsync "
                                                    ">/dev/null 2>&1 </dev/null &")):
            if bg:
                env.exec(bg, 30)
                time.sleep(1)
            xs = []
            for k in range(n(30, 3)):
                t = now()
                sh(["zfs", "snapshot", f"{disk.ds}@{label}{k}"])
                xs.append(now() - t)
            xs.sort()
            res[f"{label}_p50"] = xs[len(xs) // 2]
            res[f"{label}_max"] = xs[-1]
        env.exec("pkill dd; true", 30)
        env.stop(graceful=True)
        disk.destroy()
        return res

    if B.ZFS and want("snap-latency"):
        guard(rec, {"part": "snap-latency"}, snaplat, [env])
    with contextlib.suppress(Exception):
        disk.destroy()
    # 4. 搬到另一台机器要传多少、多快：zfs send | zfs recv（同池代替跨机，网络另算）
    for i in range(n(3, 1) if want("zfs-send") else 0):
        disk = Disk(f"zs{i}")

        def one():
            disk.create()
            m = B.host_mount(disk.dev, disk.name)
            sh(f"head -c {n(1024, 64)}M /dev/urandom > {m}/blob && sync")
            B.host_umount(m)
            sh(["zfs", "snapshot", f"{disk.ds}@s1"])
            full = int(sh(["zfs", "send", "-c", "-nvP", f"{disk.ds}@s1"]).stdout.split()[-1])
            t = now()
            sh(f"zfs send -c {disk.ds}@s1 | zfs recv -F {B.POOL}/recv/zs{i}", timeout=3600)
            t_full = now() - t
            m = B.host_mount(disk.dev, disk.name)
            sh(f"head -c {n(1024, 64)}M /dev/urandom > {m}/delta && sync")  # H8b：改动 1 GiB
            B.host_umount(m)
            sh(["zfs", "snapshot", f"{disk.ds}@s2"])
            inc = int(sh(["zfs", "send", "-c", "-nvP", "-i", "s1", f"{disk.ds}@s2"]).stdout.split()[-1])
            t = now()
            sh(f"zfs send -c -i s1 {disk.ds}@s2 | zfs recv -F {B.POOL}/recv/zs{i}", timeout=3600)
            t_inc = now() - t
            sh(["zfs", "destroy", "-r", f"{B.POOL}/recv/zs{i}"])
            disk.destroy()
            return {"full_bytes": full, "t_full": t_full, "full_MBps": full / MIB / t_full,
                    "inc_bytes": inc, "t_inc": t_inc, "inc_MBps": inc / MIB / t_inc}

        guard(rec, {"part": "zfs-send", "rep": i}, one)
    # H8b：加密的盘按密文原样传（zfs send -w），全量和改 1 GiB 的增量
    edisk = Disk("zse", flavor="enc")

    def enc_send():
        edisk.create()
        res = {}
        for step, size in (("s1", n(1024, 64)), ("s2", n(1024, 64))):
            m = B.host_mount(edisk.dev, f"zse-{step}")
            sh(f"head -c {size}M /dev/urandom > {m}/{step} && sync")
            B.host_umount(m)
            sh(["zfs", "snapshot", f"{edisk.ds}@{step}"])
        for lab, args in (("full", ["-w", f"{edisk.ds}@s1"]), ("inc", ["-w", "-i", "s1", f"{edisk.ds}@s2"])):
            size = int(sh(["zfs", "send", "-nvP", *args]).stdout.split()[-1])
            t = now()
            sh(f"zfs send {' '.join(args)} | zfs recv -F {B.POOL}/recv/zse", timeout=3600)
            res[f"{lab}_bytes"], res[f"t_{lab}"] = size, now() - t
        sh(["zfs", "destroy", "-r", f"{B.POOL}/recv/zse"])
        edisk.destroy()
        return res

    if want("zfs-send-enc"):
        guard(rec, {"part": "zfs-send-enc"}, enc_send)
    with contextlib.suppress(Exception):
        edisk.destroy()
    # 5. 压缩：真实的工作区（装好的 node_modules、编完的 redis）放进 lz4、zstd、不压缩三种数据集
    disk = Disk("comp")
    env = make_env("native", "comp", disk=disk, cpus=PIN2)

    def comp():
        disk.create()
        env.start()
        rc, out = env.exec("cd /work/webapp && npm_config_cache=/work/.npm npm ci --offline --no-audit --no-fund "
                           "--loglevel=error && npm run build >/dev/null && cd /work/redis && make -j2 >/dev/null 2>&1", 3600)
        if rc:
            raise RuntimeError(out[-400:])
        env.stop()
        m = B.host_mount(disk.dev, "comp-src")
        res = {"workspace_bytes": int(sh(["du", "-sb", m]).stdout.split()[0])}
        for algo in ("off", "lz4", "zstd"):
            ds = f"{B.POOL}/comp/{algo}"
            sh(["zfs", "create", "-o", f"compression={algo}", ds])
            mp = sh(["zfs", "get", "-H", "-o", "value", "mountpoint", ds]).stdout.strip()
            t = now()
            sh(f"tar -C {m} -cf - . | tar -C {mp} -xf - && sync", timeout=3600)
            res[f"{algo}_t_copy"] = now() - t
            p = B.zfs_props(ds, "used,logicalused,compressratio")
            res[f"{algo}_used"] = p.get("used")
            res[f"{algo}_logicalused"] = p.get("logicalused")
            res[f"{algo}_ratio"] = p.get("compressratio")
            sh(["zfs", "destroy", "-r", ds])
        B.host_umount(m)
        disk.destroy()
        return res

    if want("compression"):
        guard(rec, {"part": "compression"}, comp, [env])
    with contextlib.suppress(Exception):
        disk.destroy()


# ---------------- density ----------------

DENSITY = [("gvisor-systrap", "pages"), ("fc-snap", "pages"), ("ch-snap-cow", "pages"), ("kata-clh", "pages"),
           ("runc", "pages"), ("fc-snap", "dute"), ("ch-snap-cow", "dute"), ("fc-cold", "dute"), ("ch-cold", "dute")]


def exp_density():
    rec = Rec("density")
    cap = n(300, 3)
    reserve_kb = 3 * GIB // 1024
    tcap = 8 * 60 if not B.QUICK else 120
    for kind, shape in DENSITY:
        cleanup_all()
        drop_caches()
        time.sleep(3)
        m0 = B.meminfo()
        free0 = B.safe_free_kb(m0)
        envs, fails, t_begin = [], 0, now()
        stop_reason = "cap"
        for i in range(cap):
            # 下一台按它整个形状算（共用快照的那几种也可能把内存全写脏），再留 3 GiB 真空闲
            if B.safe_free_kb() < reserve_kb + B.SHAPES[shape][1] * 1024:
                stop_reason = "mem"
                break
            if now() - t_begin > tcap:
                stop_reason = "time"
                break
            env = make_env(kind, f"d{i}-{kind}-{shape}", shape=shape)
            t0 = now()
            try:
                env.start()
                if shape == "pages":
                    rc, out = env.exec(APP_NODISK, 120)
                    if rc:
                        raise RuntimeError(f"应用没起来: {out[-200:]}")
                envs.append(env)
                fails = 0
                rec.put(ok=True, part="step", kind=kind, shape=shape, i=i, t_start=now() - t0,
                        mem_avail_kb=B.meminfo()["MemAvailable"], safe_free_kb=B.safe_free_kb())
            except Exception as e:  # noqa: BLE001
                fails += 1
                rec.put(ok=False, part="step", kind=kind, shape=shape, i=i, err=repr(e)[:800])
                with contextlib.suppress(Exception):
                    env.stop(graceful=False)
                if fails >= 3:
                    stop_reason = "fail"
                    break
        t_fill = now() - t_begin
        time.sleep(n(30, 3))
        m1 = B.meminfo()
        k = len(envs)
        tot = {"pss_kb": 0, "rss_kb": 0, "mem_current": 0}
        for env in envs:
            hm = env.host_mem()
            tot["pss_kb"] += hm.get("pss_kb", 0)
            tot["rss_kb"] += hm.get("rss_kb", 0)
            tot["mem_current"] += hm.get("mem_current", 0) or 0
        paused = None
        if shape == "pages" and kind in ("fc-snap", "ch-snap-cow", "gvisor-systrap") and envs:
            npaused = 0
            for env in envs:
                try:
                    env.pause()
                    npaused += 1
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(n(30, 3))
            m2 = B.meminfo()
            ppss = sum(env.host_mem().get("pss_kb", 0) for env in envs)
            paused = {f"paused_per_d_{x}_kb": ((m0[x] - m2[x]) if x in ("MemAvailable", "MemFree") else (m2[x] - m0[x])) / k
                      for x in ("MemAvailable", "MemFree", "Cached", "AnonPages")}
            paused.update(paused_count=npaused, paused_per_pss_kb=ppss / k)
            for env in envs:
                with contextlib.suppress(Exception):
                    env.resume()
        t = now()
        for env in envs:
            with contextlib.suppress(Exception):
                env.stop(graceful=False)
        t_teardown = now() - t
        dk = {f"d_{x}_kb": m0[x] - m1[x] if x in ("MemAvailable", "MemFree") else m1[x] - m0[x]
              for x in ("MemAvailable", "MemFree", "Cached", "Mapped", "Shmem", "AnonPages", "PageTables",
                        "KernelStack", "Slab")}
        per = {f"per_{x}": (v / k if k else None) for x, v in dk.items()}
        rec.put(ok=k > 0, part="summary", kind=kind, shape=shape, count=k, stop_reason=stop_reason,
                t_fill=t_fill, t_teardown=t_teardown, mem_total_kb=m0["MemTotal"],
                sum_pss_kb=tot["pss_kb"], sum_rss_kb=tot["rss_kb"], sum_cg_mem=tot["mem_current"],
                per_pss_kb=tot["pss_kb"] / k if k else None, safe_free_start_kb=free0,
                est_max_count=int((free0 - reserve_kb) / (tot["pss_kb"] / k)) if k and tot["pss_kb"] else None,
                **dk, **per, **(paused or {}))
        log("密度", kind, shape, "放了", k, "台，停在", stop_reason)
    # 一台大虚拟机（4 核 8 GB）里放多少个 gVisor 沙箱（Pages 那条路），每个跑着 Node
    cleanup_all()
    host = make_env("ch-cold", "dgvm", shape="pageshost")
    try:
        host.start()
        a0 = guest_avail_kb(host)
        r0 = host.host_mem()
        k, stop_reason, t_begin = 0, "cap", now()
        for i in range(n(200, 3)):
            if (guest_avail_kb(host) or 0) < 600 * 1024:
                stop_reason = "mem"
                break
            if now() - t_begin > tcap:
                stop_reason = "time"
                break
            t0 = now()
            host.exec(f"mkdir -p /tmp/d{i} && cp /etc/os-release /tmp/d{i}/.berth-marker", 30)
            try:
                gv_call(host, f"{GV} up g{i} /tmp/d{i}")
            except Exception as e:  # noqa: BLE001
                rec.put(ok=False, part="step", kind="gvisor-in-vm", shape="pages", i=i, err=repr(e)[-1500:])
                stop_reason = "fail"
                break
            k += 1
            rec.put(ok=True, part="step", kind="gvisor-in-vm", shape="pages", i=i, t_start=now() - t0)
        time.sleep(n(30, 3))
        a1 = guest_avail_kb(host)
        r1 = host.host_mem()
        # 每个沙箱没有单独的资源上限（gv.sh 用 --ignore-cgroups），这个容量带着这个条件
        rec.put(ok=k > 0, part="summary", kind="gvisor-in-vm", shape="pages", count=k, stop_reason=stop_reason,
                guest_mem_total_kb=8192 * 1024, per_guest_kb=(a0 - a1) / k if k else None,
                vmm_rss_growth_kb=(r1.get("rss_kb", 0) - r0.get("rss_kb", 0)),
                vmm_pss_growth_kb=(r1.get("pss_kb", 0) - r0.get("pss_kb", 0)),
                per_vmm_rss_kb=(r1.get("rss_kb", 0) - r0.get("rss_kb", 0)) / k if k else None, per_sandbox_limit=False)
    except Exception as e:  # noqa: BLE001
        rec.put(ok=False, part="summary", kind="gvisor-in-vm", shape="pages", count=0, stop_reason="error",
                err=repr(e)[:800], vmm_log=host.vmm_log())
    finally:
        host.stop(graceful=False)


# ---------------- build ----------------

def wl_script(wl, vcpus):
    if wl == "npm":
        return ("cd /work/webapp && export npm_config_cache=/work/.npm npm_config_update_notifier=false && "
                "t0=$(date +%s.%N) && { npm ci --offline --no-audit --no-fund --loglevel=error > /tmp/npm.log 2>&1 "
                "|| { tail -20 /tmp/npm.log; exit 3; }; } && t1=$(date +%s.%N) && "
                "{ npm run build > /tmp/build.log 2>&1 || { tail -20 /tmp/build.log; exit 4; }; } && "
                "t2=$(date +%s.%N) && sync && echo \"T0=$t0 T1=$t1 T2=$t2\"")
    if wl == "redis":
        return (f"cd /work/redis && t0=$(date +%s.%N) && {{ make -j{vcpus} > /tmp/make.log 2>&1 || "
                "{ tail -30 /tmp/make.log; exit 3; }; } && t1=$(date +%s.%N) && sync && echo \"T0=$t0 T1=$t1\"")
    if wl == "zig":
        return ("cd /work/sqlite && export ZIG_GLOBAL_CACHE_DIR=/work/.zig-cache ZIG_LOCAL_CACHE_DIR=/work/.zig-cache && "
                "t0=$(date +%s.%N) && zig cc -O2 -c sqlite3.c -o /tmp/s.o && t1=$(date +%s.%N) && echo \"T0=$t0 T1=$t1\"")
    raise ValueError(wl)


BUILD_KINDS = [("native", 5), ("runc", 3), ("gvisor-systrap", 5), ("gvisor-kvm", 3), ("kata-clh", 5),
               ("fc-snap", 5), ("ch-snap-cow", 5),
               ("ch-snap-cow+wall", 3),  # H6：虚拟机里再套一层用户命名空间的墙（bubblewrap）
               ("ch-snap-cow@raw", 3),   # H8：这个人的盘放原生块设备上（对照 ZFS 卷）
               ("ch-snap-cow@enc", 3)]   # H8b：这个人的盘是 ZFS 原生加密的卷
WALL = ("bwrap --unshare-user --uid 0 --gid 0 --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup-try "
        "--ro-bind / / --bind /work /work --dev /dev --proc /proc --tmpfs /tmp --tmpfs /run --new-session "
        "--die-with-parent --cap-drop ALL /bin/sh -c ")


def split_kind(kind):
    """'ch-snap-cow@raw' -> ('ch-snap-cow', 'raw', False)；'ch-snap-cow+wall' -> ('ch-snap-cow', 'zvol', True)。"""
    wall = kind.endswith("+wall")
    base = kind[:-5] if wall else kind
    flavor = "zvol"
    if "@" in base:
        base, flavor = base.split("@", 1)
    return base, flavor, wall
BUILD_WLS = [("npm", 1.0), ("redis", 1.0), ("zig", 0.2)]


def exp_build():
    rec = Rec("build")
    only = set(filter(None, os.environ.get("BENCH_BUILD_KINDS", "").split(",")))
    kinds = [(k, r) for k, r in BUILD_KINDS if not only or k in only]
    maxr = max(r for _, r in kinds)
    for rep in range(n(maxr, 1)):
        for kind, reps in kinds:
            for wl, frac in BUILD_WLS:
                if rep >= n(max(1, round(reps * frac)), 1):
                    continue
                name = f"bd{rep}-{kind.replace('@', '-').replace('+', '-')}-{wl}"
                base, flavor, wall = split_kind(kind)
                if flavor == "raw" and not (B.STATE.get("raw_dev") or B.QUICK):
                    rec.put(ok=False, kind=kind, wl=wl, rep=rep, skipped=True, err="这台机器没切出原生块设备分区")
                    continue
                disk = Disk(name, flavor=flavor)
                env = make_env(base, name, shape="dute", disk=disk, cpus=PIN2)

                def one():
                    disk.create()
                    env.start()
                    u0 = env.usage()
                    script = wl_script(wl, env.vcpus)
                    if wall:
                        script = WALL + shlex.quote(script)
                    rc, out, t_host = env.timed_exec(script, 3600)
                    u1 = env.usage()
                    if rc:
                        raise RuntimeError(f"{wl} 失败 rc={rc}: {out[-600:]}")
                    ts = dict(kv.split("=") for kv in out.strip().splitlines()[-1].split())
                    t = [float(ts[k]) for k in sorted(ts)]
                    res = {"t_host": t_host, "t_total": t[-1] - t[0]}
                    if wl == "npm":
                        res.update(t_npm_ci=t[1] - t[0], t_vite_build=t[2] - t[1])
                    if "cpu_usage_usec" in u0 and "cpu_usage_usec" in u1:
                        res["host_cpu_s"] = (u1["cpu_usage_usec"] - u0["cpu_usage_usec"]) / 1e6
                    for k in ("io_rbytes", "io_wbytes"):
                        if k in u0 and k in u1:
                            res[f"host_{k}"] = u1[k] - u0[k]
                    res["mem_peak"] = u1.get("mem_peak")
                    res["pinned"] = pinned_ok(env)
                    res["pin_ok"] = bool(res["pinned"]) and all(cpu_set(x) <= cpu_set(PIN2) for x in res["pinned"])
                    env.stop(graceful=False)
                    disk.destroy()
                    return res

                guard(rec, {"kind": kind, "wl": wl, "rep": rep}, one, [env], key=("build", kind, wl))
                with contextlib.suppress(Exception):
                    disk.destroy()


def cpu_set(text):
    out = set()
    for part in (text or "").split(","):
        if part:
            a, _, b = part.partition("-")
            out |= set(range(int(a), int(b or a) + 1))
    return out


def pinned_ok(env):
    """核一下这个环境的进程是不是真的只在钉的那几个核上跑。"""
    want = set(PIN2.split(",")) if PIN2 else set()
    seen = set()
    for p in env.pids():
        with contextlib.suppress(OSError):
            for line in open(f"/proc/{p}/status"):
                if line.startswith("Cpus_allowed_list"):
                    seen.add(line.split(":")[1].strip())
    return sorted(seen)


# ---------------- reclaim ----------------

RECLAIM = [("fc-cold", {"fpr": True}), ("fc-cold", {"fpr": False}), ("ch-cold", {"fpr": True}),
           ("ch-cold", {"fpr": False}), ("kata-clh", {"reclaim": False}), ("kata-clh", {"reclaim": True}),
           ("gvisor-systrap", {}), ("runc", {})]


def mem_of(env):
    hm = env.host_mem()
    return {"rss_kb": hm.get("rss_kb"), "pss_kb": hm.get("pss_kb"), "cg_kb": (hm.get("mem_current") or 0) // 1024}


def wait_guest(env, cond, timeout=300):
    end = now() + timeout
    while now() < end:
        rc, _ = env.exec(cond, 30)
        if rc == 0:
            return
        time.sleep(0.2)
    raise TimeoutError(cond)


def exp_reclaim():
    rec = Rec("reclaim")
    A = n(1024, 256)
    for kind, opts in RECLAIM:
        for i in range(n(2, 1)):
            env = make_env(kind, f"rc{i}-{kind}", shape="dute", **opts)
            tag = {"kind": kind, **opts, "rep": i}

            def one():
                env.start()
                time.sleep(3)
                rows = [{"phase": "idle", "t": 0, **mem_of(env)}]
                env.exec(f"setsid nohup python3 /usr/local/lib/berth/alloc.py {A} 5 /tmp/alloc.done >/dev/null 2>&1 </dev/null &", 30)
                wait_guest(env, "test -f /tmp/alloc.done")
                rows.append({"phase": "held", "t": 0, **mem_of(env)})
                wait_guest(env, "! pgrep -f '[a]lloc.py' >/dev/null")
                t0 = now()
                for at in (1, 2, 5, 10, 20, 30):
                    time.sleep(max(0, t0 + at - now()))
                    rows.append({"phase": "freed", "t": at, **mem_of(env)})
                if isinstance(env, B.VMEnv) and env.opts.get("balloon", True):
                    # 气球：宿主要回 1 GiB
                    env.exec(f"setsid nohup python3 /usr/local/lib/berth/alloc.py {A} 5 /tmp/alloc2.done >/dev/null 2>&1 </dev/null &", 30)
                    wait_guest(env, "test -f /tmp/alloc2.done")
                    wait_guest(env, "! pgrep -f '[a]lloc.py' >/dev/null")
                    rows.append({"phase": "before-balloon", "t": 0, **mem_of(env)})
                    t0 = now()
                    env.balloon(A)
                    for at in (0.5, 1, 2, 5, 10, 20):
                        time.sleep(max(0, t0 + at - now()))
                        rows.append({"phase": "balloon", "t": at, **mem_of(env)})
                    env.balloon(0)
                if isinstance(env, B.VMEnv) or kind == "kata-clh":
                    # 页缓存：读 1 GiB 进客机缓存，再让客机丢掉
                    if isinstance(env, B.VMEnv):
                        env.exec(f"dd if=/dev/vda of=/dev/null bs=1M count={A} status=none; true", 300)
                    else:  # Kata：经 virtio-fs 把整个根目录读一遍（约 1 GiB）
                        env.exec("find /usr /opt -xdev -type f -size +0 -print0 | xargs -0 cat > /dev/null 2>&1; true", 600)
                    rows.append({"phase": "cached", "t": 0, **mem_of(env)})
                    env.exec("sync; echo 3 > /proc/sys/vm/drop_caches", 60)
                    t0 = now()
                    for at in (1, 2, 5, 10, 20, 30):
                        time.sleep(max(0, t0 + at - now()))
                        rows.append({"phase": "dropped", "t": at, **mem_of(env)})
                env.stop(graceful=False)
                for r in rows:
                    rec.put(ok=True, **tag, **r)
                return {"phase": "done"}

            guard(rec, tag, one, [env])


# ---------------- usage ----------------

USAGE = ["native", "runc", "gvisor-systrap", "kata-clh", "fc-cold", "ch-cold"]


def exp_usage():
    rec = Rec("usage")
    idx = 0
    for kind in USAGE:
        for i in range(n(3, 1)):
            idx += 1
            net = None
            if kind in ("fc-cold", "ch-cold"):
                net = B.tap_make(idx)
            elif kind in ("runc", "gvisor-systrap"):
                net = B.netns_make(idx)
            disk = Disk(f"us{i}-{kind}")
            env = make_env(kind, f"us{i}-{kind}", shape="dute", disk=disk, net=net, cpus=PIN2)
            tag = {"kind": kind, "rep": i}

            def one():
                disk.create()
                env.start()
                h_up0 = now()
                rc, up0 = env.exec("cut -d' ' -f1 /proc/uptime", 30)
                time.sleep(2)
                out = {}
                # 闲着 20 秒，宿主记了多少 CPU
                u0 = env.usage()
                time.sleep(n(20, 3))
                u1 = env.usage()
                out["idle_cpu_ms_per_s"] = (u1.get("cpu_usage_usec", 0) - u0.get("cpu_usage_usec", 0)) / 1000 / n(20, 3)
                # 烧一个核 10 秒
                u0 = env.usage()
                rc, o = env.exec("python3 /usr/local/lib/berth/burn.py 10", 120)
                u1 = env.usage()
                kv = dict(x.split("=") for x in o.strip().split()[-2:])
                out["guest_cpu_s"] = float(kv["cpu"])
                out["host_cpu_s"] = (u1.get("cpu_usage_usec", 0) - u0.get("cpu_usage_usec", 0)) / 1e6
                # 占 512 MiB
                m0 = mem_of(env)
                env.exec("setsid nohup python3 /usr/local/lib/berth/alloc.py 512 20 /tmp/u.done >/dev/null 2>&1 </dev/null &", 30)
                wait_guest(env, "test -f /tmp/u.done")
                time.sleep(1)
                m1 = mem_of(env)
                out.update({f"mem_{k}_delta_kb": (m1[k] or 0) - (m0[k] or 0) for k in m0})
                out["mem_expected_kb"] = 512 * 1024
                # 写 1 GiB 随机数据到这个人的盘并刷盘
                p0, u0 = disk.props(), env.usage()
                rc, o, t_w = env.timed_exec("head -c 1073741824 /dev/urandom > /work/blob && sync", 900)
                p1, u1 = disk.props(), env.usage()
                out["disk_expected"] = GIB
                out["t_write_1g"] = t_w
                for k in p0:
                    if isinstance(p0[k], int):
                        out[f"zfs_{k}_delta"] = p1[k] - p0[k]
                if "io_wbytes" in u0:
                    out["cg_io_wbytes_delta"] = u1["io_wbytes"] - u0["io_wbytes"]
                # 删掉再 trim：池里的空间回不回来
                if isinstance(env, B.VMEnv):
                    rc, o = env.exec("rm -f /work/blob && sync && fstrim -v /work", 300)
                    out["trim_by"] = "guest"
                else:
                    # 容器没有 CAP_SYS_ADMIN，trim 由宿主对这块盘的挂载点做（berth 自己做也是这样）
                    env.exec("rm -f /work/blob && sync", 300)
                    r = sh(["fstrim", "-v", env.mnt], check=False)
                    rc, o = r.returncode, r.stdout + r.stderr
                    out["trim_by"] = "host"
                out["fstrim_rc"] = rc
                out["fstrim_out"] = o.strip()[-200:]
                time.sleep(2)
                p2 = disk.props()
                for k in ("referenced", "used", "logicalreferenced"):
                    if k in p2 and k in p1:
                        out[f"zfs_{k}_after_trim_delta"] = p2[k] - p1[k]
                # 发 256 MiB 给宿主
                if net:
                    sink = B.Sink(net["host_ip"])
                    c0 = B.net_counters(net["dev"])
                    rc, o = env.exec(f"python3 /usr/local/lib/berth/send.py {net['host_ip']} 9000 {n(256, 16)}", 600)
                    got = sink.close()
                    if rc:
                        raise RuntimeError(f"往宿主发字节失败: {o[-300:]}")
                    time.sleep(1)
                    c1 = B.net_counters(net["dev"])
                    out["net_app_bytes"] = got
                    out["net_sent_reported"] = o.strip()[-60:]
                    out["net_dev_rx_bytes"] = c1["rx_bytes"] - c0["rx_bytes"]
                    out["net_dev_tx_bytes"] = c1["tx_bytes"] - c0["tx_bytes"]
                    out["net_dev_rx_packets"] = c1["rx_packets"] - c0["rx_packets"]
                # H15：醒着的时长——宿主记的 vs 环境自己的开机时长增量；内存峰值——cgroup 的 peak vs 实际占的
                rc, up1 = env.exec("cut -d' ' -f1 /proc/uptime", 30)
                with contextlib.suppress(ValueError, IndexError):
                    out["awake_host_s"] = now() - h_up0
                    out["awake_guest_s"] = float(up1.split()[-1]) - float(up0.split()[-1])
                out["mem_peak_cg"] = env.usage().get("mem_peak")
                env.stop(graceful=True)
                disk.destroy()
                return out

            guard(rec, tag, one, [env])
            with contextlib.suppress(Exception):
                disk.destroy()


# ---------------- smt ----------------

def exp_smt():
    rec = Rec("smt")
    ctl = "/sys/devices/system/cpu/smt/control"
    state0 = open(ctl).read().strip() if os.path.exists(ctl) else "没有 smt/control"
    if state0 not in ("on", "off"):
        rec.put(ok=False, err=f"这台机器不能切超线程: {state0}")
        return
    cpus0 = online_cpus()
    groups = B.STATE.get("core_groups") or []
    host_core = next((g for g in groups if 0 in g), [0])  # 0 号核连同它的兄弟线程留给宿主
    try:
        for state in ("on", "off"):
            envs, disks = [], []
            try:
                with open(ctl, "w") as f:
                    f.write(state)
                time.sleep(2)
                drop_caches()
                cpus = [c for c in online_cpus() if c not in host_core]
                if not cpus:
                    rec.put(ok=False, smt=state, skipped=True, err="除了留给宿主的核，没有别的核可用")
                    continue
                vul = {os.path.basename(p): open(p).read().strip() for p in glob.glob("/sys/devices/system/cpu/vulnerabilities/*")}
                for c in cpus:
                    d = Disk(f"smt-{state}-{c}")
                    d.create()
                    disks.append(d)
                    e = make_env("fc-cold", f"smt-{state}-{c}", shape="solo", disk=d, cpus=str(c))
                    e.start()
                    envs.append(e)
                import concurrent.futures as cf
                t0 = now()
                with cf.ThreadPoolExecutor(len(envs)) as ex:
                    futs = [ex.submit(e.timed_exec, wl_script("redis", 1), n(900, 1800)) for e in envs]
                    res = []
                    for fu in futs:
                        try:
                            res.append(fu.result())
                        except Exception as e:  # noqa: BLE001
                            res.append((-1, repr(e)[:200], None))
                wall = now() - t0
                ok = all(r[0] == 0 for r in res)
                rec.put(ok=ok, smt=state, n=len(envs), cpus=cpus, wall=wall,
                        builds_per_hour=sum(1 for r in res if r[0] == 0) * 3600 / wall, each=[r[2] for r in res],
                        vulnerabilities=vul)
            except Exception as e:  # noqa: BLE001
                rec.put(ok=False, smt=state, err=repr(e)[:800])
            finally:
                for e in envs:
                    e.stop(graceful=False)
                for d in disks:
                    with contextlib.suppress(Exception):
                        d.destroy()
    finally:
        with contextlib.suppress(OSError):
            with open(ctl, "w") as f:
                f.write(state0)
        for c in cpus0:  # 写回 on 以后，把实验前在线的 CPU 逐个确认拉回来
            p = f"/sys/devices/system/cpu/cpu{c}/online"
            if os.path.exists(p):
                with contextlib.suppress(OSError):
                    if open(p).read().strip() != "1":
                        with open(p, "w") as f:
                            f.write("1")
        rec.put(ok=sorted(online_cpus()) == sorted(cpus0), part="restore", online=online_cpus(), expected=cpus0)


def online_cpus():
    out = []
    for part in open("/sys/devices/system/cpu/online").read().strip().split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


# ---------------- netplay ----------------

RTT_PAIRS = [("runc", "runc"), ("gvisor-systrap", "gvisor-systrap"), ("kata-clh", "kata-clh"), ("fc-cold", "fc-cold"),
             ("ch-cold", "ch-cold"), ("ch-cold", "gvisor-systrap")]
GODOT_KINDS = ["native", "runc", "gvisor-systrap", "gvisor-kvm", "kata-clh", "fc-snap", "ch-snap-cow", "gvisor-in-vm"]
GODOT_RUN = ("cd {d}/godot || exit 6\n"
             "t0=$(date +%s.%N)\n"
             "{ HOME=/tmp godot --headless --path {d}/godot --script res://main.gd; echo $? > /tmp/godot.rc; } 2>&1 | "
             "while IFS= read -r l; do case \"$l\" in READY*) echo \"GR=$(date +%s.%N)\";; esac; echo \"$l\" >> /tmp/godot.log; done\n"
             "echo \"G0=$t0\"\n"
             "[ \"$(cat /tmp/godot.rc)\" = 0 ] || { tail -20 /tmp/godot.log; exit 8; }")


def godot_run(d):
    return GODOT_RUN.replace("{d}", d)


def godot_ready_s(out):
    kv = dict(x.split("=", 1) for x in out.split() if x.startswith(("G0=", "GR=")))
    if "GR" not in kv:
        raise RuntimeError(f"Godot 没打出 READY: {out[-300:]}")
    return float(kv["GR"]) - float(kv["G0"])


def net_for(kind, idx):
    if kind in ("fc-cold", "ch-cold"):
        return B.tap_make(idx, bridge=True)
    return B.netns_make(idx, bridge=True)


def parse_rtt(out):
    line = [x for x in out.strip().splitlines() if x.startswith("n=")][-1]
    return {k: float(v) for k, v in (kv.split("=") for kv in line.split())}


RTT_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guest", "rtt.py")
RULE_LOOP = ("import time\nwhile True:\n    t = time.perf_counter(); x = 0\n"
             "    while time.perf_counter() - t < 0.004:\n        x += 1\n    time.sleep(0.029)")  # 一拍约 33 毫秒、算 4 毫秒


def exp_gateway(rec, N):
    client = make_env("ch-cold", "gwc", shape="pages", net=B.tap_make(90, bridge=True))
    rule = make_env("gvisor-systrap", "gwr", shape="pages", net=B.netns_make(91, bridge=True))
    procs, busy = [], []
    try:
        client.start()
        rule.start()
        rc, out = rule.exec("setsid nohup python3 /usr/local/lib/berth/rtt.py serve2 7000 >/dev/null 2>&1 </dev/null & "
                            "i=0; while [ ! -f /tmp/rtt2.ready ]; do i=$((i+1)); [ $i -gt 600 ] && exit 9; sleep 0.05; done", 60)
        if rc:
            raise RuntimeError(f"规则那头的回声服务没起来: {out[-200:]}")
        # 宿主上的网关（转发到规则）和"到网关折返"（宿主自己回）
        procs.append(subprocess.Popen(["socat", "-b", "262144", "TCP-LISTEN:7100,bind=10.210.0.1,reuseaddr,fork",
                                       f"TCP:{rule.net['guest_ip']}:7000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        procs.append(subprocess.Popen([sys.executable, RTT_PY, "serve2", "7101"], cwd="/tmp",
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1)

        def ping(target, port, size):
            rc, out = client.exec(f"python3 /usr/local/lib/berth/rtt.py ping2 {target} {port} {N} {size}", 900)
            if rc:
                raise RuntimeError(f"来回失败（{target}:{port}，{size} 字节）: {out[-300:]}")
            return parse_rtt(out)

        for phase in ("idle", "busy"):
            if phase == "busy":
                # 同一台上几款游戏的规则同时在下（各一个 gVisor 沙箱、一拍算 4 毫秒），加一个检查容器（2 核、cgroup v2 最低权重）压满
                for k in range(3):
                    e = make_env("gvisor-systrap", f"gwb{k}", shape="pages")
                    busy.append(e)
                    e.start()
                    e.exec(f"setsid nohup python3 -c {shlex.quote(RULE_LOOP)} >/dev/null 2>&1 </dev/null &", 30)
                chk = make_env("runc", "gwchk", shape="dute")
                busy.append(chk)
                chk.start()
                with open(f"{chk.cg}/cpu.weight", "w") as fw:
                    fw.write("1")
                chk.exec("for k in 1 2; do setsid nohup python3 /usr/local/lib/berth/burn.py 3600 >/dev/null 2>&1 </dev/null & done", 30)
                time.sleep(2)
            for size in (1024, 16384, 65536):
                for rep in range(n(3, 1)):
                    guard(rec, {"part": "gateway", "path": "via-gateway", "phase": phase, "size": size, "rep": rep},
                          lambda: ping("10.210.0.1", 7100, size))
            if phase == "idle":
                for path, (tgt, port) in (("to-gateway-host", ("10.210.0.1", 7101)), ("direct-to-rule", (rule.net["guest_ip"], 7000))):
                    guard(rec, {"part": "gateway", "path": path, "phase": phase, "size": 1024, "rep": 0},
                          lambda: ping(tgt, port, 1024))
    except Exception as e:  # noqa: BLE001
        rec.put(ok=False, part="gateway", path="setup", err=repr(e)[:1500])
    finally:
        for p in procs:
            with contextlib.suppress(Exception):
                p.kill()
        for e in busy + [rule, client]:
            with contextlib.suppress(Exception):
                e.stop(graceful=False)


def exp_netplay():
    rec = Rec("netplay")
    N = n(20000, 2000)
    # 1. 同一台机器上两台环境之间一次来回（模拟 netplay 联机服务到 Pages 规则网关）
    idx = 0
    for a_kind, b_kind in RTT_PAIRS:
        for i in range(n(3, 1) if want("rtt") else 0):
            idx += 2
            a = make_env(a_kind, f"rta{i}-{a_kind}", shape="pages", net=net_for(a_kind, idx))
            b = make_env(b_kind, f"rtb{i}-{b_kind}", shape="pages", net=net_for(b_kind, idx + 1))

            def one():
                a.start()
                b.start()
                rc, out = b.exec("setsid nohup python3 /usr/local/lib/berth/rtt.py serve 7000 >/dev/null 2>&1 </dev/null & "
                                 "i=0; while [ ! -f /tmp/rtt.ready ]; do i=$((i+1)); [ $i -gt 600 ] && exit 9; sleep 0.05; done", 60)
                if rc:
                    raise RuntimeError(f"回声服务没起来: {out[-200:]}")
                res = {}
                for proto in ("tcp", "udp"):
                    rc, out = a.exec(f"python3 /usr/local/lib/berth/rtt.py ping {b.net['guest_ip']} 7000 {proto} {N} 64", 600)
                    if rc:
                        raise RuntimeError(f"{proto} 来回失败: {out[-300:]}")
                    res.update({f"{proto}_{k}": v for k, v in parse_rtt(out).items()})
                a.stop(graceful=False)
                b.stop(graceful=False)
                return res

            guard(rec, {"part": "rtt", "a": a_kind, "b": b_kind, "rep": i}, one, [a, b], key=("rtt", a_kind, b_kind))
    #    下限对照：同一个 runc 容器里走 127.0.0.1（只剩 Python 自己的开销）
    env = make_env("runc", "rtloop", shape="pages")

    def loop():
        env.start()
        env.exec("setsid nohup python3 /usr/local/lib/berth/rtt.py serve 7000 >/dev/null 2>&1 </dev/null & "
                 "while [ ! -f /tmp/rtt.ready ]; do sleep 0.05; done", 60)
        res = {}
        for proto in ("tcp", "udp"):
            rc, out = env.exec(f"python3 /usr/local/lib/berth/rtt.py ping 127.0.0.1 7000 {proto} {N} 64", 600)
            res.update({f"{proto}_{k}": v for k, v in parse_rtt(out).items()})
        env.stop(graceful=False)
        return res

    if want("rtt"):
        guard(rec, {"part": "rtt", "a": "loopback", "b": "loopback", "rep": 0}, loop, [env])
    # 1b. H19 照 netplay 2026-11-20 定的量法：netplay 一侧（一台虚拟机）带票 → 宿主上的 Pages 规则网关（这里用 socat 转发）
    #     → gVisor 沙箱自己的网络栈 → 规则回包，扣掉规则自己的计算；回包 1KB、16KB、64KB 三档；空闲、机器忙各一遍。
    #     "到网关折返"（宿主上直接回）和"不经网关直连规则"只拿来查卡在哪。一台机器上是同机近似，真实部署是同机房两台。
    if want("gateway"):
        exp_gateway(rec, n(20000, 2000))
    # 2. Godot 4.7.2 无界面版冷启动：从要一个新环境到 Godot 脚本跑到第一行；再跑一次看热的
    gkinds = [k for k in GODOT_KINDS if not os.environ.get("BENCH_GODOT_KINDS") or k in os.environ["BENCH_GODOT_KINDS"].split(",")]
    for i in range(n(10, 1) if want("godot") else 0):
        for kind in gkinds:
            disk = Disk(f"gd{i}-{kind}")
            hk = "ch-snap-cow" if kind == "gvisor-in-vm" else kind
            env = make_env(hk, f"gd{i}-{kind}", shape="dute" if kind == "gvisor-in-vm" else "pages", disk=disk)

            def one():
                disk.create()
                t0 = now()
                env.start()
                if kind == "gvisor-in-vm":
                    gv_call(env, f"{GV} start gd /work")
                    env.exec(f"cat > /work/godot-run.sh <<'EOFG'\n{godot_run('/data')}\nEOFG", 30)

                    def run():
                        try:
                            return 0, gv_call(env, f"{GV} x gd 'sh /data/godot-run.sh'", 300)
                        except RuntimeError as e:
                            return 1, str(e)
                else:
                    run = lambda: env.exec(godot_run("/work"), 300)
                t_env = now() - t0
                t = now()
                rc, out = run()
                if rc:
                    raise RuntimeError(f"Godot 没跑通 rc={rc}: {out[-500:]}")
                t_first_cmd = now() - t
                ready1 = godot_ready_s(out)
                rc, out = run()
                if rc:
                    raise RuntimeError(f"Godot 第二次没跑通 rc={rc}: {out[-500:]}")
                ready2 = godot_ready_s(out)
                env.stop(graceful=False)
                disk.destroy()
                # 冷启动 = 起新环境（宿主钟）+ Godot 从起进程到打出 READY（环境里的钟）；整条命令连退出也记下
                return {"t_env": t_env, "t_godot_first": ready1, "t_cold_total": t_env + ready1,
                        "t_godot_first_cmd": t_first_cmd, "t_godot_warm": ready2}

            guard(rec, {"part": "godot", "kind": kind, "rep": i}, one, [env], key=("godot", kind))
            with contextlib.suppress(Exception):
                disk.destroy()


# ---- 待验假设新加的实验（H6、H22、H24、H25、H26）在 exp_hyp.py；这里只挂上 ----
try:
    import exp_hyp  # noqa: E402
    HYP_EXPS = exp_hyp.EXPS
except ImportError:
    HYP_EXPS = {}


EXPS = {"env": exp_env, "probe": exp_probe, "snapshots": exp_snapshots, "boot": exp_boot, "disk": exp_disk, "density": exp_density,
        "build": exp_build, "reclaim": exp_reclaim, "usage": exp_usage, "smt": exp_smt, "netplay": exp_netplay,
        "cleanup": cleanup_all, **HYP_EXPS}


def main():
    resource.setrlimit(resource.RLIMIT_NOFILE, (1048576, 1048576))
    # 实验进程自己不设防 OOM：设了会被虚拟机、容器这些子进程继承（冷读 2026-11-20）；
    # 密度按"真空闲 + 下一台整个形状 + 3 GiB"停，sshd 和 testbox 的代理在装机时护住
    os.makedirs("/run/vm", exist_ok=True)
    for d in (B.VMS, B.MNT, B.BUNDLES, B.RESULTS):
        os.makedirs(d, exist_ok=True)
    names = sys.argv[1:] or ["env"]
    for name in names:
        if name not in EXPS:
            sys.exit(f"不认识的实验 {name}；可选：{' '.join(EXPS)}")
    for name in names:
        log("==== 开始", name, "（快跑）" if B.QUICK else "")
        t = now()
        if name not in ("env", "cleanup"):
            cleanup_all()
        try:
            EXPS[name]()
        finally:
            if name not in ("env",):
                cleanup_all()
        log("==== 做完", name, f"{now() - t:.0f} 秒")


if __name__ == "__main__":
    main()
