"""B 站弹幕：下载、解析、转 ASS。

    from jiliu import danmaku
    r = danmaku.for_video(cfg, "https://www.bilibili.com/video/BV1xx411c7mD")
    r["files"]      # {"xml": "...", "ass": "..."}

为什么不直接用现成脚本？因为「下了 XML 就完事」这一步对播放器毫无意义 ——
MPV/PotPlayer 认的是 ASS，而 B 站 XML 只给「什么时候、什么颜色、什么内容」，
不给「第几行、多快滚过去」。滚动的轨道分配、字号换算、颜色字节序
（ASS 是 BGR）全是转 ASS 才出现的问题，这个模块把它们一次解决。

支持的弹幕类型（B 站 mode）：
    1/2/3  滚动（右→左）
    4      底部固定
    5      顶部固定
    6      逆向滚动（左→右）
    7/8    高级/代码弹幕 —— 默认丢弃，转出来只会是一堆乱码
"""
from __future__ import annotations

import html
import pathlib
import re
from typing import Any, Dict, Iterable, List

# 一屏最多塞多少行滚动弹幕（按 PlayResY 算，见 _lanes）
LANE_GAP = 1.35          # 行高 = 字号 × 这个系数，太挤会叠字
SIDE_MARGIN = 0.02       # 左右留白占画面宽度的比例
TOP_MARGIN = 0.03

DEFAULT_FONT = "Microsoft YaHei"
DEFAULT_SIZE = 36
DEFAULT_ALPHA = 0.78
SCROLL_SECONDS = 8.0
STATIC_SECONDS = 4.0

_DM_RE = re.compile(r"<d\s+([^>]*)>(.*?)</d>", re.S)
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


# ==========================================================================
# 解析
# ==========================================================================

def parse_xml(text: str) -> List[Dict[str, Any]]:
    """B 站弹幕 XML → 结构化列表。

    返回项：{t, mode, size, color, text, pool, uid}
    t 是视频内的秒数（浮点），color 是 0xRRGGBB 整数。
    """
    out: List[Dict[str, Any]] = []
    if not text:
        return out
    for m in _DM_RE.finditer(text):
        attrs = {k: v for k, v in _ATTR_RE.findall(m.group(1) or "")}
        body = m.group(2) or ""
        # 弹幕正文里可能有 &amp; 之类的实体，也可能是 HTML 片段
        body = html.unescape(re.sub(r"<[^>]+>", "", body)).strip()
        if not body:
            continue
        try:
            t = float(attrs.get("p", "0").split(",")[0])
        except (TypeError, ValueError):
            t = 0.0
        p = (attrs.get("p") or "").split(",")
        out.append({
            "t": t,
            "mode": _int(p[1] if len(p) > 1 else 1, 1),
            "size": _int(p[2] if len(p) > 2 else 25, 25),
            "color": _int(p[3] if len(p) > 3 else 16777215, 16777215),
            "text": body,
            "pool": _int(p[5] if len(p) > 5 else 0, 0),
            "uid": (p[6] if len(p) > 6 else ""),
        })
    out.sort(key=lambda x: x["t"])
    return out


def _int(v: Any, default: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def to_xml(items: Iterable[Dict[str, Any]], chatid: int = 0) -> str:
    """结构化列表 → B 站格式 XML。

    下载下来的原文要留一份，播放器/剪辑软件认的是这个；
    ASS 只是派生物，删了还能从 XML 重新生成。
    """
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             "<i>",
             f"  <chatid>{int(chatid or 0)}</chatid>",
             "  <chatserver>chat.bilibili.com</chatserver>",
             "  <mission>0</mission>",
             "  <maxlimit>8000</maxlimit>",
             "  <state>0</state>",
             "  <real_name>0</real_name>",
             "  <source>k-v</source>"]
    for d in items:
        p = "{:.3f},{},{},{},{},0,{},0".format(
            float(d.get("t") or 0), int(d.get("mode") or 1),
            int(d.get("size") or 25), int(d.get("color") or 16777215),
            int(d.get("pool") or 0), d.get("uid") or "")
        body = html.escape(str(d.get("text") or ""), quote=False)
        lines.append(f'  <d p="{p}">{body}</d>')
    lines.append("</i>")
    return "\n".join(lines) + "\n"


# ==========================================================================
# 转 ASS
# ==========================================================================

def _fmt_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_color(rgb: int) -> str:
    """0xRRGGBB → ASS 的 &HBBGGRR&。字节序错了整屏配色都会反。"""
    rgb = int(rgb or 0) & 0xFFFFFF
    r = (rgb >> 16) & 0xFF
    g = (rgb >> 8) & 0xFF
    b = rgb & 0xFF
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _clean(text: str) -> str:
    """清掉会打断 ASS 语法的字符。

    ASS 用 {} 包裹样式标签，正文里出现花括号就会被当指令吞掉，
    严重的会让整行不显示。换成全角括号最安全。
    """
    t = str(text or "").replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    t = t.replace("\r", " ").replace("\n", " ")
    return t.strip()


