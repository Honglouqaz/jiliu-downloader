"""计划任务 / 时段限速。

配置形如::

    "schedule": {
        "enabled": true,
        "days": [0,1,2,3,4],        # 周一=0 … 周日=6
        "start": "00:30",
        "end": "07:30",
        "speed_limit": 0            # 时段内的限速，0 = 不限
    }

只做一件事：判断"现在该不该跑"，以及"这段时间限速多少"。
真正的暂停/恢复/改限速由 manager 执行 —— 这个模块保持纯函数，
方便直接测（不依赖时钟以外的任何东西）。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Optional


def _minutes(hhmm: Any) -> Optional[int]:
    s = str(hhmm or "").strip()
    if not s or ":" not in s:
        return None
    try:
        h, m = s.split(":", 1)
        h, m = int(h), int(m)
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def normalize(raw: Any) -> Dict[str, Any]:
    """把用户填的东西收敛成一份能用的配置（坏值一律回默认）。"""
    d = dict(raw or {}) if isinstance(raw, dict) else {}
    days = d.get("days")
    if not isinstance(days, list) or not days:
        days = list(range(7))
    try:
        days = sorted({int(x) for x in days if 0 <= int(x) <= 6})
    except (TypeError, ValueError):
        days = list(range(7))
    start = _minutes(d.get("start"))
    end = _minutes(d.get("end"))
    return {
        "enabled": bool(d.get("enabled")),
        "days": days,
        "start": str(d.get("start") or "00:00"),
        "end": str(d.get("end") or "07:00"),
        "speed_limit": max(0, int(d.get("speed_limit") or 0)),
        "_start": start if start is not None else 0,
        "_end": end if end is not None else 7 * 60,
    }


def in_window(sched: Dict[str, Any], now: Optional[_dt.datetime] = None) -> bool:
    """现在是否落在允许下载的时段里。支持跨午夜（22:00–06:00）。"""
    s = normalize(sched)
    now = now or _dt.datetime.now()
    if now.weekday() not in s["days"]:
        return False
    cur = now.hour * 60 + now.minute
    start, end = s["_start"], s["_end"]
    if start == end:
        return True                      # 起止相同 = 全天
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end     # 跨午夜


def evaluate(cfg: Dict[str, Any], now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    """给 manager 用的判定结果。"""
    s = normalize((cfg or {}).get("schedule"))
    if not s["enabled"]:
        return {"enabled": False, "open": True, "speed_limit": 0,
                "note": "计划任务未启用"}
    opened = in_window(s, now)
    return {
        "enabled": True,
        "open": opened,
        "speed_limit": int(s["speed_limit"]),
        "note": ("在计划时段内" if opened else "不在计划时段，队列暂停"),
        "window": f"{s['start']}–{s['end']}",
        "days": s["days"],
    }


def describe(cfg: Dict[str, Any], now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    return evaluate(cfg, now)
