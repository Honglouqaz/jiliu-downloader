"""本地 HTTP API + Web 面板。

只监听 127.0.0.1。浏览器扩展和面板都通过它来干活。

安全上做了两件事（都不是可选项）：
  1. 只绑定回环地址，不对局域网/公网开放
  2. Origin 白名单 —— 默认只放行 chrome-extension:// / moz-extension://
     和本机页面。否则任何网页都能 POST 过来往你硬盘里写文件
"""
from __future__ import annotations

import atexit
import base64
import gzip
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from . import (APP_NAME, __version__, about, accounts, backends, batch, bili,
               capture_rules, channels, checksum, clipboard, config as _config,
               cookies, danmaku, desktop, dropwin, douyin, edition, engines,
               feed, fetch_tools, finish, grabber, i18n, layout,
               license as _license, naming,
               netdisk, notify, pagesniff, plugins, presets, protocols, proxy,
               qr, queues,
               resource, schedule, shellmenu, sites, snapshot, sniffproxy,
               subscriptions, theme, tools, torrentfile, transfer, update,
               urltake, video)
from .engine import human_size, probe as http_probe
from .manager import TaskManager

# 已下载记录（下过的还下不下，靠它判断）。惰性建一次，别每次请求都读盘。
_history: Optional[naming.History] = None
_history_lock = threading.Lock()

# 订阅定时检查 / 剪贴板轮询 / 最近抓到的链接
_FEED_WATCHER: Optional[Any] = None
_CLIP_WATCHER: Optional[Any] = None
_CLIP_RECENT: List[Dict[str, Any]] = []

# 协议唤起的最近结果。用户点了网页链接却「什么都没发生」时，
# 界面能把原因摆出来（链接不合法 / 被门禁拦了 / 服务没接住）。
_OPEN_RECENT: List[Dict[str, Any]] = []

# 出站配置脱敏：这些键永远不原样回给前端
_MASK = "••••••"
_SECRET_KEYS = ("proxy_pass", "aria2_rpc_secret", "motrix_rpc_secret", "rpc_secret")


def note_protocol_open(info: Dict[str, Any], res: Dict[str, Any]) -> None:
    # /api/tasks 的成功响应是 {ok, task:{id,...}, kind}；合集是 {ok, ids:[...]}；
    # 失败是 {ok:False, error}。三种都收，别只认一种。
    task = res.get("task") if isinstance(res.get("task"), dict) else {}
    tid = str(task.get("id") or "")
    if not tid:
        ids = res.get("ids") or [res.get("id")] or [""]
        tid = str(ids[0] or "") if isinstance(ids, (list, tuple)) and ids else ""
    _OPEN_RECENT.append({
        "url": str(info.get("url") or "")[:300],
        "ok": bool(res.get("ok")),
        "error": str(res.get("error") or "")[:200],
        "task": tid,
        "title": str(task.get("title") or info.get("title") or "")[:120],
        "at": time.time(),
    })
    del _OPEN_RECENT[:-20]
_FEED_LOCK = threading.Lock()

# 拖拽悬浮窗句柄（[0] 为 None 表示没开）。用列表持有是因为
# 下面几个函数要能改它，而模块级 global 在多线程里更容易写错。
_DROP: List[Any] = [None]
_DROP_LOCK = threading.Lock()


def _task_meta(b: Dict[str, Any]) -> Dict[str, Any]:
    """把任务级选项收进 meta。

    以前只有 referer/from，1.8.0 加了任务级限速、镜像、弹幕开关，
    这些东西必须跟着任务持久化（重启续传还在），所以统一在这里拼。
    """
    meta: Dict[str, Any] = {"referer": b.get("referer") or "",
                            "from": b.get("from") or ""}
    # 任务级请求头：网页解析 / 浏览器扩展给的 Referer·Cookie·UA。
    # 存放在任务里（随任务持久化），重启后重试也带得上。
    hdrs = b.get("headers")
    if isinstance(hdrs, dict):
        clean = {str(k): str(v) for k, v in hdrs.items() if v not in (None, "")}
        if clean:
            meta["headers"] = clean
    try:
        if b.get("speed_limit"):
            meta["speed_limit"] = int(b["speed_limit"])
    except (TypeError, ValueError):
        pass
    mir = b.get("mirrors")
    if isinstance(mir, (list, tuple)):
        meta["mirrors"] = [str(x).strip() for x in mir if str(x).strip()]
    if b.get("danmaku") is not None:
        meta["danmaku"] = bool(b["danmaku"])
    if b.get("filename_hint"):
        meta["filename_hint"] = str(b["filename_hint"])[:120]
    return meta


def _flat_sum(r: Dict[str, Any], algo: str = "") -> Dict[str, Any]:
    """把校验结果摊平成 `{"sha256": "...", "md5": "..."}`。

    compute() 给的是 {"algo":..,"hex":..}，compute_all() 给的是
    {"hashes": {...}} —— 两种形状让前端要写两套取值逻辑，不如在这里统一。
    """
    if not isinstance(r, dict):
        return {}
    for k, v in (r.pop("hashes", None) or {}).items():
        r.setdefault(k, v)
    if algo and r.get("hex"):
        r.setdefault(str(algo).lower(), r["hex"])
    return r


def _safe_save(cfg: Dict[str, Any]) -> None:
    """写配置，失败不抛（磁盘满/权限问题不该让一次点击变 500）。"""
    try:
        _config.save(cfg)
    except Exception:  # noqa: BLE001
        pass


def _drop_toggle(on: bool, handler: Any) -> Dict[str, Any]:
    """开/关拖拽悬浮窗。

    窗是 Win32 窗口，必须活在**有消息循环的线程**里 —— DropWindow.start()
    自己起了线程，这里只管创建和销毁。
    """
    with _DROP_LOCK:
        cur = _DROP[0]
        if on:
            if cur is not None:
                return {"ok": True, "running": True, "note": "已经在运行"}
            if not hasattr(dropwin, "DropWindow"):
                return {"ok": False, "error": "当前平台不支持悬浮窗"}
            try:
                def on_urls(urls: List[str]) -> None:
                    for u in urls:
                        try:
                            handler.tm.create_http(u, auto_classify=False)
                        except Exception:  # noqa: BLE001
                            continue

                w = dropwin.DropWindow(on_urls)
                if not w.start():
                    return {"ok": False, "error": "窗口创建失败（可能被安全软件拦了）"}
                _DROP[0] = w
                handler.cfg["drop_target"] = True
                _safe_save(handler.cfg)
                return {"ok": True, "running": True}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if cur is None:
            handler.cfg["drop_target"] = False
            _safe_save(handler.cfg)
            return {"ok": True, "running": False}
        try:
            cur.stop()
        except Exception:  # noqa: BLE001
            pass
        _DROP[0] = None
        handler.cfg["drop_target"] = False
        _safe_save(handler.cfg)
        return {"ok": True, "running": False}


def history() -> naming.History:
    global _history
    with _history_lock:
        if _history is None:
            _history = naming.History()
        return _history


def _queue_feed_items(tm: TaskManager, cfg: Dict[str, Any],
                      sub: Dict[str, Any], items: List[Dict[str, Any]]) -> int:
    """把订阅新条目变成任务。单条失败不影响其他条目。"""
    n = 0
    for it in items:
        url = str(it.get("url") or it.get("link") or "").strip()
        if not url:
            continue
        try:
            tm.create_http(url, dest_dir=str(sub.get("folder") or ""),
                           meta={"from": "feed", "sub": str(sub.get("id") or ""),
                                 "referer": str(it.get("link") or "")})
            n += 1
        except Exception:  # noqa: BLE001
            continue
    return n


# B 站登录态要问一次网络（whoami）。面板每两秒轮询一次 /api/state，
# 不做缓存等于每两秒朝 B站打一发，既慢又容易被风控盯上。
_bili_cache: Dict[str, Any] = {"at": 0.0, "data": None}
_bili_lock = threading.Lock()
BILI_TTL = 60.0


def bili_status(cfg: Dict[str, Any], *, refresh: bool = False) -> Dict[str, Any]:
    now = time.monotonic()
    with _bili_lock:
        if (not refresh and _bili_cache["data"] is not None
                and now - _bili_cache["at"] < BILI_TTL):
            return _bili_cache["data"]
    try:
        data = bili.describe(cfg)
    except Exception as exc:  # noqa: BLE001
        data = {"logged_in": False, "user": None, "cookie_file": "",
                "error": f"{type(exc).__name__}: {exc}",
                "note": "B站状态查询失败（多半是网络或代理不通）"}
    with _bili_lock:
        _bili_cache["at"], _bili_cache["data"] = now, data
    return data


def bili_status_invalidate() -> None:
    with _bili_lock:
        _bili_cache["at"], _bili_cache["data"] = 0.0, None


# 插件唤起主程序用的「待办提示」：浏览器插件点悬浮按钮后，把链接先放进
# 这里，再把主窗口带到前台；面板轮询到有 hint 就弹出确认框。
# 只留最新一条 —— 插件连点几个视频，用户真正想下的多半是最后一个。
_ui_hint: Dict[str, Any] = {}
_ui_hint_lock = threading.Lock()


def _focus_main_window() -> Tuple[bool, str]:
    """把汲流主窗口带到前台。

    两条路，先优先后者失败的兜底：
      1. pywebview 的窗口对象（原生窗口模式）—— show()/restore() 都试一遍，
         不同版本实现的窗口态不同，单个方法不一定够；
      2. Win32 FindWindow 按标题找（浏览器模式或 webview 对象拿不到时）。
         浏览器模式只能做到「提示用户自己切标签页」—— 浏览器的标签页
         不归我们管，这是诚实的能力边界。
    """
    try:
        import webview
        wins = list(getattr(webview, "windows", []) or [])
        if wins:
            w = wins[0]
            for m in ("show", "restore"):
                fn = getattr(w, m, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
            return True, "webview"
    except Exception:
        pass
    if sys.platform.startswith("win"):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, desktop.TITLE)
            if hwnd:
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)   # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                return True, "win32"
        except Exception:
            pass
    return False, "没有找到主窗口（浏览器模式下请自行切换到汲流标签页）"


