"""第一批真机实验里补测的五条假设。"""
import contextlib
import json
import os
import random
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import benchlib as B


def _bench():
    # bench.py 直接运行时，用已经载入的入口，避免再载入一套 guard 的失败计数。
    main = sys.modules["__main__"]
    if getattr(main, "HYP_EXPS", None) is EXPS:
        return main
    import bench
    return bench


def _guest(env, script, timeout):
    rc, out = env.exec(script, timeout)
    if rc:
        raise RuntimeError(f"客机命令失败 rc={rc}: {out[-600:]}")
    return out


def _run_env(bench, rec, base, env, measure, disk=None):
    def one():
        if disk is not None:
            disk.create()
        env.start()
        measure()

    # guard 先留失败现场的 VMM 日志，外层再确保异常和中断时都先关环境、再删盘。
    with contextlib.ExitStack() as cleanup:
        if disk is not None:
            cleanup.callback(bench.guard, rec, {**base, "part": "disk-cleanup"}, disk.destroy)
        cleanup.callback(bench.guard, rec, {**base, "part": "env-cleanup"}, lambda: env.stop(graceful=False))
        bench.guard(rec, {**base, "part": "run"}, one, [env])


def _timings(bench, rec, env, case, reps, command, *, setup="", after=":", timeout=60):
    # 客机内一次发完，计时只包住这一条命令，清理放在第二次读钟以后。
    script = fr"""set -eu
{setup}
for rep in $(seq 1 {reps}); do
    before=$(date +%s%N)
    if {command}; then
        after=$(date +%s%N)
        printf 'timing %s %s\n' "$rep" "$((after - before))"
    else
        printf 'failed %s %s\n' "$rep" "$?"
    fi
    {after}
done
"""

    def one():
        rc, out = env.exec(script, timeout)
        samples = re.findall(r"^(timing|failed) (\d+) (-?\d+)$", out, re.M)
        for result, i, value in samples:
            base = {"part": "timing", "case": case, "rep": int(i)}
            if result == "failed":
                rec.put(**base, ok=False, err=f"被测命令退出 {value}")
            else:
                ns = int(value)
                rec.put(**base, ok=ns >= 0, elapsed_ns=ns)
        complete = [int(i) for _, i, _ in samples] == list(range(1, reps + 1))
        valid = sum(result == "timing" and int(value) >= 0 for result, _, value in samples)
        if rc or not complete or valid != reps:
            raise RuntimeError(f"{case} 计时未完整取得：rc={rc}，有效 {valid}/{reps}，输出尾部 {out[-600:]!r}")

    bench.guard(rec, {"part": "timing-batch", "case": case}, one)


# ---------------- H6 虚拟机里的用户命名空间墙 ----------------

BWRAP = ("bwrap --unshare-user --uid 0 --gid 0 --unshare-pid --unshare-ipc --unshare-uts "
         "--unshare-cgroup-try --ro-bind / / --bind /work /work --dev /dev --proc /proc "
         "--tmpfs /tmp --tmpfs /run --new-session --die-with-parent --cap-drop ALL true")


def exp_wall():
    bench = _bench()
    rec = B.Rec("wall")
    disk = B.Disk("hyp-wall")
    env = B.make_env("ch-snap-cow", "hyp-wall", shape="dute", disk=disk)

    def measure():
        reps = bench.n(100, 5)
        # 直接执行 /usr/bin/true，两组都量真正起一个进程。bwrap 这组未加载 seccomp 过滤器。
        _timings(bench, rec, env, "direct", reps, "/usr/bin/true")
        _timings(bench, rec, env, "bwrap", reps, BWRAP)

    _run_env(bench, rec, {"kind": "ch-snap-cow"}, env, measure, disk=disk)


# ---------------- H22 每条命令一个网络出口 ----------------

