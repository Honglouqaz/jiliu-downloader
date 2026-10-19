"""配置管理。

配置默认落在 ~/.jiliu/config.json，首次运行自动生成。

便携模式：在程序（或 exe）旁边放一个名为 `portable` 的空文件，
状态就改存到程序目录下的 data/，不动用户家目录 —— 拷走整个文件夹
就是完整的搬家，适合装在 U 盘或绿色目录里。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any, Dict


def _app_dir() -> pathlib.Path:
    """程序所在目录（源码运行 = 项目根，冻结运行 = exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent.parent


def is_portable() -> bool:
    """是否便携模式。可用环境变量强制打开。"""
    if (os.environ.get("JILIU_PORTABLE") or "").strip().lower() in ("1", "true", "yes"):
        return True
    d = _app_dir()
    for marker in ("portable", "portable.txt", ".portable"):
        try:
            if (d / marker).exists():
                return True
        except Exception:
            pass
    return False


def _home() -> pathlib.Path:
    # 环境变量优先级最高，测试与多实例都靠它隔离
    env = os.environ.get("JILIU_HOME")
    if env:
        return pathlib.Path(env)
    if is_portable():
        return _app_dir() / "data"
    return pathlib.Path.home() / ".jiliu"


HOME = _home()
CONFIG_PATH = HOME / "config.json"
TASKS_PATH = HOME / "tasks.json"

