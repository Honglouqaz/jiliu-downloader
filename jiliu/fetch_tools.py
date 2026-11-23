"""缺失组件的按需获取：把「未安装」变成「点一下就好」。

为什么需要这个模块：

  「环境体检」原来只会告诉你 `wget 未安装`，然后给一行
  `scoop install wget`。对开发者这没问题，对普通用户等于没说 ——
  他们不知道 scoop 是什么，也不该为了下个视频去装包管理器。

  而默认引擎顺序第一位是 aria2，机器上没有 aria2c 时就会静默退回自研引擎，
  用户以为「开了自适应并发」，其实没有。这个落差是开箱体验最伤的一处。

两条路一起走：

  1) aria2c **随包带**（压完 5.6MB，值得）—— 装完即用，零等待。
     GPLv2+ 的分发义务靠 licenses/aria2-COPYING.txt 与「源码地址」声明履行。
  2) 大件（ffmpeg 114MB、N_m3u8DL-RE、wget）**按需下载**。随包会让安装包
     从 20MB 涨到 140MB，绝大多数人用不上，不如点了再下。

下载只认官方地址，并保留镜像前缀回退（GitHub 直连在国内经常被 reset）。
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import threading
import time
import urllib.request
import zipfile
from typing import Any, Dict, List, Optional

from . import config as _config, proxy, tools

# ---------------------------------------------------------------------------
# 可自动获取的组件
# ---------------------------------------------------------------------------
# mirrors 是**前缀**，拼在官方 URL 前面。顺序即尝试顺序。
_GITHUB_MIRRORS = ["https://ghfast.top/", "https://gh-proxy.com/"]

SOURCES: Dict[str, Dict[str, Any]] = {
    "aria2": {
        "label": "aria2c",
        "exe": "aria2c.exe",
        "url": "https://github.com/aria2/aria2/releases/download/release-1.37.0/"
               "aria2-1.37.0-win-64bit-build1.zip",
        "mirrors": _GITHUB_MIRRORS,
        "kind": "zip",
        "pick": ["aria2c.exe"],
        "also": {"COPYING": "licenses/aria2-COPYING.txt"},
        "size": 2_475_379,
        "license": "GPL-2.0-or-later",
        "home": "https://aria2.github.io/",
        "source": "https://github.com/aria2/aria2",
        "note": "已随包附带，通常不需要再下。",
    },
    "ffmpeg": {
        "label": "FFmpeg",
        "exe": "ffmpeg.exe",
        "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "mirrors": [],
        "kind": "zip",
        "pick": ["ffmpeg.exe", "ffprobe.exe"],
        "also": {},
        "size": 114_768_076,
        "license": "LGPL-2.1-or-later（本构建）",
        "home": "https://ffmpeg.org/",
        "source": "https://ffmpeg.org/download.html",
        "note": "约 110MB，用于音视频合流与转码。不下也能下文件，只是"
                "画质与音轨分离的视频合不起来。",
    },
    "N_m3u8DL-RE": {
        "label": "N_m3u8DL-RE",
        "exe": "N_m3u8DL-RE.exe",
        "url": "https://github.com/nilaoda/N_m3u8DL-RE/releases/latest/download/"
               "N_m3u8DL-RE_v0.3.0-beta_win-x64_20241231.zip",
        "mirrors": _GITHUB_MIRRORS,
        "kind": "zip",
        "pick": ["N_m3u8DL-RE.exe"],
        "also": {},
        "size": 12_000_000,
        "license": "MIT",
        "home": "https://github.com/nilaoda/N_m3u8DL-RE",
        "source": "https://github.com/nilaoda/N_m3u8DL-RE",
        "note": "可选加强：HLS/DASH 分片加密与多音轨比 ffmpeg 稳。",
    },
    "wget": {
        "label": "GNU Wget",
        "exe": "wget.exe",
        "url": "https://eternallybored.org/misc/wget/current/wget.exe",
        "mirrors": [],
        "kind": "exe",
        "pick": ["wget.exe"],
        "also": {},
        "size": 5_000_000,
        "license": "GPL-3.0-or-later",
        "home": "https://www.gnu.org/software/wget/",
        "source": "https://git.savannah.gnu.org/cgit/wget.git",
        "note": "单文件。断流续传最稳的兜底引擎。",
    },
}

# 运行中的任务。key → {status, got, total, error, at, tries}
_JOBS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def target_dir() -> pathlib.Path:
    """下载下来的组件放哪。

    frozen 时不能写 _MEIPASS（那是临时解包目录，退出就没），
    要放 exe 旁边的 bin/ —— 这也正好是 tools.find() 的第二个搜索位置。
    """
    if tools.IS_FROZEN:
        d = tools._exe_dir() / "bin"
    else:
        d = pathlib.Path(__file__).resolve().parent.parent / "bin"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _safe_name(name: str) -> str:
    return os.path.basename(name).replace("..", "_")


def _opener(cfg: Dict[str, Any], url: str):
    """按代理设置开一个 opener。本机/直连走 ProxyHandler({})，
    否则沿用系统或手动代理 —— 否则在必须挂代理的网络里下不动。"""
    p = ""
    try:
        p = proxy.select(cfg or {}, url)
    except Exception:
        p = ""
    if not p:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": p, "https": p}))


def _set(key: str, **kw) -> None:
    with _LOCK:
        j = _JOBS.setdefault(key, {})
        j.update(kw)
        j["at"] = time.time()


def job(key: str) -> Dict[str, Any]:
    with _LOCK:
        return dict(_JOBS.get(key) or {"status": "idle", "key": key})


def _download(key: str, cfg: Dict[str, Any], cancel: threading.Event) -> None:
    src = SOURCES[key]
    urls = [src["url"]] + [m + src["url"] for m in (src.get("mirrors") or [])]
    last_err = ""
    for url in urls:
        if cancel.is_set():
            _set(key, status="cancelled")
            return
        try:
            op = _opener(cfg, url)
            req = urllib.request.Request(url, headers={"User-Agent": "jiliu/1.8"})
            _set(key, status="downloading", got=0, total=src.get("size") or 0,
                 error="", source=url)
            buf = bytearray()
            with op.open(req, timeout=60) as r:
                total = int(r.headers.get("Content-Length") or 0) or (src.get("size") or 0)
                _set(key, total=total)
                while True:
                    if cancel.is_set():
                        _set(key, status="cancelled")
                        return
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    _set(key, got=len(buf))
            _set(key, status="unpacking")

            # 组件下载以前是「写进 bin/ 就直接执行」——镜像被投毒或域名
            # 被接管等于把任意 exe 装进用户机器。有 pin 的哈希必须对上；
            # 没有 pin 的也把实际哈希记下来（界面能看到"未校验"）。
            digest = hashlib.sha256(bytes(buf)).hexdigest()
            want = str(src.get("sha256") or "").lower()
            if want and digest != want:
                _set(key, status="error",
                     error=f"校验失败：期望 {want[:12]}…，实际 {digest[:12]}…（已拒绝安装）")
                return

            dest = target_dir()
            if src["kind"] == "zip":
                _unzip(buf, src, dest)
            else:
                name = _safe_name(src["exe"])
                (dest / name).write_bytes(bytes(buf))

            _set(key, status="done", got=len(buf),
                 total=len(buf) or src.get("size") or 0,
                 sha256=digest, verified=bool(want))
            try:
                tools.invalidate_probe_cache()
            except Exception:
                pass
            return
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            continue
    _set(key, status="error", error=last_err or "下载失败")


def _unzip(buf: bytearray, src: Dict[str, Any], dest: pathlib.Path) -> None:
    import io
    z = zipfile.ZipFile(io.BytesIO(bytes(buf)))
    want = {w.lower() for w in src.get("pick") or []}
    # 许可文件跟着走 —— 分发 GPL/LGPL 二进制时这是义务，不是可选
    also = {os.path.basename(k).upper(): v
            for k, v in (src.get("also") or {}).items()}
    root = pathlib.Path(__file__).resolve().parent.parent
    for info in z.infolist():
        base = os.path.basename(info.filename)
        if not base:
            continue
        if base.lower() in want:
            with z.open(info) as f, open(dest / base, "wb") as out:
                shutil.copyfileobj(f, out)
        elif base.upper() in also:
            out_path = root / also[base.upper()]
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as f, open(out_path, "wb") as out:
                    shutil.copyfileobj(f, out)
            except Exception:
                pass


def start(key: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """开始下载。已经在跑就直接返回当前进度，不重复起线程。"""
    key = (key or "").strip()
    if key not in SOURCES:
        return {"ok": False, "error": f"不支持自动获取：{key or '（空）'}"}
    if os.name != "nt":
        return {"ok": False,
                "error": "自动获取目前只支持 Windows；macOS / Linux 请用包管理器（brew / apt）"}
    j = job(key)
    if j.get("status") in ("downloading", "unpacking"):
        return {"ok": True, "job": j, "already": True}
    cancel = threading.Event()
    _CANCELS[key] = cancel
    _set(key, status="downloading", got=0, total=SOURCES[key].get("size") or 0, error="")
    t = threading.Thread(target=_download, args=(key, cfg or {}, cancel),
                         daemon=True, name=f"fetch-{key}")
    t.start()
    return {"ok": True, "job": job(key)}


_CANCELS: Dict[str, threading.Event] = {}


def cancel(key: str) -> Dict[str, Any]:
    ev = _CANCELS.get((key or "").strip())
    if ev:
        ev.set()
        return {"ok": True}
    return {"ok": False, "error": "没有正在进行的下载"}


def describe(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给界面用：每项的来源、体积、许可、当前是否已装、下载进度。"""
    rows: List[Dict[str, Any]] = []
    for k, s in SOURCES.items():
        found = ""
        try:
            found = tools.find(os.path.splitext(s["exe"])[0], "") or ""
        except Exception:
            found = ""
        rows.append({
            "key": k,
            "label": s["label"],
            "exe": s["exe"],
            "size": s.get("size") or 0,
            "license": s.get("license") or "",
            "home": s.get("home") or "",
            "source": s.get("source") or "",
            "note": s.get("note") or "",
            "installed": bool(found),
            "path": found,
            "job": job(k),
        })
    return {"ok": True, "items": rows, "target": str(target_dir()),
            "os": "win" if os.name == "nt" else os.name}
