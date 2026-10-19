"""系统通知 + 阻止休眠。

不引第三方依赖：
  - Windows 10/11 的 toast 直接走 PowerShell 的 WinRT 接口（系统自带）；
    失败就退化成一声响，绝不让"通知发不出去"影响下载。
  - 阻止休眠用 SetThreadExecutionState。这个标志是线程级的，所以必须
    在线程里成对调用 —— 由 manager 在任务开始/结束时调用。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Optional

# ES_CONTINUOUS | ES_SYSTEM_REQUIRED
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_sleep_lock = threading.Lock()
_sleep_depth = 0

# 用 PowerShell 自己的 AppID，Win10 上不需要额外注册
_PS_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:JILIU_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:JILIU_TOAST_APP).Show($toast)
"""


def _xml_escape(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def notify(title: str, body: str = "", *, silent: bool = False) -> bool:
    """弹一条系统通知。返回是否真的发出去了。

    silent=True 时不发声（用于"开始下载"这类低价值事件）。
    """
    title = str(title or "汲流")
    body = str(body or "")
    if os.environ.get("JILIU_NO_NOTIFY"):
        return False
    if os.name == "nt":
        xml = ("<toast><visual><binding template=\"ToastText02\">"
               f"<text id=\"1\">{_xml_escape(title)}</text>"
               f"<text id=\"2\">{_xml_escape(body)}</text>"
               "</binding></visual></toast>")
        env = dict(os.environ, JILIU_TOAST_XML=xml, JILIU_TOAST_APP=_PS_APP_ID)
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                 "-Command", _TOAST_PS],
                env=env, capture_output=True, timeout=12,
                creationflags=0x08000000,
            )
            return True
        except Exception:  # noqa: BLE001
            pass
    if not silent:
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:  # noqa: BLE001
            pass
    return False


def beep(ok: bool = True) -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK if ok else winsound.MB_ICONHAND)
    except Exception:  # noqa: BLE001
        pass


def prevent_sleep(enable: bool) -> bool:
    """阻止系统进入睡眠（下载期间）。可重入：多层调用只真正生效一次。"""
    global _sleep_depth
    if os.name != "nt":
        return False
    with _sleep_lock:
        if enable:
            _sleep_depth += 1
            if _sleep_depth > 1:
                return True
        else:
            if _sleep_depth <= 0:
                return True
            _sleep_depth -= 1
            if _sleep_depth > 0:
                return True
        try:
            import ctypes
            flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if enable else 0)
            return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
        except Exception:  # noqa: BLE001
            return False


def run_after_command(cfg: dict, path: Optional[str]) -> None:
    """下载完成后的自定义命令。%file% / %dir% 会被替换。"""
    cmd = str((cfg or {}).get("after_download_command") or "").strip()
    if not cmd:
        return
    if path:
        p = os.path.abspath(str(path))
        cmd = cmd.replace("%file%", p).replace("%dir%", os.path.dirname(p))
    else:
        cmd = cmd.replace("%file%", "").replace("%dir%", "")
    try:
        subprocess.Popen(cmd, shell=True, creationflags=0x08000000 if os.name == "nt" else 0)
    except Exception:  # noqa: BLE001
        pass


def platform_supported() -> dict:
    return {
        "toast": os.name == "nt",
        "prevent_sleep": os.name == "nt",
        "python": sys.version.split()[0],
    }
