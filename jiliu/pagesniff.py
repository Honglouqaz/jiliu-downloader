"""网页视频解析（播放页 → 可下载的流地址）。

为什么需要这一层：

    yt-dlp 认识 1800+ 站点，但**免费影视站、短剧站、小众站点大多不在名单里**。
    它们的播放页结构却高度同质化 —— 苹果CMS(MacCMS) / 海洋CMS 系模板
    占了绝大多数，播放页里就是一段 `player_aaaa = {...}` 的 JSON，
    `url` 字段即 m3u8 直链：多清晰度用 `#` 分隔、再用 `$` 拆「名称/地址」，
    多线路就是 `player_bbbb` / `player_cccc` 各来一份。

    IDM / 4K Video Downloader 靠浏览器扩展在**运行时**嗅探这条请求；
    我们是下载器本体，可以直接从 HTML 里把它抠出来 —— 不用开播放器、
    不用看贴片广告、不用装扩展也能解析。

覆盖的六种形态（按实际命中率排序）：

    1) 播放器变量 `player_xxx = {...}`      苹果CMS / 海洋CMS / 派生模板
    2) `<video>` / `<source src>`、`data-*`  自建播放器、老站、短剧站
    3) `<iframe>` 里的播放器                 站外解析接口、第三方播放器（递归 2 层）
    4) 内联脚本里的媒体地址                  hls.js / dash.js 配置、__INITIAL_STATE__
    5) `og:video` / JSON-LD / twitter:player 正规站点的元数据
    6) 页面本身就是流地址                    直接透传

明确不做（也不假装能做）的四类：

    - DRM（Widevine / PlayReady / FairPlay）：分片是密文，密钥在版权方手上，
      浏览器的 blob 播放本身就靠许可证解密。腾讯/爱奇艺/Netflix 会员正片属于这类。
    - 播放地址二次加密（`encrypt=1`）：需要该站专用算法，改一版就失效，
      写进来就是技术债 + 侵权风险。
    - `blob:` + MSE 自定义分片：地址只存在内存里，只能靠浏览器扩展在运行时截获
      —— 那是 extension/ 的职责，不是这里。
    - 需要登录/付费的清晰度：带你自己的 Cookie 能拿到「你能看的档位」，
      但不绕付费墙（那是盗链服务干的事）。

一句话边界：**只解析任何人都能打开的那一页里公开的地址。**
"""
from __future__ import annotations

import html as _html
import json
import pathlib
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import cookies as _cookies
from . import proxy as _proxy

# 媒体后缀 → 类型。顺序有意义：m3u8 必须先判，否则会被 .ts 之类误伤。
KIND_BY_EXT = {
    ".m3u8": "hls", ".m3u": "hls",
    ".mpd": "dash",
    ".mp4": "file", ".m4v": "file", ".webm": "file", ".mkv": "file",
    ".mov": "file", ".flv": "file", ".avi": "file", ".ts": "file",
    ".mp3": "audio", ".m4a": "audio", ".aac": "audio", ".flac": "audio",
}
MEDIA_EXT = tuple(KIND_BY_EXT)

# 页面里明确出现这些字符串，就不是"没找到地址"，而是"有地址也下不了"
_DRM_HINTS = ("widevine", "playready", "fairplay", "com.apple.streamingkeydelivery",
              "skd://", "license?")
_BLOB_RE = re.compile(r"blob:\s*https?://", re.I)

# 噪声：这些命中通常是广告图 / 封面，不是片子
_NOISE_HOSTS = ("doubleclick", "googletagmanager", "google-analytics", "hm.baidu.com",
                "cnzz.com", "51.la", "umeng.com")

_MAX_HTML = 3 * 1024 * 1024        # 播放页超过 3MB 就不看了（正常也就 30-200KB）
_MAX_STREAMS = 40
_UA_FALLBACK = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _unescape(s: str) -> str:
    """JSON / JS 字符串里的转义还原。

    `https:\\/\\/cdn.x.com\\/a.m3u8` 这种在播放器配置里极常见 ——
    不还原的话 urljoin 会把它当成相对路径，拼出来的地址一定是错的。
    """
    if not s:
        return ""
    s = s.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    s = s.replace("\\u0026", "&").replace("\\u003D", "=").replace("\\u003d", "=")
    s = s.replace("\\u003F", "?").replace("\\u003f", "?")
    return _html.unescape(s.strip())


