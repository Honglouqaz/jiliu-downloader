"""下载队列。

为什么需要
    只有一个全局 max_concurrent 时，三个 4K 视频会占满全部名额，
    用户临时想下个 10 MB 的驱动包得等到天荒地老。队列把「同时跑几个」
    从全局常量变成「每条通道自己的配额」，顺带能按队列限速 ——
    视频队列限速 2 MB/s，网页就不会卡。

调度语义（和 IDM / AB DM 一致）
    - 队列之间轮转（round-robin），防止高优先级队列饿死别人
    - 队列内部按任务 priority 降序，同级按创建顺序
    - 全局 max_concurrent 仍然是硬上限：所有队列加起来不能超过它
    - 任务不属于任何队列（队列被删了）时归到 default，不会永远排队
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

DEFAULT_ID = "default"

DEFAULT_QUEUES: List[Dict[str, Any]] = [
    {
        "id": "default",
        "name": "默认",
        "max_concurrent": 3,
        "speed_limit": 0,
        "priority": 0,
        "dir": "",
    },
    {
        "id": "video",
        "name": "视频",
        "max_concurrent": 2,
        # 视频档默认限速 0（不限），但配额小 —— 大文件不该挤死小文件
        "speed_limit": 0,
        "priority": 5,
        "dir": "",
    },
]

MAX_CONCURRENT_LIMIT = 16
_ID_RE = re.compile(r"[^a-zA-Z0-9_\-]+")


def _clean_id(raw: Any, fallback: str) -> str:
    s = _ID_RE.sub("", str(raw or "")).strip()
    return s or fallback


def _int(v: Any, lo: int, hi: int, default: int = 0) -> int:
    try:
        n = int(v)
    except Exception:
        return default
    return max(lo, min(hi, n))


def normalize(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把配置里的队列列表收敛成合法结构。不修改 cfg，返回新列表。"""
    raw = cfg.get("queues")
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, q in enumerate(raw or []):
        if not isinstance(q, dict):
            continue
        qid = _clean_id(q.get("id") or q.get("name"), f"q{i + 1}")
        if qid in seen:
            continue
        seen.add(qid)
        name = str(q.get("name") or qid).strip()[:24] or qid
        out.append({
            "id": qid,
            "name": name,
            "max_concurrent": _int(q.get("max_concurrent"), 1, MAX_CONCURRENT_LIMIT, 2),
            "speed_limit": _int(q.get("speed_limit"), 0, 1 << 40, 0),
            "priority": _int(q.get("priority"), -99, 99, 0),
            "dir": str(q.get("dir") or "").strip(),
        })
    if not out:
        out = [dict(x) for x in DEFAULT_QUEUES]
    if not any(q["id"] == DEFAULT_ID for q in out):
        out.insert(0, dict(DEFAULT_QUEUES[0]))
    return out


def ids(cfg: Dict[str, Any]) -> List[str]:
    return [q["id"] for q in normalize(cfg)]


def get(cfg: Dict[str, Any], qid: str) -> Dict[str, Any]:
    qid = _clean_id(qid, DEFAULT_ID)
    for q in normalize(cfg):
        if q["id"] == qid:
            return q
    return dict(DEFAULT_QUEUES[0])


def resolve(cfg: Dict[str, Any], qid: str) -> str:
    """任务归属哪条队列：给了但不存在就落到 default。"""
    qid = _clean_id(qid, DEFAULT_ID)
    return qid if qid in ids(cfg) else DEFAULT_ID


def speed_limit_for(cfg: Dict[str, Any], qid: str) -> int:
    return int(get(cfg, qid).get("speed_limit") or 0)


def dir_for(cfg: Dict[str, Any], qid: str) -> str:
    return str(get(cfg, qid).get("dir") or "")


def upsert(cfg: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """新增或修改一条队列。返回收敛后的列表（调用方负责落盘）。"""
    qs = normalize(cfg)
    qid = _clean_id(patch.get("id") or patch.get("name"), "")
    if not qid:
        return {"ok": False, "error": "队列 id 不能为空", "queues": qs}
    item = {
        "id": qid,
        "name": str(patch.get("name") or qid).strip()[:24] or qid,
        "max_concurrent": _int(patch.get("max_concurrent"), 1, MAX_CONCURRENT_LIMIT, 2),
        "speed_limit": _int(patch.get("speed_limit"), 0, 1 << 40, 0),
        "priority": _int(patch.get("priority"), -99, 99, 0),
        "dir": str(patch.get("dir") or "").strip(),
    }
    for i, q in enumerate(qs):
        if q["id"] == qid:
            qs[i] = item
            break
    else:
        qs.append(item)
    cfg["queues"] = qs
    return {"ok": True, "queues": qs, "queue": item}


def remove(cfg: Dict[str, Any], qid: str,
           move_to: str = DEFAULT_ID) -> Dict[str, Any]:
    """删一条队列。default 不允许删（否则任务无处安放）。"""
    qid = _clean_id(qid, "")
    if qid == DEFAULT_ID:
        return {"ok": False, "error": "默认队列不能删除", "queues": normalize(cfg)}
    qs = [q for q in normalize(cfg) if q["id"] != qid]
    if len(qs) == len(normalize(cfg)):
        return {"ok": False, "error": f"没有这条队列：{qid}", "queues": qs}
    cfg["queues"] = qs
    return {"ok": True, "queues": qs, "move_to": resolve(cfg, move_to)}


def summary(cfg: Dict[str, Any], tasks: List[Any]) -> List[Dict[str, Any]]:
    """给面板用的队列概览：每条队列有多少在跑/在等、当前总速度。"""
    counts: Dict[str, Dict[str, Any]] = {}
    for q in normalize(cfg):
        counts[q["id"]] = {"running": 0, "queued": 0, "done": 0, "error": 0,
                           "speed": 0.0, "total": 0}
    for t in tasks:
        qid = resolve(cfg, getattr(t, "queue", "") or DEFAULT_ID)
        c = counts.setdefault(qid, {"running": 0, "queued": 0, "done": 0,
                                    "error": 0, "speed": 0.0, "total": 0})
        c["total"] += 1
        st = getattr(t, "status", "")
        if st in ("running", "queued", "done", "error"):
            c[st] += 1
        if st == "running":
            try:
                c["speed"] += float(getattr(t, "speed", 0) or 0)
            except Exception:
                pass
    out: List[Dict[str, Any]] = []
    for q in normalize(cfg):
        row = dict(q)
        row.update(counts.get(q["id"], {}))
        out.append(row)
    return out


def total_slots(cfg: Dict[str, Any]) -> int:
    """所有队列并发名额之和。用于面板提示「已用 5 / 8」。"""
    return sum(int(q["max_concurrent"]) for q in normalize(cfg))


def ensure_in_cfg(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """首次运行时把默认队列写进配置（幂等）。"""
    qs = normalize(cfg)
    if cfg.get("queues") != qs:
        cfg["queues"] = qs
    return qs
