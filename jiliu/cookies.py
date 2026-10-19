"""Cookie 管理：给需要登录态的站点提供凭证（YouTube、B站、私密视频等）。

YouTube 的 `Sign in to confirm you're not a bot` 不是 bug，是 Google 的
风控：从数据中心/共享出口 IP 过来的匿名请求，它一律要求出示登录态。
拿不到 cookie，解析就一定失败，跟 yt-dlp 版本没关系。

三条路，按可靠性排序：

1）**浏览器扩展一键导出**（推荐，本模块的主路径）
   汲流自带扩展，而主人日常用的浏览器里早就登录了 YouTube / B站。
   扩展用 chrome.cookies 把 cookie 读出来发给汲流，我们转成 Netscape
   格式存档。用的是现成登录态，不用再登一次，也不用关浏览器。

2）**`--cookies-from-browser` 直读**
   让 yt-dlp 自己去读浏览器 cookie 库。⚠ 现实是这条路废了大半：
   Chrome 127+ 引入 App-Bound 加密，cookie 库不再由 DPAPI 直接保护，
   纯 Python（包括 yt-dlp）解不开，报
   `Could not copy Chrome cookie database`（yt-dlp issue #7271）。
   Edge 同理。只有 Firefox 和旧版 Chromium 还可靠。
   所以这个功能保留，但失败时必须给出**能看懂的解释**，而不是把
   yt-dlp 的英文原文丢给用户。

3）**手动导入 cookies.txt**
   什么情况下都能用的保底。浏览器插件（如
   "Get cookies.txt LOCALLY"）导出的就是这个格式。

存下来的 cookie 按域名自动匹配使用：`cookies/youtube.com.txt` 只在访问
youtube.com 时带上，不会把 YouTube 的凭证发给 B站。
"""
from __future__ import annotations

import os
import pathlib
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config as _config

# 支持的浏览器。值是 (可执行文件候选路径, 用户数据目录, 说明)
_LA = os.environ.get("LOCALAPPDATA", "")
_RA = os.environ.get("APPDATA", "")
_PF = os.environ.get("PROGRAMFILES", r"C:\Program Files")
_PF86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")

BROWSERS: Dict[str, Dict[str, Any]] = {
    "chrome": {
        "label": "Google Chrome",
        "exe": [rf"{_PF}\Google\Chrome\Application\chrome.exe",
                rf"{_PF86}\Google\Chrome\Application\chrome.exe"],
        "data": rf"{_LA}\Google\Chrome\User Data",
        # Chrome 新版对 cookie 库加了 App-Bound 加密，yt-dlp 读不了
        "hard": True,
        "note": "127 版以后 cookie 库加了额外加密，yt-dlp 直读大概率失败；建议用扩展导出",
    },
    "edge": {
        "label": "Microsoft Edge",
        "exe": [rf"{_PF86}\Microsoft\Edge\Application\msedge.exe",
                rf"{_PF}\Microsoft\Edge\Application\msedge.exe"],
        "data": rf"{_LA}\Microsoft\Edge\User Data",
        "hard": True,
        "note": "和 Chrome 同源，同样有 App-Bound 加密问题",
    },
    "firefox": {
        "label": "Mozilla Firefox",
        "exe": [rf"{_PF}\Mozilla Firefox\firefox.exe",
                rf"{_PF86}\Mozilla Firefox\firefox.exe"],
        "data": rf"{_RA}\Mozilla\Firefox\Profiles",
        "hard": False,   # Firefox 不用 App-Bound，yt-dlp 能正常读
        "note": "cookie 未加 App-Bound 加密，是直读最可靠的浏览器",
    },
    "brave": {
        "label": "Brave",
        "exe": [rf"{_PF}\BraveSoftware\Brave-Browser\Application\brave.exe"],
        "data": rf"{_LA}\BraveSoftware\Brave-Browser\User Data",
        "hard": True,
        "note": "",
    },
    "chromium": {
        "label": "Chromium",
        "exe": [rf"{_PF}\Chromium\Application\chrome.exe"],
        "data": rf"{_LA}\Chromium\User Data",
        "hard": True,
        "note": "",
    },
    "vivaldi": {
        "label": "Vivaldi",
        "exe": [rf"{_LA}\Vivaldi\Application\vivaldi.exe"],
        "data": rf"{_LA}\Vivaldi\User Data",
        "hard": True,
        "note": "",
    },
    "opera": {
        "label": "Opera",
        "exe": [rf"{_LA}\Programs\Opera\opera.exe"],
        "data": rf"{_RA}\Opera Software\Opera Stable",
        "hard": True,
        "note": "",
    },
}

