#!/usr/bin/env python3
"""把实验的原始记录（results/*.jsonl）算成表：python3 report.py <结果目录> > summary.md

分位数用最近秩法：30 次里的 p99 就是最大值。失败的次数单列，不进分位数。
最后按方案第 3 节的判据（G1～G9）逐条判过没过。Pages 的醒来上限可用环境变量 PAGES_P50_MAX、PAGES_P99_MAX（秒）换成 Pages 给的数重判。
"""
import json
import math
import os
import statistics
import sys
from collections import defaultdict

D = sys.argv[1] if len(sys.argv) > 1 else "results"


def load(name):
    """默认只取这个文件里最新一次运行（run）的样本；REPORT_ALL_RUNS=1 时全取。
    某一组补跑过的，用 REPORT_RUN_<实验名>=<运行号> 指定用哪一次。"""
    p = os.path.join(D, f"{name}.jsonl")
    if not os.path.exists(p):
        return []
    rows = [json.loads(x) for x in open(p) if x.strip()]
    if os.environ.get("REPORT_ALL_RUNS") == "1":
        return rows
    runs = [r.get("run") for r in rows if r.get("run")]
    want = os.environ.get(f"REPORT_RUN_{name}") or (max(runs) if runs else None)
    return [r for r in rows if r.get("run") == want] if want else rows


def warm_rows(rows):
    return [r for r in rows if not r.get("host_cache_cold") and not r.get("image_cache_cold")]


def need(have, expected, what):
    """该有的组合缺了就返回缺哪些（判据不能在证据不全时判过）。"""
    miss = [e for e in expected if not have.get(e)]
    return [f"{what}缺 {m}" for m in miss]


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    return xs[max(0, math.ceil(q * len(xs)) - 1)]


def f(v, unit=""):
    if v is None:
        return "—"
    if unit == "ms":
        return f"{v * 1000:.0f}" if v >= 0.1 else f"{v * 1000:.1f}"
    if unit == "MB":
        return f"{v / 1024:.1f}"  # kB -> MB
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def stat_row(rows, key, unit="ms"):
    xs = [r[key] for r in rows if r.get("ok") and isinstance(r.get(key), (int, float))]
    if not xs:
        return ["—"] * 4
    return [f(pct(xs, .5), unit), f(pct(xs, .99), unit), f(min(xs), unit), f(max(xs), unit)]


def table(title, head, rows):
    print(f"\n### {title}\n")
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in rows:
        print("| " + " | ".join(str(x) for x in r) + " |")


def group(rows, *keys):
    g = defaultdict(list)
    for r in rows:
        g[tuple(r.get(k) for k in keys)].append(r)
    return g


gates = {}

# 这次量的是哪些版本：跟 gVisor 行为有关的几项（撞上限证据、不联网、醒来时间、换代切换）都是下面这个 gVisor 版本量的
_vf = os.path.join(D, "versions.txt")
if os.path.exists(_vf):
    print("### 这次量的版本\n")
    for line in open(_vf):
        if line.strip():
            print(f"- {line.strip()}")
    _sf = os.path.join(D, "SOURCES.txt")
    if os.path.exists(_sf):
        print("\n下载件的 sha256 在结果目录的 `SOURCES.txt`（官方给了校验和的已照校验）。")
    print("\n跟 gVisor 行为有关的几项（撞上限证据、不联网、醒来时间、换代切换）都是上面这个 gVisor 版本量的，跟旧版的数对照时注明版本。")

