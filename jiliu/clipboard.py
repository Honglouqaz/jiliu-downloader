"""剪贴板监听（Windows）。

IDM 的 MonitorUrlClipboard 默认关、可开；汲流之前只能靠地址栏或扩展。
这里用 ctypes 直接读 CF_UNICODETEXT，不引依赖。

行为刻意保守：
  - 只在开着的时候轮询（默认关）；
  - 同一段文本只报一次；
  - 只认 http(s)，长度超过 4KB 的（比如整篇文档）直接忽略；
  - 去掉尾部标点，避免把中文句号粘进 URL。
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Callable, Optional

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_TRAIL = "。，、；：！？）》」』）,.!?;:)]}\"'"


def _read_clipboard() -> str:
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        # 64 位下必须声明签名：HANDLE 默认被截成 32 位，GlobalLock 会失败
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.GetClipboardData.argtypes = [wintypes.UINT]
        u.GetClipboardData.restype = wintypes.HANDLE
        u.CloseClipboard.restype = wintypes.BOOL
        k.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k.GlobalLock.restype = wintypes.LPVOID
        k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        CF_UNICODETEXT = 13
        if not u.OpenClipboard(None):
            return ""
        try:
            h = u.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            ptr = k.GlobalLock(h)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                k.GlobalUnlock(h)
        finally:
            u.CloseClipboard()
    except Exception:  # noqa: BLE001
        return ""


def extract(text: str) -> str:
    """从一段文本里取第一个像样的 URL。"""
    if not text or len(text) > 4096:
        return ""
    m = _URL_RE.search(text)
    if not m:
        return ""
    return m.group(0).rstrip(_TRAIL)


class Watcher:
    """后台轮询。on_url(url) 由调用方决定是弹提示还是直接建任务。"""

    def __init__(self, on_url: Callable[[str], None], interval: float = 0.8):
        self.on_url = on_url
        self.interval = interval
        self._stop = threading.Event()
        self._th: Optional[threading.Thread] = None
        self._last = ""

    def start(self) -> bool:
        if self._th and self._th.is_alive():
            return True
        if os.name != "nt":
            return False
        self._th = threading.Thread(target=self._loop, daemon=True, name="jiliu-clip")
        self._th.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                text = _read_clipboard()
                url = extract(text)
                if not url or url == self._last:
                    continue
                self._last = url
                self.on_url(url)
            except Exception:  # noqa: BLE001
                pass
