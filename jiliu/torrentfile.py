"""本地种子/下载描述文件的处理。

汲流要「打开」的不只是经典 .torrent，实际流通的相关文件还有：
  - .magnet / .url / .txt —— 存磁力或链接的文本文件
  - .meta4 / .metalink —— Metalink 多源下载描述（XML），aria2 原生支持

这里只做**解析**（纯函数、不联网、不碰 aria2）：
建任务、落盘、交给引擎都是 server / manager 的事。

bencode 是自己写的最小实现 —— BitTorrent 元信息的编码只有四种类型
（整数 / 字节串 / 列表 / 字典），40 行足够，不值得为此引依赖。
info_hash 的算法：info 字典的**原始 bencode 字节**做 SHA-1。
注意必须用原始字节而不是重新编码 —— 重新编码可能改变键序，哈希就变了。
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple


class TorrentError(ValueError):
    """种子/描述文件解析失败。"""


# ---------------------------------------------------------------- bencode

def _decode(data: bytes, i: int) -> Tuple[Any, int]:
    """从 data[i:] 解码一个 bencode 值，返回 (值, 结束下标)。"""
    c = data[i:i + 1]
    if c == b"i":  # 整数 i<number>e
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":  # 列表 l<value...>e
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _decode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":  # 字典 d<str><value>...e
        i += 1
        out: Dict[bytes, Any] = {}
        while data[i:i + 1] != b"e":
            k, i = _decode(data, i)
            v, i = _decode(data, i)
            out[k] = v
        return out, i + 1
    # 字节串 <len>:<bytes>
    j = data.index(b":", i)
    n = int(data[i:j])
    start = j + 1
    return data[start:start + n], start + n


def _decode_top(data: bytes) -> Any:
    v, end = _decode(data, 0)
    if end != len(data):
        raise TorrentError("种子文件尾部有多余数据（文件可能损坏）")
    return v


def info_raw(data: bytes) -> bytes:
    """取 info 字典的原始 bencode 字节（算 info_hash 用，见模块头注释）。"""
    i, n = 0, len(data)
    if data[i:i + 1] != b"d":
        raise TorrentError("种子文件不是 bencode 字典")
    i += 1
    while i < n and data[i:i + 1] != b"e":
        k, i = _decode(data, i)
        if k == b"info":
            start = i
            _, i = _decode(data, i)  # 只为推进到 value 结束位置
            return data[start:i]
        _, i = _decode(data, i)
    raise TorrentError("种子文件里没有 info 字段")


def parse_torrent(data: bytes) -> Dict[str, Any]:
    """解析 .torrent，抽出建任务要用的少量字段。

    返回 {name, size, files, info_hash}。name 是 info.name（单文件为文件名，
    多文件为目录名）—— aria2 多文件模式不指定 out，产物就落在 <dir>/<name>/ 下。
    """
    if not data:
        raise TorrentError("种子文件是空的")
    try:
        top = _decode_top(data)
    except TorrentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TorrentError(f"不是有效的 bencode：{type(exc).__name__}") from exc
    if not isinstance(top, dict) or b"info" not in top:
        raise TorrentError("不是 BT 种子（缺少 info 字段）")
    info = top[b"info"]
    if not isinstance(info, dict):
        raise TorrentError("info 字段类型异常")

    def _s(k: bytes) -> str:
        v = info.get(k)
        return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""

    name = _s(b"name") or _s(b"name.utf-8") or "BT 下载"
    files = info.get(b"files")
    if isinstance(files, list):
        size = 0
        for f in files:
            if isinstance(f, dict) and isinstance(f.get(b"length"), int):
                size += f[b"length"]
    else:
        size = info.get(b"length") if isinstance(info.get(b"length"), int) else 0
    return {
        "name": name,
        "size": size,
        "files": len(files) if isinstance(files, list) else 1,
        "info_hash": hashlib.sha1(info_raw(data)).hexdigest(),
    }


# ---------------------------------------------------------------- metalink

def parse_metalink(data: bytes) -> List[Dict[str, Any]]:
    """解析 .meta4 / .metalink，按 <file> 返回 [{name, urls}]。

    Metalink 3 用无命名空间的 <files><file>，Metalink 4 (RFC 5854)
    用 xmlns 包着的 <file> —— 直接遍历所有层级的 file 标签省得分版本讨论。
    """
    try:
        root = ET.fromstring(data)
    except Exception as exc:  # noqa: BLE001
        raise TorrentError(f"不是有效的 Metalink XML：{type(exc).__name__}") from exc

    out: List[Dict[str, Any]] = []
    # findall(".//file") 在两种命名空间下都能命中（tag 带 xmlns 前缀时用通配）
    for f in root.iter():
        if not f.tag.endswith("file"):
            continue
        name = f.get("name") or ""
        urls: List[str] = []
        hashes: List[Dict[str, str]] = []
        for u in f.iter():
            if u.tag.endswith("url") and (u.text or "").strip():
                urls.append(u.text.strip())
            elif u.tag.endswith("hash") and (u.text or "").strip():
                hashes.append({"type": u.get("type") or "",
                               "value": u.text.strip()})
        if urls:
            out.append({"name": name, "urls": urls, "hashes": hashes})
    if not out:
        raise TorrentError("Metalink 里没有带 URL 的文件条目")
    return out


# ---------------------------------------------------------------- 链接文本

# 伸手抓的协议：磁力 / HTTP(S) / 迅雷 / 旋风 / 快车 / ed2k / FTP
_LINK_RE = re.compile(
    r"(?:magnet:\?xt=urn:btih:[A-Za-z0-9]+[^\s\"'<>]*"
    r"|https?://[^\s\"'<>]+"
    r"|thunder://[^\s\"'<>]+"
    r"|qqdl://[^\s\"'<>]+"
    r"|flashget://[^\s\"'<>]+"
    r"|ed2k://[^\s\"'<>]+"   # ed2k 链接本身就靠 | 分段，不能排除 |
    r"|ftp://[^\s\"'<>]+)",
    re.IGNORECASE,
)


def extract_links(text: str) -> List[str]:
    """从文本（.magnet/.url/.txt）里抓下载链接，去重保序。"""
    seen, out = set(), []
    for m in _LINK_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;，。；）)")
        if u.lower() not in seen:
            seen.add(u.lower())
            out.append(u)
    return out


# ---------------------------------------------------------------- 分流

_TORRENT_EXTS = (".torrent",)
_METALINK_EXTS = (".meta4", ".metalink")
_LINKTEXT_EXTS = (".magnet", ".url", ".txt")


def kind_for(filename: str) -> Optional[str]:
    """按文件名判断这是哪类种子/描述文件。

    返回 "torrent" / "metalink" / "links" / None。
    未知后缀不算错 —— server 层可以再按内容兜底（文件头 d4:info = bencode）。
    """
    low = (filename or "").strip().lower()
    if low.endswith(_TORRENT_EXTS):
        return "torrent"
    if low.endswith(_METALINK_EXTS):
        return "metalink"
    if low.endswith(_LINKTEXT_EXTS):
        return "links"
    return None


def sniff_kind(data: bytes, filename: str = "") -> str:
    """先看后缀，看不出来就闻内容。返回 kind_for 的取值，认不出抛 TorrentError。"""
    k = kind_for(filename)
    if k:
        return k
    head = (data or b"")[:200]
    if head.lstrip().startswith(b"d") and b"4:info" in head:
        return "torrent"
    if b"<metalink" in head.lower() or b"<files" in head.lower():
        return "metalink"
    raise TorrentError(
        "认不出这个文件 —— 支持 .torrent / .meta4 / .metalink / .magnet / .url / .txt")
