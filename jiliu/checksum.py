"""校验和：流式计算 + 与期望值比对 + 自动找同目录的校验文件。

为什么不能「读进内存再算」
    1 GB 文件读进内存就是 1 GB RSS，两三个并发就把机器顶爆。
    必须 1 MB 一块流式喂给 hashlib，内存占用与文件大小无关。

为什么还要去同目录找 .sha256
    IDM 只比对大小；AB DM 要手动粘哈希。而绝大多数开源发行版
    （GitHub Release、Apache、Ubuntu ISO）都会在文件旁边放一份
    `xxx.sha256` 或 `SHA256SUMS`，下完自动拉下来比对，用户什么都不用做。
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import threading
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

ALGOS = ("md5", "sha1", "sha256", "sha512")
# 每种算法的十六进制长度，用来判断"粘进来的这一串是不是这个算法"
HEX_LEN = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128}
# 同目录候选文件名，按可信度排序
SIDECAR_SUFFIX = (".sha256", ".sha256sum", ".sha1", ".md5", ".sha512",
                  ".checksum", ".hash", ".txt")
SUMS_FILES = ("SHA256SUMS", "SHA256SUMS.txt", "sha256sums.txt",
              "SHA512SUMS", "MD5SUMS", "checksums.txt")
FETCH_LIMIT = 512 * 1024     # 校验文件不该超过这个大小
_HEX_RE = re.compile(r"\b([0-9a-fA-F]{32,128})\b")


def algo_for_hex(value: str) -> str:
    """按长度猜算法。认不出来返回空。"""
    s = (value or "").strip()
    for algo, n in HEX_LEN.items():
        if len(s) == n and re.fullmatch(r"[0-9a-fA-F]+", s):
            return algo
    return ""


def normalize_expected(value: str) -> Tuple[str, str]:
    """把用户粘进来的东西解析成 (algo, hex)。

    支持这些写法：
        <64 hex>
        sha256:<64 hex>
        <64 hex>  jiliu.exe
        SHA256 (jiliu.exe) = <64 hex>
    """
    s = (value or "").strip()
    if not s:
        return "", ""
    m = re.match(r"^(md5|sha1|sha256|sha512)\s*[:=]\s*([0-9a-fA-F]+)$", s, re.I)
    if m:
        return m.group(1).lower(), m.group(2).lower()
    m = re.search(r"=\s*([0-9a-fA-F]+)\s*$", s)
    if m:
        hexv = m.group(1).lower()
        algo = algo_for_hex(hexv)
        if algo:
            return algo, hexv
    m = _HEX_RE.search(s)
    if m:
        hexv = m.group(1).lower()
        algo = algo_for_hex(hexv)
        if algo:
            return algo, hexv
    return "", ""


def compute(path: str | pathlib.Path, algo: str = "sha256", *,
            chunk: int = 1024 * 1024,
            progress: Optional[Callable[[int, int], None]] = None,
            cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    """流式计算。返回 {ok, algo, hex, bytes, elapsed, error}。"""
    algo = (algo or "sha256").lower()
    if algo not in ALGOS:
        return {"ok": False, "algo": algo, "hex": "", "bytes": 0,
                "elapsed": 0.0, "error": f"不支持的算法：{algo}"}
    p = pathlib.Path(path)
    if not p.is_file():
        return {"ok": False, "algo": algo, "hex": "", "bytes": 0,
                "elapsed": 0.0, "error": f"文件不存在：{p}"}

    import time
    t0 = time.monotonic()
    h = hashlib.new(algo)
    total = 0
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            while True:
                if cancel is not None and cancel.is_set():
                    return {"ok": False, "algo": algo, "hex": "", "bytes": total,
                            "elapsed": time.monotonic() - t0, "error": "已取消"}
                buf = f.read(chunk)
                if not buf:
                    break
                h.update(buf)
                total += len(buf)
                if progress:
                    try:
                        progress(total, size)
                    except Exception:
                        pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "algo": algo, "hex": "", "bytes": total,
                "elapsed": time.monotonic() - t0,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "algo": algo, "hex": h.hexdigest(), "bytes": total,
            "elapsed": time.monotonic() - t0, "error": ""}


def compute_all(path: str | pathlib.Path, *, algorithms: Optional[List[str]] = None,
                progress: Optional[Callable[[str, int, int], None]] = None,
                cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    """一次算出多种哈希（只读一遍盘）。"""
    algos = [a for a in (algorithms or ALGOS) if a in ALGOS]
    p = pathlib.Path(path)
    if not p.is_file():
        return {"ok": False, "error": f"文件不存在：{p}", "hashes": {}}
    pool = {a: hashlib.new(a) for a in algos}
    import time
    t0 = time.monotonic()
    total = 0
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            while True:
                if cancel is not None and cancel.is_set():
                    return {"ok": False, "error": "已取消", "hashes": {}}
                buf = f.read(1024 * 1024)
                if not buf:
                    break
                for h in pool.values():
                    h.update(buf)
                total += len(buf)
                if progress:
                    try:
                        progress("", total, size)
                    except Exception:
                        pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "hashes": {}}
    return {"ok": True, "error": "", "bytes": total,
            "elapsed": time.monotonic() - t0,
            "hashes": {a: h.hexdigest() for a, h in pool.items()}}


def compare(actual: str, expected: str) -> Dict[str, Any]:
    """比对。expected 可以是裸哈希，也可以带算法前缀或文件名。"""
    algo, want = normalize_expected(expected)
    got = (actual or "").strip().lower()
    if not want:
        return {"ok": False, "match": None, "error": "没认出期望值里的哈希",
                "algo": "", "expected": "", "actual": got}
    # 用户粘的算法与算出来的不一致（长度不同）时，按长度重判
    if len(want) != len(got):
        return {"ok": False, "match": False, "algo": algo, "expected": want,
                "actual": got,
                "error": f"长度不一致：期望 {len(want)} 位，实际 {len(got)} 位"}
    return {"ok": True, "match": want == got, "algo": algo, "expected": want,
            "actual": got, "error": ""}


# ----------------------------------------------------------------------
# 同目录校验文件
# ----------------------------------------------------------------------

def sidecar_candidates(url: str) -> List[str]:
    """给定文件 URL，列出同目录下可能存在的校验文件。"""
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return []
    if p.scheme not in ("http", "https"):
        return []
    path = p.path
    base_dir = path.rsplit("/", 1)[0] if "/" in path else ""
    name = path.rsplit("/", 1)[-1]
    out: List[str] = []
    if name:
        for suf in SIDECAR_SUFFIX:
            out.append(urllib.parse.urlunsplit(
                (p.scheme, p.netloc, f"{base_dir}/{name}{suf}", "", "")))
        # `xxx.zip.sha256` 之外还有一种：`xxx.zip.SHA256`
        out.append(urllib.parse.urlunsplit(
            (p.scheme, p.netloc, f"{base_dir}/{name}.SHA256", "", "")))
    for sums in SUMS_FILES:
        out.append(urllib.parse.urlunsplit(
            (p.scheme, p.netloc, f"{base_dir}/{sums}", "", "")))
    return out


def parse_sums(text: str, filename: str = "", strict: bool = False) -> List[Dict[str, str]]:
    """从 SHA256SUMS 这类文件里解析出 (algo, hex, name)。

    两种常见格式：
        <hex>  <name>
        SHA256 (<name>) = <hex>

    strict=True 且给了 filename 时，没匹配上就返回空列表 —— 不能回退成
    「全部条目」，否则调用方取 rows[0] 会拿同目录别的文件的哈希来比对，
    把正确的文件误报成校验不一致。
    """
    rows: List[Dict[str, str]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(md5|sha1|sha256|sha512)\s*\((.+?)\)\s*=\s*([0-9a-fA-F]+)$", line, re.I)
        if m:
            rows.append({"algo": m.group(1).lower(), "name": m.group(2).strip(),
                         "hex": m.group(3).lower()})
            continue
        parts = line.replace("*", " ").split()
        if len(parts) >= 2:
            hexv = parts[0].lower()
            algo = algo_for_hex(hexv)
            if algo:
                rows.append({"algo": algo, "name": " ".join(parts[1:]).strip(),
                             "hex": hexv})
            continue
        # 只有一串哈希、没有文件名的情况（`a.zip.sha256` 里最常见）
        if len(parts) == 1:
            hexv = parts[0].lower()
            algo = algo_for_hex(hexv)
            if algo:
                rows.append({"algo": algo, "name": "", "hex": hexv})
    if filename:
        want = filename.lower()
        exact = [r for r in rows if r["name"].lower() == want]
        if exact:
            return exact
        tail = [r for r in rows if r["name"].lower().endswith("/" + want)]
        if tail:
            return tail
        if strict:
            return []
    return rows


def fetch_sidecar(url: str, cfg: Dict[str, Any], filename: str = "",
                  timeout: float = 8.0) -> Dict[str, Any]:
    """去同目录找校验文件并解析出这条文件的期望哈希。

    返回 {ok, algo, hex, source, tried, error}
    """
    from . import proxy
    import urllib.request

    tried: List[str] = []
    for cand in sidecar_candidates(url):
        tried.append(cand)
        try:
            op = proxy.opener(cfg, cand, timeout=timeout)
            req = urllib.request.Request(cand, headers={
                "User-Agent": "Jiliu/1.0", "Range": f"bytes=0-{FETCH_LIMIT - 1}"})
            with op.open(req, timeout=timeout) as r:
                raw = r.read(FETCH_LIMIT)
        except Exception:
            continue
        text = raw.decode("utf-8", "replace")
        rows = parse_sums(text, filename, strict=True)
        if not rows:
            # 单文件校验：内容本身就是一串哈希（必须只有一行有效内容，
            # 否则它是别文件的 SUMS，不能拿来当本文件的期望值）
            body = [ln.strip() for ln in text.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
            if len(body) == 1:
                algo, hexv = normalize_expected(body[0])
                if hexv:
                    return {"ok": True, "algo": algo, "hex": hexv, "source": cand,
                            "tried": tried, "error": ""}
            continue
        row = rows[0]
        return {"ok": True, "algo": row["algo"], "hex": row["hex"],
                "source": cand, "tried": tried, "error": ""}
    return {"ok": False, "algo": "", "hex": "", "source": "",
            "tried": tried, "error": "同目录没有找到校验文件"}


def sidecar_path(dest: str | pathlib.Path) -> Optional[pathlib.Path]:
    """本地已下载的文件旁边有没有校验文件（离线场景）。"""
    p = pathlib.Path(dest)
    for suf in (".sha256", ".sha256sum", ".sha1", ".md5", ".sha512"):
        q = pathlib.Path(str(p) + suf)
        if q.is_file():
            return q
    for name in SUMS_FILES:
        q = p.parent / name
        if q.is_file():
            return q
    return None


def verify_local(dest: str | pathlib.Path) -> Dict[str, Any]:
    """用本地旁的校验文件验证（不联网）。没有校验文件就返回 skipped。"""
    p = pathlib.Path(dest)
    side = sidecar_path(p)
    if side is None:
        return {"ok": False, "skipped": True, "error": "没有本地校验文件"}
    try:
        text = side.read_text(encoding="utf-8", errors="replace")[:FETCH_LIMIT]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "skipped": False, "error": f"读校验文件失败：{exc}"}
    rows = parse_sums(text, p.name, strict=True)
    if not rows:
        body = [ln.strip() for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        algo, hexv = ("", "")
        if len(body) == 1:
            algo, hexv = normalize_expected(body[0])
        if not hexv:
            return {"ok": False, "skipped": False,
                    "error": f"校验文件里没有 {p.name} 的记录"}
        rows = [{"algo": algo, "name": p.name, "hex": hexv}]
    row = rows[0]
    res = compute(p, row["algo"])
    if not res["ok"]:
        return {"ok": False, "skipped": False, "error": res["error"]}
    match = res["hex"] == row["hex"]
    return {"ok": True, "skipped": False, "match": match, "algo": row["algo"],
            "expected": row["hex"], "actual": res["hex"], "source": str(side),
            "error": "" if match else "与本地校验文件不一致"}
