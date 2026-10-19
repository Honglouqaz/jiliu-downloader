"""外部工具探测与调用。

汲流本身零第三方依赖，但有三件事交给成熟的开源工具做更靠谱：
    yt-dlp   视频解析（1800+ 站点，国内外主流平台都覆盖）
    ffmpeg   音视频合并 / 转码
    aria2c   备用下载引擎（Motrix 底层用它）

yt-dlp 有三种存在形式，探测顺序：
    1. 配置里显式指定的路径
    2. 项目自带的 bin/yt-dlp.exe（单文件可执行版）
    3. PATH / 已知安装位置（scoop 等包管理器装的）
    4. vendor/ 里内置的 yt_dlp Python 包（`python -m yt_dlp` 跑）
"""
from __future__ import annotations

import copy
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 目录布局
# --------------------------------------------------------------------------
#
# 三种运行形态，找 bin/ 与 vendor/ 的位置不一样：
#
#   源码运行     <项目>/bin、<项目>/vendor
#   PyInstaller  <解包临时目录>/bin、<解包临时目录>/vendor（随 exe 一起打进去的）
#   便携安装     还允许把 bin/ vendor/ 放在 exe 旁边 —— 用户想换个新版
#                yt-dlp.exe 直接扔进去就行，不必重新打包

def _resource_root() -> pathlib.Path:
    """打包进去的只读资源在哪。"""
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        return pathlib.Path(meipass)
    return pathlib.Path(__file__).resolve().parent.parent


def _exe_dir() -> pathlib.Path:
    """可执行文件旁边的目录（源码运行时就是项目根）。"""
    if IS_FROZEN:
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent.parent


IS_FROZEN = bool(getattr(sys, "frozen", False))

PKG_DIR = (pathlib.Path(getattr(sys, "_MEIPASS")) if getattr(sys, "_MEIPASS", "")
           else pathlib.Path(__file__).resolve().parent)
ROOT_DIR = _resource_root()

# 项目自带的 bin 目录（放单文件可执行工具）
BIN_DIR = ROOT_DIR / "bin"
# 项目自带的 Python 包目录（放 pip --target 解出来的纯 Python 工具，如 yt_dlp）
VENDOR_DIR = ROOT_DIR / "vendor"

# 额外的可搜索位置：exe 旁边（便携安装时用户可以自己丢工具进来）。
# 源码运行时不加，免得把项目里的 tools/ 误当成工具目录。
def _extra_dirs(name: str, skip: pathlib.Path) -> List[pathlib.Path]:
    if not IS_FROZEN:
        return []
    d = _exe_dir() / name
    return [d] if d != skip else []


EXTRA_BIN_DIRS = _extra_dirs("bin", BIN_DIR) + _extra_dirs("tools", BIN_DIR)
EXTRA_VENDOR_DIRS = _extra_dirs("vendor", VENDOR_DIR)

# 已知的常见安装位置（装过就有，省得用户配 PATH）。
# 用 %VAR% 模板而不是写死路径 —— 写死用户名的话，这份程序换台机器就全失效了。
_KNOWN_TEMPLATES: Dict[str, List[str]] = {
    "yt-dlp": [
        r"%USERPROFILE%\scoop\shims\yt-dlp.exe",
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\yt-dlp.exe",
        r"%ProgramFiles%\yt-dlp\yt-dlp.exe",
    ],
    "ffmpeg": [
        r"%USERPROFILE%\scoop\shims\ffmpeg.exe",
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe",
        r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe",
    ],
    "ffprobe": [
        r"%USERPROFILE%\scoop\shims\ffprobe.exe",
        r"%ProgramFiles%\ffmpeg\bin\ffprobe.exe",
    ],
    "aria2c": [
        r"%USERPROFILE%\scoop\shims\aria2c.exe",
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\aria2c.exe",
        r"%ProgramFiles%\aria2\aria2c.exe",
        r"%ProgramFiles%\Motrix\resources\extra\win\aria2\aria2c.exe",
    ],
    # curl 从 Windows 10 1803 起就系统自带，所以 PATH 命中率极高。
    # 这里列的位置是「PATH 里没有」时的兜底。
    "curl": [
        r"%SystemRoot%\System32\curl.exe",
        r"%USERPROFILE%\scoop\shims\curl.exe",
    ],
    "wget": [
        r"%USERPROFILE%\scoop\shims\wget.exe",
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\wget.exe",
        r"%ProgramFiles%\GnuWin32\bin\wget.exe",
    ],
    # 专业 HLS/DASH 下载器。没有它也能用（ffmpeg 兜底），
    # 但碰上分片加密 / 多音轨的流，它比 ffmpeg 稳。
    "N_m3u8DL-RE": [
        r"%USERPROFILE%\scoop\shims\N_m3u8DL-RE.exe",
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\N_m3u8DL-RE.exe",
    ],
    "lux": [
        r"%USERPROFILE%\scoop\shims\lux.exe",
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\lux.exe",
    ],
}


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


