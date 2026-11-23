#!/usr/bin/env python3
"""轻量唤起器（jiliu_wake.py）的回归测试。

这层薄壳存在的意义就是**快**，所以它出问题的两种典型方式都很隐蔽：

  1) **静默失败**：windowed 进程没有控制台，异常退出等于"点了没反应"。
     用户不知道发生了什么事，我们也查不到。所以任何失败路径都必须
     留一行日志 —— 这一组专门验它真的写了。
  2) **多开实例**：热投递成功之后又去拉主程序，就会有两个进程抢同一个
     tasks.json，任务互相覆盖（用户看到的是"下载记录莫名少了"）。
     所以每条成功分支都要验"没有去 launch"。

主程序定位（同目录优先 / 注册表兜底 / 都没有就返回 3）也在这一组里。

    python tests/test_wake.py
"""
# -*- coding: utf-8 -*-
import io
import os
import pathlib
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 隔离状态目录：必须在 import jiliu 之前（config.HOME 是模块级常量），
# 否则测试会往真实的 <安装目录>/data 里写日志。
_TMP = tempfile.mkdtemp(prefix="jiliu-wake-")
os.environ["JILIU_HOME"] = _TMP
# 注册表隔离：真实机器上 JiliuPath 可能指向已安装的汲流。
# 不做这一步，测试「找不到主程序」的分支会真的去启动本机的 exe，
# 结果既不稳定（有安装就失败）又危险（测试拉起 GUI 进程）。
os.environ["JILIU_URIKEY_SUFFIX"] = "__waketest"
os.environ["JILIU_NO_REGISTRY"] = "1"

import jiliu_wake as wake  # noqa: E402
from jiliu import urltake as u  # noqa: E402

ok = fail = 0


def check(name: str, cond, detail="") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {name}")
    else:
        fail += 1
        print(f"  FAIL {name}   {detail!r}")


# ================================================================ 夹具
_REAL_URT = wake.urltake
_REAL_LAUNCH = wake._launch
_REAL_BASE = wake._base_dir

launched: list = []


def fake_launch(spec: str) -> int:
    launched.append(spec)
    return 0


def stub(**kw):
    """造一个只实现被用到方法的 urltake 替身，并记下调用顺序。"""
    calls: list = []

    class S:
        FLAG = u.FLAG
        CLASS_KEY = u.CLASS_KEY

        def split_argv(self, argv):
            calls.append("split")
            return kw.get("spec", ""), list(kw.get("rest") or [])

        def parse(self, spec):
            calls.append("parse")
            return kw.get("info", {"ok": True, "url": "https://example.com/a.zip"})

        def read_runtime(self):
            calls.append("read_runtime")
            return kw.get("runtime")

        def scan_instance(self):
            calls.append("scan_instance")
            return kw.get("scanned", 0)

        def handoff(self, info):
            calls.append("handoff")
            return kw.get("hit", {"ok": False, "reason": "no-instance"})

    s = S()
    s.calls = calls        # type: ignore[attr-defined]
    return s


def run(**kw) -> int:
    """用替身跑一次 main()，返回退出码。"""
    launched.clear()
    wake.urltake = stub(**kw)      # type: ignore[assignment]
    wake._launch = fake_launch     # type: ignore[assignment]
    try:
        return wake.main()
    finally:
        wake.urltake = _REAL_URT   # type: ignore[assignment]
        wake._launch = _REAL_LAUNCH  # type: ignore[assignment]


# ================================================================ 1 分支
print("\n[1] 分支选择")

rc = run(spec="")
check("没有 --open-url → 拉起主程序", rc == 0 and launched == [""], (rc, launched))

rc = run(spec="jiliu://wake", runtime={"port": 9000, "pid": 1},
         info={"ok": False, "wake": True})
check("wake 且已有实例 → 直接退出", rc == 0 and launched == [], (rc, launched))

rc = run(spec="jiliu://wake", runtime=None, info={"ok": False, "wake": True})
check("wake 且没有实例 → 拉起主程序（只开窗）",
      rc == 0 and launched == [""], (rc, launched))

# runtime.json 可能被清掉、或者里面的 pid 是上一个进程的 —— 这时
# read_runtime 返回 None，但实例确实在跑。只认 read_runtime 的话，
# 用户每点一次就多开一个窗口，两个进程抢同一份 tasks.json。
rc = run(spec="jiliu://wake", runtime=None, scanned=8789,
         info={"ok": False, "wake": True})
check("wake 且 runtime 丢了但端口扫到实例 → 不重复启动",
      rc == 0 and launched == [], (rc, launched))

rc = run(spec="jiliu://download?url=https%3A%2F%2Fa%2Fb.zip",
         info={"ok": True, "url": "https://a/b.zip"},
         hit={"ok": True, "mode": "reused"})
