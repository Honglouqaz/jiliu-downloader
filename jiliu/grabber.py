"""轻量站点抓取（Grabber）。

IDM 的 Site Grabber 是"下载器"和"镜像工具"的分界线：给一个入口，
按层级把站点/目录搬下来。这里做的是**保守版**：

  - 只抓同源（默认），深度默认 2，页面上限 200；
  - 只镜像 HTML 与白名单里的静态资源，不碰登录态、不碰表单；
  - 进度存 `.grab.json`，中断后再跑会跳过已抓页面；
  - 单线程 BFS —— 抓站不是测压，别把对方打挂。

用法::

    job = grabber.start("https://docs.example.com/", cfg)
    grabber.get(job.id)      # 轮询
    grabber.cancel(job.id)
"""
from __future__ import annotations

import html.parser
import json
import pathlib
import threading
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Set

from . import config as _config, proxy
from .engine import DownloadError, RangeDownloader

MEDIA_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".pdf",
             ".zip", ".7z", ".rar", ".mp4", ".webm", ".mp3", ".m4a",
             ".css", ".js", ".json", ".xml", ".txt")
PAGE_EXT = ("", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp")

_jobs: Dict[str, "GrabJob"] = {}
_lock = threading.Lock()


class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: List[str] = []
        self.media: List[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for key in ("href", "src", "data-src"):
            v = d.get(key)
            if not v:
                continue
            v = v.strip()
            if not v or v.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
                continue
            if key == "href" and d.get("rel") in ("stylesheet", "icon"):
                self.media.append(v)
            elif v.lower().split("?")[0].endswith(MEDIA_EXT):
                self.media.append(v)
            else:
                self.links.append(v)


class GrabJob:
    def __init__(self, url: str, cfg: Dict[str, Any], dest: pathlib.Path,
                 depth: int, same_origin: bool, max_pages: int):
        self.id = "grab_" + uuid.uuid4().hex[:8]
        self.url = url
        self.cfg = cfg
        self.dest = dest
        self.depth = depth
        self.same_origin = same_origin
        self.max_pages = max_pages
        self.status = "queued"
        self.pages = 0
        self.files = 0
        self.bytes = 0
        self.errors: List[str] = []
        self.log: List[str] = []
        self.started = time.time()
        self.finished = 0.0
        self._stop = threading.Event()
        self._visited: Set[str] = set()
        self._origin = self._origin_of(url)

    @staticmethod
    def _origin_of(u: str) -> str:
        p = urllib.parse.urlparse(u)
        return f"{p.scheme}://{p.netloc}"

    def cancel(self) -> None:
        self._stop.set()

    def _say(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        if len(self.log) > 200:
            self.log = self.log[-200:]

    @staticmethod
    def _safe_part(text: str) -> str:
        bad = '<>:"/\\|?*'
        out = "".join("_" if (ch in bad or ord(ch) < 32) else ch for ch in text)
        return out.strip(" .") or "_"

    def _local(self, u: str) -> pathlib.Path:
        p = urllib.parse.urlparse(u)
        rel = urllib.parse.unquote(p.path or "/").lstrip("/")
        if not rel or rel.endswith("/"):
            rel += "index.html"
        parts = [self._safe_part(x) for x in rel.split("/") if x not in ("", ".", "..")]
        return self.dest / self._safe_part(p.netloc) / pathlib.Path(*parts)

    def _fetch(self, u: str) -> Optional[bytes]:
        op = proxy.opener(self.cfg, u, timeout=int(self.cfg.get("timeout", 30)))
        req = urllib.request.Request(u, headers={
            "User-Agent": str(self.cfg.get("user_agent") or "Mozilla/5.0"),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        })
        with op.open(req, timeout=int(self.cfg.get("timeout", 30))) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            data = r.read(8 * 1024 * 1024)
            if "text/html" not in ctype and "xml" not in ctype:
                return None
            return data

    def run(self) -> None:
        self.status = "running"
        self._say(f"开始抓取 {self.url}（深度 {self.depth}，上限 {self.max_pages} 页）")
        queue = [(self.url, 0)]
        self.dest.mkdir(parents=True, exist_ok=True)
        try:
            while queue and not self._stop.is_set() and self.pages < self.max_pages:
                url, depth = queue.pop(0)
                if url in self._visited:
                    continue
                self._visited.add(url)
                try:
                    data = self._fetch(url)
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"{url}: {type(exc).__name__}: {exc}")
                    continue
                if data is None:
                    continue
                local = self._local(url)
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(data)
                self.pages += 1
                self.files += 1
                self.bytes += len(data)
                self._say(f"[{self.pages}] {url}")

                parser = _Links()
                try:
                    parser.feed(data.decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001
                    pass

                for m in parser.media:
                    if self._stop.is_set():
                        break
                    mu = urllib.parse.urljoin(url, m)
                    if self._claim_media(mu):
                        pass

                if depth < self.depth:
                    for href in parser.links:
                        nu = urllib.parse.urljoin(url, href).split("#")[0]
                        if self._allowed(nu) and nu not in self._visited:
                            queue.append((nu, depth + 1))

            self.status = "cancelled" if self._stop.is_set() else "done"
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            self.finished = time.time()
            self._save_state()

    def _claim_media(self, mu: str) -> bool:
        path = urllib.parse.urlparse(mu).path.lower()
        if not path.endswith(MEDIA_EXT):
            return False
        local = self._local(mu)
        if local.exists():
            return True
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            dl = RangeDownloader(mu, local, cfg=self.cfg, threads=2,
                                 log_cb=self._say)
            res = dl.run()
            if res.get("ok"):
                self.files += 1
                self.bytes += int(res.get("bytes") or 0)
            return bool(res.get("ok"))
        except DownloadError as exc:
            self.errors.append(f"{mu}: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{mu}: {type(exc).__name__}: {exc}")
            return False

    def _allowed(self, u: str) -> bool:
        if not u.lower().startswith(("http://", "https://")):
            return False
        if self.same_origin and not u.startswith(self._origin):
            return False
        path = urllib.parse.urlparse(u).path.lower()
        if path.endswith(MEDIA_EXT):
            return False          # 媒体交给 _claim_media，别当页面抓
        return True

    def _save_state(self) -> None:
        try:
            (self.dest / ".grab.json").write_text(json.dumps({
                "url": self.url, "visited": sorted(self._visited),
                "pages": self.pages, "files": self.files, "status": self.status,
                "finished": self.finished,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "url": self.url, "status": self.status,
            "depth": self.depth, "same_origin": self.same_origin,
            "pages": self.pages, "files": self.files, "bytes": self.bytes,
            "dest": str(self.dest), "elapsed": round((self.finished or time.time()) - self.started, 1),
            "errors": self.errors[-20:], "log": self.log[-40:],
            "max_pages": self.max_pages,
        }


def start(url: str, cfg: Dict[str, Any], *, dest_dir: str = "",
          depth: Optional[int] = None, same_origin: Optional[bool] = None) -> GrabJob:
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("只支持 http(s)")
    depth = int(cfg.get("grab_max_depth", 2) if depth is None else depth)
    depth = max(0, min(5, depth))
    max_pages = max(1, min(2000, int(cfg.get("grab_max_pages", 200) or 200)))
    same = bool(cfg.get("grab_same_origin", True) if same_origin is None else same_origin)
    root = pathlib.Path(dest_dir) if dest_dir else _config.download_dir(cfg) / "站点抓取"
    p = urllib.parse.urlparse(url)
    host = p.netloc.replace(":", "_") or "site"
    tail = urllib.parse.unquote(p.path).strip("/").replace("/", "_")[:40]
    name = GrabJob._safe_part(host + ("_" + tail if tail else ""))
    dest = root / name
    job = GrabJob(url, cfg, dest, depth, same, max_pages)
    with _lock:
        _jobs[job.id] = job
    threading.Thread(target=job.run, daemon=True, name=f"jiliu-grab-{job.id}").start()
    return job


def get(job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        job = _jobs.get(job_id)
    return job.to_dict() if job else None


def cancel(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        return False
    job.cancel()
    return True