KNOWN: Dict[str, List[str]] = {
    k: [_expand(t) for t in v] for k, v in _KNOWN_TEMPLATES.items()
}

EXE_SUFFIX = ".exe" if os.name == "nt" else ""


def find(name: str, override: str = "") -> Optional[str]:
    """按优先级找一个可执行文件：显式指定 > 项目 bin > exe 旁的 bin > PATH > 已知位置。"""
    if override:
        p = pathlib.Path(override).expanduser()
        if p.is_file():
            return str(p)

    for d in [BIN_DIR] + EXTRA_BIN_DIRS:
        local = d / (name + EXE_SUFFIX)
        if local.is_file():
            return str(local)

    hit = shutil.which(name)
    if hit:
        return hit

    for cand in KNOWN.get(name, []):
        if os.path.isfile(cand):
            return cand
    return None


def version_of(path: str, args: Optional[List[str]] = None) -> str:
    """取版本号，拿不到就返回空串。"""
    if not path:
        return ""
    cmd = [path] + (args or ["--version"])
    try:
        out = subprocess.run(
            cmd, capture_output=True, timeout=15,
            creationflags=_no_window(),
        )
        text = (out.stdout or out.stderr or b"").decode("utf-8", "replace")
        return text.strip().splitlines()[0] if text.strip() else ""
    except Exception:
        return ""


def _no_window() -> int:
    """Windows 上不要弹出控制台窗口。"""
    return 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


# ---- 运行中的 worker 子进程登记 -------------------------------------------
# yt-dlp worker（frozen 下是再生的 jiliu.exe --run-ytdlp）和 aria2c 都是
# 独立进程：主程序退出时它们不会自动跟着死。GUI 关窗收尾时统一终止，
# 避免「面板关了还有进程在后台偷偷下载」的孤儿。

_LIVE_PROCS: set = set()
_LIVE_LOCK = threading.Lock()


def track_proc(proc) -> None:
    """登记一个 worker 子进程（GUI 退出时会被统一终止）。"""
    with _LIVE_LOCK:
        _LIVE_PROCS.add(proc)


def untrack_proc(proc) -> None:
    with _LIVE_LOCK:
        _LIVE_PROCS.discard(proc)


def kill_tracked() -> int:
    """终止所有还活着的 worker。返回终止数量。已退出的直接忽略。"""
    with _LIVE_LOCK:
        procs = list(_LIVE_PROCS)
        _LIVE_PROCS.clear()
    n = 0
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
                n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


# 探测结果缓存。探测要 spawn 子进程，一次 5 秒上下，而面板是轮询的，
# 所以必须缓存。TTL 取 60 秒：够短，用户新装的工具一分钟内就能认出来；
# 够长，界面轮询不会反复烧子进程。
PROBE_TTL = 60.0
_PROBE_CACHE: Dict[str, Any] = {"t": 0.0, "key": None, "val": None}


