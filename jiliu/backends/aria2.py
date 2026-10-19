"""aria2 后端 —— 通过 JSON-RPC 调用 aria2c。

这就是 Motrix 的做法：aria2 负责真正下载，外面套一层管理和界面。
两种接入方式：

    spawn   汲流自己拉起一个 aria2c 进程（--enable-rpc），用完即停
    attach  连到已在运行的 RPC 端点 —— 比如 Motrix（默认 127.0.0.1:16800/jsonrpc）

协议：
    POST <url>
    {"jsonrpc":"2.0","id":"jiliu","method":"aria2.addUri",
     "params":["token:<secret>", ["<uri>"], {"dir":"...","out":"..."}]}
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .. import config as _config, tools
from ..engine import DownloadError, human_size

# Motrix 默认把 aria2 的 RPC 开在这里
MOTRIX_RPC = "http://127.0.0.1:16800/jsonrpc"

# 探测结果缓存。detect() 要 spawn 一次 aria2c --version，还要对 Motrix RPC
# 做一次 1.5 秒超时的连接尝试（没开 Motrix 就白等）。而 backends.describe()
# 一次调用里就会跑它两回，面板又是轮询 describe 的 —— 不缓存太亏。
DETECT_TTL = 60.0
_DETECT_CACHE: Dict[str, Any] = {"t": 0.0, "key": None, "val": None}


def _no_window() -> int:
    return 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------------------
# 参数档位
# --------------------------------------------------------------------------
# 一套参数打所有站是不对的：视频 CDN（B 站 / YouTube）对同 IP 的并发连接
# 很敏感，4K Video Downloader 自己在 UI 里都警告「高强度可能被临时封 IP」；
# 而 BT 恰恰相反，连接越多越容易找到源。所以按资源类型分三档。

PROFILES = ("video", "default", "p2p")

_PROFILE_KEYS = {
    "video": ("aria2_video_conn", "aria2_video_min_split", 4, "16M"),
    "default": ("aria2_default_conn", "aria2_default_min_split", 8, "8M"),
    "p2p": ("aria2_p2p_conn", "aria2_p2p_min_split", 16, "1M"),
}

_VIDEO_HOSTS = (
    "bilibili.com", "b23.tv", "youtube.com", "youtu.be", "vimeo.com",
    "twitch.tv", "douyin.com", "ixigua.com", "kuaishou.com", "iqiyi.com",
    "v.qq.com", "youku.com", "mgtv.com", "acfun.cn", "weibo.com",
    "xiaohongshu.com", "tiktok.com",
)


def choose_profile(cfg: Dict[str, Any], url: str = "", p2p: bool = False) -> str:
    """决定这次下载用哪一档。

    显式配置优先（用户说了算）；auto 时按协议与站点判断。
    """
    fixed = str(cfg.get("aria2_profile") or "auto").strip().lower()
    if fixed in PROFILES:
        return fixed
    if p2p:
        return "p2p"
    try:
        from .. import sites as _sites
        if _sites.match(url):
            return "video"
    except Exception:  # noqa: BLE001
        pass
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _VIDEO_HOSTS):
        return "video"
    return "default"


def profile_options(cfg: Dict[str, Any], profile: str) -> Dict[str, str]:
    """把档位翻译成 aria2 的 per-download 选项。"""
    ck, sk, dc, ds = _PROFILE_KEYS.get(profile, _PROFILE_KEYS["default"])
    conn = int(cfg.get(ck, dc) or dc)
    conn = max(1, min(16, conn))
    return {
        "max-connection-per-server": str(conn),
        "split": str(conn),
        "min-split-size": str(cfg.get(sk, ds) or ds),
    }


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


class Aria2Error(Exception):
    pass


class Aria2Client:
    """最小可用的 aria2 JSON-RPC 客户端。"""

    def __init__(self, url: str, secret: str = "", timeout: int = 10):
        self.url = url
        self.secret = secret or ""
        self.timeout = timeout

    def call(self, method: str, params: Optional[List[Any]] = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": "jiliu",
            "method": method,
            "params": ([f"token:{self.secret}"] if self.secret else []) + list(params or []),
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise Aria2Error(f"无法连接 aria2 RPC {self.url}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise Aria2Error(f"aria2 RPC 调用失败: {exc}") from exc

        if "error" in body:
            err = body["error"]
            raise Aria2Error(f"{err.get('message')} (code {err.get('code')})")
        return body.get("result")

    # ---- 语义化封装 ----

    def version(self) -> Dict[str, Any]:
        return self.call("aria2.getVersion") or {}

    def add_uri(self, uris: List[str], options: Optional[Dict[str, Any]] = None) -> str:
        # 注意：aria2 的 header 参数必须是 JSON 数组，不能 str() 掉
        opts: Dict[str, Any] = {}
        for k, v in (options or {}).items():
            if v is None or v == "":
                continue
            opts[k] = v if isinstance(v, (list, dict)) else str(v)
        return self.call("aria2.addUri", [list(uris), opts])

    def add_torrent(self, torrent: bytes, options: Optional[Dict[str, Any]] = None,
                    uris: Optional[List[str]] = None) -> str:
        """aria2.addTorrent —— 种子的内容要以 base64 塞进 RPC 参数。

        注意参数顺序：addTorrent(torrent, uris, options)，
        第二个 webseed 列表不能省，省了会当成 options 解析而报错。
        """
        opts: Dict[str, Any] = {}
        for k, v in (options or {}).items():
            if v is None or v == "":
                continue
            opts[k] = v if isinstance(v, (list, dict)) else str(v)
        return self.call("aria2.addTorrent",
                         [base64.b64encode(torrent).decode("ascii"),
                          list(uris or []), opts])

    def add_metalink(self, metalink: bytes,
                     options: Optional[Dict[str, Any]] = None) -> List[str]:
        """aria2.addMetalink —— .meta4 / .metalink 文件。"""
        opts: Dict[str, Any] = {}
        for k, v in (options or {}).items():
            if v is None or v == "":
                continue
            opts[k] = v if isinstance(v, (list, dict)) else str(v)
        return self.call("aria2.addMetalink",
                         [base64.b64encode(metalink).decode("ascii"), opts]) or []

    def bt_status(self, gid: str) -> Dict[str, Any]:
        """BT 任务专属状态：连接数、做种数、info hash。

        tellStatus 里这些字段对 HTTP 任务是空的，所以要单独取，
        拿不到就返回空字典，不影响普通任务。
        """
        keys = ["gid", "status", "totalLength", "completedLength",
                "downloadSpeed", "uploadSpeed", "connections", "numSeeders",
                "seeder", "infoHash", "bittorrent", "files", "errorMessage"]
        try:
            st = self.tell_status(gid, keys)
        except Aria2Error:
            return {}
        bt = st.get("bittorrent") or {}
        return {
            "info_hash": st.get("infoHash") or "",
            "num_seeders": int(st.get("numSeeders") or 0),
            "connections": int(st.get("connections") or 0),
            "is_seeder": bool(st.get("seeder")),
            "peers": len(bt.get("announceList") or []),
            "announce": (bt.get("announceList") or [[]])[0][:8]
                        if bt.get("announceList") else [],
        }

    def change_option(self, gid: str, options: Dict[str, Any]) -> str:
        """aria2.changeOption —— 任务跑着也能改（比如临时限速）。"""
        opts = {k: (v if isinstance(v, (list, dict)) else str(v))
                for k, v in (options or {}).items() if v is not None}
        return self.call("aria2.changeOption", [gid, opts])

    def tell_status(self, gid: str, keys: Optional[List[str]] = None) -> Dict[str, Any]:
        params: List[Any] = [gid]
        if keys:
            params.append(keys)
        return self.call("aria2.tellStatus", params) or {}

    def pause(self, gid: str) -> str:
        return self.call("aria2.pause", [gid])

    def unpause(self, gid: str) -> str:
        return self.call("aria2.unpause", [gid])

    def remove(self, gid: str) -> str:
        return self.call("aria2.remove", [gid])

    def force_remove(self, gid: str) -> str:
        """立即移除（aria2.forceRemove）。

        remove 是优雅停（发完握手再退），BT 大任务可能拖几秒；
        用户点「取消」要的就是立刻停 —— forceRemove 不等连接收尾。
        """
        return self.call("aria2.forceRemove", [gid])

    def remove_result(self, gid: str) -> str:
        try:
            return self.call("aria2.removeDownloadResult", [gid])
        except Aria2Error:
            return ""

    def global_stat(self) -> Dict[str, Any]:
        return self.call("aria2.getGlobalStat") or {}

    def change_global_option(self, options: Dict[str, Any]) -> str:
        return self.call("aria2.changeGlobalOption", [ {k: str(v) for k, v in options.items()} ])


STATUS_KEYS = [
    "gid", "status", "totalLength", "completedLength", "downloadSpeed",
    "connections", "errorCode", "errorMessage", "dir", "files", "bittorrent",
    "uploadSpeed", "numSeeders", "seeder",
]

# aria2 状态 -> 汲流统一状态
STATE_MAP = {
    "active": "running",
    "waiting": "queued",
    "paused": "paused",
    "complete": "done",
    "error": "error",
    "removed": "cancelled",
}


class Aria2Service:
    """管理我们自己拉起的 aria2c 进程。"""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self.port = int(cfg.get("aria2_port", 6800))
        self.secret = cfg.get("aria2_secret", "") or "jiliu"
        self.url = f"http://127.0.0.1:{self.port}/jsonrpc"
        self.log_path = pathlib.Path(_config.HOME) / "logs" / "aria2.log"
        self._logfh = None

    @property
    def binary(self) -> Optional[str]:
        return tools.find("aria2c", self.cfg.get("aria2_path", ""))

    def build_args(self) -> List[str]:
        """拼出拉起 aria2c 的完整命令行。

        单独抽出来是为了能被测试直接验：
        aria2 遇到**不认的选项名**不会警告，而是打印 usage 直接退出
        （退出码 28），整个 RPC 起不来。所以「参数名拼错」这一类 bug
        必须由测试拦住 —— tests/test_aria2.py 会拿 `aria2c -h#all`
        的选项表逐个核对。
        """
        limit = int(self.cfg.get("speed_limit", 0) or 0)
        args = [
            self.binary or "aria2c",
            "--enable-rpc",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={self.port}",
            f"--rpc-secret={self.secret}",
            # RPC 只在本机监听，不给外网碰
            "--rpc-allow-origin-all=false",
            # 断点续传
            "--continue=true",
            # 分片：服务启动时按「通用档」给个稳妥值，具体任务会按
            # video / default / p2p 再覆盖（见 profile_options）
            f"--max-connection-per-server={int(self.cfg.get('aria2_default_conn', 8))}",
            f"--split={int(self.cfg.get('aria2_default_conn', 8))}",
            f"--min-split-size={self.cfg.get('aria2_default_min_split', '8M')}",
            # 写盘前先在内存里攒一攒，减少小 IO；SSD 上别做预分配
            f"--disk-cache={self.cfg.get('aria2_disk_cache', '24M')}",
            "--file-allocation=none",
            "--auto-save-interval=1",
            # 重试
            "--max-tries=5",
            "--retry-wait=2",
            "--auto-file-renaming=false",
            "--allow-overwrite=true",
            "--console-log-level=warn",
            "--summary-interval=0",
            "--daemon=false",
        ]

        # ---- BT / 磁力 相关 ----
        # 自研引擎不做 P2P，磁力、种子、ed2k 全靠 aria2 这一条路。
        # 默认参数在 Windows 上跑 BT 基本没源，得把 DHT / PEX / LPD 都打开。
        if self.cfg.get("bt_enabled", True):
            bt_dir = pathlib.Path(_config.HOME) / "bt"
            try:
                bt_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                bt_dir = pathlib.Path(_config.HOME)
            seed_time = int(self.cfg.get("bt_seed_time", 0) or 0)
            args += [
                # 找源的三件套，缺一个存活率就明显下降
                "--enable-dht=true",
                "--dht-listen-port=6881-6999",
                "--bt-enable-lpd=true",
                "--enable-peer-exchange=true",
                "--follow-torrent=true",
                # 磁力先拿到元数据才能开始下
                "--bt-save-metadata=true",
                # 复用上次存下来的元数据。⚠ 别写成 --bt-load-error-ignore ——
                # aria2 **没有**这个选项，遇到不认的参数它会直接打印 usage
                # 并以退出码 28 退出，RPC 根本起不来。
                "--bt-load-saved-metadata=true",
                f"--bt-max-peers={int(self.cfg.get('bt_max_peers', 64) or 64)}",
                f"--listen-port={int(self.cfg.get('bt_listen_port', 6881))}",
                # 不做种：下载完就停。默认 0 —— 用户要的是下文件，
                # 不是被默默变成种子服务器占掉上行。
                f"--seed-time={seed_time}",
                f"--seed-ratio={float(self.cfg.get('bt_seed_ratio', 0.0) or 0.0)}",
                "--bt-detach-seed-only=true",
                "--bt-tracker-connect-timeout=10",
                "--bt-tracker-timeout=10",
                f"--dht-file-path={bt_dir / 'dht.dat'}",
                f"--dht-file-path6={bt_dir / 'dht6.dat'}",
                "--enable-dht6=false",
            ]
            extra_tr = str(self.cfg.get("bt_trackers") or "").strip()
            if extra_tr:
                args.append(f"--bt-tracker={extra_tr}")
        else:
            args.append("--follow-torrent=false")

        if limit > 0:
            args.append(f"--max-overall-download-limit={limit}")
        if self.cfg.get("user_agent"):
            args.append(f"--user-agent={self.cfg['user_agent']}")
        if self.cfg.get("referer"):
            args.append(f"--referer={self.cfg['referer']}")
        if self.cfg.get("cookie_file"):
            args.append(f"--load-cookies={self.cfg['cookie_file']}")
        return args

    def refresh_speed_limit(self, cfg: Optional[Dict[str, Any]] = None) -> bool:
        """把配置里的 speed_limit 热推给运行中的 daemon（RPC 修改全局限速）。

        daemon 没起来时返回 False —— 不算错，下次 start() 会按新配置拉起。
        """
        if cfg is not None:
            self.cfg = cfg
        limit = int(self.cfg.get("speed_limit", 0) or 0)
        try:
            self.call("aria2.changeGlobalOption", [{
                "max-overall-download-limit": str(limit) if limit > 0 else "0"
            }])
            return True
        except Exception:
            return False

    def start(self) -> Aria2Client:
        if self.proc and self.proc.poll() is None:
            return Aria2Client(self.url, self.secret)
        if not self.binary:
            raise Aria2Error("找不到 aria2c 可执行文件")

        args = self.build_args()

        # 输出重定向到**文件**：
        #   · 不能丢进 DEVNULL —— aria2 遇到不认的参数会立刻打印 usage 退出，
        #     消息一扔，用户只看到一句「已启动但 RPC 未响应」，完全无从下手。
        #   · 也不能用 PIPE —— 没人读的管道写满就会把 aria2 阻塞住。
        # 踩过这个坑：BT 参数里写错一个选项名，整个 aria2 后端静默不可用，
        # 上层只会悄悄回退到自研引擎，看着像「aria2 没生效」。
        sink: Any = subprocess.DEVNULL
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._logfh = self.log_path.open("ab")
            sink = self._logfh
        except Exception:
            self._logfh = None

        self.proc = subprocess.Popen(
            args,
            stdout=sink,
            stderr=sink,
            stdin=subprocess.DEVNULL,
            creationflags=_no_window(),
        )
        # 登记：主程序（GUI）退出时统一清杀，aria2c 不留孤儿
        tools.track_proc(self.proc)   # 主程序退出时统一清杀，不留孤儿

        # 等 RPC 起来
        deadline = time.time() + 8
        client = Aria2Client(self.url, self.secret)
        while time.time() < deadline:
            if self.proc.poll() is not None:
                # 进程已经退了，别再傻等满 8 秒 —— 直接把它的遗言端上来
                raise Aria2Error(
                    f"aria2c 启动即退出（退出码 {self.proc.returncode}）。"
                    f"多半是某个命令行参数它不认。原始输出见 {self.log_path}"
                )
            try:
                client.version()
                return client
            except Aria2Error:
                time.sleep(0.2)
        raise Aria2Error(
            f"aria2c 在跑但 RPC 一直没响应（端口 {self.port}，等了 8 秒）。"
            f"可能是端口被占或被防火墙拦了。原始输出见 {self.log_path}"
        )

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                Aria2Client(self.url, self.secret).call("aria2.shutdown")
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.proc = None
        if self._logfh is not None:
            try:
                self._logfh.close()
            except Exception:
                pass
            self._logfh = None


class Aria2Downloader:
    """把 aria2 的下载包装成和 RangeDownloader 一样的接口。"""

    engine = "aria2"

    def __init__(
        self,
        url: str,
        dest: pathlib.Path,
        *,
        cfg: Dict[str, Any],
        client: Aria2Client,
        threads: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
        speed_limit: Optional[int] = None,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        verify_size: bool = True,
    ):
        self.url = url
        self.dest = pathlib.Path(dest)
        self.cfg = cfg
        self.client = client
        self.threads = int(threads or cfg.get("threads", 8))
        self.headers = headers or {}
        self.speed_limit = speed_limit
        self.progress_cb = progress_cb
        self.log_cb = log_cb
        self.verify_size = verify_size

        self.gid: Optional[str] = None
        self._last: Dict[str, Any] = {}
        self._paused = False
        self._cancelled = False
        self._error: Optional[str] = None
        self._started_at = 0.0
        self._finished_at = 0.0
        # ⚠ 必须在这里初始化：_bt 是在 run() 里按协议分流时才置 True 的，
        #   而 snapshot() 一开始就会读它。普通 HTTP 下载走不到那段赋值，
        #   不预置就是 AttributeError —— 而且是在进度回调里炸，
        #   表现为「下载失败但错误信息莫名其妙」。
        self._bt = False
        self._bt_info: Dict[str, Any] = {}

    # ---- 与原生引擎同名的方法，方便上层统一调度 ----

    def pause(self) -> None:
        self._paused = True
        if self.gid:
            try:
                self.client.pause(self.gid)
            except Aria2Error:
                pass

    def resume(self) -> None:
        self._paused = False
        if self.gid:
            try:
                self.client.unpause(self.gid)
            except Aria2Error:
                pass

    def cancel(self) -> None:
        self._cancelled = True
        if not self.gid:
            return

        # 这里不能静默吞错 —— RPC 失败还把任务标成已取消的话，
        # aria2 会在后台继续把这个下载跑完，用户以为停了其实没停。
        # 所以：forceRemove 立即移除，失败重试一次，再失败记进日志。
        last: Optional[Exception] = None
        for _ in range(2):
            try:
                self.client.force_remove(self.gid)
                return
            except Aria2Error as exc:
                last = exc
                time.sleep(0.2)
        self._log(f"⚠ 取消时 aria2 没停干净（{last}）。"
                  f"如果退出后 aria2c 仍在跑，请手动结束进程。")

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def set_speed_limit(self, bytes_per_sec: int) -> None:
        """运行中改本任务限速（aria2 支持热改 max-download-limit）。"""
        self.speed_limit = max(0, int(bytes_per_sec or 0))
        if not self.gid:
            return
        try:
            self.client.change_option(self.gid, {"max-download-limit": str(self.speed_limit)})
        except Exception as exc:  # noqa: BLE001
            self._log(f"改限速失败（不影响下载）：{exc}")

    def done_bytes(self) -> int:
        return int(self._last.get("completedLength") or 0)

    def snapshot(self) -> Dict[str, Any]:
        st = self._last
        total = int(st.get("totalLength") or 0)
        done = int(st.get("completedLength") or 0)
        speed = float(st.get("downloadSpeed") or 0)
        eta = (total - done) / speed if speed > 0 and total > done else None
        files = st.get("files") or []
        if self._bt:
            # 多文件 BT：产物是目录，报目录（用户要打开的是那个文件夹）。
            # 单文件 BT：files 只有一个 —— 报文件本身，否则 filename 会
            # 变成父目录名（如 "BT"），任务列表显示成目录名，还打不开文件。
            if len(files) == 1:
                path = str((files[0].get("path") or "") or self.dest)
            else:
                first = str((files[0].get("path") if files else "") or "")
                path = str(pathlib.Path(first).parent) if first else str(self.dest)
        else:
            path = (files[0].get("path") if files else "") or str(self.dest)
        return {
            "url": self.url,
            "dest": path,
            "filename": pathlib.Path(path).name if path else self.dest.name,
            "size": total,
            "size_text": human_size(total),
            "done": done,
            "done_text": human_size(done),
            "percent": round(done * 100.0 / total, 2) if total else 0.0,
            "speed": speed,
            "speed_text": human_size(speed) + "/s",
            "eta": eta,
            "eta_text": "-" if not eta else f"{int(eta)}s",
            "elapsed": (self._finished_at or time.monotonic()) - self._started_at if self._started_at else 0,
            "threads": int(st.get("connections") or 0),
            "backend": "aria2",
            "gid": self.gid,
            "aria2_status": st.get("status", ""),
            "segments_created": int(st.get("connections") or 0),
            "paused": self._paused,
            "cancelled": self._cancelled,
            "error": self._error or (st.get("errorMessage") or None),
            # P2P 任务的专属信息：做种数、peer 连接、info hash。
            # 界面拿它显示「3 个做种 / 12 个连接」—— BT 下载里
            # 这两个数比速度更能说明「为什么慢」。
            "p2p": self._bt,
            "bt": (self._bt_status_safe()),
        }

    def _bt_status_safe(self) -> Dict[str, Any]:
        """拿 BT 状态，失败不抛 —— 进度回调里抛异常会打断整个下载。"""
        if not self._bt or not self.gid:
            return {}
        try:
            self._bt_info = self.client.bt_status(self.gid)
        except Exception:
            pass
        return self._bt_info

    def _fetch_torrent(self) -> bytes:
        """把 .torrent 文件本身取下来。

        种子得先落到内存再交给 aria2 的 addTorrent ——
        aria2 接受的是种子**内容**，不是 URL。

        来源有两种：HTTP(S) 直链（浏览器里点的种子下载链接），
        和本地文件（「打开种子文件」功能：server 把上传的种子落盘到
        HOME/torrents/ 后用 file:// URL 建任务）。
        """
        if self.url.lower().startswith("file://"):
            from .. import protocols as _p2
            local = urllib.parse.urlparse(self.url).path
            # Windows 的 file:///D:/x.torrent path 是 /D:/x.torrent，要去掉开头的 /
            if re.match(r"^/[A-Za-z]:", local):
                local = local[1:]
            local = urllib.parse.unquote(local)
            try:
                return pathlib.Path(local).read_bytes()
            except Exception as exc:  # noqa: BLE001
                self._log(f"读本地种子失败：{type(exc).__name__}: {exc}")
                return b""
        if "://" not in self.url:
            # 裸路径（内部调用直接给路径的场景）
            try:
                return pathlib.Path(self.url).read_bytes()
            except Exception as exc:  # noqa: BLE001
                self._log(f"读本地种子失败：{type(exc).__name__}: {exc}")
                return b""
        from .. import proxy as _proxy
        try:
            op = _proxy.opener(self.cfg, self.url, timeout=30)
            hdrs = dict(self.headers or {})
            hdrs.setdefault("User-Agent",
                            self.cfg.get("user_agent") or "Jiliu/1.0")
            req = urllib.request.Request(self.url, headers=hdrs)
            with op.open(req, timeout=30) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            self._log(f"取种子文件失败：{type(exc).__name__}: {exc}")
            return b""

    def _emit(self) -> None:
        if self.progress_cb:
            try:
                self.progress_cb(self.snapshot())
            except Exception:
                pass

    def _log(self, msg: str) -> None:
        if self.log_cb:
            try:
                self.log_cb(msg)
            except Exception:
                pass

    def run(self) -> Dict[str, Any]:
        self._started_at = time.monotonic()
        try:
            # ---- 按协议分流 ----
            # 自研引擎不做 P2P，所以磁力 / 种子 / ed2k 只有 aria2 这一条路。
            # 协议判断交给 protocols 模块，别在这儿重复写正则。
            from .. import protocols as _proto
            pinfo = _proto.detect(self.url)

            profile = choose_profile(self.cfg, self.url, p2p=bool(pinfo.p2p))
            popts = profile_options(self.cfg, profile)
            # 调用方显式给了更小的线程数就听调用方的 —— 但不会超过档位上限
            try:
                if self.threads and self.threads < int(popts["split"]):
                    popts["split"] = str(max(1, self.threads))
                    popts["max-connection-per-server"] = str(
                        min(max(1, self.threads), int(popts["max-connection-per-server"])))
            except (TypeError, ValueError):
                pass

            options: Dict[str, Any] = {
                "dir": str(self.dest.parent),
                "out": self.dest.name,
                "continue": "true",
                **popts,
            }
            self._log(f"aria2 档位 {profile}：{popts['max-connection-per-server']} 连接"
                      f" / 最小分片 {popts['min-split-size']}")
            if self.headers:
                options["header"] = [
                    f"{k}: {v}" for k, v in self.headers.items()
                ]
            if self.headers.get("Referer"):
                options["referer"] = self.headers["Referer"]
            if self.speed_limit:
                options["max-download-limit"] = self.speed_limit

            if pinfo.engine == "aria2" and pinfo.p2p:
                # BT 是多文件，`out` 会强行套一个统一文件名，必须去掉；
                # continue 也不适用（分片由 BT 协议自己管）
                bt_opts = dict(options)
                bt_opts.pop("out", None)
                bt_opts.pop("continue", None)
                bt_opts["seed-time"] = str(int(self.cfg.get("bt_seed_time", 0) or 0))
                bt_opts["bt-save-metadata"] = "true"

                if pinfo.kind == "torrent":
                    blob = self._fetch_torrent()
                    if not blob:
                        raise DownloadError(f"种子文件下载失败：{self.url}")
                    self.gid = self.client.add_torrent(blob, bt_opts)
                    self._log(f"种子已交给 aria2（BT），gid={self.gid}")
                else:
                    if pinfo.trackers:
                        bt_opts["bt-tracker"] = ",".join(pinfo.trackers)
                    self.gid = self.client.add_uri([pinfo.url], bt_opts)
                    self._log(f"{pinfo.label}已交给 aria2，gid={self.gid}"
                              + (f"，tracker {len(pinfo.trackers)} 个"
                                 if pinfo.trackers else "（磁力没带 tracker，"
                                                        "靠 DHT 找源会慢一些）"))
                self._bt = True
                # BT 的总大小要等元数据到手才知道，这里先不校验
                self.verify_size = False
            else:
                self.gid = self.client.add_uri([self.url], options)
                self._log(f"已交给 aria2，gid={self.gid}")

            while True:
                time.sleep(0.4)
                st = self.client.tell_status(self.gid, STATUS_KEYS)
                self._last = st
                self._emit()
                status = st.get("status")

                if status == "complete":
                    self._finished_at = time.monotonic()
                    done = int(st.get("completedLength") or 0)
                    total = int(st.get("totalLength") or 0)
                    if self.verify_size and total and done != total:
                        self._error = f"文件大小不符：期望 {total}，实际 {done}"
                        return {"ok": False, "path": self.snapshot()["dest"], "bytes": done,
                                "seconds": self._finished_at - self._started_at,
                                "cancelled": False, "error": self._error, "resumed_bytes": 0}
                    self.client.remove_result(self.gid)
                    return {"ok": True, "path": self.snapshot()["dest"], "bytes": done,
                            "seconds": self._finished_at - self._started_at,
                            "cancelled": False, "error": None, "resumed_bytes": 0}

                if status in ("error", "removed"):
                    self._finished_at = time.monotonic()
                    code = st.get("errorCode")
                    msg = st.get("errorMessage") or f"aria2 状态 {status}"
                    self._error = f"{msg} (code {code})"
                    self.client.remove_result(self.gid)
                    return {"ok": False, "path": str(self.dest), "bytes": self.done_bytes(),
                            "seconds": self._finished_at - self._started_at,
                            "cancelled": status == "removed", "error": self._error,
                            "resumed_bytes": 0}

                if self._cancelled:
                    self._finished_at = time.monotonic()
                    return {"ok": False, "path": str(self.dest), "bytes": self.done_bytes(),
                            "seconds": self._finished_at - self._started_at,
                            "cancelled": True, "error": None, "resumed_bytes": 0}

        except Aria2Error as exc:
            self._finished_at = time.monotonic()
            self._error = str(exc)
            return {"ok": False, "path": str(self.dest), "bytes": self.done_bytes(),
                    "seconds": self._finished_at - self._started_at,
                    "cancelled": False, "error": self._error, "resumed_bytes": 0}
        except Exception as exc:  # noqa: BLE001
            self._finished_at = time.monotonic()
            self._error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "path": str(self.dest), "bytes": self.done_bytes(),
                    "seconds": self._finished_at - self._started_at,
                    "cancelled": False, "error": self._error, "resumed_bytes": 0}


def detect(cfg: Dict[str, Any], *, refresh: bool = False) -> Dict[str, Any]:
    """探测可用的 aria2 接入方式。结果带 TTL 缓存，理由见文件头注释。"""
    key = (cfg.get("aria2_path", ""), cfg.get("aria2_rpc_url", ""),
           cfg.get("aria2_rpc_secret", ""), cfg.get("motrix_rpc_secret", ""))
    now = time.time()
    if (not refresh and _DETECT_CACHE["val"] is not None
            and _DETECT_CACHE["key"] == key
            and now - _DETECT_CACHE["t"] < DETECT_TTL):
        return dict(_DETECT_CACHE["val"])

    out = {"binary": "", "binary_version": "", "rpc_ok": False, "rpc_url": "", "motrix": False}

    binary = tools.find("aria2c", cfg.get("aria2_path", ""))
    if binary:
        out["binary"] = binary
        out["binary_version"] = tools.version_of(binary)

    # 先看配置的 RPC
    url = cfg.get("aria2_rpc_url") or ""
    secret = cfg.get("aria2_rpc_secret") or ""
    for candidate, sec, tag in [
        (url, secret, "configured"),
        (MOTRIX_RPC, cfg.get("motrix_rpc_secret", "") or "", "motrix"),
    ]:
        if not candidate:
            continue
        client = Aria2Client(candidate, sec, timeout=1.5)
        try:
            v = client.version()
            out["rpc_ok"] = True
            out["rpc_url"] = candidate
            out["rpc_version"] = v.get("version", "")
            out["rpc_tag"] = tag
            if tag == "motrix":
                out["motrix"] = True
            break
        except Exception:
            continue

    _DETECT_CACHE["t"] = time.time()
    _DETECT_CACHE["key"] = key
    _DETECT_CACHE["val"] = dict(out)
    return out


def invalidate_detect_cache() -> None:
    """作废探测缓存。用户刚装了 aria2c / 刚启动 Motrix 之后调它。"""
    _DETECT_CACHE["t"] = 0.0
    _DETECT_CACHE["key"] = None
    _DETECT_CACHE["val"] = None
