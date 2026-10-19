"""命名模板、归档目录、已下载记录。

这三件事其实是一件事：**文件落哪儿、叫什么、下没下过**。
参考了 Bilidown 的做法 —— 它那套 `%title%` 变量 + 归档模板 + 已下载
记录确实好用，这里按同样的思路做，但翻译成 yt-dlp 的模板语法。

一个关键区别（也是唯一诚实的做法）：
    我们的变量最终要翻成 **yt-dlp 的 outtmpl** 才能生效，因为标题、
    上传者这些值只有 yt-dlp 在下载那一刻才知道。
    所以变量表里只放 yt-dlp 真有的字段。
    「下载日期」yt-dlp 没有内建字段，就不提供 —— 与其给个假变量，
    不如不给。（发布日期的字段是有的，见 %pubdate%。）
"""
from __future__ import annotations

import json
import pathlib
import re
import time
from typing import Any, Dict, List, Optional

from . import config as _config

# --------------------------------------------------------------------------
# 变量表
# --------------------------------------------------------------------------
# key: 给用户写的；yt: 翻成 yt-dlp 的表达式
VARS: List[Dict[str, str]] = [
    # 视频相关
    {"key": "%title%", "label": "视频标题", "yt": "%(title).150B", "group": "视频"},
    {"key": "%id%", "label": "视频 ID", "yt": "%(id)s", "group": "视频"},
    {"key": "%bv%", "label": "BV 号 / 视频号", "yt": "%(display_id)s", "group": "视频"},
    {"key": "%up%", "label": "UP 主 / 作者", "yt": "%(uploader).80B", "group": "视频"},
    {"key": "%uid%", "label": "UP 主 ID", "yt": "%(uploader_id)s", "group": "视频"},
    {"key": "%site%", "label": "站点名", "yt": "%(extractor_key)s", "group": "视频"},
    {"key": "%type%", "label": "视频类型（单视频/合集）",
     "yt": "%(playlist,type|单视频)B", "group": "视频"},
    # 合集相关
    {"key": "%pl_title%", "label": "合集 / 播放列表名",
     "yt": "%(playlist_title,playlist)s", "group": "合集"},
    {"key": "%pl_index%", "label": "合集内序号",
     "yt": "%(playlist_index)03d", "group": "合集"},
    {"key": "%pl_count%", "label": "合集总集数",
     "yt": "%(playlist_count)03d", "group": "合集"},
    # 日期相关（都是**发布**日期 —— 下载日期 yt-dlp 没有字段）
    {"key": "%pubdate%", "label": "发布日期 20240131",
     "yt": "%(upload_date)s", "group": "日期"},
    {"key": "%pubyear%", "label": "发布年", "yt": "%(upload_date>%Y)s", "group": "日期"},
    {"key": "%pubmonth%", "label": "发布月", "yt": "%(upload_date>%m)s", "group": "日期"},
    {"key": "%pubday%", "label": "发布日", "yt": "%(upload_date>%d)s", "group": "日期"},
    {"key": "%pub_ymd%", "label": "发布日期带横杠 2024-01-31",
     "yt": "%(upload_date>%Y-%m-%d)s", "group": "日期"},
    # 其他
    {"key": "%ext%", "label": "扩展名", "yt": "%(ext)s", "group": "其他"},
    {"key": "%quality%", "label": "分辨率", "yt": "%(resolution)s", "group": "其他"},
    {"key": "%lang%", "label": "语言", "yt": "%(language,und)s", "group": "其他"},
]

_VAR_MAP = {v["key"]: v["yt"] for v in VARS}
_VAR_RE = re.compile(r"%[a-zA-Z_]+%")

DEFAULT_FILE_PATTERN = "%title% [%id%]"
DEFAULT_DIR_PATTERN = ""
DEFAULT_PLAYLIST_PREFIX = "%pl_index% - "
# 合集文件名里判断「有没有加过序号」用的
VAR_INDEX = "%pl_index%"

# Windows 文件名禁用字符。yt-dlp 的 `.NB` 之类会处理，但归档目录名
# 是我们自己拼的，得自己清。
_BAD_CHARS = '<>:"/\\|?*'

