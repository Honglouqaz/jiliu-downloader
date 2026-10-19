"""版本形态：国内版 / 全球版。

为什么要真的分成两个版本，而不是藏几个按钮：

  国内版面对的是「只用国内站」的场景 —— 单位电脑、内网、给家人装的机器。
  这类环境下把 YouTube / 代理这些东西摆在界面上，除了制造困惑没有别的作用
  （点了必然失败，还要解释为什么失败）。所以国内版是**真的一条都不发**：
  界面上不出现，逻辑上也拒掉，报错信息直接说清「本版不含此能力」。

  全球版则是全功能。

判断「是不是海外站点」这件事刻意保守：

  - 只有**明确认识**的海外平台才判为海外。白名单之外的域名一律放行。
    理由：把国内的小众站误判成海外，会让主人正常能下的东西下不了 ——
    这个方向的错误代价高得多。
  - 判定的用途只有两个：国内版拦掉海外站；以及给界面提示
    「这个要走代理」。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import proxy as _proxy

# 明确认识的海外平台。命中即判为海外。
# 留一份注释说明为什么列这些：都是 yt-dlp 支持、且国内直连基本拿不到的。
FOREIGN_PLATFORMS: List[str] = [
    "youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com",
    "twitter.com", "x.com", "t.co",
    "facebook.com", "fb.watch",
    "instagram.com",
    "tiktok.com",
    "twitch.tv",
    "netflix.com",
    "vimeo.com",
    "dailymotion.com",
    "soundcloud.com",
    "reddit.com", "redd.it",
    "pixiv.net",
    "telegram.org", "t.me",
    "discord.com", "discord.gg",
    "medium.com",
    "patreon.com",
    "spotify.com",
    "archive.org",
    "wikipedia.org",
    "github.com", "githubusercontent.com", "githubassets.com",
    "sourceforge.net",
    "mega.nz",
    "mediafire.com",
    "pornhub.com", "xvideos.com", "xnxx.com",
    "nicovideo.jp",
    "bilibili.tv",
]

EDITIONS: Dict[str, Dict[str, Any]] = {
    "cn": {
        "key": "cn",
        "name": "国内版",
        "tagline": "只做国内站点，全部直连",
        "allow_foreign": False,
        "show_proxy": False,      # 界面不出现代理设置
        "show_foreign_probe": False,
        "bt": True,
        "netdisk": True,
        "note": "面向只用国内站的场景：不出现海外站点与代理相关功能，"
                "链接判为海外时直接说明原因，而不是让你等一个必然的失败。",
    },
    "global": {
        "key": "global",
        "name": "全球版",
        "tagline": "国内国外都能下，自带代理分流",
        "allow_foreign": True,
        "show_proxy": True,
        "show_foreign_probe": True,
        "bt": True,
        "netdisk": True,
        "note": "全功能：海外站点走代理、国内站点直连，可单独配置代理并逐项测速。",
    },
}

DEFAULT_EDITION = "global"


def current(cfg: Optional[Dict[str, Any]] = None) -> str:
    """当前是哪个版本。配置里没有就用默认（全球版）。

    也允许用环境变量 JILIU_EDITION 覆盖 —— 打包时给两个版本
    塞不同的默认配置即可，不必改代码。
    """
    import os
    env = (os.environ.get("JILIU_EDITION") or "").strip().lower()
    if env in EDITIONS:
        return env
    k = str((cfg or {}).get("edition") or "").strip().lower()
    return k if k in EDITIONS else DEFAULT_EDITION


def caps(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """当前版本的能力表。界面按这个决定显示什么。"""
    return dict(EDITIONS[current(cfg)])


def is_global(cfg: Optional[Dict[str, Any]] = None) -> bool:
    return current(cfg) == "global"


def is_foreign(url_or_host: str) -> bool:
    """明确认识的海外平台。认不出来一律 False（放行）。"""
    host = (url_or_host or "").lower().strip()
    if not host:
        return False
    if "://" in host or "/" in host:
        host = _proxy.host_of(host)
    if not host:
        return False
    for d in FOREIGN_PLATFORMS:
        if host == d or host.endswith("." + d):
            return True
    return False


def gate(url: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """这个链接在当前版本下能不能下。

    返回 {allowed, reason, foreign}。国内版遇到明确的海外站点会拦下，
    并给出**能指导下一步**的原因（而不是一句「不支持」）。
    """
    foreign = is_foreign(url)
    if foreign and not caps(cfg).get("allow_foreign", True):
        return {
            "allowed": False,
            "foreign": True,
            "reason": "这是海外站点，当前是**国内版**，不包含海外站点支持。"
                      "需要下载请换用全球版。",
        }
    return {"allowed": True, "foreign": foreign, "reason": ""}


def describe(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给界面看的版本信息。"""
    c = caps(cfg)
    return {
        **c,
        "all": [{"key": k, "name": v["name"], "tagline": v["tagline"]}
                for k, v in EDITIONS.items()],
        "foreign_platforms": FOREIGN_PLATFORMS[:12] + ["…"],
        "switch_hint": "设置里可以切换版本，切换后需重启生效",
    }