DEFAULTS: Dict[str, Any] = {
    # 下载目录。便携模式下放程序目录里，整包拷走不留尾巴
    "download_dir": str((_app_dir() / "downloads") if is_portable()
                        else (pathlib.Path.home() / "Downloads")),
    # 分片线程数
    "threads": 8,
    # 单请求超时（秒）
    "timeout": 30,
    # 单个分片重试次数
    "max_retries": 5,
    # 全局限速，字节/秒，0 表示不限
    "speed_limit": 0,
    # 面板/API 端口
    "port": 8788,
    # 同时下载的任务数上限
    "max_concurrent": 3,
    # 允许访问本地 API 的来源（Origin 白名单，空表示只允许扩展与本机页面）
    "allowed_origins": [],
    # 小于该大小不走多线程
    "min_multipart": 1024 * 1024,
    # 分片块大小上下限
    "chunk_min": 512 * 1024,
    "chunk_max": 8 * 1024 * 1024,
    # 暂存目录（学 IDM 的 TempPath）：分片先写这里，校验完再搬到下载目录。
    # 留空 = 与目标同目录，零搬运；填另一块盘可以把 IO 压力分开
    "temp_dir": "",
    # yt-dlp / ffmpeg / aria2 可执行文件路径（留空则自动探测）
    "ytdlp_path": "",
    "ffmpeg_path": "",
    "aria2_path": "",
    "curl_path": "",
    "wget_path": "",

    # 下载引擎：native（自研）/ aria2 / curl / wget / auto
    "backend": "auto",
    # auto 时的尝试顺序。native 永远可用，所以它排第几决定了
    # 前面的引擎能否被自动选中。见 jiliu/engines.py
    "engine_order": ["aria2", "native", "curl", "wget"],
    # 下载失败后自动换个引擎再试一轮（native 撞上挑食的服务端时很有用）
    "auto_fallback_engine": True,

    # aria2 接入
    "aria2_port": 6800,
    "aria2_secret": "jiliu",
    "aria2_split": 16,
    "aria2_max_conn": 16,
    # 参数档位：auto = 按资源分类自动选；video / default / p2p = 锁定某一档。
    # 视频 CDN 对高并发敏感（4K Video Downloader 自己都警告会被封 IP），
    # 所以视频档比 BT 档保守得多。见 jiliu/backends/aria2.py
    "aria2_profile": "auto",
    "aria2_video_conn": 4,
    "aria2_video_min_split": "16M",
    "aria2_default_conn": 8,
    "aria2_default_min_split": "8M",
    "aria2_p2p_conn": 16,
    "aria2_p2p_min_split": "1M",
    # 磁盘缓存：写盘前先在内存里攒一攒，减少小 IO
    "aria2_disk_cache": "24M",
    # 连到已有的 aria2 RPC（留空则跳过）。Motrix 默认在 16800
    "aria2_rpc_url": "",
    "aria2_rpc_secret": "",
    "motrix_rpc_secret": "",

    # 视频默认格式：bv*+ba/b 表示"最佳画质+最佳音轨，否则最佳单文件"
    "video_format": "bv*+ba/b",
    # 视频容器
    "video_container": "mp4",
    # 单视频优先走 B 站原生 DASH（playurl + WBI），失败自动回退 yt-dlp。
    # 好处：画质表一次拿全、少起一个进程；坏处：接口变了要跟着改
    "bili_native": True,
    # 期望画质（qn）：0 = 能拿到的最高；120 = 4K；116 = 1080P60；80 = 1080P
    "bili_qn": 0,
    # 同画质下的编码偏好：avc（兼容最好）/ hevc（体积小）/ av1
    "bili_codec": "avc",
    # 视频保存子目录（相对 download_dir）。
    # 留空 = 交给 jiliu/layout.py 按分类归档（推荐）；填了就优先用它。
    # 老版本这里默认是 "videos"，见 load() 里的迁移。
    "video_dir": "",

    # ---- 分类归档（见 jiliu/layout.py）----
    # 按资源类型分到 <下载目录>/视频、/软件、/压缩包 … 里
    "auto_subdir": True,
    # 用户在新建任务时手填了目录，还要不要再套一层分类目录
    "subdir_explicit": False,
    # 单个分类的目录名覆盖，例：{"video": "我的视频", "other": ""}
    "subdir_names": {},

    # ---- 版本形态（见 jiliu/edition.py）----
    # cn     = 国内版：只用国内站点与直连，界面上不出现外网相关功能
    # global = 全球版：含海外站点、代理分流、YouTube 等（默认）
    "edition": "global",

    # 请求头
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 Jiliu/1.0"
    ),
    "referer": "",
    "cookie_file": "",
    # 视频路径专用的 UA。**留空表示不加**，让 yt-dlp 用它自己按站点调好的。
    # 上面那个 user_agent 带 "Jiliu/1.0" 后缀，拿它去请求 YouTube
    # 等于举手说自己是机器人，风控更容易盯上。
    "video_user_agent": "",

    # ---- 代理（见 jiliu/proxy.py）----
    # system = 跟随 Windows 系统代理（默认，也是大多数下载软件的做法）
    # auto   = 只读环境变量，先验活；manual = 用下面 proxy 里填的
    # off    = 全部直连
    "proxy_mode": "system",
    "proxy": "",
    # 国内站默认直连：套代理更慢，还可能触发风控
    "proxy_domestic": False,
    "proxy_foreign": True,
    "proxy_user": "",
    "proxy_pass": "",
    # 系统代理也连不上时，是否自动退回直连（不然所有请求都会撞死）
    "proxy_fallback_direct": True,

    # ---- Cookie（见 jiliu/cookies.py）----
    # auto = 优先用 cookies/ 目录里按域名匹配的文件
    "cookie_mode": "auto",
    "cookie_browser": "",
    "cookie_browser_profile": "",

    # ---- 视频 / 合集 ----
    # JS 运行时：新 yt-dlp 解 YouTube 的 n-sig 要用。auto = 自动找 node / deno
    "js_runtimes": "auto",
    # 合集最多展开多少条（太多会拖慢探测，也刷爆界面）
    "playlist_limit": 200,
    # 合集是否按合集名建子目录，避免几十个文件糊在一起
    "playlist_folder": True,
    "playlist_reverse": False,
    # 下载时并发几个条目（对站点友好，别开太大）
    "playlist_concurrent": 2,

    # ---- 输出 ----
    "audio_only": False,
    "audio_format": "mp3",
    "write_subs": False,
    "sub_langs": "zh-Hans,zh-CN,en",
    "embed_subs": True,

    # ---- 桌面窗口 ----
    # auto = 用 Edge/Chrome 的应用模式弹独立窗口（像原生软件）
    "window_mode": "auto",

    # ---- BT / 磁力 / ed2k ----
    # 这些只有 aria2 能做（自研引擎不做 P2P）
    "bt_enabled": True,
    # 下载完继续做种多少秒。0 = 不做种 —— 用户要的是下文件，
    # 不是被默默变成种子服务器占掉上行
    "bt_seed_time": 0,
    "bt_seed_ratio": 0.0,
    "bt_max_peers": 64,
    "bt_listen_port": 6881,
    # 额外 tracker，逗号分隔。磁力自带 tracker 不够时补这里
    "bt_trackers": "",

    # ---- 命名与归档（见 jiliu/naming.py）----
    # 变量写法：%title% %id% %up% %pl_index% 等，设置页里有可点选的列表
    "name_pattern": "%title% [%id%]",
    # 归档目录模板，留空表示直接放下载目录
    "dir_pattern": "",
    # 合集是否单独建一层目录
    "playlist_folder": True,
    # 下过的还下不下。开着的话同一集不会重复下载
    "skip_downloaded": True,

    # ---- 重复链接（学 IDM 的 DuplLinks 三选）----
    # rename = 自动加 (1)；skip = 跳过；overwrite = 覆盖；
    # resume = 当成续传（文件已存在就接着下）
    "duplicate_policy": "rename",

    # ---- 完成后的动作（学 4K VD / IDM）----
    # 系统通知 + 托盘常驻 + 下载期间阻止睡眠
    "notify_enabled": True,
    "tray_enabled": True,
    "prevent_sleep": True,
    # 完成后执行的命令，%file% / %dir% 会被替换。留空 = 不执行
    "after_download_command": "",
    # 合集下载完生成 .m3u（播放器按顺序播）
    "m3u_enabled": True,
    # 文件名序号补零位数（合集 > 9 集时不至于 10 排到 2 前面）
    "number_pad": 2,

    # ---- 订阅（见 jiliu/feed.py + jiliu/subscriptions.py）----
    # 每隔多少分钟检查一次订阅源；0 = 关闭定时检查
    "feed_check_minutes": 60,
    # 单个订阅源一次最多入队多少条新条目（防手滑订阅大站）
    "feed_max_per_check": 10,

    # ---- 计划任务 / 时段限速（见 jiliu/schedule.py）----
    # enabled 打开后：只在 days 指定的星期、start–end 时段内下载；
    # 其余时间自动暂停队列。speed_limit 在该时段生效（0 = 不限）
    "schedule": {
        "enabled": False,
        "days": [0, 1, 2, 3, 4, 5, 6],
        "start": "00:00",
        "end": "07:00",
        "speed_limit": 0,
    },

    # ---- 接管黑名单（对齐 IDM 的 ExceptionServers）----
    # 域名 glob / 后缀 / 体积阈值：命中就不弹接管提示
    "capture_exceptions": [
        "*.update.microsoft.com",
        "*.windowsupdate.com",
        "media.tenor.co",
        "*.giphy.com",
        "*.gstatic.com",
    ],
    # 超过这个体积的媒体不进嗅探列表（MB），0 = 不限
    "capture_max_mb": 0,
    # 本地嗅探代理（P1-1）：默认关。开了以后没装扩展的浏览器/Electron
    # 客户端也能被接管，代价是多一个本机端口
    "sniff_proxy_enabled": False,
    "sniff_proxy_port": 8899,
    # 剪贴板监听（IDM 的 MonitorUrlClipboard）：默认关，开了会轮询剪贴板
    "clipboard_watch": False,

    # ---- 站点抓取（见 jiliu/grabber.py）----
    "grab_max_depth": 2,
    "grab_max_pages": 200,
    "grab_same_origin": True,

    # ---- 限速预设（任务级下拉用）----
    "speed_presets": [0, 1048576, 5242880, 20971520],
}

