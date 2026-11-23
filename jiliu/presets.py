"""站点预设（Smart Mode）。

问题很具体：下 B 站想要 1080P、下 YouTube 想要 4K、下抖音想要无水印原声，
但视频画质/编码/目录/字幕是全局一套 —— 每换一个站就得改一次设置。

做法：配置里存 `site_presets[host] = {qn, codec, container, dir, sub_langs,
audio_only}`，解析 URL 时按 host 命中就套用，界面上显示「已套用 B 站预设」
并允许一键改回全局。

为什么 key 用「host 后缀」而不是完整域名
    同一个站有 www.bilibili.com / m.bilibili.com / api.bilibili.com，
    用户只想写一条 `bilibili.com`。匹配时按「相等或 .endswith」判断，
    顺序上先长后短，避免 `b.com` 抢走 `a.b.com`。
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional

FIELDS = ("qn", "codec", "container", "dir", "sub_langs", "audio_only",
          "danmaku")

FIELD_TEXT = {
    "qn": "画质",
    "codec": "编码",
    "container": "容器",
    "dir": "保存目录",
    "sub_langs": "字幕语言",
    "audio_only": "仅音频",
    "danmaku": "下载弹幕",
}

# 画质编号（B 站与通用 yt-dlp 共用一套习惯叫法）
QN_TEXT = {
    0: "最高可用", 127: "8K", 126: "杜比视界", 125: "HDR", 120: "4K",
    116: "1080P60", 112: "1080P+", 80: "1080P", 74: "720P60", 64: "720P",
    32: "480P", 16: "360P", 6: "240P",
}


def host_of(url_or_host: str) -> str:
    s = (url_or_host or "").strip()
    if not s:
        return ""
    if "://" not in s:
        # 允许直接写 bilibili.com
        s = "http://" + s
    try:
        return (urllib.parse.urlsplit(s).hostname or "").lower()
    except Exception:
        return ""


def normalize(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """收敛配置里的预设表。返回新字典（不改 cfg）。"""
    raw = cfg.get("site_presets")
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        host = host_of(str(key)).lstrip(".")
        if not host:
            continue
        item: Dict[str, Any] = {}
        for f in FIELDS:
            if f not in val:
                continue
            v = val[f]
            if f == "qn":
                try:
                    item[f] = int(v)
                except Exception:
                    continue
            elif f in ("audio_only", "danmaku"):
                item[f] = bool(v)
            else:
                item[f] = str(v)
        if item:
            out[host] = item
    return out


def match(cfg: Dict[str, Any], url: str) -> Dict[str, Any]:
    """找出这条 URL 命中的预设。返回 {ok, host, key, preset}。

    最长后缀优先：`a.b.com` 存在时不会被 `b.com` 抢走。
    """
    host = host_of(url)
    table = normalize(cfg)
    if not host or not table:
        return {"ok": False, "host": host, "key": "", "preset": {}}
    if host in table:
        return {"ok": True, "host": host, "key": host, "preset": dict(table[host])}
    best = ""
    for key in table:
        if host.endswith("." + key) and len(key) > len(best):
            best = key
    if best:
        return {"ok": True, "host": host, "key": best, "preset": dict(table[best])}
    return {"ok": False, "host": host, "key": "", "preset": {}}


def apply(cfg: Dict[str, Any], url: str, opts: Optional[Dict[str, Any]] = None
          ) -> Dict[str, Any]:
    """把预设合并进一组选项。显式传入的 opts 优先于预设。"""
    hit = match(cfg, url)
    if not hit["ok"]:
        return {"applied": False, "source": "", "opts": dict(opts or {})}
    out = dict(hit["preset"])
    for k, v in (opts or {}).items():
        if v not in (None, "", 0, False):
            out[k] = v
    return {"applied": True, "source": hit["key"], "opts": out}


def remember(cfg: Dict[str, Any], url: str,
             opts: Dict[str, Any]) -> Dict[str, Any]:
    """把这次用的选项记成该站点的预设（调用方负责落盘）。"""
    host = host_of(url)
    if not host:
        return {"ok": False, "error": "认不出站点域名"}
    table = normalize(cfg)
    item = {}
    for f in FIELDS:
        if f in opts and opts[f] not in (None, ""):
            item[f] = opts[f]
    if not item:
        return {"ok": False, "error": "没有可记住的设置"}
    # 用注册域（去掉 www / m / api 这种前缀）当 key，一次记住所有子域。
    # 反过来做（取最后两段）会把 bbc.co.uk 记成 co.uk，那是错的。
    key = host
    for pre in ("www.", "www2.", "m.", "api.", "mobile."):
        if host.startswith(pre) and len(host) > len(pre) + 3:
            key = host[len(pre):]
            break
    table[key] = item
    cfg["site_presets"] = table
    return {"ok": True, "key": key, "preset": item, "presets": table}


def forget(cfg: Dict[str, Any], key: str) -> Dict[str, Any]:
    table = normalize(cfg)
    k = host_of(key).lstrip(".") or str(key)
    if k not in table:
        return {"ok": False, "error": f"没有这个站点的预设：{k}", "presets": table}
    table.pop(k)
    cfg["site_presets"] = table
    return {"ok": True, "presets": table}


def describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """给设置页用：预设列表 + 字段元数据。"""
    table = normalize(cfg)
    rows: List[Dict[str, Any]] = []
    for key, item in sorted(table.items()):
        shown = []
        for f, v in item.items():
            if f == "qn":
                shown.append(f"{FIELD_TEXT[f]} {QN_TEXT.get(int(v), v)}")
            elif f in ("audio_only", "danmaku"):
                if v:
                    shown.append(FIELD_TEXT[f])
            else:
                shown.append(f"{FIELD_TEXT.get(f, f)} {v}")
        rows.append({"key": key, "preset": item,
                     "summary": " · ".join(shown) or "（空）"})
    return {
        "presets": rows,
        "count": len(rows),
        "fields": [{"id": f, "text": FIELD_TEXT[f]} for f in FIELDS],
        "qn": [{"id": k, "text": v} for k, v in QN_TEXT.items()],
    }