CMDNET_TAP = 99
CMDNET_ROUTE = "10.99.0.0/16"
NETNS = r"""
net_add() {
    ip netns add "c$1" &&
    ip link add "vh$1" type veth peer name "vc$1" &&
    ip link set "vc$1" netns "c$1" &&
    ip addr add "10.99.$1.1/30" dev "vh$1" &&
    ip link set "vh$1" up &&
    ip -n "c$1" addr add "10.99.$1.2/30" dev "vc$1" &&
    ip -n "c$1" link set "vc$1" up &&
    ip -n "c$1" link set lo up &&
    ip -n "c$1" route add default via "10.99.$1.1"
}
net_del() {
    # 删网络空间时内核会顺带拆掉那对网卡，但不是立刻拆完；"已经没了"不算失败（真机 2026-11-21 撞上 RTNETLINK No such device）
    ip netns del "c$1" 2>/dev/null || :
    i=0
    while ip link show "vh$1" >/dev/null 2>&1 && [ "$i" -lt 200 ]; do ip link del "vh$1" 2>/dev/null || :; i=$((i + 1)); sleep 0.005; done
    return 0
}
net_clean() {  # 开头先清掉上一次可能留下的，放在计时之外
    for k in $(seq 1 "$1"); do net_del "$k"; done
}
"""


SRC_CODE = ("import socket\ns=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "s.bind(('0.0.0.0', 9100)); s.listen(64); print('R', flush=True)\nwhile True:\n    c,a=s.accept()\n"
            "    try:\n        c.sendall((a[0]+'\\n').encode())\n    finally:\n        c.close()")


@contextlib.contextmanager
def _ns_source_server(guest_ip):
    """来源服务放在网桥上的网络空间里（真机 2026-11-21：testbox 的宿主防火墙挡连宿主自己端口的连接）。"""
    ns = B.ns_on_bridge("bsrc", 240, routes=[f"{CMDNET_ROUTE} via {guest_ip}"])
    p = subprocess.Popen(["ip", "netns", "exec", "bsrc", sys.executable, "-c", SRC_CODE], stdout=subprocess.PIPE, text=True)
    try:
        if p.stdout.readline().strip() != "R":
            raise RuntimeError("来源服务没起来")
        yield ns["ip"], 9100
    finally:
        p.kill()
        B.ns_drop(ns)


@contextlib.contextmanager
def _source_server(host):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((host, 0))
        listener.listen(32)
        listener.settimeout(0.2)
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    conn, addr = listener.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.settimeout(1)
                    with contextlib.suppress(OSError):
                        conn.sendall(f"{addr[0]}\n".encode("ascii"))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1]
        finally:
            stop.set()
            thread.join(3)
            if thread.is_alive():
                raise TimeoutError("宿主的源地址服务没停下来")


def _net_peers(rec, env, host, port, count):
    # 先建好所有网络空间，再放行各条命令同时连宿主。回话里的地址由宿主从 TCP 连接读取。
    script = fr"""set -eu
{NETNS}
tmp=$(mktemp -d /tmp/berth-cmdnet.XXXXXX)
pids=
finish() {{
    status=$?
    trap - EXIT
    for p in $pids; do kill "$p" 2>/dev/null || :; done
    for p in $pids; do wait "$p" 2>/dev/null || :; done
    for k in $(seq 1 {count}); do net_del "$k" || status=1; done
    rm -rf "$tmp" || status=1
    exit "$status"
}}
trap finish EXIT
trap 'exit 1' HUP INT TERM
net_clean 32
for k in $(seq 1 {count}); do net_add "$k"; done
for k in $(seq 1 {count}); do
    timeout -k 1s 15s sh -eu -c '
        while [ ! -e "$1" ]; do sleep 0.01; done
        exec ip netns exec "$2" socat -u -T 5 "$3" -
    ' cmdnet "$tmp/go" "c$k" "TCP4:{host}:{port},connect-timeout=5" </dev/null >"$tmp/$k" 2>&1 &
    pids="$pids $!"
done
touch "$tmp/go"
k=0
failed=0
for p in $pids; do
    k=$((k + 1))
    if wait "$p"; then
        printf 'peer %s %s\n' "$k" "$(cat "$tmp/$k")"
    else
        cat "$tmp/$k" >&2
        failed=1
    fi
done
pids=
exit "$failed"
"""
    rc, out = env.exec(script, 60)
    peers = {int(i): addr for i, addr in re.findall(r"^peer (\d+) (\S+)$", out, re.M)}
    for rep in range(1, count + 1):
        expected = f"10.99.{rep}.2"
        addr = peers.get(rep)
        rec.put(part="peer", rep=rep, expected_ip=expected, source_ip=addr, ok=addr == expected)
    if rc:
        raise RuntimeError(f"并发命令或网络空间清理失败 rc={rc}: {out[-600:]}")


