"""B 站原生取流：playurl(DASH) → 双流下载 → ffmpeg 合并。

为什么单独一条路径（对照 Bilidown 的做法）：

    yt-dlp 也能下 B 站，但每次都要起进程、解析文本进度，而且拿不到
    "本机现在能下哪些画质"的完整列表。这里直接走官方 playurl：

        video.m4s  ┐
                   ├─ ffmpeg -c copy → xxx.mp4
        audio.m4s  ┘

    好处是画质表（8K/4K/HDR/杜比）一次拿全、进度按字节合并、失败能
    精确知道卡在哪一步。风险是 WBI 签名会变 —— 所以调用方（manager）
    在拿到 fallback 时照旧走 yt-dlp，功能不会因为接口变了就断。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional

from . import bili, config as _config, naming, tools
from .engine import DownloadError, RangeDownloader, probe, unique_path

VIDEO_HOSTS = ("bilibili.com", "b23.tv")


def should_use(cfg: Dict[str, Any], url: str, *, playlist: bool = False,
               audio_only: bool = False, subs_only: bool = False) -> bool:
    """值不值得走原生路径。合集/字幕仍然交给 yt-dlp（那边 extractor 更全）。"""
    if not bool(cfg.get("bili_native", True)):
        return False
    if playlist or subs_only:
        return False
    host = (url or "").split("/")[2].lower() if "://" in url and len(url.split("/")) > 2 else ""
    if not any(host == h or host.endswith("." + h) for h in VIDEO_HOSTS):
        return False
    info = bili.parse_url(url)
    return bool(info.get("bvid"))


def _pick_target(cfg: Dict[str, Any], url: str) -> Dict[str, Any]:
    info = bili.parse_url(url)
    bvid = str(info.get("bvid") or "")
    if not bvid:
        raise bili.BiliError("不是单视频链接")
    vi = bili.video_info(cfg, bvid=bvid)
    if not vi.get("ok"):
        raise bili.BiliError(str(vi.get("error") or "取视频信息失败"))
    d = vi.get("data") or {}
    pages = d.get("pages") or []
    page = int(info.get("page") or 1)
    cid = 0
    part = ""
    if pages:
        idx = min(max(1, page), len(pages)) - 1
        cid = int(pages[idx].get("cid") or 0)
        part = str(pages[idx].get("part") or "")
    cid = cid or int(d.get("cid") or 0)
    if not cid:
        raise bili.BiliError("拿不到 cid")
    return {
        "bvid": bvid,
        "cid": cid,
        "title": str(d.get("title") or bvid),
        "part": part,
        "page": page,
        "pages": len(pages),
        "duration": d.get("duration") or 0,
        "uploader": (d.get("owner") or {}).get("name") or "",
    }


def _ffmpeg(cfg: Dict[str, Any]) -> Optional[str]:
    return tools.find("ffmpeg", cfg.get("ffmpeg_path", "") or "")


def _merge(cfg: Dict[str, Any], items: list, dest: pathlib.Path,
           audio_only: bool = False) -> None:
    """ffmpeg 合并。失败抛 DownloadError（调用方据此回退 yt-dlp）。"""
    exe = _ffmpeg(cfg)
    if not exe:
        raise DownloadError("没有 ffmpeg，无法合并音视频")
    if audio_only:
        afmt = str(cfg.get("audio_format") or "mp3")
        args = [exe, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(items[0]), "-vn", str(dest)]
    else:
        args = [exe, "-hide_banner", "-loglevel", "error", "-y"]
        for it in items:
            args += ["-i", str(it)]
        args += ["-c", "copy", "-movflags", "+faststart", str(dest)]
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800,
                           creationflags=0x08000000 if os.name == "nt" else 0)
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"ffmpeg 启动失败：{exc}") from exc
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        tail = (r.stderr or "").strip().splitlines()[-3:]
        raise DownloadError("ffmpeg 合并失败：" + " / ".join(tail))


def download(
    url: str,
    outdir: pathlib.Path,
    cfg: Dict[str, Any],
    *,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    audio_only: bool = False,
) -> Dict[str, Any]:
    """返回 {ok, path, produced, seconds, outdir}；
    接口不支持时返回 {ok: False, fallback: True, error}，让上层走 yt-dlp。
    """
    started = time.monotonic()
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        if log_cb:
            try:
                log_cb(msg)
            except Exception:  # noqa: BLE001
                pass

    def emit(snap: Dict[str, Any]) -> None:
        if progress_cb:
            try:
                progress_cb(snap)
            except Exception:  # noqa: BLE001
                pass

    try:
        target = _pick_target(cfg, url)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "fallback": True, "error": str(exc)}

    log(f"B 站原生解析：{target['title']}（{target['bvid']}"
        + (f" · P{target['page']}" if target["pages"] > 1 else "") + "）")

    qn = int(cfg.get("bili_qn", 0) or 0)
    try:
        pl = bili.playurl(cfg, bvid=target["bvid"], cid=target["cid"], qn=qn)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "fallback": True, "error": str(exc)}

    video = bili.best_video(pl.get("video") or [], qn=qn,
                            prefer_codec=str(cfg.get("bili_codec") or "avc"))
    audio = bili.best_audio(pl)
    if not video:
        return {"ok": False, "fallback": True, "error": "playurl 没给视频流"}

    labels = bili.QUALITY_LABELS
    got_qn = int(video.get("qn") or 0)
    avail = [labels.get(int(q), f"qn{q}") for q in (pl.get("accept_quality") or [])]
    log(f"画质：{labels.get(got_qn, f'qn{got_qn}')}"
        + (f"（可选 {', '.join(avail)}）" if avail else "")
        + (f" · {video.get('codecs') or ''}" if video.get("codecs") else ""))

    # ---- 目标文件名 ----
    base = naming.sanitize(str(target["title"]))
    if target["pages"] > 1:
        base = f"{base} P{int(target['page']):02d}" + (
            f" {naming.sanitize(target['part'])}" if target["part"] else "")
    tag = str(cfg.get("name_pattern") or naming.DEFAULT_FILE_PATTERN)
    if "%id%" in tag:
        base = f"{base} [{target['bvid']}]"
    ext = str(cfg.get("audio_format") or "mp3") if audio_only else "mp4"
    dest = unique_path(outdir / f"{base}.{ext}")

    tmp_v = dest.with_name(dest.stem + ".video.m4s")
    tmp_a = dest.with_name(dest.stem + ".audio.m4s")

    headers = {
        "Referer": "https://www.bilibili.com/",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        "Origin": "https://www.bilibili.com",
    }

    # 先探两路大小，进度才能按字节加权（否则"下到一半"永远是假数）
    est: Dict[str, int] = {}
    for key, st in (("v", video), ("a", audio)):
        if not st or audio_only and key == "v":
            continue
        try:
            est[key] = int(probe(st["url"], cfg).size or 0)
        except Exception:  # noqa: BLE001
            est[key] = 0
    total_est = sum(est.values()) or 0

    done_map = {"v": 0, "a": 0}
    t0 = time.monotonic()

    def make_cb(key: str):
        def cb(snap: Dict[str, Any]) -> None:
            done_map[key] = int(snap.get("done") or 0)
            done = sum(done_map.values())
            elapsed = max(0.001, time.monotonic() - t0)
            speed = done / elapsed
            emit({
                "size": total_est or None,
                "done": done,
                "percent": round(done * 100.0 / total_est, 2) if total_est else 0.0,
                "speed": speed,
                "eta": ((total_est - done) / speed) if total_est and speed > 0 else None,
                "dest": str(dest),
                "filename": dest.name,
                "segments_total": snap.get("segments_total"),
                "segments_created": snap.get("segments_created"),
            })
        return cb

    dl = None
    try:
        phases = []
        if not audio_only:
            phases.append(("v", video, tmp_v))
        if audio:
            phases.append(("a", audio, tmp_a))
        for key, st, tmp in phases:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadError("已取消")
            log(f"下载{'视频' if key == 'v' else '音频'}流…")
            dl = RangeDownloader(
                st["url"], tmp, cfg=cfg,
                threads=min(4, int(cfg.get("threads", 8) or 4)),
                headers=headers,
                progress_cb=make_cb(key),
                log_cb=log_cb,
                verify_size=True,
            )
            # 取消要能穿透到分片线程
            stop_watch = threading.Event()

            def watch() -> None:
                while not stop_watch.wait(0.3):
                    if cancel_event is not None and cancel_event.is_set():
                        dl.cancel()      # type: ignore[union-attr]
                        return

            w = threading.Thread(target=watch, daemon=True, name="jiliu-bili-cancel")
            w.start()
            try:
                res = dl.run()
            finally:
                stop_watch.set()
            if not res.get("ok"):
                if res.get("cancelled") or (cancel_event is not None and cancel_event.is_set()):
                    for p in (tmp_v, tmp_a):
                        try:
                            p.unlink(missing_ok=True)
                        except Exception:  # noqa: BLE001
                            pass
                    return {"ok": False, "cancelled": True, "error": None,
                            "path": str(dest)}
                raise DownloadError(str(res.get("error") or "流下载失败"))

        items = [p for p in (tmp_v, tmp_a) if p.exists()]
        if not items:
            raise DownloadError("没有拿到任何流")
        log("合并音视频…")
        _merge(cfg, items, dest, audio_only=audio_only)
    except Exception as exc:  # noqa: BLE001
        for p in (tmp_v, tmp_a):
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        if cancel_event is not None and cancel_event.is_set():
            return {"ok": False, "cancelled": True, "error": None, "path": str(dest)}
        # 合并/下载失败：清掉半成品，交给 yt-dlp 再试
        try:
            if dest.exists() and dest.stat().st_size == 0:
                dest.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "fallback": True, "error": str(exc)}

    for p in (tmp_v, tmp_a):
        try:
            p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    size = dest.stat().st_size
    emit({"size": size, "done": size, "percent": 100.0, "speed": 0,
          "dest": str(dest), "filename": dest.name, "eta": 0})
    return {
        "ok": True,
        "path": str(dest),
        "outdir": str(outdir),
        "produced": [str(dest)],
        "seconds": time.monotonic() - started,
        "bytes": size,
        "quality": got_qn,
        "error": None,
    }
