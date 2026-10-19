"""视频站点注册表。

为什么要单独维护一份站表：

  1）**判断「这是不是视频页」不能靠扩展名。** `https://v.qq.com/x/cover/xxx.html`
     看着是个 HTML，实际是播放页，该交给 yt-dlp 而不是当网页下下来。
     反过来，直链 `.mp4` 也不一定在视频站上。所以判定必须落到站点维度。
  2）**国内站的可用性差别很大**，而且会变。哪家能免登录拿 1080P、
     哪家必须登录、哪家已经彻底拿不到，这些事实得有地方记 ——
     记在代码旁边，比记在某个人的脑子里强。
  3）**测试需要清单。** 主人要求「国内一般的视频网站链接都要做测试」，
     测试的对象就是这个表。

字段说明：
    hosts     域名（含子域匹配）
    patterns  播放页路径特征。留空 = 该站任意页面都当视频页试
    auth      拿高清/完整内容所需的登录态，用于在界面上如实提示
    probe     默认的测试样例链接（可以是一个能公开访问的页面）
    note      现状说明
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List, Optional

SITES: List[Dict[str, Any]] = [
    # ---------------- 国内 ----------------
    {
        "key": "bilibili", "name": "哔哩哔哩", "domestic": True,
        "hosts": ["bilibili.com", "b23.tv", "acg.tv"],
        "patterns": [r"/video/([Bb][Vv]|[Aa][Vv])\w+", r"/bangumi/play/",
                     r"/list/", r"/medialist/", r"b23\.tv/", r"/festival/"],
        "auth": "1080P 以上需要登录；4K/8K 需要大会员",
        "probe": "https://www.bilibili.com/video/BV1GJ411x7h7",
        "note": "✅ 实测可用（免登录拿 1080P，15 档格式）。支持最好的一家："
                "有官方扫码登录、合集/分P 有结构化接口",
    },
    {
        "key": "youku", "name": "优酷", "domestic": True,
        "hosts": ["youku.com", "tudou.com"],
        "patterns": [r"/v_show/", r"player\.youku", r"/album/"],
        "auth": "多数内容需要登录；会员内容下不了高清",
        "probe": "https://v.youku.com/v_show/id_XNTk1NTQ3MzY4MA==.html",
        "note": "✅ 实测可用（免登录 1080P）。yt-dlp 有 extractor",
    },
    {
        "key": "qq", "name": "腾讯视频", "domestic": True,
        "hosts": ["v.qq.com", "vv.video.qq.com"],
        "patterns": [r"/x/cover/", r"/x/page/", r"/x/detail/"],
        "auth": "普遍需要登录；会员正片是 DRM 加密，属于硬边界",
        "probe": "https://v.qq.com/x/cover/mzc00200mp8vo9b.html",
        "note": "✅ 实测可解析免费内容（yt-dlp 认 `/x/cover/<cid>.html` 剧集页；"
                "不带登录态通常只给试看片段）。⚠️ VIP 正片走 DRM 加密"
                "（Widevine），分片是密文 —— 汲流不做第三方 VIP 破解解析"
                "（盗链服务，侵权且随时失效）。带自己的 Cookie 能拿到你能看的清晰度。",
    },
    {
        "key": "iqiyi", "name": "爱奇艺", "domestic": True,
        "hosts": ["iqiyi.com", "pps.tv", "iq.com"],
        "patterns": [r"/v_", r"/a_", r"player\.iqiyi"],
        "auth": "需要登录；VIP 内容受限",
        "probe": "https://www.iqiyi.com/v_ex2qv89oe0.html",
        "note": "⚠️ 实测：yt-dlp 的 iqiyi 解析器当前失效（换多个有效播放页均报 "
                "Can't find any video，非链接问题、非 Cookie 问题）——爱奇艺改版后"
                "上游未跟上。要下只能自研接口或等 yt-dlp 更新。",
    },
    {
        "key": "mgtv", "name": "芒果TV", "domestic": True,
        "hosts": ["mgtv.com", "hunantv.com"],
        "patterns": [r"/b/\d+", r"/h/"],
        "auth": "部分内容需要登录/会员",
        "probe": "https://www.mgtv.com/b/896231/24547204.html",
        "note": "✅ 实测可用。⚠️ 老链接会 404（节目下架就换页），"
                "要用当前的 /b/<栏目id>/<集id>.html",
    },
    {
        "key": "xigua", "name": "西瓜视频", "domestic": True,
        "hosts": ["ixigua.com"],
        "patterns": [r"/\d{6,}", r"v\.ixigua\.com/"],
        "auth": "yt-dlp 要求带 Cookie（不必登录，但要一份有效 Cookie）",
        "probe": "https://www.ixigua.com/7309889994192224783",
        "note": "⚠️ 实测：不带 Cookie 报 `Ixigua: Cookies (not necessarily "
                "logged in) are needed`。短链 v.ixigua.com 需先展开。",
    },
    {
        "key": "douyin", "name": "抖音", "domestic": True,
        "hosts": ["douyin.com", "iesdouyin.com"],
        "patterns": [r"/video/\d+", r"v\.douyin\.com/", r"/note/"],
        "auth": "yt-dlp 要求一份**新鲜**的 Cookie（不必登录）",
        "probe": "https://www.douyin.com/video/7013182973793160485",
        "note": "⚠️ 实测：报 `Douyin: Fresh cookies (not necessarily logged in) "
                "are needed` —— 旧的 Cookie 也不行，得现取。",
    },
    {
        "key": "kuaishou", "name": "快手", "domestic": True,
        "hosts": ["kuaishou.com", "gifshow.com", "chenzhongtech.com"],
        "patterns": [r"/short-video/", r"/f/", r"v\.kuaishou\.com/"],
        "auth": "多数需要登录态",
        "probe": "https://www.kuaishou.com/",
        "note": "❌ 实测：yt-dlp 没有快手的解析器（域名覆盖为空）。"
                "要支持只能自研接口。",
    },
    {
        "key": "weibo", "name": "微博", "domestic": True,
        "hosts": ["weibo.com", "weibo.cn", "t.cn"],
        "patterns": [r"/tv/show/", r"/status/", r"/u/\d+", r"video\.weibo"],
        "auth": "视频部分需要登录",
        "probe": "https://weibo.com/tv/show/1034:4686345264889977",
        "note": "✅ 实测可用（免登录）。秒拍/微博视频都在这个域下，"
                "短链 t.cn 需要先展开",
    },
    {
        "key": "xiaohongshu", "name": "小红书", "domestic": True,
        "hosts": ["xiaohongshu.com", "xhslink.com"],
        "patterns": [r"/explore/[0-9a-f]{16,}", r"/discovery/item/",
                     r"xhslink\.com/"],
        "auth": "基本都要登录态",
        "probe": "https://www.xiaohongshu.com/explore",
        "note": "⚠️ 实测：不带登录态时图文帖直接报 `No video formats found`，"
                "必须给**视频**帖链接 + 登录 Cookie。原 patterns 里的 "
                "`/explore/` 太宽，图文帖也会被当视频页，已收紧成带 id 的形态。",
    },
    {
        "key": "acfun", "name": "AcFun", "domestic": True,
        "hosts": ["acfun.cn"],
        "patterns": [r"/v/ac", r"/bangumi/", r"/v/ab"],
        "auth": "1080P 需登录",
        "probe": "https://www.acfun.cn/v/ac4741185",
        "note": "✅ 实测可用（免登录拿到 720P/1080P 多档）。与 B站同类",
    },
    {
        "key": "sohu", "name": "搜狐视频", "domestic": True,
        "hosts": ["sohu.com"],
        "patterns": [r"/v/", r"/tv/", r"/album/"],
        "auth": "多数免费",
        "probe": "http://tv.sohu.com/v/MjAyNTA4MDcvbjYyMDA4OTcwMy5zaHRtbA==.html",
        "note": "⚠️ 实测：解析器认链接（SohuV），但接口会 **429 限流** ——"
                "换代理 IP 或隔一会儿再试，不是代码问题。老片源多",
    },
    {
        "key": "migu", "name": "咪咕视频", "domestic": True,
        "hosts": ["miguvideo.com", "migu.cn"],
        "patterns": [r"/p/detail/", r"/detail/", r"/drama/"],
        "auth": "体育赛事需会员",
        "probe": "https://www.miguvideo.com/",
        "note": "❌ 实测：yt-dlp 没有咪咕的解析器。体育与影视",
    },
    {
        "key": "cctv", "name": "央视网", "domestic": True,
        "hosts": ["cctv.com", "cntv.cn", "cctv.cn", "yangshipin.cn"],
        "patterns": [r"/\d{4}/\d{2}/\d{2}/VIDE\w+", r"/video/", r"/live/"],
        "auth": "免费，但部分栏目地区限制",
        "probe": ("https://tv.cctv.com/2024/09/17/"
                  "VIDETyWYmriBWGNooHKTyDii240917.shtml"),
        "note": "✅ 实测可用（免登录）。⚠️ yt-dlp 的 `CCTV` 解析器只认 "
                "`/<年>/<月>/<日>/VIDE….shtml` 和 `/video/` 这类具体页，"
                "栏目页（如 /live/index.shtml）不算播放页。",
    },
    {
        "key": "pptv", "name": "PP视频", "domestic": True,
        "hosts": ["pptv.com"],
        "patterns": [r"/show/", r"/v/"],
        "auth": "多数免费",
        "probe": "http://v.pptv.com/show/IiaELiafFXxwVo5k4.html",
        "note": "❌ 实测：yt-dlp 没有 PP视频的解析器。老牌站点，源多为低清",
    },
    {
        "key": "1905", "name": "电影网", "domestic": True,
        "hosts": ["1905.com"],
        "patterns": [r"/vod/", r"/video/play/", r"/video/"],
        "auth": "多数免费",
        "probe": "https://www.1905.com/",
        "note": "❌ 实测：yt-dlp 没有 1905 的解析器；且站点对非浏览器 UA "
                "直接 **403**，连首页都抓不到。要支持只能自研接口。",
    },
    # ---------------- 海外 ----------------
    {
        "key": "youtube", "name": "YouTube", "domestic": False,
        "hosts": ["youtube.com", "youtu.be", "youtube-nocookie.com"],
        "patterns": [r"/watch", r"youtu\.be/", r"/playlist", r"/shorts/", r"/live/"],
        "auth": "风控严格：需本机代理 + 浏览器登录 Cookie，两者缺一不可",
        "probe": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "note": "报 Sign in to confirm you're not a bot 是风控，不是解析器坏了",
    },
    {
        "key": "vimeo", "name": "Vimeo", "domestic": False,
        "hosts": ["vimeo.com"],
        # ⚠️ 别用宽泛的 r"/\d+"：player.vimeo.com/progressive_redirect/... 这类
        #    直链里也有数字，会被误当成播放页。只认真正的视频页形态。
        "patterns": [r"vimeo\.com/\d+", r"player\.vimeo\.com/video/\d+"],
        "auth": "公开视频免登录", "probe": "https://vimeo.com/76979871",
        "note": "⚠️ 实测：走代理能连上，但报 `The web client only works when…` "
                "——需要浏览器登录 Cookie 才给密钥。免登录可下的公开视频变少了。",
    },
    {
        "key": "twitch", "name": "Twitch", "domestic": False,
        "hosts": ["twitch.tv"],
        # ⚠️ 同理：原先的 r"/[a-z0-9_]+$" 会把 gql.twitch.tv/gql 判成播放页。
        "patterns": [r"/videos/\d+", r"clips\.twitch\.tv/", r"/clip/"],
        "auth": "回放需要登录", "probe": "https://www.twitch.tv/twitch",
        "note": "⚠️ 实测：频道页只在**正在直播**时可解析，主播没开播就报 "
                "`The channel is not currently live`。回放有 7~60 天过期，"
                "链接失效属正常。",
    },
    {
        "key": "twitter", "name": "X / Twitter", "domestic": False,
        "hosts": ["twitter.com", "x.com"], "patterns": [r"/status/\d+"],
        "auth": "需要登录态",
        "probe": "https://x.com/",
        "note": "⚠️ 实测：解析器在，但只认 `/status/<id>` 的**单条推文**链接；"
                "首页不算。且基本都需要登录 Cookie。",
    },
    {
        "key": "tiktok", "name": "TikTok", "domestic": False,
        "hosts": ["tiktok.com"], "patterns": [r"/video/\d+", r"/@[^/]+/video/"],
        "auth": "多数需要登录",
        "probe": "https://www.tiktok.com/",
        "note": "⚠️ 实测：解析器在，但只认 `/@user/video/<id>` 的**具体视频页**；"
                "首页不算。多数内容需要登录 Cookie。",
    },
]


def host_of(url: str) -> str:
    s = (url or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    try:
        return (urllib.parse.urlparse(s).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def match(url: str) -> Optional[Dict[str, Any]]:
    """这个链接属于哪个视频站。认不出返回 None。"""
    host = host_of(url)
    if not host:
        return None
    for s in SITES:
        for h in s["hosts"]:
            if host == h or host.endswith("." + h):
                return s
    return None


def is_video_page(url: str) -> bool:
    """是不是某个视频站的播放页。

    认得站点但路径不像播放页（比如 B站首页）也不算 —— 那种交给普通下载，
    硬塞给 yt-dlp 只会得到一个「不支持的 URL」。
    """
    s = match(url)
    if s is None:
        return False
    pats = s.get("patterns") or []
    if not pats:
        return True
    u = url or ""
    return any(re.search(p, u, re.I) for p in pats)


def domestic_sites() -> List[Dict[str, Any]]:
    return [s for s in SITES if s.get("domestic")]


def foreign_sites() -> List[Dict[str, Any]]:
    return [s for s in SITES if not s.get("domestic")]


def describe() -> Dict[str, Any]:
    return {
        "total": len(SITES),
        "domestic": len(domestic_sites()),
        "foreign": len(foreign_sites()),
        "sites": [
            {"key": s["key"], "name": s["name"], "domestic": s["domestic"],
             "hosts": s["hosts"], "auth": s.get("auth", ""),
             "probe": s.get("probe", ""), "note": s.get("note", "")}
            for s in SITES
        ],
    }