def exp_cmdnet():
    bench = _bench()
    rec = B.Rec("cmdnet")
    with contextlib.ExitStack() as cleanup:
        def one():
            # tap_make 中途失败也可能留下接口。只在本次加路由成功后登记删路由。
            cleanup.callback(bench.guard, rec, {"part": "tap-cleanup"}, lambda: B.net_drop({"dev": f"btap{CMDNET_TAP}"}))
            net = B.tap_make(CMDNET_TAP, bridge=True)
            env = B.make_env("ch-cold", "hyp-cmdnet", shape="dute", net=net)
            cleanup.callback(bench.guard, rec, {"part": "env-cleanup"}, lambda: env.stop(graceful=False))
            if bench.guard(rec, {"part": "boot"}, env.start, [env]) is None:
                return
            _guest(env, "sysctl -qw net.ipv4.ip_forward=1", 15)
            # 回 10.99.0.0/16 的路由加在来源服务的网络空间里（见 _ns_source_server），宿主上不加
            reps = bench.n(30, 3)
            _timings(bench, rec, env, "direct", reps, "/usr/bin/true")
            _timings(bench, rec, env, "netns", reps, 'net_add "$rep" && ip netns exec "c$rep" true',
                     setup=NETNS + f"net_clean {max(reps, 16)}\n" + """rep=0
trap 'net_del "$rep"' EXIT
trap 'exit 1' HUP INT TERM""", after='net_del "$rep"')
            with _ns_source_server(net["guest_ip"]) as (src_ip, port):
                bench.guard(rec, {"part": "peer-batch"},
                            lambda: _net_peers(rec, env, src_ip, port, bench.n(16, 4)))

        bench.guard(rec, {"part": "run"}, one)


# ---------------- H24 机器代理被杀后重新接回 ----------------

AGENT_SCRIPT = Path(__file__).resolve().parent / "lib" / "agent_reattach.py"