def probe_all(cfg: Optional[Dict] = None, *, refresh: bool = False
              ) -> Dict[str, Dict[str, str]]:
    """探测外部工具，返回可用性和版本。

    结果带 TTL 缓存 —— 一次完整探测要 spawn 四五个子进程（yt-dlp 尤其慢，
    实测 5 秒上下），而 /api/state 是面板**轮询**的接口。没有缓存的话，
    每轮一次刷新都会重新拉一遍版本号，界面看着像卡死，CPU 也白烧。

    要强制重新探测（比如用户刚把 yt-dlp.exe 丢进 bin/），传 refresh=True。
    """
    cfg = cfg or {}
    key = (cfg.get("ytdlp_path", ""), cfg.get("ffmpeg_path", ""),
           cfg.get("aria2_path", ""), cfg.get("curl_path", ""),
           cfg.get("wget_path", ""))
    now = time.time()
    if (not refresh and _PROBE_CACHE["val"] is not None
            and _PROBE_CACHE["key"] == key
            and now - _PROBE_CACHE["t"] < PROBE_TTL):
        return copy.deepcopy(_PROBE_CACHE["val"])

    out: Dict[str, Dict[str, str]] = {}

    # yt-dlp 特殊：可能是独立 exe，也可能是 python -m yt_dlp
    yi = ytdlp_info(cfg.get("ytdlp_path", ""))
    out["yt-dlp"] = {
        "name": "yt-dlp",
        "path": yi["path"],
        "available": "yes" if yi["available"] else "no",
        "version": yi["version"],
        "source": yi.get("source") or ytdlp_source(yi),
        "mode": yi["mode"],
        "candidates": "; ".join(
            f'{c["source"]} {c["version"]}' for c in yi.get("candidates", [])
        ),
    }

    spec = [
        ("ffmpeg", cfg.get("ffmpeg_path", ""), ["-version"]),
        ("ffprobe", "", ["-version"]),
        ("aria2c", cfg.get("aria2_path", ""), ["--version"]),
        # 兜底下载引擎：curl 基本系统自带，wget 装了就有
        ("curl", cfg.get("curl_path", ""), ["--version"]),
        ("wget", cfg.get("wget_path", ""), ["--version"]),
        # 流下载/合流的专用工具，有就更好
        ("N_m3u8DL-RE", "", ["--version"]),
        ("lux", "", ["--version"]),
    ]
    for name, override, vargs in spec:
        p = find(name, override)
        out[name] = {
            "name": name,
            "path": p or "",
            "available": "yes" if p else "no",
            "version": version_of(p, vargs) if p else "",
            "source": _source_of(name, p),
            "mode": "binary" if p else "-",
        }

    _PROBE_CACHE["t"] = now
    _PROBE_CACHE["key"] = key
    _PROBE_CACHE["val"] = copy.deepcopy(out)
    return out


def probe_cached_age() -> float:
    """缓存是多久以前填的（秒）。没填过返回 -1。"""
    if _PROBE_CACHE["val"] is None:
        return -1.0
    return time.time() - float(_PROBE_CACHE["t"])


def invalidate_probe_cache() -> None:
    """手动作废探测缓存。装了新工具、改了路径之后调它。

    这里连 yt-dlp 的「选了哪一份」缓存一起清 —— 两个缓存是连着的，
    只清一个会出现「工具列表说新版可用，视频模块还在用旧版」的矛盾状态。
    """
    _PROBE_CACHE["t"] = 0.0
    _PROBE_CACHE["key"] = None
    _PROBE_CACHE["val"] = None
    _SEL_CACHE["t"] = 0.0
    _SEL_CACHE["val"] = None
    _CMD_CACHE.clear()


def _source_of(name: str, path: Optional[str]) -> str:
    if not path:
        return "-"
    try:
        if BIN_DIR in pathlib.Path(path).parents:
            return "项目 bin/"
    except Exception:
        pass
    if shutil.which(name) == path:
        return "PATH"
    return "已知位置"


def ytdlp_source(yi: Dict[str, Any]) -> str:
    """yt-dlp 的来源描述，方便一眼看清用的是哪一份。"""
    if not yi.get("available"):
        return "-"
    if yi.get("mode") == "binary":
        p = yi.get("path") or ""
        try:
            if BIN_DIR in pathlib.Path(p).parents:
                return "项目 bin/"
        except Exception:
            pass
        return "PATH" if shutil.which("yt-dlp") == p else "已知位置"
    return "vendor/ 内置包" if (VENDOR_DIR / "yt_dlp").is_dir() else "已装 Python 包"


def which_ffmpeg_dir() -> str:
    """给 yt-dlp 用的 ffmpeg 所在目录。"""
    p = find("ffmpeg")
    return str(pathlib.Path(p).parent) if p else ""


# --------------------------------------------------------------------------
# yt-dlp：三种存在形式，按「版本新旧」择优
# --------------------------------------------------------------------------
#
# 为什么不是简单的 exe 优先：
#   yt-dlp 是个跟平台改版赛跑的工具 —— B 站改一次播放页、YouTube 换一次
#   签名算法，旧版本就可能集体解析失败。所以这里不看形式，看版本号：
#   谁新用谁。老 exe 和新 vendor 包同时存在时，选新的那个。