# ---- 一 起机和醒来 ----
boot = load("boot")
if boot:
    g = group([r for r in boot if r.get("part") == "wake-dute"], "kind")
    out = []
    for (kind,), rs in g.items():
        ok = [r for r in rs if r.get("ok")]
        out.append([kind, len(ok), len(rs) - len(ok), *stat_row(rs, "t_ready")[:2], *stat_row(rs, "t_usable")[:2],
                    *stat_row(rs, "t_app")[:2], stat_row(rs, "idle_pss_kb", "MB")[0], stat_row(rs, "t_stop")[0],
                    stat_row(rs, "t_disk_prep")[0]])
    table("dute 形状：从请求到能用（毫秒；内存 MB）",
          ["起法", "成", "败", "能跑命令 p50", "p99", "盘能读 p50", "p99", "再起 Node p50", "p99", "闲时 PSS", "扔掉", "克隆盘"], out)
    snap_kinds = ("fc-snap", "ch-snap-cow", "ch-snap-copy", "ch-snap-uffd")
    best = []
    for k in snap_kinds:
        rs = g.get((k,), [])
        warm = [r["t_usable"] for r in rs if r.get("ok") and "t_usable" in r and not r.get("host_cache_cold")]
        cold = [r["t_usable"] for r in rs if r.get("ok") and "t_usable" in r and r.get("host_cache_cold")]
        fails = sum(1 for r in rs if not r.get("ok"))
        if warm:
            best.append((pct(warm, .99), k, fails, cold[0] if cold else None))
    if best:
        p99, k, fails, cold = min(best)
        gates["G1"] = (p99 <= 10 and fails == 0, f"最快的共用快照做法 {k}：宿主缓存热时盘能读 p99 {p99 * 1000:.0f} ms，"
                       f"失败 {fails} 次；第一次（宿主缓存冷）{cold * 1000:.0f} ms" if cold else
                       f"最快的共用快照做法 {k}：盘能读 p99 {p99 * 1000:.0f} ms，失败 {fails} 次")
    g = group([r for r in boot if r.get("part") == "wake-pages"], "kind")
    out = []
    for (kind,), rs in g.items():
        ok = [r for r in rs if r.get("ok")]
        out.append([kind, len(ok), len(rs) - len(ok), *stat_row(rs, "t_to_app")[:2], stat_row(rs, "app_pss_kb", "MB")[0]])
    table("Pages 形状：从请求到 Node 服务回话，带 /data（毫秒；内存 MB）", ["起法", "成", "败", "到回话 p50", "p99", "跑着时 PSS"], out)
    # Pages 的醒来上限：中位 1.0 秒、p99 1.5 秒（Pages 主会话 2026-11-20，经 berth 主会话转来）
    P50_MAX, P99_MAX = float(os.environ.get("PAGES_P50_MAX", 1.0)), float(os.environ.get("PAGES_P99_MAX", 1.5))
    # G9：Pages 的每一种醒法都要有数，逐条判（只用宿主缓存热的样本；冷的、刚写完的检查点另报）
    pages_wake = {k: [r["t_to_app"] for r in warm_rows(rs) if r.get("ok")] for (k,), rs in g.items()}
    pages_fail = {k: sum(1 for r in rs if not r.get("ok")) for (k,), rs in g.items()}
    ways, wfail = {}, {}
    for k in ("gvisor-systrap", "fc-snap", "ch-snap-cow", "kata-clh", "runc"):
        ways[f"{k} 新起"] = pages_wake.get(k, [])
        wfail[f"{k} 新起"] = pages_fail.get(k, 0)
    for (k, d), rs in group([r for r in boot if r.get("part") == "gvisor-in-vm"], "kind", "data").items():
        ways[f"{k} 里的 gVisor（/data {d}）"] = [r["t_to_app"] for r in rs if r.get("ok")]
        wfail[f"{k} 里的 gVisor（/data {d}）"] = sum(1 for r in rs if not r.get("ok"))
    for lab, cold in (("gVisor 检查点恢复（镜像在缓存里）", False), ("gVisor 检查点恢复（镜像不在缓存里）", True)):
        rs = [r for r in boot if r.get("part") == "gvisor-ckpt" and bool(r.get("image_cache_cold")) == cold]
        ways[lab] = [r["t_to_app"] for r in rs if r.get("ok")]
        wfail[lab] = sum(1 for r in rs if not r.get("ok"))
    for k in ("fc-snap", "ch-snap-cow", "gvisor-systrap"):
        rs = [r for r in boot if r.get("part") == "pause" and r.get("kind") == k]
        ways[f"{k} 内存里暂停后恢复"] = [r["t_resume_to_app"] for r in rs if r.get("ok")]
        wfail[f"{k} 内存里暂停后恢复"] = sum(1 for r in rs if not r.get("ok"))
    rows9 = []
    for lab, xs in ways.items():
        okk = bool(xs) and pct(xs, .5) <= P50_MAX and pct(xs, .99) <= P99_MAX and wfail[lab] == 0
        rows9.append([lab, len(xs), wfail[lab], f(pct(xs, .5), "ms") if xs else "—", f(pct(xs, .99), "ms") if xs else "—",
                      "过" if okk else ("没数" if not xs else "没过")])
    table(f"Pages 每种醒法（判据中位 ≤ {P50_MAX}s、p99 ≤ {P99_MAX}s，失败 0 次；毫秒）", ["醒法", "成", "败", "中位", "p99", "判"], rows9)
    bad9 = [r[0] for r in rows9 if r[5] != "过"]
    gates["G9"] = (not bad9, "全部守住" if not bad9 else "没守住或没数的：" + "、".join(bad9))
    pass
    g = group([r for r in boot if r.get("part") == "gvisor-in-vm"], "kind", "data")
    table("开着的虚拟机里起 gVisor 沙箱挂 /data（毫秒）", ["虚拟机", "/data 在哪", "成", "败", "到回话 p50", "p99", "每个沙箱客机内存 MB"],
          [[k, d, sum(1 for r in rs if r.get("ok")), sum(1 for r in rs if not r.get("ok")), *stat_row(rs, "t_to_app")[:2],
            stat_row(rs, "guest_mem_kb", "MB")[0]] for (k, d), rs in g.items()])
    ck = [r for r in boot if r.get("part") == "gvisor-ckpt"]
    if ck:
        table("gVisor 检查点：存下、恢复到回话", ["成", "败", "存 p50 ms", "存档 MB", "恢复到回话 p50 ms", "p99"],
              [[sum(1 for r in ck if r.get("ok")), sum(1 for r in ck if not r.get("ok")), stat_row(ck, "t_checkpoint")[0],
                f(pct([r["image_bytes"] / 1048576 for r in ck if r.get("ok")], .5)), *stat_row(ck, "t_to_app")[:2]]])
    g = group([r for r in boot if r.get("part") == "wake-burst"], "kind", "burst")
    if g:
        table("同时醒（毫秒，每个环境从请求到回话；不算判据）", ["做法", "几个一起", "成", "败", "中位", "p99", "最慢"],
              [[k, b, sum(1 for r in rs if r.get("ok")), sum(1 for r in rs if not r.get("ok")), *stat_row(rs, "t_to_app")[:2],
                stat_row(rs, "t_to_app")[3]] for (k, b), rs in sorted(g.items(), key=lambda kv: (kv[0][0], kv[0][1]))])
    g = group([r for r in boot if r.get("part") == "pause"], "kind")
    table("内存里暂停，恢复到 Node 回话（毫秒）", ["隔离", "暂停 p50", "p99", "恢复到回话 p50", "p99"],
          [[k, *stat_row(rs, "t_pause")[:2], *stat_row(rs, "t_resume_to_app")[:2]] for (k,), rs in g.items()])
    g = group([r for r in boot if r.get("part") == "user-memsnap"], "kind", "mode")
    table("旧做法：每人一份整机内存快照（1 GiB 脏内存）",
          ["VMM", "恢复方式", "成/败", "拍快照 p50 ms", "快照占盘 MB", "恢复到能用 p50 ms", "p99", "进程都在"],
          [[k, m, f"{sum(1 for r in rs if r.get('ok'))}/{sum(1 for r in rs if not r.get('ok'))}", stat_row(rs, "t_snap")[0],
            f(pct([r["snap_disk_bytes"] / 1048576 for r in rs if r.get("ok")], .5)), *stat_row(rs, "t_usable")[:2],
            all(r.get("procs_survived") for r in rs if r.get("ok"))] for (k, m), rs in g.items()])

