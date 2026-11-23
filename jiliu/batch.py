"""批量链接：提取 → 通配符展开 → 并发预探测 → 勾选入队。

解决的是「60 个链接要粘贴 60 次」这个交互缺口。AB Download Manager
凭这一点拿了 16k star，技术上没有任何难度，纯粹是想到了。

三个容易做错的地方
  1. 整段文字里夹着链接（从网页/聊天记录复制）要能捞出来，不能只认
     一行一个 URL。
  2. 通配符 `[001-100]` 的补零位数必须跟随起始值，`[1-9]` 不能变成
     `01 02 …`。
  3. 探测失败不等于链接无效 —— 很多站不支持 HEAD，要退化成
     `Range: bytes=0-0`；再失败就标「未知」但仍然允许下载。
"""
from __future__ import annotations

import concurrent.futures
import re
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

# 提取链接：http(s)/ftp/magnet/ed2k/迅雷
_URL_RE = re.compile(
    r"(?:https?|ftp)://[^\s<>\"'\)\]]+"
    r"|magnet:\?[^\s<>\"']+"
    r"|ed2k://[^\s<>\"']+",
    re.IGNORECASE,
)
# 结尾常见的标点是被句子吃进去的，不是链接的一部分
_TRAIL = ",.;:!?，。；：！？、）】》\"'"

# 区间占位：[001-100] / [1-10:2] / [1..10]
_RANGE_RE = re.compile(r"\[(\d+)\s*(?:-|\.\.)\s*(\d+)(?:\s*:\s*(\d+))?\]")
# 枚举占位：{a,b,c} 或 {01,02}
_SET_RE = re.compile(r"\{([^{}]*)\}")

MAX_EXPAND = 2000


def _clean_one(raw: str) -> str:
    s = (raw or "").strip()
    # 去掉链接末尾的标点与被截断的右括号
    while s and s[-1] in _TRAIL:
        s = s[:-1]
    if s.endswith(")") and s.count("(") < s.count(")"):
        s = s[:-1]
    return s


def extract(text: str) -> List[str]:
    """从任意文本里捞出所有链接（保持原始顺序，不去重）。"""
    out: List[str] = []
    for m in _URL_RE.finditer(str(text or "")):
        u = _clean_one(m.group(0))
        if len(u) > 8:
            out.append(u)
    return out


def normalize_url(url: str) -> str:
    """去重用的规范化：协议与 host 小写、去掉 fragment、去掉默认端口。"""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except Exception:
        return url.strip()
    scheme = p.scheme.lower()
    host = (p.hostname or "").lower()
    port = p.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    q = p.query
    return urllib.parse.urlunsplit((scheme, host, p.path, q, ""))


def dedupe(urls: List[str]) -> Tuple[List[str], int]:
    """去重，保留首次出现顺序。返回 (去重后, 去掉的个数)。"""
    seen = set()
    out: List[str] = []
    for u in urls:
        key = normalize_url(u)
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out, len(urls) - len(out)


def _pad(num: int, width: int) -> str:
    return str(num).zfill(width) if width > 1 else str(num)


def expand(text: str, limit: int = MAX_EXPAND) -> Dict[str, Any]:
    """把通配符展开成候选字符串（不判 URL、不补协议）。

    支持：
        img-[001-100].jpg     区间（补零位数跟随起点）
        img-[1-10:2].jpg      步长
        img-[1..10].jpg       双点写法
        p-{a,b,c}.txt         枚举
    不支持 `*` —— 没有范围信息，猜出来的一定不是用户要的，直接说明。
    """
    raw = str(text or "").strip()
    if not raw:
        return {"ok": True, "lines": [], "count": 0, "error": "", "truncated": False}

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    out: List[str] = []
    truncated = False

    for line in lines:
        cands = [line]
        # 先展开区间（可能多个），再展开枚举
        for _ in range(3):
            nxt: List[str] = []
            hit = False
            for c in cands:
                m = _RANGE_RE.search(c)
                if not m:
                    nxt.append(c)
                    continue
                hit = True
                start_s, end_s, step_s = m.group(1), m.group(2), m.group(3)
                width = len(start_s) if start_s.startswith("0") and len(start_s) > 1 else 1
                try:
                    a, b = int(start_s), int(end_s)
                    step = int(step_s) if step_s else (1 if b >= a else -1)
                except Exception:
                    nxt.append(c)
                    continue
                if step == 0:
                    return {"ok": False, "lines": [], "count": 0,
                            "error": f"步长不能为 0：{m.group(0)}", "truncated": False}
                span = b - a
                if (span > 0 and step < 0) or (span < 0 and step > 0):
                    step = -step
                count = abs(span) // abs(step) + 1
                if count > limit:
                    return {"ok": False, "lines": [], "count": 0,
                            "error": f"区间展开 {count} 条，超过 {limit} 条上限（请缩小范围）",
                            "truncated": False}
                stop = b + (1 if step > 0 else -1)
                for v in range(a, stop, step):
                    nxt.append(c[:m.start()] + _pad(v, width) + c[m.end():])
            cands = nxt
            if not hit:
                break

        expanded: List[str] = []
        for c in cands:
            m = _SET_RE.search(c)
            if not m:
                expanded.append(c)
                continue
            parts = [x.strip() for x in m.group(1).split(",")]
            parts = [p for p in parts if p]
            if not parts:
                expanded.append(c)
                continue
            if len(parts) > limit:
                return {"ok": False, "lines": [], "count": 0,
                        "error": f"枚举超过 {limit} 条上限", "truncated": False}
            for p in parts:
                expanded.append(c[:m.start()] + p + c[m.end():])

        for c in expanded:
            if "*" in c:
                return {"ok": False, "lines": [], "count": 0,
                        "error": "不支持 * 通配：没有范围信息，请写成 [1-100] 这样的区间",
                        "truncated": False}
            if len(out) >= limit:
                truncated = True
                break
            out.append(c)
        if truncated:
            break

    if truncated:
        return {"ok": False, "lines": out, "count": len(out),
                "error": f"展开超过 {limit} 条上限，已拒绝（请缩小范围）",
                "truncated": True}
    return {"ok": True, "lines": out, "count": len(out), "error": "", "truncated": False}


