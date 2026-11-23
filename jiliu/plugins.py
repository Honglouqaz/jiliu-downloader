"""解析插件：让汲流自己长出新站点。

    # 插件放在 <配置目录>/plugins/*.py，一个文件一个插件
    from jiliu import plugins
    plugins.load()
    hit = plugins.resolve(url, cfg)     # 有插件认领就返回它的解析结果
    plugins.emit("done", task)          # 广播事件给所有插件

为什么是「用户目录下的 .py 文件」而不是内置注册表：
站点的破法三天一变，把解法焊死在 exe 里，等于每次都要重新发版。
放在用户目录里，改一行存盘、点「重新加载」就生效 —— 用户能自己救自己。

插件接口（全部可选，有就用）：

    NAME = "我的站点"                 # 显示名
    VERSION = "1.0"
    DESCRIPTION = "一句话说明"
    DOMAINS = ["example.com"]         # 只在这些域名下被询问（留空 = 全都问）

    def match(url) -> bool: ...       # 快速筛，省得每条链接都真跑一次
    def resolve(url, cfg) -> dict:    # 返回 None = 不认领
        # 认领就返回：
        #   {"url": "直链"}            或 {"urls": [直链, 镜像]}
        #   "filename", "headers", "meta", "kind"（http/video）, "note"
    def on_event(event, task) -> None: ...   # created/done/error/cancelled

安全边界（很重要，因为这是**用户自己的**代码）：
  - 只在用户目录下加载，不扫当前工作目录（避免「解压就中招」）
  - 单个插件的 import / 调用异常一律吞掉并记在插件状态里，绝不带崩主程序
  - resolve 有超时（默认 8 秒），卡死的插件只会让这条链接慢一下
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

MODULE_PREFIX = "jiliu_plugin_"
RESOLVE_TIMEOUT = 8.0
EVENTS = ("created", "started", "done", "error", "cancelled")

_lock = threading.RLock()
_cache: List[Dict[str, Any]] = []
_loaded = False
# 插件自己的 traceback 不写进主日志（会刷屏），留在这里给界面看
_MAX_ERRORS = 20


# --------------------------------------------------------------------------
# 发现与加载
# --------------------------------------------------------------------------

def plugin_dir() -> pathlib.Path:
    """插件目录：<配置目录>/plugins。不存在就建一个（空目录也算个提示）。"""
    from . import config as _config

    d = pathlib.Path(_config.HOME) / "plugins"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


def _disabled(cfg: Dict[str, Any]) -> set:
    raw = (cfg or {}).get("plugins_disabled") or []
    return {str(x).strip().lower() for x in raw if str(x).strip()}


def load(cfg: Optional[Dict[str, Any]] = None, force: bool = False) -> List[Dict[str, Any]]:
    """扫描并加载插件。返回插件状态列表（含加载失败的）。"""
    global _loaded
    with _lock:
        if _loaded and not force and not cfg:
            return list(_cache)
        cfg = cfg or {}
        off = _disabled(cfg)
        out: List[Dict[str, Any]] = []
        d = plugin_dir()
        files = sorted([p for p in d.glob("*.py")
                        if not p.name.startswith("_")], key=lambda p: p.name.lower())
        for f in files:
            if f.stem.lower() in off:
                out.append(_stub(f, enabled=False, error="已在设置里停用"))
                continue
            rec = _load_one(f)
            out.append(rec)
        _cache.clear()
        _cache.extend(out)
        _loaded = True
        return list(out)


def _stub(path: pathlib.Path, *, enabled: bool, error: str = "") -> Dict[str, Any]:
    return {"file": path.name, "path": str(path), "key": path.stem,
            "name": path.stem, "version": "", "description": "",
            "domains": [], "enabled": enabled, "ok": False, "error": error,
            "hooks": [], "loaded_at": time.time(), "calls": 0, "errors": 0,
            "last_error": ""}


def _load_one(path: pathlib.Path) -> Dict[str, Any]:
    rec = _stub(path, enabled=True)
    modname = MODULE_PREFIX + path.stem
    # 重新加载时先把旧的踢掉，否则改了代码不生效
    for k in list(sys.modules.keys()):
        if k == modname:
            sys.modules.pop(k, None)
    old_path = list(sys.path)
    try:
        # 让插件能 import 同目录下的兄弟模块
        d = str(path.parent)
        if d not in sys.path:
            sys.path.insert(0, d)
        spec = importlib.util.spec_from_file_location(modname, str(path))
        if spec is None or spec.loader is None:
            rec["error"] = "无法创建模块规格"
            return rec
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["trace"] = traceback.format_exc(limit=3)
        return rec
    finally:
        sys.path[:] = old_path

    name = str(getattr(mod, "NAME", "") or path.stem)
    rec.update({
        "name": name,
        "version": str(getattr(mod, "VERSION", "") or ""),
        "description": str(getattr(mod, "DESCRIPTION", "") or ""),
        "domains": _norm_domains(getattr(mod, "DOMAINS", None)),
        "hooks": [h for h in ("match", "resolve", "on_event")
                  if callable(getattr(mod, h, None))],
        "ok": True,
        "error": "" if callable(getattr(mod, "resolve", None)) else "没有 resolve()，只能收事件",
        "module": mod,
    })
    return rec


def _norm_domains(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for d in raw:
        s = str(d or "").strip().lower()
        if s.startswith("*."):
            s = s[2:]
        if s:
            out.append(s)
    return out


def list_plugins(cfg: Optional[Dict[str, Any]] = None, reload: bool = False) -> List[Dict[str, Any]]:
    """给界面用的插件清单（不返回模块对象）。"""
    with _lock:
        recs = list(_cache)
    if reload or not recs:
        recs = load(cfg, force=True)
    out = []
    for r in recs:
        out.append({k: v for k, v in r.items() if k not in ("module", "trace")})
    return out


def reload(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """重新扫描（改完插件代码点一下就行，不用重启）。"""
    return list_plugins(cfg, reload=True)


def set_enabled(key: str, on: bool, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """停用/启用某个插件（写进配置的 plugins_disabled）。"""
    from . import config as _config

    key = str(key or "").strip().lower()
    if not key:
        return {"ok": False, "error": "插件名不能为空"}
    off = _disabled(cfg)
    if on:
        off.discard(key)
    else:
        off.add(key)
    cfg["plugins_disabled"] = sorted(off)
    try:
        _config.save(cfg)
    except Exception:  # noqa: BLE001
        pass
    load(cfg, force=True)
    return {"ok": True, "key": key, "enabled": bool(on)}


# --------------------------------------------------------------------------
# 调用
# --------------------------------------------------------------------------

def _active() -> List[Dict[str, Any]]:
    with _lock:
        recs = list(_cache)
    return [r for r in recs if r.get("ok") and r.get("enabled")]


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _wants(rec: Dict[str, Any], url: str) -> bool:
    doms = rec.get("domains") or []
    host = _host(url)
    if not doms:
        return True
    return any(host == d or host.endswith("." + d) for d in doms)


def _call(rec: Dict[str, Any], fn_name: str, *args, timeout: float = 0.0):
    """带超时调用插件的函数。超时返回 (None, "timeout")。

    timeout 默认取模块级 RESOLVE_TIMEOUT 的**当前值**（不能写成默认参数，
    那会在函数定义时就把 8.0 焊死，运行期再调 RESOLVE_TIMEOUT 就不生效了）。
    """
    if timeout <= 0:
        timeout = RESOLVE_TIMEOUT
    mod = rec.get("module")
    fn = getattr(mod, fn_name, None) if mod else None
    if not callable(fn):
        return None, "no-hook"
    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn(*args)
        except Exception as exc:  # noqa: BLE001
            box["error"] = f"{type(exc).__name__}: {exc}"
            box["trace"] = traceback.format_exc(limit=3)

    th = threading.Thread(target=run, daemon=True,
                          name=f"plugin-{rec.get('key')}-{fn_name}")
    th.start()
    th.join(timeout)
    rec["calls"] = int(rec.get("calls") or 0) + 1
    if th.is_alive():
        rec["errors"] = int(rec.get("errors") or 0) + 1
        rec["last_error"] = f"{fn_name} 超时（>{timeout:.0f}s）"
        return None, "timeout"
    if "error" in box:
        rec["errors"] = int(rec.get("errors") or 0) + 1
        rec["last_error"] = box["error"]
        return None, box["error"]
    return box.get("value"), ""


def resolve(url: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """问所有插件「这条链接你能处理吗」。第一个认领的说了算。"""
    if not (cfg or {}).get("plugins_enabled", True):
        return None
    if not _active() and not _loaded:
        load(cfg)
    u = (url or "").strip()
    if not u:
        return None
    for rec in _active():
        if not _wants(rec, u):
            continue
        if "match" in (rec.get("hooks") or []):
            hit, err = _call(rec, "match", u)
            if err or not hit:
                continue
        val, err = _call(rec, "resolve", u, cfg)
        if err or not isinstance(val, dict):
            continue
        out = _pack(rec, u, val)
        if out:
            return out
    return None


def _pack(rec: Dict[str, Any], url: str, val: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把插件的返回值规整成管理器认识的样子。"""
    urls: List[str] = []
    if val.get("url"):
        urls.append(str(val["url"]).strip())
    for x in (val.get("urls") or []):
        s = str(x or "").strip()
        if s and s not in urls:
            urls.append(s)
    if not urls:
        return None
    headers = {str(k): str(v) for k, v in (val.get("headers") or {}).items() if v}
    meta = dict(val.get("meta") or {})
    if headers:
        meta["headers"] = headers
    return {
        "plugin": rec.get("key"),
        "plugin_name": rec.get("name"),
        "url": urls[0],
        "mirrors": urls[1:],
        "filename": str(val.get("filename") or ""),
        "kind": str(val.get("kind") or "http").lower(),
        "meta": meta,
        "note": str(val.get("note") or ""),
        # 多个产物：插件可以给成 tasks，交给管理器逐个建
        "tasks": [t for t in (val.get("tasks") or []) if isinstance(t, dict)],
    }


def emit(event: str, task: Any, cfg: Optional[Dict[str, Any]] = None) -> int:
    """把事件广播给所有插件。返回成功调用数。"""
    if cfg is not None and not (cfg or {}).get("plugins_enabled", True):
        return 0
    n = 0
    for rec in _active():
        if "on_event" not in (rec.get("hooks") or []):
            continue
        val, err = _call(rec, "on_event", event, task, timeout=3.0)
        if not err:
            n += 1
    return n


def status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    recs = _cache or load(cfg)
    good = [r for r in recs if r.get("ok") and r.get("enabled")]
    return {
        "dir": str(plugin_dir()),
        "enabled": bool(cfg.get("plugins_enabled", True)),
        "total": len(recs),
        "active": len(good),
        "plugins": [{k: v for k, v in r.items() if k not in ("module", "trace")}
                    for r in recs],
    }


def describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "dir": str(plugin_dir()),
        "timeout": RESOLVE_TIMEOUT,
        "events": list(EVENTS),
        "note": "插件是用户目录下的 .py；改完点重新加载即可生效，异常不会影响主程序",
    }