# ---- 二 盘 ----
disk = load("disk")
if disk:
    g = group([r for r in disk if r.get("part") in ("move", "crash", "hotplug")], "part", "kind")
    out = []
    bad = 0
    for (part, kind), rs in g.items():
        ok = [r for r in rs if r.get("ok")]
        f1 = sum(1 for r in ok if r.get("f1_ok"))
        bad += len(ok) - f1
        keys = {"move": ("t_attach_a", "t_detach", "t_attach_b"), "crash": ("t_attach_b_after_crash",),
                "hotplug": ("t_hot_add", "t_hot_remove", "t_hot_add_other")}[part]
        out.append([part, kind, len(ok), len(rs) - len(ok), f"{f1}/{len(ok)}",
                    "；".join(f"{k} {stat_row(rs, k)[0]}" for k in keys),
                    f"{sum(1 for r in ok if r.get('f2_ok') or r.get('f2_survived'))}/{len(ok)}"])
    table("换盘（毫秒）", ["做法", "VMM", "成", "败", "fsync 过的都在", "中位数", "没 fsync 的也在"], out)
    fails = sum(1 for r in disk if r.get("part") in ("move", "crash", "hotplug") and not r.get("ok"))
    ig = [r for r in disk if r.get("part") == "integrity"]
    if ig:
        table("槽位换盘以后读到的是不是这个人的盘", ["VMM", "成", "败", "e2fsck 干净", "模板指纹对"],
              [[k, sum(1 for r in rs if r.get("ok")), sum(1 for r in rs if not r.get("ok")),
                f"{sum(1 for r in rs if r.get('fsck_rc') == 0)}/{len(rs)}", f"{sum(1 for r in rs if r.get('digest_ok'))}/{len(rs)}"]
               for (k,), rs in group(ig, "kind").items()])
    bad += sum(1 for r in ig if r.get("ok") and not (r.get("fsck_rc") == 0 and r.get("digest_ok") and r.get("token_ok")))
    miss5 = [p for p in ("move", "crash", "integrity") if not any(r.get("part") == p and r.get("ok") for r in disk)]
    fails += sum(1 for r in ig if not r.get("ok"))
    gates["G5"] = (bad == 0 and fails == 0 and not miss5, f"fsync 过的文件、模板内容或盘标记对不上 {bad} 次；测量本身失败 {fails} 次"
                   + "；突然没了那一路是'宿主还在、虚拟机管理程序崩了'，不是宿主断电"
                   + (f"；缺：{'、'.join(miss5)}" if miss5 else ""))
    for r in disk:
        if r.get("part") == "zfs-send" and r.get("ok"):
            print(f"\n- zfs 收发（第 {r['rep']} 次）：全量 {r['full_bytes'] / 1048576:.0f} MiB {r['t_full']:.1f} 秒"
                  f"（{r['full_MBps']:.0f} MiB/s）；增量 {r['inc_bytes'] / 1048576:.0f} MiB {r['t_inc']:.1f} 秒")
        if r.get("part") == "compression" and r.get("ok"):
            print(f"\n- 压缩：工作区 {r['workspace_bytes'] / 1048576:.0f} MiB；"
                  + "；".join(f"{a} 占 {(r.get(a + '_used') or 0) / 1048576:.0f} MiB（压缩比 {r.get(a + '_ratio')}，拷贝 {r.get(a + '_t_copy', 0):.1f} 秒）"
                             for a in ("off", "lz4", "zstd")))

# ---- 三 编译 ----
build = load("build")
if build:
    g = group(build, "wl", "kind")
    med = {}
    out = []
    for (wl, kind), rs in sorted(g.items()):
        ok = [r for r in rs if r.get("ok") and r.get("pin_ok", True)]
        xs = [r["t_total"] for r in ok]
        m = statistics.median(xs) if xs else None
        med[(wl, kind)] = m
        out.append([wl, kind, len(ok), len(rs) - len(ok), f(m), f(min(xs)) if xs else "—", f(max(xs)) if xs else "—",
                    f(statistics.median([r["host_cpu_s"] for r in ok if "host_cpu_s" in r])) if ok else "—",
                    ",".join(sorted({c for r in ok for c in r.get("pinned", [])}))])
    for row in out:
        base = med.get((row[0], "native"))
        m = med.get((row[0], row[1]))
        row.insert(5, f"{m / base:.2f}" if base and m else "—")
    table("编译（秒；倍数相对 native）", ["活", "隔离", "成", "败", "中位数", "倍数", "最快", "最慢", "宿主 CPU 秒", "钉的核"], out)
    gv = med.get(("npm", "gvisor-systrap"))
    vms = [v for v in (med.get(("npm", "fc-snap")), med.get(("npm", "ch-snap-cow"))) if v]
    if gv and vms:
        gates["G3"] = (gv <= 1.10 * min(vms), f"npm 那组 gVisor {gv:.1f} 秒，microVM 最快 {min(vms):.1f} 秒，{gv / min(vms):.2f} 倍")