def _kill_agent(argv, quick):
    proc = subprocess.Popen([*argv, "--hold"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        if not select.select([proc.stdout], [], [], 12)[0]:
            raise TimeoutError("旧代理没有报出接回结果")
        data = os.read(proc.stdout.fileno(), 4096)
        try:
            old = json.loads(data)
        except ValueError as exc:
            raise RuntimeError(f"旧代理接回失败：{data[-600:]!r}") from exc
        if not old["running"]:
            raise RuntimeError("旧代理接回时客机命令已经结束")
        time.sleep(random.uniform(0.025, 0.10) if quick else random.uniform(0.10, 0.50))
    finally:
        try:
            # 只杀代理这一进程组。虚拟机从一开始就在 systemd 独立的服务单元里。
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        finally:
            proc.stdout.close()
    if proc.returncode != -signal.SIGKILL:
        raise RuntimeError(f"旧代理没有被 SIGKILL：退出码 {proc.returncode}")
    return old


def _stop_unit(unit):
    env = {**os.environ, "LC_ALL": "C"}

    def stop():
        r = B.sh(["systemctl", "stop", unit], check=False, timeout=25, env=env)
        if r.returncode and "not loaded" not in r.stderr:
            raise RuntimeError(f"停 {unit} 失败：{r.stderr[-600:]}")

    try:
        stop()
    except (RuntimeError, subprocess.TimeoutExpired):
        B.sh(["systemctl", "kill", "--signal=SIGKILL", unit], check=False, timeout=10, env=env)
        stop()


def exp_agent():
    bench = _bench()
    rec = B.Rec("agent")
    generation = uuid.uuid4().hex
    name = f"hyp-{generation[:10]}"
    unit = f"bench-agent-{name}.service"
    disk = B.Disk(name)
    env = B.CH(name, shape="dute", disk=disk, mode="copyonwrite")
    duration = bench.n(30, 6)
    base = {"expected_generation": generation, "duration_s": duration}

    with contextlib.ExitStack() as cleanup:
        cleanup.callback(bench.guard, rec, {"part": "disk-cleanup"}, disk.destroy)
        cleanup.callback(bench.guard, rec, {"part": "env-cleanup"}, lambda: env.stop(graceful=False))

        def one():
            disk.create()
            env._prep_dir()
            cleanup.callback(bench.guard, rec, {"part": "unit-cleanup"}, lambda: _stop_unit(unit))
            # vmns exec 到 VMM。mixed 在最后停服务时也能清掉单元里的其余进程。
            snap = f"{B.SNAP}/ch-dute"
            argv = [B.VMNS, env.dir, "-", f"{B.BIN}/cloud-hypervisor", "--api-socket", "path=/run/vm/api.sock",
                    "--restore", f"source_url=file://{snap},memory_restore_mode=copyonwrite,resume=true"]
            try:
                B.sh(["systemd-run", f"--unit={unit}", "--collect", "--service-type=exec", "--expand-environment=no",
                      "--property=KillMode=mixed", "--property=TimeoutStopSec=10s",
                      f"--property=StandardOutput=append:{env.dir}/vmm.log", "--property=StandardError=inherit",
                      "--", *argv], timeout=15)
                B.wait_path(env.api_sock, 15)
                B.wait_vsock_ready(env.uds, timeout=60)
                env.mount_user("/dev/vdb")
            except Exception as exc:
                raise RuntimeError(f"{exc}；{unit} 的 VMM 日志尾部：{env.vmm_log()!r}") from exc

            state = Path(env.dir) / "agent.json"
            state.write_text(json.dumps({"unit": unit, "api_sock": env.api_sock, "uds": env.uds, "generation": generation}))
            agent = [sys.executable, str(AGENT_SCRIPT), str(state)]
            # 代号留在 /run，输出留在这块人的盘上。把后台命令的三个文件描述符和 vsock 分开。
            command = (f'set -e; for i in $(seq {duration}); do echo "$i" >> /work/cmd.out; sleep 1; done; '
                       'echo done > /work/cmd.rc.tmp; mv /work/cmd.rc.tmp /work/cmd.rc')
            _guest(env, fr"""set -eu
printf '%s\n' {generation} > /run/berth-gen
: > /work/cmd.out
rm -f /work/cmd.rc /work/cmd.rc.tmp
setsid nohup sh -c {shlex.quote(command)} </dev/null >/dev/null 2>&1 &
timeout 5s sh -c 'while [ ! -s /work/cmd.out ]; do sleep 0.02; done'
""", 10)

            def reattach():
                old = _kill_agent(agent, B.QUICK)
                r = B.sh(agent, timeout=12)
                new = json.loads(r.stdout)
                if not new["running"] or not 1 <= old["lines"] <= new["lines"] <= duration:
                    raise RuntimeError(f"新代理没接回正在运行的命令：杀前 {old}，接回 {new}")
                return {**new, "lines_before_kill": old["lines"]}

            for rep in range(1, bench.n(10, 2) + 1):
                bench.guard(rec, {**base, "part": "reattach", "rep": rep}, reattach)

            def finish():
                out = _guest(env, f"""set -eu
timeout {duration + 5}s sh -c 'while [ ! -e /work/cmd.rc ]; do sleep 0.1; done'
cat /run/berth-gen /work/cmd.rc /work/cmd.out
""", duration + 10)
                want = [generation, "done", *(str(i) for i in range(1, duration + 1))]
                if out.splitlines() != want:
                    raise RuntimeError(f"命令的代号、退出标记或输出不完整：{out[-600:]!r}")
                _guest(env, "sync && umount /work", 30)
                return {"generation": generation, "cmd_rc": "done", "lines": duration}

            bench.guard(rec, {**base, "part": "finish"}, finish)

        bench.guard(rec, {**base, "part": "run"}, one)


# ---------------- H25 作品内存额度含哪些开销 ----------------

GVL_CASES = (("gvisor-512", "runsc", 0), ("gvisor-tmp", "runsc", 128),
             ("gvisor-extra", "runsc", 0), ("runc-512", "runc", 0))
MIB = 1 << 20
ALLOCATOR = "/usr/local/lib/berth/alloc_until.py"


def _oom_events(cg):
    events = dict(line.split() for line in (Path(cg) / "memory.events").read_text().splitlines())
    return {key: int(events[key]) for key in ("oom", "oom_kill")}


def exp_gvlimits():
    bench = _bench()
    rec = B.Rec("gvlimits")
    for rep in range(1, bench.n(2, 1) + 1):
        for case, runtime, tmp_mib in GVL_CASES:
            name = f"hyp-{case}-{rep}"
            env = B.OCIEnv(name, shape="pages", runtime=runtime, platform="systrap")
            base = {"part": "limit", "case": case, "rep": rep, "tmp_mib": tmp_mib}

            def one():
                if case == "gvisor-extra":
                    probe = B.OCIEnv(f"{name}-idle", shape="pages", runtime=runtime, platform="systrap")
                    try:
                        probe.start()
                        idle = B.cg_stat(probe.cg)["mem_current"]
                    finally:
                        probe.stop(graceful=False)
                    # OCIEnv 接收整 MiB。把量出的字节数和向上取整后给的额度一起记。
                    base["idle_bytes"] = idle
                    env.mem += (idle + MIB - 1) // MIB
                env.start()
                limit = int((Path(env.cg) / "memory.max").read_text())
                base["limit_bytes"] = limit
                if limit != env.mem * MIB:
                    raise RuntimeError(f"{env.kind} 实际 memory.max {limit} 与设定的 {env.mem} MiB 不符")
                if tmp_mib:
                    _guest(env, f"dd if=/dev/zero of=/tmp/berth-gvlimits bs=1M count={tmp_mib} status=none", 30)
                before = _oom_events(env.cg)
                rc, out = env.exec(f"exec python3 -u {ALLOCATOR}", 90)
                sizes = [int(line) for line in out.splitlines() if line.isascii() and line.isdigit()]
                base.update(alloc_mib=sizes[-1] if sizes else None, alloc_rc=rc)
                base["peak_bytes"] = B.cg_stat(env.cg)["mem_peak"]
                after = _oom_events(env.cg)
                delta = {k: after[k] - before[k] for k in before}
                base.update(delta)
                # gVisor 可能是 Sentry 整个被杀，runsc exec 的退出码这时未必是 137。
                if rc == 0 or delta["oom"] <= 0 or delta["oom_kill"] <= 0:
                    raise RuntimeError(f"没有撞到 cgroup OOM 并被杀的证据：rc={rc}，{delta}，输出尾部 {out[-400:]!r}")
                if not sizes or sizes != list(range(16, sizes[-1] + 1, 16)):
                    raise RuntimeError(f"分配器的累计 MiB 不完整：{out[-400:]!r}")

            try:
                bench.guard(rec, base, one)
            finally:
                bench.guard(rec, {"part": "env-cleanup", "case": case, "rep": rep}, lambda: env.stop(graceful=False))


# ---------------- H26 同机墙钟偏差 ----------------

CLOCK_KINDS = ("fc-cold", "fc-snap", "ch-cold", "ch-snap-cow", "gvisor-systrap", "kata-clh")


def exp_clock():
    bench = _bench()
    rec = B.Rec("clock")
    for kind in CLOCK_KINDS:
        env = B.make_env(kind, f"hyp-clock-{kind}", shape="dute")
        idle = bench.n(60, 3)

        def measure():
            # 冷读 2026-11-20：每次 exec 一个 date，往返几毫秒，1 毫秒的线判不出来。改成在环境里起一个常驻的
            # 小循环（python 读一行回一个 time.time_ns()），宿主跟它一问一答，往返只剩几百微秒。
            reader = ClockReader(env)

            def sample():
                t1 = time.time_ns()
                guest = reader.read()
                t2 = time.time_ns()
                if t2 < t1:
                    raise RuntimeError("宿主钟倒退")
                # 保存整数读数。用区间两端的较大距离算上界，等价于绝对偏差加不确定度。
                return {"t1_ns": t1, "t2_ns": t2, "guest_ns": guest, "offset_ns": guest - (t1 + t2) // 2,
                        "uncertainty_ns": (t2 - t1 + 1) // 2, "bound_ns": max(abs(guest - t1), abs(guest - t2))}

            for phase in ("start", "idle"):
                if phase == "idle":
                    time.sleep(idle)
                for rep in range(1, bench.n(20, 3) + 1):
                    bench.guard(rec, {"part": "sample", "kind": kind, "phase": phase, "rep": rep, "idle_s": idle},
                                sample, key=("clock", kind, phase))

        _run_env(bench, rec, {"kind": kind}, env, measure)


# 先回一声 R 再开始答：不然宿主紧接着发的第一行可能被客机里的 shell 一起读走、丢了（干跑 2026-11-20 撞上）
CLOCK_LOOP = "import sys,time\nprint('R', flush=True)\nfor _ in sys.stdin:\n    print(time.time_ns(), flush=True)"


class ClockReader:
    """跟环境里常驻的读钟循环一问一答。虚拟机走 vsock 的交互连接；容器走 exec 的标准输入输出。"""

    def __init__(self, env):
        self.env = env
        self.sock = self.proc = None
        if isinstance(env, B.VMEnv):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect(env.uds)
            s.sendall(b"CONNECT 52\n")
            if not B._readline(s).startswith(b"OK"):
                raise RuntimeError("vsock 握手没回 OK")
            s.sendall(f"exec python3 -c {shlex.quote(CLOCK_LOOP)}\n".encode())
            self.sock, self.f = s, s.makefile("rb")
        else:
            if isinstance(env, B.KataEnv):
                cmd = env._ctr("task", "exec", "--exec-id", f"clk{uuid.uuid4().hex[:6]}", env.name,
                               "/usr/bin/env", f"PATH={B.GUEST_PATH}", "python3", "-c", CLOCK_LOOP)
            else:
                cmd = env._cli() + ["exec", env.name, "python3", "-c", CLOCK_LOOP]
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
            self.f = self.proc.stdout
        first = self.f.readline()
        if first.strip() != b"R":
            raise RuntimeError(f"读钟循环没起来：{first[-200:]!r}")
        for _ in range(5):  # 热身：python 起来、第一次往返不算
            self.read()

    def read(self):
        if self.sock:
            self.sock.sendall(b"x\n")
        else:
            self.proc.stdin.write(b"x\n")
        line = self.f.readline()
        if not line.strip().isdigit():
            raise RuntimeError(f"读钟循环没回数：{line[-200:]!r}")
        return int(line)


EXPS = {"wall": exp_wall, "cmdnet": exp_cmdnet, "agent": exp_agent, "gvlimits": exp_gvlimits, "clock": exp_clock}