# 受保护的片段：变量、yt-dlp 的字段表达式、以及目录分隔符 "/"。
# 这些东西里面出现的字符**不能**当成非法字符清掉，否则模板就废了：
#   %(upload_date>%Y)s 里的 ">"、%(a,b|默认)s 里的 "|"、"(...)" 括号本身
#
# ⚠ 最外层那对括号是**必需的捕获组**：re.split 只有把分隔符捕获下来
#   才会把它留在结果里。写成 (?:...) 的话分隔符会被整段丢弃，
#   于是 %(title).150B 直接消失 —— 这个坑踩过一次。
_PROTECT_RE = re.compile(r"(%\([^)]*\)(?:[.\d]*[a-zA-Z])?|%[a-zA-Z_]+%|/)")


def _sanitize_literals(tpl: str) -> str:
    """只清模板里**字面部分**的非法字符，变量部分原样保留。

    用户写 `%title%: 高清` 时那个冒号会让 Windows 建不出目录，
    但整串 sanitize 又会把 `%(...)` 里的括号一起吃掉 —— 所以要分开处理。
    `re.split` 带捕获组时，奇数下标就是被捕获的受保护片段。
    """
    parts = _PROTECT_RE.split(tpl or "")
    out: List[str] = []
    for i, seg in enumerate(parts):
        if i % 2 == 1:                       # 受保护片段，原样留着
            out.append(seg)
            continue
        out.append("".join("_" if ch in _BAD_CHARS else ch for ch in seg))
    return "".join(out)


def variables() -> List[Dict[str, str]]:
    """给界面展示可选变量。"""
    return list(VARS)


def check(pattern: str) -> Dict[str, Any]:
    """检查模板里的变量是否都认识。界面用来实时提示。"""
    if not pattern:
        return {"ok": True, "unknown": [], "used": [], "suggest": ""}
    used = _VAR_RE.findall(pattern)
    unknown = [u for u in used if u not in _VAR_MAP]
    out: Dict[str, Any] = {
        "ok": not unknown,
        "unknown": sorted(set(unknown)),
        "used": sorted(set(used)),
        "suggest": "",
    }
    if unknown:
        # 给个最接近的建议，省得用户对着说明猜
        best, score = "", 0
        for u in set(unknown):
            core = u.strip("%").lower()
            for v in VARS:
                k = v["key"].strip("%")
                common = len(set(core) & set(k))
                if common > score:
                    best, score = v["key"], common
        if best and score >= 2:
            out["suggest"] = pattern.replace(unknown[0], best)
    return out


def sanitize(name: str) -> str:
    """清掉文件名里的非法字符。"""
    s = "".join(("_" if ch in _BAD_CHARS else ch) for ch in (name or ""))
    s = s.strip().strip(".")
    return s or "未命名"


def to_ytdlp(pattern: str, default: str = DEFAULT_FILE_PATTERN) -> str:
    """把 `%title% [%id%]` 翻成 yt-dlp 的 `%(title).150B [%(id)s]`。

    认不出来的 `%xxx%` 原样保留 —— 宁可让 yt-dlp 报错让用户发现，
    也不要静默丢一段模板，那样文件名的样子会莫名其妙。
    """
    p = (pattern or "").strip() or default
    for k, v in _VAR_MAP.items():
        p = p.replace(k, v)
    return p


def build_output(dir_pattern: str, file_pattern: str, *,
                 is_playlist: bool = False, use_playlist_folder: bool = True
                 ) -> str:
    """拼出完整的 yt-dlp 输出模板（含归档目录）。

    目录层级：
        <下载目录> / <归档模板渲染> / <合集名> / <文件名模板>
    合集那层只在是合集、且开着 playlist_folder 时才有 ——
    不然几十个文件会和下载目录里原有的东西糊在一起。
    """
    parts: List[str] = []
    if dir_pattern:
        parts.append(_sanitize_literals(to_ytdlp(dir_pattern, "")))
    if is_playlist and use_playlist_folder:
        parts.append("%(playlist_title,playlist|合集).120B")
    parts.append(_sanitize_literals(to_ytdlp(file_pattern)) + ".%(ext)s")
    return "/".join(p for p in parts if p)


# --------------------------------------------------------------------------
# 已下载记录
# --------------------------------------------------------------------------

