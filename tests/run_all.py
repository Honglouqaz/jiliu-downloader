#!/usr/bin/env python3
"""一把跑完汲流的所有测试。

    python tests/run_all.py            # 全跑
    python tests/run_all.py resource   # 只跑名字里带 resource 的

会自动把前置条件准备好 —— 造样本、起三个本地测试服务器，
跑完再收干净。以前这一步靠人手做，漏了就是满屏 10061，
看着像代码坏了，其实只是服务没开。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _fixtures import Servers, ensure_samples  # noqa: E402

SUITES = ["test_protocols.py", "test_engine.py", "test_aria2.py", "test_video.py",
          "test_resource.py", "test_pipeline.py", "test_panel.py",
          "test_platform.py"]


def main(argv):
    want = argv[0].lower() if argv else ""
    files = [f for f in SUITES if os.path.isfile(os.path.join(HERE, f))
             and (not want or want in f.lower())]
    if not files:
        print("没有匹配的测试文件")
        return 1

    print("=" * 72)
    print("  准备前置条件")
    print("=" * 72)
    ensure_samples()
    try:
        servers = Servers().start()
    except RuntimeError as exc:
        print(f"  !! {exc}")
        return 1

    total_pass = total_fail = 0
    summary = []
    try:
        for f in files:
            print("=" * 72)
            print("  " + f)
            print("=" * 72)
            t0 = time.monotonic()
            r = subprocess.run([sys.executable, os.path.join(HERE, f)],
                               capture_output=True, text=True, cwd=ROOT,
                               encoding="utf-8", errors="replace")
            out = (r.stdout or "") + (r.stderr or "")
            for line in out.splitlines():
                if re.search(r"\[FAIL\]|未通过|Traceback|Error:", line):
                    print("  " + line.strip())
            m = re.search(r"结果[:：]\s*(\d+)/(\d+)\s*通过", out)
            if m:
                p, n = int(m.group(1)), int(m.group(2))
                total_pass += p
                total_fail += n - p
                ok = "OK  " if p == n else "FAIL"
                summary.append(f"  [{ok}] {f:24s} {p:3d}/{n:3d}   {time.monotonic()-t0:.1f}s")
            else:
                tail = [l for l in out.strip().splitlines() if l.strip()][-3:]
                summary.append(f"  [ERR ] {f:24s} 无法解析结果 rc={r.returncode}")
                for l in tail:
                    summary.append("         " + l[:160])
    finally:
        servers.stop()

    print("\n" + "=" * 72)
    print("总览")
    print("=" * 72)
    for s in summary:
        print(s)
    print("-" * 72)
    print(f"合计: {total_pass}/{total_pass + total_fail} 通过"
          + ("" if total_fail == 0 else f"，{total_fail} 项失败"))
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