def kind_of(url: str) -> str:
    """按路径后缀判类型。查询串先剥掉，`a.m3u8?sign=xx` 同样是 HLS。"""
    try:
        path = urllib.parse.urlparse(url or "").path.lower()
    except Exception:      # noqa: BLE001
        return ""
    for ext, kind in KIND_BY_EXT.items():
        if path.endswith(ext):
            return kind
    return ""


def looks_media(url: str) -> bool:
    return bool(kind_of(url))


def _absolute(base: str, raw: str) -> str:
    """相对地址补全 + 去转义 + 去空白。"""
    u = _unescape(raw)
    if not u or u.startswith(("data:", "blob:", "javascript:", "about:", "#")):
        return ""
    try:
        return urllib.parse.urljoin(base, u)
    except Exception:      # noqa: BLE001
        return ""


def _is_noise(url: str) -> bool:
    low = (url or "").lower()
    return any(h in low for h in _NOISE_HOSTS)


def _brace_json(text: str, start: int) -> Optional[Dict[str, Any]]:
    """从 `{` 开始抓一个完整的 JSON 对象。

    不能用正则 `\\{.*?\\}`：播放器变量里嵌着 `link_next` 之类的子对象，
    非贪婪匹配会在一半断掉。这里按花括号计数，并且**跳过字符串里的括号**
    （标题里带 `{` 的情况虽然少见，但出了就是解析错位）。
    """
    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    depth = 0
    quote = ""
    esc = False
    for i in range(start, min(len(text), start + 20000)):
        ch = text[i]
        if quote:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = text[start:i + 1]
                try:
                    obj = json.loads(blob)
                except Exception:      # noqa: BLE001
                    # 单引号 key / 尾逗号：退回粗解析，别整个丢掉
                    obj = _loose_json(blob)
                return obj if isinstance(obj, dict) else None
    return None


def _loose_json(blob: str) -> Optional[Dict[str, Any]]:
    """给非严格 JSON 的兜底：只取 `"key":"value"` 与 `key:数字`。

    这类模板（尤其是二次改过的）可能带单引号 key 或尾逗号。
    能捞多少算多少 —— 我们要的只有 `url` 一个字段。
    """
    out: Dict[str, Any] = {}
    for m in re.finditer(r'["\']?([A-Za-z_][\w]*)["\']?\s*:\s*("(?:[^"\\]|\\.)*"|\d+|true|false)',
                         blob):
        k, v = m.group(1), m.group(2)
        if v.startswith('"'):
            out[k] = _unescape(v[1:-1])
        elif v in ("true", "false"):
            out[k] = (v == "true")
        else:
            try:
                out[k] = int(v)
            except Exception:      # noqa: BLE001
                out[k] = v
    return out or None


_ATTR_RE = re.compile(r'([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))')


def _tag_attrs(tag: str) -> Dict[str, str]:
    """把一个标签里的属性拆出来（大小写统一成小写）。"""
    out: Dict[str, str] = {}
    for m in _ATTR_RE.finditer(tag):
        k = m.group(1).lower()
        v = m.group(3) or m.group(4) or m.group(5) or ""
        out[k] = _unescape(_html.unescape(v))
    return out


def _split_multi(raw: str) -> List[Tuple[str, str]]:
    """拆「多清晰度 / 多集」串。

    苹果CMS 的两种写法这里都会遇到：
        4K$https://a/1.m3u8#1080P$https://b/2.m3u8      ← 多清晰度
        第01集$https://a/1.m3u8#第02集$https://a/2.m3u8  ← 多集
    分不清也不要紧：名称原样带出去给用户看，用户自己认得。
    """
    out: List[Tuple[str, str]] = []
    for part in str(raw or "").split("#"):
        part = part.strip()
        if not part:
            continue
        name, sep, url = part.partition("$")
        if not sep:
            name, url = "", part
        url = url.strip()
        if url:
            out.append((name.strip(), url))
    return out


# --------------------------------------------------------------------------
# 页面解析（纯函数，不碰网络 —— 单元测试全靠它）
# --------------------------------------------------------------------------

