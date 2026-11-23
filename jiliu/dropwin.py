"""拖拽投递悬浮窗（Win32 原生，无第三方依赖）。

为什么不用 WebView2 页面接拖放
    浏览器安全模型不让页面拿到本地文件真实路径（只能拿到 File 对象，
    没有 path）。下载器要的是路径，所以必须在 Win32 层接 WM_DROPFILES。

一个容易踩的坑：从浏览器拖**链接**过来时，拿到的不是 URL 字符串，
而是一个临时 `.url` 快捷方式文件，内容形如

    [InternetShortcut]
    URL=https://example.com/a.zip

所以 `_urls_from_drop()` 要能把这类文件读回成 URL —— 只按"是不是文件"
处理的话，用户拖链接进来会变成下载一个几十字节的 .url 文件。
"""
from __future__ import annotations

import ctypes
import os
import pathlib
import threading
import time
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_DROPFILES = 0x0233
WM_NCHITTEST = 0x0084
WM_MOUSEACTIVATE = 0x0021
WM_RBUTTONUP = 0x0205
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_EXITSIZEMOVE = 0x0232
WM_APP = 0x8000
# 自定义：真正销毁窗口（WM_CLOSE 只是隐藏，方便托盘再叫出来）
WM_QUIT_APP = WM_APP + 41
HTCAPTION = 2
CS_DROPSHADOW = 0x00020000
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002
DT_CENTER = 0x00000001
DT_VCENTER = 0x00000004
DT_SINGLELINE = 0x0020
DT_WORDBREAK = 0x0010
TRANSPARENT = 1
FW_NORMAL = 400
FW_SEMIBOLD = 600
DEFAULT_CHARSET = 1
CLEARTYPE_QUALITY = 5

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
                ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT),
                ("lPrivate", wintypes.DWORD)]


def _url_from_shortcut(path: pathlib.Path) -> str:
    """读 .url 快捷方式里的 URL= 行。不是快捷方式就返回空。"""
    try:
        if path.suffix.lower() not in (".url", ".webloc"):
            return ""
        raw = path.read_bytes()[:4096].decode("utf-8", "replace")
    except Exception:
        return ""
    for line in raw.splitlines():
        line = line.strip()
        if line.lower().startswith("url="):
            return line[4:].strip()
    return ""


def urls_from_paths(paths: List[str]) -> List[str]:
    """把拖进来的路径/快捷方式转成可下载的 URL。

    - http(s):// 直接用
    - .url/.webloc 读里面的 URL
    - 其余本地文件 → file:// URI（引擎会走本地复制）
    """
    out: List[str] = []
    for raw in paths:
        p = (raw or "").strip().strip('"')
        if not p:
            continue
        low = p.lower()
        if low.startswith(("http://", "https://", "ftp://", "magnet:", "ed2k://")):
            out.append(p)
            continue
        f = pathlib.Path(p)
        u = _url_from_shortcut(f)
        if u:
            out.append(u)
            continue
        if f.is_file():
            out.append(f.as_uri() if hasattr(f, "as_uri") else "file:///" + str(f).replace("\\", "/"))
        elif f.is_dir():
            # 拖文件夹：交给整站抓取不现实，改成"把里面的种子/清单文件投进去"
            for sub in sorted(f.glob("*")):
                if sub.suffix.lower() in (".torrent", ".txt", ".json"):
                    out.append(sub.as_uri())
    return out


