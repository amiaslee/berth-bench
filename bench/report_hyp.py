"""把五条补测的原始记录汇成表和判据。"""
from collections import defaultdict
from statistics import median


def _ms(ns):
    return "—" if ns is None else f"{ns / 10**6:.3f}"


def _delta(new, old):
    return None if new is None or old is None else new - old


def _complete(rows, count):
    return len(rows) == count and {r.get("rep") for r in rows} == set(range(1, count + 1))


def _failures(rows):
    count = sum(not r.get("ok") for r in rows)
    return f"，失败或跳过 {count} 条记录" if count else ""


def _timing(rows, table, pct, title, case, label, reps):
    samples = [
        [r for r in rows if r.get("ok") and r.get("part") == "timing" and r.get("case") == kind
         and isinstance(r.get("elapsed_ns"), int) and r["elapsed_ns"] >= 0]
        for kind in ("direct", case)
    ]
    values = [[r["elapsed_ns"] for r in rs] for rs in samples]
    p50 = [median(xs) if xs else None for xs in values]
    p99 = [pct(xs, .99) for xs in values]
    extra = _delta(p50[1], p50[0])
    counts = [f"{len(rs)}/{reps}" for rs in samples]
    table(title, ["做法", "有效样本", "中位数", "p99"],
          [["直接起", counts[0], _ms(p50[0]), _ms(p99[0])],
           [label, counts[1], _ms(p50[1]), _ms(p99[1])],
           ["多花（两组统计量相减）", "—", _ms(extra), _ms(_delta(p99[1], p99[0]))]])
    return extra, all(_complete(rs, reps) for rs in samples), f"直接起 {counts[0]}，{label} {counts[1]}"


def _wall(rows, quick, table, pct, f):
    extra, complete, counts = _timing(rows, table, pct, "H6 虚拟机里起命令（毫秒，bwrap 未加载 seccomp 过滤器）",
                                       "bwrap", "bwrap", 5 if quick else 100)
    passed = complete and extra <= 100 * 10**6 and all(r.get("ok") for r in rows)
    return passed, (f"起命令多花中位 {_ms(extra)} 毫秒，上限 100 毫秒，{counts}{_failures(rows)}，"
                    "构建慢不过 5% 由编译实验验证")


def _cmdnet(rows, quick, table, pct, f):
    extra, complete, counts = _timing(rows, table, pct, "H22 建网络空间并起命令（毫秒）",
                                       "netns", "每条命令一个网络空间", 3 if quick else 30)
    total = 4 if quick else 16
    peers = [r for r in rows if r.get("part") == "peer"]
    by_rep = {r["rep"]: r for r in peers}
    matched = 0
    detail = []
    for k in range(1, total + 1):
        row = by_rep.get(k, {})
        expected = f"10.99.{k}.2"
        same = row.get("ok") and row.get("source_ip") == expected
        matched += bool(same)
        detail.append([f"c{k}", expected, row.get("source_ip") or "—", "对" if same else "没对上"])
    table(f"H22 同时起 {total} 条命令的归属", ["网络空间", "应看到的源地址", "宿主看到的源地址", "归属"], detail)
    passed = (complete and extra <= 50 * 10**6 and _complete(peers, total) and matched == total
              and all(r.get("ok") for r in rows))
    return passed, (f"起命令多花中位 {_ms(extra)} 毫秒，上限 50 毫秒，{counts}，"
                    f"来源归属 {matched}/{total}{_failures(rows)}")


def _agent(rows, quick, table, pct, f):
    total, duration = (2, 6) if quick else (10, 30)
    probes = [r for r in rows if r.get("part") == "reattach"]
    final = [r for r in rows if r.get("part") == "finish"]
    generation = next((r["expected_generation"] for r in rows if r.get("expected_generation")), None)
    matched = []
    detail = []
    for r in probes:
        same = generation and r.get("generation") == r.get("expected_generation") == generation
        ok = r.get("ok") and same and r.get("running")
        if ok:
            matched.append(r)
        detail.append([r.get("rep"), r.get("generation") or "—", f(r.get("lines_before_kill")), f(r.get("lines")),
                       "接回" if ok else "失败"])
    table(f"H24 命令运行中杀代理再接回（{duration} 秒命令）", ["轮次", "客机代号", "杀前行数", "接回行数", "结果"], detail)
    finished = (len(final) == 1 and final[0].get("ok") and bool(generation) and final[0].get("generation") == generation
                and final[0].get("cmd_rc") == "done" and final[0].get("lines") == duration)
    output = f"{duration} 行齐全且 done" if finished else "缺完整的命令输出"
    passed = _complete(probes, total) and len(matched) == total and finished and all(r.get("ok") for r in rows)
    return passed, f"同一代且命令仍在跑 {len(matched)}/{total} 次，{output}{_failures(rows)}"


