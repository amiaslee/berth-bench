"""berth 裸金属实验的公共库（只用 Python 标准库）。

管这几样：计时和记录、宿主读数（内存、cgroup、盘、网卡）、这个人的盘（zvol 克隆）、
七种环境的驱动：native、runc、gVisor（systrap / KVM 平台）、Kata（Cloud Hypervisor 后端）、
Firecracker、Cloud Hypervisor。虚拟机经 vsock 52 口上的 socat 跑命令（见 guest/berth-init）。

所有计时都用宿主的单调时钟，起点是"产品发出请求"那一刻（起进程、发接口之前），
终点是"命令真能跑"（客机里 true 跑完回来）或"这个人的盘能读"（标记文件读出来）。
"""
import base64
import contextlib
import glob
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = os.environ.get("BENCH_ROOT", "/opt/bench")
SRV = os.environ.get("BENCH_SRV", "/srv/bench")
RESULTS = os.environ.get("BENCH_RESULTS", f"{SRV}/results")
QUICK = os.environ.get("BENCH_QUICK", "0") == "1"
BIN = f"{ROOT}/bin"
VMNS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "vmns"))
KERNEL = f"{ROOT}/guest/vmlinux"
BASE_IMG = f"{ROOT}/images/base.ext4"
PLACEHOLDER = f"{ROOT}/images/placeholder.img"
ROOTFS = f"{ROOT}/rootfs"
SNAP = f"{ROOT}/snap"
VMS = f"{SRV}/vms"
MNT = f"{SRV}/mnt"
BUNDLES = f"{SRV}/bundles"
CG = "/sys/fs/cgroup/bench"
STATE = json.load(open(f"{ROOT}/state.json")) if os.path.exists(f"{ROOT}/state.json") else {}
POOL = STATE.get("pool", "bench")
ZFS = STATE.get("storage") == "zfs"
RUNSC_ROOT = "/run/bench-runsc"
RUNC_ROOT = "/run/bench-runc"
CTR_NS = "bench"
# 这一次运行的编号：补跑时新旧样本写在同一个文件里，报告只取最新一次（冷读 2026-11-20）
RUN_ID = os.environ.get("BENCH_RUN") or time.strftime("%Y%m%dT%H%M%S")

# 两种形状：dute 的个人/临时环境 2 核 2 GB；Pages 作品 1 核 512 MB（作品 256 MB 加沙箱自己的）
SHAPES = {"dute": (2, 2048), "pages": (1, 512), "pageshost": (4, 8192), "solo": (1, 1536)}
GUEST_ARGS = ("console=ttyS0 reboot=k panic=1 quiet loglevel=1 init=/sbin/berth-init "
              "i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd")
GUEST_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


# ---------- 计时、日志、记录 ----------

def now():
    return time.monotonic()


_logf = None


def log(*a):
    global _logf
    msg = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(msg, file=sys.stderr, flush=True)
    os.makedirs(RESULTS, exist_ok=True)
    if _logf is None:
        _logf = open(f"{RESULTS}/log.txt", "a")
    _logf.write(msg + "\n")
    _logf.flush()


class Rec:
    """一个实验一个 JSONL 文件，一行一次测量；失败的也记（ok=false），不从样本里删。"""

    def __init__(self, exp):
        os.makedirs(RESULTS, exist_ok=True)
        self.exp = exp
        self.f = open(f"{RESULTS}/{exp}.jsonl", "a")

    def put(self, **kw):
        row = {"exp": self.exp, "run": RUN_ID, "quick": QUICK, "ts": round(time.time(), 3), **kw}
        for k, v in list(row.items()):
            if isinstance(v, float):
                row[k] = round(v, 6)
        self.f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.f.flush()
        return row


def reset_oom():
    """给子进程用：实验进程自己防 OOM（-1000），虚拟机管理程序、容器这些子进程要恢复普通分数，
    真到全机 OOM 时它们才该是被挑的（冷读 2026-11-20：oom_score_adj 会被子进程继承）。"""
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("0")
    except OSError:
        pass


def sh(cmd, check=True, timeout=900, **kw):
    if isinstance(cmd, str):
        cmd = ["/bin/sh", "-c", cmd]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)[:200]} 退出 {r.returncode}: {(r.stderr or r.stdout)[-800:]}")
    return r


def wait_path(p, timeout, step=0.001):
    end = now() + timeout
    while now() < end:
        if os.path.exists(p):
            return
        time.sleep(step)
    raise TimeoutError(f"等不到 {p}")


# ---------- 宿主读数 ----------

def meminfo():
    d = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        d[k] = int(v.split()[0])
    return d  # kB


def smaps(pid):
    d = {}
    try:
        for line in open(f"/proc/{pid}/smaps_rollup"):
            p = line.split()
            if len(p) >= 3 and p[0].endswith(":"):
                d[p[0][:-1]] = int(p[1])
    except OSError:
        pass
    return d


def safe_free_kb(m=None):
    """宿主真能再给出去的内存：空闲页 + 没被映射的页缓存。共用快照的内存文件被虚拟机映射着，
    gVisor 的内存在 shmem 里，都不算（冷读 2026-11-20：MemAvailable 会把它们当成可用）。"""
    m = m or meminfo()
    # 文件页 LRU 不含 shmem；Mapped 里也含映射着的 shmem，扣下来偏保守（宁可早停）
    return m["MemFree"] + max(0, m.get("Active(file)", 0) + m.get("Inactive(file)", 0) - m.get("Mapped", 0))


def protect_from_oom(pid="self"):
    with contextlib.suppress(OSError):
        with open(f"/proc/{pid}/oom_score_adj", "w") as f:
            f.write("-1000")


def pss_rss(pids):
    pss = rss = 0
    for p in pids:
        s = smaps(p)
        pss += s.get("Pss", 0)
        rss += s.get("Rss", 0)
    return {"pss_kb": pss, "rss_kb": rss, "nproc": len(pids)}


def cg_procs(cg):
    out = []
    for f in glob.glob(f"{cg}/**/cgroup.procs", recursive=True) + [f"{cg}/cgroup.procs"]:
        with contextlib.suppress(OSError):
            out += [int(x) for x in open(f).read().split()]
    return sorted(set(out))


def cg_stat(cg):
    r = {}
    if not cg or not os.path.isdir(cg):
        return r

    def rd(f):
        try:
            return open(f"{cg}/{f}").read()
        except OSError:
            return None

    for f, k in (("memory.current", "mem_current"), ("memory.peak", "mem_peak")):
        v = rd(f)
        if v and v.strip().isdigit():
            r[k] = int(v)
    v = rd("memory.stat")
    if v:
        for line in v.splitlines():
            k, val = line.split()
            if k in ("anon", "file", "shmem", "kernel", "pagetables", "sock", "file_mapped"):
                r["mem_" + k] = int(val)
    v = rd("cpu.stat")
    if v:
        for line in v.splitlines():
            k, val = line.split()
            if k in ("usage_usec", "user_usec", "system_usec", "nr_throttled", "throttled_usec"):
                r["cpu_" + k] = int(val)
    v = rd("io.stat")
    if v is not None:
        rb = wb = 0
        for line in v.splitlines():
            for kv in line.split()[1:]:
                k, _, val = kv.partition("=")
                if k == "rbytes":
                    rb += int(val)
                elif k == "wbytes":
                    wb += int(val)
        r["io_rbytes"], r["io_wbytes"] = rb, wb
    return r