# ---- 四 密度 ----
dens = [r for r in load("density") if r.get("part") == "summary"]
if dens:
    table("一台挤多少（MB 是每台平均）",
          ["隔离", "形状", "放了", "停在", "按 PSS 估一台最多", "每台 MemAvailable 降", "每台 MemFree 降", "每台 PSS", "每台缓存增", "放满用时 s", "全扔用时 s"],
          [[r["kind"], r["shape"], r.get("count"), r.get("stop_reason"), r.get("est_max_count", "—"), f(r.get("per_d_MemAvailable_kb"), "MB"),
            f(r.get("per_d_MemFree_kb"), "MB"), f(r.get("per_pss_kb"), "MB"), f(r.get("per_d_Cached_kb"), "MB"),
            f(r.get("t_fill")), f(r.get("t_teardown"))] for r in dens])
    pz = [r for r in dens if r.get("paused_per_d_MemAvailable_kb") is not None]
    if pz:
        table("全部在内存里暂停以后（每台平均 MB）", ["隔离", "台数", "确认暂停的", "暂停后每台 PSS", "跑着时每台 PSS", "暂停后每台 MemFree 降"],
              [[r["kind"], r["count"], r.get("paused_count"), f(r.get("paused_per_pss_kb"), "MB"), f(r.get("per_pss_kb"), "MB"),
                f(r.get("paused_per_d_MemFree_kb"), "MB")] for r in pz])
    gi = [r for r in dens if r.get("kind") == "gvisor-in-vm"]
    for r in gi:
        print(f"\n- 一台 4 核 8 GB 虚拟机里放了 {r.get('count')} 个 gVisor 沙箱（停在 {r.get('stop_reason')}），"
              f"每个占客机内存 {f(r.get('per_guest_kb'), 'MB')} MB，宿主上虚拟机的 RSS 每个涨 {f(r.get('per_vmm_rss_kb'), 'MB')} MB"
              f"（每个沙箱没有单独的资源上限）")
    # 内存口径用 PSS（冷读 2026-11-20：MemAvailable 会把共用快照的页缓存当成可用，cgroup 会把共享页记给第一台）
    per = {(r["kind"], r["shape"]): r.get("per_pss_kb") for r in dens if r.get("count") and r.get("kind") != "gvisor-in-vm"}
    gv = per.get(("gvisor-systrap", "pages"))
    vms = {k: per.get((k, "pages")) for k in ("fc-snap", "ch-snap-cow") if per.get((k, "pages"))}
    one = {}
    if boot:
        for k in ("gvisor-systrap", "fc-snap", "ch-snap-cow"):
            xs = [r["app_pss_kb"] for r in boot if r.get("part") == "wake-pages" and r.get("kind") == k and r.get("ok") and r.get("app_pss_kb")]
            one[k] = pct(xs, .5) if xs else None
    # G2：同一种虚拟机管理程序要同时满足开销（G2a）和唤醒（G2b），不拿两家各自的长处拼
    P50_MAX, P99_MAX = float(os.environ.get("PAGES_P50_MAX", 1.0)), float(os.environ.get("PAGES_P99_MAX", 1.5))
    pw = {}
    if boot:
        for k in ("fc-snap", "ch-snap-cow"):
            pw[k] = [r["t_to_app"] for r in warm_rows([r for r in boot if r.get("part") == "wake-pages" and r.get("kind") == k])
                     if r.get("ok")]
    stop_of = {(r["kind"], r["shape"]): r.get("stop_reason") for r in dens}
    cand, notes = [], []
    for k in ("fc-snap", "ch-snap-cow"):
        v, xs = vms.get(k), pw.get(k) or []
        if not gv or not v or not xs:
            notes.append(f"{k} 缺数")
            continue
        mem_ok = v <= 1.25 * gv
        wake_ok = pct(xs, .5) <= P50_MAX and pct(xs, .99) <= P99_MAX
        notes.append(f"{k}：PSS {v / gv:.2f} 倍 gVisor（密度停在 {stop_of.get((k, 'pages'))}），醒来中位 {pct(xs, .5) * 1000:.0f} ms、"
                     f"p99 {pct(xs, .99) * 1000:.0f} ms")
        if mem_ok:
            gates.setdefault("G2a", (True, ""))
        if mem_ok and wake_ok:
            cand.append(k)
    if notes:
        mem_any = any(vms.get(k) and gv and vms[k] <= 1.25 * gv for k in ("fc-snap", "ch-snap-cow"))
        gates["G2a"] = (mem_any, "；".join(notes))
        gates["G2b"] = (bool(cand), "同时满足开销和唤醒的：" + ("、".join(cand) if cand else "无") + "；" + "；".join(notes))
    ratios = []
    for shp in ("pages", "dute"):
        a, b = per.get(("fc-snap", shp)), per.get(("ch-snap-cow", shp))
        if a and b:
            ratios.append(("内存 " + shp, max(a / b, b / a)))
    if boot:
        def p99k(kinds, key):
            xs = [r[key] for r in warm_rows(boot) if r.get("part") == "wake-dute" and r.get("kind") in kinds and r.get("ok") and key in r]
            return pct(xs, .99) if xs else None
        for label, fa, cb in (("冷启 p99", ("fc-cold",), ("ch-cold",)),
                              ("快照恢复 p99", ("fc-snap",), ("ch-snap-cow", "ch-snap-copy", "ch-snap-uffd"))):
            a, b = p99k(fa, "t_usable"), p99k(cb, "t_usable")
            if a and b:
                ratios.append((label, max(a / b, b / a)))
    have4 = {l.split()[0] if l.startswith("内存") else l: True for l, _ in ratios}
    miss4 = [x for x in ("内存", "冷启 p99", "快照恢复 p99") if not any(l.startswith(x) for l, _ in ratios)]
    if ratios:
        lab, worst = max(ratios, key=lambda x: x[1])
        gates["G4"] = (worst < 10 and not miss4, "；".join(f"{l} 差 {r:.1f} 倍" for l, r in ratios) + f"（最大在{lab}）"
                       + (f"；缺：{'、'.join(miss4)}" if miss4 else ""))

# ---- 五 回收 ----
rc = load("reclaim")
if rc:
    g = group([r for r in rc if r.get("phase") not in (None, "done")], "kind", "fpr", "reclaim", "rep")
    out = []
    for (kind, fpr, recl, rep), rs in g.items():
        if rep != 0:
            continue
        by = {(r["phase"], r["t"]): r for r in rs}

        def kb(ph, t):
            r = by.get((ph, t))
            if not r:
                return None
            return r.get("rss_kb") if kind.startswith(("fc", "ch")) else (r.get("cg_kb") or r.get("pss_kb"))

        idle, held, freed = kb("idle", 0), kb("held", 0), kb("freed", 30)
        opt = "回报开" if fpr else ("回报关" if fpr is False else ("回收开" if recl else ("回收关" if recl is False else "")))
        out.append([kind, opt, f(idle, "MB"), f(held, "MB"), f(freed, "MB"),
                    f"{(held - freed) / max(1, held - idle):.0%}" if None not in (idle, held, freed) else "—",
                    f(kb("balloon", 20), "MB"), f(kb("cached", 0), "MB"), f(kb("dropped", 30), "MB")])
    table("内存回收（宿主上这台占的 MB）", ["隔离", "开关", "闲着", "占着 1G", "放掉 30 秒后", "收回比例", "气球 20 秒后", "读满缓存", "丢缓存 30 秒后"], out)