def _lanes(height: int, size: int) -> int:
    line_h = max(1.0, size * LANE_GAP)
    usable = height * (1 - 2 * TOP_MARGIN)
    return max(1, int(usable // line_h))


def _pick_lane(free: List[float], start: float) -> int:
    """找一条当前空闲的轨道；全忙就挑最早空出来的（接受轻微重叠）。"""
    for i, f in enumerate(free):
        if f <= start:
            return i
    return min(range(len(free)), key=lambda i: free[i])


def to_ass(items: Iterable[Dict[str, Any]], *, width: int = 1920,
           height: int = 1080, font: str = DEFAULT_FONT,
           alpha: float = DEFAULT_ALPHA, scroll: float = SCROLL_SECONDS,
           static: float = STATIC_SECONDS,
           skip_special: bool = True,
           max_items: int = 0) -> str:
    """弹幕列表 → ASS 字幕。

    轨道分配是这里唯一的「算法」：同一时刻的弹幕不能全挤在第一行，
    否则 10 秒内涌进 200 条时画面上只有一坨白字。给每条记一个
    「这条轨道什么时候空出来」，来新的就挑最早空的那条。
    """
    width = max(320, int(width or 1920))
    height = max(240, int(height or 1080))
    font = font or DEFAULT_FONT
    alpha_hex = f"{int(max(0.0, min(1.0, alpha)) * 255):02X}"
    base = int(DEFAULT_SIZE * height / 1080)

    scrolls = [d for d in items if int(d.get("mode") or 1) in (1, 2, 3, 6)]
    tops = [d for d in items if int(d.get("mode") or 1) == 5]
    bottoms = [d for d in items if int(d.get("mode") or 1) == 4]
    if skip_special:
        # mode 7/8 是脚本弹幕，转 ASS 只会显示成乱码
        pass
    else:
        tops += [d for d in items if int(d.get("mode") or 1) in (7, 8)]

    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "Collisions: Normal",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},{base},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H00000000,0,0,0,0,100,100,0,0,1,1.2,0.8,7,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    ]

    events: List[str] = []
    n_lane = _lanes(height, base)
    line_h = max(1.0, base * LANE_GAP)
    margin = height * TOP_MARGIN

    # —— 滚动弹幕 ——
    free = [0.0] * n_lane
    for d in sorted(scrolls, key=lambda x: x["t"]):
        text = _clean(d.get("text"))
        if not text:
            continue
        start = max(0.0, float(d.get("t") or 0))
        lane = _pick_lane(free, start)
        free[lane] = start + scroll * 0.9
        y = int(margin + lane * line_h + base * 0.5)
        size = max(12, int(base * (int(d.get("size") or 25) / 25.0)))
        # 文字宽度粗估：中文按 1 字宽、西文按 0.55 —— 只要够让整行滑完
        tw = _text_width(text, size)
        tags = (f"{{\\an7\\move({width},{y},{-tw - 8},{y})"
                f"\\fs{size}\\c{_ass_color(d.get('color') or 16777215)}"
                f"\\alpha&H{alpha_hex}&}}")
        reverse = int(d.get("mode") or 1) == 6
        if reverse:
            tags = (f"{{\\an7\\move({-tw - 8},{y},{width},{y})"
                    f"\\fs{size}\\c{_ass_color(d.get('color') or 16777215)}"
                    f"\\alpha&H{alpha_hex}&}}")
        events.append(_event(start, start + scroll, tags + text))
        if max_items and len(events) >= max_items:
            break

    # —— 顶部：从上往下排，屏幕上方固定 ——
    free = [0.0] * max(1, n_lane // 2)
    for d in sorted(tops, key=lambda x: x["t"]):
        if max_items and len(events) >= max_items:
            break
        text = _clean(d.get("text"))
        if not text:
            continue
        start = max(0.0, float(d.get("t") or 0))
        lane = _pick_lane(free, start)
        free[lane] = start + static
        y = int(margin + lane * line_h + base * 0.5)
        size = max(12, int(base * (int(d.get("size") or 25) / 25.0)))
        tags = (f"{{\\an8\\pos({width // 2},{y})\\fs{size}"
                f"\\c{_ass_color(d.get('color') or 16777215)}"
                f"\\alpha&H{alpha_hex}&}}")
        events.append(_event(start, start + static, tags + text))

    # —— 底部：从下往上排 ——
    free = [0.0] * max(1, n_lane // 2)
    for d in sorted(bottoms, key=lambda x: x["t"]):
        if max_items and len(events) >= max_items:
            break
        text = _clean(d.get("text"))
        if not text:
            continue
        start = max(0.0, float(d.get("t") or 0))
        lane = _pick_lane(free, start)
        free[lane] = start + static
        y = int(height - margin - lane * line_h - base * 0.5)
        size = max(12, int(base * (int(d.get("size") or 25) / 25.0)))
        tags = (f"{{\\an2\\pos({width // 2},{y})\\fs{size}"
                f"\\c{_ass_color(d.get('color') or 16777215)}"
                f"\\alpha&H{alpha_hex}&}}")
        events.append(_event(start, start + static, tags + text))

    events.sort(key=lambda e: e[0])
    return "\n".join(head + [e[1] for e in events]) + "\n"


def _event(start: float, end: float, body: str):
    return (start, f"Dialogue: 0,{_fmt_time(start)},{_fmt_time(max(end, start + 0.2))},"
                   f"Default,,0,0,0,,{body}")


def _text_width(text: str, size: int) -> int:
    """粗估一行弹幕的像素宽度（只用于算滚动起点，不必精确）。"""
    w = 0.0
    for ch in text:
        w += 1.0 if ord(ch) > 0x2E80 else 0.55
    return int(w * size)


# ==========================================================================
# 落盘
# ==========================================================================

def save(xml_text: str, dest: pathlib.Path, *, formats: Iterable[str] = ("xml", "ass"),
         width: int = 1920, height: int = 1080, font: str = DEFAULT_FONT,
         alpha: float = DEFAULT_ALPHA, skip_special: bool = True,
         max_items: int = 0) -> Dict[str, Any]:
    """把一份弹幕 XML 存成若干格式，返回 {files, count}。

    dest 是**不带后缀的基名**（如 .../视频名），生成 视频名.danmaku.xml /
    视频名.ass。ASS 用视频同名后缀，播放器才会自动加载。
    """
    dest = pathlib.Path(dest)
    items = parse_xml(xml_text or "")
    want = {str(f).lower().lstrip(".") for f in formats}
    files: Dict[str, str] = {}
    if not items:
        return {"ok": False, "files": files, "count": 0, "error": "弹幕是空的"}

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass

    if "xml" in want:
        p = dest.with_name(dest.name + ".danmaku.xml")
        p.write_text(to_xml(items), encoding="utf-8")
        files["xml"] = str(p)
    if "ass" in want:
        # 用「基名 + .ass」而不是 with_suffix：视频名里常带点
        # （"某剧 S01E01.1080p" 会被 with_suffix 咬掉一截）
        p = dest.parent / (dest.name + ".ass")
        p.write_text(to_ass(items, width=width, height=height, font=font,
                            alpha=alpha, skip_special=skip_special,
                            max_items=max_items), encoding="utf-8")
        files["ass"] = str(p)
    if "json" in want:
        import json
        p = dest.with_name(dest.name + ".danmaku.json")
        p.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        files["json"] = str(p)
    return {"ok": bool(files), "files": files, "count": len(items)}


# ==========================================================================
# 抓取
# ==========================================================================

def cid_for(url: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """从视频链接拿到「第 p 个分P」的 cid。"""
    from . import bili

    info = bili.parse_url(url)
    if info.get("kind") != "video":
        return {"ok": False, "error": f"不是普通投稿（{info.get('kind')}），暂不支持"}
    kw = {"bvid": info["bvid"]} if info.get("bvid") else {"aid": int(info.get("aid") or 0)}
    v = bili.video_info(cfg, **kw)
    if not v.get("ok"):
        return {"ok": False, "error": v.get("error") or "拿不到视频信息"}
    data = v["data"]
    pages = data.get("pages") or []
    idx = max(1, int(info.get("page") or 1))
    if pages and idx <= len(pages):
        page = pages[idx - 1]
        return {"ok": True, "cid": int(page.get("cid") or 0),
                "title": data.get("title") or "", "part": page.get("part") or "",
                "page": idx, "pages": len(pages), "duration": page.get("duration") or 0}
    cid = int(data.get("cid") or 0)
    if not cid:
        return {"ok": False, "error": "视频信息里没有 cid"}
    return {"ok": True, "cid": cid, "title": data.get("title") or "",
            "part": "", "page": 1, "pages": 1,
            "duration": data.get("duration") or 0}


def for_video(url: str, cfg: Dict[str, Any], dest_base: pathlib.Path, *,
              formats: Iterable[str] = ("xml", "ass"), **kw) -> Dict[str, Any]:
    """按视频链接抓弹幕并存盘。

    dest_base 一般就是视频文件去掉后缀的名字，这样 ASS 能被播放器
    自动挂载（同名同目录）。
    """
    from . import bili

    got = cid_for(url, cfg)
    if not got.get("ok"):
        return {"ok": False, "error": got.get("error") or "定位 cid 失败"}
    try:
        xml_text = bili.danmaku_xml(cfg, got["cid"])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not (xml_text or "").strip():
        return {"ok": False, "error": "服务器返回的弹幕为空（可能要登录）"}
    out = save(xml_text, dest_base, formats=formats, **kw)
    out.update({"cid": got["cid"], "title": got.get("title") or "",
                "page": got.get("page"), "pages": got.get("pages")})
    return out


def describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "available": True,
        "formats": ["xml", "ass", "json"],
        "font": cfg.get("danmaku_font") or DEFAULT_FONT,
        "alpha": cfg.get("danmaku_alpha") or DEFAULT_ALPHA,
        "note": "滚动/顶部/底部弹幕会分配到不同轨道；高级弹幕默认丢弃",
    }