def ytdlp_module_available() -> bool:
    """是否有可用的 yt_dlp 模块 —— 随包内置、已装到解释器、或 exe 旁的 vendor/。"""
    for d in [VENDOR_DIR] + EXTRA_VENDOR_DIRS:
        if (d / "yt_dlp" / "__init__.py").is_file():
            return True
    try:
        import importlib.util
        return importlib.util.find_spec("yt_dlp") is not None
    except Exception:
        return False


def tool_env() -> Dict[str, str]:
    """跑外部工具时的环境变量。

    关键点：把所有 vendor/ 挂进 PYTHONPATH，这样子进程 `python -m yt_dlp`
    无论用哪个解释器都能 import 到它。
    """
    env = dict(os.environ)
    dirs = [str(d) for d in [VENDOR_DIR] + EXTRA_VENDOR_DIRS if (d / "yt_dlp").is_dir()]
    if dirs:
        old = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(dirs + ([old] if old else []))
    return env


def _cmd_env(cmd: List[str]) -> Optional[Dict[str, str]]:
    """模块形式的命令才需要注入 PYTHONPATH。"""
    return tool_env() if "yt_dlp" in cmd else None


def ytdlp_version_of_cmd(cmd: List[str]) -> str:
    try:
        out = subprocess.run(cmd + ["--version"], capture_output=True, timeout=25,
                             env=_cmd_env(cmd), creationflags=_no_window())
        lines = (out.stdout or out.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return lines[0].strip() if lines else ""
    except Exception:
        return ""


def _frozen_ytdlp_cmd() -> Optional[List[str]]:
    """冻结运行时，exe 自己就是 yt-dlp 宿主。

    走 `jiliu.exe --run-ytdlp ...`，由 jiliu.py 里的分发转到内置 yt_dlp 模块。
    这样单文件便携版不依赖用户装 Python，也不依赖外部 yt-dlp.exe。
    """
    if not IS_FROZEN:
        return None
    try:
        import importlib.util
        has = importlib.util.find_spec("yt_dlp") is not None
    except Exception:
        has = False
    if not has:
        return None
    return [sys.executable, "--run-ytdlp"]


def version_key(v: str) -> tuple:
    """把 '2026.08.19' 这类版本号变成可比较的元组；解析不了就给最小。"""
    parts = []
    for chunk in str(v or "").replace("-", ".").split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts) if parts else (0,)


def ytdlp_candidates(override: str = "") -> List[Dict[str, Any]]:
    """列出所有能找到的 yt-dlp（同一文件的不同写法会去重）。"""
    out: List[Dict[str, Any]] = []
    seen = set()

    def key_of(cmd: List[str]) -> str:
        # 按真实路径 + 规范化大小写去重：PATH 给的 yt-dlp.EXE 和已知位置的
        # yt-dlp.exe 是同一个文件，不该算两份。
        head = cmd[0]
        try:
            head = os.path.normcase(os.path.realpath(head))
        except Exception:
            head = os.path.normcase(head)
        return head + "\x00" + "\x00".join(cmd[1:])

    def add(kind: str, cmd: List[str], label: str) -> None:
        k = key_of(cmd)
        if k in seen:
            return
        seen.add(k)
        out.append({"mode": kind, "command": cmd, "source": label})

    if override:
        p = pathlib.Path(override).expanduser()
        if p.is_file():
            add("binary", [str(p)], "配置指定")

    local = BIN_DIR / ("yt-dlp" + EXE_SUFFIX)
    if local.is_file():
        add("binary", [str(local)], "程序内置")

    for d in EXTRA_BIN_DIRS:
        p = d / ("yt-dlp" + EXE_SUFFIX)
        if p.is_file():
            add("binary", [str(p)], "exe 旁 bin/")

    hit = shutil.which("yt-dlp")
    if hit:
        add("binary", [hit], "PATH")

    for cand in KNOWN.get("yt-dlp", []):
        if os.path.isfile(cand):
            add("binary", [cand], "已知位置")

    frozen = _frozen_ytdlp_cmd()
    if frozen:
        add("frozen", frozen, "内置 yt_dlp 模块")

    if ytdlp_module_available():
        for d in [VENDOR_DIR] + EXTRA_VENDOR_DIRS:
            if (d / "yt_dlp" / "__init__.py").is_file():
                add("module", [sys.executable, "-m", "yt_dlp"], f"{d.name}/ 内置包")
                break
    return out


