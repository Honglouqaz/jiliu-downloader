"""任务导入导出：JSON / aria2 输入文件 / 纯文本清单。

    from jiliu import transfer
    text = transfer.export(tasks, "aria2")
    r = transfer.import_text(text)        # 自动认格式

为什么是这三种格式：
  - **JSON**：自己的一份完整快照，带着队列、线程数、请求头、预期哈希。
    换机器搬迁、备份任务表都靠它。
  - **aria2**：`.aria2` 输入文件（aria2c -i list.txt）。这是老玩家的
    通用货币，从别的下载器搬过来、或搬出去给 NAS 用，就靠它。
  - **txt**：一行一个链接。谁都会做，也谁都认。

三者都是「单向可降级」的：JSON 能生成 aria2/txt，反过来不行 ——
aria2 里没有队列概念，txt 里连文件名都没有。导入时缺什么补什么。
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Any, Dict, Iterable, List, Tuple

MAGIC = "jiliu.tasks"
FORMAT_VERSION = 1
FORMATS = ("json", "aria2", "txt")

# 能跨机器搬的字段。运行态（status/percent/速度）一律不带 ——
# 导进去就该是「待下载」，而不是「上次跑到 37%」。
_EXPORT_FIELDS = ("url", "kind", "filename", "dest_dir", "threads", "queue",
                  "chunk_size", "verify_hash", "expected_sha256", "category",
                  "format", "expected_size")
# meta 里值得带走的键（其余是运行时产物）
_META_KEYS = ("headers", "mirrors", "p2p", "info_hash", "trackers",
              "playlist", "playlist_items", "group_title", "audio_only",
              "subs_only", "danmaku", "live_minutes", "speed_limit",
              "expected_sha256", "referer", "resource")


# ==========================================================================
# 导出
# ==========================================================================

def export(tasks: Iterable[Any], fmt: str = "json", *, cfg: Dict[str, Any] | None = None
           ) -> Dict[str, Any]:
    """把任务列表导出成文本。返回 {ok, fmt, text, count, skipped}。

    tasks 可以是 Task 对象，也可以是 dict（从 JSON 读回来再导出）。
    """
    fmt = (fmt or "json").lower().strip()
    if fmt not in FORMATS:
        return {"ok": False, "fmt": fmt, "text": "", "count": 0, "skipped": 0,
                "error": f"不认识的格式：{fmt}"}
    rows = [_row(t) for t in tasks if _url_of(t)]
    if fmt == "json":
        return _ok(fmt, _dump_json(rows), len(rows), 0)
    if fmt == "aria2":
        text, used, skipped = _dump_aria2(rows)
        return _ok(fmt, text, used, skipped)
    return _ok(fmt, _dump_txt(rows), len(rows), 0)


def _ok(fmt: str, text: str, count: int, skipped: int) -> Dict[str, Any]:
    return {"ok": True, "fmt": fmt, "text": text, "count": count,
            "skipped": skipped, "bytes": len(text.encode("utf-8"))}


def _url_of(t: Any) -> str:
    if isinstance(t, dict):
        return str(t.get("url") or "").strip()
    return str(getattr(t, "url", "") or "").strip()


def _row(t: Any) -> Dict[str, Any]:
    """Task / dict → 可移植的扁平字典。"""
    if isinstance(t, dict):
        raw = dict(t)
        meta = dict(raw.get("meta") or {})
    else:
        raw = {}
        for f in _EXPORT_FIELDS:
            raw[f] = getattr(t, f, None)
        meta = dict(getattr(t, "meta", None) or {})
        raw["expected_size"] = getattr(t, "size", None) if not raw.get("expected_size") else raw["expected_size"]

    out: Dict[str, Any] = {}
    for f in _EXPORT_FIELDS:
        v = raw.get(f)
        if v in (None, "", 0) and f not in ("url",):
            continue
        out[f] = v
    out["url"] = _url_of(t)
    m = {k: meta[k] for k in _META_KEYS if meta.get(k) not in (None, "", [], {})}
    # resource 里全是识别结论，跨机器没意义，只留类型
    if "resource" in m:
        r = m["resource"] or {}
        m["resource"] = {"category": r.get("category"), "label": r.get("label")}
    if m:
        out["meta"] = m
    return out


def _dump_json(rows: List[Dict[str, Any]]) -> str:
    env = {
        "magic": MAGIC,
        "version": FORMAT_VERSION,
        "app": "汲流 Jiliu",
        "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(rows),
        "tasks": rows,
    }
    return json.dumps(env, ensure_ascii=False, indent=2)


def _dump_aria2(rows: List[Dict[str, Any]]) -> Tuple[str, int, int]:
    """aria2c -i 的输入格式。

    语法要点（踩过的坑）：
      - URI 行必须顶格，选项行缩进 1 个空格以上
      - 选项只对「上面最近的那个 URI」生效，所以每个 URI 都要重写一遍
      - 同一个 URI 可以重复出现，那表示镜像/分片源
      - 不能出现空选项值，否则 aria2 直接拒绝整个文件
    """
    lines: List[str] = [
        "# 由汲流导出 —— aria2c -i this-file.txt",
        f"# {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    used = skipped = 0
    for r in rows:
        url = r["url"]
        if r.get("kind") == "video":
            # 视频站的「页面地址」不是文件地址，aria2 拿到只会下载一个 HTML
            lines.append(f"# 跳过视频任务（需 yt-dlp 解析）：{url}")
            skipped += 1
            continue
        if not url.lower().startswith(("http://", "https://", "ftp://",
                                       "magnet:", "ed2k://")):
            skipped += 1
            continue
        opts: List[Tuple[str, str]] = []
        d = r.get("dest_dir")
        if d:
            opts.append(("dir", str(d)))
        if r.get("filename"):
            opts.append(("out", str(r["filename"])))
        threads = int(r.get("threads") or 0)
        if threads > 1:
            opts.append(("split", str(threads)))
            opts.append(("max-connection-per-server", str(threads)))
        chunk = int(r.get("chunk_size") or 0)
        if chunk > 0:
            opts.append(("min-split-size", str(max(1048576, chunk))))
        meta = r.get("meta") or {}
        for k, v in (meta.get("headers") or {}).items():
            if v:
                # header 值里若含换行会把文件撑坏，压成一行
                opts.append(("header", f"{k}: {str(v).replace(chr(10), ' ').replace(chr(13), ' ')}"))
        lines.append(url)
        for k, v in opts:
            lines.append(f"  {k}={v}")
        if r.get("expected_sha256"):
            # aria2 只认 checksum=，且要带算法前缀
            lines.append(f"  checksum=sha-256={r['expected_sha256']}")
        lines.append("")
        # 镜像只写成注释：aria2 的输入文件里没有「多源合一」的语法，
        # 重复写 URI 会被当成两个独立下载（白下一遍）。
        for mir in (meta.get("mirrors") or []):
            if mir and mir != url:
                lines.append(f"# 备用镜像：{mir}")
        used += 1
    return "\n".join(lines).rstrip() + "\n", used, skipped


def _dump_txt(rows: List[Dict[str, Any]]) -> str:
    """一行一个链接。带文件名时用 Tab 分隔（跟 IDM 的导出兼容）。"""
    out: List[str] = []
    for r in rows:
        if r.get("filename"):
            out.append(f"{r['url']}\t{r['filename']}")
        else:
            out.append(r["url"])
    return "\n".join(out) + ("\n" if out else "")


# ==========================================================================
# 导入
# ==========================================================================

def detect_format(text: str) -> str:
    """认一下这段文本是什么格式。"""
    s = (text or "").lstrip("\ufeff \t\r\n")
    if not s:
        return ""
    if s[0] in "[{":
        try:
            data = json.loads(s)
        except Exception:  # noqa: BLE001
            return "txt"
        if isinstance(data, list):
            return "json"
        if isinstance(data, dict):
            # 注意用 `in` 而不是真值判断：{"tasks": []} 是合法的空导出
            if "tasks" in data or data.get("magic") == MAGIC:
                return "json"
        return "txt"
    head = s[:400].lower()
    if "aria2" in head or " checksum=" in s.lower() or "\n  out=" in s.lower():
        return "aria2"
    # 有缩进的选项行基本就是 aria2
    for line in s.splitlines()[:40]:
        if line.startswith(" ") and "=" in line:
            return "aria2"
    return "txt"


def import_text(text: str, fmt: str = "auto") -> Dict[str, Any]:
    """把文本解析成任务字典列表。

    返回 {ok, fmt, tasks, count, skipped, error}
    每一项都长得像 create_http 的参数，直接喂给 manager 就行。
    """
    fmt = (fmt or "auto").lower()
    if fmt in ("auto", "", "detect"):
        fmt = detect_format(text)
    if not fmt:
        return {"ok": False, "fmt": "", "tasks": [], "count": 0, "skipped": 0,
                "error": "内容是空的"}
    if fmt == "json":
        return _import_json(text)
    if fmt == "aria2":
        return _import_aria2(text)
    return _import_txt(text)


def _import_json(text: str) -> Dict[str, Any]:
    try:
        data = json.loads((text or "").lstrip("\ufeff"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "fmt": "json", "tasks": [], "count": 0, "skipped": 0,
                "error": f"JSON 解析失败：{exc}"}
    if isinstance(data, dict):
        rows = data.get("tasks") or []
    elif isinstance(data, list):
        rows = data
    else:
        return {"ok": False, "fmt": "json", "tasks": [], "count": 0, "skipped": 0,
                "error": "JSON 顶层既不是数组也没有 tasks 字段"}

    tasks: List[Dict[str, Any]] = []
    skipped = 0
    for r in rows:
        if not isinstance(r, dict):
            skipped += 1
            continue
        url = str(r.get("url") or "").strip()
        if not url:
            skipped += 1
            continue
        t: Dict[str, Any] = {"url": url}
        for k in ("kind", "filename", "dest_dir", "category", "format"):
            if r.get(k):
                t[k] = r[k]
        for k in ("threads", "chunk_size"):
            try:
                v = int(r.get(k) or 0)
            except (TypeError, ValueError):
                v = 0
            if v > 0:
                t[k] = v
        if r.get("queue"):
            t["queue"] = str(r["queue"])
        if r.get("verify_hash"):
            t["verify_hash"] = True
        if r.get("expected_sha256"):
            t["expected_sha256"] = str(r["expected_sha256"])
        meta = r.get("meta")
        if isinstance(meta, dict) and meta:
            t["meta"] = {k: v for k, v in meta.items() if v not in (None, "", [], {})}
        tasks.append(t)
    return {"ok": True, "fmt": "json", "tasks": tasks, "count": len(tasks),
            "skipped": skipped, "error": ""}


def _import_aria2(text: str) -> Dict[str, Any]:
    """解析 aria2 输入文件。

    语法：顶格的 URI 行 + 紧跟其后的缩进选项行，直到下一个顶格行。
    「同一个 dir + out」出现多次时合并成一条任务，多出来的 URI 记为镜像 ——
    这是从别处导出的清单里最常见的形态。
    """
    blocks: List[Dict[str, Any]] = []
    cur_url = ""
    opts: Dict[str, List[str]] = {}
    skipped = 0

    def flush() -> None:
        nonlocal cur_url
        if cur_url:
            blocks.append({"url": cur_url, "opts": {k: list(v) for k, v in opts.items()}})
        cur_url = ""
        opts.clear()

    for raw in (text or "").lstrip("\ufeff").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            body = line.strip()
            if "=" in body:
                k, v = body.split("=", 1)
                opts.setdefault(k.strip().lower(), []).append(v.strip())
            continue
        flush()
        low = line.strip().lower()
        if not low.startswith(("http://", "https://", "ftp://", "magnet:", "ed2k://")):
            skipped += 1
            cur_url = ""
            continue
        cur_url = line.strip()
    flush()

    tasks: List[Dict[str, Any]] = []
    seen: Dict[Tuple[str, str], int] = {}
    for b in blocks:
        o = b["opts"]
        d = (o.get("dir") or [""])[-1]
        name = (o.get("out") or [""])[-1]
        key = (d, name)
        if name and key in seen:
            # 同目录同名 = 同一份资源的另一个源
            t = tasks[seen[key]]
            if b["url"] != t["url"]:
                t.setdefault("meta", {}).setdefault("mirrors", []).append(b["url"])
            continue

        t: Dict[str, Any] = {"url": b["url"], "meta": {}}
        if d:
            t["dest_dir"] = d
        if name:
            t["filename"] = name
        for key_name in ("split", "max-connection-per-server"):
            if o.get(key_name):
                try:
                    t["threads"] = max(int(t.get("threads") or 0), int(o[key_name][-1]))
                except (TypeError, ValueError):
                    pass
        if o.get("min-split-size"):
            try:
                t["chunk_size"] = int(o["min-split-size"][-1])
            except (TypeError, ValueError):
                pass
        headers: Dict[str, str] = {}
        for h in o.get("header") or []:
            if ":" in h:
                k, v = h.split(":", 1)
                if k.strip():
                    headers[k.strip()] = v.strip()
        if headers:
            t["meta"]["headers"] = headers
        if o.get("checksum"):
            c = o["checksum"][-1]
            if "=" in c:
                algo, hexv = c.split("=", 1)
                if "sha-256" in algo.replace("_", "-").lower() and len(hexv.strip()) >= 64:
                    t["expected_sha256"] = hexv.strip()
                    t["verify_hash"] = True
        if not t["meta"]:
            del t["meta"]
        if name:
            seen[key] = len(tasks)
        tasks.append(t)
    return {"ok": True, "fmt": "aria2", "tasks": tasks, "count": len(tasks),
            "skipped": skipped, "error": ""}


def _import_txt(text: str) -> Dict[str, Any]:
    """纯文本：一行一个链接，允许「链接<Tab>文件名」。"""
    from . import batch

    tasks: List[Dict[str, Any]] = []
    seen: set = set()
    skipped = 0
    for line in (text or "").lstrip("\ufeff").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        name = ""
        url_part = raw
        if "\t" in raw:
            url_part, name = raw.split("\t", 1)
            url_part, name = url_part.strip(), name.strip()
        # 一行里可能夹着说明文字，交给 batch.extract 去捞链接
        found = batch.extract(url_part)
        if not found:
            skipped += 1
            continue
        for u in found:
            if u in seen:
                continue
            seen.add(u)
            t: Dict[str, Any] = {"url": u}
            if name and len(found) == 1:
                t["filename"] = name
            tasks.append(t)
    if not tasks:
        # 一段既不是 JSON、又捞不到链接的文字，别假装导入成功
        return {"ok": False, "fmt": "txt", "tasks": [], "count": 0,
                "skipped": skipped, "error": "没从文本里找到可下载的链接"}
    return {"ok": True, "fmt": "txt", "tasks": tasks, "count": len(tasks),
            "skipped": skipped, "error": ""}


# ==========================================================================
# 文件级
# ==========================================================================

def save_file(tasks: Iterable[Any], path: pathlib.Path, fmt: str = "") -> Dict[str, Any]:
    """导出到文件。fmt 留空就按后缀猜。"""
    p = pathlib.Path(path)
    f = (fmt or p.suffix.lstrip(".") or "json").lower()
    if f in ("aria2", "txt2"):
        f = "aria2"
    r = export(tasks, "txt" if f in ("text", "list", "url") else f)
    if not r.get("ok"):
        return r
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(r["text"], encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {**r, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {**r, "path": str(p)}


def load_file(path: pathlib.Path) -> Dict[str, Any]:
    p = pathlib.Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "fmt": "", "tasks": [], "count": 0, "skipped": 0,
                "error": f"{type(exc).__name__}: {exc}"}
    fmt = "json" if p.suffix.lower() == ".json" else "auto"
    return import_text(text, fmt)


def describe() -> Dict[str, Any]:
    return {
        "formats": list(FORMATS),
        "version": FORMAT_VERSION,
        "fields": list(_EXPORT_FIELDS),
        "note": "JSON 带队列/请求头/哈希，可完整迁移；aria2/txt 只带链接与目标名",
    }