# ---- 六 用量 ----
us = load("usage")
if us:
    g = group(us, "kind")
    out = []
    g6_bad = []
    for (kind,), rs in g.items():
        ok = [r for r in rs if r.get("ok")]
        if not ok:
            out.append([kind, 0, len(rs)] + ["—"] * 9)
            g6_bad.append(f"{kind} 全部失败")
            continue

        def m(fn):
            xs = [fn(r) for r in ok]
            xs = [x for x in xs if x is not None]
            return statistics.median(xs) if xs else None

        cpu = m(lambda r: r["host_cpu_s"] / r["guest_cpu_s"] if r.get("guest_cpu_s") and r.get("host_cpu_s") else None)
        mem = m(lambda r: (r.get("mem_cg_kb_delta_kb") if not kind.startswith(("fc", "ch")) else r.get("mem_rss_kb_delta_kb")) / r["mem_expected_kb"])
        dsk = m(lambda r: r["zfs_logicalreferenced_delta"] / r["disk_expected"] if "zfs_logicalreferenced_delta" in r else None)
        wr = m(lambda r: r["zfs_written_delta"] / r["disk_expected"] if "zfs_written_delta" in r else None)
        net = m(lambda r: r["net_dev_rx_bytes"] / r["net_app_bytes"] if r.get("net_app_bytes") else None)
        # 包头：以太网 14 + IPv4 20 + TCP 带时间戳 32 = 每包 66 字节；扣掉以后应当几乎对上
        netx = m(lambda r: (r["net_dev_rx_bytes"] - 66 * r["net_dev_rx_packets"]) / r["net_app_bytes"] if r.get("net_app_bytes") else None)
        cgw = m(lambda r: r["cg_io_wbytes_delta"] / r["disk_expected"] if "cg_io_wbytes_delta" in r else None)
        trim = m(lambda r: r.get("zfs_referenced_after_trim_delta"))
        idle = m(lambda r: r.get("idle_cpu_ms_per_s"))
        has_net = kind in ("runc", "gvisor-systrap", "fc-cold", "ch-cold")
        checks = [("CPU", cpu, .05), ("内存", mem, .10), ("盘", dsk, .05)] + ([("网卡（扣包头）", netx, .05)] if has_net else [])
        for name, v, tol in checks:
            if not v:
                g6_bad.append(f"{kind} 的{name}没数")
            elif abs(v - 1) > tol:
                g6_bad.append(f"{kind} 的{name} {v:.3f}")
        out.append([kind, len(ok), len(rs) - len(ok), f(cpu), f(mem), f(dsk), f(wr), f(cgw), f(net) if has_net else "没测",
                    f(netx) if has_net else "没测", f(trim / 1048576 if trim is not None else None), f(idle)])
    table("用量：宿主记的 ÷ 环境里实际的", ["隔离", "成", "败", "CPU", "内存", "盘（逻辑）", "盘（ZFS 实写）", "盘（cgroup 写）",
                                         "网卡/应用层", "网卡扣包头", "trim 后池里变化 MiB", "闲时 CPU ms/秒"], out)
    for k in ("native", "runc", "gvisor-systrap", "kata-clh", "fc-cold", "ch-cold"):
        if (k,) not in g:
            g6_bad.append(f"{k} 没跑")
    gates["G6"] = (not g6_bad, "CPU、盘 ±5%，内存 ±10%，网卡扣掉包头后 ±5%；" + ("超出或没数的：" + "、".join(g6_bad) if g6_bad else "都在线内"))

# ---- 七 超线程 ----
smt = load("smt")
if smt:
    table("超线程", ["状态", "台数", "总墙钟 s", "一小时编几份", "成"],
          [[r.get("smt"), r.get("n"), f(r.get("wall")), f(r.get("builds_per_hour")), r.get("ok")] for r in smt])