# 缓存选中的命令（存 list，不要存字符串 —— list("abc") 会变成 ['a','b','c']）
_CMD_CACHE: Dict[str, Optional[List[str]]] = {}


def ytdlp_command(override: str = "", *, refresh: bool = False) -> Optional[List[str]]:
    """挑一个 yt-dlp 来用：显式指定 > 版本最新的那个。

    结果缓存，避免每调一次都 spawn 好几个进程去问版本。
    """
    if override:
        return [override]

    if not refresh and "cmd" in _CMD_CACHE:
        cached = _CMD_CACHE["cmd"]
        return list(cached) if cached else None

    cands = ytdlp_candidates()
    best: Optional[Dict[str, Any]] = None
    best_key: tuple = ()
    for c in cands:
        v = ytdlp_version_of_cmd(c["command"])
        if not v:
            continue
        k = version_key(v)
        if best is None or k > best_key:
            best, best_key = c, k

    _CMD_CACHE["cmd"] = list(best["command"]) if best else None
    return list(best["command"]) if best else None


_SEL_CACHE: Dict[str, Any] = {"t": 0.0, "val": None}


def ytdlp_selected(*, refresh: bool = False) -> Dict[str, Any]:
    """当前选中了哪一份 yt-dlp（含落选者清单，方便排查）。

    结果带 TTL 缓存。判断「该选哪一份」得把每个候选都跑一遍 --version，
    实测 2.6 秒；而它是 /api/state 间接依赖的（面板轮询这个接口）。
    不缓存的话，界面每刷新一次就重新起一堆子进程。
    """
    now = time.time()
    if (not refresh and _SEL_CACHE["val"] is not None
            and now - _SEL_CACHE["t"] < PROBE_TTL):
        return copy.deepcopy(_SEL_CACHE["val"])

    cmd = ytdlp_command()
    if not cmd:
        result: Dict[str, Any] = {
            "available": False, "mode": "-", "path": "", "version": "",
            "command": [], "source": "-", "candidates": []}
        _SEL_CACHE["t"] = time.time()
        _SEL_CACHE["val"] = copy.deepcopy(result)
        return result

    chosen = None
    listing = []
    for c in ytdlp_candidates():
        v = ytdlp_version_of_cmd(c["command"])
        c = dict(c, version=v)
        listing.append({
            "mode": c["mode"], "source": c["source"], "version": v,
            "path": c["command"][0] if c["mode"] == "binary" else " ".join(c["command"]),
        })
        if c["command"] == cmd:
            chosen = c

    if chosen is None:  # override 情形
        ver = ytdlp_version_of_cmd(cmd)
        chosen = {"mode": "module" if "yt_dlp" in cmd else "binary",
                  "source": "配置指定", "version": ver, "command": cmd}

    result = {
        "available": bool(chosen.get("version")),
        "mode": chosen["mode"],
        "path": cmd[0] if chosen["mode"] == "binary" else " ".join(cmd),
        "version": chosen.get("version", ""),
        "command": cmd,
        "source": chosen.get("source", "-"),
        "candidates": listing,
    }
    _SEL_CACHE["t"] = time.time()
    _SEL_CACHE["val"] = copy.deepcopy(result)
    return result


def ytdlp_info(override: str = "") -> Dict[str, Any]:
    """yt-dlp 的可用性与版本（兼容旧接口）。"""
    if override:
        cmd = [override]
        ver = ytdlp_version_of_cmd(cmd)
        return {
            "available": bool(ver), "mode": "binary", "path": override,
            "version": ver, "command": cmd, "env": {},
            "source": "配置指定", "candidates": [],
        }
    sel = ytdlp_selected()
    sel["env"] = _cmd_env(sel["command"]) or {}
    return sel


def ytdlp_run(args: List[str], override: str = "", timeout: int = 120,
              cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    """统一入口：以正确的环境跑一次 yt-dlp。"""
    cmd = ytdlp_command(override)
    if not cmd:
        raise RuntimeError("未找到 yt-dlp：装一个 exe 放进 bin/，或把 yt_dlp 包放进 vendor/")
    return subprocess.run(
        cmd + args,
        capture_output=True,
        timeout=timeout,
        cwd=cwd,
        env=tool_env(),
        creationflags=_no_window(),
    )