_cache: Dict[str, Any] | None = None


def load(force: bool = False) -> Dict[str, Any]:
    """读配置，缺失项用默认值补齐并写回。"""
    global _cache
    if _cache is not None and not force:
        return _cache

    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
        except Exception:
            pass

    _migrate(cfg)
    _cache = cfg
    save(cfg)
    return cfg


# 老默认值 → 新默认值的迁移表。
# 这些值本身没写错，是新版本改了「默认」的含义，而老配置文件里
# 躺着的正是旧默认值 —— 分不清用户是主动写的还是当年被写进去的，
# 一律按「当年被写进去的」处理（用户主动改回旧值的概率极低）。
_MIGRATIONS: Dict[str, tuple] = {
    # video_dir 的职责并给了 layout.py（按分类归档），空 = 交给 layout
    "video_dir": ("videos", ""),
    # 代理默认从「读环境变量」改成「跟随系统代理」
    "proxy_mode": ("auto", "system"),
}


def _migrate(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """就地迁移老配置。幂等 —— 迁移后的值等于新默认值，再跑一次无副作用。"""
    for key, (old, new) in _MIGRATIONS.items():
        if cfg.get(key) == old:
            cfg[key] = new
    return cfg


def save(cfg: Dict[str, Any]) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    global _cache
    _cache = cfg


def update(patch: Dict[str, Any]) -> Dict[str, Any]:
    """局部更新，只接受已知键。"""
    cfg = load()
    for k, v in patch.items():
        if k in DEFAULTS:
            cfg[k] = v
    save(cfg)
    return cfg


def download_dir(cfg: Dict[str, Any] | None = None) -> pathlib.Path:
    cfg = cfg or load()
    p = pathlib.Path(cfg["download_dir"]).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p