check("热投递成功 → 退出且不再开第二个实例", rc == 0 and launched == [], (rc, launched))

rc = run(spec="jiliu://download?url=https%3A%2F%2Fa%2Fb.zip",
         info={"ok": True, "url": "https://a/b.zip"},
         hit={"ok": False, "reason": "no-instance"})
check("热投递失败 → 冷启动并带上协议串",
      rc == 0 and launched == ["jiliu://download?url=https%3A%2F%2Fa%2Fb.zip"],
      (rc, launched))

rc = run(spec="jiliu://download?url=javascript%3Aalert(1)",
         info={"ok": False, "error": "不支持的协议"})
check("非法链接 → 交给主程序去报错（界面里看得到原因）",
      rc == 0 and launched == ["jiliu://download?url=javascript%3Aalert(1)"],
      (rc, launched))


# ================================================================ 2 失败留痕
print("\n[2] 失败留痕（windowed 下这是唯一的排查线索）")

log_path = pathlib.Path(_TMP) / "logs" / "wake.log"
if log_path.is_file():
    log_path.unlink()
rc = run(spec="jiliu://download?url=javascript%3Aalert(1)",
         info={"ok": False, "error": "不支持的协议"})
check("拒绝的链接写进了 wake.log",
      log_path.is_file() and "不支持的协议" in log_path.read_text(encoding="utf-8"),
      log_path.read_text(encoding="utf-8") if log_path.is_file() else "(没有文件)")

log_path.unlink(missing_ok=True)
rc = run(spec="jiliu://download?url=https%3A%2F%2Fa%2Fb.zip",
         info={"ok": True, "url": "https://a/b.zip"},
         hit={"ok": False, "reason": "no-instance"})
txt = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
check("冷启动也留一行（说明为什么没走热路径）", "no-instance" in txt, txt)

# 成功路径不写日志：这个文件不该在正常情况下变大
log_path.unlink(missing_ok=True)
run(spec="jiliu://download?url=https%3A%2F%2Fa%2Fb.zip",
    info={"ok": True, "url": "https://a/b.zip"}, hit={"ok": True})
check("成功时不写日志", not log_path.is_file(), "文件被创建了")

# 超过 64KB 会重开，避免长期使用无限增长
log_path.parent.mkdir(parents=True, exist_ok=True)
log_path.write_text("x" * 70000, encoding="utf-8")
wake._log("重开后第一行")
check("超过 64KB 自动重开",
      log_path.read_text(encoding="utf-8").strip().endswith("重开后第一行")
      and log_path.stat().st_size < 1024,
      log_path.stat().st_size)


# ================================================================ 3 主程序定位
print("\n[3] 主程序定位")

fake = pathlib.Path(_TMP) / "app"
fake.mkdir(parents=True, exist_ok=True)
old_base = wake._base_dir
wake._base_dir = lambda: fake        # type: ignore[assignment]
try:
    got = wake._main_exe()
    check("同目录没有 jiliu.exe → 返回预期路径（由调用方判定失败）",
          got == fake / "jiliu.exe", got)

    (fake / "jiliu.exe").write_text("", encoding="utf-8")
    check("同目录有 jiliu.exe → 就用它",
          wake._main_exe() == fake / "jiliu.exe", wake._main_exe())
finally:
    wake._base_dir = old_base        # type: ignore[assignment]

# 真跑一次找不到主程序的路径：必须返回 3（而不是 0），
# 0 会被上层当成"成功"，就什么都不提示了
wake._base_dir = lambda: pathlib.Path(_TMP) / "nowhere"   # type: ignore[assignment]
try:
    rc = wake._launch("")
    check("找不到主程序 → 返回 3 且留痕", rc == 3, rc)
finally:
    wake._base_dir = old_base        # type: ignore[assignment]

check("源码态 _base_dir 是仓库根",
      _REAL_BASE() == pathlib.Path(__file__).resolve().parent.parent,
      _REAL_BASE())


# ================================================================ 4 与真 urltake 的配合
print("\n[4] 换回真 urltake 后仍能工作")

# 这一条防的是"替身测得好好的，真模块签名对不上"——
# 用真 urltake 走一遍无实例的冷路径（不启动真进程，_launch 仍是替身）
launched.clear()
wake.urltake = _REAL_URT
wake._launch = fake_launch
try:
    real_spec = u.build("https://example.com/a+b.zip", title="a+b")
    check("真 build 出来的串能被 split 摘出来",
          u.split_argv([u.FLAG, real_spec])[0] == real_spec, real_spec)
    info = u.parse(real_spec)
    check("真 parse 认得自己 build 的串", info.get("ok"), info)
    check("url 里的 + 没被吃掉",
          "+" in (info.get("url") or ""), info.get("url"))
finally:
    wake._launch = _REAL_LAUNCH

shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {ok}/{ok + fail} 通过")
sys.exit(1 if fail else 0)
