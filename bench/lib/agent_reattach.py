"""从宿主状态文件接回一台由 systemd 管的虚拟机。"""
import argparse
import json
import time
from pathlib import Path

import benchlib as B


def reattach(state):
    B.sh(["systemctl", "is-active", "--quiet", state["unit"]], timeout=3)
    B.api(state["api_sock"], "GET", "/api/v1/vm.info", timeout=3)
    rc, out = B.vsock_exec(state["uds"], "set -e; cat /run/berth-gen; wc -l </work/cmd.out; "
                           "if [ -f /work/cmd.rc ]; then cat /work/cmd.rc; else echo running; fi", timeout=3)
    if rc:
        raise RuntimeError(f"读客机代号和命令进度失败 rc={rc}: {out[-300:]}")
    generation, lines, status = out.strip().splitlines()
    if generation != state["generation"]:
        raise RuntimeError(f"客机代号对不上：等的是 {state['generation']}，读到 {generation}")
    if status not in ("running", "done"):
        raise RuntimeError(f"命令状态无法识别：{status!r}")
    return {"generation": generation, "lines": int(lines), "running": status == "running"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("--hold", action="store_true")
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    print(json.dumps(reattach(state)), flush=True)
    if args.hold:
        # 模拟接回后的常驻代理，给宿主时间在这一进程还活着时杀掉它。
        time.sleep(30)


if __name__ == "__main__":
    main()
