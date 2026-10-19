"""Windows 托盘图标（纯 ctypes，无第三方依赖）。

为什么自己写：pystray/Pillow 是可选依赖，打包时不一定在；而"关掉窗口还能
在后台跑"是下载器的基本预期。这里用 Shell_NotifyIconW 直接挂一个托盘图标，
菜单三项：打开面板 / 暂停全部 / 退出。

失败一律返回 False，绝不让托盘拖垮主程序。
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, Optional

WM_APP = 0x8000
WM_TRAY = WM_APP + 20
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_QUIT = 0x0012

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04

MF_STRING = 0x0000
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002

ID_OPEN, ID_PAUSE, ID_QUIT = 1001, 1002, 1003


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
    ]


class Tray:
    def __init__(self, on_open: Optional[Callable[[], None]] = None,
                 on_pause: Optional[Callable[[], None]] = None,
                 on_quit: Optional[Callable[[], None]] = None,
                 tooltip: str = "汲流"):
        self.on_open = on_open or (lambda: None)
        self.on_pause = on_pause or (lambda: None)
        self.on_quit = on_quit or (lambda: None)
        self.tooltip = tooltip[:127]
        self._hwnd = None
        self._tid = 0
        self._th: Optional[threading.Thread] = None
        self._wndproc = None
        self._ok = False
        self._err = ""

    # ---- 生命周期 ----

    def start(self) -> bool:
        try:
            self._th = threading.Thread(target=self._run, daemon=True, name="jiliu-tray")
            self._th.start()
            self._th.join(1.5)
            return bool(self._ok)
        except Exception:  # noqa: BLE001
            return False

    def stop(self) -> None:
        try:
            if self._tid:
                ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        except Exception:  # noqa: BLE001
            pass
        self._ok = False

    # ---- 内部 ----

    def _run(self) -> None:
        try:
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            shell = ctypes.windll.shell32
            _declare(u, shell)
            hinst = k.GetModuleHandleW(None)
            self._tid = k.GetCurrentThreadId()
            cls = "JiliuTrayWnd"

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND,
                                          wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            self._wndproc = WNDPROC(self._proc)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = cls
            if not u.RegisterClassW(ctypes.byref(wc)):
                self._err = f"RegisterClass failed {k.GetLastError()}"
                return

            self._hwnd = u.CreateWindowExW(0, cls, "jiliu", 0, 0, 0, 0, 0,
                                            None, None, hinst, None)
            if not self._hwnd:
                self._err = f"CreateWindowEx failed {k.GetLastError()}"
                return

            nid = _NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            # IDI_APPLICATION 是 MAKEINTRESOURCE(32512)，要按指针传
            nid.hIcon = u.LoadIconW(None, ctypes.cast(ctypes.c_void_p(32512), wintypes.LPCWSTR))
            nid.szTip = self.tooltip
            if not shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                self._err = f"Shell_NotifyIcon failed {k.GetLastError()}"
                return
            self._ok = True

            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))

            shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            u.DestroyWindow(self._hwnd)
        except Exception as exc:  # noqa: BLE001
            self._err = f"{type(exc).__name__}: {exc}"
            self._ok = False

    def _proc(self, hwnd, msg, wparam, lparam):
        u = ctypes.windll.user32
        try:
            if msg == WM_TRAY:
                event = int(lparam) & 0xFFFF
                if event == WM_LBUTTONUP:
                    self.on_open()
                elif event == WM_RBUTTONUP:
                    self._menu(hwnd)
                return 0
            if msg == 0x0010:  # WM_CLOSE
                u.DestroyWindow(hwnd)
                return 0
            if msg == 0x0002:  # WM_DESTROY
                u.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001
            pass
        try:
            return u.DefWindowProcW(hwnd, msg,
                                    ctypes.c_size_t(wparam).value,
                                    ctypes.c_ssize_t(lparam).value)
        except Exception:  # noqa: BLE001
            return 0

    def _menu(self, hwnd) -> None:
        u = ctypes.windll.user32
        hmenu = u.CreatePopupMenu()
        u.AppendMenuW(hmenu, MF_STRING, ID_OPEN, "打开汲流面板")
        u.AppendMenuW(hmenu, MF_STRING, ID_PAUSE, "暂停 / 继续全部")
        u.AppendMenuW(hmenu, MF_STRING, ID_QUIT, "退出")
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(hwnd)
        cmd = u.TrackPopupMenu(hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                               pt.x, pt.y, 0, hwnd, None)
        u.DestroyMenu(hmenu)
        if cmd == ID_OPEN:
            self.on_open()
        elif cmd == ID_PAUSE:
            self.on_pause()
        elif cmd == ID_QUIT:
            self.on_quit()


def supported() -> bool:
    import os
    return os.name == "nt"


def _declare(u, shell=None) -> None:
    """显式声明签名 —— 64 位下 LPARAM/WPARAM 不声明就会 OverflowError。"""
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
    u.DefWindowProcW.restype = ctypes.c_ssize_t
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                  wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    u.CreateWindowExW.restype = wintypes.HWND
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                              wintypes.UINT, wintypes.UINT]
    u.GetMessageW.restype = ctypes.c_int
    u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    sh = shell or ctypes.windll.shell32
    sh.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
    sh.Shell_NotifyIconW.restype = wintypes.BOOL
    u.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    u.LoadIconW.restype = wintypes.HICON
    u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT,
                              wintypes.LPCWSTR]
    u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                 wintypes.LPVOID]
    u.TrackPopupMenu.restype = wintypes.UINT
