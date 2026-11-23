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

1.8.0 新增：
    update     自动更新（检查 + 静默替换）
    shellmenu  系统右键菜单注册
    dropwin    拖拽投递悬浮窗（Win32）
    queues     多队列（各自并发/限速/优先级）
    batch      批量粘贴、通配符展开、预探测
    checksum   校验和（MD5/SHA1/SHA256/SHA512 + sidecar 比对）
    finish     全部完成后关机/休眠/锁定
    presets    站点预设（Smart Mode）
    channels   Webhook 多通知渠道
    live       HLS 直播流录制
    danmaku    B 站弹幕下载与 ASS 转换
    transfer   任务导入导出（JSON/aria2/txt）
    plugins    解析插件机制
    i18n       界面语言
    theme      主题与配色

1.9.2 新增：
    pagesniff  网页播放页解析（player 变量 / <video> / iframe / 内联脚本
               / og:video / 流直链），并把 Referer·Origin·Cookie·UA
               一路透传给下载引擎 —— 免费影视站与防盗链 CDN 的解法

1.9.4 修的是「协议唤起了但扩展没跟上」：
    扩展只给建任务（ADD）做了唤醒，视频悬浮按钮那条路 fetch 一失败
    就直接弹「请先启动汲流」—— 用户点的正是那条路。现在四条入口
    （建任务 / 悬浮按钮 / 播放页解析 / 右键解析）统一过 withWake。
    另外补了两个会让「应用明明开着却唤不起」的结构性坑：
      · desktop.pick_port 会在 8788 被占时顺延，而扩展只认 8788；
        现在扩展和唤起器都会扫一段端口，靠 /api/health 的 app 字段
        认身份（不把别人的本地服务当汲流）
      · runtime.json 丢了或 pid 对不上时，手上有实例却判定"没有"，
        于是又开一个 —— 两个进程抢 tasks.json，任务看起来会消失
    中转页也不再 2.6 秒无条件自关：改为探到服务就绪才关，
    否则按钮露出来 1 秒页面就没了，用户连点都来不及点。

1.8.1 修的是「功能做了但用户用不到」：
    队列并发字段名对不上（改不动）· 版本号显示 v? ·
    aria2 后端丢掉镜像与预期哈希 · 探测提示吐出 HTML 标签 ·
    新建任务没有参数面板 · 校验和/站点预设/队列归属没有入口 ·
    本机与内网地址被塞给代理 · .bin 被改判成 .txt ·
    抽屉表单宽度塌陷与操作后不刷新 · 发布包会带上 downloads
"""
from __future__ import annotations

import pathlib
import sys

__version__ = "1.9.6"
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