# ---- 八 NetPlay ----
npl = load("netplay")
if npl:
    rt = group([r for r in npl if r.get("part") == "rtt"], "a", "b")
    out = []
    worst = {}
    for (a, b), rs in rt.items():
        ok = [r for r in rs if r.get("ok")]
        mx = lambda k: max((r[k] for r in ok if k in r), default=None)  # 跨轮取最差的那一轮
        out.append([f"{a} ↔ {b}", len(ok), len(rs) - len(ok), f(mx("tcp_p50")), f(mx("tcp_p90")), f(mx("tcp_p99")),
                    f(mx("tcp_p999")), f(mx("tcp_max")), f(mx("udp_p50")), f(mx("udp_p99")), f(mx("udp_p999")),
                    f(mx("udp_max")), f(sum(r.get("udp_lost", 0) for r in ok))])
        if a != "loopback":
            worst[(a, b)] = (ok, len(rs) - len(ok))
    gw = [r for r in npl if r.get("part") == "gateway"]
    if gw:
        rows_gw, bad_gw = [], []
        for path in ("via-gateway", "to-gateway-host", "direct-to-rule"):
            for phase in ("idle", "busy"):
                for size in (1024, 16384, 65536):
                    rs = [r for r in gw if r.get("path") == path and r.get("phase") == phase and r.get("size") == size]
                    if not rs:
                        continue
                    ok = [r for r in rs if r.get("ok")]
                    p50 = max((r["p50"] for r in ok), default=None)
                    p99 = max((r["p99"] for r in ok), default=None)
                    rows_gw.append([path, phase, size, len(ok), len(rs) - len(ok), f(p50), f(p99),
                                    f(max((r["p999"] for r in ok), default=None)), f(max((r["max"] for r in ok), default=None))])
                    if path == "via-gateway" and phase == "idle" and (not ok or len(ok) < len(rs) or p50 > 500 or p99 > 2000):
                        bad_gw.append(f"{size} 字节（p50 {f(p50)}、p99 {f(p99)} 微秒，成 {len(ok)}/{len(rs)}）")
        table("H19 的量法：netplay 一侧经 Pages 规则网关进 gVisor 到规则回包，扣掉规则计算（微秒，几轮里最差的那一轮；同机近似）",
              ["路径", "机器", "回包字节", "成", "败", "p50", "p99", "p99.9", "最大"], rows_gw)
        need = [s for s in (1024, 16384, 65536) if not any(r.get("path") == "via-gateway" and r.get("phase") == "idle" and r.get("size") == s and r.get("ok") for r in gw)]
        gates["G7"] = (not bad_gw and not need, ("经网关这一段三档回包都在 p50 0.5 毫秒、p99 2 毫秒以内" if not bad_gw and not need
                       else "没过或没数的：" + "、".join(bad_gw + [f"{s} 字节没数" for s in need]))
                       + "；机器忙时的 p99 另报在表里；一台机器上是同机近似，真实部署是同机房两台")
    table("同机两台环境之间一次来回（微秒，几轮里最差的那一轮；四种隔离两两直连，只作诊断）",
          ["配对", "成", "败", "TCP p50", "p90", "p99", "p99.9", "最大", "UDP p50", "p99", "p99.9", "最大", "UDP 超时"], out)
    bad = []
    for pair in [("runc", "runc"), ("gvisor-systrap", "gvisor-systrap"), ("kata-clh", "kata-clh"), ("fc-cold", "fc-cold"),
                 ("ch-cold", "ch-cold"), ("ch-cold", "gvisor-systrap")]:
        ok, nf = worst.get(pair, ([], 0))
        name = f"{pair[0]}↔{pair[1]}"
        if not ok or nf:
            bad.append(f"{name} 没数或有失败")
            continue
        p50 = max(max(r["tcp_p50"], r["udp_p50"]) for r in ok)
        p99 = max(max(r["tcp_p99"], r["udp_p99"]) for r in ok)
        lost = sum(r.get("udp_lost", 0) for r in ok) / max(1, sum(r.get("udp_n", 0) + r.get("udp_lost", 0) for r in ok))
        if p50 > 500 or p99 > 2000 or lost > 0.001:
            bad.append(f"{name}（p50 {p50:.0f}、p99 {p99:.0f} 微秒，UDP 超时 {lost:.2%}）")
    if not gw:  # 没跑经网关那一段时，退回按两两直连判（旧口径）
        gates["G7"] = (not bad, "（旧口径：两两直连）都在 p50 0.5 毫秒、p99 2 毫秒以内，UDP 超时不过千分之一" if not bad else "（旧口径）没过的：" + "、".join(bad))
    gd = group([r for r in npl if r.get("part") == "godot"], "kind")
    out = []
    med = {}
    for (k,), rs in gd.items():
        ok = [r for r in rs if r.get("ok")]
        med[k] = statistics.median([r["t_cold_total"] for r in ok]) if ok else None
        out.append([k, len(ok), len(rs) - len(ok), *stat_row(rs, "t_env")[:1], *stat_row(rs, "t_godot_first")[:1],
                    *stat_row(rs, "t_cold_total")[:2], stat_row(rs, "t_godot_warm")[0]])
    table("Godot 4.7.2 无界面版冷启动（毫秒）", ["隔离", "成", "败", "新环境能用 p50", "Godot 第一次 p50", "合计 p50", "合计 p99", "再跑一次 p50"], out)
    miss8 = [k for k in ("native", "runc", "gvisor-systrap", "gvisor-kvm", "kata-clh", "fc-snap", "ch-snap-cow", "gvisor-in-vm")
             if not med.get(k)]
    if miss8:
        gates["G8"] = (False, "没数的：" + "、".join(miss8))
    elif med.get("gvisor-systrap"):
        base = med["gvisor-systrap"]
        over5 = [k for k, v in med.items() if v and v > 5]
        slower = [k for k in ("fc-snap", "ch-snap-cow", "kata-clh", "gvisor-in-vm") if med.get(k) and med[k] > base]
        gates["G8"] = (not over5 and not slower,
                       f"宿主 gVisor 合计中位数 {base:.2f} 秒；超 5 秒的：{'、'.join(over5) or '无'}；比 gVisor 慢的替换做法：{'、'.join(slower) or '无'}")

# ---- 蓝图的待验假设（main 上的 docs/design/hypotheses.md，提交 1b64534）：能从上面几组的数判的，在这里判 ----
hyp = []  # (编号, 过没过或 None=只交数字, 说明)
if boot:
    r100 = {k: [r for r in boot if r.get("part") == "restore100" and r.get("kind") == k] for k in ("ch-snap-cow", "fc-snap")}
    uf = [r for r in boot if r.get("part") == "uffd-fault"]
    selfs = [r["vmm_self_kb"] for r in r100["ch-snap-cow"] if r.get("ok") and r.get("vmm_self_kb")]
    ch_ok = sum(1 for r in r100["ch-snap-cow"] if r.get("ok"))
    hung = [r for r in uf if r.get("outcome") == "hung"]
    hp = [r for r in (disk or []) if r.get("part") == "hotplug"] if "disk" in dir() else []
    hp_ok = bool(hp) and all(r.get("ok") and r.get("f1_ok") for r in hp)
    need100 = 3 if any(r.get("quick") for r in boot) else 100
    uf_ok = [r for r in uf if r.get("ok")]
    if r100["ch-snap-cow"] or uf:
        hyp.append(("H1", ch_ok >= need100 and len(r100["ch-snap-cow"]) >= need100 and bool(selfs) and pct(selfs, .5) <= 80 * 1024
                    and bool(uf_ok) and not hung and hp_ok,
                    f"Cloud Hypervisor 共用快照恢复 {ch_ok}/{len(r100['ch-snap-cow'])} 次成功；管理程序自己常驻中位 "
                    f"{(pct(selfs, .5) or 0) / 1024:.1f} MB（线 80 MB）；热插的盘读写{'都对' if hp_ok else '有不对或没数'}；"
                    f"按需补页中途坏了：{'、'.join(sorted({r.get('outcome', '?') for r in uf})) or '没数'}"
                    + (f"，退成冷开机 {pct([r['t_fallback_cold'] for r in uf if r.get('ok')], .5) * 1000:.0f} ms" if any(r.get('ok') for r in uf) else "")))
    for k in ("ch-snap-cow", "fc-snap"):
        one = [r["t_usable"] for r in warm_rows([r for r in boot if r.get("part") == "wake-dute" and r.get("kind") == k]) if r.get("ok") and "t_usable" in r]
        b10 = [r.get("t_usable", r.get("t_to_app")) for r in boot if r.get("part") == "wake-burst" and r.get("shape") == "dute" and r.get("kind") == k and r.get("ok")]
        b10f = sum(1 for r in boot if r.get("part") == "wake-burst" and r.get("shape") == "dute" and r.get("kind") == k and not r.get("ok"))
        if one or b10:
            okk = bool(one) and bool(b10) and pct(one, .5) <= 2 and pct(one, .99) <= 10 and pct(b10, .5) <= 2 and pct(b10, .99) <= 10 and b10f == 0
            hyp.append((f"H2（{k}）", okk, f"单独醒 p50 {f(pct(one, .5), 'ms')}、p99 {f(pct(one, .99), 'ms')} ms；"
                        f"同时醒 10 台（到盘能读；这台 8 线程放 20 个 vCPU，算力在排队）p50 {f(pct(b10, .5), 'ms') if b10 else '—'}、p99 {f(pct(b10, .99), 'ms') if b10 else '—'} ms，失败 {b10f}"))
    for k in ("ch-snap-cow", "fc-snap"):
        pz = [r["t_resume_to_app"] for r in boot if r.get("part") == "pause" and r.get("shape") == "dute" and r.get("kind") == k and r.get("ok")]
        pzf = sum(1 for r in boot if r.get("part") == "pause" and r.get("shape") == "dute" and r.get("kind") == k and not r.get("ok"))
        hyp.append((f"H3（{k}）", bool(pz) and pzf == 0 and pct(pz, .99) <= 0.2,
                    f"dute 形状内存里暂停后接着用 p99 {f(pct(pz, .99), 'ms') if pz else '—'} ms（线 200 ms；含恢复后进环境一次 curl），"
                    f"成 {len(pz)}、败 {pzf}"))
    pw = [r for r in boot if r.get("part") == "wake-pages" and r.get("kind") == "gvisor-systrap" and r.get("ok")]
    if pw:
        xs7 = [r["t_to_app"] for r in pw]
        hyp.append(("H7", pct(xs7, .5) <= 1.0 and pct(xs7, .99) <= 1.5, f"宿主上 gVisor 新起、挂回 /data：环境起来 {f(pct([r['t_env'] for r in pw if 't_env' in r], .5), 'ms')} ms、"
                    f"主程序起来再加 {f(pct([r['t_main_launch'] for r in pw if 't_main_launch' in r], .5), 'ms')} ms、"
                    f"应用连得上中位 {f(pct(xs7, .5), 'ms')}、p99 {f(pct(xs7, .99), 'ms')} ms（线 1.0 / 1.5 秒）；"
                    "主程序起来记的是 Node 进程拉起那一刻，不等端口"))
