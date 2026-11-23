"""订阅：feed 解析 + 历史比对 + 定时检查。

4K Video Downloader 最黏人的功能是"订阅频道/播放列表，新内容自动下"。
汲流已有 feed.py（RSS/Atom 解析），这里补上"记住看过什么"和"定时再看一眼"。

状态存 <HOME>/subscriptions.json，形如::

    [{"id": "sub_ab12", "url": "https://.../feed", "title": "某播客",
      "folder": "", "enabled": true, "seen": ["guid1", ...],
      "last_check": 1730000000, "last_error": ""}]

只做三件事：加/删、比对、拉取。建任务由调用方（server）完成，
这样订阅模块不依赖 TaskManager，能单独测。
"""
from __future__ import annotations

import json
import pathlib
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from . import atomicio, config as _config, feed

STORE = _config.HOME / "subscriptions.json"
_lock = threading.RLock()


def _read() -> List[Dict[str, Any]]:
    data = atomicio.read_json(STORE, default=[])
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _write(items: List[Dict[str, Any]]) -> None:
    try:
        atomicio.write_json(STORE, items, indent=2)
    except Exception as exc:  # noqa: BLE001
        atomicio.warn(f"订阅表保存失败：{type(exc).__name__}: {exc}")


def raw_list() -> List[Dict[str, Any]]:
    """内部用：带完整 seen 历史。对外接口不要返回这个（太长）。"""
    with _lock:
        return [dict(x) for x in _read()]


def list_all() -> List[Dict[str, Any]]:
    with _lock:
        out = []
        for s in _read():
            x = dict(s)
            x["seen_count"] = len(x.get("seen") or [])
            x.pop("seen", None)
            out.append(x)
        return out


def add(url: str, cfg: Dict[str, Any], *, title: str = "",
        folder: str = "") -> Dict[str, Any]:
    url = (url or "").strip()
    if not url:
        raise ValueError("缺少订阅地址")
    info = feed.try_inspect(url, cfg)
    if not info.get("ok"):
        raise ValueError(info.get("error") or "订阅源解析失败")
    with _lock:
        items = _read()
        if any(x.get("url") == url for x in items):
            raise ValueError("这个订阅源已经在列表里了")
        sub = {
            "id": "sub_" + uuid.uuid4().hex[:8],
            "url": url,
            "title": title or info.get("title") or url,
            "folder": folder or "",
            "enabled": True,
            "seen": [str(i.get("id") or i.get("url") or i.get("link") or "")
                     for i in (info.get("items") or [])],
            "last_check": time.time(),
            "last_error": "",
            "added": time.time(),
        }
        items.append(sub)
        _write(items)
    out = dict(sub)
    out.pop("seen", None)
    out["seen_count"] = len(sub["seen"])
    return out


def remove(sid: str) -> bool:
    with _lock:
        items = _read()
        left = [x for x in items if x.get("id") != sid]
        if len(left) == len(items):
            return False
        _write(left)
        return True


def set_enabled(sid: str, enabled: bool) -> bool:
    with _lock:
        items = _read()
        hit = False
        for x in items:
            if x.get("id") == sid:
                x["enabled"] = bool(enabled)
                hit = True
        if hit:
            _write(items)
        return hit


def check_one(sub: Dict[str, Any], cfg: Dict[str, Any],
              limit: int = 10) -> Dict[str, Any]:
    """拉一次并比对。返回新增条目（不含建任务）。"""
    url = str(sub.get("url") or "")
    try:
        info = feed.try_inspect(url, cfg)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "new": []}
    if not info.get("ok"):
        return {"ok": False, "error": info.get("error") or "解析失败", "new": []}

    seen = set(str(x) for x in (sub.get("seen") or []))
    fresh: List[Dict[str, Any]] = []
    for it in (info.get("items") or []):
        key = str(it.get("id") or it.get("url") or it.get("link") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        if it.get("url") or it.get("link"):
            fresh.append(it)
    return {"ok": True, "title": info.get("title") or sub.get("title"),
            "new": fresh[:limit], "total": len(fresh)}


def mark_checked(sid: str, new_items: List[Dict[str, Any]], error: str = "") -> None:
    with _lock:
        items = _read()
        for x in items:
            if x.get("id") != sid:
                continue
            seen = list(x.get("seen") or [])
            for it in new_items:
                key = str(it.get("id") or it.get("url") or it.get("link") or "")
                if key and key not in seen:
                    seen.append(key)
            x["seen"] = seen[-2000:]
            x["last_check"] = time.time()
            x["last_error"] = error
        _write(items)


class Watcher:
    """后台定时检查。on_new(sub, items) 由调用方负责建任务。"""

    def __init__(self, cfg: Dict[str, Any],
                 on_new: Callable[[Dict[str, Any], List[Dict[str, Any]]], int]):
        self.cfg = cfg
        self.on_new = on_new
        self._stop = threading.Event()
        self._th: Optional[threading.Thread] = None
        self.last_run = 0.0
        self.last_result: Dict[str, Any] = {}

    def start(self) -> bool:
        minutes = int(self.cfg.get("feed_check_minutes", 60) or 0)
        if minutes <= 0:
            return False
        if self._th and self._th.is_alive():
            return True
        self._th = threading.Thread(target=self._loop, daemon=True, name="jiliu-feed")
        self._th.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_all()
            except Exception:  # noqa: BLE001
                pass
            minutes = int(self.cfg.get("feed_check_minutes", 60) or 60)
            self._stop.wait(max(60, minutes * 60))

    def check_all(self) -> Dict[str, Any]:
        cfg = self.cfg
        limit = int(cfg.get("feed_max_per_check", 10) or 10)
        added = 0
        details = []
        for sub in raw_list():
            if not sub.get("enabled", True):
                continue
            res = check_one(sub, cfg, limit=limit)
            if not res.get("ok"):
                mark_checked(sub["id"], [], str(res.get("error") or ""))
                details.append({"id": sub["id"], "ok": False, "error": res.get("error")})
                continue
            fresh = res.get("new") or []
            n = 0
            failed = False
            if fresh:
                try:
                    n = int(self.on_new(sub, fresh) or 0)
                except Exception as exc:  # noqa: BLE001
                    n = 0
                    failed = True
                    details.append({"id": sub["id"], "ok": False,
                                    "error": f"建任务失败：{exc}"})
            if failed:
                # 建任务失败时绝不能把这批条目标成已见：下一轮会被
                # seen 挡住，新内容永久丢失。只更新检查时间，保留条目。
                with _lock:
                    items = _read()
                    for x in items:
                        if x.get("id") == sub.get("id"):
                            x["last_check"] = time.time()
                            x["last_error"] = "有新条目但建任务失败"
                    _write(items)
                continue
            mark_checked(sub["id"], fresh, "")
            added += n
            details.append({"id": sub["id"], "title": sub.get("title"),
                            "ok": True, "new": len(fresh), "queued": n})
        self.last_run = time.time()
        self.last_result = {"new": added, "subs": details}
        return self.last_result
