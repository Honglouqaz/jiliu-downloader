"""自动更新：检查新版本 → 下载 → 校验 → 退出时替换。

为什么要「退出时替换」
    onefile 打包的 exe 在运行期间被 Windows 锁住，自己覆盖自己一定失败
    （WinError 32）。所以流程是：先把新文件放到 update/ 下，写好一个
    延迟脚本，程序正常退出后由脚本接管 —— 等进程真的没了，再 move。

为什么不用 /S 静默安装器
    安装版走的是「下载 setup → 等退出 → setup --quiet --target <当前目录>
    → 启动」，安装器自己知道怎么覆盖旧文件、保配置、重建快捷方式，
    比我们自己 move 更可靠。

安全上的两条硬规矩
    1. 校验不过绝不替换（宁可让用户手动下，也不能装进一个坏二进制）
    2. 只认 GitHub Releases 域名的下载地址（防止被改配置指向别处）
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__, config as _config, proxy

# 官方仓库。cfg["update_repo"] 可以覆盖（fork 之后自更新用）
DEFAULT_REPO = "Honglouqaz/jiliu-downloader"
# 允许从哪些 host 下载资产
_ALLOWED_HOSTS = ("github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com",
                 "github-releases.githubusercontent.com")
# 检查结果的缓存时间（秒）——设置页反复刷新不该每次都打 GitHub
CHECK_TTL = 600.0
# 启动后多久做第一次静默检查（秒）
STARTUP_DELAY = 30.0
# 后台检查间隔（秒）
WATCH_INTERVAL = 6 * 3600.0

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"at": 0.0, "data": None}
_watcher: Optional[threading.Thread] = None
_watcher_stop = threading.Event()


# ----------------------------------------------------------------------
# 版本号
# ----------------------------------------------------------------------

_VER_RE = re.compile(
    r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?"
    r"(?:[-_.]?(alpha|beta|rc|pre)\.?(\d*))?$"
)


def parse_version(text: str) -> Tuple[int, ...]:
    """把 "v1.7.1" / "1.7.1-beta.2" 变成可比较的元组。

    预发布版排在正式版之前：1.8.0-beta.2 < 1.8.0。
    认不出来的版本号当成 0 —— 宁可判成「没有新版」，也不要因为一个
    奇怪的 tag 就去提示用户更新。
    """
    m = _VER_RE.match(str(text or "").strip())
    if not m:
        return (0, 0, 0, 0, 0, 0)
    parts = [int(x or 0) for x in m.group(1, 2, 3, 4)]
    tag = (m.group(5) or "").lower()
    # 正式版 = 1，预发布 = 0；再带一个序号
    stage = 1 if not tag else 0
    num = int(m.group(6) or 0) if tag else 0
    return tuple(parts) + (stage, num)


def is_newer(latest: str, current: str = "") -> bool:
    return parse_version(latest) > parse_version(current or __version__)


# ----------------------------------------------------------------------
# 安装形态
# ----------------------------------------------------------------------

def app_dir() -> pathlib.Path:
    """程序所在目录（源码运行 = 项目根）。"""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent.parent


def install_kind() -> str:
    """source | portable | installed

    安装版和便携版的区别只在「怎么替换」：安装版交给 setup，
    便携版直接 move 单文件。
    """
    if not getattr(sys, "frozen", False):
        return "source"
    d = app_dir()
    # 安装器装出来的目录会有这些痕迹
    if (d / "install.json").is_file() or (d / "uninstall.exe").is_file():
        return "installed"
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Jiliu",
        ):
            return "installed"
    except Exception:
        pass
    return "portable"


def can_self_update() -> Tuple[bool, str]:
    kind = install_kind()
    if kind == "source":
        return False, "源码运行模式不自动替换（请 git pull 或用发行包）"
    d = app_dir()
    try:
        probe = d / ".jiliu-write-test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except Exception:
        return False, f"程序目录不可写：{d}"
    if kind == "installed":
        # 安装版要靠 setup 覆盖；找不到就退化成「只提示」
        return True, ""
    return True, ""


# ----------------------------------------------------------------------
# 检查
# ----------------------------------------------------------------------

def _api_url(cfg: Dict[str, Any]) -> str:
    # 测试/自建镜像可以覆盖
    env = (os.environ.get("JILIU_UPDATE_API") or "").strip()
    if env:
        return env
    repo = (cfg.get("update_repo") or DEFAULT_REPO).strip() or DEFAULT_REPO
    return f"https://api.github.com/repos/{repo}/releases/latest"


def _get_json(cfg: Dict[str, Any], url: str, timeout: float = 12.0) -> Any:
    op = proxy.opener(cfg, url, timeout=timeout)
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Jiliu/{__version__}",
    })
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _asset_sha(asset: Dict[str, Any]) -> str:
    """GitHub 2024 起给 asset 带 digest 字段，形如 "sha256:abc..."。"""
    d = str(asset.get("digest") or "")
    if d.lower().startswith("sha256:"):
        return d.split(":", 1)[1].strip().lower()
    return ""


def pick_asset(assets: List[Dict[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    """挑用于更新的资产。

    安装版优先拿 setup（它会处理快捷方式与卸载项），
    其余优先拿单文件 jiliu.exe（体积最小，替换最简单）。
    """
    def by_name(pred: Callable[[str], bool]) -> List[Dict[str, Any]]:
        return [a for a in assets if pred(str(a.get("name") or "").lower())]

    if kind == "installed":
        for a in by_name(lambda n: n.startswith("jiliu-setup") and n.endswith(".exe")):
            return a
    for a in by_name(lambda n: n == "jiliu.exe"):
        return a
    for a in by_name(lambda n: n.startswith("jiliu") and n.endswith(".exe")):
        return a
    for a in by_name(lambda n: n.startswith("jiliu-portable") and n.endswith(".zip")):
        return a
    return None


def _allowed_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)


def check(cfg: Optional[Dict[str, Any]] = None, *, force: bool = False,
          timeout: float = 12.0) -> Dict[str, Any]:
    """查一次有没有新版。永远不抛异常 —— 更新检查失败不该影响开程序。"""
    cfg = cfg or _config.load()
    now = time.monotonic()
    with _cache_lock:
        if not force and _cache["data"] is not None and now - _cache["at"] < CHECK_TTL:
            return dict(_cache["data"])

    kind = install_kind()
    out: Dict[str, Any] = {
        "ok": False,
        "current": __version__,
        "latest": "",
        "has_update": False,
        "kind": kind,
        "can_update": can_self_update()[0],
        "reason": "",
        "error": "",
        "notes": "",
        "url": "",
        "published": "",
        "asset": None,
        "checked_at": time.time(),
        "auto": bool(cfg.get("update_check", True)),
    }
    if not cfg.get("update_check", True) and not force:
        out["reason"] = "已在设置里关闭更新检查"
        return out

    url = _api_url(cfg)
    try:
        data = _get_json(cfg, url, timeout=timeout)
    except urllib.error.HTTPError as exc:
        out["error"] = f"GitHub 返回 {exc.code}" + (
            "（仓库还没有发布任何 Release）" if exc.code == 404 else "")
        with _cache_lock:
            _cache.update(at=now, data=out)
        return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        with _cache_lock:
            _cache.update(at=now, data=out)
        return out

    latest = str(data.get("tag_name") or data.get("name") or "").strip()
    out["ok"] = True
    out["latest"] = latest.lstrip("vV")
    out["notes"] = str(data.get("body") or "")
    out["url"] = str(data.get("html_url") or "")
    out["published"] = str(data.get("published_at") or "")
    out["prerelease"] = bool(data.get("prerelease"))
    out["has_update"] = is_newer(latest, __version__)

    # 预发布版只有显式选了 beta 通道才提示
    if out["has_update"] and out["prerelease"] and cfg.get("update_channel") != "beta":
        out["has_update"] = False
        out["reason"] = "新版本是预发布版，当前通道只收稳定版"

    asset = pick_asset(list(data.get("assets") or []), kind)
    if asset:
        out["asset"] = {
            "name": asset.get("name"),
            "size": int(asset.get("size") or 0),
            "url": asset.get("browser_download_url") or "",
            "sha256": _asset_sha(asset),
        }
    if out["has_update"] and not out["asset"]:
        out["has_update"] = False
        out["reason"] = "新版本没有可用的更新包"

    ok, why = can_self_update()
    if out["has_update"] and not ok:
        out["can_update"] = False
        out["reason"] = why

    with _cache_lock:
        _cache.update(at=now, data=out)
    return out


def invalidate() -> None:
    with _cache_lock:
        _cache.update(at=0.0, data=None)


# ----------------------------------------------------------------------
# 下载与校验
# ----------------------------------------------------------------------

def download(url: str, dest: pathlib.Path, cfg: Dict[str, Any],
             *, expect_sha: str = "", progress: Optional[Callable[[int, int], None]] = None,
             cancel: Optional[threading.Event] = None,
             timeout: float = 30.0) -> Dict[str, Any]:
    """流式下载并同时算 sha256。返回 {ok, bytes, sha256, error}。

    先写 .part，校验通过再改名 —— 半截文件绝不能出现在 update/ 里，
    否则下次启动会以为已经下好。
    """
    if not _allowed_url(url):
        return {"ok": False, "error": f"下载地址不在允许列表内：{url}"}

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(str(dest) + ".part")
    h = hashlib.sha256()
    got = 0
    total = 0
    try:
        op = proxy.opener(cfg, url, timeout=timeout)
        req = urllib.request.Request(url, headers={"User-Agent": f"Jiliu/{__version__}"})
        with op.open(req, timeout=timeout) as r:
            total = int(r.headers.get("Content-Length") or 0)
            with open(tmp, "wb") as f:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise RuntimeError("已取消")
                    buf = r.read(262144)
                    if not buf:
                        break
                    f.write(buf)
                    h.update(buf)
                    got += len(buf)
                    if progress:
                        try:
                            progress(got, total)
                        except Exception:
                            pass
        digest = h.hexdigest()
        want = (expect_sha or "").strip().lower()
        if want and digest != want:
            tmp.unlink(missing_ok=True)
            return {"ok": False, "bytes": got, "sha256": digest,
                    "error": f"校验失败：期望 {want[:12]}…，实际 {digest[:12]}…"}
        tmp.replace(dest)
        return {"ok": True, "bytes": got, "sha256": digest, "error": ""}
    except Exception as exc:  # noqa: BLE001
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "bytes": got, "sha256": "", "error": f"{type(exc).__name__}: {exc}"}


def sha256_file(path: pathlib.Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ----------------------------------------------------------------------
# 暂存与延迟替换
# ----------------------------------------------------------------------

def staging_dir() -> pathlib.Path:
    return _config.HOME / "update"


def pending_path() -> pathlib.Path:
    return staging_dir() / "pending.json"


def stage(info: Dict[str, Any], cfg: Dict[str, Any],
          progress: Optional[Callable[[int, int], None]] = None,
          cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
    """下载更新包到 update/，并写 pending.json。"""
    asset = info.get("asset") or {}
    url = str(asset.get("url") or "")
    if not url:
        return {"ok": False, "error": "没有可下载的更新包"}
    # 没有官方 digest 的资产拒绝自动安装，只提示手动下载。
    # 以前 `if want and ...` —— want 为空时直接跳过校验，
    # 旧 release 或镜像资产等于只靠 HTTPS 保平安。
    if not str(asset.get("sha256") or "").strip():
        return {
            "ok": False,
            "error": "更新包缺少官方 SHA-256，已拒绝自动安装。"
                     "请到 Release 页面手动下载。",
            "url": info.get("url") or "",
        }
    name = pathlib.Path(str(asset.get("name") or "update.bin")).name
    dest = staging_dir() / name
    res = download(url, dest, cfg, expect_sha=str(asset.get("sha256") or ""),
                   progress=progress, cancel=cancel)
    if not res.get("ok"):
        return res
    payload = {
        "version": info.get("latest") or "",
        "kind": info.get("kind") or install_kind(),
        "file": str(dest),
        "sha256": res.get("sha256") or "",
        "size": res.get("bytes") or 0,
        "staged_at": time.time(),
        "notes_url": info.get("url") or "",
    }
    pending_path().write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    return {"ok": True, "pending": payload}


def pending() -> Optional[Dict[str, Any]]:
    p = pending_path()
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    f = pathlib.Path(str(d.get("file") or ""))
    if not f.is_file():
        return None
    d["file"] = str(f)
    return d


def discard() -> None:
    """丢掉暂存的更新（用户点「以后再说」时用）。"""
    p = pending_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except Exception:
        data = {}
    for key in ("file",):
        f = pathlib.Path(str(data.get(key) or ""))
        try:
            if f.is_file():
                f.unlink()
        except Exception:
            pass
    try:
        p.unlink(missing_ok=True)
    except Exception:
        pass


def _bat_quote(p: pathlib.Path | str) -> str:
    return '"' + str(p).replace('"', '""') + '"'


def write_apply_script(kind: str, payload: Dict[str, Any]) -> pathlib.Path:
    """写延迟替换脚本，返回 .cmd 路径。

    脚本干的事：等本进程退出 → 替换/安装 → 启动新版 → 删自己。
    全 ASCII，避免 chcp 与编码问题。
    """
    root = app_dir()
    st = staging_dir()
    bat = st / "apply.cmd"
    pid = os.getpid()
    pkg = pathlib.Path(str(payload.get("file") or ""))
    exe = root / "jiliu.exe"

    lines = [
        "@echo off",
        "setlocal",
        # 等父进程真正退出（最多 180 秒，超时也继续 —— 用户可能已经强杀）
        f'powershell -NoProfile -WindowStyle Hidden -Command'
        f' "try{{Wait-Process -Id {pid} -Timeout 180 -ErrorAction Stop}}catch{{}}"',
    ]
    if kind == "installed":
        lines += [
            f"{_bat_quote(pkg)} --quiet --target {_bat_quote(root)}"
            + " --no-desktop >nul 2>&1",
        ]
    else:
        lines += [
            f'move /y {_bat_quote(pkg)} {_bat_quote(exe)} >nul 2>&1',
            # 极少数情况下 move 失败（杀软扫描），再试一次
            f'if not exist {_bat_quote(exe)} timeout /t 2 /nobreak >nul',
            f'if exist {_bat_quote(pkg)} move /y {_bat_quote(pkg)} {_bat_quote(exe)} >nul 2>&1',
        ]
    lines += [
        f'start "" {_bat_quote(exe)}',
        f'del {_bat_quote(pkg)} >nul 2>&1',
        "del \"%~f0\"",
    ]
    bat.write_text("\r\n".join(lines) + "\r\n", encoding="ascii", errors="replace")

    # 便携版如果下的是 zip，脚本里还得先解压 —— 这里单独处理
    if kind != "installed" and pkg.suffix.lower() == ".zip":
        lines = [
            "@echo off",
            "setlocal",
            f'powershell -NoProfile -WindowStyle Hidden -Command'
            f' "try{{Wait-Process -Id {pid} -Timeout 180 -ErrorAction Stop}}catch{{}}"',
            f'powershell -NoProfile -WindowStyle Hidden -Command'
            f' "Expand-Archive -LiteralPath {_bat_quote(pkg)} -DestinationPath'
            f' {_bat_quote(st / "x")} -Force"',
            # 便携 zip 是「jiliu-portable-x.y.z/」一层目录
            f'for /d %%D in ({_bat_quote(st / "x")}\\*) do ('
            f'copy /y "%%D\\jiliu.exe" {_bat_quote(exe)} >nul 2>&1)',
            f'start "" {_bat_quote(exe)}',
            f'rmdir /s /q {_bat_quote(st / "x")} >nul 2>&1',
            f'del {_bat_quote(pkg)} >nul 2>&1',
            "del \"%~f0\"",
        ]
        bat.write_text("\r\n".join(lines) + "\r\n", encoding="ascii", errors="replace")
    return bat


def apply_pending(quiet: bool = False) -> bool:
    """在程序退出前调用：有暂存更新就安排替换。

    返回 True 表示「已安排」，进程随后应当正常退出。
    源码运行时不做事（不能把自己换掉）。
    """
    data = pending()
    if not data:
        return False
    if install_kind() == "source":
        return False
    try:
        bat = write_apply_script(str(data.get("kind") or install_kind()), data)
    except Exception:
        return False
    flags = 0
    if os.name == "nt":
        flags = 0x08000000 | 0x00000008   # CREATE_NO_WINDOW | DETACHED_PROCESS
    try:
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=flags, close_fds=True)
    except Exception:
        return False
    return True


# ----------------------------------------------------------------------
# 后台检查
# ----------------------------------------------------------------------

def on_update_available(cfg: Dict[str, Any], info: Dict[str, Any]) -> None:
    if not cfg.get("notify_enabled", True):
        return
    try:
        from . import notify as _notify
        _notify.notify(
            f"汲流 {info.get('latest')} 可用",
            "打开「关于」页即可一键更新" if not cfg.get("update_auto_download")
            else "正在后台下载，完成后退出时自动替换",
        )
    except Exception:
        pass


def start_watcher(cfg: Dict[str, Any], on_found: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
    """启动后台检查线程（启动 30 秒后一次，之后每 6 小时一次）。"""
    global _watcher
    if _watcher is not None and _watcher.is_alive():
        return
    if not cfg.get("update_check", True):
        return
    _watcher_stop.clear()

    def loop() -> None:
        if _watcher_stop.wait(STARTUP_DELAY):
            return
        while not _watcher_stop.is_set():
            try:
                info = check(cfg, force=True)
                if info.get("has_update"):
                    if on_found:
                        try:
                            on_found(info)
                        except Exception:
                            pass
                    if cfg.get("update_auto_download"):
                        try:
                            stage(info, cfg)
                        except Exception:
                            pass
            except Exception:
                pass
            if _watcher_stop.wait(WATCH_INTERVAL):
                return

    _watcher = threading.Thread(target=loop, daemon=True, name="jiliu-update")
    _watcher.start()


def stop_watcher() -> None:
    _watcher_stop.set()


# ----------------------------------------------------------------------
# 自测用：把「远程」换成本地目录
# ----------------------------------------------------------------------

def describe(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or _config.load()
    ok, why = can_self_update()
    try:
        ver = __version__
    except Exception:
        ver = ""
    return {
        "current": ver,
        "kind": install_kind(),
        "can_update": ok,
        "reason": why,
        "dir": str(app_dir()),
        "pending": (pending() or {}).get("version", ""),
        "channel": cfg.get("update_channel", "stable"),
        "auto_check": bool(cfg.get("update_check", True)),
        "auto_download": bool(cfg.get("update_auto_download", False)),
    }
