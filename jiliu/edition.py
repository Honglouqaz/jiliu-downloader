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


# ---------------------------------------------------------------------------
# 发行形态：同一份代码，两种卖法
# ---------------------------------------------------------------------------
# 「版本（cn/global）」回答的是**在哪用**，「形态（oss/archive）」回答的是
# **卖给谁、拿什么卖**。两件事必须分开，否则会得出「国内版不能解析」这种
# 既不对也没用的结论 —— 国内版当然能解析，只是不发海外站。
#
#   oss      开源引流版：全功能，含视频站解析。代码 MIT，Pro 授权卖调度与自动化。
#            解析能力留在开源部分，责任与社区一起分散；我们卖的是「省时间」，
#            不是「能下某个站」。
#
#   archive  归档版：只做公开资源（直链 / RSS / 磁力 / 网盘公开分享 / 站点抓取），
#            **不含视频站解析**。这个版本可以正当收费：客户买的是「把公开的
#            资料批量搬回家并整理好」，不是「帮我绕过谁家的播放器」。
#
# 形态由环境变量 JILIU_FORM 或配置项 form 决定，打包时各带一个默认值。
FORMS: Dict[str, Dict[str, Any]] = {
    "oss": {
        "key": "oss",
        "name": "开源版",
        "tagline": "全功能，含视频站解析；Pro 解锁调度与自动化",
        "allow_video_sites": True,
        "show_video_entry": True,
        "placeholder": "粘贴链接：直链 / 视频页 / 磁力 / 迅雷 / ed2k / .torrent / 网盘分享页",
        "compliance": "",
        "sells": "调度、自动化、批量与通知（Pro）",
    },
    "archive": {
        "key": "archive",
        "name": "归档版",
        "tagline": "公开资源批量归档，不含视频站解析",
        "allow_video_sites": False,
        "show_video_entry": False,
        "placeholder": "粘贴公开资源链接：直链 / RSS / 磁力 / .torrent / 公开分享页",
        "compliance": "本版只处理公开可访问的资源，不解析、不下载任何受版权保护的"
                      "流媒体内容。请只归档你有权保存的资料。",
        "sells": "批量归档、自动整理、长期订阅（Pro）",
    },
}

DEFAULT_FORM = "oss"


def _packaged_form() -> str:
    """安装目录里有没有 form.txt。

    两个发行版是同一份代码打出来的，区别只在随包的这个小文件：
    用户装了「归档版」就不该在设置里翻出一个「切换成开源版」的开关自己改回去
    —— 那是两条不同的产品线，不是一个主题。所以形态跟着安装包走，
    配置项与 form.txt 冲突时以安装包为准。
    """
    import pathlib
    import sys
    try:
        base = (pathlib.Path(sys.executable).resolve().parent
                if getattr(sys, "frozen", False)
                else pathlib.Path(__file__).resolve().parent.parent)
        p = base / "form.txt"
        if p.is_file():
            k = p.read_text(encoding="utf-8").strip().lower()
            if k in FORMS:
                return k
    except Exception:
        pass
    return ""


def current_form(cfg: Optional[Dict[str, Any]] = None) -> str:
    import os
    env = (os.environ.get("JILIU_FORM") or "").strip().lower()
    if env in FORMS:
        return env
    packed = _packaged_form()
    if packed:
        return packed
    k = str((cfg or {}).get("form") or "").strip().lower()
    return k if k in FORMS else DEFAULT_FORM


def form(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return dict(FORMS[current_form(cfg)])


def is_archive(cfg: Optional[Dict[str, Any]] = None) -> bool:
    return current_form(cfg) == "archive"


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
    """当前版本的能力表。界面按这个决定显示什么。

    版本（cn/global）与形态（oss/archive）合并成一张表：
    界面只关心「这个功能现在有没有」，不关心它属于哪个维度。
    """
    out = dict(EDITIONS[current(cfg)])
    f = form(cfg)
    out.update({
        "form": f["key"],
        "form_name": f["name"],
        "form_tagline": f["tagline"],
        "allow_video_sites": f["allow_video_sites"],
        "show_video_entry": f["show_video_entry"],
        "placeholder": f["placeholder"],
        "compliance": f["compliance"],
        "sells": f["sells"],
    })
    return out


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

    返回 {allowed, reason, foreign}。两层过滤，顺序不能反：

      1）形态：归档版不碰视频站。**先拦这个** —— 归档版的用户拿到一句
         「请升级到开源版」是没意义的，他们买的就不是那个能力。
      2）发行：国内版不碰海外站，并给出能指导下一步的原因
         （而不是一句「不支持」）。
    """
    # 形态层
    if not form(cfg).get("allow_video_sites", True):
        try:
            from . import sites as _sites
            hit = _sites.match(url)
        except Exception:
            hit = None
        if hit:
            return {
                "allowed": False,
                "foreign": False,
                "reason": f"这是 {hit.get('name') or '视频站'} 的链接。"
                          f"归档版只处理公开资源（直链 / RSS / 磁力 / 公开分享页），"
                          f"不解析视频站的播放页；需要这项能力请用开源版。",
            }

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
        "forms": [{"key": k, "name": v["name"], "tagline": v["tagline"],
                   "sells": v["sells"]} for k, v in FORMS.items()],
        "foreign_platforms": FOREIGN_PLATFORMS[:12] + ["…"],
        "switch_hint": "设置里可以切换版本，切换后需重启生效",
    }