def _already(url: str, existing: Optional[set]) -> bool:
    """这条链接是不是已经在任务列表/下过了。

    用调用方传进来的规范化集合判断 —— 不去碰 history.json：
    那个文件按「站点 + 视频 ID」记，判 URL 反而容易误判。
    """
    if not existing:
        return False
    return normalize_url(url) in existing


def probe_one(url: str, cfg: Dict[str, Any], timeout: float = 4.0,
              existing: Optional[set] = None) -> Dict[str, Any]:
    """探测单个链接。任何异常都变成 ok=False + reason，不抛出。"""
    row: Dict[str, Any] = {
        "url": url,
        "ok": False,
        "known": False,
        "size": 0,
        "size_text": "",
        "type": "",
        "filename": "",
        "accept_ranges": False,
        "error": "",
        "already": _already(url, existing),
        "category": "",
    }
    try:
        from . import engine
        from .engine import human_size
        pr = engine.probe(url, cfg, timeout=max(1, int(timeout)))
        # engine.probe 不抛异常，失败信息放在 .error / .status 里。
        # 判「探到了」的标准是拿到了大小 —— 只看 error 为空会误判：
        # 有的站 HEAD 405 但 GET Range 成功，error 会被清掉，那才是真探到。
        size = getattr(pr, "size", None)
        status = int(getattr(pr, "status", 0) or 0)
        err = str(getattr(pr, "error", "") or "")
        if size is None and (err or status >= 400 or status == 0):
            row["error"] = (err or f"HTTP {status}")[:160]
            row["size_text"] = "未知"
        else:
            row["ok"] = True
            row["known"] = size is not None
            row["size"] = int(size or 0)
            row["type"] = str(getattr(pr, "content_type", "") or "")
            row["filename"] = str(getattr(pr, "filename", "") or "")
            row["accept_ranges"] = bool(getattr(pr, "accept_ranges", False))
            plan = getattr(pr, "plan", None)
            if isinstance(plan, dict):
                row["category"] = str(plan.get("category") or "")
            elif plan is not None:
                row["category"] = str(getattr(plan, "category", "") or "")
            row["size_text"] = human_size(row["size"]) if row["size"] else "未知"
    except Exception as exc:  # noqa: BLE001
        # 超时/不支持 HEAD 都只是「未知」，不是「不能下」
        row["error"] = f"{type(exc).__name__}: {exc}"[:160]
        row["size_text"] = "未知"
    return row


def probe_many(urls: List[str], cfg: Dict[str, Any], *, workers: int = 8,
               timeout: float = 4.0,
               existing: Optional[set] = None,
               progress: Optional[Callable[[int, int], None]] = None) -> List[Dict[str, Any]]:
    """并发探测。保持输入顺序返回。"""
    urls = [u for u in urls if u]
    if not urls:
        return []
    out: List[Optional[Dict[str, Any]]] = [None] * len(urls)
    done = 0
    lock = __import__("threading").Lock()

    def work(i: int) -> None:
        nonlocal done
        out[i] = probe_one(urls[i], cfg, timeout, existing)
        with lock:
            done += 1
            if progress:
                try:
                    progress(done, len(urls))
                except Exception:
                    pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as ex:
        list(ex.map(work, range(len(urls))))
    return [r for r in out if r is not None]


def parse(text: str, cfg: Dict[str, Any], *, limit: int = MAX_EXPAND) -> Dict[str, Any]:
    """提取 + 展开 + 去重（不联网）。批量面板先调这个再调探测。

    一行里可能出现三种东西：
      1. 一个或多个完整 URL（整段网页复制过来的）
      2. 通配符模板（img-[001-100].jpg）
      3. 裸域名/路径（example.com/a.zip）—— 补 https://
    三种混在一段文本里也要能各归各位。
    """
    ex = expand(text, limit=limit)
    if not ex["ok"] and not ex["lines"]:
        return {"ok": False, "error": ex["error"], "urls": [], "dups": 0}

    collected: List[str] = []
    for line in ex["lines"]:
        found = extract(line)
        if found:
            collected.extend(found)
            continue
        s = line.strip()
        if not s:
            continue
        if re.match(r"^(https?|ftp)://|magnet:|ed2k://", s, re.I):
            collected.append(s)
        elif re.match(r"^[\w.-]+\.[a-zA-Z]{2,}(/|$|\?)", s):
            # 裸域名：example.com/a.zip → https://example.com/a.zip
            collected.append("https://" + s.lstrip("/"))

    uniq, dups = dedupe(collected)
    return {"ok": True, "urls": uniq, "dups": dups,
            "error": ex["error"], "truncated": ex.get("truncated", False),
            "count": len(uniq)}


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    from .engine import human_size
    total = len(rows)
    known = sum(1 for r in rows if r.get("known"))
    sized = sum(int(r.get("size") or 0) for r in rows)
    already = sum(1 for r in rows if r.get("already"))
    ranges = sum(1 for r in rows if r.get("accept_ranges"))
    return {
        "total": total,
        "known": known,
        "unknown": total - known,
        "already": already,
        "ranges": ranges,
        "bytes": sized,
        "size_text": human_size(sized) if sized else "-",
    }
