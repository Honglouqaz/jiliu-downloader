#!/usr/bin/env python3
"""真实走一遍：让 Windows 自己按注册表启动协议处理器。

前面所有测试都是"直接执行 jiliu-wake.exe"。但用户点网页链接时，是
**系统 shell 读注册表**去拉程序 —— 中间隔着 URL Protocol 键、命令模板、
working directory 这些环节，任何一处写错都表现为"点了没反应"，而直接
执行 exe 的测试一个都发现不了。

    python scripts/real_protocol_check.py

会真的开一个汲流窗口，测完自己关掉。
"""
import os
import pathlib
import subprocess
import sys
import time
import json
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jiliu import urltake  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {name}")
    else:
        fail += 1
        print(f"  FAIL {name}   {extra!r}")


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def living_port(span=4):
    """扫一小段就够：顺延是一格一格来的，扫 16 个只是白白多等。"""
    return urltake.scan_instance(start=8788, span=span)


def jiliu_pids():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq jiliu.exe"],
                         capture_output=True)
    txt = out.stdout.decode("gbk", "replace")
    pids = []
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() == "jiliu.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


print("[0] 环境检查")
st = urltake.status()
print(f"  注册的处理器: {st.get('handler') or '(空)'}")
print(f"  是否注册    : {st.get('registered')}  stale={st.get('stale')}")
check("注册表已注册", bool(st.get("registered")), st)
handler = str(st.get("handler") or "")
check("处理器文件存在", bool(handler) and pathlib.Path(handler).is_file(), handler)

before = jiliu_pids()
print(f"  已经在跑的 jiliu.exe: {before or '无'}")

print("\n[1] 通过系统 shell 打开 jiliu://wake（等价于点网页链接）")
t0 = time.time()
try:
    os.startfile("jiliu://wake")
    launched = True
except Exception as exc:               # noqa: BLE001
    launched = False
    print(f"  startfile 失败：{type(exc).__name__}: {exc}")
check("系统接受并派发了这个协议", launched)

port = 0
for _ in range(50):                     # 最多 25 秒
    port = living_port()
    if port:
        break
    time.sleep(0.5)
elapsed = time.time() - t0
check("汲流真的起来了", bool(port), f"{elapsed:.1f}s 内没等到")
if port:
    print(f"  起在端口 {port}，耗时 {elapsed:.1f} 秒")
    with opener().open(f"http://127.0.0.1:{port}/api/health", timeout=5) as r:
        h = json.loads(r.read().decode("utf-8"))
    check("health 认得是汲流", "汲" in str(h.get("app")) or "jiliu" in str(h.get("app")).lower(), h)
    check("只醒来、没有顺手建任务",
          not json.loads(opener().open(
              f"http://127.0.0.1:{port}/api/state", timeout=10).read().decode("utf-8")
          ).get("tasks"), "wake 建出了任务")
    check("20 秒内起来（用户能忍的上限）", elapsed < 20, f"{elapsed:.1f}s")

print("\n[2] 再点一次：不能多开一个实例")
if port:
    n_before = len(jiliu_pids())
    t1 = time.time()
    try:
        os.startfile("jiliu://wake")
    except Exception:                   # noqa: BLE001
        pass
    time.sleep(2.5)
    n_after = len(jiliu_pids())
    check("没有多出进程", n_after <= n_before, f"{n_before} → {n_after}")
    print(f"  第二次调用耗时 {time.time() - t1:.1f} 秒（含 2.5s 观察窗口）")

print("\n[3] 收尾：关掉我们自己起来的那个")
if port:
    try:
        opener().open(f"http://127.0.0.1:{port}/api/shutdown", timeout=5).read()
        print("  已请求关闭")
    except Exception as exc:            # noqa: BLE001
        print(f"  shutdown 调用失败：{exc}")
    time.sleep(1.5)
    left = jiliu_pids()
    print(f"  剩余 jiliu.exe: {left or '无'}")

print(f"\n结果: {ok}/{ok + fail} 通过")
sys.exit(1 if fail else 0)