def _add(streams: List[Dict[str, Any]], url: str, *, how: str, base: str,
         label: str = "", encrypted: bool = False) -> None:
    """收一条候选。同地址只留第一条（先命中的来源通常最可靠）。

    去重按「去掉查询串」的形态比对：同一页里 `<video src="/a.m3u8?t=1">`
    会被内联脚本正则再捞一次成 `/a.m3u8`，两条其实是一条流 ——
    但带签名的那个更有用，所以**带查询串的优先保留**，不能反向覆盖。
    """
    if not url or len(streams) >= _MAX_STREAMS:
        return
    if _is_noise(url):
        return
    kind = kind_of(url)
    if not kind:
        return
    key = url.split("#")[0].split("?")[0]
    for i, s in enumerate(streams):
        if s["url"] == url:
            return
        if s["url"].split("#")[0].split("?")[0] == key:
            if "?" not in s["url"] and "?" in url:
                streams[i] = {"url": url, "kind": kind, "how": how,
                              "label": label or s.get("label", ""),
                              "encrypted": bool(encrypted), "from_url": base}
            return
    streams.append({
        "url": url,
        "kind": kind,
        "how": how,
        "label": label or "",
        "encrypted": bool(encrypted),
        "from_url": base,
    })


def parse_page(html_text: str, base_url: str) -> Dict[str, Any]:
    """从一页 HTML 里把所有可能的流地址抠出来。不发请求。"""
    text = str(html_text or "")
    streams: List[Dict[str, Any]] = []
    iframes: List[str] = []
    encrypted: List[Dict[str, Any]] = []
    notes: List[str] = []

    # ---- 1) 播放器变量 player_xxx = {...} ----
    # 名字不固定：player_aaaa / player_bbbb（多线路）/ player_AAAA，
    # 所以按前缀扫，不写死 aaaa。
    for m in re.finditer(r'\b(\w*player_[A-Za-z0-9_]+)\s*=\s*\{', text):
        name = m.group(1)
        obj = _brace_json(text, text.index("{", m.start()))
        if not obj:
            continue
        enc = int(obj.get("encrypt") or 0) if str(obj.get("encrypt") or "0").isdigit() else 0
        raw = str(obj.get("url") or "").strip()
        if not raw:
            continue
        pairs = _split_multi(raw)
        for label, u in pairs:
            full = _absolute(base_url, u)
            if enc:
                # encrypt=1：地址是密文，站点自己的 JS 解密。不做解密，但要让
                # 用户知道「不是没找到，是这站加密了」——两种情况的处置完全不同。
                if full or u:
                    encrypted.append({"how": name, "url": u[:200],
                                      "label": label, "encrypt": enc})
                continue
            _add(streams, full, how=name, base=base_url, label=label)

    # ---- 2) <video> / <source> / data-* ----
    for m in re.finditer(r"<video\b[^>]*>", text, re.I):
        a = _tag_attrs(m.group(0))
        for k in ("src", "data-src", "data-video", "data-url"):
            u = _absolute(base_url, a.get(k, ""))
            _add(streams, u, how="<video>", base=base_url)
    for m in re.finditer(r"<source\b[^>]*>", text, re.I):
        a = _tag_attrs(m.group(0))
        u = _absolute(base_url, a.get("src", ""))
        _add(streams, u, how="<source>", base=base_url, label=a.get("title", ""))

    # data-* 挂任意元素上（老站与短剧站很爱这么写）
    for m in re.finditer(r'data-(?:src|video|url|mp4|play|stream)\s*=\s*"([^"]{6,600})"', text, re.I):
        u = _absolute(base_url, m.group(1))
        _add(streams, u, how="data-*", base=base_url)

    # ---- 3) iframe（只收集，递归在 resolve 里做）----
    for m in re.finditer(r"<iframe\b[^>]*>", text, re.I):
        a = _tag_attrs(m.group(0))
        u = _absolute(base_url, a.get("src") or a.get("data-src") or "")
        if not u:
            continue
        if u not in iframes:
            iframes.append(u)
        # 播放器常把真实地址塞在 ?url= 参数里（`player/?url=https%3A%2F%2F...m3u8`）。
        # 顺手解一层百分号编码 —— 解不出东西就算了，不猜 base64。
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        except Exception:      # noqa: BLE001
            q = {}
        for vals in q.values():
            for v in vals:
                inner = _unescape(urllib.parse.unquote(v))
                if inner.startswith(("http://", "https://")) and looks_media(inner):
                    _add(streams, inner, how="iframe 参数", base=base_url)

    # ---- 4) 元数据 ----
    for m in re.finditer(r'<meta[^>]+(?:property|name)\s*=\s*["\']'
                         r'(?:og:video(?::url|:secure_url)?|twitter:player:stream)["\']'
                         r'[^>]*>', text, re.I):
        a = _tag_attrs(m.group(0))
        _add(streams, _absolute(base_url, a.get("content", "")), how="og:video", base=base_url)
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
                         text, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:      # noqa: BLE001
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            for k in ("contentUrl", "embedUrl", "url"):
                u = obj.get(k)
                if isinstance(u, str):
                    _add(streams, _absolute(base_url, u), how="JSON-LD", base=base_url)

    # ---- 5) 内联脚本里的媒体地址 ----
    # 这是覆盖面最广的一条：hls.js 的 source、window.__NUXT__、各种
    # `playUrl: "https://...m3u8"` 都会在这里被捞到。
    #
    # 必须先解 JS 转义再扫：`https:\/\/cdn.x.com\/a.m3u8` 里没有连续的 `//`，
    # 直接在原文上跑正则一定漏。`\u0026` 不还原的话，`?sign=a\u0026e=1`
    # 会被截成 `?sign=a` —— 带签名的 CDN 少了半个参数就是 403。
    scan = (text.replace("\\/", "/")
                .replace("\\u0026", "&").replace("\\u002F", "/").replace("\\u002f", "/")
                .replace("\\u003D", "=").replace("\\u003d", "=")
                .replace("\\u003F", "?").replace("\\u003f", "?"))
    for m in re.finditer(
            r'(?:(?:https?:)?//[^\s"\'<>\\)]{4,600}?)\.(?:m3u8|mpd|mp4|flv|m4v)(?:\?[^\s"\'<>\\)]{0,300})?',
            scan, re.I):
        u = _absolute(base_url, m.group(0))
        _add(streams, u, how="内联脚本", base=base_url)
    # 相对路径形式：`"url":"/vod/1/index.m3u8"`
    for m in re.finditer(r'["\'](/[^"\']{2,300}\.(?:m3u8|mpd|mp4|flv))(?:["\']|\?[^"\']*)',
                         scan, re.I):
        u = _absolute(base_url, m.group(1))
        _add(streams, u, how="内联脚本", base=base_url)

    # ---- 6) 页面本身就是流 ----
    if not streams and looks_media(base_url):
        _add(streams, base_url, how="直链", base=base_url)

    # ---- 判定「有地址但下不了」 ----
    low = text.lower()
    drm = [h for h in _DRM_HINTS if h in low]
    if drm:
        notes.append("页面出现 DRM 相关关键字（" + "、".join(drm[:2])
                     + "）：这类流有密文分片，任何下载器都拿不到明文")
    if _BLOB_RE.search(text):
        notes.append("页面用 blob: 播放（MSE 内存流）：真实地址不在 HTML 里，"
                     "需要在浏览器里运行时嗅探（装汲流扩展）")

    # 排序：HLS 优先（免费站主战场），其次整段文件，最后 DASH/音频
    rank = {"hls": 0, "file": 1, "dash": 2, "audio": 3}
    streams.sort(key=lambda s: rank.get(s["kind"], 9))
    return {"streams": streams, "iframes": iframes, "encrypted": encrypted,
            "notes": notes, "drm": bool(drm)}


