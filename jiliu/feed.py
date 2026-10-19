"""RSS / Atom 订阅源解析 —— 播客、资讯流、番剧更新的批量入口。

定位（和 netdisk.py 同一层的设计理由）：
    feeds 是「一个 URL 换一串任务」的聚合入口，不属于 protocols 的
    纯本地链接解码，也不属于单个站点的自研解析 —— 它是独立的第三类。

零依赖：xml.etree 解析（RSS 2.0 / Atom 双格式），不引 feedparser。
日期原样透传（各源的 RFC822/ISO8601 混着来，解析了反而不一致）。

条目两类（决定建什么任务）：
    - 带 enclosure（播客/播客视频/直接挂媒体）→ 直链 http 任务
    - 只有页面链接（资讯流/视频页）→ video 任务，交给 yt-dlp 解析页面
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from . import proxy as _proxy

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_MAX_ITEMS = 200          # 单次最多带出的条目数（防巨型 feed）
_TIMEOUT = 25


class FeedError(Exception):
    """feed 拉取/解析失败，error 给用户看，hint 给建议。"""

    def __init__(self, error: str, hint: str = ""):
        super().__init__(error)
        self.error = error
        self.hint = hint

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": False, "error": self.error, "hint": self.hint}


# ---------------------------------------------------------------------------
# URL 启发式（保守：只认明显的 feed 特征，避免把普通页面拉进来探测）
# ---------------------------------------------------------------------------

def looks_feed_url(url: str) -> bool:
    try:
        path = (urllib.parse.urlparse(url).path or "").lower()
    except ValueError:
        return False
    return (path.endswith((".xml", ".rss", ".atom"))
            or re.search(r"/(feed|rss|atom)(/|$|\?)", path) is not None)


def looks_like_feed(raw: bytes) -> bool:
    """按内容判定：root 标签是 rss/feed/RDF 就算（不信任 Content-Type）。"""
    head = raw[:600].lstrip()
    # XML 声明 + 注释都能出现在 root 前，直接搜
    return re.search(rb"<(rss|feed|rdf:RDF)[\s>]", head) is not None


# ---------------------------------------------------------------------------
# 拉取
# ---------------------------------------------------------------------------

def fetch(url: str, cfg: Dict[str, Any],
          timeout: int = _TIMEOUT) -> bytes:
    op = _proxy.opener(cfg, url, timeout=timeout)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip",
    })
    try:
        with op.open(req, timeout=timeout) as r:
            raw = r.read(20 * 1024 * 1024)     # 20MB 上限，feed 再大不伺候
            if raw[:2] == b"\x1f\x8b":
                import gzip
                import io
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return raw
    except Exception as exc:  # noqa: BLE001
        raise FeedError(f"拉取订阅源失败（{type(exc).__name__}）",
                        hint="检查链接是否能打开，或稍后再试") from exc


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _text(el: Optional[ET.Element]) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _find_child(el: ET.Element, name: str) -> Optional[ET.Element]:
    for ch in el:
        if _strip_ns(ch.tag) == name:
            return ch
    return None


def _iter(el: ET.Element, name: str):
    for ch in el.iter():
        if ch is not el and _strip_ns(ch.tag) == name:
            yield ch


def _fmt_size(n: int) -> str:
    if n >= 1073741824:
        return f"{n / 1073741824:.2f} GB"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n:
        return f"{n / 1024:.0f} KB"
    return ""


_MEDIA_HINT = re.compile(r"audio|video|application/octet-stream", re.I)


def _pick_enclosure(item: ET.Element) -> Tuple[str, int]:
    """挑 enclosure：优先音视频类型，其次第一个。返回 (url, 字节数)。"""
    first: Tuple[str, int] = ("", 0)
    for enc in _iter(item, "enclosure"):
        u = (enc.get("url") or "").strip()
        if not u:
            continue
        try:
            length = int(enc.get("length") or enc.get("fileSize") or 0)
        except ValueError:
            length = 0
        if _MEDIA_HINT.search(enc.get("type") or ""):
            return u, length
        if not first[0]:
            first = (u, length)
    return first


def _parse_rss(root: ET.Element) -> Dict[str, Any]:
    channel = _find_child(root, "channel")
    if channel is None:
        raise FeedError("RSS 缺少 channel 节点", hint="这可能不是标准的 RSS 源")
    items: List[Dict[str, Any]] = []
    for it in _iter(channel, "item"):
        if len(items) >= _MAX_ITEMS:
            break
        title = _text(_find_child(it, "title"))
        link = _text(_find_child(it, "link"))
        # RSS 里 link 可能写在 rdf:about 属性（RSS 1.0）
        if not link:
            link = it.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about") or ""
        enc_url, enc_size = _pick_enclosure(it)
        if not title and not enc_url and not link:
            continue
        size = enc_size
        items.append({
            "title": title or "(无标题)",
            "url": enc_url,               # 媒体直链（无则空）
            "link": link,                 # 条目页面
            "size": size,
            "size_text": _fmt_size(size),
            "date": _text(_find_child(it, "pubdate")),
            "direct": bool(enc_url),
        })
    return {"title": _text(_find_child(channel, "title")) or "RSS 订阅",
            "items": items}


def _parse_atom(root: ET.Element) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for entry in _iter(root, "entry"):
        if len(items) >= _MAX_ITEMS:
            break
        title = _text(_find_child(entry, "title"))
        page = ""
        media = ""
        media_size = 0
        for ln in _iter(entry, "link"):
            rel = (ln.get("rel") or "alternate").lower()
            href = (ln.get("href") or "").strip()
            if not href:
                continue
            if rel == "enclosure":
                if not media:
                    media = href
                    try:
                        media_size = int(ln.get("length") or 0)
                    except ValueError:
                        media_size = 0
            elif not page:
                page = href
        if not title and not media and not page:
            continue
        items.append({
            "title": title or "(无标题)",
            "url": media,
            "link": page or media,
            "size": media_size,
            "size_text": _fmt_size(media_size),
            "date": (_text(_find_child(entry, "updated"))
                     or _text(_find_child(entry, "published"))),
            "direct": bool(media),
        })
    return {"title": _text(_find_child(root, "title")) or "Atom 订阅",
            "items": items}


def parse(raw: bytes) -> Dict[str, Any]:
    """解析 feed 字节流 → {title, items, total}。"""
    # 容错：剥掉 BOM / 逃逸字符；解析不了再看 doctype
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # 有些 feed 头部带垃圾字符，试着找第一个 '<' 重新解析
        i = raw.find(b"<")
        if i > 0:
            try:
                root = ET.fromstring(raw[i:])
            except ET.ParseError as exc:
                raise FeedError("订阅源不是合法的 XML",
                                hint="确认这是 RSS/Atom 链接") from exc
        else:
            raise FeedError("订阅源不是合法的 XML",
                            hint="确认这是 RSS/Atom 链接") from None
    tag = _strip_ns(root.tag)
    if tag == "rss":
        out = _parse_rss(root)
    elif tag == "feed":
        out = _parse_atom(root)
    elif tag == "rdf":
        # RSS 1.0 (RDF)：item 挂在根下
        out = _parse_rss_by_items(root, _text_for_rdf_title(root))
    else:
        raise FeedError("这不是 RSS/Atom 订阅源",
                        hint="汲流认 rss/feed/RDF 三种根节点")
    out["kind"] = "feed"
    out["total"] = len(out["items"])
    return out


def _text_for_rdf_title(root: ET.Element) -> str:
    ch = _find_child(root, "channel")
    return _text(_find_child(ch, "title")) if ch is not None else ""


def _parse_rss_by_items(root: ET.Element, title: str) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for it in _iter(root, "item"):
        if len(items) >= _MAX_ITEMS:
            break
        t = _text(_find_child(it, "title"))
        link = _text(_find_child(it, "link"))
        enc_url, enc_size = _pick_enclosure(it)
        if not t and not link and not enc_url:
            continue
        items.append({"title": t or "(无标题)", "url": enc_url, "link": link,
                      "size": enc_size, "size_text": _fmt_size(enc_size),
                      "date": _text(_find_child(it, "date")),
                      "direct": bool(enc_url)})
    return {"title": title or "RSS 订阅", "items": items}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def inspect(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """拉取 + 解析一个订阅源。失败抛 FeedError。"""
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise FeedError("不是 http(s) 链接")
    raw = fetch(url, cfg)
    if not looks_like_feed(raw):
        raise FeedError("这个链接不是订阅源",
                        hint="汲流按内容判定（root 为 rss/feed/RDF）")
    return parse(raw)


def try_inspect(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """宽容入口：失败返回 {ok:False, ...}，不抛异常。"""
    try:
        out = inspect(url, cfg)
        out["ok"] = True
        return out
    except FeedError as e:
        d = e.to_dict()
        d["not_feed"] = "不是" in e.error     # 供调用方区分「不是feed」和「拉取失败」
        return d
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "hint": ""}