if boot:
    ho = {w: [r for r in boot if r.get("part") == "handover" and r.get("way") == w] for w in ("berth", "pages-today")}
    if ho["berth"] or ho["pages-today"]:
        hb = [r["handover_s"] for r in ho["berth"] if r.get("ok")]
        hp_ = [r["handover_s"] for r in ho["pages-today"] if r.get("ok")]
        fails = sum(1 for w in ho for r in ho[w] if not r.get("ok"))
        um = [r["t_umount"] for r in ho["berth"] if r.get("ok") and r.get("t_umount") is not None]
        mo = [r["t_mount"] for r in ho["berth"] if r.get("ok") and r.get("t_mount") is not None]
        okk = bool(hb) and bool(hp_) and fails == 0 and pct(hb, .5) <= pct(hp_, .5) and pct(hb, .99) <= pct(hp_, .99)
        hyp.append(("H27", okk, f"换代停顿 handoverMs：berth 做法（停写、卸盘、挂盘、起新）中位 {f(pct(hb, .5), 'ms') if hb else '—'}、"
                    f"p99 {f(pct(hb, .99), 'ms') if hb else '—'} ms；同机 Pages 今天的做法（停旧、直接起新）中位 {f(pct(hp_, .5), 'ms') if hp_ else '—'}、"
                    f"p99 {f(pct(hp_, .99), 'ms') if hp_ else '—'} ms；卸盘中位 {f(pct(um, .5), 'ms') if um else '—'}、p99 {f(pct(um, .99), 'ms') if um else '—'} ms，"
                    f"挂盘中位 {f(pct(mo, .5), 'ms') if mo else '—'}、p99 {f(pct(mo, .99), 'ms') if mo else '—'} ms；失败 {fails}（不比 Pages 做法长才算过）"))
if build:
    def med_of(wl, kind):
        xs = [r["t_total"] for r in build if r.get("wl") == wl and r.get("kind") == kind and r.get("ok") and r.get("pin_ok", True)]
        return statistics.median(xs) if xs else None

    for hid, a, b, line, what in (("H5", "ch-snap-cow", "native", 1.3, "虚拟机（Cloud Hypervisor）比原生"),
                                  ("H6b", "ch-snap-cow+wall", "ch-snap-cow", 1.05, "虚拟机里再套一层墙比不套"),
                                  ("H8", "ch-snap-cow", "ch-snap-cow@raw", 1.10, "ZFS 卷比原生块设备"),
                                  ("H8b", "ch-snap-cow@enc", "ch-snap-cow", 1.10, "加密的 ZFS 卷比不加密")):
        parts, okk = [], True
        for wl in ("npm", "redis"):
            x, y = med_of(wl, a), med_of(wl, b)
            if x and y:
                parts.append(f"{wl} {x / y:.2f} 倍")
                okk = okk and x / y <= line
            else:
                parts.append(f"{wl} 没数")
                okk = False
        hyp.append((hid, okk, f"{what}：" + "，".join(parts) + f"（线 {line} 倍）"))
