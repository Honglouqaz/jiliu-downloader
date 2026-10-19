"""接管规则（对齐 IDM 的 Extensions + ExceptionServers）。

两类东西不该被接管：
  1. 站点例外 —— 更新服务器、表情站、统计探针，它们的响应也长得像"文件"；
  2. 明显不是用户想要的 —— 小图标、字体、追踪像素。

扩展和后端共用这套规则：后端通过 /api/capture/rules 发给扩展，
扩展在 onHeadersReceived 里先过滤一遍，减少无意义的"接管"弹窗。
"""
from __future__ import annotations

import fnmatch
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

# 默认永不接管的后缀（体积小、数量大、接管了只会烦人）
NOISE_SUFFIX = (
    ".ico", ".css", ".js", ".map", ".woff", ".woff2", ".ttf", ".eot",
    ".svg", ".gif", ".webp", ".avif",
)

# 默认例外站点
DEFAULT_HOSTS = [
    "*.update.microsoft.com",
    "*.windowsupdate.com",
    "*.gstatic.com",
    "media.tenor.co",
    "*.giphy.com",
    "*.doubleclick.net",
    "*.google-analytics.com",
]


def exceptions(cfg: Dict[str, Any]) -> List[str]:
    raw = (cfg or {}).get("capture_exceptions")
    if not isinstance(raw, list):
        return list(DEFAULT_HOSTS)
    return [str(x).strip() for x in raw if str(x).strip()]


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def host_blocked(host: str, patterns: List[str]) -> bool:
    host = (host or "").lower()
    for p in patterns:
        p = p.strip().lower()
        if not p:
            continue
        if p.startswith("*."):
            base = p[2:]
            if host == base or host.endswith("." + base):
                return True
        elif fnmatch.fnmatch(host, p):
            return True
    return False


def suffix_blocked(url: str) -> bool:
    path = (urllib.parse.urlparse(url).path or "").lower()
    return any(path.endswith(s) for s in NOISE_SUFFIX)


def should_capture(url: str, cfg: Dict[str, Any], *,
                   size: Optional[int] = None,
                   mime: str = "") -> Tuple[bool, str]:
    """返回 (要不要接管, 原因)。扩展和后端都调这一个。"""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False, "非 http(s)"
    host = host_of(url)
    pats = exceptions(cfg)
    if host_blocked(host, pats):
        return False, f"站点例外：{host}"
    if suffix_blocked(url):
        return False, "后缀属于噪音类型"
    limit_mb = int((cfg or {}).get("capture_max_mb", 0) or 0)
    if limit_mb and size and size > limit_mb * 1024 * 1024:
        return False, f"超过 {limit_mb}MB 阈值"
    if mime and mime.startswith("text/"):
        return False, "文本响应"
    return True, "ok"


def describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "hosts": exceptions(cfg),
        "noise_suffix": list(NOISE_SUFFIX),
        "max_mb": int((cfg or {}).get("capture_max_mb", 0) or 0),
    }
