"""抖音无水印直链解析（v1.5.1，借鉴 videdown 的思路后自己实现）。

为什么单独做一个模块而不靠 yt-dlp：
    yt-dlp 的抖音提取器长期要登录态（缺 cookie 就报空 formats），
    而手机分享出来的链接走的是 iesdouyin 的 share 页 —— 那个页面
    不登录也能拿到视频数据（内嵌在 `window._ROUTER_DATA` JSON 里）。
    从那里取 play_addr 拼直链，是公开接口性质的路子：不逆向加密
    参数、不碰私有签名，页面自己返回什么我们就用什么。

无水印的原理（一句话）：
    抖音给每条视频一个内部 id（play_addr.uri，形如 v0d00fg10000…）。
    `download_addr` 是带水印的播放地址，而
    `https://aweme.snssdk.com/aweme/v1/play/?video_id=<uri>&ratio=1080p`
    返回的是同一视频的**无水印**源。uri 是页面 JSON 里就有的，不用算。

⚠ 2026-09 真网络实测结论（写在最显眼的地方，免得重走弯路）：
    分享短链跳转 → 拿 vid 这段**是通的**（实测 v.douyin.com → iesdouyin
    share 页正常）。但 2026 版分享页 SSR 只回壳子（ua/query/commonContext），
    item_list / play_addr 不再内嵌；数据由客户端 JS 从带 argus/msToken
    签名的接口拉。免登录的公开 API（iteminfo）已死（返回空），
    web detail 接口 403 要签名，bot UA 被拒。
    **不逆向签名（项目红线）。**
    抖音的现实路径收敛为：douyin.com 的 cookie（扫码登录或游客导入均可，
    yt-dlp 实测只要新鲜 cookie、不必登录）+ yt-dlp 解析。
    resolve() 保留 _ROUTER_DATA 解析作为前向兼容 —— 哪天抖音把数据
    放回 SSR，这里自动就能用。

边界（说清楚，免得以后自己都忘了）：
    1. 只处理公开视频。私密/仅粉丝可见的拿不到，跟登录无关。
    2. 这是「页面给什么用什么」—— 抖音改版（比如 _ROUTER_DATA 改名）
       这里就会失效，所以 resolve() 的失败信息要把「怎么判断是改版」
       写出来，别让下一个接手的人猜。
    3. 音频单独提取（BGM）不做 —— 那要另走 /music/ 接口，先不做。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import proxy as _proxy

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# 各形态的抖音链接 → 视频 id
_URL_PATTERNS = [
    re.compile(r"douyin\.com/video/(\d+)"),
    re.compile(r"iesdouyin\.com/share/video/(\d+)"),
    re.compile(r"iesdouyin\.com/share/note/(\d+)"),
    re.compile(r"douyin\.com/note/(\d+)"),
    re.compile(r"[?&]modal_id=(\d+)"),
]

# 分享文案里带一堆乱码文字，真正的 URL 藏在里面
_URL_IN_TEXT = re.compile(r"https?://[^\s，,。\"'）)]+")


def looks_like_douyin(url: str) -> bool:
    u = (url or "").lower()
    return ("douyin.com" in u or "iesdouyin.com" in u
            or "douyinpic.com" in u or "snssdk.com" in u)


def extract_url(text: str) -> str:
    """从分享文案里抠出第一条像样的链接。"""
    for m in _URL_IN_TEXT.findall(text or ""):
        if looks_like_douyin(m):
            return m.rstrip(")/.,")
    return (text or "").strip()


def parse_video_id(url: str) -> str:
    """识别视频 id。v.douyin.com 短链要等重定向之后才有。"""
    for pat in _URL_PATTERNS:
        m = pat.search(url or "")
        if m:
            return m.group(1)
    return ""


def _share_page_url(vid: str) -> str:
    return f"https://www.iesdouyin.com/share/video/{vid}/"


def _fetch(url: str, cfg: Dict[str, Any], timeout: int = 20
           ) -> Tuple[int, str, str]:
    """GET 一个页面，返回 (状态码, 最终URL, 正文)。跟随重定向。"""
    op = _proxy.opener(cfg, url, timeout=timeout)
    req = urllib.request.Request(url, headers={
        "User-Agent": MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with op.open(req, timeout=timeout) as r:
            return r.status, r.geturl(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也要把 body 拿出来 —— 抖音的风控页里会写原因
        try:
            body = e.read().decode("utf-8", "replace")[:2000]
        except Exception:
            body = ""
        return e.code, url, body
    except Exception as exc:  # noqa: BLE001
        return 0, url, f"{type(exc).__name__}: {exc}"


_ROUTER_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>",
                        re.S)


def _extract_router_data(html: str) -> Optional[Dict[str, Any]]:
    """从 share 页 HTML 里抠 _ROUTER_DATA JSON。"""
    m = _ROUTER_RE.search(html or "")
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _find_item(router: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """_ROUTER_DATA 的 loaderData 键名带视频 id（如 video_(7341...)/page），
    不能写死 —— 遍历找第一个带 item_list 的。"""
    for v in (router.get("loaderData") or {}).values():
        if not isinstance(v, dict):
            continue
        items = (v.get("videoInfoRes") or {}).get("item_list") or []
        for it in items:
            if isinstance(it, dict) and it.get("video"):
                return it
    return None


def _play_addr(item: Dict[str, Any]) -> Dict[str, Any]:
    v = item.get("video") or {}
    pa = v.get("play_addr") or {}
    uri = str(pa.get("uri") or "").strip()
    urls: List[str] = [str(u) for u in (pa.get("url_list") or []) if u]
    no_wm = ""
    if uri:
        no_wm = ("https://aweme.snssdk.com/aweme/v1/play/?"
                 + urllib.parse.urlencode({"video_id": uri, "ratio": "1080p", "line": "0"}))
    return {"uri": uri, "url_list": urls, "nowatermark": no_wm}


def resolve(url: str, cfg: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    """解析一条抖音链接 → 无水印直链 + 元信息。

    返回 {ok, url(无水印直链), fallback_urls, title, author, cover,
          duration, vid, via}；失败时 ok=False + 可读的 error。
    """
    raw = extract_url(url or "")
    if not raw:
        return {"ok": False, "error": "没有可解析的链接"}
    if not looks_like_douyin(raw):
        return {"ok": False, "error": "这不是抖音链接（只认 douyin.com / iesdouyin.com / v.douyin.com）"}

    vid = parse_video_id(raw)
    page_url = raw

    # 短链：跟随重定向拿真实视频 id
    if not vid:
        code, final, _ = _fetch(raw, cfg, timeout=timeout)
        if code and "douyin" in final:
            vid = parse_video_id(final)
            page_url = final
        if not vid:
            return {"ok": False, "error":
                    "短链重定向后没认出视频 id。可能是链接失效，或抖音改了跳转格式。",
                    "status": code, "final_url": final}

    code, _, html = _fetch(_share_page_url(vid), cfg, timeout=timeout)
    if code != 200:
        return {"ok": False, "vid": vid, "status": code, "error":
                f"分享页请求失败（HTTP {code or '网络错误'}）。"
                + ("像是被风控了，过会儿再试。" if code in (403, 412) else "")}

    router = _extract_router_data(html)
    item = _find_item(router) if router else None

    if item is None:
        # _ROUTER_DATA 拿不到 —— 2026-09 实测：分享页 SSR 只回壳
        # （ua/query/commonContext），item_list 不再内嵌；数据由客户端
        # JS 从带 argus/msToken 签名的接口拉。不逆向签名（项目红线）。
        # 现实路径：有 douyin.com.txt cookie（扫码登录或导入的游客 cookie
        # 都行，yt-dlp 实测只要新鲜 cookie、不必登录）就交给 yt-dlp，
        # 按域名的 cookie 机制会自动带上 —— 这里把下一步说清楚。
        from . import cookies as _cookies
        has_ck = _cookies.site_file(url) is not None
        return {"ok": False, "vid": vid, "error":
                ("分享页已不给视频数据（2026 版 SSR 只回壳子）。"
                 "但这条链接的账号侧已经通了：检测到 douyin.com 的 cookie，"
                 "直接用「下载视频」走 yt-dlp 解析即可（会自动带上 cookie）。")
                if has_ck else
                ("分享页已不给视频数据（2026 版 SSR 只回壳子，接口带签名，"
                 "汲流不逆向私有接口）。可行做法：账号登录 → 扫码登录抖音"
                 "（哪怕不登录账号，扫码拿到的游客 cookie 也够 yt-dlp 解析），"
                 "之后正常「下载视频」即可 —— cookie 会自动带上、按站点隔离。")}


    pa = _play_addr(item)
    target = pa["nowatermark"] or (pa["url_list"][0] if pa["url_list"] else "")

    return {
        "ok": bool(target),
        "vid": vid,
        "via": "iesdouyin-share-page",
        "url": target,
        "nowatermark": bool(pa["nowatermark"]),
        "fallback_urls": pa["url_list"],
        "title": str(item.get("desc") or "").strip()[:200],
        "author": str((item.get("author") or {}).get("nickname") or "").strip(),
        "cover": str(((item.get("video") or {}).get("cover") or {})
                     .get("url_list", [""])[0] if (item.get("video") or {}).get("cover") else ""),
        "duration": (item.get("video") or {}).get("duration") or 0,
        "page_url": page_url,
    }


def describe() -> Dict[str, Any]:
    """给设置页/体检看的模块说明。"""
    return {
        "name": "抖音无水印解析",
        "endpoint": "/api/douyin/resolve",
        "note": "走 iesdouyin 公开分享页取 play_addr，不逆向签名；抖音改版会失效",
    }