def _reveal(target: str, *, mode: str = "reveal") -> Tuple[bool, str]:
    """在系统文件管理器里定位文件、打开文件、或打开目录。

    mode 三态（对应右键菜单的三项，语义必须分开）：
        "open"    用系统默认程序**打开这个文件**（播放器/看图/解压）
        "reveal"  打开所在**文件夹**并把该文件选中（IDM 的「打开所在文件夹」）
        "folder"  直接打开**目录本身**（不选中任何东西）

    区分这三者不是吹毛求疵：以前 open 和 reveal 都走 explorer /select，
    结果「打开文件」永远只是定位到它、并不会真的打开；而已经下完的文件
    想「打开所在文件夹」又因为路径是文件而行为不稳。

    安全边界（这几条不是可选的）：
      1. 调用方只传 t.dest / t.dest_dir，且任务 id 必须先查得到 ——
         不能拿它当「打开任意路径」的跳板。
      2. 空串必须早退，见下面的注释。
      3. 真正挡住网页里乱来的是 Origin 白名单（见 _origin_ok）：
         跨站请求会被 403，浏览器不会替攻击者伪造 Origin。
    """
    raw = str(target or "").strip()
    if not raw:
        # 必须先挡空串：pathlib.Path("") 会变成 "."（当前目录），
        # 而且 .exists() 为真 —— 不拦的话「没给路径」会变成「打开工作目录」。
        return False, "没有可打开的路径"
    p = pathlib.Path(raw).expanduser()
    if not p.exists():
        return False, f"路径不存在：{p}"

    win = sys.platform.startswith("win")
    mac = sys.platform == "darwin"

    try:
        if mode == "open":
            # 用默认程序打开文件；给的是目录就退化成打开目录
            if p.is_file():
                if win:
                    os.startfile(str(p))          # noqa: S606
                elif mac:
                    subprocess.Popen(["open", str(p)])
                else:
                    subprocess.Popen(["xdg-open", str(p)])
            else:
                if win:
                    os.startfile(str(p))          # noqa: S606
                elif mac:
                    subprocess.Popen(["open", str(p)])
                else:
                    subprocess.Popen(["xdg-open", str(p)])
            return True, str(p)

        if mode == "folder":
            # 打开目录本身
            d = p if p.is_dir() else p.parent
            if win:
                os.startfile(str(d))              # noqa: S606
            elif mac:
                subprocess.Popen(["open", str(d)])
            else:
                subprocess.Popen(["xdg-open", str(d)])
            return True, str(d)

        # 默认 reveal：定位（选中）文件或目录
        if win:
            if p.is_file():
                # explorer 的 /select, 后面那个逗号是语法的一部分，别删
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                os.startfile(str(p))              # noqa: S606
        elif mac:
            subprocess.Popen(["open", "-R", str(p)] if p.is_file()
                             else ["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else p)])
        return True, str(p)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _pick_dir(initial: str = "") -> Tuple[bool, str, bool]:
    """弹系统目录选择框。返回 (是否成功, 路径或错误, 是否用户取消)。

    为什么要在后端弹：
        网页里 `input[type=file][webkitdirectory]` 只能拿到**相对路径**，
        拿不到用户机器上的绝对路径 —— 浏览器出于安全刻意不给。
        而我们需要的恰恰是绝对路径。所以由本地服务调系统对话框。

    实现：
        Windows 走 tkinter.filedialog.askdirectory（标准库，无需额外依赖），
        在**独立子进程**里跑 —— tkinter 的 mainloop 与 pywebview 的
        WinForms 消息循环同线程会互相抢消息，另起进程最干净。
        用户在对话框里点了取消，askdirectory 返回空串，这不算错误。

    安全边界：
        只回传用户**亲手选中**的路径，不接受网页指定任意路径回读内容；
        对话框本身是模态的，用户看得见自己在选什么。
    """
    import subprocess as sp
    import sys as _sys

    init = str(initial or "").strip()
    code = (
        "import sys, tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
        "p = filedialog.askdirectory(initialdir=sys.argv[1] or None,\n"
        "                            title='选择下载目录', mustexist=False)\n"
        "root.destroy()\n"
        "sys.stdout.write(p or '')\n"
    )
    try:
        proc = sp.run([_sys.executable, "-c", code, init],
                      capture_output=True, timeout=300)
        # 打包成 exe 后 sys.executable 就是 jiliu.exe，它自己是 GUI 入口
        # 会忽略 -c。这种情况退回用独立 python；没有就明说做不到。
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            if getattr(_sys, "frozen", False):
                return False, ("打包版无法内嵌目录选择框。"
                               "请手动把路径粘到输入框里。"), False
            return False, f"目录选择框打不开：{err[:200]}", False
        chosen = (proc.stdout or b"").decode("utf-8", "replace").strip()
        if not chosen:
            return False, "", True          # 用户取消
        return True, chosen, False
    except sp.TimeoutExpired:
        return False, "目录选择框没有响应（超时）", False
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", False


def _web_dir() -> pathlib.Path:
    """面板静态目录。打包后 web/ 在解包资源目录里，源码运行就在项目下。"""
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cand = pathlib.Path(meipass) / "web"
        if cand.is_dir():
            return cand
    return pathlib.Path(__file__).resolve().parent.parent / "web"


WEB_DIR = _web_dir()

# ---- 慢探测的后台快照 ------------------------------------------------------
#
# /api/state 里唯一「必须等子进程」的部分：七八个外部工具的版本号、
# yt-dlp 有哪几份可用、aria2 的 RPC 通不通。冷启动第一轮 2.5 秒（慢机器上
# 更多），而这个接口是每秒轮询的、也是首屏的必经之路。
#
# 做成快照之后，接口永远立刻返回：有快照给快照，没有给占位（probing=True），
# 后台继续跑。启动时再预热一次，用户真正打开页面时多半已经好了。
_ENV_SNAP: Optional[Any] = None
_ENV_SNAP_KEY: Optional[tuple] = None
_ENV_SNAP_LOCK = threading.Lock()


def _env_key(cfg: Dict[str, Any]) -> tuple:
    """配置里任何影响探测结果的东西。变了就重探。"""
    return tuple(str(cfg.get(k) or "") for k in (
        "ytdlp_path", "ffmpeg_path", "aria2_path", "curl_path", "wget_path",
        "aria2_rpc_url", "aria2_rpc_secret", "motrix_rpc_secret",
        "backend", "engine_order"))


def _probe_env(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """一趟把慢探测全跑完，存进快照。"""
    tl = tools.probe_all(cfg, refresh=True)
    return {
        "tools": tl,
        # 下面三项都会命中上面刚填好的 yt-dlp/ffmpeg 缓存，
        # engines 那边只剩 aria2 的一次 RPC 连接（1.5 秒上限）
        "video": video.available(cfg),
        "engines": engines.describe(cfg),
        "backends": backends.describe(cfg),
    }


def _env_snapshot(cfg: Dict[str, Any]) -> "snapshot.Snapshot":
    global _ENV_SNAP, _ENV_SNAP_KEY
    key = _env_key(cfg)
    with _ENV_SNAP_LOCK:
        if _ENV_SNAP is None or key != _ENV_SNAP_KEY:
            _ENV_SNAP_KEY = key
            _ENV_SNAP = snapshot.Snapshot(
                lambda: _probe_env(cfg), ttl=45.0, name="jiliu-env-probe")
        return _ENV_SNAP


def warm_probes(cfg: Optional[Dict[str, Any]] = None) -> None:
    """启动即预热：不等结果，后台先跑一趟。

    窗口创建 + webview 初始化本身要一到两秒，这段时间够探测跑完了 ——
    用户真正打开页面时 /api/state 直接命中快照，看不到等待。
    """
    try:
        _env_snapshot(cfg or _config.load()).refresh_async()
    except Exception:                                # 预热失败不影响启动
        pass


# 首页是 170KB 的单文件（样式和脚本都内联在里面）。每次请求都读盘 +
# UTF-8 编码 + 走一遍 TCP，在窗口首次渲染时都是白花的力气。
# 缓存住字节，并预先压一份 gzip：浏览器实测能省下 80% 的传输与解码前的拷贝。
_INDEX_CACHE: Dict[str, Any] = {"key": None, "raw": b"", "gz": b"", "etag": ""}


def index_asset() -> Tuple[bytes, bytes, str]:
    """返回 (原始字节, gzip 字节, ETag)。文件没变就不重复读盘。"""
    idx = WEB_DIR / "index.html"
    try:
        st = idx.stat()
        key = (str(idx), int(st.st_mtime), int(st.st_size))
    except OSError:
        return b"", b"", ""
    if _INDEX_CACHE["key"] != key:
        raw = idx.read_bytes()
        _INDEX_CACHE.update({
            "key": key,
            "raw": raw,
            "gz": gzip.compress(raw, 6),
            "etag": '"' + hashlib.sha1(raw).hexdigest()[:16] + '"',
        })
    return _INDEX_CACHE["raw"], _INDEX_CACHE["gz"], _INDEX_CACHE["etag"]


def _video_placeholder(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """video 在探测回来之前的空壳。cookies 是读盘，很快，给真值。"""
    return {
        "available": False, "mode": "-", "path": "", "version": "",
        "source": "", "ffmpeg_dir": "", "js_runtimes": [], "js_ok": False,
        "cookies": cookies.describe(cfg),
        "note": "正在探测 yt-dlp / ffmpeg…", "probing": True,
    }


def _backends_placeholder(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """backends 在探测回来之前的空壳，字段与 describe() 对齐。"""
    return {
        "configured": cfg.get("backend", "auto"),
        "native": {"available": True, "note": "自研多线程分片引擎（零依赖）"},
        "aria2": {"available": False, "binary": "", "binary_version": "",
                  "rpc_ok": False, "rpc_url": "", "motrix": False,
                  "note": "正在探测…"},
        "effective": "native",
        "probing": True,
    }

# 视频平台特征已收敛到 resource.PLATFORMS（那份更全），这里不再重复维护。
MEDIA_EXT = (".mp4", ".mkv", ".webm", ".flv", ".mov", ".avi", ".m4v",
             ".mp3", ".m4a", ".aac", ".flac", ".wav", ".m3u8", ".mpd", ".ts")


def guess_kind(url: str) -> str:
    """判断该走 yt-dlp 还是直链。现在统一由 resource 模块给结论。"""
    plan = resource.classify(url=url)
    if plan.needs_extractor:
        return "video"
    if plan.category == "video" and plan.ext in ("m3u8", "mpd", "f4m", "ism"):
        return "video"
    return "http"


class Handler(BaseHTTPRequestHandler):
    server_version = f"Jiliu/{__version__}"
    protocol_version = "HTTP/1.1"

    tm: TaskManager
    cfg: Dict[str, Any]

    # ---------------- 基础 ----------------

    def log_message(self, fmt, *args):  # 静音 access log
        pass

    # 注意：「客户端半路挂了」的静音**不在这里**做。
    # ThreadingMixIn 捕获异常后调的是 **server 对象**的 handle_error
    # （见 socketserver.process_request_thread），Handler 上写这个是死代码 ——
    # 曾经真这么写过，结果日志照刷 traceback，白修一次。
    # 真正的实现见下面的 HttpServer.handle_error。

    def _masked_cfg(self) -> Dict[str, Any]:
        """出站配置脱敏。

        面板每秒轮询 /api/state，以前每次把 proxy_pass、rpc 密钥、
        Webhook token 全部明文推一遍。前端拿不到真值也不影响使用，
        但没必要让它们一直在回环上裸奔。
        """
        d = dict(self.cfg)
        for k in _SECRET_KEYS:
            if d.get(k):
                d[k] = _MASK
        ch = d.get("notify_channels")
        if isinstance(ch, list):
            rows = []
            for item in ch:
                if not isinstance(item, dict):
                    continue
                it = dict(item)
                for k in ("token", "key", "secret"):
                    if it.get(k):
                        it[k] = _MASK
                rows.append(it)
            d["notify_channels"] = rows
        return d

    def _path_allowed(self, target: str) -> bool:
        """校验类接口的路径白名单：下载目录 / 状态目录 / 任务产物所在目录。"""
        try:
            p = pathlib.Path(target).expanduser().resolve()
        except Exception:  # noqa: BLE001
            return False
        roots = [pathlib.Path(self.cfg.get("download_dir") or ".").expanduser(), _config.HOME]
        try:
            for t in self.tm.list():
                if t.get("dest"):
                    roots.append(pathlib.Path(str(t["dest"])).parent)
        except Exception:  # noqa: BLE001
            pass
        for root in roots:
            try:
                p.relative_to(pathlib.Path(root).resolve())
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin") or ""
        if not origin:
            return True  # 命令行 / curl / 扩展的后台请求通常不带 Origin
        if origin.startswith("chrome-extension://") or origin.startswith("moz-extension://"):
            return True
        # 只认「本服务自己」的 origin。以前是 startswith("http://127.0.0.1:")
        # —— 本机任何端口上的网页都能改配置、建任务（写配置里还有
        # ytdlp_path / after_download_command 这种代码执行入口）。
        try:
            u = urllib.parse.urlparse(origin)
            port = int(u.port or (443 if u.scheme == "https" else 80))
            if u.scheme in ("http", "https") and u.hostname in ("127.0.0.1", "localhost", "::1"):
                return port == int(self.server.server_address[1])
        except Exception:  # noqa: BLE001
            return False
        extra = self.cfg.get("allowed_origins") or []
        return origin in extra

    def _cors(self) -> None:
        origin = self.headers.get("Origin") or ""
        if origin and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def _json(self, code: int, obj: Any) -> None:
        # 协议唤起进来的建任务请求：在这里统一记一条结果。
        # 集中在一个出口记，比在建任务的每个分支各记一次可靠得多
        # （/api/tasks 有七八个 return 点，漏一个就少一条记录）。
        ctx = getattr(self, "_proto_ctx", None)
        if ctx and isinstance(obj, dict):
            try:
                note_protocol_open({"url": ctx.get("url", "")}, obj)
            except Exception:      # noqa: BLE001
                pass
            self._proto_ctx = None
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_index(self, raw: bytes, gz: bytes, etag: str) -> None:
        """首页：支持 gzip 与 304，别让每次打开都重传 170KB。"""
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        accept = (self.headers.get("Accept-Encoding") or "").lower()
        use_gz = bool(gz) and "gzip" in accept
        body = gz if use_gz else raw
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
        # no-cache = 可以存，但每次用之前要问一声（拿到 304 就别重传）
        self.send_header("Cache-Control", "no-cache")
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 32 * 1024 * 1024:
            # 以前按 Content-Length 无上限读入：一个恶意页面就能让进程
            # 分配几百 MB。超限直接断开，让调用方拿到明确失败。
            self.close_connection = True
            return {}
        raw = self.rfile.read(n)
        try:
            d = json.loads(raw.decode("utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _guard(self) -> bool:
        if not self._origin_ok():
            self._json(403, {"ok": False, "error": "Origin 不在白名单，已拒绝"})
            return False
        return True

    # ---------------- 路由 ----------------

    # ==================================================================
    # 1.8.0 新增能力的路由
    #
    # 全部集中在这两个方法里，而不是塞进 do_GET/_post 那条几百行 if 链：
    # 那条链已经够长了，再插十几条分支，以后谁都不敢动。
    # 返回 True 表示「已处理」，调用方直接 return。
    # ==================================================================

    def _deny(self, feature: str) -> bool:
        """Pro 功能闸门。返回 True 表示已经回了响应，调用方直接 return。

        用 402 而不是 403：403 是「你无权」，402 是「这项要付费解锁」——
        前端据此弹「去激活」而不是「操作失败」，用户才知道下一步点哪。
        提示文案由 license.guard 生成，必须带**具体是哪一项**，
        不能只说「需要 Pro」。
        """
        g = _license.guard(feature)
        if g.get("ok"):
            return False
        self._json(402, g)
        return True

    # 「还在这儿动着」的状态。completed / cancelled / error 都不算 —— 它们
    # 不该再影响总进度条。
    _PROGRESS_LIVE = ("running", "queued", "paused", "interrupted")

    def _progress_payload(self) -> Dict[str, Any]:
        """进度悬浮窗用的轻量快照（GET /api/progress）。

        为什么不复用 /api/state：那边要把工具探测、授权、主题、i18n 十几块
        描述拼齐，而这些一秒都不会变。浏览器扩展的悬浮窗每 1~2 秒要看一眼，
        把整份状态序列化一遍属于纯浪费 —— 而且它是**每个标签页**都在看。

        overall 只统计未完成任务。把已完成的历史算进来，百分比会永远卡在
        99.x% 看着像卡住 —— 用户要的是「这一批还剩多少」。
        """
        tasks = self.tm.list(log_tail=0)
        counts: Dict[str, int] = {}
        speed = 0.0
        done_b = 0.0
        size_b = 0.0
        current: Optional[Dict[str, Any]] = None
        rows: List[Dict[str, Any]] = []

        for t in tasks:
            st = str(t.get("status") or "")
            counts[st] = counts.get(st, 0) + 1
            if st not in self._PROGRESS_LIVE:
                continue

            d = float(t.get("done") or 0)
            s = float(t.get("size") or 0)
            if s > 0:
                done_b += min(d, s)
                size_b += s

            if st == "running":
                speed += float(t.get("speed") or 0)
                if current is None:
                    current = {
                        "id": t.get("id"),
                        "filename": t.get("filename") or "",
                        "title": t.get("title") or "",
                        "percent": t.get("percent") or 0,
                        "speed": t.get("speed") or 0,
                        "speed_text": t.get("speed_text") or "-",
                        "eta_text": t.get("eta_text") or "-",
                        "done_text": t.get("done_text") or "-",
                        "size_text": t.get("size_text") or "-",
                        "status": st,
                    }

            if len(rows) < 8:
                # 悬浮窗的「当前文件」视图会在这些行之间轮流展示 ——
                # 同时下三个文件时，用户能看到每一个的进度，而不是永远第一个
                rows.append({
                    "id": t.get("id"),
                    "filename": t.get("filename") or "",
                    "title": t.get("title") or "",
                    "status": st,
                    "percent": t.get("percent") or 0,
                    "speed": t.get("speed") or 0,
                    "speed_text": t.get("speed_text") or "-",
                    "eta_text": t.get("eta_text") or "-",
                    "done_text": t.get("done_text") or "-",
                    "size_text": t.get("size_text") or "-",
                })

        pct = round(done_b * 100.0 / size_b, 1) if size_b > 0 else 0.0
        return {
            "ok": True,
            "version": __version__,
            "ts": int(time.time()),
            "counts": counts,
            "total": len(tasks),
            "active": counts.get("running", 0),
            "queued": counts.get("queued", 0),
            "paused": counts.get("paused", 0),
            "speed": round(speed),
            "speed_text": (human_size(speed) + "/s") if speed else "-",
            "done_bytes": int(done_b),
            "size_bytes": int(size_b),
            "done_text": human_size(done_b) if done_b else "-",
            "size_text": human_size(size_b) if size_b else "-",
            "percent": pct,
            "current": current,
            "tasks": rows,
        }

    def _features_payload(self, tasks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """1.8.0 各项能力的快照。/api/features 与 /api/state 共用一份。

        /api/state 每秒被轮询一次；以前它把任务列表序列化两遍
        （features 里的 queues.summary 一次 + 顶层 tasks 一次），
        300 个任务时白烧一半 CPU。现在由调用方传入同一份。
        """
        if tasks is None:
            tasks = self.tm.list(log_tail=0)
        return {
            "version": __version__,
            "queues": queues.summary(self.cfg, tasks),
            "update": {"pending": update.pending() or {},
                       "kind": update.install_kind(),
                       "can_self_update": update.can_self_update()[0]},
            "shell": shellmenu.status(),
            "finish": finish.status(),
            "presets": presets.describe(self.cfg),
            "channels": channels.describe(self.cfg),
            "plugins": plugins.status(self.cfg),
            "danmaku": danmaku.describe(self.cfg),
            "transfer": transfer.describe(),
            "live": {"enabled": bool(self.cfg.get("live_record", True)),
                     "minutes": self.cfg.get("live_minutes", 0)},
            "drop": {"enabled": bool(self.cfg.get("drop_target")),
                     "running": _DROP[0] is not None},
            "stats": self.tm.stats(),
        }

    def _get_features(self, p: str, q: Dict[str, List[str]]) -> bool:
        one = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731

        if p == "/api/features":
            self._json(200, {"ok": True, **self._features_payload()})
            return True

        if p == "/api/queues":
            self._json(200, {
                "ok": True,
                "queues": queues.summary(self.cfg, self.tm.list(log_tail=0)),
                "slots": queues.total_slots(self.cfg),
                "max_concurrent": int(self.cfg.get("max_concurrent") or 0),
            })
            return True

        if p == "/api/update":
            pend = update.pending()
            self._json(200, {"ok": True, "pending": pend or {},
                             "kind": update.install_kind(),
                             "can_self_update": update.can_self_update()[0],
                             "current": __version__})
            return True

        if p == "/api/update/check":
            force = one("force") not in ("0", "", "false", "no")
            self._json(200, update.check(self.cfg, force=force))
            return True

        if p == "/api/shell":
            self._json(200, {"ok": True, **shellmenu.status()})
            return True

        if p == "/api/finish":
            # status() 只反映「倒计时有没有在走」，用户配的是 config 里的值，
            # 界面要同时看见两者，否则改完再读会显示成 none
            st = finish.status()
            st.update({
                "action": str(self.cfg.get("finish_action") or "none"),
                "armed_action": st.get("action") or "",
                "when_all_done": bool(self.cfg.get("finish_when_all_done", True)),
                "delay": int(self.cfg.get("finish_delay") or 0),
            })
            st["action_text"] = next((a["text"] for a in finish.describe(self.cfg)["actions"]
                                      if a["id"] == st["action"]), st["action"])
            self._json(200, {"ok": True, **st,
                             "describe": finish.describe(self.cfg)})
            return True

        if p == "/api/presets":
            self._json(200, {"ok": True, **presets.describe(self.cfg)})
            return True

        if p == "/api/channels":
            self._json(200, {"ok": True, **channels.describe(self.cfg)})
            return True

        if p == "/api/plugins":
            if one("reload") not in ("0", "", "false", "no"):
                plugins.reload(self.cfg)
            self._json(200, {"ok": True, **plugins.status(self.cfg)})
            return True

        if p == "/api/theme":
            self._json(200, {"ok": True, **theme.apply(self.cfg, one("mode"))})
            return True

        if p == "/api/i18n":
            lang = one("lang") or str(self.cfg.get("ui_lang") or "")
            self._json(200, {"ok": True, "current": i18n.normalize(lang),
                             "languages": i18n.languages(),
                             "strings": i18n.bundle(lang, one("prefix"))})
            return True

        if p == "/api/checksum":
            target = one("path")
            if not target:
                self._json(400, {"ok": False, "error": "缺少 path"})
                return True
            # 以前任意路径都给算：等于一个「文件是否存在 / 内容变没变」的
            # 探测 oracle。只允许下载目录、状态目录和任务产物。
            if not self._path_allowed(target):
                self._json(403, {"ok": False, "error": "只能校验下载目录内的文件"})
                return True
            algo = one("algo")
            r = checksum.compute(target, algo) if algo else checksum.compute_all(target)
            r = _flat_sum(r, algo)
            self._json(200, r)
            return True

        if p == "/api/batch/expand":
            text = one("text")
            self._json(200, batch.expand(text or ""))
            return True

        return False

    def _post_features(self, p: str, b: Dict[str, Any]) -> bool:
        # ---- 更新 ----
        if p == "/api/update/check":
            # GET 是主入口，但「关于」页以前用 POST 调它 → 恒 404。
            # 两个方法都接住，行为一致。
            force = str(b.get("force", "")).strip().lower() not in ("0", "", "false", "no")
            self._json(200, update.check(self.cfg, force=force))
            return True
        if p == "/api/update/download":
            # update.check() 的返回本身就是 info（含 asset/latest/kind），
            # 以前这里找不存在的 chk["info"] → 恒「没有可用的新版本信息」。
            info = b.get("info") or {}
            if not info:
                chk = update.check(self.cfg)
                info = chk if chk.get("asset") else {}
            if not info:
                self._json(400, {"ok": False, "error": "没有可用的新版本信息"})
                return True
            self._json(200, update.stage(info, self.cfg))
            return True
        if p == "/api/update/apply":
            # apply_pending 的签名是 (quiet=False)，以前把 cfg 当第一个
            # 位置参数传进去，返回值又是裸 bool，前端读 j.ok 永远 undefined。
            ok = bool(update.apply_pending())
            self._json(200, {"ok": ok, "error": "" if ok else "没有待安装的更新"})
            if ok:
                # 替换脚本在等本进程退出。以前没人退，180 秒后脚本对
                # 锁定中的 exe 执行 move 失败，又把旧版拉起来 ——
                # 用户以为升级了，其实还是旧版。
                def _exit_for_update() -> None:
                    time.sleep(0.8)
                    try:
                        self.tm.save()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        self.server.shutdown()
                    except Exception:  # noqa: BLE001
                        pass
                    os._exit(0)

                threading.Thread(target=_exit_for_update, daemon=True,
                                 name="jiliu-update-exit").start()
            return True
        if p == "/api/update/discard":
            update.discard()
            self._json(200, {"ok": True})
            return True

        # ---- 系统集成 ----
        if p == "/api/shell":
            if b.get("on"):
                r = shellmenu.register(self.cfg)
                if r.get("ok"):
                    self.cfg["shell_menu"] = True
            else:
                r = shellmenu.unregister()
                if r.get("ok"):
                    self.cfg["shell_menu"] = False
            _safe_save(self.cfg)
            self._json(200, r)
            return True

        if p == "/api/drop":
            self._json(200, _drop_toggle(bool(b.get("on")), self))
            return True

        # ---- 队列 ----
        if p == "/api/queues":
            # 改已有队列（含默认队列的并发）免费；**新开**队列属于 Pro 的多队列调度。
            # 这样免费版的并发仍然可调，收回去的只是「按类别分开排队」。
            _qid = str(b.get("id") or "").strip()
            _exists = any(str(q.get("id")) == _qid
                          for q in queues.normalize(self.cfg))
            if _qid and not _exists and self._deny("queues"):
                return True
            r = queues.upsert(self.cfg, b)
            if r.get("ok"):
                self.cfg["queues"] = r.get("queues") or queues.normalize(self.cfg)
                _safe_save(self.cfg)
            self._json(200, r)
            return True
        if p == "/api/queues/remove":
            r = queues.remove(self.cfg, str(b.get("id") or ""))
            if r.get("ok"):
                _safe_save(self.cfg)
            self._json(200, r)
            return True

        m = re.match(r"^/api/tasks/([^/]+)/queue$", p)
        if m:
            t = self.tm.set_task_queue(m.group(1), str(b.get("queue") or ""))
            self._json(200 if t else 404,
                       {"ok": bool(t), "task": t.to_dict() if t else None,
                        "error": "" if t else "任务不存在"})
            return True

        # ---- 批量 ----
        if p == "/api/batch/probe":
            text = str(b.get("text") or "")
            if b.get("expand"):
                text = batch.expand(text, limit=int(b.get("limit") or 2000)).get("text") or text
            rows = batch.parse(text, self.cfg,
                               limit=int(b.get("limit") or 2000))
            self._json(200, rows)
            return True

        if p == "/api/batch/create":
            text = str(b.get("text") or "")
            urls = b.get("urls") or []
            if not urls:
                urls = batch.parse(text, self.cfg,
                                   limit=int(b.get("limit") or 2000)).get("urls") or []
            # 批量额度：免费版一次 5 条。不是不给用，是给到「够用但不顶事」——
            # 归档场景真正的量级在几百条，5 条只是让人确认能力真的存在。
            if len(urls) > _license.FREE_BATCH_MAX and not _license.requires("batch"):
                st = _license.status()
                self._json(402, {
                    "ok": False, "feature": "batch",
                    "error": f"这次有 {len(urls)} 条，当前是{st.get('label') or '免费版'}，"
                             f"一次最多 {_license.FREE_BATCH_MAX} 条。"
                             f"可以分几批加（下载本身不受影响），"
                             f"或在「设置 › 版本与授权」激活以解除上限。",
                    "limit": _license.FREE_BATCH_MAX,
                })
                return True
            made, errs = [], []
            for u in urls:
                try:
                    t = self.tm.create_http(
                        str(u), dest_dir=str(b.get("dest_dir") or ""),
                        queue=str(b.get("queue") or ""),
                        auto_classify=not bool(b.get("skip_probe")))
                    made.append(t.id)
                except Exception as exc:  # noqa: BLE001
                    errs.append(f"{u}: {type(exc).__name__}: {exc}")
            self._json(200, {"ok": True, "created": len(made),
                             "ids": made, "errors": errs})
            return True

        # ---- 校验和 ----
        m = re.match(r"^/api/tasks/([^/]+)/checksum$", p)
        if m:
            t = self.tm.get(m.group(1))
            if not t:
                self._json(404, {"ok": False, "error": "任务不存在"})
                return True
            target = str(b.get("path") or t.dest or "")
            if not target:
                self._json(400, {"ok": False, "error": "任务还没有产物文件"})
                return True
            algo = str(b.get("algo") or "")
            r = checksum.compute(target, algo) if algo else checksum.compute_all(target)
            r = _flat_sum(r, algo)
            if t.expected_sha256 and r.get("sha256"):
                r["compare"] = checksum.compare(r["sha256"], t.expected_sha256)
            self._json(200, r)
            return True

        # ---- 站点预设 ----
        if p == "/api/presets":
            opts = dict(b.get("opts") or {})
            url = str(b.get("url") or b.get("host") or "")
            if b.get("forget"):
                r = presets.forget(self.cfg, url)
            else:
                r = presets.remember(self.cfg, url, opts)
            r["presets"] = presets.normalize(self.cfg)
            _safe_save(self.cfg)
            self._json(200, r)
            return True

        # ---- 通知渠道 ----
        if p == "/api/channels":
            if self._deny("channels"):
                return True
            r = channels.add(self.cfg, b)
            if r.get("ok"):
                _safe_save(self.cfg)
            self._json(200, r)
            return True
        if p == "/api/channels/remove":
            r = channels.remove(self.cfg, str(b.get("id") or ""))
            if r.get("ok"):
                _safe_save(self.cfg)
            self._json(200, r)
            return True
        if p == "/api/channels/test":
            rows = channels.notify_sync(self.cfg, "汲流 · 测试通知",
                                        "这是一条测试消息，收到说明配置正确。",
                                        event="test")
            sent = [x for x in (rows or []) if isinstance(x, dict) and x.get("ok")]
            self._json(200, {"ok": bool(sent), "sent": len(sent),
                             "total": len(rows or []), "results": rows or [],
                             "error": "" if sent else ("没有启用的渠道"
                                                       if not rows else "全部渠道都失败了")})
            return True

        # ---- 插件 ----
        if p == "/api/plugins/reload":
            self._json(200, {"ok": True, "plugins": plugins.reload(self.cfg)})
            return True
        if p == "/api/plugins/toggle":
            r = plugins.set_enabled(str(b.get("key") or ""), bool(b.get("on")),
                                    self.cfg)
            self._json(200, r)
            return True

        # ---- 完成动作 ----
        if p == "/api/finish":
            if b.get("action"):
                self.cfg["finish_action"] = str(b["action"])
            if b.get("when_all_done") is not None:
                self.cfg["finish_when_all_done"] = bool(b["when_all_done"])
            if b.get("delay") is not None:
                self.cfg["finish_delay"] = max(0, int(b["delay"]))
            _safe_save(self.cfg)
            st = finish.status()
            st.update({"action": str(self.cfg.get("finish_action") or "none"),
                       "when_all_done": bool(self.cfg.get("finish_when_all_done", True)),
                       "delay": int(self.cfg.get("finish_delay") or 0)})
            self._json(200, {"ok": True, **st})
            return True
        if p == "/api/finish/cancel":
            self._json(200, finish.cancel())
            return True

        # ---- 直播 ----
        if p == "/api/live":
            if self._deny("live"):
                return True
            url = str(b.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return True
            try:
                t = self.tm.create_live(url, minutes=float(b.get("minutes") or 0),
                                        dest_dir=str(b.get("dest_dir") or ""),
                                        filename=str(b.get("filename") or ""),
                                        queue=str(b.get("queue") or ""))
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"ok": False,
                                 "error": f"{type(exc).__name__}: {exc}"})
                return True
            self._json(200, {"ok": True, "task": t.to_dict()})
            return True

        # ---- 导入导出 ----
        if p == "/api/tasks/export":
            ids = b.get("ids") or None
            self._json(200, self.tm.export_tasks(ids, str(b.get("fmt") or "json")))
            return True
        if p == "/api/tasks/import":
            self._json(200, self.tm.import_tasks(
                str(b.get("text") or ""), fmt=str(b.get("fmt") or "auto"),
                queue=str(b.get("queue") or ""),
                dest_dir=str(b.get("dest_dir") or ""),
                start=bool(b.get("start"))))
            return True

        # ---- 外观 ----
        if p == "/api/theme":
            for k in ("ui_theme", "ui_mode", "ui_accent"):
                if b.get(k) is not None:
                    self.cfg[k] = str(b[k])
            _safe_save(self.cfg)
            self._json(200, {"ok": True, **theme.apply(self.cfg)})
            return True
        if p == "/api/i18n":
            if b.get("lang"):
                self.cfg["ui_lang"] = i18n.normalize(str(b["lang"]))
                _safe_save(self.cfg)
            lang = self.cfg.get("ui_lang") or i18n.DEFAULT_LANG
            self._json(200, {"ok": True, "current": i18n.normalize(lang),
                             "strings": i18n.bundle(lang)})
            return True

        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path.rstrip("/") or "/"
        q = urllib.parse.parse_qs(u.query)

        if p == "/":
            raw, gz, etag = index_asset()
            if not raw:
                self._html(500, "<h1>web/index.html 缺失</h1>")
                return
            self._send_index(raw, gz, etag)
            return

        if not self._guard():
            return

        # 1.8.0 的新接口先过一遍，命中就到此为止
        if self._get_features(p, q):
            return

        if p == "/api/health":
            self._json(200, {"ok": True, "app": APP_NAME, "version": __version__})
        elif p == "/api/progress":
            # 浏览器扩展的进度悬浮窗轮询这一个。刻意比 /api/state 轻：
            # 悬浮窗只关心速度 / 当前文件 / 总百分比三个数。
            self._json(200, self._progress_payload())
        elif p == "/api/state":
            # 慢探测走快照：最多为它等 0.2 秒，超时就用占位。窗口是秒开的，
            # 页面不该在这几秒里空白 —— 探测完成后下一轮轮询自然补上。
            env = _env_snapshot(self.cfg).get(wait=0.2)
            tasks = self.tm.list(log_tail=0)
            self._json(200, {
                "ok": True,
                "version": __version__,
                "tasks": tasks,
                "stats": self.tm.stats(),
                "config": self._masked_cfg(),
                "tools": (env or {}).get("tools") or {"probing": True},
                "backends": (env or {}).get("backends") or _backends_placeholder(self.cfg),
                "video": (env or {}).get("video") or _video_placeholder(self.cfg),
                "categories": resource.describe_categories(),
                "state_dir": str(_config.HOME),
                # 首屏就把状态栏要的东西给全，省掉一串串行请求
                "bili": bili_status(self.cfg),
                "proxy": proxy.describe(self.cfg, ""),
                "cookies": cookies.describe(self.cfg, ""),
                "history": history().stats(),
                "naming": {"variables": naming.variables(),
                           "name_pattern": self.cfg.get("name_pattern"),
                           "dir_pattern": self.cfg.get("dir_pattern")},
                # 引擎总览、版本形态、归档布局 —— 界面首屏就要用
                "engines": (env or {}).get("engines") or engines.placeholder(self.cfg),
                "edition": edition.describe(self.cfg),
                # 授权与「关于」：首屏就给，设置页不用再拉一次
                "license": _license.status(),
                "about": about.describe(self.cfg),
                "layout": layout.describe(self.cfg),
                "desktop": desktop.describe(self.cfg),
                "schedule": schedule.describe(self.cfg),
                # 协议唤起：设置页要看注册状态，界面要看最近一次唤起的结果
                "urltake": {**urltake.describe(), "recent": _OPEN_RECENT[-5:]},
                "subscriptions": {"count": len(subscriptions.list_all()),
                                  "items": subscriptions.list_all(),
                                  "check_minutes": int(self.cfg.get("feed_check_minutes", 60) or 0)},
                "sites": {"total": len(sites.SITES),
                          "domestic": len(sites.domestic_sites()),
                          "foreign": len(sites.foreign_sites())},
                # 1.8.0 的各项能力：首屏一起给，设置页才不用逐块拉
                "features": self._features_payload(tasks),
                "theme": theme.apply(self.cfg),
                "i18n": i18n.describe(self.cfg),
            })
        elif p == "/api/categories":
            self._json(200, {"ok": True, "categories": resource.describe_categories()})

        # ---- 缺失组件的按需获取（引擎页的「下载安装」）----
        elif p == "/api/tools/fetch":
            self._json(200, fetch_tools.describe(self.cfg))

        # ---- 授权状态 ----
        elif p == "/api/license":
            self._json(200, {"ok": True, **_license.status()})

        # ---- 诊断包：出问题时一键打包 ----
        elif p == "/api/diagnose":
            self._json(200, about.export_diagnostics(self.cfg))

        # ---- 下载引擎总览 ----
        elif p == "/api/engines":
            force = (q.get("refresh") or ["0"])[0] not in ("0", "", "false", "no")
            if force:
                tools.invalidate_probe_cache()
                backends.invalidate_cache()
            ed = engines.describe(self.cfg, refresh=force)
            if force:
                # 刚重算过，直接把结果写回快照 —— 接着轮询的 /api/state
                # 立刻就是新值，不必再等后台跑一趟，也不会短暂回到「探测中」
                _env_snapshot(self.cfg).set({
                    "tools": tools.probe_all(self.cfg),
                    "video": video.available(self.cfg),
                    "engines": ed,
                    "backends": backends.describe(self.cfg),
                })
            self._json(200, {"ok": True, **ed})

        # ---- 引擎没装时怎么装（环境体检的「下载」按钮）----
        elif p == "/api/engines/install":
            key = (q.get("key") or [""])[0].strip()
            if not key:
                # 不带 key 就把所有引擎的安装信息一次给全，
                # 免得前端为了渲染一排按钮来回请求
                self._json(200, {
                    "ok": True,
                    "all": {e["key"]: engines.install_info(e["key"])
                            for e in engines.CATALOG},
                })
            else:
                self._json(200, engines.install_info(key))

        # ---- 版本形态（国内版 / 全球版）----
        elif p == "/api/edition":
            self._json(200, {"ok": True, **edition.describe(self.cfg)})

        # ---- 视频站点表 ----
        elif p == "/api/pagesniff":
            # 能力清单（六种可解析形态 + 四类明确不做）。界面「关于」与
            # 解析面板的提示文案都从这里取，避免前后端各写一份。
            self._json(200, {"ok": True, **pagesniff.describe()})
        elif p == "/api/sites":
            self._json(200, {"ok": True, **sites.describe()})

        # ---- 计划任务：现在该不该跑 ----
        elif p == "/api/schedule":
            self._json(200, {"ok": True, **schedule.describe(self.cfg)})

        # ---- 订阅列表 ----
        elif p == "/api/subscriptions":
            self._json(200, {
                "ok": True,
                "items": subscriptions.list_all(),
                "check_minutes": int(self.cfg.get("feed_check_minutes", 60) or 0),
                "last": (_FEED_WATCHER.last_result if _FEED_WATCHER else {}),
            })

        # ---- 接管规则（扩展启动时拉一次）----
        elif p == "/api/capture/rules":
            self._json(200, {"ok": True, **capture_rules.describe(self.cfg)})

        # ---- 本地嗅探代理状态 ----
        elif p == "/api/sniff":
            self._json(200, {"ok": True, **sniffproxy.status(self.cfg),
                             "recent": sniffproxy.recent(30)})

        # ---- 剪贴板最近抓到的链接 ----
        elif p == "/api/clipboard":
            self._json(200, {"ok": True, "enabled": bool(self.cfg.get("clipboard_watch")),
                             "recent": _CLIP_RECENT[-20:]})

        # ---- 站点抓取任务状态 ----
        elif p.startswith("/api/grab/") and len(p.split("/")) >= 4:
            jid = p.split("/")[3]
            job = grabber.get(jid)
            self._json(200 if job else 404, {"ok": bool(job), "job": job})

        # ---- 归档布局 ----
        elif p == "/api/layout":
            url = (q.get("url") or [""])[0]
            cat = (q.get("category") or ["video"])[0]
            out = layout.describe(self.cfg)
            if url:
                plan = resource.classify(url=url).to_dict()
                cat = plan.get("category") or cat
            base = _config.download_dir(self.cfg)
            out["base"] = str(base)
            out["example"] = {
                "category": cat,
                "folder": layout.folder_name(cat, self.cfg),
                "path": str(layout.target_dir(base, cat, self.cfg)),
            }
            self._json(200, {"ok": True, **out})

        # ---- 桌面窗口能力 ----
        elif p == "/api/desktop":
            self._json(200, {"ok": True, **desktop.describe(self.cfg)})

        # ---- 环境体检：代理 / Cookie / 工具 / 后端，一次拿全 ----
        elif p == "/api/env":
            force = (q.get("refresh") or ["0"])[0] not in ("0", "", "false", "no")
            if force:
                proxy.invalidate_cache()
            near = proxy.describe(self.cfg, "")
            self._json(200, {
                "ok": True,
                "proxy": {
                    "config": {k: self.cfg.get(k) for k in
                               ("proxy_mode", "proxy", "proxy_domestic", "proxy_foreign")},
                    "near": near,
                    "env": proxy.detect_env(refresh=force).to_dict(),
                },
                "cookies": cookies.describe(self.cfg, ""),
                "cookie_files": cookies.list_files(),
                "browsers": cookies.available_browsers(),
                "bili": bili_status(self.cfg, refresh=force),
                "js_runtimes": video.js_runtimes(refresh=force),
                "engines": engines.describe(self.cfg, refresh=force),
                "desktop": desktop.describe(self.cfg),
                "layout": layout.describe(self.cfg),
                "naming": {"variables": naming.variables(),
                           "name_pattern": self.cfg.get("name_pattern"),
                           "dir_pattern": self.cfg.get("dir_pattern"),
                           "preview": naming.check(self.cfg.get("name_pattern") or "")},
                "history": history().stats(),
            })

        # ---- 代理 ----
        elif p == "/api/proxy":
            url = (q.get("url") or [""])[0]
            self._json(200, {
                "ok": True,
                "selected": proxy.select_info(self.cfg, url).to_dict(),
                "env": proxy.detect_env().to_dict(),
                "for": url,
            })

        # ---- Cookie ----
        elif p == "/api/cookies":
            url = (q.get("url") or [""])[0]
            self._json(200, {
                "ok": True,
                "files": cookies.list_files(),
                "browsers": cookies.available_browsers(),
                "current": cookies.describe(self.cfg, url),
                "for": url,
            })

        # ---- 命名模板 ----
        elif p == "/api/naming":
            pat = (q.get("pattern") or [self.cfg.get("name_pattern") or ""])[0]
            self._json(200, {"ok": True, "variables": naming.variables(),
                             "check": naming.check(pat),
                             "name_pattern": self.cfg.get("name_pattern"),
                             "dir_pattern": self.cfg.get("dir_pattern")})

        # ---- 已下载记录 ----
        elif p == "/api/history":
            h = history()
            self._json(200, {"ok": True, "stats": h.stats(),
                             "items": h.list(q=(q.get("q") or [""])[0],
                                             limit=int((q.get("limit") or ["300"])[0]))})

        # ---- B 站 ----
        elif p == "/api/bili":
            force = (q.get("refresh") or ["0"])[0] not in ("0", "", "false", "no")
            self._json(200, {"ok": True, **bili_status(self.cfg, refresh=force)})

        # ---- 多平台账号登录 ----
        elif p == "/api/accounts":
            self._json(200, accounts.list_accounts(self.cfg))

        # ---- 插件唤起的待办提示（读一次即清空）----
        elif p == "/api/ui/hint":
            with _ui_hint_lock:
                hint = dict(_ui_hint)
                _ui_hint.clear()
            self._json(200, {"ok": True, "hint": hint or None})

        elif p == "/api/bili/resolve":
            url = (q.get("url") or [""])[0]
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            self._json(200, {"ok": True, "result": bili.resolve(self.cfg, url)})

        # ---- 抖音无水印直链（公开分享页路子，见 jiliu/douyin.py）----
        elif p == "/api/douyin/resolve":
            url = (q.get("url") or [""])[0]
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            self._json(200, douyin.resolve(url, self.cfg))

        elif p == "/api/tasks":
            tail = int((q.get("log") or ["0"])[0])
            self._json(200, {"ok": True, "tasks": self.tm.list(log_tail=tail)})
        elif p.startswith("/api/tasks/"):
            tid = p.split("/")[3]
            t = self.tm.get(tid)
            if not t:
                self._json(404, {"ok": False, "error": "任务不存在"})
            else:
                self._json(200, {"ok": True, "task": t.to_dict(log_tail=100)})
        elif p == "/api/config":
            self._json(200, {"ok": True, "config": self._masked_cfg()})
        elif p == "/api/tools":
            # ?refresh=1 强制重扫 —— 用户刚把 yt-dlp.exe 丢进 bin/ 时要能立刻认出来
            force = (q.get("refresh") or ["0"])[0] not in ("0", "", "false", "no")
            if force:
                tools.invalidate_probe_cache()
                backends.invalidate_cache()
            self._json(200, {"ok": True,
                             "tools": tools.probe_all(self.cfg, refresh=force),
                             "age": tools.probe_cached_age(),
                             "backends": backends.describe(self.cfg, refresh=force)})
        else:
            self._json(404, {"ok": False, "error": "未知路径", "path": p})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path.rstrip("/") or "/"
        if not self._guard():
            return
        # ---- aria2 兼容 RPC（LinkSwift 等脚本推送入口）----
        # 单独走：请求体可能是 JSON-RPC 批量数组，_body() 接不住。
        if p in ("/rpc", "/jsonrpc"):
            try:
                self._rpc()
            except Exception as exc:  # noqa: BLE001
                self._rpc_out({"jsonrpc": "2.0", "id": None,
                               "error": {"code": -32603,
                                         "message": f"{type(exc).__name__}: {exc}"}})
            return
        try:
            self._post(p)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    # ---------------- aria2 兼容 RPC ----------------

    def _rpc_out(self, obj: Any) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _rpc(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._rpc_out({"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32700, "message": "Parse error"}})
            return
        if isinstance(req, list):
            out = [self._rpc_one(r) for r in req if isinstance(r, dict)]
        else:
            out = self._rpc_one(req if isinstance(req, dict) else {})
        self._rpc_out(out)

    def _rpc_one(self, req: Dict[str, Any]) -> Dict[str, Any]:
        rid = req.get("id")
        method = str(req.get("method") or "")
        params = req.get("params") or []
        if not isinstance(params, list):
            params = [params]

        # token 鉴权：配置了 rpc_secret 就必须 token:xxx 且对上；
        # 没配就谁来都行（端口只绑 127.0.0.1，风险可控）。
        secret = str(self.cfg.get("rpc_secret") or "")
        if params and isinstance(params[0], str) and params[0].startswith("token:"):
            if secret and params[0][6:] != secret:
                return self._rpc_err(rid, 1, "RPC 密钥不对")
            params = params[1:]
        elif secret:
            return self._rpc_err(rid, 1, "需要 token: 密钥（汲流设置里配置了 rpc_secret）")

        try:
            result = self._rpc_dispatch(method, params)
        except Exception as exc:  # noqa: BLE001
            return self._rpc_err(rid, 1, f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _rpc_err(rid: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": code, "message": message}}

    def _rpc_dispatch(self, method: str, params: List[Any]) -> Any:
        if method in ("aria2.getVersion", "system.getVersion"):
            return {"version": f"1.36.0-jiliu/{__version__}",
                    "enabledFeatures": ["Multi-Connection", "Async DNS",
                                        "jiliu-rpc-shim"]}
        if method in ("aria2.addUri", "aria2.addUriWithOptions"):
            return self._rpc_add(params)
        if method == "aria2.tellStatus":
            return self._rpc_tell(params)
        if method == "aria2.getGlobalStat":
            try:
                ts = self.tm.list()
            except Exception:  # noqa: BLE001
                ts = []
            active = [t for t in ts if t.get("status") == "running"]
            queued = [t for t in ts if t.get("status") in ("queued", "paused")]
            speed = sum(float(t.get("speed") or 0) for t in active)
            return {"numActive": str(len(active)),
                    "numWaiting": str(len(queued)),
                    "numStopped": str(len(ts) - len(active) - len(queued)),
                    "numStoppedTotal": str(len(ts) - len(active) - len(queued)),
                    "downloadSpeed": str(int(speed)),
                    "uploadSpeed": "0"}
        if method in ("aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped",
                      "aria2.getPeers", "aria2.getServers"):
            return []
        if method in ("aria2.changeGlobalOption", "aria2.changeUri",
                      "aria2.remove", "aria2.forceRemove", "aria2.pause",
                      "aria2.unpause"):
            # 接受但不动真格 ——汲流自己的任务管理走自己的 API
            return "OK"
        if method == "system.listMethods":
            return ["aria2.getVersion", "aria2.addUri", "aria2.addUriWithOptions",
                    "aria2.tellStatus", "aria2.getGlobalStat", "system.listMethods"]
        raise ValueError(f"不支持的方法: {method}")

    def _rpc_add(self, params: List[Any]) -> str:
        """addUri [uris, {options}] → 汲流任务，返回 gid（=任务 id）。"""
        uris = params[0] if params else []
        opts = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
        url = ""
        for u in uris if isinstance(uris, list) else [uris]:
            if isinstance(u, str) and u.strip():
                url = u.strip()
                break
        if not url:
            raise ValueError("没有可用的下载地址")

        headers: Dict[str, str] = {}
        for line in (opts.get("header") or []):
            if isinstance(line, str) and ":" in line:
                k, v = line.split(":", 1)
                k = k.strip().title().replace("X-", "X-")
                headers[k] = v.strip()
        for src, dst in (("user-agent", "User-Agent"), ("referer", "Referer"),
                         ("cookie", "Cookie")):
            if opts.get(src):
                headers[dst] = str(opts[src])

        out = str(opts.get("out") or "").strip().replace("\\", "/")
        if "/" in out:
            out = out.rsplit("/", 1)[-1]
        dest_dir = str(opts.get("dir") or "").strip()

        t = self.tm.create_http(
            url,
            filename=out,
            dest_dir=dest_dir,
            headers=headers or None,
            meta={"from": "aria2-rpc", "alt_urls": [
                u for u in (uris if isinstance(uris, list) else [uris])
                if isinstance(u, str) and u.strip() and u.strip() != url]},
            auto_classify=not out,
        )
        return str(t.id)

    def _rpc_tell(self, params: List[Any]) -> Dict[str, Any]:
        gid = str(params[0]) if params else ""
        t = self.tm.get(gid)
        if t is None:
            raise ValueError(f"任务不存在: {gid}")
        st_map = {"queued": "waiting", "paused": "paused", "running": "active",
                  "done": "complete", "error": "error",
                  "cancelled": "removed"}
        return {"gid": gid,
                "status": st_map.get(t.status, t.status),
                "totalLength": str(t.size or 0),
                "completedLength": str(t.done or 0),
                "uploadLength": "0",
                "downloadSpeed": str(int(t.speed or 0)),
                "connections": str(t.threads or 1),
                "numPieces": "0", "pieceLength": "0",
                "errorMessage": t.error or "",
                "dir": t.dest_dir or "",
                "files": [{"index": "1", "path": t.dest or t.filename,
                           "length": str(t.size or 0),
                           "completedLength": str(t.done or 0),
                           "selected": "true",
                           "uris": [{"uri": t.url, "status": "used"}]}]}

    def _post(self, p: str) -> None:
        b = self._body()

        if self._post_features(p, b):
            return

        # ---- 激活 / 取消激活（离线验签，全程不联网）----
        if p == "/api/license":
            r = _license.activate(str(b.get("key") or ""))
            if not r.get("ok"):
                self._json(400, r)
                return
            self._json(200, {"ok": True, **_license.status()})
            return
        if p == "/api/license/clear":
            _license.deactivate()
            self._json(200, {"ok": True, **_license.status()})
            return

        # ---- 组件下载：起一个后台线程，前端轮询 /api/tools/fetch ----
        if p == "/api/tools/fetch":
            self._json(200, fetch_tools.start(str(b.get("key") or ""), self.cfg))
            return
        if p == "/api/tools/fetch/cancel":
            self._json(200, fetch_tools.cancel(str(b.get("key") or "")))
            return

        # ---- 新建任务 ----
        if p == "/api/tasks":
            url = (b.get("url") or "").strip()
            # 标记来源，供 _json 出口统一记账（见 _json 里的说明）
            self._proto_ctx = ({"url": url} if b.get("from") == "protocol" else None)
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return

            # ---- RSS/Atom 订阅源 → 条目清单（同文件夹模式：不建任务，回清单）----
            # 触发：kind=feed 显式指定，或 URL 特征像 feed（/feed、.xml…）。
            # 探测后不是 feed 的（比如一个 .xml 直链文件）就放行走正常下载。
            if (b.get("kind") == "feed" or feed.looks_feed_url(url)) \
                    and not b.get("force"):
                fres = feed.try_inspect(url, self.cfg)
                if fres.get("kind") == "feed":
                    self._json(200, {
                        "ok": True, "kind": "feed",
                        "title": fres.get("title") or "订阅源",
                        "items": fres.get("items") or [],
                        "total": fres.get("total") or 0,
                    })
                    return
                if b.get("kind") == "feed":
                    # 用户明确说是 feed 但解析失败 → 如实报错，不能瞎建任务
                    self._json(400, {"ok": False, "error": fres.get("error"),
                                     "hint": fres.get("hint")})
                    return

            # ---- 网盘分享页拦截（夸克/百度/阿里…）----
            # 分享页本身是 HTML。不拦的话任务会「下载成功」，落盘的却是
            # 一个网页 —— 主人遇到的「夸克链接下载成功但不是对的文件」
            # 就是这个。在入口拦下，把下一步怎么做说清楚。
            # 例外：蓝奏云公开分享页**可以直接解析**（免登录、无绕过，
            # 浏览器能打开的就能解析）—— 先解析，拿到干净直链再建任务。
            resolved_netdisk: Dict[str, Any] = {}
            if not b.get("force"):
                det = protocols.detect(url)
                if det.kind == "netdisk":
                    if netdisk.is_lanzou(url):
                        res = netdisk.try_resolve(
                            url, self.cfg, pwd=(b.get("pwd") or ""))
                        if res.get("kind") == "folder":
                            # 文件夹分享：不建任务，把清单递给前端挑
                            self._json(200, {
                                "ok": True, "kind": "folder",
                                "name": res.get("name") or "蓝奏云文件夹",
                                "items": res.get("items") or [],
                                "total": res.get("total") or 0,
                                "note": res.get("note") or "",
                            })
                            return
                        if res.get("ok"):
                            resolved_netdisk = res
                            url = res["url"]
                        else:
                            self._json(400, {
                                "ok": False,
                                "blocked": True,
                                "netdisk": "蓝奏云",
                                "error": res.get("error") or "解析失败",
                                "hint": res.get("hint") or "",
                                "dead": bool(res.get("dead")),
                                "need_pwd": bool(res.get("need_pwd")),
                            })
                            return
                    else:
                        self._json(400, {
                            "ok": False,
                            "blocked": True,
                            "netdisk": det.label,
                            "name": det.label.replace("链接", ""),
                            "error": (det.error
                                      or "分享页是一个普通网页，不是文件直链 —— "
                                         "硬下只会得到一份 HTML"),
                            "note": det.note or "",
                            "guide": [
                                "网盘分享页 = 一个普通网页，不是文件直链 —— "
                                "硬下只会得到一份 HTML",
                                "在浏览器打开分享页并登录，勾选文件后转存到自己的网盘",
                                "用网盘官方客户端下载；或先想办法拿到文件直链",
                                "拿到真正的直链（URL 直接打开就是文件内容）后，"
                                "汲流可以多线程下载它",
                                "蓝奏云的公开分享链接可以直接粘进来 —— 那个能自动解析",
                            ],
                        })
                        return
            # 版本门禁：国内版遇到明确的海外站点，这里就拦下并说清原因，
            # 而不是让它跑到一半再报一个看不懂的错
            gate = edition.gate(url, self.cfg)
            if not gate.get("allowed", True):
                self._json(400, {"ok": False, "error": gate["reason"],
                                 "blocked": True, "foreign": gate.get("foreign")})
                return
            kind = (b.get("kind") or "").strip() or guess_kind(url)

            # 合集：优先**逐条按各自的 URL 建任务**。
            #
            # 为什么不继续用 --playlist-items：
            #   那个参数里的序号，是 yt-dlp **自己**在 URL 上解析出的
            #   playlist 里的位置；而界面上的序号来自 bili.resolve 的
            #   ugc_season / pages 顺序。两者是两套坐标系，不一定对齐。
            #   错位时的表现就是主人遇到的：「勾了 3 集，只下了第一个」。
            # 而每条 entry 本来就自带独立 url，逐条建任务既准确又能
            # 各自显示进度、各自暂停/重试，比塞给一次 yt-dlp 调用更好管。
            # 只有拿不到 url 时（少数形态）才退回 --playlist-items。
            urls = b.get("urls")
            if kind == "video" and isinstance(urls, (list, tuple)) and urls:
                group = (b.get("group_title") or b.get("title") or "").strip()
                # 每条自己的标题（合集展开时界面就知道了的）。
                # 任务栏按集显示各自标题；group_title 只做目录/兜底展示。
                titles = b.get("titles")
                if not isinstance(titles, (list, tuple)):
                    titles = []
                made = []
                for i, u in enumerate(urls):
                    u = str(u or "").strip()
                    if not u:
                        continue
                    own_title = str(titles[i]).strip() if i < len(titles) else ""
                    sub_gate = edition.gate(u, self.cfg)
                    if not sub_gate.get("allowed", True):
                        continue
                    made.append(self.tm.create_video(
                        u,
                        fmt=(b.get("format") or "").strip(),
                        dest_dir=(b.get("dest_dir") or "").strip(),
                        title=own_title,
                        # 逐条下时**不启用** playlist 语义：每条就是一个普通
                        # 视频，避免 yt-dlp 又从单集页里展开出整个合集
                        playlist=False,
                        items="",
                        group_title=group,
                        audio_only=bool(b.get("audio_only")),
                        subs_only=bool(b.get("subs_only")),
                        queue=(b.get("queue") or "").strip(),
                        meta=_task_meta(b),
                    ).id)
                if made:
                    self._json(200, {
                        "ok": True,
                        "count": len(made),
                        "mode": "per-entry",
                        "ids": made,
                    })
                    return
                # 全部被门禁拦掉
                self._json(400, {"ok": False,
                                 "error": "选中的条目都属于海外站点，当前是国内版"})
                return

            if kind == "video":
                # 回退路径：没有逐条 url，只能整体交给 yt-dlp
                items = b.get("items")
                if isinstance(items, (list, tuple)):
                    items = video.playlist_item_spec([int(x) for x in items])
                else:
                    items = str(items or "").strip()
                t = self.tm.create_video(
                    url,
                    fmt=(b.get("format") or "").strip(),
                    dest_dir=(b.get("dest_dir") or "").strip(),
                    title=(b.get("title") or "").strip(),
                    playlist=bool(b.get("playlist")),
                    items=items,
                    group_title=(b.get("group_title") or "").strip(),
                    audio_only=bool(b.get("audio_only")),
                    subs_only=bool(b.get("subs_only")),
                    queue=(b.get("queue") or "").strip(),
                    meta=_task_meta(b),
                )
            else:
                t = self.tm.create_http(
                    url,
                    threads=int(b["threads"]) if b.get("threads") else None,
                    filename=((b.get("filename") or "").strip()
                              or str(resolved_netdisk.get("name") or "")),
                    dest_dir=(b.get("dest_dir") or "").strip(),
                    backend=(b.get("backend") or "").strip(),
                    category=(b.get("category") or "").strip(),
                    chunk_size=int(b["chunk_size"]) if b.get("chunk_size") else 0,
                    verify_hash=bool(b.get("verify_hash")),
                    expected_sha256=(b.get("sha256") or b.get("expected_sha256") or "").strip(),
                    # 插件已经判过类型就把结论带过来，省一次探测。
                    #
                    # 协议唤起（from=protocol）额外强制跳过**联网**探测：
                    # 那是同步 HEAD/RANGE，碰上慢站点能耗掉两三秒（实测
                    # example.com 2.3s）。用户点网页链接时浏览器在等这个
                    # 请求返回，等三秒的感受就是「点了没反应」，还以为
                    # 没装上。离线识别仍在 create_http 里照常做，任务类型
                    # 标签、归档目录、默认线程数都不受影响；至于文件大小和
                    # 是否支持分片，下载线程真发请求时本来就会拿到。
                    auto_classify=((b.get("auto_classify", True) is not False)
                                   and b.get("from") != "protocol"),
                    headers=resolved_netdisk.get("headers") or None,
                    queue=(b.get("queue") or "").strip(),
                    meta=_task_meta(b),
                )
            self._json(200, {"ok": True, "task": t.to_dict(), "kind": kind,
                             **({"netdisk_resolved": resolved_netdisk}
                                if resolved_netdisk else {})})
            return

        # ---- 网盘分享页解析预览（不建任务）----
        # body: {"url": "https://lanzoui.com/xxx", "pwd": ""}
        # 给前端「先看看这是什么文件」用。蓝奏云之外一律 skip。
        if p == "/api/netdisk/resolve":
            rurl = (b.get("url") or "").strip()
            if not rurl:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            self._json(200, netdisk.try_resolve(
                rurl, self.cfg, pwd=(b.get("pwd") or "")))
            return

        # ---- 协议唤起（jiliu://）：注册 / 注销 / 自检 ----
        #
        # body: {"action": "register" | "unregister" | "test",
        #        "spec": "jiliu://download?url=..."}
        # 注册只动 HKCU，不提权；"test" 走一遍完整解析 + 建任务，
        # 用来验证链路（也方便自动化测试点一下）。
        if p == "/api/urltake":
            action = (b.get("action") or "").strip().lower()
            if action == "register":
                r = urltake.register()
                self._json(200 if r.get("ok") else 500, {**r, "status": urltake.status()})
                return
            if action == "unregister":
                r = urltake.unregister()
                self._json(200 if r.get("ok") else 500, {**r, "status": urltake.status()})
                return
            if action == "test":
                spec = str(b.get("spec") or b.get("url") or "").strip()
                info = urltake.parse(spec)
                if not info.get("ok"):
                    self._json(400, {"ok": False, "error": info.get("error"),
                                     "parsed": info})
                    return
                self._json(200, {"ok": True, "parsed": {
                    "url": info["url"], "referer": info.get("referer", ""),
                    "title": info.get("title", ""), "kind": info.get("kind", "")}})
                return
            self._json(400, {"ok": False, "error": "action 只能是 register / unregister / test"})
            return

        # ---- 播放页解析：网页 → 可下载的流地址（不建任务）----
        #
        # body: {"url": "https://站点/vodplay/1-1-1.html",
        #        "depth": 2, "probe_quality": true,
        #        "headers": {"Cookie": "...", "Referer": "..."}}
        # 只解析、不下单：界面拿到候选清单后由用户挑一条再建任务，
        # 避免"点一下就开始下"下错东西。
        if p == "/api/pagesniff":
            rurl = str(b.get("url") or "").strip()
            if not rurl:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            hdrs = b.get("headers") if isinstance(b.get("headers"), dict) else {}
            hdrs = {str(k): str(v) for k, v in hdrs.items() if v}
            if b.get("cookie"):
                hdrs.setdefault("Cookie", str(b["cookie"]))
            if b.get("referer"):
                hdrs.setdefault("Referer", str(b["referer"]))
            try:
                depth = max(0, min(int(b.get("depth") or 2), 3))
            except (TypeError, ValueError):
                depth = 2
            try:
                res = pagesniff.resolve(
                    rurl, self.cfg, depth=depth,
                    probe_quality=b.get("probe_quality", True) is not False,
                    page_headers=hdrs or None)
            except Exception as exc:      # noqa: BLE001
                self._json(500, {"ok": False, "streams": [],
                                 "error": f"{type(exc).__name__}: {exc}"})
                return
            self._json(200 if res.get("ok") else 400, res)
            return

        # ---- RSS/Atom 订阅源预览（不建任务）----
        # body: {"url": "https://site/feed/"} → {ok, kind:"feed", title,
        #        items:[{title,url,link,size,size_text,date,direct}], total}
        if p == "/api/feed/inspect":
            furl = (b.get("url") or "").strip()
            if not furl:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            self._json(200, feed.try_inspect(furl, self.cfg))
            return

        # ---- 打开本地种子 / 下载描述文件 ----
        # body: {"filename": "xx.torrent", "data": "<base64>"}
        # 支持三类（torrentfile.kind_for 的注释里有完整理由）：
        #   torrent  —— .torrent，落盘到 HOME/torrents/ 后用 file:// URL 建任务，
        #              _fetch_torrent 会从本地读字节交给 aria2.addTorrent
        #   metalink —— .meta4/.metalink，按 <file> 逐个建 HTTP 任务
        #   links    —— .magnet/.url/.txt 文本，抓出里面的链接逐个建任务
        if p == "/api/seed-file":
            fname = (b.get("filename") or "").strip()
            try:
                data = base64.b64decode(b.get("data") or "")
            except Exception:  # noqa: BLE001
                data = b""
            if not data:
                self._json(400, {"ok": False, "error": "文件内容是空的"})
                return
            if len(data) > 20 * 1024 * 1024:
                self._json(400, {"ok": False, "error": "文件超过 20MB，不太像种子/描述文件"})
                return
            try:
                skind = torrentfile.sniff_kind(data, fname)
            except torrentfile.TorrentError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return

            if skind == "torrent":
                try:
                    info = torrentfile.parse_torrent(data)
                except torrentfile.TorrentError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                tdir = pathlib.Path(_config.HOME) / "torrents"
                try:
                    tdir.mkdir(parents=True, exist_ok=True)
                    tpath = tdir / f"{info['info_hash']}.torrent"
                    tpath.write_bytes(data)
                except OSError as exc:
                    self._json(500, {"ok": False, "error": f"种子落盘失败：{exc}"})
                    return
                try:
                    t = self.tm.create_http(
                        tpath.as_uri(),
                        filename=info["name"],
                        auto_classify=False,
                        meta={"p2p": True, "info_hash": info["info_hash"],
                              "seed_file": fname},
                    )
                except Exception as exc:  # noqa: BLE001
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                self._json(200, {"ok": True, "kind": "torrent", "task": t.to_dict(),
                                 "info": {"name": info["name"], "size": info["size"],
                                          "files": info["files"],
                                          "info_hash": info["info_hash"]}})
                return

            if skind == "metalink":
                try:
                    entries = torrentfile.parse_metalink(data)
                except torrentfile.TorrentError as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                made, skipped = [], 0
                for e in entries[:50]:
                    url = e["urls"][0]
                    try:
                        sha = ""
                        for h in e.get("hashes", []):
                            if h.get("type", "").lower().replace("-", "") == "sha256":
                                sha = h["value"]
                                break
                        t = self.tm.create_http(
                            url, filename=e.get("name") or "",
                            auto_classify=False,
                            expected_sha256=sha,
                            meta={"seed_file": fname},
                        )
                        made.append(t.to_dict())
                    except Exception:  # noqa: BLE001
                        skipped += 1
                self._json(200, {"ok": True, "kind": "metalink",
                                 "tasks": made, "skipped": skipped,
                                 "total": len(entries)})
                return

            # links：.magnet / .url / .txt
            text = data.decode("utf-8", "replace")
            urls = torrentfile.extract_links(text)
            if not urls:
                self._json(400, {"ok": False, "error":
                          "文本里没找到可下载的链接（支持 magnet: / http(s) / "
                          "thunder: / ed2k / ftp:）"})
                return
            made = []
            for u in urls[:100]:
                try:
                    made.append(self.tm.create_http(
                        u, auto_classify=False,
                        meta={"seed_file": fname}).to_dict())
                except Exception:  # noqa: BLE001
                    pass
            if not made:
                self._json(400, {"ok": False,
                                 "error": "链接都建不了任务（可能被版本门禁拦下）"})
                return
            self._json(200, {"ok": True, "kind": "links", "tasks": made,
                             "total": len(urls)})
            return

        # ---- 任务控制 ----
        m = re.match(r"^/api/tasks/([^/]+)/(pause|resume|cancel|remove)$", p)
        if m:
            tid, action = m.group(1), m.group(2)
            fn = {"pause": self.tm.pause, "resume": self.tm.resume,
                  "cancel": self.tm.cancel, "remove": self.tm.remove}[action]
            ok = fn(tid)
            self._json(200 if ok else 400, {"ok": ok, "action": action, "id": tid})
            return

        # ---- 重新下载（推倒重来，区别于续传）----
        m = re.match(r"^/api/tasks/([^/]+)/restart$", p)
        if m:
            t = self.tm.restart(m.group(1))
            if t is None:
                self._json(400, {"ok": False,
                                 "error": "任务不存在，或正在运行（先取消再重新下载）"})
            else:
                self._json(200, {"ok": True, "task": t.to_dict()})
            return

        # ---- 队列优先级 ----
        # body: {"top": true} 置顶，或 {"priority": 5} 直接设值
        m = re.match(r"^/api/tasks/([^/]+)/priority$", p)
        if m:
            t = self.tm.set_priority(m.group(1), value=b.get("priority"),
                                     top=bool(b.get("top")))
            if t is None:
                self._json(404, {"ok": False, "error": "任务不存在"})
            else:
                self._json(200, {"ok": True, "id": t.id, "priority": t.priority,
                                 "task": t.to_dict()})
            return

        if p == "/api/tasks/clear-finished":
            n = self.tm.clear_finished()
            self._json(200, {"ok": True, "removed": n})
            return

        # ---- 单任务限速（P2-4）----
        # body: {"speed_limit": 5242880}（0 = 跟随全局）
        m = re.match(r"^/api/tasks/([^/]+)/speed$", p)
        if m:
            try:
                bps = int(b.get("speed_limit") or 0)
            except (TypeError, ValueError):
                self._json(400, {"ok": False, "error": "speed_limit 必须是整数"})
                return
            t = self.tm.set_task_speed(m.group(1), bps)
            if t is None:
                self._json(404, {"ok": False, "error": "任务不存在"})
            else:
                self._json(200, {"ok": True, "id": t.id, "speed_limit": bps,
                                 "task": t.to_dict()})
            return

        # ---- 订阅：新增 / 删除 / 立即检查 ----
        if p == "/api/subscriptions":
            if self._deny("rss"):
                return True
            url = str(b.get("url") or "").strip()
            try:
                sub = subscriptions.add(url, self.cfg,
                                        title=str(b.get("title") or ""),
                                        folder=str(b.get("folder") or ""))
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            self._json(200, {"ok": True, "item": sub})
            return

        if p == "/api/subscriptions/remove":
            ok = subscriptions.remove(str(b.get("id") or ""))
            self._json(200 if ok else 404, {"ok": ok})
            return

        if p == "/api/subscriptions/check":
            sid = str(b.get("id") or "").strip()
            results = []
            for sub in subscriptions.raw_list():
                if sid and sub.get("id") != sid:
                    continue
                res = subscriptions.check_one(sub, self.cfg,
                                              limit=int(self.cfg.get("feed_max_per_check", 10) or 10))
                if res.get("ok"):
                    n = _queue_feed_items(self.tm, self.cfg, sub, res.get("new") or [])
                    subscriptions.mark_checked(sub["id"], res.get("new") or [], "")
                    results.append({"id": sub["id"], "new": len(res.get("new") or []),
                                    "queued": n})
                else:
                    subscriptions.mark_checked(sub["id"], [], str(res.get("error") or ""))
                    results.append({"id": sub["id"], "ok": False,
                                    "error": res.get("error")})
            self._json(200, {"ok": True, "results": results})
            return

        # ---- 本地嗅探代理开关 ----
        if p == "/api/sniff":
            if bool(b.get("enabled")) and self._deny("sniff"):
                return True
            want = bool(b.get("enabled"))
            if want:
                info = sniffproxy.start(self.cfg, int(b.get("port") or 0))
                _config.update({"sniff_proxy_enabled": True})
            else:
                sniffproxy.stop()
                _config.update({"sniff_proxy_enabled": False})
                info = {"ok": True, "stopped": True}
            self._json(200, {"ok": True, **info, **sniffproxy.status(self.cfg)})
            return

        # ---- 站点抓取（P2-1）----
        if p == "/api/grab":
            try:
                job = grabber.start(
                    str(b.get("url") or ""), self.cfg,
                    dest_dir=str(b.get("dest_dir") or ""),
                    depth=b.get("depth"),
                    same_origin=b.get("same_origin"),
                )
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, "job": job.to_dict()})
            return

        m = re.match(r"^/api/grab/([^/]+)/cancel$", p)
        if m:
            ok = grabber.cancel(m.group(1))
            self._json(200 if ok else 404, {"ok": ok})
            return

        # ---- 打开一个路径（设置里的「打开文件夹」）----
        # 与 /api/tasks/<id>/open 不同：那个只允许打开任务自己的产物，
        # 这个允许打开用户填在设置里的目录 —— 但**只允许打开**，
        # 不回读内容，且必须是已存在的目录/文件。
        if p == "/api/open-path":
            raw = str(b.get("path") or "").strip()
            mode = str(b.get("mode") or "folder").strip()
            if mode not in ("open", "reveal", "folder"):
                mode = "folder"
            if not raw:
                # 没填就用默认下载目录 —— 设置里留空是常态
                raw = str(_config.download_dir(self.cfg))
            ok, msg = _reveal(raw, mode=mode)
            self._json(200 if ok else 400,
                       {"ok": ok, "path": msg, "error": None if ok else msg})
            return

        # ---- 弹系统目录选择框 ----
        if p == "/api/pick-dir":
            ok, msg, cancelled = _pick_dir(b.get("path") or "")
            self._json(200, {"ok": ok, "path": msg if ok else "",
                             "cancelled": cancelled,
                             "error": None if (ok or cancelled) else msg})
            return

        # ---- 在资源管理器里定位产物（IDM 的双击/打开文件夹）----
        #   open   用默认程序打开文件
        #   reveal 打开所在文件夹并选中它
        #   folder 只打开目录
        m2 = re.match(r"^/api/tasks/([^/]+)/(open|reveal|folder)$", p)
        if m2:
            t = self.tm.get(m2.group(1))
            if not t:
                self._json(404, {"ok": False, "error": "任务不存在"})
                return
            mode = m2.group(2)
            target = t.dest or t.dest_dir
            # "打开"对没下完的任务没意义（文件还不完整），但"定位"可以 ——
            # 用户常想看看 .part 躺在哪、下了多少
            if mode == "open" and t.status != "done":
                self._json(400, {"ok": False,
                                 "error": f"任务还没下完（{t.status}），"
                                          f"先用「打开所在文件夹」看进度文件"})
                return
            ok, msg = _reveal(target, mode=mode)
            self._json(200 if ok else 400, {"ok": ok, "path": msg,
                                            "error": None if ok else msg})
            return

        # ---- 探测直链 ----
        if p == "/api/probe":
            url = (b.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            pr = http_probe(url, self.cfg)
            self._json(200, {"ok": True, "probe": pr.to_dict()})
            return

        # ---- 识别资源类型（纯离线判断，不联网）----
        if p == "/api/classify":
            url = (b.get("url") or "").strip()
            if not url and not b.get("filename"):
                self._json(400, {"ok": False, "error": "至少给 url 或 filename"})
                return
            size = b.get("size")
            try:
                size = int(size) if size not in (None, "") else None
            except Exception:
                size = None
            plan = resource.classify(
                url=url,
                filename=(b.get("filename") or "").strip(),
                mime=(b.get("mime") or "").strip(),
                content_disposition=(b.get("content_disposition") or "").strip(),
                size=size,
            )
            self._json(200, {"ok": True, "plan": plan.to_dict()})
            return

        # ---- 探测 + 识别一步到位（会联网发 HEAD / 1KB Range）----
        if p == "/api/inspect":
            url = (b.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            self._json(200, {"ok": True, "result": self.tm.classify(url)})
            return

        # ---- 浏览器插件问：这个下载我要不要接过来？----
        if p == "/api/intercept":
            url = (b.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            size = b.get("size")
            try:
                size = int(size) if size not in (None, "") else None
            except Exception:
                size = None
            d = resource.intercept_decision(
                url,
                mime=(b.get("mime") or "").strip(),
                filename=(b.get("filename") or "").strip(),
                content_disposition=(b.get("content_disposition") or "").strip(),
                size=size,
                category_hint=(b.get("category_hint") or "").strip(),
            )
            self._json(200, {"ok": True, **d})
            return

        # ---- 探测视频 ----
        if p == "/api/video/probe":
            url = (b.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            try:
                info = video.probe(url, self.cfg)
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, "info": info})
            return

        # ---- 展开合集 / 播放列表（列条目让用户勾选）----
        if p == "/api/video/playlist":
            url = (b.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            limit = int(b.get("limit") or self.cfg.get("playlist_limit") or 200)
            try:
                # B 站优先走官方 API —— 0.4 秒拿全标题，比 yt-dlp 的 flat 模式
                # 快一个数量级，而且标题不是空的。但番剧/UP合集/收藏夹这些
                # 形态官方接口这套没覆盖全，bili.resolve 会说 delegate=True，
                # 那就老老实实交给 yt-dlp。
                res = None
                if bili.parse_url(url).get("kind") not in (None, "", "unknown"):
                    r0 = bili.resolve(self.cfg, url, limit=limit)
                    if r0.get("ok"):
                        res = r0
                        res["via"] = "bilibili-api"
                if res is None:
                    res = video.probe_playlist(url, self.cfg, limit=limit)
                    res["via"] = "yt-dlp"
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"ok": False, "error": str(exc),
                                 "hint": video.humanize_error(str(exc), self.cfg, url)})
                return
            self._json(200, {"ok": True, "result": res})
            return

        # ---- 单条链接的一步到位体检（含 yt-dlp 播放列表判断）----
        if p == "/api/video/inspect":
            url = (b.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            try:
                self._json(200, {"ok": True, "result": video.inspect(url, self.cfg)})
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"ok": False, "error": str(exc)})
            return

        # ---- 逐项体检：为什么这条链接下来会失败 ----
        if p == "/api/video/diagnose":
            url = (b.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            self._json(200, {"ok": True, "result": video.diagnose(url, self.cfg)})
            return

        # ---- 代理 ----
        if p == "/api/proxy/test":
            target = (b.get("proxy") or self.cfg.get("proxy") or "").strip()
            if not target:
                self._json(400, {"ok": False,
                                 "error": "没给代理地址，配置里也没有。形如 http://127.0.0.1:7890"})
                return
            info = proxy.test(target, timeout=float(b.get("timeout") or 3.0))
            self._json(200, {"ok": info.alive, "proxy": info.to_dict()})
            return

        if p == "/api/proxy/detect":
            proxy.invalidate_cache()
            self._json(200, {"ok": True, "env": proxy.detect_env(refresh=True).to_dict()})
            return

        # ---- Cookie ----
        if p == "/api/cookies/save":
            text = b.get("text") or ""
            if not text.strip():
                self._json(400, {"ok": False, "error": "cookie 内容为空"})
                return
            info = cookies.validate(text)
            if not info.get("ok"):
                self._json(400, {"ok": False, "error": info.get("error") or "格式不认",
                                 "detail": info})
                return
            path = cookies.save_text(text, (b.get("label") or "imported").strip())
            self._json(200, {"ok": True, "path": str(path), "info": info})
            return

        if p == "/api/cookies/from-browser":
            br = (b.get("browser") or "").strip()
            if not br:
                self._json(400, {"ok": False, "error": "请指定浏览器，如 firefox / chrome"})
                return
            try:
                res = cookies.from_browser(br, (b.get("profile") or "").strip())
            except Exception as exc:  # noqa: BLE001
                self._json(400, {"ok": False, "error": str(exc)})
                return
            # from_browser 自己返回完整结果（含失败时的可读解释），
            # 原样透传 —— 之前把 dict 塞进 "path" 字段，前端拿到的结构是坏的
            self._json(200 if res.get("ok") else 400, res)
            return

        if p == "/api/cookies/delete":
            ok = cookies.remove((b.get("path") or b.get("name") or "").strip())
            self._json(200 if ok else 404, {"ok": ok})
            return

        # ---- 命名模板 ----
        if p == "/api/naming":
            pat = (b.get("pattern") or "").strip()
            self._json(200, {"ok": True, "check": naming.check(pat),
                             "variables": naming.variables()})
            return

        # ---- 已下载记录 ----
        if p == "/api/history/remove":
            ok = history().remove((b.get("key") or "").strip())
            self._json(200 if ok else 404, {"ok": ok})
            return

        if p == "/api/history/clear":
            self._json(200, {"ok": True, "removed": history().clear()})
            return

        # ---- B 站扫码登录 ----
        if p in ("/api/bili/login/qr", "/api/bili/qr"):
            try:
                self._json(200, {"ok": True, **bili.qr_start(self.cfg)})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        # ---- 多平台扫码登录（B站 + 抖音实现；其余平台给 Cookie 引导）----
        if p == "/api/accounts/login/qr":
            platform = (b.get("platform") or "").strip()
            self._json(200, accounts.qr_start(self.cfg, platform))
            return

        if p == "/api/accounts/login/poll":
            platform = (b.get("platform") or "").strip()
            key = (b.get("key") or "").strip()
            if not key:
                self._json(400, {"ok": False, "error": "缺少 key"})
                return
            res = accounts.qr_poll(self.cfg, platform, key)
            if res.get("state") == "ok":
                # 登录态变了，缓存里的「未登录」结论作废
                bili_status_invalidate()
            self._json(200, res)
            return

        if p == "/api/accounts/logout":
            res = accounts.logout(self.cfg, (b.get("platform") or "").strip())
            if res.get("ok"):
                bili_status_invalidate()
            self._json(200, res)
            return

        # ---- 插件唤起主程序（写入待办提示）----
        if p == "/api/ui/hint":
            hint = {
                "action": (b.get("action") or "new-task").strip(),
                "url": (b.get("url") or "").strip(),
                "title": (b.get("title") or "").strip(),
                "from": (b.get("from") or "").strip(),
                "at": time.time(),
            }
            if hint["action"] == "new-task" and not hint["url"]:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            with _ui_hint_lock:
                _ui_hint.clear()
                _ui_hint.update(hint)
            self._json(200, {"ok": True})
            return

        # ---- 唤起主窗口 ----
        if p == "/api/focus":
            ok, how = _focus_main_window()
            self._json(200 if ok else 404, {"ok": ok, "how": how})
            return

        if p in ("/api/bili/login/poll", "/api/bili/poll"):
            key = (b.get("key") or b.get("qrcode_key") or "").strip()
            if not key:
                self._json(400, {"ok": False, "error": "缺少 key（qr 接口返回的 qrcode_key）"})
                return
            res = bili.qr_poll(self.cfg, key)
            if res.get("state") == "ok":
                # 刚登录成功，把缓存的「未登录」结论作废
                bili_status_invalidate()
            self._json(200, {"ok": True, **res})
            return

        if p == "/api/bili/logout":
            removed = bili.logout()
            bili_status_invalidate()
            self._json(200, {"ok": True, "removed": removed})
            return

        # ---- 识别一个链接的协议（磁力/迅雷/网盘…）----
        if p == "/api/protocol":
            raw = (b.get("url") or "").strip()
            if not raw:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            pinfo = protocols.detect(raw)
            self._json(200, {"ok": True, "protocol": pinfo.to_dict(),
                             "summary": protocols.summarize(pinfo),
                             "plan": resource.classify(url=raw).to_dict()})
            return

        # ---- 交给 Motrix（或任意已配置的 aria2 RPC）----
        if p == "/api/handoff/motrix":
            url = (b.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "缺少 url"})
                return
            det = backends.aria2.detect(self.cfg)
            if not det.get("rpc_ok"):
                self._json(400, {
                    "ok": False,
                    "error": "没有可连接的 aria2 RPC。Motrix 需在设置里开启 RPC 并把密钥填进汲流配置。",
                    "hint": "Motrix 默认 RPC 端口 16800",
                })
                return
            try:
                from .backends.aria2 import Aria2Client
                secret = (self.cfg.get("motrix_rpc_secret") or ""
                          if det.get("motrix") else self.cfg.get("aria2_rpc_secret") or "")
                client = Aria2Client(det["rpc_url"], secret)
                out = b.get("dest_dir") or str(_config.download_dir(self.cfg))
                gid = client.add_uri([url], {"dir": out})
                self._json(200, {"ok": True, "gid": gid, "rpc": det["rpc_url"]})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": str(exc)})
            return

        # ---- 改配置 ----
        if p == "/api/config":
            patch = {k: v for k, v in b.items()}
            # 前端把脱敏值原样回传时不要覆盖真值（否则保存一次设置
            # 就把密码变成 "••••••"）。
            patch = {k: v for k, v in patch.items() if v != _MASK}
            # 危险键：从浏览器扩展来的请求不能改。这三个键等于本机代码执行
            # （ytdlp_path 指向任意 exe / after_download_command 跑命令 /
            # update_repo 把更新源指到别处），面板自己改是用户意图，
            # 随便一个装进来的扩展改就不是了。
            origin = self.headers.get("Origin") or ""
            if origin.startswith(("chrome-extension://", "moz-extension://")):
                for k in ("ytdlp_path", "after_download_command", "update_repo", "cookie_file"):
                    patch.pop(k, None)
            # 数值字段做一下类型收敛。前端送来的是字符串，
            # 直接塞进配置会让后面 int() 到处炸。
            for key in ("threads", "port", "speed_limit", "max_concurrent",
                        "aria2_split", "aria2_max_conn", "aria2_port",
                        "playlist_limit", "playlist_concurrent",
                        "bt_seed_time", "bt_max_peers",
                        "bt_listen_port", "window_width", "window_height",
                        "feed_check_minutes", "feed_max_per_check",
                        "sniff_proxy_port", "capture_max_mb",
                        "grab_max_depth", "grab_max_pages", "number_pad",
                        "bili_qn", "aria2_video_conn", "aria2_default_conn",
                        "aria2_p2p_conn"):
                if key in patch and patch[key] not in ("", None):
                    try:
                        patch[key] = int(patch[key])
                    except Exception:
                        patch.pop(key, None)
            # 浮点字段（aria2 的分享率允许 0.5 这种）——
            # 注意别把它放进上面的 int 列表里：int("0.5") 会抛异常，
            # 结果就是把用户填的分享率悄悄删掉。
            if "bt_seed_ratio" in patch and patch["bt_seed_ratio"] not in ("", None):
                try:
                    patch["bt_seed_ratio"] = float(patch["bt_seed_ratio"])
                except Exception:
                    patch.pop("bt_seed_ratio", None)
            # 布尔字段同理：HTTP 上来是 "true"/"false" 字符串，
            # 而 "false" 在 Python 里是真值 —— 不转就会被悄悄打开
            for key in ("proxy_domestic", "proxy_foreign", "playlist_folder",
                        "playlist_reverse", "audio_only", "write_subs",
                        "embed_subs", "bt_enabled", "skip_downloaded",
                        "auto_subdir", "subdir_explicit",
                        "proxy_fallback_direct", "auto_fallback_engine",
                        "window_debug", "notify_enabled", "prevent_sleep",
                        "tray_enabled", "m3u_enabled", "clipboard_watch",
                        "sniff_proxy_enabled", "bili_native", "grab_same_origin",
                        "onboarded"):
                if key in patch:
                    v = patch[key]
                    if isinstance(v, str):
                        patch[key] = v.strip().lower() not in ("0", "", "false", "no", "off")
                    else:
                        patch[key] = bool(v)
            # 枚举字段：只接受已知取值，拼错就丢掉而不是写进配置
            for key, allowed in (
                ("edition", tuple(edition.EDITIONS)),
                ("form", tuple(edition.FORMS)),
                ("backend", ("native", "aria2", "curl", "wget", "auto")),
                ("proxy_mode", ("system", "auto", "manual", "off")),
                ("window_mode", ("auto", "native", "browser")),
                ("duplicate_policy", ("rename", "resume", "skip", "overwrite")),
                ("aria2_profile", ("auto", "video", "default", "p2p")),
                ("bili_codec", ("avc", "hevc", "av1")),
            ):
                if key in patch:
                    v = str(patch[key] or "").strip().lower()
                    if v in allowed:
                        patch[key] = v
                    else:
                        patch.pop(key, None)
            # 归档目录名：走一遍清洗，挡掉路径穿越（`..\..\Windows` 这类）
            names = patch.get("subdir_names")
            if isinstance(names, dict):
                cleaned = {}
                for k, v in names.items():
                    if k not in layout.SUBDIRS:
                        continue
                    cleaned[str(k)] = ("" if str(v).strip() == ""
                                       else layout.sanitize_segment(str(v)))
                patch["subdir_names"] = cleaned
            # 引擎顺序：只留认识的，去重
            order = patch.get("engine_order")
            if isinstance(order, list):
                seen, ok = set(), []
                for k in order:
                    k = str(k).strip().lower()
                    if k in ("native", "aria2", "curl", "wget") and k not in seen:
                        seen.add(k)
                        ok.append(k)
                patch["engine_order"] = ok or ["aria2", "native"]
            # 计划任务：收敛成合法结构（坏时间/坏星期一律回默认）
            if "schedule" in patch and isinstance(patch["schedule"], dict):
                patch["schedule"] = schedule.normalize(patch["schedule"])
                patch["schedule"].pop("_start", None)
                patch["schedule"].pop("_end", None)
                # 定时下载是 Pro 项。这里不拒绝整次保存（用户同时还改了别的设置），
                # 而是把它落成关，并在响应里说清原因 —— 界面据此提示，不会出现
                # 「勾上了但到点不跑」这种最让人火大的静默失败。
                if patch["schedule"].get("enabled") and not _license.requires("schedule"):
                    patch["schedule"]["enabled"] = False
                    _denied_schedule = _license.guard("schedule").get("reason", "")
            # 例外站点：列表化 + 去空
            if "capture_exceptions" in patch:
                raw = patch["capture_exceptions"]
                if isinstance(raw, str):
                    raw = raw.splitlines()
                patch["capture_exceptions"] = [str(x).strip() for x in (raw or [])
                                               if str(x).strip()]

            before = dict(self.cfg)
            self.cfg = _config.update(patch)
            self.tm.cfg = self.cfg

            # ---- 需要立刻生效的开关 ----
            try:
                if patch.get("sniff_proxy_enabled") and not before.get("sniff_proxy_enabled"):
                    sniffproxy.start(self.cfg)
                elif "sniff_proxy_enabled" in patch and not patch["sniff_proxy_enabled"]:
                    sniffproxy.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                global _CLIP_WATCHER
                if patch.get("clipboard_watch") and _CLIP_WATCHER is None:
                    def _on_clip(url: str) -> None:
                        _CLIP_RECENT.append({"url": url, "at": time.time()})
                        del _CLIP_RECENT[:-50]
                    _CLIP_WATCHER = clipboard.Watcher(_on_clip)
                    _CLIP_WATCHER.start()
                elif "clipboard_watch" in patch and not patch["clipboard_watch"] and _CLIP_WATCHER:
                    _CLIP_WATCHER.stop()
                    _CLIP_WATCHER = None
            except Exception:  # noqa: BLE001
                pass
            try:
                global _FEED_WATCHER
                if "feed_check_minutes" in patch:
                    if _FEED_WATCHER:
                        _FEED_WATCHER.stop()
                    _FEED_WATCHER = subscriptions.Watcher(
                        self.cfg, lambda sub, items: _queue_feed_items(self.tm, self.cfg, sub, items))
                    if not _FEED_WATCHER.start():
                        _FEED_WATCHER = None
            except Exception:  # noqa: BLE001
                pass
            # 限速改了就热推给运行中的 aria2（不用重启 daemon；
            # native 引擎是每任务建 limiter，新任务自然读新配置）。
            if "speed_limit" in patch:
                try:
                    backends.get_service(self.cfg).refresh_speed_limit(self.cfg)
                except Exception:
                    pass  # daemon 没在跑也没关系，下次拉起时按新配置启动
            # 代理/ Cookie 换了就得重新判定，别用着旧结论
            proxy.invalidate_cache()
            out = {"ok": True, "config": self.cfg}
            try:
                if _denied_schedule:
                    out["warning"] = _denied_schedule
                    out["denied"] = ["schedule"]
            except NameError:
                pass
            self._json(200, out)
            return

        self._json(404, {"ok": False, "error": "未知接口", "path": p})


class HttpServer(ThreadingHTTPServer):
    """静音「客户端先挂了」的 HTTP 服务。

    为什么必须写成 server 的子类：ThreadingMixIn 的处理线程是这样兜异常的 ——

        def process_request_thread(self, request, client_address):
            try:
                self.finish_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)   # self 是 server！

    也就是说 handle_error 的接收者是 **server 实例**，不是 Handler。
    把它写在 Handler 上永远不会被调用（本项目踩过这个坑：写是写了，
    日志照刷，一度以为"修好了"）。

    而网络里客户端半路消失太常见了：浏览器预连接后立刻断开、面板标签页被关、
    插件轮询被取消、curl 超时退出 —— 服务端写响应时就会撞上
    ConnectionAbortedError（WinError 10053）。这些是常态不是故障，
    任其刷屏只会把真正的错误淹掉。
    """

    daemon_threads = True

    # 这些属于「客户端那边的事」，不值得留痕；
    # FileNotFoundError / PermissionError 是我们自己的问题，照常报。
    _QUIET = (ConnectionError, TimeoutError, BrokenPipeError, ConnectionResetError)

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, self._QUIET):
            return
        if isinstance(exc, OSError) and not isinstance(
                exc, (FileNotFoundError, PermissionError)):
            return
        super().handle_error(request, client_address)


def serve(cfg: Optional[Dict[str, Any]] = None, *, port: Optional[int] = None,
          host: str = "127.0.0.1", quiet: bool = False,
          open_browser: bool = False) -> Tuple[ThreadingHTTPServer, TaskManager]:
    """启动服务。返回 (server, manager)，调用方负责 shutdown。"""
    cfg = cfg or _config.load()
    port = int(port or cfg.get("port", 8788))

    tm = TaskManager(cfg)
    handler = type("BoundHandler", (Handler,), {"tm": tm, "cfg": cfg})
    try:
        httpd = HttpServer((host, port), handler)
    except OSError as exc:
        raise RuntimeError(
            f"端口 {port} 起不来（{exc}）。多半是已经在跑了，"
            f"或者换个端口：jiliu serve --port {port + 1}"
        ) from exc
    httpd.daemon_threads = True

    # 慢探测在后台先跑起来。窗口创建 + webview 初始化要一两秒，
    # 这段时间正好被它用掉 —— 等页面真的来要 /api/state 时已经好了。
    warm_probes(cfg)

    # 自动更新：后台定时检查。1.8.0 就写好了 start_watcher，但全仓库
    # 没有任何调用点 —— 这条功能实际上从未启动过，用户只能手动点，
    # 而手动链路也是断的（见 1.9.5 修复）。
    try:
        def _on_update(info: Dict[str, Any]) -> None:
            try:
                notify.notify(
                    "汲流有新版本",
                    f"{info.get('latest') or ''} 可用，打开「关于」页即可更新",
                )
            except Exception:  # noqa: BLE001
                pass

        update.start_watcher(cfg, on_found=_on_update)
    except Exception:  # noqa: BLE001
        pass

    # 订阅定时检查 + 剪贴板轮询：跟着服务一起起，进程退出即结束
    global _FEED_WATCHER, _CLIP_WATCHER
    try:
        _FEED_WATCHER = subscriptions.Watcher(
            cfg, lambda sub, items: _queue_feed_items(tm, cfg, sub, items))
        _FEED_WATCHER.start()
    except Exception:  # noqa: BLE001
        _FEED_WATCHER = None
    try:
        if cfg.get("sniff_proxy_enabled"):
            sniffproxy.start(cfg)
    except Exception:  # noqa: BLE001
        pass
    try:
        if cfg.get("clipboard_watch"):
            def _on_clip(url: str) -> None:
                _CLIP_RECENT.append({"url": url, "at": time.time()})
                del _CLIP_RECENT[:-50]
            _CLIP_WATCHER = clipboard.Watcher(_on_clip)
            _CLIP_WATCHER.start()
    except Exception:  # noqa: BLE001
        _CLIP_WATCHER = None

    # 先把服务线程拉起来，再打印横幅。
    # 横幅里要探工具版本（spawn 好几个子进程，5 秒上下），
    # 挡在前面的话，开着面板却连不上，看着就像卡死了。
    th = threading.Thread(target=httpd.serve_forever, daemon=True, name="jiliu-http")
    th.start()

    # 记下"我在哪个端口"，供下一次协议唤起复用（热路径）。
    # 必须用 httpd.server_address：传 port=0 时真实端口是内核分配的，
    # 拿参数里的 0 去写会写出一条完全没用的记录。
    real_port = int(httpd.server_address[1])
    try:
        urltake.write_runtime(real_port)
        atexit.register(urltake.clear_runtime, os.getpid())
    except Exception:      # noqa: BLE001
        pass

    # 冷启动待办：从协议唤起进来的那个链接，现在服务能收活了，投出去。
    # 放在 serve_forever 之后 —— 投递走的是 HTTP，服务没跑起来就必然失败。
    try:
        urltake.consume_pending(real_port)
    except Exception:      # noqa: BLE001
        pass

    if not quiet:
        print(f"  {APP_NAME} v{__version__}  面板 http://{host}:{port}/")
        print(f"  状态目录 {_config.HOME}"
              + ("   （便携模式）" if _config.is_portable() else ""))
        eff = backends.resolve(cfg)
        print(f"  下载引擎 {eff}（配置：{cfg.get('backend')}）")
        vlist = tools.probe_all(cfg)
        for k in ("yt-dlp", "ffmpeg", "aria2c"):
            info = vlist.get(k, {})
            mark = "OK " if info.get("available") == "yes" else "-- "
            print(f"    {mark}{k:<8} {info.get('version', '')[:44] or '未安装'}"
                  f"   {info.get('source', '')}")
        print("  Ctrl+C 停止")

    if open_browser:
        try:
            webbrowser.open(f"http://{host}:{port}/")
        except Exception:
            pass
    return httpd, tm


if __name__ == "__main__":
    srv, mgr = serve()
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        print("\n正在停止…")
        mgr.shutdown()
        srv.shutdown()
