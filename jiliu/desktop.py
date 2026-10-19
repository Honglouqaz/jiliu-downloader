"""原生桌面窗口。

之前是「起个服务，然后打开浏览器」。主人明确说了不要这样 ——
要的是**双击就出来一个窗口**，没有终端、没有浏览器地址栏。

用 pywebview：它在 Windows 上走 Edge WebView2（WinForms 宿主），
拿到的就是一个真正的原生窗口 —— 有标题栏、有任务栏图标、
能最小化最大化、关掉就退出。而界面仍然是我们那套 HTML，
不用为了「像桌面软件」把整个 UI 用 Qt 重写一遍。

几个必须处理好的细节：

  - **服务先起，窗口后开。** 窗口一出来就要能加载到页面，
    所以要在主线程进入 GUI 事件循环之前把 HTTP 服务拉起来。
  - **端口被占要自动换。** 用户可能已经开着一个汲流，
    这时候不该直接报错退出，换个端口再开就行。
  - **窗口关闭要收干净。** 服务线程、下载任务都得停 ——
    否则进程退不出去，任务管理器里留个僵尸进程。
  - **start() 必须在主线程调。** 这是 pywebview 的硬要求，
    所以 run() 是个阻塞调用，调用方（CLI）要接受这一点。
"""
from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple

TITLE = "汲流 · 下载器"
DEFAULT_SIZE = (1280, 820)
MIN_SIZE = (980, 620)

_unavailable = ""

_prewarm_started = False


def prewarm() -> None:
    """后台线程提前把 webview 拉进内存（幂等，只起一次）。

    frozen 下 `import webview` 实测要 ~1.8s（源码态只要 0.11s——
    PYZ 读取 + webview.http 连带 bottle 全家桶都在这时候进来）。
    让它和 config.load / server.serve 并行跑，主线程走到 _load()
    时 import 锁会自然汇合 —— 窗口能提前 ~1.5s 出来。
    """
    global _prewarm_started
    if _prewarm_started:
        return
    _prewarm_started = True
    threading.Thread(target=_load, daemon=True,
                     name="webview-prewarm").start()


def available() -> bool:
    """本机能不能开原生窗口。"""
    return _load() is not None


def unavailable_reason() -> str:
    return _unavailable


def _load() -> Optional[Any]:
    global _unavailable
    try:
        import webview
        return webview
    except Exception as exc:  # noqa: BLE001
        _unavailable = (
            f"{type(exc).__name__}: {exc}"
            "。装一个：pip install pywebview（需要系统有 WebView2 运行时，"
            "Windows 10/11 通常自带）"
        )
        return None


def _port_free(host: str, port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def pick_port(host: str = "127.0.0.1", want: int = 8788,
              tries: int = 20) -> int:
    """找一个能用的端口。已经开着汲流时自动往后顺延。"""
    for i in range(tries):
        p = int(want) + i
        if _port_free(host, p):
            return p
    return int(want)


def run(cfg: Dict[str, Any], *, port: Optional[int] = None,
        host: str = "127.0.0.1") -> int:
    """起服务 + 开窗口，阻塞到窗口关闭。返回进程退出码。"""
    from . import config as _config
    from . import server as _server

    cfg = dict(cfg or _config.load())
    want = int(port or cfg.get("port", 8788))
    use = pick_port(host, want)
    if use != want:
        print(f"  端口 {want} 被占用，改用 {use}")

    # 先起服务、后碰 webview：面板地址尽早可用。frozen 下 webview/clr
    # 的加载比源码态贵得多，别让它挡在 HTTP 服务前面。
    httpd, tm = _server.serve(cfg, port=use, host=host, quiet=True)
    url = f"http://{host}:{use}/"

    webview = _load()
    if webview is None:
        _shutdown(tm, httpd)
        raise RuntimeError(unavailable_reason())

    win = webview.create_window(
        TITLE, url,
        width=int(cfg.get("window_width") or DEFAULT_SIZE[0]),
        height=int(cfg.get("window_height") or DEFAULT_SIZE[1]),
        min_size=MIN_SIZE,
        text_select=True,
        confirm_close=False,
    )

    state: Dict[str, Any] = {"closed": False}

    def _on_closed() -> None:
        state["closed"] = True

    try:
        win.events.closed += _on_closed
    except Exception:  # noqa: BLE001
        pass

    # ---- 托盘（关窗口后仍能看一眼进度 / 暂停 / 退出）----
    tray_icon = None
    if cfg.get("tray_enabled", True):
        try:
            from . import tray as _tray

            def _open() -> None:
                try:
                    win.show()
                    win.restore()
                except Exception:  # noqa: BLE001
                    pass

            def _pause() -> None:
                # 有暂停中的就继续，否则暂停全部 —— 一个按钮两种语义，
                # 托盘菜单只有一行位置，值得这样折中
                if tm.resume_all() == 0:
                    tm.pause_all()

            def _quit() -> None:
                state["closed"] = True
                try:
                    win.destroy()
                except Exception:  # noqa: BLE001
                    os._exit(0)

            tray_icon = _tray.Tray(on_open=_open, on_pause=_pause, on_quit=_quit,
                                   tooltip=TITLE)
            if not tray_icon.start():
                tray_icon = None
        except Exception:  # noqa: BLE001
            tray_icon = None

    code = 0
    try:
        # 关键：start() 必须跑在主线程，且会阻塞到窗口关闭。
        # private_mode=False 让 localStorage 留下来（界面里存了列宽等偏好）。
        webview.start(debug=bool(cfg.get("window_debug")), private_mode=False)
    except Exception as exc:  # noqa: BLE001
        code = 1
        print(f"  窗口启动失败：{type(exc).__name__}: {exc}")
    finally:
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception:  # noqa: BLE001
                pass
        _shutdown(tm, httpd)
        # 关窗 = 整个程序结束：worker（yt-dlp / aria2c）一并带走，
        # 然后硬退出 —— 不给任何残留线程把进程挂在后台的机会。
        # 实测（2026-09-24）：正常路径本来 2.1s 内就能退干净，
        # os._exit 是保险丝，防的是「某天多了个非 daemon 线程」这类回归。
        try:
            from . import tools as _tools
            _tools.kill_tracked()
        except Exception:  # noqa: BLE001
            pass
        os._exit(int(code))


def _shutdown(tm: Any, httpd: Any) -> None:
    """收干净：先停任务（会写盘），再停服务。"""
    try:
        tm.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        httpd.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        httpd.server_close()
    except Exception:  # noqa: BLE001
        pass


def describe(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给界面/环境体检看的窗口能力。"""
    cfg = cfg or {}
    mode = (cfg.get("window_mode") or "auto").strip().lower()
    return {
        "available": available(),
        "reason": unavailable_reason(),
        "mode": mode,
        "title": TITLE,
        "size": list(DEFAULT_SIZE),
        "min_size": list(MIN_SIZE),
        "modes": [
            {"key": "auto", "label": "自动",
             "desc": "能开原生窗口就开，不行才退回浏览器"},
            {"key": "native", "label": "原生窗口",
             "desc": "始终用桌面窗口打开，不弹终端、不开浏览器"},
            {"key": "browser", "label": "浏览器",
             "desc": "用系统默认浏览器打开面板"},
        ],
    }
