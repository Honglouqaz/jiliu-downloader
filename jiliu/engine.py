"""多线程分片下载核心。

设计要点（对照 IDM 的做法）：

1. **HTTP Range 分片**
   探测 `Content-Length` 与 `Accept-Ranges`，把 [0, size-1] 切成若干区间，
   每个工作线程独立开一条连接、独立 Range 请求、独立文件句柄 + seek 写入。

2. **动态分片（IDM 的核心特征）**
   不预先固定"每线程一段"，而是维护一个**待办区间池**。
   谁空下来就从**当前最大的区间**里领一块（默认 512KB~8MB），
   于是慢线程不会拖住快线程，剩余区间会被自动越切越细。
   这就是 IDM 那种"连接数动态伸缩"的效果。

3. **断点续传**
   用 sidecar 文件 `<目标文件>.jiliu.json` 记录**尚未完成的区间**。
   注意：领取中的块也记在里面 —— 否则进程崩溃会在文件里留下空洞。
   续传时把 sidecar 里的区间直接灌回区间池。

4. **暂存 + 组装（学 IDM 的 TempPath）**
   分片先写 `<目标文件>.part`，不碰最终文件；下完、校验通过再原子改名。
   `temp_dir` 可以把暂存区放到另一块盘（IDM 的 TempPath 就是这么用的）：
   终态搬运只在最后一刻发生，取消/崩溃时目标目录里不会留下"看起来完整"
   的半成品，也不会因为目标文件被杀软/播放器占用而整任务失败。

5. **全局限速**
   令牌桶，所有线程共享。

6. **可暂停 / 可取消**
   threading.Event 控制；取消时把未写完的区间归还池子并落盘。
   `.part` 保留，下次续传接着写。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

READ_SIZE = 256 * 1024
STATE_SUFFIX = ".jiliu.json"
# 暂存文件后缀。最终文件在下载完成并通过校验前不存在
PART_SUFFIX = ".part"
# 空间预检留的余量：元数据、文件系统块对齐、以及"刚好差一点"的尴尬
SPACE_MARGIN = 16 * 1024 * 1024


class DownloadError(Exception):
    """不可恢复的下载错误。"""


class _Cancelled(Exception):
    """内部：收到取消信号。"""


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def human_size(n: Optional[float]) -> str:
    if n is None:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.2f} {u}" if u != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v:.2f} TB"


def human_speed(n: float) -> str:
    return human_size(n) + "/s"


def sha256_file(path: pathlib.Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _filename_from_disposition(value: str) -> str:
    """从 Content-Disposition 解析文件名，优先 RFC 5987 的 filename*。"""
    if not value:
        return ""
    m = re.search(r"filename\*\s*=\s*([^;]+)", value, re.I)
    if m:
        raw = m.group(1).strip().strip('"')
        if "''" in raw:
            raw = raw.split("''", 1)[1]
        return urllib.parse.unquote(raw)
    m = re.search(r'filename\s*=\s*"([^"]+)"', value, re.I)
    if m:
        return m.group(1)
    m = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    if m:
        return m.group(1).strip()
    return ""


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip().strip(".")
    return name or "download.bin"


def unique_path(dest: pathlib.Path) -> pathlib.Path:
    """目标已存在时自动加 (1)、(2) 后缀。"""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(1, 1000):
        cand = dest.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    return dest


def _same_dir(a: pathlib.Path, b: pathlib.Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(a))) == \
            os.path.normcase(os.path.abspath(str(b)))
    except Exception:  # noqa: BLE001
        return False


def part_path(dest: pathlib.Path, temp_dir: str = "") -> pathlib.Path:
    """暂存文件路径。

    temp_dir 为空 = 与目标同目录（默认，零搬运）。
    配了别的盘就把分片写到那儿；跨目录时文件名带目标路径的短哈希，
    避免不同目录下的同名文件在共享临时区里互相覆盖。
    """
    dest = pathlib.Path(dest)
    raw = str(temp_dir or "").strip()
    if not raw:
        return dest.with_name(dest.name + PART_SUFFIX)
    root = pathlib.Path(os.path.expandvars(os.path.expanduser(raw)))
    if _same_dir(root, dest.parent):
        return dest.with_name(dest.name + PART_SUFFIX)
    tag = hashlib.sha1(str(dest.absolute()).encode("utf-8", "ignore")).hexdigest()[:8]
    return root / f"{dest.name}.{tag}{PART_SUFFIX}"


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------

@dataclass
class ProbeResult:
    url: str
    final_url: str = ""
    size: Optional[int] = None
    accept_ranges: bool = False
    filename: str = ""
    content_type: str = ""
    content_disposition: str = ""
    magic: bytes = b""              # 前 1KB 原始字节，给 resource.sniff 用
    etag: str = ""
    last_modified: str = ""
    status: int = 0
    error: str = ""
    plan: Dict[str, Any] = field(default_factory=dict)   # resource.classify 的结果

    def to_dict(self) -> Dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "magic"}
        d["size_text"] = human_size(self.size)
        d["magic_len"] = len(self.magic)
        d["magic_hex"] = self.magic[:16].hex()
        return d


def _base_headers(cfg: Dict[str, Any], extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {"User-Agent": cfg.get("user_agent", "Jiliu/1.0")}
    if cfg.get("referer"):
        h["Referer"] = cfg["referer"]
    if extra:
        h.update({k: v for k, v in extra.items() if v})
    return h


def probe(url: str, cfg: Dict[str, Any], timeout: Optional[int] = None) -> ProbeResult:
    """探测资源信息。先 HEAD，不行再用 `Range: bytes=0-0` 的 GET。"""
    timeout = timeout or int(cfg.get("timeout", 30))
    res = ProbeResult(url=url, final_url=url)
    headers = _base_headers(cfg)

    def absorb(h) -> None:
        cl = h.get("Content-Length")
        if cl and cl.isdigit():
            res.size = int(cl)
        if (h.get("Accept-Ranges") or "").lower() == "bytes":
            res.accept_ranges = True
        res.etag = h.get("ETag") or res.etag
        res.last_modified = h.get("Last-Modified") or res.last_modified
        res.content_type = h.get("Content-Type") or res.content_type
        cd = h.get("Content-Disposition") or ""
        if cd:
            res.content_disposition = cd
        fn = _filename_from_disposition(cd)
        if fn:
            res.filename = fn

    # 1) HEAD
    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res.final_url = r.geturl()
            res.status = getattr(r, "status", r.getcode())
            absorb(r.headers)
    except urllib.error.HTTPError as exc:
        res.status = exc.code
        res.error = f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"

    # 2) GET + Range 0-1023
    # 为什么不是 bytes=0-0：顺带把前 1KB 抓回来做文件头嗅探，
    # 这样「服务端把 .exe 说成 .bin」这类谎话能被识破。
    if res.size is None or not res.accept_ranges or not res.magic:
        try:
            h2 = dict(headers)
            h2["Range"] = "bytes=0-1023"
            req = urllib.request.Request(url, headers=h2)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res.final_url = r.geturl()
                code = getattr(r, "status", r.getcode())
                res.status = code
                absorb(r.headers)
                cr = r.headers.get("Content-Range") or ""
                # 形如 bytes 0-1023/123456。
                # 必须在 absorb 之后处理：分段响应的 Content-Length 是 1024
                # （只是这一段有多长），拿它当总长度会把文件下成 1KB。
                if "/" in cr:
                    total = cr.rsplit("/", 1)[-1].strip()
                    if total.isdigit():
                        res.size = int(total)
                        res.accept_ranges = True
                # 只读响应自己声明的那点长度，别多读 ——
                # keep-alive 连接上多读会一直等下一批字节，直到超时。
                want = 1024
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit():
                    want = min(want, int(cl))
                try:
                    res.magic = r.read(want) if want > 0 else b""
                except Exception:
                    pass
                res.error = ""
        except urllib.error.HTTPError as exc:
            res.status = res.status or exc.code
            res.error = f"HTTP {exc.code} {exc.reason}"
        except Exception as exc:  # noqa: BLE001
            res.error = res.error or f"{type(exc).__name__}: {exc}"

    if not res.filename:
        path = urllib.parse.urlparse(res.final_url or url).path
        res.filename = _safe_name(urllib.parse.unquote(os.path.basename(path)) or "download.bin")
    else:
        res.filename = _safe_name(res.filename)

    # 顺手做一次资源识别：四条线索这一趟全拿到了，不识别白不识别。
    # 懒导入是为了避免 engine <-> resource 的循环依赖。
    try:
        from . import resource as _res
        plan = _res.classify_probe(res)
        res.plan = plan.to_dict()
        res.filename = _res.suggest_filename(plan, res.filename)
    except Exception:  # noqa: BLE001
        pass

    return res


# --------------------------------------------------------------------------
# 限速
# --------------------------------------------------------------------------

class RateLimiter:
    """令牌桶，线程安全。所有工作线程共享，实现全局限速。"""

    def __init__(self, bytes_per_sec: float):
        self.rate = float(bytes_per_sec)
        self.allowance = float(bytes_per_sec)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def set_rate(self, bytes_per_sec: float) -> None:
        """运行中改限速（时段限速 / 单任务限速用）。传 0 表示取消限速。"""
        with self.lock:
            self.rate = float(bytes_per_sec or 0)
            self.allowance = float(self.rate)
            self.last = time.monotonic()

    def acquire(self, n: int) -> None:
        if self.rate <= 0:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                self.allowance = min(self.rate, self.allowance + (now - self.last) * self.rate)
                self.last = now
                if self.allowance >= n:
                    self.allowance -= n
                    return
                need = (n - self.allowance) / self.rate
            time.sleep(min(max(need, 0.005), 0.2))


# --------------------------------------------------------------------------
# 主下载器
# --------------------------------------------------------------------------

class RangeDownloader:
    """把 url 下载到 dest，多线程分片、可续传、可暂停取消。"""

    engine = "native"

    def __init__(
        self,
        url: str,
        dest: pathlib.Path,
        *,
        cfg: Optional[Dict[str, Any]] = None,
        threads: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
        speed_limit: Optional[int] = None,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        verify_size: bool = True,
        chunk_size: Optional[int] = None,
        verify_hash: bool = False,
        expected_sha256: str = "",
    ):
        from . import config as _config

        self.cfg = cfg or _config.load()
        self.url = url
        self.dest = pathlib.Path(dest)
        self.state_path = pathlib.Path(str(self.dest) + STATE_SUFFIX)
        self.work_path = part_path(self.dest, str(self.cfg.get("temp_dir") or ""))
        # 旧版本直接把分片写进最终文件。遇到这种遗留状态时沿用原文件续传，
        # 不再搬一次 —— 迁移只做一次，新任务一律走 .part。
        self._legacy_inplace = False
        self.headers = headers or {}
        self.threads = max(1, int(threads if threads is not None else self.cfg.get("threads", 8)))
        self.timeout = int(self.cfg.get("timeout", 30))
        self.max_retries = int(self.cfg.get("max_retries", 5))
        self.verify_size = verify_size
        self.progress_cb = progress_cb
        self.log_cb = log_cb

        # 资源类型给出的分片建议（见 resource.py）。None = 按文件大小自适应。
        self.chunk_size = int(chunk_size) if chunk_size else None
        # 装包/压缩包这类下坏了代价高的，下完对一次哈希
        self.verify_hash = bool(verify_hash)
        self.expected_sha256 = (expected_sha256 or "").strip().lower()
        self.actual_sha256 = ""
        self.hash_ok: Optional[bool] = None
        # resource.classify 的识别结果（probe 时填上）
        self.plan: Dict[str, Any] = {}

        limit = speed_limit if speed_limit is not None else int(self.cfg.get("speed_limit", 0) or 0)
        self.limiter = RateLimiter(limit) if limit and limit > 0 else None

        self.size: Optional[int] = None
        self.accept_ranges = False
        self.filename = self.dest.name

        # 待办 / 在途 区间池，元素为 (start, end) 闭区间
        self._pending: List[Tuple[int, int]] = []
        self._inflight: Dict[Tuple[int, int], int] = {}
        self._lock = threading.RLock()
        # 累计切出过多少个分片 —— 用来度量动态分片的强度
        self._segments_created = 0

        self._paused = threading.Event()
        self._cancelled = threading.Event()

        self._samples: List[Tuple[float, float]] = []
        self._started_at = 0.0
        self._finished_at = 0.0
        self._error: Optional[str] = None
        self._resumed_bytes = 0
        # 区间池是否已初始化。
        # 没有这个标志，在「探测到 size」到「区间池填好」之间的窗口里，
        # done_bytes() 会算出 size - 0 = 满进度 —— 界面上就是"瞬间跳到 100%"。
        self._initialized = False

        self._threads: List[threading.Thread] = []

    # ---------------- 生命周期 ----------------

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self) -> None:
        self._cancelled.set()
        self._paused.clear()

    def set_speed_limit(self, bytes_per_sec: int) -> None:
        """运行中调整本任务限速。0 = 取消限速。"""
        self.speed_limit = max(0, int(bytes_per_sec or 0))
        if self.limiter is None:
            if self.speed_limit > 0:
                self.limiter = RateLimiter(self.speed_limit)
            return
        self.limiter.set_rate(self.speed_limit)

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    # ---------------- 进度 ----------------

    def done_bytes(self) -> int:
        if self.size is None:
            # 未知长度：只能按已写入字节算
            with self._lock:
                written = sum(self._inflight.values())
            return self._resumed_bytes + written
        if not self._initialized:
            # 区间池还没建好，不能按"剩余区间"反推进度
            return self._resumed_bytes
        with self._lock:
            pend = sum(e - s + 1 for s, e in self._pending)
            infl = sum(e - s + 1 for (s, e) in self._inflight)
            written = sum(self._inflight.values())
        return max(0, self.size - pend - infl + written)

    def snapshot(self) -> Dict[str, Any]:
        done = self.done_bytes()
        total = self.size
        elapsed = (self._finished_at or time.monotonic()) - self._started_at if self._started_at else 0
        speed = 0.0
        if len(self._samples) >= 2:
            (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
            if t1 > t0:
                speed = (b1 - b0) / (t1 - t0)
        elif elapsed > 0:
            speed = done / elapsed

        eta = None
        if total and speed > 0 and done < total:
            eta = (total - done) / speed

        with self._lock:
            seg_pending = len(self._pending)
            seg_inflight = len(self._inflight)
            seg_total = seg_pending + seg_inflight

        return {
            "url": self.url,
            "dest": str(self.dest),
            "work_path": str(self.work_path),
            "filename": self.filename,
            "size": total,
            "size_text": human_size(total),
            "done": done,
            "done_text": human_size(done),
            "percent": round(done * 100.0 / total, 2) if total else 0.0,
            "speed": speed,
            "speed_text": human_speed(speed),
            "eta": eta,
            "eta_text": _fmt_eta(eta),
            "elapsed": elapsed,
            "threads": self.threads,
            "segments_pending": seg_pending,
            "segments_inflight": seg_inflight,
            "segments_total": seg_total,
            "segments_created": self._segments_created,
            "accept_ranges": self.accept_ranges,
            "paused": self.paused,
            "cancelled": self.cancelled,
            "plan": self.plan,
            "category": self.plan.get("category", ""),
            "sha256": self.actual_sha256,
            "hash_ok": self.hash_ok,
            "error": self._error,
        }

    def _emit(self) -> None:
        if self.progress_cb:
            try:
                self.progress_cb(self.snapshot())
            except Exception:
                pass

    def _log(self, msg: str) -> None:
        if self.log_cb:
            try:
                self.log_cb(msg)
            except Exception:
                pass

    # ---------------- 暂存 / 组装 ----------------

    def _check_space(self, size: int) -> None:
        """开下之前的磁盘预检。

        IDM 在临时目录满时才报错，用户已经等了半小时。这里提前算，
        并把「把临时目录指到别的盘」写进提示里。
        """
        need = int(size * 1.05) + SPACE_MARGIN
        try:
            free = shutil.disk_usage(str(self.work_path.parent)).free
        except Exception:  # noqa: BLE001
            return
        if free < need:
            raise DownloadError(
                f"磁盘空间不足：需要约 {human_size(need)}，"
                f"{self.work_path.parent} 只剩 {human_size(free)}。"
                "可以在设置里把「临时目录」指到空间更大的盘。"
            )

    def _promote(self) -> None:
        """把暂存文件搬到最终位置。

        同盘走 os.replace（原子，不产生中间态）；跨盘 Windows 会抛
        OSError，退化成 copy + 删除。搬运前最终文件始终不存在，
        所以不会出现"目标已存在但内容不完整"。
        """
        if self._legacy_inplace or self.work_path == self.dest:
            return
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(self.work_path, self.dest)
        except OSError:
            shutil.move(str(self.work_path), str(self.dest))
        self.work_path = self.dest

    # ---------------- 状态文件 ----------------

    def _save_state(self) -> None:
        with self._lock:
            remaining = sorted(self._pending + list(self._inflight.keys()), key=lambda x: x[0])
        data = {
            "url": self.url,
            "size": self.size,
            "etag": getattr(self, "_etag", ""),
            "last_modified": getattr(self, "_last_modified", ""),
            "remaining": [[s, e] for s, e in remaining],
            "work": str(self.work_path),
            "updated": time.time(),
        }
        try:
            tmp = pathlib.Path(str(self.state_path) + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception as exc:  # noqa: BLE001
            self._log(f"状态写入失败: {exc}")

    def _load_state(self, probe_res: ProbeResult) -> bool:
        """尝试读取 sidecar，成功则把区间灌回池子并返回 True。"""
        if not self.state_path.exists():
            return False
        if not self.work_path.exists():
            # 老任务：分片写的就是最终文件本身
            if self.dest.exists():
                self.work_path = self.dest
                self._legacy_inplace = True
            else:
                return False
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return False

        if data.get("url") != self.url:
            self._log("sidecar 的 URL 不匹配，忽略并重新开始")
            return False
        if data.get("size") != probe_res.size:
            self._log("sidecar 的文件大小已变（服务器内容更新），重新开始")
            return False
        if probe_res.etag and data.get("etag") and data["etag"] != probe_res.etag:
            self._log("ETag 已变，重新开始")
            return False

        rem = [tuple(x) for x in data.get("remaining", []) if len(x) == 2 and x[0] <= x[1]]
        if not rem:
            return False

        self._pending = [(int(s), int(e)) for s, e in rem]
        self._resumed_bytes = probe_res.size - sum(e - s + 1 for s, e in self._pending)
        self._initialized = True
        self._log(
            f"断点续传：已存在 {human_size(self._resumed_bytes)}"
            f"，剩余 {human_size(sum(e - s + 1 for s, e in self._pending))}"
        )
        return True

    def _clear_state(self) -> None:
        try:
            if self.state_path.exists():
                self.state_path.unlink()
        except Exception:
            pass

    # ---------------- 区间池 ----------------

    def _init_ranges(self, size: int, threads: int) -> None:
        """初始化区间池。

        小文件直接一整段；大文件先按线程数均分，
        之后由 _claim 的动态切分继续细分。
        """
        chunk_min = int(self.chunk_size or self.cfg.get("chunk_min", 512 * 1024))
        if size <= max(int(self.cfg.get("min_multipart", 1024 * 1024)), chunk_min * 2):
            self._pending = [(0, size - 1)]
            self._initialized = True
            return

        per = max(chunk_min, size // max(1, threads))
        ranges: List[Tuple[int, int]] = []
        pos = 0
        while pos < size:
            end = min(pos + per - 1, size - 1)
            ranges.append((pos, end))
            pos = end + 1
        self._pending = ranges
        self._initialized = True

    def _claim(self) -> Optional[Tuple[int, int]]:
        """从最大的待办区间里领一块。这就是动态分片。"""
        chunk_min = int(self.cfg.get("chunk_min", 512 * 1024))
        chunk_max = int(self.cfg.get("chunk_max", 8 * 1024 * 1024))
        size = self.size or 0
        if self.chunk_size:
            # 资源类型已经给了建议分片，就别再自作主张
            # （镜像 4MB、字体 256KB 这种差异是有道理的）
            chunk = max(chunk_min, min(chunk_max, self.chunk_size))
        else:
            # 块大小随文件大小在线调整：文件越大块越大，避免元数据开销
            chunk = max(chunk_min, min(chunk_max, size // max(1, self.threads * 8) or chunk_min))

        with self._lock:
            if not self._pending:
                return None
            idx = max(range(len(self._pending)), key=lambda i: self._pending[i][1] - self._pending[i][0])
            s, e = self._pending[idx]
            if self.accept_ranges:
                take = min(chunk, e - s + 1)
            else:
                # 服务器不支持 Range：整段一口气拿。再按 chunk 切的话，
                # 每一小块都要从 0 重下再丢弃前缀 —— 流量直接 O(n²)。
                take = e - s + 1
            seg = (s, s + take - 1)
            rest = (s + take, e)
            self._pending.pop(idx)
            if rest[0] <= rest[1]:
                self._pending.append(rest)
            self._inflight[seg] = 0
            self._segments_created += 1
        return seg

    def _complete(self, seg: Tuple[int, int]) -> None:
        with self._lock:
            self._inflight.pop(seg, None)

    def _release(self, seg: Tuple[int, int]) -> None:
        with self._lock:
            self._inflight.pop(seg, None)
            if seg[0] <= seg[1]:
                self._pending.append(seg)

    def _note_written(self, seg: Tuple[int, int], n: int) -> None:
        with self._lock:
            if seg in self._inflight:
                self._inflight[seg] += n

    # ---------------- 实际抓取 ----------------

    def _open(self, headers: Dict[str, str]):
        req = urllib.request.Request(self.url, headers=headers)
        return urllib.request.urlopen(req, timeout=self.timeout)

    def _fetch_range(self, fh, seg: Tuple[int, int]) -> int:
        """把 seg 区间写进 fh。返回实际完成到的下一个位置（e+1 表示写满）。

        区间内自带重试：失败后从 pos 继续，不会重下已经写好的部分。
        """
        s, e = seg
        pos = s
        attempt = 0

        while pos <= e:
            if self._cancelled.is_set():
                raise _Cancelled()
            while self._paused.is_set():
                if self._cancelled.is_set():
                    raise _Cancelled()
                time.sleep(0.1)

            headers = _base_headers(self.cfg, self.headers)
            headers["Range"] = f"bytes={pos}-{e}"

            try:
                with self._open(headers) as resp:
                    status = getattr(resp, "status", resp.getcode())
                    if status not in (200, 206):
                        raise DownloadError(f"HTTP {status}")
                    if status == 200 and pos > 0:
                        # 服务器不支持 Range：body 一律从 0 开始。
                        # 不管是段内重试（pos>s）还是分片本身起点在后面
                        # （s>0 且 pos==s），都得先把 pos 字节丢掉再写，
                        # 否则就把文件开头的内容错位写进中段（T9 的坑）。
                        skip = pos
                        while skip > 0:
                            b = resp.read(min(READ_SIZE, skip))
                            if not b:
                                break
                            skip -= len(b)
                        if skip > 0:
                            raise DownloadError("服务器未返回请求的区间")
                    if status == 200 and pos > 0:
                        self.accept_ranges = False

                    fh.seek(pos, os.SEEK_SET)
                    while pos <= e:
                        if self._cancelled.is_set():
                            raise _Cancelled()
                        buf = resp.read(min(READ_SIZE, e - pos + 1))
                        if not buf:
                            break
                        if self.limiter is not None:
                            self.limiter.acquire(len(buf))
                        fh.write(buf)
                        pos += len(buf)
                        self._note_written(seg, len(buf))
                    return pos
            except _Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt > self.max_retries:
                    raise DownloadError(f"分片 {seg} 重试 {self.max_retries} 次仍失败: {exc}") from exc
                self._log(f"分片 {seg} 第 {attempt} 次失败（{exc}），{min(2 ** attempt, 8)}s 后从 {pos} 续传")
                time.sleep(min(2 ** attempt, 8))

        return pos

    def _worker(self, wid: int) -> None:
        fh = None
        try:
            fh = open(self.work_path, "rb+", buffering=0)
            while not self._cancelled.is_set():
                while self._paused.is_set():
                    if self._cancelled.is_set():
                        return
                    time.sleep(0.1)
                seg = self._claim()
                if seg is None:
                    return
                pos = seg[0]
                try:
                    pos = self._fetch_range(fh, seg)
                    self._complete(seg)
                except _Cancelled:
                    if pos <= seg[1]:
                        self._release((pos, seg[1]))
                    return
                except Exception as exc:  # noqa: BLE001
                    if pos <= seg[1]:
                        self._release((pos, seg[1]))
                    self._error = str(exc)
                    self._log(f"线程 {wid} 终止: {exc}")
                    return
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            self._log(f"线程 {wid} 打开文件失败: {exc}")
        finally:
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass

    def _monitor(self, stop: threading.Event) -> None:
        while not stop.wait(0.2):
            self._samples.append((time.monotonic(), self.done_bytes()))
            if len(self._samples) > 12:
                self._samples.pop(0)
            self._emit()

    # ---------------- 未知长度 ----------------

    def _download_unknown(self) -> None:
        """服务器不给 Content-Length：只能顺序写，不支持续传。"""
        headers = _base_headers(self.cfg, self.headers)
        self.work_path.parent.mkdir(parents=True, exist_ok=True)
        with self._open(headers) as resp, open(self.work_path, "wb", buffering=0) as fh:
            while True:
                if self._cancelled.is_set():
                    break
                while self._paused.is_set():
                    if self._cancelled.is_set():
                        break
                    time.sleep(0.1)
                buf = resp.read(READ_SIZE)
                if not buf:
                    break
                if self.limiter is not None:
                    self.limiter.acquire(len(buf))
                fh.write(buf)
                self._resumed_bytes += len(buf)

    # ---------------- 入口 ----------------

    def run(self) -> Dict[str, Any]:
        self._started_at = time.monotonic()
        try:
            return self._run_inner()
        except _Cancelled:
            self._finished_at = time.monotonic()
            self._save_state()
            return {
                "ok": False, "path": str(self.dest), "bytes": self.done_bytes(),
                "seconds": self._finished_at - self._started_at,
                "cancelled": True, "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            self._finished_at = time.monotonic()
            self._error = f"{type(exc).__name__}: {exc}"
            self._save_state()
            self._emit()
            return {
                "ok": False, "path": str(self.dest), "bytes": self.done_bytes(),
                "seconds": self._finished_at - self._started_at,
                "cancelled": False, "error": self._error,
            }

    def _run_inner(self) -> Dict[str, Any]:
        pres = probe(self.url, self.cfg, self.timeout)
        self.size = pres.size
        self.accept_ranges = pres.accept_ranges
        self.filename = pres.filename
        self._etag = pres.etag
        self._last_modified = pres.last_modified
        self.plan = pres.plan or {}
        if self.plan:
            kind = self.plan.get("label") or ""
            sub = self.plan.get("filetype") or ""
            if kind:
                self._log(f"识别为{kind}" + (f"（{sub}）" if sub else "")
                          + f"，并发 {self.threads}"
                          + (f"，分片 {human_size(self.chunk_size)}" if self.chunk_size else ""))

        # status 可能为 None（file:// 等非 HTTP 源探测不到状态码），
        # 直接比较会 TypeError —— 当成「非错误状态」走下面的流程
        if (pres.status or 0) >= 400:
            raise DownloadError(pres.error or f"HTTP {pres.status}")

        self.dest.parent.mkdir(parents=True, exist_ok=True)
        self.work_path.parent.mkdir(parents=True, exist_ok=True)

        # 未知长度
        if self.size is None:
            self._log("服务器未返回 Content-Length，改用单线程流式下载（不支持续传）")
            self.threads = 1
            self._download_unknown()
            self._finished_at = time.monotonic()
            got = self.work_path.stat().st_size if self.work_path.exists() else 0
            ok = got > 0 and not self.cancelled
            if ok:
                self._promote()
            self._emit()
            return {
                "ok": ok,
                "path": str(self.dest),
                "bytes": got,
                "seconds": self._finished_at - self._started_at,
                "cancelled": self.cancelled,
                "error": self._error,
            }

        resumed = self._load_state(pres)

        if not resumed:
            # 全新开始：先算空间，再把暂存文件撑到完整大小
            self._check_space(self.size)
            with open(self.work_path, "wb") as f:
                f.truncate(self.size)
            self._init_ranges(self.size, self.threads)

        if not self.accept_ranges:
            self._log("服务器不支持 Range，退化为单线程整段下载")
            self.threads = 1
            with self._lock:
                if self._pending:
                    lo = min(s for s, _ in self._pending)
                    hi = max(e for _, e in self._pending)
                    self._pending = [(lo, hi)]

        self._save_state()

        stop_mon = threading.Event()
        mon = threading.Thread(target=self._monitor, args=(stop_mon,), daemon=True, name="jiliu-monitor")
        mon.start()

        try:
            self._threads = []
            for i in range(self.threads):
                t = threading.Thread(target=self._worker, args=(i,), daemon=True, name=f"jiliu-w{i}")
                t.start()
                self._threads.append(t)
            for t in self._threads:
                t.join()
        finally:
            stop_mon.set()
            self._finished_at = time.monotonic()

        # 收尾
        with self._lock:
            outstanding = len(self._pending) + len(self._inflight)

        if self._cancelled.is_set() or outstanding > 0:
            self._save_state()
            self._emit()
            return {
                "ok": False,
                "path": str(self.dest),
                "bytes": self.done_bytes(),
                "seconds": self._finished_at - self._started_at,
                "cancelled": self.cancelled,
                "error": self._error,
                "outstanding_segments": outstanding,
                "resumed_bytes": self._resumed_bytes,
            }

        # 完整性校验：校验的是暂存文件，确认无误才让它变成最终文件
        actual = self.work_path.stat().st_size
        if self.verify_size and actual != self.size:
            self._error = f"文件大小不符：期望 {self.size}，实际 {actual}"
            self._emit()
            return {
                "ok": False, "path": str(self.dest), "bytes": actual,
                "seconds": self._finished_at - self._started_at,
                "cancelled": False, "error": self._error,
            }

        # 哈希校验：安装包 / 压缩包 / 镜像这类，下坏了代价高，
        # 但对哈希需要再读一遍全文件 —— 所以只在 verify_hash 打开时做。
        if self.verify_hash:
            try:
                self.actual_sha256 = sha256_file(self.work_path)
                if self.expected_sha256:
                    self.hash_ok = self.actual_sha256 == self.expected_sha256
                    if not self.hash_ok:
                        self._error = (
                            f"SHA256 不匹配：期望 {self.expected_sha256[:16]}…，"
                            f"实际 {self.actual_sha256[:16]}…"
                        )
                        self._log("哈希校验未通过，" + self._error)
                        try:
                            # 内容已确认是错的：留着 .part 只会让下一次
                            # 重试从坏数据上续传（curl -C - 尤其明显）
                            self.work_path.unlink(missing_ok=True)
                        except Exception:  # noqa: BLE001
                            pass
                        self._clear_state()
                        self._emit()
                        return {
                            "ok": False, "path": str(self.dest), "bytes": actual,
                            "seconds": self._finished_at - self._started_at,
                            "cancelled": False, "error": self._error,
                            "sha256": self.actual_sha256, "hash_ok": False,
                        }
                    self._log(f"哈希校验通过：{self.actual_sha256[:16]}…")
                else:
                    self._log(f"SHA256（服务端未提供预期值）：{self.actual_sha256}")
            except Exception as exc:  # noqa: BLE001
                self._log(f"哈希计算失败（不影响文件）：{exc}")

        # 组装：暂存 → 最终位置。到这一步才第一次出现目标文件
        self._promote()
        self._clear_state()

        self._emit()
        return {
            "ok": True,
            "path": str(self.dest),
            "bytes": actual,
            "seconds": self._finished_at - self._started_at,
            "cancelled": False,
            "error": None,
            "resumed_bytes": self._resumed_bytes,
            "sha256": self.actual_sha256,
            "hash_ok": self.hash_ok,
        }


def _fmt_eta(sec: Optional[float]) -> str:
    if sec is None or sec != sec or sec <= 0:
        return "-"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile
    from . import config as _config

    if len(sys.argv) < 3:
        print("用法: python -m jiliu.engine <url> <输出文件> [线程数]")
        raise SystemExit(2)

    _url, _out = sys.argv[1], pathlib.Path(sys.argv[2])
    _n = int(sys.argv[3]) if len(sys.argv) > 3 else 8

    def _on_progress(s: Dict[str, Any]) -> None:
        print(
            f"\r  {s['percent']:6.2f}%  {s['done_text']:>12}/{s['size_text']:<12}"
            f"  {s['speed_text']:>14}  ETA {s['eta_text']:<8}  分片 {s['segments_pending']}+{s['segments_inflight']}",
            end="",
            flush=True,
        )

    _cfg = _config.load()
    _dl = RangeDownloader(_url, _out, cfg=_cfg, threads=_n, progress_cb=_on_progress, log_cb=lambda m: print("\n  [log] " + m))
    _r = _dl.run()
    print()
    print("结果:", _r)
    print("sha256:", sha256_file(_out) if _out.exists() else "-")