def cg_make(name, cpus=None, mem_max=None):
    path = f"{CG}/{name}"
    os.makedirs(path, exist_ok=True)
    if cpus:
        with open(f"{path}/cpuset.cpus", "w") as f:
            f.write(cpus)
    if mem_max:
        with open(f"{path}/memory.max", "w") as f:
            f.write(str(mem_max))
    return path


def cg_remove(path):
    if not path:
        return
    for _ in range(200):
        if not os.path.isdir(path):
            return
        for p in cg_procs(path):
            with contextlib.suppress(OSError):
                os.kill(p, signal.SIGKILL)
        for sub in sorted(glob.glob(f"{path}/**/", recursive=True), key=len, reverse=True):
            with contextlib.suppress(OSError):
                os.rmdir(sub)
        with contextlib.suppress(OSError):
            os.rmdir(path)
        time.sleep(0.05)
    log("cgroup 删不掉:", path)


def join_cgroup_fn(cg):
    def pre():
        reset_oom()
        os.setsid()
        with open(f"{cg}/cgroup.procs", "w") as f:
            f.write("0")
    return pre


def net_counters(dev):
    b = f"/sys/class/net/{dev}/statistics"
    return {k: int(open(f"{b}/{k}").read()) for k in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets")}


def zfs_props(ds, props="written,used,referenced,logicalreferenced,logicalused,compressratio"):
    r = sh(["zfs", "get", "-Hp", "-o", "property,value", props, ds], check=False)
    out = {}
    for line in r.stdout.splitlines():
        k, v = line.split("\t")
        try:
            out[k] = float(v.rstrip("x")) if k == "compressratio" else int(v)
        except ValueError:
            out[k] = v
    return out


# ---------- 这个人的盘 ----------

class Disk:
    """从模板克隆一份 10G 的盘（里面已有编译测试的源码和 npm 缓存）。

    ZFS：zvol 克隆，秒级、只占改过的块；没有 ZFS（只在干跑时）：稀疏文件拷贝。
    """

    def __init__(self, name, flavor="zvol"):
        """flavor：zvol（默认，ZFS 卷克隆）；enc（ZFS 原生加密的卷，H8b）；raw（原生块设备或文件，H8 的对照，只有一份、一次一个用）。"""
        self.name = name
        self.flavor = flavor if ZFS else "file"
        if self.flavor == "raw":
            self.ds = None
            self.dev = STATE.get("raw_dev") or f"{ROOT}/images/raw.img"
        elif self.flavor == "enc":
            self.ds = f"{POOL}/enc/users/{name}"
            self.dev = f"/dev/zvol/{self.ds}"
        elif self.flavor == "zvol":
            self.ds = f"{POOL}/users/{name}"
            self.dev = f"/dev/zvol/{self.ds}"
        else:
            self.ds = None
            self.dev = f"{ROOT}/users/{name}.img"

    def create(self):
        t = now()
        if self.flavor == "raw":
            # 把模板盘的文件系统（只拷用到的块）铺到原生块设备上
            if not self.dev.startswith("/dev/") and not os.path.exists(self.dev):
                with open(self.dev, "wb") as f:
                    f.truncate(10 << 30)
            if self.dev.startswith("/dev/"):
                sh(["blkdiscard", "-f", self.dev], check=False, timeout=600)
            else:
                os.unlink(self.dev)
                with open(self.dev, "wb") as f:
                    f.truncate(10 << 30)
            sh(["e2image", "-ra", f"/dev/zvol/{POOL}/tmpl/work", self.dev], timeout=600)
            sh("sync; echo 3 > /proc/sys/vm/drop_caches", check=False)
        elif self.flavor == "enc":
            sh(["zfs", "clone", "-o", "volmode=dev", f"{POOL}/enc/tmpl@v1", self.ds])
            wait_path(self.dev, 30)
        elif self.ds:
            sh(["zfs", "clone", "-o", "volmode=dev", f"{POOL}/tmpl/work@v1", self.ds])
            wait_path(self.dev, 30)
        else:
            os.makedirs(f"{ROOT}/users", exist_ok=True)
            sh(["cp", "--sparse=always", f"{ROOT}/images/work.img", self.dev])
        return now() - t

    def destroy(self):
        if self.flavor == "raw":
            return
        if not self.ds:
            with contextlib.suppress(OSError):
                os.unlink(self.dev)
            return
        last = ""
        for _ in range(100):
            r = sh(["zfs", "destroy", "-r", self.ds], check=False)
            if r.returncode == 0 or "does not exist" in r.stderr:
                return
            last = r.stderr
            time.sleep(0.2)
        raise RuntimeError(f"盘删不掉 {self.ds}: {last}")

    def props(self):
        if self.ds:
            # 先让 ZFS 把这一轮事务组落盘，不然刚 sync 完读到的写入量只有一部分（真机 2026-11-21：1 GiB 只记到 0.56）
            sh(["zpool", "sync", POOL], check=False, timeout=120)
            return zfs_props(self.ds)
        return {"file_bytes": os.stat(self.dev).st_blocks * 512} if not self.dev.startswith("/dev/") else {}


def host_mount(dev, name):
    m = f"{MNT}/{name}"
    os.makedirs(m, exist_ok=True)
    opts = "noatime" if ZFS else "noatime,loop"
    sh(["mount", "-o", opts, dev, m])
    return m


def host_umount(m):
    if not m:
        return
    if os.path.ismount(m):
        sh(["umount", m], check=False)
    with contextlib.suppress(OSError):
        os.rmdir(m)


# ---------- vsock 和接口 ----------

class GuestErr(Exception):
    pass


def _readline(s, limit=512):
    b = b""
    while not b.endswith(b"\n"):
        c = s.recv(1)
        if not c:
            raise ConnectionResetError("vsock 握手没回")
        b += c
        if len(b) > limit:
            break
    return b


def vsock_exec(uds, script, timeout=1800, port=52):
    """经 Firecracker / Cloud Hypervisor 的混合 vsock 在客机里跑一段 sh 脚本，返回 (退出码, 输出)。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    out = bytearray()
    try:
        s.connect(uds)
        s.sendall(f"CONNECT {port}\n".encode())
        if not _readline(s).startswith(b"OK"):
            raise ConnectionResetError("CONNECT 没回 OK")
        b64 = base64.b64encode(script.encode()).decode()
        # 命令放进自己的会话里跑（setsid -w 等它跑完、交回它的退出码）：虚拟机里起 gVisor 时，runsc 会连带
        # 杀掉调用它的那一组进程，经 vsock 发命令的这个 shell 就没机会交回退出码（干跑 2026-11-20 撞上）
        s.sendall(f"exec 2>&1; echo {b64} | base64 -d > /tmp/.c$$ && setsid -w sh /tmp/.c$$; echo __RC=$?; exit\n".encode())
        while True:
            c = s.recv(65536)
            if not c:
                break
            out += c
    finally:
        s.close()
    text = out.decode(errors="replace")
    m = re.search(r"__RC=(\d+)\s*$", text)
    if not m:
        raise GuestErr(f"命令没跑完（连接断了）: {text[-400:]!r}")
    return int(m.group(1)), text[:m.start()]


def wait_vsock_ready(uds, timeout=60, proc=None):
    end = now() + timeout
    last = None
    while now() < end:
        if proc is not None and proc.poll() is not None:
            raise GuestErr(f"虚拟机管理程序退出了（{proc.returncode}）")
        try:
            rc, _ = vsock_exec(uds, "true", timeout=5)
            if rc == 0:
                return
        except (OSError, GuestErr) as e:
            last = e
        time.sleep(0.002)
    raise TimeoutError(f"客机 {timeout}s 没就绪: {last!r}")


class _UHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._p = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._p)
        self.sock = s


def api(sock, method, url, body=None, timeout=300):
    data = json.dumps(body) if body is not None else None
    end = now() + 5
    while True:
        c = _UHTTP(sock, timeout)
        try:
            c.request(method, url, body=data, headers={"Content-Type": "application/json", "Accept": "application/json"})
            r = c.getresponse()
            raw = r.read()
            break
        except (ConnectionRefusedError, FileNotFoundError):
            # socket 文件已经在、管理程序还没开始听（干跑 2026-11-20 撞上）；只在最开始的几秒里重试
            if now() > end:
                raise
            time.sleep(0.002)
        finally:
            c.close()
    if r.status >= 300:
        raise RuntimeError(f"{method} {url} -> {r.status}: {raw[:600]!r}")
    try:
        return json.loads(raw) if raw.strip() else None
    except ValueError:
        return raw.decode(errors="replace")


# ---------- 网络（只有用量实验要） ----------

BRIDGE = "bbr0"


def bridge_up():
    """同一台机器上的环境互相连：一座网桥 10.210.0.1/24（延迟实验用）。"""
    sh(f"ip link show {BRIDGE} >/dev/null 2>&1 || {{ ip link add {BRIDGE} type bridge && "
       f"ip addr add 10.210.0.1/24 dev {BRIDGE} && ip link set {BRIDGE} up; }}")
    # 装了 Docker 的机器上 br_netfilter 会让网桥上的包过 iptables 的 FORWARD（默认 DROP），关掉
    sh("sysctl -qw net.bridge.bridge-nf-call-iptables=0 net.bridge.bridge-nf-call-ip6tables=0 2>/dev/null; true", check=False)


def tap_make(idx, bridge=False):
    tap = f"btap{idx}"
    sh(f"ip link del {tap} 2>/dev/null; ip tuntap add {tap} mode tap")
    if bridge:
        bridge_up()
        sh(f"ip link set {tap} master {BRIDGE} && ip link set {tap} up")
        gip, host = f"10.210.0.{10 + idx}", "10.210.0.1"
        cidr = f"{gip}/24"
    else:
        sh(f"ip addr add 10.200.{idx}.1/30 dev {tap} && ip link set {tap} up")
        gip, host = f"10.200.{idx}.2", f"10.200.{idx}.1"
        cidr = f"{gip}/30"
    return {"dev": tap, "tap": tap, "host_ip": host, "guest_ip": gip, "cidr": cidr,
            "mac": f"06:00:0a:c8:{idx:02x}:02"}


def netns_make(idx, bridge=False):
    ns, vb, vc = f"bns{idx}", f"bvb{idx}", f"bvc{idx}"
    if bridge:
        bridge_up()
        gip, host, plen = f"10.210.0.{10 + idx}", "10.210.0.1", 24
        hostside = f"ip link set {vb} master {BRIDGE} && ip link set {vb} up"
    else:
        gip, host, plen = f"10.201.{idx}.2", f"10.201.{idx}.1", 30
        hostside = f"ip addr add {host}/30 dev {vb} && ip link set {vb} up"
    sh(f"ip netns del {ns} 2>/dev/null; ip link del {vb} 2>/dev/null; ip netns add {ns} && "
       f"ip link add {vb} type veth peer name {vc} && ip link set {vc} netns {ns} && {hostside} && "
       f"ip netns exec {ns} ip addr add {gip}/{plen} dev {vc} && "
       f"ip netns exec {ns} ip link set {vc} up && ip netns exec {ns} ip link set lo up")
    return {"dev": vb, "ns": ns, "nspath": f"/var/run/netns/{ns}", "host_ip": host, "guest_ip": gip,
            "cidr": f"{gip}/{plen}"}


def ns_on_bridge(name, last_octet, routes=()):
    """在网桥上开一个独立的网络空间（地址 10.210.0.<last_octet>/24），给收字节、网关、来源服务用。"""
    bridge_up()
    vb, vc = f"bx{last_octet}", f"by{last_octet}"
    ip = f"10.210.0.{last_octet}"
    sh(f"ip netns del {name} 2>/dev/null; ip link del {vb} 2>/dev/null; ip netns add {name} && "
       f"ip link add {vb} type veth peer name {vc} && ip link set {vc} netns {name} && "
       f"ip link set {vb} master {BRIDGE} && ip link set {vb} up && "
       f"ip -n {name} addr add {ip}/24 dev {vc} && ip -n {name} link set {vc} up && ip -n {name} link set lo up")
    for r in routes:
        sh(f"ip -n {name} route add {r}")
    return {"ns": name, "ip": ip, "dev": vb}


def ns_drop(ns):
    if ns:
        sh(f"ip netns del {ns['ns']} 2>/dev/null; ip link del {ns['dev']} 2>/dev/null; true", check=False)


SINK_CODE = ("import socket,sys\ns=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
             "s.bind((sys.argv[1], int(sys.argv[2]))); s.listen(1); s.settimeout(120)\nprint('R', flush=True)\n"
             "c,_=s.accept(); c.settimeout(300); n=0\nwhile True:\n    b=c.recv(1<<20)\n    if not b: break\n    n+=len(b)\n"
             "c.close(); print(n, flush=True)")


class NsSink:
    """在网桥上的网络空间里收字节，数应用层收到多少（替换原来开在宿主上的 Sink）。"""

    def __init__(self, ns, port=9000):
        self.p = subprocess.Popen(["ip", "netns", "exec", ns["ns"], sys.executable, "-c", SINK_CODE, ns["ip"], str(port)],
                                  stdout=subprocess.PIPE, text=True)
        if self.p.stdout.readline().strip() != "R":
            raise RuntimeError("收字节的服务没起来")

    def close(self):
        try:
            out, _ = self.p.communicate(timeout=300)
            return int(out.strip().split()[-1])
        except Exception:  # noqa: BLE001
            self.p.kill()
            return None


def net_drop(net):
    if not net:
        return
    sh(f"ip link del {net['dev']} 2>/dev/null; true", check=False)
    if net.get("ns"):
        sh(f"ip netns del {net['ns']} 2>/dev/null; true", check=False)


class Sink:
    """宿主上收字节的 TCP 口：数应用层收到多少字节，收完关连接。"""

    def __init__(self, ip, port=9000):
        self.s = socket.socket()
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.bind((ip, port))
        self.s.listen(1)
        self.got = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        self.s.settimeout(60)
        try:
            c, _ = self.s.accept()
        except OSError:
            return
        c.settimeout(300)
        n = 0
        while True:
            b = c.recv(1 << 20)
            if not b:
                break
            n += len(b)
        c.close()
        self.got = n

    def close(self):
        self.t.join(120)
        self.s.close()
        return self.got


# ---------- 环境 ----------

class Env:
    """一个环境：start() 起来并（有盘时）挂上这个人的盘；exec() 跑命令；stop() 扔掉。"""
    kind = "?"

    def __init__(self, name, shape="dute", disk=None, net=None, **opts):
        self.name = name
        self.shape = shape
        self.vcpus, self.mem = SHAPES[shape]
        self.disk = disk
        self.net = net
        self.opts = opts
        self.cg = None
        self.t = {}

    def cpus(self):
        return self.opts.get("cpus")

    def usage(self):
        return cg_stat(self.cg)

    def host_mem(self):
        return {**pss_rss(self.pids()), **{k: v for k, v in cg_stat(self.cg).items() if k.startswith("mem_")}}

    def pids(self):
        return cg_procs(self.cg) if self.cg else []

    def timed_exec(self, script, timeout=1800):
        t = now()
        rc, out = self.exec(script, timeout)
        return rc, out, now() - t


class VMEnv(Env):
    def __init__(self, name, **kw):
        super().__init__(name, **kw)
        self.dir = f"{VMS}/{name}"
        self.uds = f"{self.dir}/v.sock"
        self.api_sock = f"{self.dir}/api.sock"
        self.proc = None

    def _prep_dir(self):
        # 槽位换盘：快照里"这个人的盘"那个槽位，恢复前指到这个人的盘；没带盘、或热插那一路，
        # 放一块这台自己的空占位盘（稀疏文件、跟用户盘一样大）。不能几台共用一个占位文件：
        # Cloud Hypervisor 会给可写盘加独占锁，第二台就打不开（干跑 2026-11-20 撞上）
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir)
        if self.disk is not None and self.opts.get("attach", "slot") == "slot":
            os.symlink(self.disk.dev, f"{self.dir}/user.img")
        else:
            with open(f"{self.dir}/user.img", "wb") as f:
                f.truncate(os.path.getsize(PLACEHOLDER) if os.path.exists(PLACEHOLDER) else 10 << 30)

    def _spawn(self, argv):
        self.cg = cg_make(self.name, cpus=self.cpus())
        logf = open(f"{self.dir}/vmm.log", "w")
        self.proc = subprocess.Popen([VMNS, self.dir, self.cg, *argv], stdin=subprocess.DEVNULL, stdout=logf,
                                     stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        logf.close()

    def exec(self, script, timeout=1800):
        return vsock_exec(self.uds, script, timeout)

    def mount_user(self, dev="/dev/vdb", wait=False, path="/work"):
        s = ""
        if wait:
            s += f"i=0; while [ ! -b {dev} ]; do i=$((i+1)); [ $i -gt 20000 ] && exit 9; sleep 0.001; done\n"
        s += f"mkdir -p {path} && mount -o noatime {dev} {path} && cat {path}/.berth-marker >/dev/null"
        rc, out = self.exec(s, 60)
        if rc:
            raise GuestErr(f"挂盘失败 rc={rc}: {out[-300:]}")

    def start(self):
        self._prep_dir()
        t0 = now()
        self._boot()
        wait_vsock_ready(self.uds, 120, self.proc)
        self.t["t_ready"] = now() - t0
        if self.disk is not None:
            self._attach_user()
            self.t["t_usable"] = now() - t0
        return self.t

    def _attach_user(self):
        self.mount_user("/dev/vdb")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def vmm_self_kb(self):
        """虚拟机管理程序自己的常驻内存（H1）：smaps 里去掉客机内存那一大块（或几块）映射以后的 Rss 之和。"""
        if not self.alive():
            return None
        guest_kb = self.mem * 1024
        total = cur_size = 0
        try:
            for line in open(f"/proc/{self.proc.pid}/smaps"):
                p = line.split()
                if len(p) >= 5 and "-" in p[0] and ":" not in p[0]:
                    a, b = p[0].split("-")
                    cur_size = (int(b, 16) - int(a, 16)) // 1024
                elif p and p[0] == "Rss:" and cur_size < guest_kb // 4:
                    total += int(p[1])
        except OSError:
            return None
        return total

    def host_mem(self):
        return {**super().host_mem(), "vmm_self_kb": self.vmm_self_kb()}

    def pids(self):
        return [self.proc.pid] if self.alive() else []

    def vmm_log(self):
        out = ""
        for f in ("vmm.log", "serial.log"):
            with contextlib.suppress(OSError):
                out += f"== {f}\n" + open(f"{self.dir}/{f}", errors="replace").read()[-1500:]
        return out

    def stop(self, graceful=True):
        r = {}
        t0 = now()
        if graceful and self.disk is not None and self.alive():
            with contextlib.suppress(Exception):
                self.exec("sync; umount /work", 120)
            r["t_umount"] = now() - t0
        if self.proc is not None:
            with contextlib.suppress(OSError):
                self.proc.kill()
            with contextlib.suppress(Exception):
                self.proc.wait(20)
        r["t_stop"] = now() - t0
        cg_remove(self.cg)
        net_drop(self.net)
        shutil.rmtree(self.dir, ignore_errors=True)
        return r


class FC(VMEnv):
    kind = "fc"

    def _config(self):
        args = GUEST_ARGS + " pci=off"
        if self.net:
            args += f" berth.ip={self.net['cidr']}"
        cfg = {
            "boot-source": {"kernel_image_path": KERNEL, "boot_args": args},
            "drives": [
                {"drive_id": "rootfs", "path_on_host": BASE_IMG, "is_root_device": True, "is_read_only": True},
                # 这个人的盘照生产配置：Writeback 才向客机广告 FLUSH，客机 fsync 才落到宿主；discard 让删掉的块还给池
                # （Firecracker 默认 Unsafe、不开 discard，冷读 2026-11-20 指出）
                {"drive_id": "user", "path_on_host": "/run/vm/user.img", "is_root_device": False, "is_read_only": False,
                 "cache_type": "Writeback", "discard": True},
            ],
            "machine-config": {"vcpu_count": self.vcpus, "mem_size_mib": self.mem},
            "vsock": {"guest_cid": 3, "uds_path": "/run/vm/v.sock"},
        }
        if self.opts.get("balloon", True):
            cfg["balloon"] = {"amount_mib": 0, "deflate_on_oom": True, "stats_polling_interval_s": 0,
                              "free_page_reporting": bool(self.opts.get("fpr", True))}
        if self.net:
            cfg["network-interfaces"] = [{"iface_id": "eth0", "host_dev_name": self.net["tap"],
                                          "guest_mac": self.net["mac"]}]
        return cfg

    def _boot(self):
        if self.opts.get("mode", "cold") == "cold":
            with open(f"{self.dir}/config.json", "w") as f:
                json.dump(self._config(), f)
            self._spawn([f"{BIN}/firecracker", "--api-sock", "/run/vm/api.sock", "--config-file", "/run/vm/config.json"])
        else:
            snap = self.opts.get("snap") or f"{SNAP}/fc-{self.shape}"
            if not os.path.exists(f"{snap}/vmstate"):
                raise FileNotFoundError(f"没有快照 {snap}")
            self._spawn([f"{BIN}/firecracker", "--api-sock", "/run/vm/api.sock"])
            wait_path(self.api_sock, 10)
            api(self.api_sock, "PUT", "/snapshot/load", {
                "snapshot_path": f"{snap}/vmstate",
                "mem_backend": {"backend_type": "File", "backend_path": f"{snap}/mem"},
                "resume_vm": True})

    def pause(self):
        api(self.api_sock, "PATCH", "/vm", {"state": "Paused"})

    def resume(self):
        api(self.api_sock, "PATCH", "/vm", {"state": "Resumed"})

    def snapshot(self, out):
        os.makedirs(out, exist_ok=True)
        self.pause()
        api(self.api_sock, "PUT", "/snapshot/create",
            {"snapshot_type": "Full", "snapshot_path": f"{out}/vmstate", "mem_file_path": f"{out}/mem"})

    def balloon(self, mib):
        api(self.api_sock, "PATCH", "/balloon", {"amount_mib": int(mib)})


class CH(VMEnv):
    kind = "ch"

    def _argv_cold(self):
        args = GUEST_ARGS + " root=/dev/vda ro"
        if self.net:
            args += f" berth.ip={self.net['cidr']}"
        a = [f"{BIN}/cloud-hypervisor", "--api-socket", "path=/run/vm/api.sock",
             "--kernel", KERNEL, "--cmdline", args,
             "--cpus", f"boot={self.vcpus}", "--memory", f"size={self.mem}M",
             "--disk", f"path={BASE_IMG},readonly=on,image_type=raw", "path=/run/vm/user.img,image_type=raw",
             "--vsock", "cid=3,socket=/run/vm/v.sock",
             "--serial", "file=/run/vm/serial.log", "--console", "off"]
        if self.opts.get("balloon", True):
            a += ["--balloon", "size=0,deflate_on_oom=on" + (",free_page_reporting=on" if self.opts.get("fpr", True) else "")]
        if self.net:
            a += ["--net", f"tap={self.net['tap']},mac={self.net['mac']}"]
        return a

    def _boot(self):
        mode = self.opts.get("mode", "cold")  # cold | copy | copyonwrite | ondemand
        if mode == "cold":
            self._spawn(self._argv_cold())
        else:
            snap = self.opts.get("snap") or f"{SNAP}/ch-{self.shape}"
            if not os.path.isdir(snap) or not os.listdir(snap):
                raise FileNotFoundError(f"没有快照 {snap}")
            src = f"source_url=file://{snap}" + ("" if mode == "copy" else f",memory_restore_mode={mode}") + ",resume=true"
            self._spawn([f"{BIN}/cloud-hypervisor", "--api-socket", "path=/run/vm/api.sock", "--restore", src])

    def _attach_user(self):
        if self.opts.get("attach", "slot") == "hotplug":
            self.hot_add(self.disk)
        else:
            self.mount_user("/dev/vdb")

    def hot_add(self, disk, path="/work"):
        rc, before = self.exec("ls /sys/block", 30)
        before = set(before.split())
        r = api(self.api_sock, "PUT", "/api/v1/vm.add-disk", {"path": disk.dev, "image_type": "Raw"})
        self.hot_id = r.get("id") if isinstance(r, dict) else None
        self.hot_path = path
        # 等客机里多出一块 vd* 盘（热拔过的旧名字可能还占着，不假定新盘叫 vdc）
        known = " " + " ".join(sorted(before)) + " "
        script = (f'B="{known}"; i=0\n'
                  'while :; do\n'
                  '  for d in $(ls /sys/block); do\n'
                  '    case "$B" in *" $d "*) ;; *) case $d in vd*) echo $d; exit 0;; esac;; esac\n'
                  '  done\n'
                  '  i=$((i+1)); [ $i -gt 20000 ] && exit 9; sleep 0.001\n'
                  'done')
        rc, out = self.exec(script, 60)
        if rc:
            raise GuestErr("热插以后客机里没出现新盘")
        self.hot_dev = "/dev/" + out.split()[-1]
        self.mount_user(self.hot_dev, path=path)

    def hot_remove(self):
        rc, out = self.exec(f"sync; umount {getattr(self, 'hot_path', '/work')}", 120)
        if rc:
            raise GuestErr(f"卸盘失败: {out[-300:]}")
        api(self.api_sock, "PUT", "/api/v1/vm.remove-device", {"id": self.hot_id})
        dev = getattr(self, "hot_dev", "/dev/vdc")
        rc, out = self.exec(f"i=0; while [ -b {dev} ]; do i=$((i+1)); [ $i -gt 20000 ] && exit 9; sleep 0.001; done", 60)
        if rc:
            raise GuestErr(f"热拔以后客机里 {dev} 还在")

    def pause(self):
        api(self.api_sock, "PUT", "/api/v1/vm.pause")

    def resume(self):
        api(self.api_sock, "PUT", "/api/v1/vm.resume")

    def snapshot(self, out):
        os.makedirs(out, exist_ok=True)
        self.pause()
        api(self.api_sock, "PUT", "/api/v1/vm.snapshot", {"destination_url": f"file://{out}"})

    def balloon(self, mib):
        api(self.api_sock, "PUT", "/api/v1/vm.resize", {"desired_balloon": int(mib) << 20})


# ---------- 容器那几种 ----------

_CAPS = ["CAP_" + c for c in ("CHOWN", "DAC_OVERRIDE", "FSETID", "FOWNER", "MKNOD", "NET_RAW", "SETGID", "SETUID",
                               "SETFCAP", "SETPCAP", "NET_BIND_SERVICE", "SYS_CHROOT", "KILL", "AUDIT_WRITE")]


def oci_spec(name, args, work=None, cpus=None, mem_max=None, netns=None, vcpus=None):
    tm = lambda d, mode: {"destination": d, "type": "tmpfs", "source": "tmpfs", "options": ["nosuid", "nodev", f"mode={mode}"]}
    mounts = [
        {"destination": "/proc", "type": "proc", "source": "proc"},
        {"destination": "/dev", "type": "tmpfs", "source": "tmpfs", "options": ["nosuid", "strictatime", "mode=755", "size=65536k"]},
        {"destination": "/dev/pts", "type": "devpts", "source": "devpts",
         "options": ["nosuid", "noexec", "newinstance", "ptmxmode=0666", "mode=0620"]},
        {"destination": "/dev/shm", "type": "tmpfs", "source": "shm", "options": ["nosuid", "noexec", "nodev", "mode=1777", "size=65536k"]},
        {"destination": "/sys", "type": "sysfs", "source": "sysfs", "options": ["nosuid", "noexec", "nodev", "ro"]},
        tm("/tmp", "1777"), tm("/var/tmp", "1777"), tm("/root", "700"), tm("/run", "755"),
    ]
    if work:
        mounts.append({"destination": "/work", "type": "bind", "source": work, "options": ["rbind", "rw"]})
    ns = [{"type": t} for t in ("pid", "ipc", "uts", "mount")]
    ns.append({"type": "network", "path": netns} if netns else {"type": "network"})
    res = {"cpu": {"quota": int(vcpus * 100000), "period": 100000}} if vcpus else {}
    if cpus:
        res.setdefault("cpu", {})["cpus"] = cpus
    if mem_max:
        res["memory"] = {"limit": int(mem_max)}
    return {
        "ociVersion": "1.0.2",
        "process": {"terminal": False, "user": {"uid": 0, "gid": 0}, "args": args,
                    "env": [f"PATH={GUEST_PATH}", "HOME=/root", "LANG=C.UTF-8"], "cwd": "/",
                    "capabilities": {k: _CAPS for k in ("bounding", "effective", "permitted")},
                    "rlimits": [{"type": "RLIMIT_NOFILE", "hard": 1048576, "soft": 1048576}],
                    "noNewPrivileges": True},
        "root": {"path": ROOTFS, "readonly": True},
        "hostname": "bench",
        "mounts": mounts,
        "linux": {"namespaces": ns, "cgroupsPath": f"/bench/{name}", "resources": res,
                  "maskedPaths": ["/proc/kcore", "/proc/keys", "/proc/timer_list", "/sys/firmware"],
                  "readonlyPaths": ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"]},
    }


class OCIEnv(Env):
    """runc 或 gVisor（runsc）：直接给 OCI 包，不经 containerd。根是共用的只读目录，/work 绑这个人的盘。"""

    def __init__(self, name, runtime="runc", platform="systrap", **kw):
        super().__init__(name, **kw)
        self.runtime = runtime
        self.platform = platform
        self.kind = runtime if runtime == "runc" else f"gvisor-{platform}"
        self.mnt = None
        self.bundle = f"{BUNDLES}/{name}"

    def _cli(self):
        if self.runtime == "runc":
            return ["runc", "--root", RUNC_ROOT]
        return [f"{BIN}/runsc", "--root", RUNSC_ROOT, f"--platform={self.platform}",
                f"--network={'sandbox' if self.net else 'none'}", "--overlay2=none"]

    def start(self):
        t0 = now()
        if self.opts.get("mnt"):
            # 用一个已经挂着的目录（H27 对照"Pages 今天的做法"：新容器直接用旧的挂载点，不卸不挂）
            self.mnt, self.own_mnt = self.opts["mnt"], False
        elif self.disk is not None:
            t_m = now()
            self.mnt = host_mount(self.disk.dev, self.name)
            self.t["t_mount"] = now() - t_m
        shutil.rmtree(self.bundle, ignore_errors=True)
        os.makedirs(self.bundle)
        mem_max = self.mem << 20  # 整个环境的预算，跟虚拟机的内存一样大
        with open(f"{self.bundle}/config.json", "w") as f:
            json.dump(oci_spec(self.name, self.opts.get("init_args") or ["/bin/sleep", "infinity"], self.mnt, self.cpus(), mem_max,
                               self.net["nspath"] if self.net else None, vcpus=self.vcpus), f)
        self.cg = f"{CG}/{self.name}"
        with open(f"{self.bundle}/run.log", "w") as lf:
            r = subprocess.run(self._cli() + ["run", "--detach", "--bundle", self.bundle, self.name],
                               stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, timeout=120)
        if r.returncode:
            raise RuntimeError(f"{self.kind} run 退出 {r.returncode}: {open(self.bundle + '/run.log').read()[-600:]}")
        if not self.opts.get("probe", True):
            self.t["t_ready"] = now() - t0
            return self.t
        rc, out = self.exec("true", 60)
        if rc:
            raise RuntimeError(f"{self.kind} 第一条命令失败: {out[-300:]}")
        self.t["t_ready"] = now() - t0
        if self.disk is not None:
            rc, out = self.exec("cat /work/.berth-marker >/dev/null", 60)
            if rc:
                raise RuntimeError(f"读不到这个人的盘: {out[-300:]}")
            self.t["t_usable"] = now() - t0
        return self.t

    def exec(self, script, timeout=1800):
        r = subprocess.run(self._cli() + ["exec", self.name, "/bin/sh", "-c", script],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr

    def stop_writes(self, grace=10.0):
        """换代时让旧的停写：发 SIGTERM，最多等 grace 秒它自己退，没退再 SIGKILL，然后删掉容器（不卸盘）。"""
        sh(self._cli() + ["kill", self.name, "TERM"], check=False, timeout=30)
        end = now() + grace
        while now() < end:
            r = sh(self._cli() + ["state", self.name], check=False, timeout=30)
            if r.returncode or '"stopped"' in r.stdout:
                break
            time.sleep(0.005)
        else:
            sh(self._cli() + ["kill", self.name, "KILL"], check=False, timeout=30)
        sh(self._cli() + ["delete", "-force" if self.runtime == "runsc" else "-f", self.name], check=False, timeout=120)

    def pause(self):
        sh(self._cli() + ["pause", self.name], timeout=60)

    def resume(self):
        sh(self._cli() + ["resume", self.name], timeout=60)

    def checkpoint(self, image):
        """gVisor 的检查点：整个沙箱的内存和状态存到盘上，容器随之停下（Pages "停着保留"的另一种做法）。"""
        os.makedirs(image, exist_ok=True)
        sh(self._cli() + ["checkpoint", f"--image-path={image}", self.name], timeout=600)
        sh(self._cli() + ["delete", "-force", self.name], check=False, timeout=120)

    def restore(self, image):
        with open(f"{self.bundle}/restore.log", "w") as lf:
            r = subprocess.run(self._cli() + ["restore", f"--image-path={image}", "--detach", "--bundle", self.bundle,
                                              self.name], stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, timeout=600)
        if r.returncode:
            raise RuntimeError(f"restore 退出 {r.returncode}: {open(self.bundle + '/restore.log').read()[-600:]}")

    def stop(self, graceful=True):
        t0 = now()
        r = {}
        if graceful and self.disk is not None:
            with contextlib.suppress(Exception):
                self.exec("sync", 120)
            r["t_umount"] = now() - t0
        subprocess.run(self._cli() + ["kill", self.name, "KILL"], capture_output=True, timeout=60)
        force = "-f" if self.runtime == "runc" else "-force"
        subprocess.run(self._cli() + ["delete", force, self.name], capture_output=True, timeout=120)
        if getattr(self, "own_mnt", True):
            host_umount(self.mnt)
        cg_remove(self.cg)
        net_drop(self.net)
        shutil.rmtree(self.bundle, ignore_errors=True)
        r["t_stop"] = now() - t0
        return r


def kata_config_src():
    cands = [p for p in glob.glob("/opt/kata/share/defaults/kata-containers/**/configuration-clh*runtime-rs*.toml", recursive=True)
             if "azure" not in p]
    if not cands:
        raise RuntimeError("找不到 Kata 的 Cloud Hypervisor 配置")
    return sorted(cands, key=len)[0]


_kata_cfg = None


def kata_config(vcpus, mem, reclaim=False):
    """写 Kata runtime-rs 的默认配置：Cloud Hypervisor 后端，虚拟机按形状给核和内存。"""
    global _kata_cfg
    key = (vcpus, mem, reclaim)
    if _kata_cfg == key:
        return
    txt = open(kata_config_src()).read()

    def setv(t, k, v):
        new, n = re.subn(rf"(?m)^#?\s*{k}\s*=.*$", f"{k} = {v}", t, count=1)
        if not n:
            raise RuntimeError(f"Kata 配置里没有 {k}，不往末尾追加（会落到错的表里）")
        return new

    txt = setv(txt, "default_vcpus", vcpus)
    txt = setv(txt, "default_memory", mem)
    txt = setv(txt, "reclaim_guest_freed_memory", "true" if reclaim else "false")
    txt = setv(txt, "sandbox_cgroup_only", "true")
    os.makedirs("/etc/kata-containers/runtime-rs", exist_ok=True)
    with open("/etc/kata-containers/runtime-rs/configuration.toml", "w") as f:
        f.write(txt)
    _kata_cfg = key


class KataEnv(Env):
    """Kata（runtime-rs + Cloud Hypervisor）：经 containerd 的 ctr 起，根目录经 virtio-fs 共享进虚拟机。"""
    kind = "kata-clh"

    def __init__(self, name, **kw):
        super().__init__(name, **kw)
        self.mnt = None
        self.nexec = 0

    def _ctr(self, *a):
        return ["ctr", "-n", CTR_NS, *a]

    def start(self):
        kata_config(self.vcpus, self.mem, self.opts.get("reclaim", False))
        t0 = now()
        if self.disk is not None:
            self.mnt = host_mount(self.disk.dev, self.name)
        self.cg = cg_make(self.name, cpus=self.cpus())
        cmd = self._ctr("run", "-d", "--null-io", "--runtime", "io.containerd.kata.v2", "--rootfs", "--read-only",
                        "--cgroup", f"/bench/{self.name}", "--env", f"PATH={GUEST_PATH}", "--env", "HOME=/root",
                        "--env", "LANG=C.UTF-8")
        if self.mnt:
            cmd += ["--mount", f"type=bind,src={self.mnt},dst=/work,options=rbind:rw"]
        for d, mode in (("/tmp", "1777"), ("/var/tmp", "1777"), ("/root", "700"), ("/run", "755")):
            cmd += ["--mount", f"type=tmpfs,src=tmpfs,dst={d},options=rw:nosuid:nodev:mode={mode}"]
        if self.net:
            cmd += ["--with-ns", f"network:{self.net['nspath']}"]
        cmd += [ROOTFS, self.name, "/bin/sleep", "infinity"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode:
            raise RuntimeError(f"kata run 退出 {r.returncode}: {(r.stderr or r.stdout)[-600:]}")
        if not self.opts.get("probe", True):
            self.t["t_ready"] = now() - t0
            return self.t
        rc, out = self.exec("true", 60)
        if rc:
            raise RuntimeError(f"kata 第一条命令失败: {out[-300:]}")
        self.t["t_ready"] = now() - t0
        if self.disk is not None:
            rc, out = self.exec("cat /work/.berth-marker >/dev/null", 60)
            if rc:
                raise RuntimeError(f"读不到这个人的盘: {out[-300:]}")
            self.t["t_usable"] = now() - t0
        return self.t

    def exec(self, script, timeout=1800):
        self.nexec += 1
        # ctr task exec 起的进程不带 PATH（干跑 2026-11-20：nohup、make 找不到 /usr/local/bin 下的程序）
        r = subprocess.run(self._ctr("task", "exec", "--exec-id", f"e{self.nexec}", self.name, "/usr/bin/env",
                                     f"PATH={GUEST_PATH}", "HOME=/root", "LANG=C.UTF-8", "/bin/sh", "-c", script),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr

    def pids(self):
        ps = cg_procs(self.cg) if self.cg else []
        if ps:
            return ps
        r = sh(["pgrep", "-f", self.name], check=False)
        return [int(x) for x in r.stdout.split()]

    def stop(self, graceful=True):
        t0 = now()
        r = {}
        if graceful and self.disk is not None:
            with contextlib.suppress(Exception):
                self.exec("sync", 120)
            r["t_umount"] = now() - t0
        subprocess.run(self._ctr("task", "kill", "-s", "SIGKILL", self.name), capture_output=True, timeout=60)
        for _ in range(100):
            d = subprocess.run(self._ctr("task", "delete", "-f", self.name), capture_output=True, text=True, timeout=60)
            if d.returncode == 0 or "not found" in (d.stderr or ""):
                break
            time.sleep(0.1)
        subprocess.run(self._ctr("container", "delete", self.name), capture_output=True, timeout=60)
        host_umount(self.mnt)
        cg_remove(self.cg)
        net_drop(self.net)
        r["t_stop"] = now() - t0
        return r


class NativeEnv(Env):
    """不隔离的对照：同一个根目录 chroot 进去跑，同样钉核、同样挂这个人的盘。"""
    kind = "native"
    WRAP = ('R=$1; W=$2; S=$3; T=$4; mount --bind "$R" "$R" && mount -o remount,bind,ro "$R" && '
            'mount --bind "$W" "$R/work" && for d in tmp root var/tmp run; do mount --bind "$T/$d" "$R/$d"; done && '
            'mount -t proc proc "$R/proc" && mount --rbind /dev "$R/dev" && '
            f'exec chroot "$R" /usr/bin/env -i PATH={GUEST_PATH} HOME=/root LANG=C.UTF-8 /bin/sh -c "$S"')

    def start(self):
        t0 = now()
        self.cg = cg_make(self.name, cpus=self.cpus(), mem_max=self.mem << 20)
        with open(f"{self.cg}/cpu.max", "w") as f:
            f.write(f"{self.vcpus * 100000} 100000")
        self.mnt = host_mount(self.disk.dev, self.name) if self.disk is not None else f"{MNT}/empty"
        os.makedirs(self.mnt, exist_ok=True)
        self.tmp = f"{SRV}/ntmp/{self.name}"
        shutil.rmtree(self.tmp, ignore_errors=True)
        for d, mode in (("tmp", 0o1777), ("root", 0o700), ("var/tmp", 0o1777), ("run", 0o755)):
            os.makedirs(f"{self.tmp}/{d}", exist_ok=True)
            os.chmod(f"{self.tmp}/{d}", mode)
        rc, out = self.exec("cat /work/.berth-marker >/dev/null" if self.disk is not None else "true", 60)
        if rc:
            raise RuntimeError(f"native 起不来: {out[-300:]}")
        self.t["t_ready"] = self.t["t_usable"] = now() - t0
        return self.t

    def exec(self, script, timeout=1800):
        r = subprocess.run(["unshare", "-m", "--propagation", "private", "--", "/bin/sh", "-c", self.WRAP, "x",
                            ROOTFS, self.mnt, script, self.tmp], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout, preexec_fn=join_cgroup_fn(self.cg))
        return r.returncode, r.stdout + r.stderr

    def stop(self, graceful=True):
        t0 = now()
        cg_remove(self.cg)  # 先杀掉留在这个环境里的进程，再卸盘
        if self.disk is not None:
            host_umount(self.mnt)
        shutil.rmtree(getattr(self, "tmp", ""), ignore_errors=True)
        return {"t_stop": now() - t0}


# 名字 -> 怎么造。实验脚本只认这些名字。
KINDS = {
    "native": lambda n, **kw: NativeEnv(n, **kw),
    "runc": lambda n, **kw: OCIEnv(n, runtime="runc", **kw),
    "gvisor-systrap": lambda n, **kw: OCIEnv(n, runtime="runsc", platform="systrap", **kw),
    "gvisor-kvm": lambda n, **kw: OCIEnv(n, runtime="runsc", platform="kvm", **kw),
    "kata-clh": lambda n, **kw: KataEnv(n, **kw),
    "fc-cold": lambda n, **kw: FC(n, mode="cold", **kw),
    "fc-snap": lambda n, **kw: FC(n, mode="snap", **kw),
    "ch-cold": lambda n, **kw: CH(n, mode="cold", **kw),
    "ch-snap-copy": lambda n, **kw: CH(n, mode="copy", **kw),
    "ch-snap-cow": lambda n, **kw: CH(n, mode="copyonwrite", **kw),
    "ch-snap-uffd": lambda n, **kw: CH(n, mode="ondemand", **kw),
    "ch-snap-hotplug": lambda n, **kw: CH(n, mode="copyonwrite", attach="hotplug", **kw),
}


def make_env(kind, name, **kw):
    e = KINDS[kind](name, **kw)
    e.kind = kind
    return e


# ---------- 收拾 ----------

def cleanup_all():
    """实验之间、出错以后：把这套实验起过的东西全部清掉。"""
    for pat in (f"{BIN}/firecracker", f"{BIN}/cloud-hypervisor"):
        sh(["pkill", "-9", "-f", pat], check=False)
    for root, rt in ((RUNSC_ROOT, [f"{BIN}/runsc", "--root", RUNSC_ROOT]), (RUNC_ROOT, ["runc", "--root", RUNC_ROOT])):
        if os.path.isdir(root):
            r = sh(rt + ["list", "-q"], check=False)
            for cid in r.stdout.split():
                sh(rt + ["kill", cid, "KILL"], check=False)
                sh(rt + ["delete", "-force" if "runsc" in rt[0] else "-f", cid], check=False)
    r = sh(["ctr", "-n", CTR_NS, "task", "ls", "-q"], check=False)
    for cid in r.stdout.split():
        sh(["ctr", "-n", CTR_NS, "task", "kill", "-s", "SIGKILL", cid], check=False)
        sh(["ctr", "-n", CTR_NS, "task", "delete", "-f", cid], check=False)
    r = sh(["ctr", "-n", CTR_NS, "container", "ls", "-q"], check=False)
    for cid in r.stdout.split():
        sh(["ctr", "-n", CTR_NS, "container", "delete", cid], check=False)
    for m in sorted(glob.glob(f"{MNT}/*")):
        host_umount(m)
    for d in (VMS, BUNDLES):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    sh("for n in $(ip netns list | grep -o '^b[a-z]*ns[0-9]*\\|^bgw\\|^bsrc\\|^bsink[0-9]*'); do ip netns del $n; done; "
       "for d in $(ip -o link | awk -F': ' '{print $2}' | cut -d@ -f1 | grep '^bx[0-9]*$'); do ip link del $d; done; true", check=False)
    if ZFS:
        for parent in (f"{POOL}/users", f"{POOL}/recv", f"{POOL}/comp", f"{POOL}/enc/users"):
            r = sh(["zfs", "list", "-H", "-o", "name", "-d", "1", parent], check=False)
            for ds in r.stdout.split()[1:]:
                for _ in range(30):
                    if sh(["zfs", "destroy", "-r", ds], check=False).returncode == 0:
                        break
                    time.sleep(0.3)
                else:
                    log("收拾时删不掉:", ds)
    else:
        shutil.rmtree(f"{ROOT}/users", ignore_errors=True)
    sh("for d in $(ip -o link | grep -o 'btap[0-9]*\\|bvb[0-9]*' | sort -u); do ip link del $d; done; "
       "for n in $(ip netns list | grep -o '^bns[0-9]*'); do ip netns del $n; done; true", check=False)
    sh(f"ip link del {BRIDGE} 2>/dev/null; true", check=False)
    for sub in sorted(glob.glob(f"{CG}/*/"), key=len, reverse=True):
        cg_remove(sub.rstrip("/"))