if disk:
    zs = [r for r in disk if r.get("part") in ("zfs-send", "zfs-send-enc") and r.get("ok")]
    if zs:
        tinc = [r["t_inc"] for r in zs if r.get("t_inc")]
        hyp.append(("H8b（传）", bool(tinc) and max(tinc) <= 60, "；".join(
            (f"明文增量 {r['inc_bytes'] / 1048576:.0f} MiB {r['t_inc']:.1f} 秒" if r.get("part") == "zfs-send"
             else f"密文原样传：全量 {r['full_bytes'] / 1048576:.0f} MiB {r['t_full']:.1f} 秒、增量 {r['inc_bytes'] / 1048576:.0f} MiB {r['t_inc']:.1f} 秒")
            for r in zs) + "；增量按 60 秒内判；同池代替跨机，网络另算"))
    sl = [r for r in disk if r.get("part") == "snap-latency" and r.get("ok")]
    if sl:
        r = sl[0]
        hyp.append(("H23", r["idle_max"] <= 0.05 and r["busy_p50"] <= 0.05, f"给工作盘打 ZFS 快照：闲着中位 {r['idle_p50'] * 1000:.0f} ms、最慢 {r['idle_max'] * 1000:.0f} ms；"
                    f"环境正在写 2 GiB 时中位 {r['busy_p50'] * 1000:.0f} ms、最慢 {r['busy_max'] * 1000:.0f} ms（线：几十毫秒，按闲着最慢和正在写的中位 ≤ 50 ms 判；"
                    "含等事务组落盘）；换机回到检查点要第二台"))
envj = os.path.join(D, "env.json")
if os.path.exists(envj):
    h = json.load(open(envj)).get("h12") or {}
    if h:
        hyp.append(("H12", bool(h.get("kvm") and h.get("vuln_readable") and h.get("nvme") and h.get("can_disable_nested")),
                    f"KVM {'有' if h.get('kvm') else '没有'}；漏洞状态{'读得出' if h.get('vuln_readable') else '读不全'}；"
                    f"本机 NVMe {'有' if h.get('nvme') else '没有（是 SATA SSD 或别的）'}；关嵌套虚拟化{'能' if h.get('can_disable_nested') else '不能'}"
                    f"（原来 {h.get('nested_before')}，关后 {h.get('nested_off')}，改回 {h.get('nested_restored')}）"))
if us:
    devs = []
    for r in us:
        if not r.get("ok"):
            continue
        if r.get("awake_host_s") and r.get("awake_guest_s"):
            devs.append(("醒着时长", r["kind"], r["awake_guest_s"] / r["awake_host_s"]))
        key = "mem_rss_kb_delta_kb" if r["kind"].startswith(("fc", "ch")) else "mem_cg_kb_delta_kb"
        if r.get(key) and r.get("mem_expected_kb"):
            devs.append(("内存", r["kind"], r[key] / r["mem_expected_kb"]))
        if "zfs_used_delta" in r:
            devs.append(("盘实占", r["kind"], r["zfs_used_delta"] / r["disk_expected"]))
        if "zfs_logicalreferenced_delta" in r:
            devs.append(("盘", r["kind"], r["zfs_logicalreferenced_delta"] / r["disk_expected"]))
        if r.get("guest_cpu_s") and r.get("host_cpu_s"):
            devs.append(("CPU（宿主÷客机）", r["kind"], r["host_cpu_s"] / r["guest_cpu_s"]))
    if devs:
        worst = max(devs, key=lambda d: abs(d[2] - 1))
        hyp.append(("H15", abs(worst[2] - 1) <= 0.02, f"最大偏差 {worst[1]} 的{worst[0]} {worst[2]:.3f}（线 ±2%）；CPU 一项含虚拟化开销，"
                    "宿主多记的部分算不算进用户的账是计费口径的事，表在上面的用量那一节"))
if "npl" in dir() and npl:
    if "G7" in gates:
        hyp.append(("H19（同机那一半）", gates["G7"][0], "同 G7：" + gates["G7"][1] + "；同地区跨机要第二台，没测"))
    gd = [r for r in npl if r.get("part") == "godot" and r.get("ok")]
    if gd:
        base = [r["t_cold_total"] for r in gd if r.get("kind") == "gvisor-systrap"]
        rows = []
        okk = bool(base)
        for k in ("fc-snap", "ch-snap-cow", "kata-clh", "gvisor-in-vm"):
            xs = [r["t_cold_total"] for r in gd if r.get("kind") == k]
            if not xs:
                rows.append(f"{k} 没数")
                okk = False
            if xs and base:
                rows.append(f"{k} 中位 {pct(xs, .5):.2f}/p99 {pct(xs, .99):.2f} 秒")
                okk = okk and pct(xs, .5) <= pct(base, .5) and pct(xs, .99) <= pct(base, .99)
        hyp.append(("H20", okk, f"宿主 gVisor 中位 {pct(base, .5) if base else 0:.2f}/p99 {pct(base, .99) if base else 0:.2f} 秒；" + "；".join(rows)
                    + "（中位和 p99 都不比宿主 gVisor 慢才算过）"))
if dens:
    pz = [r for r in dens if r.get("paused_per_pss_kb") is not None]
    if pz:
        hyp.append(("H21", None, "；".join(f"{r['kind']} 停在内存里每台 PSS {r['paused_per_pss_kb'] / 1024:.0f} MB（放了 {r['count']} 台、确认暂停 {r.get('paused_count')}）" for r in pz)))
    hyp.append(("H4", None, "；".join(f"{r['kind']}/{r['shape']} 放了 {r.get('count')} 台（停在 {r.get('stop_reason')}，按 PSS 估最多 {r.get('est_max_count', '—')}）"
                                         for r in dens if r.get("kind") != "gvisor-in-vm") + "；满了以后明确拒要 berth 的准入，这批没有"))
if hyp:
    table("蓝图的待验假设（能在这一批判的）", ["编号", "判", "依据的数"],
          [[h, "只交数字" if okk is None else ("过" if okk else "没过"), why] for h, okk, why in hyp])
    for h, okk, why in hyp:
        gates[h] = (okk, why) if okk is not None else (True, "只交数字：" + why)

# ---- 待验假设新加的那几组（H6、H22、H24、H25、H26）：表和判据在 report_hyp.py ----
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import report_hyp  # noqa: E402
    report_hyp.render(load, table, pct, f, gates)
except ImportError:
    pass

# ---- 判据 ----
print("\n### 判据（方案第 3 节）\n")
print("| 判据 | 过没过 | 依据的数 |\n|---|---|---|")
for k in ["G1", "G2a", "G2b", "G3", "G4", "G5", "G6", "G7", "G8", "G9"] + sorted(k for k in gates if k.startswith("H")):
    if k in gates:
        ok, why = gates[k]
        print(f"| {k} | {'过' if ok else '没过'} | {why} |")
    else:
        print(f"| {k} | 没数 | 这组没跑成 |")