# Netscape cookie 文件的首行，不能少 —— 少了 MozillaCookieJar 不认
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
NETSCAPE_HINT = "# 由汲流生成。这个文件等于你的登录凭证，别外传。"


def _first_existing(paths: List[str]) -> str:
    for p in paths:
        try:
            if p and pathlib.Path(os.path.expandvars(p)).is_file():
                return os.path.expandvars(p)
        except Exception:
            continue
    return ""


def cookie_dir() -> pathlib.Path:
    d = _config.HOME / "cookies"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# 浏览器探测
# --------------------------------------------------------------------------

def available_browsers() -> List[Dict[str, Any]]:
    """列出这台机器上装了哪些浏览器、各自的 profile。"""
    out: List[Dict[str, Any]] = []
    for key, meta in BROWSERS.items():
        exe = _first_existing(meta["exe"])
        data = pathlib.Path(os.path.expandvars(meta["data"]))
        if not exe and not data.exists():
            continue
        profiles: List[str] = []
        if data.exists():
            if key == "firefox":
                profiles = [x.name for x in data.iterdir() if x.is_dir()]
            else:
                # Chromium 系：Default / Profile 1 / Profile 2
                for x in data.iterdir():
                    if not x.is_dir():
                        continue
                    if x.name == "Default" or x.name.startswith("Profile "):
                        profiles.append(x.name)
        out.append({
            "key": key,
            "label": meta["label"],
            "exe": exe,
            "data_dir": str(data) if data.exists() else "",
            "profiles": sorted(profiles)[:12],
            "installed": bool(exe),
            "hard": meta["hard"],
            "note": meta["note"],
        })
    # 装了 exe 的排前面
    out.sort(key=lambda x: (not x["installed"], x["key"]))
    return out


# --------------------------------------------------------------------------
# 格式转换（扩展 / 手动导入 都走这里）
# --------------------------------------------------------------------------

def _expiry(c: Dict[str, Any]) -> int:
    for k in ("expirationDate", "expires", "expiry"):
        v = c.get(k)
        if v in (None, ""):
            continue
        try:
            return int(float(v))
        except Exception:
            continue
    return 0   # 0 = 会话 cookie，Netscape 里是合法写法


def to_netscape(cookies: List[Dict[str, Any]], *, header: bool = True) -> str:
    """把 [{name, value, domain, ...}] 转成 Netscape cookies.txt 文本。

    格式（TAB 分隔，7 列）：
        domain  include_subdomains  path  secure  expiry  name  value

    httpOnly 的 cookie 要在 domain 前加 `#HttpOnly_` 前缀 ——
    这是 Netscape 格式的历史包袱：看着像注释行，但 Python 的
    MozillaCookieJar 会特殊处理它，加上才不会丢 HttpOnly 标记。
    """
    lines: List[str] = []
    if header:
        lines.append(NETSCAPE_HEADER)
        lines.append(NETSCAPE_HINT)
    seen = set()
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        domain = str(c.get("domain") or "").strip()
        if not name or not domain:
            continue
        value = c.get("value")
        value = "" if value is None else str(value)
        # TAB / 换行会破坏列结构，只能丢
        if any(ch in value for ch in ("\t", "\n", "\r")):
            continue
        key = (domain, str(c.get("path") or "/"), name)
        if key in seen:
            continue
        seen.add(key)

        path = str(c.get("path") or "/") or "/"
        secure = bool(c.get("secure"))
        httponly = bool(c.get("httpOnly") or c.get("httponly"))
        # 域以 . 开头表示包含子域
        sub = domain.startswith(".")
        exp = _expiry(c)

        prefix = "#HttpOnly_" if httponly else ""
        lines.append("\t".join([
            prefix + domain,
            "TRUE" if sub else "FALSE",
            path,
            "TRUE" if secure else "FALSE",
            str(exp),
            name,
            value,
        ]))
    return "\n".join(lines) + "\n"