class History:
    """已下载记录。按「站点 + 视频 ID」判重，避免同一集下两遍。

    存 JSON 而不是 sqlite：条目量级在几千，JSON 足够，而且出问题
    用户自己能打开看、能手动改。
    """

    def __init__(self, path: Optional[pathlib.Path] = None, limit: int = 5000):
        self.path = pathlib.Path(path) if path else (_config.HOME / "history.json")
        self.limit = limit
        self._data: Optional[List[Dict[str, Any]]] = None

    # ---- 读写 ----
    def _load(self) -> List[Dict[str, Any]]:
        if self._data is not None:
            return self._data
        rows: List[Dict[str, Any]] = []
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    rows = [r for r in raw if isinstance(r, dict)]
            except Exception:
                rows = []
        self._data = rows
        return rows

    def _save(self) -> None:
        rows = self._load()
        if len(rows) > self.limit:
            del rows[: len(rows) - self.limit]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    @staticmethod
    def key_of(site: str, vid: str) -> str:
        return f"{(site or '?').lower()}::{(vid or '?')}"

    # ---- 查 ----
    def has(self, site: str, vid: str) -> bool:
        k = self.key_of(site, vid)
        return any(r.get("key") == k for r in self._load())

    def get(self, site: str, vid: str) -> Optional[Dict[str, Any]]:
        k = self.key_of(site, vid)
        for r in reversed(self._load()):
            if r.get("key") == k:
                return r
        return None

    def list(self, *, q: str = "", limit: int = 300) -> List[Dict[str, Any]]:
        rows = self._load()
        if q:
            ql = q.lower()
            rows = [r for r in rows
                    if ql in str(r.get("title", "")).lower()
                    or ql in str(r.get("up", "")).lower()
                    or ql in str(r.get("vid", "")).lower()]
        return list(reversed(rows))[:limit]

    def stats(self) -> Dict[str, Any]:
        rows = self._load()
        by_site: Dict[str, int] = {}
        for r in rows:
            s = r.get("site") or "?"
            by_site[s] = by_site.get(s, 0) + 1
        return {"total": len(rows), "by_site": by_site,
                "bytes": sum(int(r.get("size") or 0) for r in rows)}

    # ---- 写 ----
    def add(self, *, site: str, vid: str, title: str = "", up: str = "",
            url: str = "", path: str = "", size: int = 0,
            quality: str = "", extra: Optional[Dict[str, Any]] = None) -> None:
        k = self.key_of(site, vid)
        rows = self._load()
        rows[:] = [r for r in rows if r.get("key") != k]   # 同一条只留最新
        entry: Dict[str, Any] = {
            "key": k, "site": site, "vid": vid, "title": title, "up": up,
            "url": url, "path": path, "size": int(size or 0),
            "quality": quality, "at": int(time.time()),
        }
        if extra:
            entry.update(extra)
        rows.append(entry)
        self._save()

    def remove(self, key: str) -> bool:
        rows = self._load()
        before = len(rows)
        rows[:] = [r for r in rows if r.get("key") != key]
        if len(rows) != before:
            self._save()
            return True
        return False

    def clear(self) -> int:
        n = len(self._load())
        self._data = []
        self._save()
        return n

    # ---- 导入导出（Bilidown 有「加密导入导出」，这里做明文 —— 
    #      加密给人虚假的安全感，记录本身不含密码）----
    def export_to(self, dest: pathlib.Path) -> pathlib.Path:
        dest = pathlib.Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(self._load(), ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return dest

    def import_from(self, src: pathlib.Path, merge: bool = True) -> int:
        src = pathlib.Path(src)
        raw = json.loads(src.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("记录文件格式不对，应该是 JSON 数组")
        incoming = [r for r in raw if isinstance(r, dict) and r.get("key")]
        if not merge:
            self._data = incoming
            self._save()
            return len(incoming)
        rows = self._load()
        index = {r.get("key"): i for i, r in enumerate(rows)}
        n = 0
        for r in incoming:
            i = index.get(r["key"])
            if i is None:
                rows.append(r)
                index[r["key"]] = len(rows) - 1
            else:
                rows[i] = r
            n += 1
        self._save()
        return n