def page_title(html_text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", str(html_text or ""), re.I | re.S)
    if not m:
        return ""
    t = _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    # 站点名尾巴（「_在线观看」「- 某某影院」）对文件名没意义
    t = re.split(r"\s*[-_|—]\s*(?:在线观看|在线播放|免费|高清|.*影院.*)$", t)[0]
    return t[:120]


# --------------------------------------------------------------------------
# 网络层
# --------------------------------------------------------------------------

def _fetch(url: str, cfg: Dict[str, Any], *, headers: Optional[Dict[str, str]] = None,
           timeout: float = 15.0) -> Tuple[int, str, str]:
    """取一页/一个 playist。返回 (status, final_url, text)。"""
    ua = str((headers or {}).get("User-Agent") or cfg.get("video_user_agent")
             or cfg.get("user_agent") or _UA_FALLBACK)
    h = {"User-Agent": ua, "Accept": "*/*",
         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    for k, v in (headers or {}).items():
        if v and k.lower() not in ("host", "accept-encoding"):
            h[k] = v
    # cookie 文件（用户导入过该站的）也带上 —— 有些站的播放页要登录才出地址
    if not any(k.lower() == "cookie" for k in h):
        f = _cookies.site_file(url)
        if f and pathlib.Path(f).is_file():
            text = pathlib.Path(f).read_text(encoding="utf-8", errors="replace")
            pairs = []
            for line in text.splitlines():
                if not line or line.startswith("#"):
                    continue
                cols = line.split("\t")
                if len(cols) >= 7:
                    pairs.append(f"{cols[5]}={cols[6]}")
            if pairs:
                h["Cookie"] = "; ".join(pairs[:60])

    req = urllib.request.Request(url, headers=h)
    op = _proxy.opener(cfg, url, timeout=timeout)
    with op.open(req, timeout=timeout) as r:
        raw = r.read(_MAX_HTML)
        ctype = (r.headers.get("Content-Type") or "").lower()
        final = r.geturl() or url
    enc = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        enc = m.group(1)
    else:
        head = raw[:2048].decode("ascii", "ignore").lower()
        if "gb2312" in head or "gbk" in head:
            enc = "gb18030"
    return r.status, final, raw.decode(enc, "replace")


def _hls_variants(url: str, cfg: Dict[str, Any],
                  headers: Optional[Dict[str, str]],
                  timeout: float = 8.0,
                  fetch: Optional[Callable[..., Tuple[int, str, str]]] = None,
                  notes: Optional[List[str]] = None
                  ) -> List[Dict[str, Any]]:
    """拉 master playlist，把清晰度列出来。

    只读 EXT-X-STREAM-INF 段 —— 用 live.parse_playlist 会把几千行分片也
    解析一遍，这里不需要。

    fetch 要能注入：单元测试不能真发请求，而这条路径决定了"能不能选清晰度"，
    必须被测试覆盖。
    """
    try:
        _, _, text = (fetch or _fetch)(url, cfg, headers=headers, timeout=timeout)
    except Exception as exc:      # noqa: BLE001
        # 以前静默 return []：防盗链 403 时用户只看到 master 直链，
        # 所有清晰度凭空消失，也不知道为什么。
        if notes is not None:
            notes.append(f"清晰度探测失败（{type(exc).__name__}: {exc}）")
        return []
    if "#EXT-X-STREAM-INF" not in text:
        return []
    out: List[Dict[str, Any]] = []
    pending: Dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            pending = {}
            res = re.search(r"RESOLUTION=(\d+x\d+)", line)
            bw = re.search(r"BANDWIDTH=(\d+)", line)
            nm = re.search(r'NAME="([^"]+)"', line)
            pending = {"resolution": res.group(1) if res else "",
                       "bandwidth": int(bw.group(1)) if bw else 0,
                       "name": nm.group(1) if nm else ""}
            continue
        if line.startswith("#"):
            continue
        if pending:
            pending["url"] = urllib.parse.urljoin(url, line)
            out.append(pending)
            pending = {}
    return out


def _quality_label(res: str, bw: int) -> str:
    if res:
        h = res.split("x")[-1]
        if h.isdigit():
            return f"{h}P"
        return res
    if bw:
        return f"{round(bw / 1000)}kbps"
    return ""


# --------------------------------------------------------------------------
# 对外：解析一个播放页
# --------------------------------------------------------------------------

def resolve(url: str, cfg: Dict[str, Any], *,
            depth: int = 2,
            timeout: float = 15.0,
            probe_quality: bool = True,
            page_headers: Optional[Dict[str, str]] = None,
            fetch: Optional[Callable[..., Tuple[int, str, str]]] = None) -> Dict[str, Any]:
    """播放页 → 候选流清单。

    参数：
        depth           iframe 递归层数（默认 2：播放页 → 播放器页 → 再一层）
        probe_quality   HLS 是否拉 master playlist 展开清晰度（多花 0.3~2s）
        page_headers    调用方带的请求头（扩展传来的 cookie/referer 就走这里）
        fetch           注入点，单元测试用；生产走 _fetch

    返回：
        {ok, page:{url, final_url, title, host}, streams:[...], iframes, encrypted,
         notes, elapsed, boundary}
    """
    started = time.monotonic()
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "请给一个 http(s) 播放页地址", "streams": []}
    f = fetch or _fetch

    out = {
        "ok": True,
        "page": {"url": url, "final_url": url, "title": "", "host": ""},
        "streams": [],
        "iframes": [],
        "encrypted": [],
        "notes": [],
        "visited": [],
        "elapsed": 0.0,
        "boundary": "只解析页面里公开的播放地址；不做 DRM 破解、不绕付费墙",
    }

    # 直接给的就是流地址：不用抓页面，省一次往返（但清晰度照样要探 ——
    # 扩展嗅探到的往往就是 master m3u8，用户要能选档）
    direct = looks_media(url)
    all_streams: List[Dict[str, Any]] = []
    if direct:
        out["page"]["host"] = urllib.parse.urlparse(url).hostname or ""
        all_streams = [{
            "url": url, "kind": kind_of(url), "how": "直链", "label": "",
            "encrypted": False, "from_url": url,
            "headers": _headers_for(url, url, page_headers, cfg),
        }]

    queue: List[Tuple[str, int]] = [] if direct else [(url, 0)]
    seen: set = set()
    iframes: List[str] = []
    notes: List[str] = []
    drm = False
    first_err = ""

    while queue and len(seen) < 5:
        cur, lvl = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        try:
            status, final_url, text = f(cur, cfg, headers=page_headers, timeout=timeout)
        except Exception as exc:      # noqa: BLE001
            if not first_err:
                first_err = f"{type(exc).__name__}: {exc}"
            continue
        if status >= 400:
            if not first_err:
                first_err = f"HTTP {status}"
            continue

        if cur == url:
            out["page"].update(final_url=final_url, title=page_title(text),
                               host=urllib.parse.urlparse(final_url).hostname or "")

        parsed = parse_page(text, final_url)
        for s in parsed["streams"]:
            if any(x["url"] == s["url"] for x in all_streams):
                continue
            s["headers"] = _headers_for(s["url"], s["from_url"] or final_url, page_headers, cfg)
            all_streams.append(s)
        for k in parsed["encrypted"]:
            if k not in out["encrypted"]:
                out["encrypted"].append(k)
        for n in parsed["notes"]:
            if n not in notes:
                notes.append(n)
        drm = drm or parsed["drm"]

        for ifr in parsed["iframes"]:
            if ifr not in iframes:
                iframes.append(ifr)
            if lvl + 1 <= depth and ifr not in seen:
                queue.append((ifr, lvl + 1))
        out["visited"].append({"url": cur, "level": lvl,
                               "found": len(parsed["streams"])})

    # HLS master → 展开清晰度（只探前 3 条，避免一页十几个流把时间拖长）
    if probe_quality:
        expanded: List[Dict[str, Any]] = []
        probed = 0
        for s in all_streams:
            if s["kind"] == "hls" and probed < 3:
                probed += 1
                vs = _hls_variants(s["url"], cfg, s.get("headers"), fetch=f, notes=notes)
                if len(vs) > 1:
                    vs.sort(key=lambda v: (v.get("bandwidth") or 0), reverse=True)
                    for v in vs:
                        ql = _quality_label(v.get("resolution", ""), v.get("bandwidth") or 0)
                        expanded.append({
                            **s,
                            "url": v["url"],
                            "label": v.get("name") or ql or s.get("label", ""),
                            "quality": ql,
                            "variants_of": s["url"],
                        })
                    continue
            expanded.append(s)
        all_streams = expanded

    # 去重（展开后同一 url 可能出现两次）
    dedup: List[Dict[str, Any]] = []
    for s in all_streams:
        if not any(x["url"] == s["url"] for x in dedup):
            dedup.append(s)

    rank = {"hls": 0, "file": 1, "dash": 2, "audio": 3}
    dedup.sort(key=lambda s: (rank.get(s["kind"], 9),
                              -int((s.get("quality") or "0").replace("P", "") or 0)))

    out["streams"] = dedup[:30]
    out["iframes"] = iframes[:10]
    out["notes"] = notes
    out["drm"] = drm
    out["elapsed"] = round(time.monotonic() - started, 2)

    if not out["streams"]:
        out["ok"] = False
        if drm:
            out["error"] = ("页面用的是 DRM 加密流（Widevine/FairPlay 之类），"
                            "分片是密文，拿到了地址也解不开")
            out["hint"] = "这类内容只能在其客户端里在线看，任何下载器都做不到"
        elif out["encrypted"]:
            out["error"] = "这个站对播放地址做了二次加密（encrypt=1），不是标准 m3u8"
            out["hint"] = ("地址是站点自己的 JS 解出来的，规则随版本变。"
                           "改用浏览器扩展在播放时嗅探（这是 IDM 的做法，也是唯一稳的办法）")
        elif first_err:
            out["error"] = f"抓播放页失败：{first_err}"
            out["hint"] = "确认这个地址能在浏览器直接打开；需要登录的站请先导入 Cookie"
        else:
            out["error"] = "页面里没找到能直接下载的流地址"
            out["hint"] = ("可能原因：① 播放器在更深一层的 iframe 里（把播放器页地址直接粘进来）；"
                           "② blob:/MSE 播放，地址只在内存里 —— 装浏览器扩展在播放时嗅探；"
                           "③ 站点用自研加密播放器")
    return out


def _headers_for(stream: str, page: str, extra: Optional[Dict[str, str]],
                 cfg: Dict[str, Any]) -> Dict[str, str]:
    """该带的请求头。

    CDN 防盗链 90% 查三样：Referer、UA、Cookie。少一样就是 403，
    而用户看到的是「地址没错却下不动」。
    """
    h: Dict[str, str] = {}
    ua = str((extra or {}).get("User-Agent") or cfg.get("video_user_agent")
             or cfg.get("user_agent") or _UA_FALLBACK)
    h["User-Agent"] = ua
    if page:
        h["Referer"] = page
        p = urllib.parse.urlparse(page)
        if p.scheme and p.netloc:
            h["Origin"] = f"{p.scheme}://{p.netloc}"
    for k, v in (extra or {}).items():
        if v:
            h[k] = v
    return h


def describe() -> Dict[str, Any]:
    """给界面/关于页用的能力说明。"""
    return {
        "forms": [
            {"key": "player_var", "label": "播放器变量",
             "desc": "苹果CMS / 海洋CMS 系的 player_xxx 配置，覆盖最多免费影视站"},
            {"key": "video_tag", "label": "video / source / data-*",
             "desc": "自建播放器、短剧站最常见的写法"},
            {"key": "iframe", "label": "iframe 内嵌播放器",
             "desc": "播放器在另一页面里时递归进去（默认 2 层）"},
            {"key": "inline", "label": "内联脚本地址",
             "desc": "hls.js/dash.js 配置、window.__INITIAL_STATE__ 里的流地址"},
            {"key": "meta", "label": "og:video / JSON-LD",
             "desc": "正规站点的结构化元数据"},
            {"key": "direct", "label": "流直链",
             "desc": "m3u8 / mpd / mp4 直接粘进来"},
        ],
        "unsupported": [
            {"key": "drm", "label": "DRM 加密（Widevine/PlayReady/FairPlay）",
             "why": "分片是密文，密钥在版权方，浏览器播放也要许可证"},
            {"key": "encrypt", "label": "播放地址二次加密（encrypt=1）",
             "why": "需要站点专用算法，随版本失效"},
            {"key": "blob", "label": "blob: + MSE 自定义分片",
             "why": "地址只在内存里，只能运行时嗅探（扩展负责）"},
            {"key": "paywall", "label": "付费/会员清晰度",
             "why": "带自己的 Cookie 能拿到你能看的档位，但不绕付费墙"},
        ],
    }
