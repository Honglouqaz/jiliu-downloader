"""汲流 —— 自建多线程下载器。

模块划分：
    config   配置读写
    tools    外部二进制探测（yt-dlp / ffmpeg / curl / wget）
    engine   多线程分片下载核心
    task     任务模型与状态机
    manager  任务管理器
    video    yt-dlp 视频解析与下载
    server   本地 HTTP API + 面板
    desktop  原生桌面窗口（pywebview）
    layout   下载目录的分类归档布局
    proxy    代理选择（系统代理 / 手动 / 直连）
    qr       二维码渲染（扫码登录用）
    accounts 多平台登录态（B站/抖音扫码；其余平台 Cookie 引导）
    bili_dash B 站原生取流（playurl + WBI + DASH 双流合并）
    schedule 计划任务 / 时段限速
    subscriptions 订阅源定时检查与自动入队
    grabber  轻量站点抓取（IDM Site Grabber 的保守版）
    clipboard 剪贴板链接监听
    capture_rules 接管例外站点 / 噪音后缀
    sniffproxy 本地嗅探代理（覆盖没装扩展的客户端）
    notify   系统通知 / 阻止休眠 / 完成后命令
    tray     Windows 托盘图标
    cli      命令行入口
"""
from __future__ import annotations

import pathlib
import sys

__version__ = "1.7.1"
APP_NAME = "汲流"


def _bootstrap_vendor() -> None:
    """把 vendor/ 挂到 sys.path，让内置的纯 Python 包（yt_dlp / qrcode / pypng）
    在本进程里也能直接 import。

    以前只有子进程能用到 vendor/（靠 tools.tool_env 注入 PYTHONPATH），
    主进程 import 不到 —— 于是 `import qrcode` 在源码运行下会失败。
    这里补上，三种运行形态都覆盖：源码 / PyInstaller 解包目录 / 便携安装。
    """
    here = pathlib.Path(__file__).resolve()
    cands = [
        # 源码：<项目>/jiliu/__init__.py → <项目>/vendor
        here.parent.parent / "vendor",
        # PyInstaller onefile：解包目录 / vendor
        pathlib.Path(getattr(sys, "_MEIPASS", "") or "/nonexistent") / "vendor",
        # 便携：exe 旁边的 vendor/
        pathlib.Path(sys.executable).resolve().parent / "vendor"
        if getattr(sys, "frozen", False) else here.parent.parent / "vendor",
    ]
    for d in cands:
        try:
            if d.is_dir():
                s = str(d)
                if s not in sys.path:
                    sys.path.append(s)
        except Exception:  # noqa: BLE001
            continue


_bootstrap_vendor()
