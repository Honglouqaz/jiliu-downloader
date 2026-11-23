"""直播流录制（m3u8 边录边写）。

和普通 m3u8 下载的区别只有一条：直播的 playlist 里**没有
`#EXT-X-ENDLIST`**，永远"还有下一条"。普通下载器会一直等到超时，
或者只拿到最开始那 6 个分片就当成下完了。

录制流程
    1. 每 TARGETDURATION/2 秒刷一次 playlist
    2. 按 `#EXT-X-MEDIA-SEQUENCE` 判断哪些分片是新的，只下新的
    3. 分片按序号落盘成 seg_000123.ts
    4. 结束（到时长 / 手动停 / 等到 ENDLIST）后按序号二进制拼接
       —— TS 流本身就可以直接拼，不需要 ffmpeg
    5. 有 ffmpeg 且要 mp4 时，再 `-c copy` 转一次封装

为什么不边下边用 ffmpeg 灌管道
    ffmpeg 从 stdin 读 ts 时，一旦断流就整体失败，前面的内容也拿不回来。
    先落分片再拼接，断流只是少几秒，已录的部分还在 —— 直播场景里
    「拿回 90%」比「要么全有要么全无」重要得多。
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .engine import human_size, urlopen

# 直播判定与参数
DEFAULT_POLL = 2.0
STALL_TIMEOUT = 30.0        # 多久没有新分片就认为断流，重连
MAX_SEGMENTS = 200000       # 兜底上限，防止 playlist 异常时无限写盘
_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(line: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in _ATTR_RE.findall(line or ""):
        out[k] = v.strip('"')
    return out


def parse_playlist(text: str, base_url: str) -> Dict[str, Any]:
    """解析 m3u8。master playlist 会返回 variants；media playlist 返回 segments。"""
    lines = [ln.strip() for ln in str(text or "").splitlines()]
    base = base_url
    variants: List[Dict[str, Any]] = []
    segments: List[Dict[str, Any]] = []
    endlist = False
    target = 0.0
    seq = 0
    pending_variant: Optional[Dict[str, Any]] = None
    pending_inf: Optional[Dict[str, Any]] = None

    for ln in lines:
        if not ln:
            continue
        if ln.startswith("#EXT-X-STREAM-INF"):
            a = _attrs(ln)
            try:
                bw = int(a.get("BANDWIDTH") or 0)
            except Exception:
                bw = 0
            pending_variant = {"bandwidth": bw, "resolution": a.get("RESOLUTION", "")}
            continue
        if ln.startswith("#EXT-X-TARGETDURATION"):
            try:
                target = float(ln.split(":", 1)[1])
            except Exception:
                target = 0.0
            continue
        if ln.startswith("#EXT-X-MEDIA-SEQUENCE"):
            try:
                seq = int(ln.split(":", 1)[1])
            except Exception:
                seq = 0
            continue
        if ln.startswith("#EXTINF"):
            try:
                dur = float(ln.split(":", 1)[1].split(",")[0])
            except Exception:
                dur = 0.0
            pending_inf = {"duration": dur}
            continue
        if ln.startswith("#EXT-X-ENDLIST"):
            endlist = True
            continue
        if ln.startswith("#"):
            continue
        # 非 # 开头 = URI
        u = urllib.parse.urljoin(base, ln)
        if pending_variant is not None:
            pending_variant["url"] = u
            variants.append(pending_variant)
            pending_variant = None
            continue
        item = {"url": u, "duration": (pending_inf or {}).get("duration", 0.0),
                "index": seq + len(segments)}
        segments.append(item)
        pending_inf = None

    return {
        "variants": variants,
        "segments": segments,
        "endlist": endlist,
        "target_duration": target or 2.0,
        "media_sequence": seq,
        "is_live": bool(segments) and not endlist,
        "is_master": bool(variants),
    }


def best_variant(playlist: Dict[str, Any]) -> Optional[str]:
    vs = [v for v in playlist.get("variants") or [] if v.get("url")]
    if not vs:
        return None
    return max(vs, key=lambda v: int(v.get("bandwidth") or 0))["url"]


def is_live_url(url: str, cfg: Dict[str, Any], timeout: float = 8.0,
                headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """判断这个 m3u8 是不是直播。返回 {ok, live, reason, playlist}。"""
    try:
        text = fetch_text(url, cfg, timeout, headers)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "live": False, "reason": f"{type(exc).__name__}: {exc}",
                "playlist": {}}
    pl = parse_playlist(text, url)
    if pl["is_master"]:
        v = best_variant(pl)
        if not v:
            return {"ok": True, "live": False, "reason": "master playlist 里没有子流",
                    "playlist": pl}
        try:
            sub = parse_playlist(fetch_text(v, cfg, timeout, headers), v)
            return {"ok": True, "live": sub["is_live"], "reason": "",
                    "playlist": sub}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "live": False, "reason": str(exc), "playlist": pl}
    if not pl["segments"]:
        return {"ok": True, "live": False, "reason": "playlist 里没有分片",
                "playlist": pl}
    return {"ok": True, "live": pl["is_live"],
            "reason": "" if pl["is_live"] else "带 ENDLIST，是点播",
            "playlist": pl}


DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _ua(cfg: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> str:
    """请求 UA：任务级 > 视频专用 > 全局 > 浏览器默认。

    以前这里是 `cfg["user_agent"] or "Jiliu/1.0"` —— 直播录制每个分片
    都拿 "Jiliu/1.0" 去请求，等于主动告诉 CDN "我是爬虫"。video.py
    早就写了这条教训，录制器没跟上。
    """
    for src in (headers or {},):
        if src.get("User-Agent") or src.get("user-agent"):
            return str(src.get("User-Agent") or src.get("user-agent"))
    ua = str(cfg.get("video_user_agent") or cfg.get("user_agent") or DEFAULT_UA)
    # 全局 UA 默认带 " Jiliu/1.0" 后缀；对直播 CDN 来说这就是自报爬虫
    return re.sub(r"\s*Jiliu/\S+\s*$", "", ua).strip() or DEFAULT_UA


def fetch_text(url: str, cfg: Dict[str, Any], timeout: float = 8.0,
               headers: Optional[Dict[str, str]] = None) -> str:
    h = {"User-Agent": _ua(cfg, headers), "Accept": "*/*"}
    for k, v in (headers or {}).items():
        if v:
            h[k] = str(v)
    req = urllib.request.Request(url, headers=h)
    with urlopen(req, timeout=timeout) as r:
        return r.read(4 * 1024 * 1024).decode("utf-8", "replace")


def fetch_segment(url: str, dest: pathlib.Path, cfg: Dict[str, Any],
                  timeout: float = 20.0,
                  headers: Optional[Dict[str, str]] = None) -> int:
    h = {"User-Agent": _ua(cfg, headers)}
    for k, v in (headers or {}).items():
        if v:
            h[k] = str(v)
    req = urllib.request.Request(url, headers=h)
    n = 0
    tmp = pathlib.Path(str(dest) + ".part")
    with urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        while True:
            buf = r.read(262144)
            if not buf:
                break
            f.write(buf)
            n += len(buf)
    tmp.replace(dest)
    return n


def merge_segments(files: List[pathlib.Path], out: pathlib.Path) -> int:
    """按顺序二进制拼接。TS 流本身可分片拼接，不需要转码。"""
    total = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(str(out) + ".part")
    with open(tmp, "wb") as w:
        for f in files:
            try:
                with open(f, "rb") as r:
                    while True:
                        buf = r.read(1024 * 1024)
                        if not buf:
                            break
                        w.write(buf)
                        total += len(buf)
            except OSError:
                continue
    tmp.replace(out)
    return total


def remux(ts: pathlib.Path, out: pathlib.Path, cfg: Dict[str, Any],
          timeout: int = 600) -> Dict[str, Any]:
    """用 ffmpeg 转封装（不改编码）。ffmpeg 不可用就返回 skipped。"""
    exe = str(cfg.get("ffmpeg_path") or "").strip()
    if not exe:
        try:
            from . import tools
            found = tools.find("ffmpeg", str(cfg.get("ffmpeg_path") or ""))
            exe = str(found or "")
        except Exception:
            exe = ""
    if not exe or not pathlib.Path(exe).exists():
        return {"ok": False, "skipped": True, "error": "没有 ffmpeg，保留 .ts"}
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(ts), "-c", "copy", "-bsf:a", "aac_adtstoasc", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        if r.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return {"ok": True, "skipped": False, "error": ""}
        return {"ok": False, "skipped": False,
                "error": (r.stderr or b"").decode("utf-8", "replace")[-300:]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "skipped": False, "error": str(exc)}


class LiveRecorder:
    """录一段直播。接口和 RangeDownloader 对齐（run/pause/cancel/snapshot）。"""

    engine = "live"

    def __init__(self, url: str, dest: pathlib.Path, *,
                 cfg: Dict[str, Any],
                 minutes: float = 0,
                 max_bytes: int = 0,
                 poll: float = 0.0,
                 container: str = "ts",
                 progress_cb=None, log_cb=None,
                 headers: Optional[Dict[str, str]] = None):
        self.url = url
        self.headers = dict(headers or {})
        self.dest = pathlib.Path(dest)
        self.cfg = cfg or {}
        self.minutes = max(0.0, float(minutes or 0))
        self.max_bytes = max(0, int(max_bytes or 0))
        self.poll = float(poll or 0)
        self.container = (container or "ts").lower()
        self.progress_cb = progress_cb
        self.log_cb = log_cb

        self._paused = threading.Event()
        self._cancelled = threading.Event()
        self._error = ""
        self.segments: List[pathlib.Path] = []
        self.bytes = 0
        self.dropped = 0
        self.media_url = ""
        self.title = ""
        self._started = 0.0
        self._finished = 0.0
        self.work_dir = pathlib.Path(str(self.dest) + ".live")

    # ---- 控制 ----
    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self) -> None:
        self._cancelled.set()
        self._paused.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def set_speed_limit(self, bps: int) -> None:
        # 直播是实时流，限速没有意义（追不上就丢分片）
        return None

    def _log(self, msg: str) -> None:
        if self.log_cb:
            try:
                self.log_cb(msg)
            except Exception:
                pass

    def _emit(self) -> None:
        if not self.progress_cb:
            return
        try:
            self.progress_cb({
                "percent": 0.0,
                "done": self.bytes,
                "size": 0,
                "speed": 0.0,
                "eta": None,
                "segments": len(self.segments),
                "live": True,
                "elapsed": (self._finished or time.monotonic()) - self._started,
            })
        except Exception:
            pass

    def snapshot(self) -> Dict[str, Any]:
        return {
            "ok": not self._error,
            "paused": self.paused,
            "cancelled": self.cancelled,
            "bytes": self.bytes,
            "segments": len(self.segments),
            "error": self._error,
            "live": True,
        }

    # ---- 主流程 ----
    def run(self) -> Dict[str, Any]:
        self._started = time.monotonic()
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            self._loop()
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            self._log(f"录制中断：{self._error}")
        finally:
            self._finished = time.monotonic()

        out = self._finalize()
        return {
            "ok": bool(out.get("ok")),
            "path": str(out.get("path") or ""),
            "bytes": out.get("bytes") or 0,
            "segments": len(self.segments),
            "seconds": self._finished - self._started,
            "cancelled": self.cancelled,
            "error": self._error if not out.get("ok") else "",
            "live": True,
        }

    def _loop(self) -> None:
        # 首跳也要带任务级 headers：有 Referer/Cookie 防盗链的直播，
        # 第一步判定就 403 的话，用户只会看到一句异常类名。
        info = is_live_url(self.url, self.cfg, headers=self.headers)
        if not info["ok"]:
            raise RuntimeError(info["reason"])
        pl = info["playlist"]
        target = float(pl.get("target_duration") or 2.0)
        media_url = self.url
        if pl["is_master"]:
            v = best_variant(pl)
            if not v:
                raise RuntimeError("master playlist 里没有子流")
            media_url = v
            pl = parse_playlist(fetch_text(v, self.cfg, headers=self.headers), v)
            target = float(pl.get("target_duration") or target)
        self.media_url = media_url
        if not info["live"]:
            self._log("这个 playlist 带 ENDLIST（点播），按普通录制处理一次到底")

        interval = self.poll or max(1.0, target / 2.0)
        seen: Dict[int, pathlib.Path] = {}
        last_new = time.monotonic()
        stalls = 0
        deadline = (self._started + self.minutes * 60) if self.minutes else 0
        files: List[pathlib.Path] = []

        while not self._cancelled.is_set():
            while self._paused.is_set():
                if self._cancelled.is_set():
                    break
                time.sleep(0.2)
            if self._cancelled.is_set():
                break
            if deadline and time.monotonic() >= deadline:
                self._log(f"到达设定时长 {self.minutes:g} 分钟，停止录制")
                break
            if self.max_bytes and self.bytes >= self.max_bytes:
                self._log(f"到达设定大小 {human_size(self.max_bytes)}，停止录制")
                break

            try:
                text = fetch_text(media_url, self.cfg, headers=self.headers)
                cur = parse_playlist(text, media_url)
            except Exception as exc:  # noqa: BLE001
                # 断流：不退出，等一会儿重连（直播掉线很常见）。
                # 以前说"尝试重连"其实只是继续轮询同一个 media_url；
                # CDN 的带 token 地址过期后永远不会恢复。超过一个
                # STALL_TIMEOUT 就重新解析一次播放列表。
                down = time.monotonic() - last_new
                self._log(f"拉取 playlist 失败（{exc}），稍后重试")
                if down > STALL_TIMEOUT * 4:
                    self._error = f"断流超过 {STALL_TIMEOUT * 4:.0f} 秒，放弃"
                    break
                if down > STALL_TIMEOUT:
                    try:
                        fresh = is_live_url(self.url, self.cfg, headers=self.headers)
                        fpl = (fresh or {}).get("playlist") or {}
                        if fresh.get("ok") and fpl.get("is_master"):
                            v2 = best_variant(fpl)
                            if v2:
                                media_url = v2
                        if fresh.get("ok"):
                            self._log("已重新解析播放列表，继续录制")
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(min(interval * 2, 5))
                continue

            new = 0
            for seg in cur["segments"]:
                if self._cancelled.is_set():
                    break
                idx = int(seg["index"])
                if idx in seen:
                    continue
                if len(files) >= MAX_SEGMENTS:
                    self._log("分片数达到上限，停止录制")
                    return
                name = self.work_dir / f"seg_{idx:08d}.ts"
                try:
                    n = fetch_segment(seg["url"], name, self.cfg,
                                      headers=self.headers)
                except Exception as exc:  # noqa: BLE001
                    self.dropped += 1
                    self._log(f"分片 {idx} 下载失败（{exc}），跳过")
                    continue
                seen[idx] = name
                files.append(name)
                self.bytes += n
                new += 1
                self._emit()

            if new:
                last_new = time.monotonic()
                stalls = 0
            else:
                stalls += 1
                if cur["endlist"]:
                    self._log("playlist 出现 ENDLIST，直播结束")
                    break
                if time.monotonic() - last_new > STALL_TIMEOUT:
                    self._log(f"{STALL_TIMEOUT:.0f} 秒没有新分片，尝试重连")
                    last_new = time.monotonic()
            if cur["endlist"] and not new:
                break
            time.sleep(interval)

        files.sort(key=lambda p: int(p.stem.split("_")[-1]))
        self.segments = files

    def _finalize(self) -> Dict[str, Any]:
        if not self.segments:
            return {"ok": False, "path": "", "bytes": 0}
        ext = "mp4" if self.container in ("mp4", "mkv") else "ts"
        out = self.dest.with_suffix("." + ext)
        self._log(f"拼接 {len(self.segments)} 个分片 → {out.name}")
        n = merge_segments(self.segments, out)
        self.bytes = max(self.bytes, n)
        if ext != "ts":
            # ffmpeg 不能读写同一个文件：先转到临时名，成功再换回来
            tmp = self.dest.with_suffix(".remux." + ext)
            r = remux(out, tmp, self.cfg)
            if r.get("ok") and tmp.is_file():
                try:
                    out.unlink()
                    tmp.replace(out)
                except Exception:
                    out = tmp
            else:
                # 转封装失败就保留 .ts，别让用户两手空空。
                # 注意：不管失败原因是「没装 ffmpeg」还是「装了但转挂了」，
                # 后缀都必须改回 .ts —— 否则 TS 字节顶着 .mp4 的名字落盘，
                # 播放器按 mp4 解析直接报「文件损坏」。
                self._log("转封装失败，保留 .ts（可直接播放）")
                try:
                    if tmp.is_file():
                        tmp.unlink()
                except Exception:
                    pass
                ts = self.dest.with_suffix(".ts")
                try:
                    if out != ts and out.is_file():
                        out.replace(ts)
                        out = ts
                except Exception:
                    pass
        try:
            shutil.rmtree(self.work_dir, ignore_errors=True)
        except Exception:
            pass
        return {"ok": out.is_file() and out.stat().st_size > 0,
                "path": str(out), "bytes": out.stat().st_size if out.is_file() else 0}


def describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "available": True,
        "default_poll": DEFAULT_POLL,
        "stall_timeout": STALL_TIMEOUT,
        "note": "缺 #EXT-X-ENDLIST 的 m3u8 会被识别为直播，可设时长上限",
    }
