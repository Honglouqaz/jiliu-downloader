#!/usr/bin/env python3
"""完成动作回归（P0-6）。

    python tests/test_finish.py

绝不真的关机：execute 被替换成记录器，只验证「什么时候会触发」。
"""
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import finish  # noqa: E402

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


class FakeTM:
    def __init__(self, tasks):
        self._t = tasks

    def list(self):
        return self._t


def tm(*statuses):
    return FakeTM([{"status": s} for s in statuses])


fired = []
_orig_execute = finish.execute   # 留着末尾恢复，用来测参数校验
finish.execute = lambda action, on_exit=None: (fired.append(action), {"ok": True})[1]

# ----------------------------------------------------------------------
print("== 触发判定 ==")
check("none 不触发", finish.should_fire({"finish_action": "none"}, tm("done")) is False)
check("有任务在跑不触发",
      finish.should_fire({"finish_action": "shutdown"}, tm("done", "running")) is False)
check("有任务排队不触发",
      finish.should_fire({"finish_action": "shutdown"}, tm("queued")) is False)
check("全完成才触发",
      finish.should_fire({"finish_action": "shutdown"}, tm("done", "done")) is True)
check("有失败也算结束",
      finish.should_fire({"finish_action": "shutdown"}, tm("done", "error")) is True)
check("重新入队后不再触发",
      finish.should_fire({"finish_action": "shutdown"}, tm("done", "done", "queued")) is False)
check("未知动作不触发",
      finish.should_fire({"finish_action": "explode"}, tm("done")) is False)

# ----------------------------------------------------------------------
print("== 倒计时 ==")
finish.reset()
cfg = {"finish_action": "shutdown", "finish_delay": 5}
armed = finish.arm(cfg, tm("done"))
check("满足条件时安排执行", armed is True)
st = finish.status()
check("status 显示已安排", st["armed"] is True, str(st))
check("动作名带中文", st["action_text"] == "关机", st["action_text"])
check("剩余秒数合理", 3 <= st["seconds_left"] <= 5, str(st["seconds_left"]))
check("重复安排被拒", finish.arm(cfg, tm("done")) is False)

finish.cancel()
st = finish.status()
check("取消后不再 armed", st["armed"] is False)
time.sleep(0.2)
check("取消后没有真的执行", fired == [], str(fired))

# 到期真的会调 execute（delay=0.25 秒）
finish.reset()
short = {"finish_action": "lock", "finish_delay": 0}
finish.arm(short, tm("done"))
time.sleep(0.5)
check("到期执行一次", fired == ["lock"], str(fired))
check("执行后 armed 归位", finish.status()["armed"] is False)
check("fired 标记置位", finish.status()["fired"] is True)

# 再次调用不会重复执行
finish.arm(short, tm("done"))
time.sleep(0.3)
check("第二轮还能再触发一次（用户又下了一轮）", fired == ["lock", "lock"], str(fired))

# ----------------------------------------------------------------------
print("== 状态描述 ==")
d = finish.describe({"finish_action": "shutdown", "finish_delay": 60})
check("动作列表 6 项", len(d["actions"]) == 6, str(len(d["actions"])))
check("当前动作正确", d["current"] == "shutdown")
check("包含状态", "status" in d and "armed" in d["status"])

# ----------------------------------------------------------------------
print("== 中止接口 ==")
r = finish.abort_shutdown()
check("abort_shutdown 不抛异常", isinstance(r, dict) and "ok" in r, str(r))
finish.execute = _orig_execute      # 恢复真实实现
r = finish.execute("none")
check("execute(none) 是空操作", r["ok"] is True)
r = finish.execute("nonsense")
check("未知动作被拒", r["ok"] is False, str(r))

print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