def parse_netscape(text: str) -> List[Dict[str, Any]]:
    """读回 Netscape 文本，用于校验与统计。"""
    out: List[Dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.strip("\n").strip("\r")
        if not line:
            continue
        httponly = False
        if line.startswith("#HttpOnly_"):
            httponly = True
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, sub, path, secure, exp, name, value = parts[:7]
        out.append({
            "domain": domain, "path": path, "name": name, "value": value,
            "secure": secure.upper() == "TRUE",
            "httpOnly": httponly,
            "includeSubdomains": sub.upper() == "TRUE",
            "expiry": int(exp) if exp.isdigit() else 0,
        })
    return out


def validate(text: str) -> Dict[str, Any]:
    """校验一份 cookies 文本是否可用，并统计覆盖的站点。"""
    rows = parse_netscape(text)
    now = int(time.time())
    domains: Dict[str, int] = {}
    expired = 0
    for r in rows:
        d = r["domain"].lstrip(".")
        domains[d] = domains.get(d, 0) + 1
        if r["expiry"] and r["expiry"] < now:
            expired += 1
    return {
        "ok": bool(rows),
        "count": len(rows),
        "domains": sorted(domains.items(), key=lambda x: -x[1])[:20],
        "expired": expired,
        "has_login_hint": any(
            r["name"].lower() in ("sid", "sapisid", "__secure-1psid",
                                  "sapisidhash", "c_user", "sessionid")
            for r in rows
        ),
    }


# --------------------------------------------------------------------------
# 存 / 取
# --------------------------------------------------------------------------

def _slug(url_or_domain: str) -> str:
    from . import proxy as _proxy
    host = url_or_domain
    if "://" in host or "/" in host:
        host = _proxy.host_of(host)
    host = (host or "").lower().strip().lstrip(".")
    # 只保留合法域名字符，防路径穿越
    safe = "".join(ch for ch in host if ch.isalnum() or ch in ".-")
    return safe.strip(".") or "cookies"


def save_text(text: str, label: str = "imported") -> pathlib.Path:
    """存一份 cookies 文本，文件名按站点归类。"""
    info = validate(text)
    if not info["ok"]:
        raise ValueError("这份内容里没有解析出任何 cookie，可能不是 Netscape 格式")
    # 用出现最多的域名当文件名
    name = label
    if not name or name == "imported":
        if info["domains"]:
            name = info["domains"][0][0]
    dest = cookie_dir() / f"{_slug(name)}.txt"
    if not text.lstrip().startswith("#"):
        text = NETSCAPE_HEADER + "\n" + NETSCAPE_HINT + "\n" + text
    dest.write_text(text, encoding="utf-8")
    try:
        os.chmod(dest, 0o600)   # 这是凭证，尽量收紧权限
    except Exception:
        pass
    return dest


def list_files() -> List[Dict[str, Any]]:
    """列出已存的所有 cookie 文件。"""
    out: List[Dict[str, Any]] = []
    d = cookie_dir()
    for f in sorted(d.glob("*.txt")):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        info = validate(txt)
        st = f.stat()
        out.append({
            "file": f.name,
            "path": str(f),
            "domain": f.stem,
            "count": info["count"],
            "domains": info["domains"][:4],
            "expired": info["expired"],
            "login_hint": info["has_login_hint"],
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    return out


def remove(path_or_name: str) -> bool:
    p = pathlib.Path(path_or_name)
    if not p.is_absolute():
        p = cookie_dir() / p
    try:
        p.unlink()
        return True
    except Exception:
        return False


def site_file(url: str) -> Optional[pathlib.Path]:
    """按 URL 域名找对应的 cookie 文件。

    匹配从最具体到最宽：www.youtube.com → www.youtube.com.txt
    → youtube.com.txt。找不到就返回 None，不会乱拿别的站的 cookie。
    """
    from . import proxy as _proxy
    host = _proxy.host_of(url)
    if not host:
        return None
    parts = host.split(".")
    d = cookie_dir()
    for i in range(len(parts) - 1):
        cand = d / (".".join(parts[i:]) + ".txt")
        if cand.is_file():
            return cand
    return None


# --------------------------------------------------------------------------
# 从浏览器直读（yt-dlp 的 API）
# --------------------------------------------------------------------------

def _humanize(err: str, browser: str) -> str:
    """把 yt-dlp 的英文报错翻成人能照着做的中文。"""
    e = (err or "").strip()
    low = e.lower()
    if "could not copy" in low and "cookie" in low:
        label = BROWSERS.get(browser, {}).get("label", browser)
        return (
            f"{label} 的 cookie 库被浏览器独占着，或者用了新版加密，读不出来。\n"
            f"  这是已知限制（yt-dlp issue #7271）：Chrome/Edge 127+ 给 cookie\n"
            f"  加了 App-Bound 加密，纯 Python 解不开，跟汲流无关。\n"
            f"  三个办法，任选：\n"
            f"    1. 用汲流的浏览器扩展导出（最省事，用你现在的登录态）\n"
            f"    2. 完全退出 {label} 后再试一次（有时只是文件被占用）\n"
            f"    3. 手动导出 cookies.txt 后导入"
        )
    if "permission" in low or "denied" in low:
        return f"没有权限读 {browser} 的 cookie 库。试试完全退出浏览器后重试。"
    if "no such file" in low or "not found" in low:
        return f"没找到 {browser} 的 cookie 文件。确认浏览器装好并至少启动过一次。"
    if "decrypt" in low or "dpapi" in low:
        return f"{browser} 的 cookie 解密失败（通常是 Windows 账户密钥变了）。建议改用扩展导出。"
    return e or "读取失败"


def from_browser(browser: str, profile: str = "", *, timeout: int = 60
                 ) -> Dict[str, Any]:
    """让 yt-dlp 去读浏览器 cookie，成功后存成我们自己的 cookies.txt。

    之所以落盘而不是每次直读：Chrome/Edge 那条路大概率读不了，
    一旦成功就该固化下来复用，省得每次下载都去撞一遍。
    """
    browser = (browser or "").strip().lower()
    if browser not in BROWSERS:
        return {"ok": False, "error": f"不认识的浏览器：{browser}"}

    try:
        import yt_dlp
    except ImportError:
        return {"ok": False,
                "error": "内置的 yt-dlp 模块不可用，无法读取浏览器 cookie"}

    # cookiesfrombrowser 的元组不同版本长度不一样，从长到短试
    attempts = [
        (browser, profile or None, None, None),
        (browser, profile or None, None),
        (browser, profile or None),
        (browser,),
    ]
    last_err = ""
    for cfb in attempts:
        opts = {
            "cookiesfrombrowser": cfb,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": max(5, int(timeout)),
            "skip_download": True,
        }
        try:
            import contextlib, io
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                with yt_dlp.YoutubeDL(opts) as ydl:
                    jar = ydl.cookiejar
                    rows = list(jar)
            if not rows:
                last_err = "浏览器 cookie 库是空的（可能从没登录过）"
                continue
            dest = cookie_dir() / f"{_slug(browser)}.txt"
            jar.save(str(dest), ignore_discard=True, ignore_expires=True)
            txt = dest.read_text(encoding="utf-8", errors="replace")
            info = validate(txt)
            return {
                "ok": True,
                "path": str(dest),
                "file": dest.name,
                "count": info["count"],
                "domains": info["domains"][:6],
                "browser": browser,
                "profile": profile,
                "login_hint": info["has_login_hint"],
            }
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            # 参数长度不对就换下一个；其他错误直接判定失败
            if "cookiesfrombrowser" not in last_err and "unexpected" not in last_err.lower():
                break
    return {"ok": False, "browser": browser, "raw": last_err,
            "error": _humanize(last_err, browser)}


# --------------------------------------------------------------------------
# 给 yt-dlp 用的参数
# --------------------------------------------------------------------------

def ytdlp_args(cfg: Dict[str, Any], url: str = "") -> List[str]:
    """这个 URL 该带哪些 cookie 参数。

    优先级：站点专属文件 > 全局 cookie_file > 浏览器直读。
    站点专属文件排第一，是因为它能同时支持多个站点且互不串味。
    """
    cfg = cfg or {}
    mode = (cfg.get("cookie_mode") or "auto").strip().lower()
    if mode in ("off", "none"):
        return []

    if url:
        f = site_file(url)
        if f is not None:
            return ["--cookies", str(f)]

    gf = (cfg.get("cookie_file") or "").strip()
    if gf and pathlib.Path(gf).is_file():
        return ["--cookies", gf]

    if mode == "browser":
        br = (cfg.get("cookie_browser") or "").strip().lower()
        if br:
            prof = (cfg.get("cookie_browser_profile") or "").strip()
            spec = f"{br}:{prof}" if prof else br
            return ["--cookies-from-browser", spec]
    return []


def describe(cfg: Dict[str, Any], url: str = "") -> Dict[str, Any]:
    """给界面看的 cookie 状态。"""
    files = list_files()
    chosen = ""
    if url:
        f = site_file(url)
        if f is not None:
            chosen = f.name
    elif (cfg.get("cookie_file") or ""):
        chosen = pathlib.Path(cfg["cookie_file"]).name
    return {
        "mode": (cfg.get("cookie_mode") or "auto"),
        "files": files,
        "dir": str(cookie_dir()),
        "site_file": chosen,
        "browser": cfg.get("cookie_browser") or "",
        "has_any": bool(files),
        "note": ("已为 %d 个站点存了 cookie" % len(files)) if files
                else "还没有任何 cookie。YouTube 这类站点需要登录态才能解析。",
    }