def _gvlimits(rows, quick, table, pct, f):
    labels = {"gvisor-512": "gVisor 512 MiB", "gvisor-tmp": "gVisor 512 MiB，/tmp 先写 128 MiB",
              "gvisor-extra": "gVisor 512 MiB 加空闲沙箱内存", "runc-512": "runc 512 MiB"}
    records = {(r.get("case"), r.get("rep")): r for r in rows if r.get("part") == "limit"}
    detail = []
    missing = 0

    def mib(value):
        return f(value / (1 << 20)) if value is not None else "—"

    for case, label in labels.items():
        for rep in range(1, 2 if quick else 3):
            r = records.get((case, rep), {})
            status = "触发 OOM" if r.get("ok") else ("失败" if r else "没数")
            missing += not r
            detail.append([label, rep, mib(r.get("idle_bytes")), mib(r.get("limit_bytes")), f(r.get("alloc_mib")),
                           mib(r.get("peak_bytes")), status])
    table("H25 gVisor 撞内存额度（MiB）",
          ["做法", "轮次", "测得的空闲沙箱内存", "实设额度", "最后报出的分配量", "memory.peak", "结果"], detail)
    got = sum(1 for d in detail if d[-1] == "触发 OOM")
    return got > 0, f"只交数字：四种额度下能分配的量和峰值见表（gVisor 里最后报出的分配量可能偏小，以 memory.peak 为准），缺 {missing} 轮{_failures(rows)}"


CLOCK_KINDS = ("fc-cold", "fc-snap", "ch-cold", "ch-snap-cow", "gvisor-systrap", "kata-clh")
SNAP_KINDS = ("fc-snap", "ch-snap-cow")


def _clock(rows, quick, table, pct, f):
    idle, reps = (3, 3) if quick else (60, 20)
    groups = defaultdict(list)
    for r in rows:
        if r.get("part") == "sample" and r.get("ok") and all(
                isinstance(r.get(k), int) for k in ("offset_ns", "uncertainty_ns", "bound_ns")):
            groups[(r.get("kind"), r.get("phase"))].append(r)
    detail = []
    missing = []
    over = []
    for kind in CLOCK_KINDS:
        first = None
        for phase, label in (("start", "启动后"), ("idle", f"闲 {idle} 秒后")):
            rs = groups[(kind, phase)]
            offset = median([r["offset_ns"] for r in rs]) if rs else None
            absolute = max((abs(r["offset_ns"]) for r in rs), default=None)
            uncertainty = max((r["uncertainty_ns"] for r in rs), default=None)
            bound = max((r["bound_ns"] for r in rs), default=None)
            delta = _delta(offset, first)
            if phase == "start":
                first = offset
            if not _complete(rs, reps):
                missing.append(f"{kind} {label} {len(rs)}/{reps}")
            if bound is not None and bound > 10**6:
                over.append(f"{kind} {label} {_ms(bound)} ms")
            note = "快照恢复后偏差至少 1 分钟" if kind in SNAP_KINDS and absolute is not None and absolute >= 60 * 10**9 else "—"
            detail.append([kind, label, f"{len(rs)}/{reps}", _ms(offset), _ms(absolute), _ms(uncertainty), _ms(bound), _ms(delta), note])
    table("H26 同机墙钟（毫秒，跨机器未测）",
          ["隔离", "时点", "有效样本", "偏差中位", "最大绝对偏差", "最大不确定度",
           "最大误差上界", "相对启动后的中位偏差变化", "备注"], detail)
    issues = []
    if missing:
        issues.append("样本缺失或重复 " + "、".join(missing))
    if over:
        issues.append("超过 1 毫秒 " + "、".join(over))
    why = "，".join(issues) or "所有同机样本的绝对偏差加不确定度都 ≤ 1 毫秒"
    passed = not issues and all(r.get("ok") for r in rows)
    return passed, f"{why}{_failures(rows)}。跨机器未测"


def render(load, table, pct, f, gates):
    for name, hyp, section in (("wall", "H6", _wall), ("cmdnet", "H22", _cmdnet), ("agent", "H24", _agent),
                               ("gvlimits", "H25", _gvlimits), ("clock", "H26", _clock)):
        runs = defaultdict(list)
        for r in load(name):
            runs[(r.get("run"), bool(r.get("quick")))].append(r)
        if not runs:
            if hyp == "H25":
                gates[hyp] = (True, "只交数字：本次没有内存额度记录")
            elif hyp == "H26":
                gates[hyp] = (False, "缺同机的墙钟记录。跨机器未测")
            else:
                gates[hyp] = (False, f"没有 {name} 实验的记录")
            continue
        verdicts = []
        for (run, quick), rows in runs.items():
            if len(runs) > 1:
                print(f"\n{hyp} 运行 {run or '无编号'}（{'快跑' if quick else '完整'}）")
            passed, why = section(rows, quick, table, pct, f)
            verdicts.append((passed, why if len(runs) == 1 else f"{run or '无编号'} {why}"))
        reason = " / ".join(why for _, why in verdicts)
        if hyp == "H25" and len(verdicts) > 1:
            reason = f"只交数字：{reason}"
        gates[hyp] = (all(passed for passed, _ in verdicts), reason)