class DropWindow:
    """一个常驻的小悬浮窗，接收拖放并把 URL 交给回调。"""

    CLASS_NAME = "JiliuDropWnd"
    WIDTH = 216
    HEIGHT = 92

    def __init__(self, on_urls: Callable[[List[str]], None],
                 x: Optional[int] = None, y: Optional[int] = None,
                 title: str = "拖到这里下载"):
        self.on_urls = on_urls
        self.title = title
        self.hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._error = ""
        self._proc = WNDPROC(self._wndproc)   # 必须持引用，否则回调被 GC
        self._cls_atom = None
        self._x = x
        self._y = y
        self._drag_origin = None
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32
        self._declare()

    # ---------------- Win32 声明 ----------------
    def _declare(self) -> None:
        u = self._user32
        u.DefWindowProcW.restype = ctypes.c_ssize_t
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
        # 拖放三件套在 shell32，不在 user32 —— 和 Shell_NotifyIconW 同一个坑
        sh = ctypes.windll.shell32
        self._shell32 = sh
        sh.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        sh.DragQueryFileW.restype = wintypes.UINT
        sh.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                      wintypes.LPWSTR, wintypes.UINT]
        sh.DragFinish.argtypes = [wintypes.HANDLE]
        u.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                 ctypes.c_byte, wintypes.DWORD]
        self._gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        self._gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
        self._gdi32.CreatePen.restype = wintypes.HPEN
        self._gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        self._gdi32.RoundRect.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int]
        self._gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        self._gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
        self._gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        u.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                                ctypes.POINTER(wintypes.RECT), wintypes.UINT]
        # CreateFontW 在 gdi32；DrawTextW 在 user32 —— 两个库，别混
        self._gdi32.CreateFontW.restype = wintypes.HFONT
        self._gdi32.CreateFontW.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.LPCWSTR]
        self._gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.UINT]
        # ---- 句柄型参数必须显式声明，否则 64 位下按 c_int 传会被截断 ----
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.BeginPaint.restype = wintypes.HDC
        u.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        u.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
        u.InvalidateRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT),
                                     wintypes.BOOL]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.UpdateWindow.argtypes = [wintypes.HWND]
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.LoadCursorW.restype = wintypes.HANDLE
        # IDC_ARROW 是 MAKEINTRESOURCE(32512)，第二个参数得按指针收，
        # 声明成 LPCWSTR 会要求字符串，传整数直接 ArgumentError
        u.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
        u.RegisterClassExW.restype = wintypes.WORD
        u.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        u.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        u.GetMessageW.restype = ctypes.c_int
        u.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                  wintypes.UINT, wintypes.UINT]
        u.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
        u.DispatchMessageW.restype = ctypes.c_ssize_t
        u.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.argtypes = [ctypes.c_int]

    # ---------------- 窗口过程 ----------------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        u = self._user32
        try:
            if msg == WM_DROPFILES:
                self._on_drop(wparam)
                return 0
            if msg == WM_PAINT:
                self._paint(hwnd)
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_DESTROY:
                u.PostQuitMessage(0)
                return 0
            if msg == WM_QUIT_APP:
                u.DestroyWindow(hwnd)
                return 0
            if msg == WM_LBUTTONDOWN:
                pt = wintypes.POINT()
                u.GetCursorPos(ctypes.byref(pt))
                r = wintypes.RECT()
                u.GetWindowRect(hwnd, ctypes.byref(r))
                self._drag_origin = (pt.x - r.left, pt.y - r.top)
                return 0
            if msg == WM_MOUSEMOVE and self._drag_origin:
                pt = wintypes.POINT()
                u.GetCursorPos(ctypes.byref(pt))
                u.SetWindowPos(hwnd, 0, pt.x - self._drag_origin[0],
                               pt.y - self._drag_origin[1], 0, 0, 0x0001 | 0x0004)
                return 0
            if msg == WM_LBUTTONUP:
                self._drag_origin = None
                return 0
            if msg == WM_RBUTTONUP:
                # 右键 = 隐藏（不退出，托盘里还能叫回来）
                u.ShowWindow(hwnd, 0)
                return 0
            if msg == WM_CLOSE:
                u.ShowWindow(hwnd, 0)
                return 0
        except Exception:
            pass
        return u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_drop(self, hdrop) -> None:
        u = self._shell32
        try:
            n = u.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
            paths: List[str] = []
            for i in range(n):
                need = u.DragQueryFileW(hdrop, i, None, 0)
                buf = ctypes.create_unicode_buffer(need + 1)
                u.DragQueryFileW(hdrop, i, buf, need + 1)
                if buf.value:
                    paths.append(buf.value)
        finally:
            u.DragFinish(hdrop)
        urls = urls_from_paths(paths)
        if urls:
            try:
                self.on_urls(urls)
            except Exception:
                pass
            self._flash()
        else:
            self.title = "没认出可下载的链接"
            self._invalidate()
            # 以前失败提示会一直粘着，直到下次拖对东西
            threading.Timer(2.0, self._reset_title).start()

    def _flash(self) -> None:
        self.title = "已加入队列"
        self._invalidate()
        threading.Timer(1.6, self._reset_title).start()

    def _reset_title(self) -> None:
        self.title = "拖到这里下载"
        self._invalidate()

    def _invalidate(self) -> None:
        if self.hwnd:
            try:
                self._user32.InvalidateRect(self.hwnd, None, True)
            except Exception:
                pass

    def _paint(self, hwnd) -> None:
        u, g = self._user32, self._gdi32
        ps = PAINTSTRUCT()
        hdc = u.BeginPaint(hwnd, ctypes.byref(ps))
        try:
            r = wintypes.RECT()
            u.GetClientRect(hwnd, ctypes.byref(r))
            w, h = r.right, r.bottom
            # 底色（深）、描边（青）
            brush = g.CreateSolidBrush(0x00241E1A)      # BGR: #1a1e24
            pen = g.CreatePen(0, 1, 0x00D9A85C)         # #5ca8d9 -> BGR
            g.SelectObject(hdc, brush)
            g.SelectObject(hdc, pen)
            g.RoundRect(hdc, 1, 1, w - 1, h - 1, 20, 20)
            g.SetBkMode(hdc, TRANSPARENT)
            g.SetTextColor(hdc, 0x00DCE2E8)             # #e8e2dc
            font = g.CreateFontW(-15, 0, 0, 0, FW_SEMIBOLD, 0, 0, 0,
                                 DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY,
                                 0, "Microsoft YaHei UI")
            g.SelectObject(hdc, font)
            box = wintypes.RECT(8, 10, w - 8, h - 26)
            u.DrawTextW(hdc, self.title, -1, ctypes.byref(box),
                        DT_CENTER | DT_VCENTER | DT_SINGLELINE)
            g.SetTextColor(hdc, 0x008C949A)              # #9a948c
            font2 = g.CreateFontW(-12, 0, 0, 0, FW_NORMAL, 0, 0, 0,
                                  DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY,
                                  0, "Microsoft YaHei UI")
            g.SelectObject(hdc, font2)
            box2 = wintypes.RECT(8, h - 30, w - 8, h - 8)
            u.DrawTextW(hdc, "链接 / 文件 / 种子   右键隐藏", -1, ctypes.byref(box2),
                        DT_CENTER | DT_VCENTER | DT_SINGLELINE)
            for obj in (font, font2, pen, brush):
                try:
                    g.DeleteObject(obj)
                except Exception:
                    pass
        finally:
            u.EndPaint(hwnd, ctypes.byref(ps))

    # ---------------- 生命周期 ----------------
    def _run(self) -> None:
        u = self._user32
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = CS_DROPSHADOW
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.hCursor = u.LoadCursorW(None, 32512)   # IDC_ARROW
        wc.lpszClassName = self.CLASS_NAME
        wc.hbrBackground = None
        atom = u.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            self._error = f"RegisterClassEx 失败 err={ctypes.get_last_error()}"
            self._ready.set()
            return
        self._cls_atom = atom

        # 默认位置：屏幕右下角往上一点
        sw = u.GetSystemMetrics(0)
        sh = u.GetSystemMetrics(1)
        x = self._x if self._x is not None else sw - self.WIDTH - 40
        y = self._y if self._y is not None else sh - self.HEIGHT - 120
        hwnd = u.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED,
            self.CLASS_NAME, "汲流 · 拖拽投递",
            WS_POPUP, x, y, self.WIDTH, self.HEIGHT,
            None, None, hinst, None)
        if not hwnd:
            self._error = f"CreateWindowEx 失败 err={ctypes.get_last_error()}"
            self._ready.set()
            u.UnregisterClassW(self.CLASS_NAME, hinst)
            return
        self.hwnd = hwnd
        self._shell32.DragAcceptFiles(hwnd, True)
        u.SetLayeredWindowAttributes(hwnd, 0, 232, LWA_ALPHA)
        u.ShowWindow(hwnd, 5)          # SW_SHOW
        u.UpdateWindow(hwnd)
        self._ready.set()

        msg = MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        try:
            u.UnregisterClassW(self.CLASS_NAME, hinst)
        except Exception:
            pass

    def start(self, timeout: float = 5.0) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="jiliu-dropwin")
        self._thread.start()
        self._ready.wait(timeout)
        return self.hwnd is not None

    @property
    def error(self) -> str:
        return self._error

    def show(self) -> None:
        if self.hwnd:
            self._user32.ShowWindow(self.hwnd, 5)
            self._user32.SetForegroundWindow(self.hwnd)

    def hide(self) -> None:
        if self.hwnd:
            self._user32.ShowWindow(self.hwnd, 0)

    def visible(self) -> bool:
        return bool(self.hwnd and self._user32.IsWindowVisible(self.hwnd))

    def stop(self) -> None:
        if self.hwnd:
            try:
                self._user32.PostMessageW(self.hwnd, WM_QUIT_APP, 0, 0)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.hwnd = None
