"""全部任务完成后的动作：关机 / 休眠 / 睡眠 / 锁屏 / 退出程序。

设计上的两个关键点
  1. **只在「全部完成」那一刻触发一次**。每个任务结束都会回调到这里，
     没有一次性标记的话，批量下 20 个文件会触发 20 次关机。
  2. **必须能取消**。用 `shutdown /s /t 60` 而不是立即关机 ——
     Windows 给了 60 秒反悔窗口，`shutdown /a` 就能中止。
     60 秒同时也覆盖了「用户正好路过看到倒计时」的情形。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

ACTIONS = ("none", "shutdown", "hibernate", "sleep", "lock", "exit")

ACTION_TEXT = {
    "none": "什么都不做",
    "shutdown": "关机",
    "hibernate": "休眠",
    "sleep": "睡眠",
    "lock": "锁屏",
    "exit": "退出汲流",
}

# Windows 关机的反悔窗口
SHUTDOWN_GRACE = 60

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "armed": False,
    "action": "none",
    "at": 0.0,          # 计划执行时刻（epoch）
    "delay": 0,
    "fired": False,
}
_cancel_ev = threading.Event()
_thread: Optional[threading.Thread] = None


def _run(cmd: list) -> bool:
    try:
        flags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
        return True
    except Exception:
        return False


def execute(action: str, on_exit=None) -> Dict[str, Any]:
    """立刻执行动作。"""
    action = (action or "none").lower()
    if action not in ACTIONS:
        return {"ok": False, "error": f"未知动作：{action}"}
    if action == "none":
        return {"ok": True, "action": "none"}
    if os.name != "nt":
        return {"ok": False, "error": "只有 Windows 支持这些动作"}

    if action == "shutdown":
        ok = _run(["shutdown", "/s", "/t", str(SHUTDOWN_GRACE)])
    elif action == "hibernate":
        ok = _run(["shutdown", "/h"])
    elif action == "sleep":
        # SetSuspendState(Hibernate=False, ForceCritical=False, DisableWakeEvent=False)
        ok = _run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
    elif action == "lock":
        ok = _run(["rundll32.exe", "user32.dll,LockWorkStation"])
    elif action == "exit":
        if on_exit:
            try:
                on_exit()
            except Exception:
                pass
        ok = True
    else:
        ok = False
    return {"ok": ok, "action": action,
            "error": "" if ok else "命令执行失败"}


def abort_shutdown() -> Dict[str, Any]:
    """中止系统已经排队的关机（`shutdown /a`）。"""
    if os.name != "nt":
        return {"ok": False, "error": "只有 Windows 支持"}
    try:
        r = subprocess.run(["shutdown", "/a"], capture_output=True,
                           creationflags=0x08000000, timeout=10)
        return {"ok": r.returncode == 0,
                "error": "" if r.returncode == 0 else "没有待中止的关机（或已被取消）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ----------------------------------------------------------------------
# 触发判定
# ----------------------------------------------------------------------

def pending_counts(tm) -> Dict[str, int]:
    """还有多少任务在跑/在等。"""
    try:
        tasks = tm.list()
    except Exception:
        return {"active": 0, "done": 0, "error": 0}
    # 暂停不是"结束"：用户暂停任务设了自动关机，最后一个跑着的任务
    # 一结束就关机器，暂停的任务被丢在身后 —— 必须算进 active。
    active = sum(1 for t in tasks
                 if t.get("status") in ("running", "queued", "paused", "interrupted"))
    done = sum(1 for t in tasks if t.get("status") == "done")
    err = sum(1 for t in tasks if t.get("status") == "error")
    return {"active": active, "done": done, "error": err}


def should_fire(cfg: Dict[str, Any], tm) -> bool:
    action = str(cfg.get("finish_action") or "none").lower()
    if action == "none" or action not in ACTIONS:
        return False
    c = pending_counts(tm)
    # 全部结束才算「完成」：一个失败一个成功也算结束（用户等的是「别再下了」）
    return c["active"] == 0 and (c["done"] + c["error"]) > 0


def arm(cfg: Dict[str, Any], tm, on_exit=None) -> bool:
    """安排一次延迟执行。已经安排过就不重复安排。"""
    global _thread
    with _lock:
        if _state["armed"]:
            return False
        action = str(cfg.get("finish_action") or "none").lower()
        if action == "none" or not should_fire(cfg, tm):
            return False
        delay = max(0, int(cfg.get("finish_delay", SHUTDOWN_GRACE) or 0))
        _cancel_ev.clear()
        _state.update({
            "armed": True, "action": action, "delay": delay,
            "at": time.time() + delay, "fired": False,
        })

    def waiter() -> None:
        try:
            if _cancel_ev.wait(delay):
                return
            with _lock:
                if not _state["armed"]:
                    return
                _state["armed"] = False
                _state["fired"] = True
            execute(action, on_exit=on_exit)
        finally:
            with _lock:
                _state["armed"] = False
            _thread = None

    _thread = threading.Thread(target=waiter, daemon=True, name="jiliu-finish")
    _thread.start()
    return True


def cancel() -> Dict[str, Any]:
    """取消倒计时，并顺手中止已经排队的系统关机。"""
    with _lock:
        was = _state["armed"]
        action = _state["action"]
        _state.update({"armed": False, "action": "none", "at": 0.0})
    _cancel_ev.set()
    aborted = False
    if was and action == "shutdown":
        aborted = bool(abort_shutdown().get("ok"))
    return {"ok": True, "cancelled": was, "aborted_shutdown": aborted}


def reset() -> None:
    """任务列表被清空/重新开始下载时调用，让下一轮还能触发。"""
    cancel()
    with _lock:
        _state["fired"] = False


def status() -> Dict[str, Any]:
    with _lock:
        armed = _state["armed"]
        at = _state["at"]
        action = _state["action"]
        left = max(0, int(at - time.time())) if armed else 0
        return {
            "armed": armed,
            "action": action,
            "action_text": ACTION_TEXT.get(action, action),
            "seconds_left": left,
            "at": at,
            "fired": _state["fired"],
        }


def maybe_fire(cfg: Dict[str, Any], tm, on_exit=None) -> bool:
    """任务结束时调用。满足条件就安排执行。"""
    return arm(cfg, tm, on_exit=on_exit)


def describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "actions": [{"id": a, "text": ACTION_TEXT[a]} for a in ACTIONS],
        "current": str(cfg.get("finish_action") or "none"),
        "delay": int(cfg.get("finish_delay", SHUTDOWN_GRACE) or 0),
        "when_all_done": bool(cfg.get("finish_when_all_done", True)),
        "status": status(),
    }
