"""B 站专项：扫码登录、分P/合集列表、弹幕、字幕。

为什么单独一个模块 —— B站有些东西 yt-dlp 拿不到或不划算：

  - **扫码登录**：yt-dlp 只能从浏览器 cookie 库读，而 Chrome/Edge 新版
    加密读不了。B站官方支持扫码登录，直接调它的接口最干净。
  - **分P / 合集的结构化列表**：yt-dlp 的 `--flat-playlist` 对 B站
    只回一个 url，**连标题都没有**（实测：flat 模式 4 个条目全是 None）。
    而 B站自己的接口一次就给出全部标题、时长、时长，还快得多。
  - **弹幕**：yt-dlp 完全不管这个。
  - **官方字幕**：yt-dlp 要走完整下载流程，这里单独拿更灵活。

踩过的坑（写在这儿免得再犯）：
    B站对没有 buvid 的请求会返回 **412 Precondition Failed**。
    裸 urllib 打 api.bilibili.com 会被挡掉 —— 先调一次 finger/spi
    把 buvid3/buvid4 拿到放进 cookie 就好。

登录后的 cookie 存成 `cookies/bilibili.com.txt`，于是
`cookies.ytdlp_args()` 会在访问 B站时自动带上它 —— 扫码登录
和 yt-dlp 下载这两条线就这样打通了，不用额外接线。
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import cookies as _cookies, proxy as _proxy, qr as _qr

PASSPORT = "https://passport.bilibili.com"
API = "https://api.bilibili.com"
WWW = "https://www.bilibili.com"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 扫码轮询的状态码
QR_OK = 0
QR_EXPIRED = 86038
QR_WAIT_SCAN = 86101
QR_WAIT_CONFIRM = 86090

_HOME = pathlib.Path.home()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _build(cfg: Dict[str, Any], url: str = "", jar=None
           ) -> Tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar]:
    """带 cookie jar 和正确代理的 opener。"""
    jar = jar if jar is not None else http.cookiejar.CookieJar()
    handlers = [
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPRedirectHandler(),
    ]
    p = _proxy.select(cfg, url or WWW)
    if p:
        handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))   # 显式直连
    return urllib.request.build_opener(*handlers), jar


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "User-Agent": UA,
        "Referer": WWW + "/",
        "Origin": WWW,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if extra:
        h.update(extra)
    return h


def _load_jar() -> http.cookiejar.CookieJar:
    """把已存盘的 B站 cookie 读进 jar。"""
    jar = http.cookiejar.CookieJar()
    f = _cookies.cookie_dir() / "bilibili.com.txt"
    if f.is_file():
        try:
            jar = http.cookiejar.MozillaCookieJar(str(f))
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            jar = http.cookiejar.CookieJar()
    return jar


def _save_jar(jar: http.cookiejar.CookieJar) -> pathlib.Path:
    """把 jar 存成 Netscape 文件，交给 cookies 模块统一管理。

    存成 bilibili.com.txt 之后，yt-dlp 访问 B站时会自动带上它 ——
    这就是「扫码登录」和「下载」两条线打通的地方。
    """
    rows: List[Dict[str, Any]] = []
    for c in jar:
        if not c.name or c.value is None:
            continue
        rows.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain or ".bilibili.com",
            "path": c.path or "/",
            "secure": bool(c.secure),
            "httpOnly": c.has_nonstandard_attr("HttpOnly") or c.name.startswith("__"),
            "expirationDate": c.expires,
        })
    if not rows:
        raise ValueError("没有解析出任何 cookie")
    text = _cookies.to_netscape(rows)
    return _cookies.save_text(text, "bilibili.com")


def _get(url: str, cfg: Dict[str, Any], jar=None, timeout: int = 15,
         headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any], Any]:
    """GET 一个返回 JSON 的接口。返回 (http状态, 解析后的 JSON, jar)。"""
    op, jar = _build(cfg, url, jar)
    req = urllib.request.Request(url, headers=_headers(headers))
    try:
        with op.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw), jar
            except json.JSONDecodeError:
                return r.status, {"_raw": raw[:400]}, jar
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if e.code == 412:
            return 412, {"_error": "412 Precondition Failed",
                         "_hint": "B站风控：通常是缺 buvid 或请求过频"}, jar
        return e.code, {"_error": f"HTTP {e.code}", "_body": body}, jar
    except Exception as e:  # noqa: BLE001
        return 0, {"_error": f"{type(e).__name__}: {e}"}, jar


def ensure_buvid(cfg: Dict[str, Any], jar=None) -> Any:
    """拿到 buvid3 / buvid4 并放进 jar。

    不先拿这两个，B站会拿 412 挡掉后续所有 API 请求。
    """
    st, data, jar = _get(f"{API}/x/frontend/finger/spi", cfg, jar, timeout=10)
    d = (data or {}).get("data") or {}
    b3, b4 = d.get("b_3"), d.get("b_4")
    if b3:
        for name, val in (("buvid3", b3), ("buvid4", b4 or "")):
            if not val:
                continue
            c = http.cookiejar.Cookie(
                version=0, name=name, value=val, port=None, port_specified=False,
                domain=".bilibili.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=False,
                expires=int(time.time()) + 86400 * 180, discard=False,
                comment=None, comment_url=None, rest={}, rfc2109=False)
            jar.set_cookie(c)
    return jar


# --------------------------------------------------------------------------
# URL 解析
# --------------------------------------------------------------------------

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d{1,15})", re.I)
_EP_RE = re.compile(r"/bangumi/play/ep(\d+)")
_SS_RE = re.compile(r"/bangumi/play/ss(\d+)")
_SID_RE = re.compile(r"sid=(\d+)")
_FID_RE = re.compile(r"fid=(\d+)")
_MID_RE = re.compile(r"space\.bilibili\.com/(\d+)")
_PAGE_RE = re.compile(r"[?&]p=(\d+)")


def parse_url(url: str) -> Dict[str, Any]:
    """辨认 B站链接的形态。认不出来就返回 kind='unknown'。"""
    u = (url or "").strip()
    out: Dict[str, Any] = {"kind": "unknown", "url": u,
                           "page": int(_PAGE_RE.search(u).group(1)) if _PAGE_RE.search(u) else 1}
    m = _BV_RE.search(u)
    if m:
        out.update(kind="video", bvid=m.group(1))
        return out
    m = _AV_RE.search(u)
    if m:
        out.update(kind="video", aid=int(m.group(1)))
        return out
    m = _EP_RE.search(u)
    if m:
        out.update(kind="bangumi_ep", ep_id=int(m.group(1)))
        return out
    m = _SS_RE.search(u)
    if m:
        out.update(kind="bangumi_season", season_id=int(m.group(1)))
        return out
    if "/channel/collectiondetail" in u or "/channel/seriesdetail" in u:
        m = _SID_RE.search(u)
        mid = _MID_RE.search(u)
        if m:
            out.update(kind="up_season", season_id=int(m.group(1)),
                       mid=int(mid.group(1)) if mid else None)
            return out
    if "/favlist" in u:
        m = _FID_RE.search(u)
        mid = _MID_RE.search(u)
        if m:
            out.update(kind="favlist", fid=int(m.group(1)),
                       mid=int(mid.group(1)) if mid else None)
            return out
    if "space.bilibili.com" in u and "/video" in u:
        m = _MID_RE.search(u)
        if m:
            out.update(kind="up_videos", mid=int(m.group(1)))
            return out
    if "b23.tv" in u:
        out["kind"] = "shortlink"
    return out


# --------------------------------------------------------------------------
# 扫码登录
# --------------------------------------------------------------------------

def qr_start(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """申请一个登录二维码。

    返回里的 `qrcode` 是**已经渲染好的 PNG（base64）**，前端塞进 `<img>` 即可。

    历史坑：这里原先只返回 `url`，而前端读的是 `qrcode` / `qrcode_url` ——
    三个名字互相不认，结果是「服务端没返回二维码图片」，二维码框永远空白。
    现在服务端把编码这步做掉，前端只认一个字段名，不再有两端各写各的空间。
    """
    jar = http.cookiejar.CookieJar()
    jar = ensure_buvid(cfg, jar)
    st, data, jar = _get(f"{PASSPORT}/x/passport-login/web/qrcode/generate",
                         cfg, jar, timeout=15)
    d = (data or {}).get("data") or {}
    if not d.get("url") or not d.get("qrcode_key"):
        return {"ok": False,
                "error": (data or {}).get("message") or (data or {}).get("_error")
                         or "没能申请到二维码",
                "raw": data}

    text = d["url"]
    out: Dict[str, Any] = {
        "ok": True,
        "url": text,               # 原始内容，排查时有用
        "qrcode_key": d["qrcode_key"],
        "expires_in": 180,
    }
    if _qr.available():
        b64 = _qr.png_base64(text)
        if b64:
            out["qrcode"] = b64
        else:
            out["qr_error"] = "二维码渲染失败"
    else:
        # 如实说，而不是让前端画一个永远扫不出来的空框
        out["ok"] = False
        out["error"] = ("本机缺少二维码库，无法渲染登录码。"
                        "运行 `pip install --target vendor qrcode pypng` 后重试。"
                        f"（{_qr.unavailable_reason()}）")
    return out


def qr_poll(cfg: Dict[str, Any], key: str) -> Dict[str, Any]:
    """轮询扫码状态。成功时把 cookie 落盘。"""
    jar = _load_jar()
    jar = ensure_buvid(cfg, jar)
    st, data, jar = _get(
        f"{PASSPORT}/x/passport-login/web/qrcode/poll?qrcode_key="
        + urllib.parse.quote(key or ""), cfg, jar, timeout=15)
    d = (data or {}).get("data") or {}
    code = d.get("code")

    if code == QR_OK:
        try:
            saved = _save_jar(jar)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "state": "error",
                    "error": f"登录成功但 cookie 保存失败：{exc}"}
        me = whoami(cfg)
        return {"ok": True, "state": "ok", "message": "登录成功",
                "cookie_file": str(saved), "user": me}

    if code == QR_WAIT_CONFIRM:
        return {"ok": True, "state": "confirm", "message": "已扫码，请在手机上确认"}
    if code == QR_WAIT_SCAN:
        return {"ok": True, "state": "scan", "message": "等待扫码"}
    if code == QR_EXPIRED:
        return {"ok": True, "state": "expired", "message": "二维码已过期，请重新获取"}

    return {"ok": True, "state": "unknown",
            "message": d.get("message") or (data or {}).get("_error") or "未知状态",
            "raw": d}


def whoami(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """当前 cookie 对应的账号信息。没登录返回 None。"""
    jar = _load_jar()
    if not any(c.name == "SESSDATA" for c in jar):
        return None
    jar = ensure_buvid(cfg, jar)
    st, data, jar = _get(f"{API}/x/web-interface/nav", cfg, jar, timeout=12)
    d = (data or {}).get("data") or {}
    if not d.get("isLogin"):
        return None
    vip = d.get("vipStatus") == 1
    return {
        "mid": d.get("mid"),
        "name": d.get("uname"),
        "face": d.get("face"),
        "level": (d.get("level_info") or {}).get("current_level"),
        "coins": d.get("money"),
        "vip": vip,
        "vip_label": (d.get("vip_label") or {}).get("text") if vip else "",
        "max_quality_hint": "8K / 4K 需要大会员" if not vip else "大会员：可下 4K / 8K",
    }


def logout() -> bool:
    f = _cookies.cookie_dir() / "bilibili.com.txt"
    try:
        f.unlink()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 视频 / 分P / 合集
# --------------------------------------------------------------------------

def video_info(cfg: Dict[str, Any], *, bvid: str = "", aid: int = 0
               ) -> Dict[str, Any]:
    """拿视频详情：分P 列表、合集(ugc_season)、UP 主等。"""
    jar = _load_jar()
    jar = ensure_buvid(cfg, jar)
    q = f"bvid={bvid}" if bvid else f"aid={aid}"
    st, data, jar = _get(f"{API}/x/web-interface/view?{q}", cfg, jar, timeout=15)
    if (data or {}).get("code") != 0:
        return {"ok": False,
                "error": (data or {}).get("message") or (data or {}).get("_error")
                         or "拿不到视频信息",
                "code": (data or {}).get("code")}
    return {"ok": True, "data": data.get("data") or {}}


def _decode_danmaku(raw: bytes, enc: str = "") -> str:
    """弹幕接口的返回值要先解压。

    坑：`x/v1/dm/list.so` 回的是 **raw deflate**（没有 zlib 头，
    也不是 gzip）。直接按文本解码会得到一坨乱码 —— 第一次跑就踩了这个，
    Content-Encoding 头还是空的，完全看不出要解压。
    """
    if b"<i>" in raw[:3000] or raw[:5] == b"<?xml":
        return raw.decode("utf-8", "replace")     # 已经是明文
    import zlib
    # 依次试：raw deflate / 带 zlib 头 / gzip
    for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS, 16 + zlib.MAX_WBITS):
        try:
            out = zlib.decompress(raw, wbits)
            if b"<" in out[:200]:
                return out.decode("utf-8", "replace")
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def danmaku_xml(cfg: Dict[str, Any], cid: int, timeout: int = 20) -> str:
    """下载弹幕（B站标准 XML）。"""
    op, jar = _build(cfg, WWW, _load_jar())
    req = urllib.request.Request(
        f"{API}/x/v1/dm/list.so?oid={int(cid)}",
        headers=_headers({"Accept": "application/xml, text/xml, */*"}))
    with op.open(req, timeout=timeout) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    return _decode_danmaku(raw, enc)


def subtitle_list(cfg: Dict[str, Any], bvid: str, cid: int) -> List[Dict[str, Any]]:
    """该视频有哪些字幕。"""
    jar = _load_jar()
    jar = ensure_buvid(cfg, jar)
    st, data, jar = _get(
        f"{API}/x/player/v2?bvid={urllib.parse.quote(bvid)}&cid={int(cid)}",
        cfg, jar, timeout=15)
    subs = ((data or {}).get("data") or {}).get("subtitle") or {}
    out = []
    for s in subs.get("subtitles") or []:
        out.append({
            "lan": s.get("lan"),
            "lan_doc": s.get("lan_doc"),
            "url": s.get("subtitle_url") or "",
            "ai": bool(s.get("ai_type")),
        })
    return out


def subtitle_fetch(cfg: Dict[str, Any], url: str, timeout: int = 20) -> str:
    """把字幕 JSON 转成 SRT 文本。"""
    if url.startswith("//"):
        url = "https:" + url
    op, jar = _build(cfg, url, _load_jar())
    req = urllib.request.Request(url, headers=_headers())
    with op.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    lines: List[str] = []
    for i, b in enumerate(data.get("body") or [], 1):
        lines.append(str(i))

        def ts(sec: float) -> str:
            ms = int(round(float(sec) * 1000))
            h, ms = divmod(ms, 3600000)
            m, ms = divmod(ms, 60000)
            s, ms = divmod(ms, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        lines.append(f"{ts(b.get('from', 0))} --> {ts(b.get('to', 0))}")
        lines.append(str(b.get("content") or "").strip())
        lines.append("")
    return "\n".join(lines)


def resolve(cfg: Dict[str, Any], url: str, limit: int = 500) -> Dict[str, Any]:
    """把任意 B站链接解析成「单视频 / 分P / 合集」。

    比 yt-dlp 的 flat 模式强的地方：**有标题**。B站的接口一次给全，
    yt-dlp 的 B站 flat 模式只回 url 列表（连条目标题都是空的）。

    返回 kind: video | pages | season
    """
    info = parse_url(url)
    kind = info.get("kind")

    # 短链先展开
    if kind == "shortlink":
        try:
            op, _ = _build(cfg, url)
            with op.open(urllib.request.Request(url, headers=_headers()),
                         timeout=12) as r:
                url = r.geturl()
            info = parse_url(url)
            kind = info.get("kind")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"短链展开失败：{exc}"}

    if kind != "video":
        # 番剧 / UP合集 / 收藏夹 交给 yt-dlp，那边 extractor 更全
        return {"ok": False, "delegate": True,
                "reason": f"链接形态 {kind} 交给 yt-dlp 处理",
                "parsed": info}

    vi = video_info(cfg, bvid=info.get("bvid") or "",
                    aid=info.get("aid") or 0)
    if not vi.get("ok"):
        return {"ok": False, "delegate": True, "reason": vi.get("error"),
                "parsed": info}
    d = vi["data"]

    owner = d.get("owner") or {}
    base = {
        "title": d.get("title") or "",
        "cover": d.get("pic") or "",
        "desc": (d.get("desc") or "")[:400],
        "uploader": owner.get("name") or "",
        "mid": owner.get("mid"),
        "bvid": d.get("bvid") or info.get("bvid") or "",
        "aid": d.get("aid"),
        "duration": d.get("duration"),
        "pubdate": d.get("pubdate"),
        "url": url,
    }

    pages = d.get("pages") or []
    season = d.get("ugc_season")

    # 合集优先：一个有合集的视频，用户要的多半是整个合集
    if season:
        entries: List[Dict[str, Any]] = []
        idx = 0
        for sec in season.get("sections") or []:
            for ep in sec.get("episodes") or []:
                idx += 1
                arc = ep.get("arc") or {}
                entries.append({
                    "index": idx,
                    "bvid": ep.get("bvid") or arc.get("bvid") or "",
                    "aid": ep.get("aid") or arc.get("aid"),
                    "cid": ep.get("cid"),
                    "title": ep.get("title") or arc.get("title") or f"第 {idx} 集",
                    "duration": arc.get("duration") or ep.get("duration"),
                    "cover": arc.get("pic") or "",
                    "url": f"https://www.bilibili.com/video/"
                           f"{ep.get('bvid') or arc.get('bvid') or ''}",
                })
                if len(entries) >= limit:
                    break
        return {
            "ok": True, "kind": "season",
            "season_id": season.get("id"),
            "season_title": season.get("title") or base["title"],
            "count": len(entries),
            "entries": entries,
            "source": "bilibili-api",
            **base,
        }

    if len(pages) > 1:
        entries = []
        for p in pages[:limit]:
            entries.append({
                "index": int(p.get("page") or len(entries) + 1),
                "cid": p.get("cid"),
                "title": p.get("part") or f"P{p.get('page')}",
                "duration": p.get("duration"),
                "cover": base["cover"],
                "url": f"{url.split('?')[0]}?p={p.get('page')}",
            })
        return {"ok": True, "kind": "pages", "count": len(entries),
                "season_title": base["title"], "entries": entries,
                "source": "bilibili-api", **base}

    # 单P
    first = pages[0] if pages else {}
    return {
        "ok": True, "kind": "video", "count": 1,
        "entries": [{
            "index": 1,
            "cid": first.get("cid"),
            "title": first.get("part") or base["title"],
            "duration": first.get("duration") or base["duration"],
            "cover": base["cover"],
            "url": url,
        }],
        "season_title": base["title"],
        "source": "bilibili-api",
        **base,
    }


def describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """给界面看的 B站状态。"""
    me = None
    err = ""
    try:
        me = whoami(cfg)
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    f = _cookies.cookie_dir() / "bilibili.com.txt"
    return {
        "logged_in": bool(me),
        "user": me,
        "cookie_file": str(f) if f.is_file() else "",
        "error": err,
        "note": ("已登录，下载会自动带上登录态（大会员可下 4K）"
                 if me else "未登录。1080P 以上、充电视频等需要登录后才能下载。"),
    }


# --------------------------------------------------------------------------
# 取流（playurl + WBI 签名）
# --------------------------------------------------------------------------
# 为什么不用 yt-dlp 取流：
#   1. 每次都要起一个进程，冷启动 1–3 秒；
#   2. 拿不到"这台机器现在能下哪些画质"的完整列表（要 -F 再解析文本）；
#   3. 8K / HDR / 杜比 / Hi-Res 这些要靠 fnval 位掩码一次性要全，
#      yt-dlp 的默认请求不一定带。
# 直接打官方接口就没有这三个问题。代价是 WBI 签名算法会变 ——
# 所以 video/manager 侧保留了 yt-dlp 兜底，这里失败就照旧走老路。

# qn → 人话
QUALITY_LABELS: Dict[int, str] = {
    127: "8K",
    126: "杜比视界",
    125: "HDR",
    120: "4K",
    116: "1080P60",
    112: "1080P+",
    80: "1080P",
    74: "720P60",
    64: "720P",
    32: "480P",
    16: "360P",
    6: "240P",
}

# fnval 位掩码：DASH + HDR + 4K + 杜比音频 + 杜比视界 + 8K + AV1
FNVAL_DASH = 16
FNVAL_ALL = 16 | 64 | 128 | 256 | 512 | 1024 | 2048       # 4048

# WBI 的 mixin key 重排表（官方前端里硬编码的 64 位表）
_WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_WBI_CACHE: Dict[str, Any] = {"t": 0.0, "img": "", "sub": ""}
_WBI_TTL = 6 * 3600


def mixin_key(img_key: str, sub_key: str) -> str:
    """把 img_key + sub_key 按重排表搅成 32 位 mixin key。"""
    raw = (img_key or "") + (sub_key or "")
    if len(raw) < 64:
        return ""
    return "".join(raw[i] for i in _WBI_TAB)[:32]


def wbi_keys(cfg: Dict[str, Any], jar=None, force: bool = False) -> Tuple[str, str]:
    """从 nav 接口取 wbi_img 的 img/sub key，缓存 6 小时。"""
    now = time.time()
    if not force and _WBI_CACHE["img"] and now - _WBI_CACHE["t"] < _WBI_TTL:
        return str(_WBI_CACHE["img"]), str(_WBI_CACHE["sub"])

    jar = jar if jar is not None else _load_jar()
    st, data, jar = _get(f"{API}/x/web-interface/nav", cfg, jar, timeout=12)
    d = (data or {}).get("data") or {}
    img = ((d.get("wbi_img") or {}).get("img_url") or "")
    sub = ((d.get("wbi_img") or {}).get("sub_url") or "")
    img_key = img.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub.rsplit("/", 1)[-1].split(".")[0]
    if not img_key or not sub_key:
        raise BiliError(f"拿不到 WBI key（HTTP {st}）：{(data or {}).get('message') or '接口没返回 wbi_img'}")
    _WBI_CACHE.update({"t": now, "img": img_key, "sub": sub_key})
    return img_key, sub_key


def wbi_sign(params: Dict[str, Any], img_key: str, sub_key: str,
             wts: Optional[int] = None) -> Dict[str, Any]:
    """给参数加 wts / w_rid。返回新字典，不改原字典。"""
    mixin = mixin_key(img_key, sub_key)
    if not mixin:
        raise BiliError("WBI key 长度不对，无法签名")
    p = {k: v for k, v in params.items() if v is not None and v != ""}
    p["wts"] = int(wts if wts is not None else time.time())
    # 官方要求：value 里的 !'()* 先去掉，再按 key 排序拼 query
    clean = {k: str(v).replace("!", "").replace("'", "").replace("(", "")
             .replace(")", "").replace("*", "") for k, v in p.items()}
    query = urllib.parse.urlencode(sorted(clean.items()))
    p["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return p


class BiliError(Exception):
    """B站接口层面的失败（网络/风控/签名）。上层据此决定是否回退。"""


def playurl(cfg: Dict[str, Any], *, bvid: str = "", cid: int = 0,
            aid: int = 0, qn: int = 0, fnval: int = FNVAL_ALL,
            jar=None) -> Dict[str, Any]:
    """取播放地址（DASH）。返回画质表与音视频流。

    返回结构::

        {
          "ok": True,
          "quality": 120,                 # 实际给到的画质
          "accept_quality": [120, 80, ...],
          "accept_description": ["4K 超清", ...],
          "video": [{qn, codecid, codecs, width, height, fps, bandwidth,
                     url, backup}],
          "audio": [{id, bandwidth, url, backup, mime}],
          "dolby": {...} | None, "flac": {...} | None,
          "duration": 123, "timelength": 123000,
        }
    """
    if not bvid and not aid:
        raise BiliError("playurl 需要 bvid 或 aid")
    if not cid:
        raise BiliError("playurl 需要 cid")

    jar = jar if jar is not None else ensure_buvid(cfg, _load_jar())
    img_key, sub_key = wbi_keys(cfg, jar)
    params = wbi_sign({
        "bvid": bvid, "cid": cid, "aid": aid or None,
        "qn": qn, "fnval": fnval, "fourk": 1, "fnver": 0,
    }, img_key, sub_key)
    url = f"{API}/x/player/wbi/playurl?" + urllib.parse.urlencode(sorted(params.items()))
    st, data, _ = _get(url, cfg, jar, timeout=15)

    code = (data or {}).get("code")
    if st != 200 or code not in (0, None):
        msg = (data or {}).get("message") or (data or {}).get("_error") or f"HTTP {st}"
        hint = ""
        if code in (-404, -403, 87008):
            hint = "（可能需要登录 / 大会员，或视频已失效）"
        raise BiliError(f"playurl 失败：{msg}{hint}")

    d = (data or {}).get("data") or {}
    dash = d.get("dash") or {}
    videos: List[Dict[str, Any]] = []
    for v in dash.get("video") or []:
        videos.append({
            "qn": int(v.get("id") or 0),
            "codecid": int(v.get("codecid") or 0),
            "codecs": v.get("codecs") or "",
            "width": v.get("width"), "height": v.get("height"),
            "fps": v.get("frameRate") or v.get("frame_rate"),
            "bandwidth": int(v.get("bandwidth") or 0),
            "mime": v.get("mimeType") or "video/mp4",
            "url": v.get("baseUrl") or v.get("base_url") or "",
            "backup": list(v.get("backupUrl") or v.get("backup_url") or []),
        })
    audios: List[Dict[str, Any]] = []
    for a in dash.get("audio") or []:
        audios.append({
            "id": int(a.get("id") or 0),
            "bandwidth": int(a.get("bandwidth") or 0),
            "mime": a.get("mimeType") or "audio/mp4",
            "codecs": a.get("codecs") or "",
            "url": a.get("baseUrl") or a.get("base_url") or "",
            "backup": list(a.get("backupUrl") or a.get("backup_url") or []),
        })

    def _extra(key: str) -> Optional[Dict[str, Any]]:
        x = dash.get(key)
        if not x:
            return None
        return {
            "url": x.get("baseUrl") or x.get("base_url") or "",
            "backup": list(x.get("backupUrl") or x.get("backup_url") or []),
            "bandwidth": int(x.get("bandwidth") or 0),
        }

    return {
        "ok": True,
        "quality": int(d.get("quality") or 0),
        "accept_quality": [int(x) for x in (d.get("accept_quality") or [])],
        "accept_description": list(d.get("accept_description") or []),
        "video": videos,
        "audio": audios,
        "dolby": _extra("dolby"),
        "flac": _extra("flac"),
        "duration": int(d.get("timelength") or 0) // 1000,
        "timelength": int(d.get("timelength") or 0),
    }


def quality_label(qn: int) -> str:
    return QUALITY_LABELS.get(int(qn or 0), f"qn{int(qn or 0)}")


def best_video(streams: List[Dict[str, Any]], qn: int = 0,
               prefer_codec: str = "avc") -> Optional[Dict[str, Any]]:
    """挑一路视频：先按画质，再按编码偏好（默认 AVC，兼容性最好）。

    同画质下 HEVC/AV1 体积更小，但老播放器/剪辑软件可能不认 ——
    默认给最稳的，用户想要极限画质可以改 prefer_codec。
    """
    if not streams:
        return None
    pool = [s for s in streams if s.get("url")]
    if not pool:
        return None
    if qn:
        # 精确命中；没有就取不高于 qn 的最高档
        exact = [s for s in pool if s["qn"] == qn]
        if exact:
            pool = exact
        else:
            lower = [s for s in pool if s["qn"] <= qn]
            if lower:
                pool = lower
    top = max(s["qn"] for s in pool)
    pool = [s for s in pool if s["qn"] == top]
    want = {"avc": 7, "hevc": 12, "av1": 13}.get(prefer_codec, 7)
    same = [s for s in pool if s["codecid"] == want]
    if same:
        return max(same, key=lambda s: s["bandwidth"])
    return max(pool, key=lambda s: s["bandwidth"])


def best_audio(pl: Dict[str, Any], prefer: str = "m4a") -> Optional[Dict[str, Any]]:
    """挑音轨：杜比 > Hi-Res(FLAC) > 普通 m4a 里码率最高的。"""
    for key in ("dolby", "flac"):
        x = pl.get(key)
        if x and x.get("url"):
            return {"url": x["url"], "backup": x.get("backup") or [],
                    "bandwidth": x.get("bandwidth") or 0, "kind": key}
    pool = [a for a in (pl.get("audio") or []) if a.get("url")]
    if not pool:
        return None
    pool.sort(key=lambda a: a.get("bandwidth") or 0, reverse=True)
    a = pool[0]
    return {"url": a["url"], "backup": a.get("backup") or [],
            "bandwidth": a.get("bandwidth") or 0, "kind": "m4a"}
