"""代理：决定每个 URL 走不走代理、走哪一个。

为什么值得单独一个模块 —— 三条都是本机踩过的现实：

1）**环境变量里的代理可能是死的。**
   本机常留着 `HTTP_PROXY=http://127.0.0.1:60042`，那是上次开的代理
   客户端留下的。客户端一关，所有请求立刻 ConnectionRefused，
   而报错里一个字都不提「代理」。用户看到的是「下载失败」，
   然后去翻代码找半天。这个模块负责在动手前先验一下，
   死了就直连并给出提示。

2）**国内外站点需求相反。**
   国内站直连最快，套上代理反而慢甚至不通；国外站没代理根本连不上。
   yt-dlp 只认一个 `--proxy`，不会自己分，得分在调用方。

3）**yt-dlp 与自研引擎读代理的方式不同。**
   自研引擎走 urllib，默认会读环境变量；yt-dlp 也读，
   但 `--proxy ""` 能强制关掉。两边都要能精确控制，
   否则会出现「插件能下、主程序不能」这类诡异不一致。
"""
from __future__ import annotations

import os
import socket
import time
import ipaddress
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import resource

# 环境里可能出现的代理变量名，大小写两种都见过
ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
            "ALL_PROXY", "all_proxy")

# 缺省端口：用户常只填主机名
_DEFAULT_PORTS = {"http": 80, "https": 443,
                  "socks": 1080, "socks4": 1080, "socks4a": 1080,
                  "socks5": 1080, "socks5h": 1080}

# 代理探测结果缓存。代理起落不频繁，30 秒内复用结论就够。
_TTL = 30.0
_cache: Dict[str, Any] = {"t": 0.0, "key": None, "val": None}


@dataclass
class ProxyInfo:
    """一个代理端点的体检报告。"""
    url: str = ""
    source: str = ""          # 来自哪个环境变量 / 配置 / 直连
    alive: bool = False
    checked: bool = False     # 是否真的连过（未检查与检查失败要区分）
    latency: float = 0.0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# URL / 域名
# --------------------------------------------------------------------------

def is_local_host(host: str) -> bool:
    """这台机器自己 / 局域网里的地址。

    这些地址**永远不该走代理**：代理客户端通常拒绝连本地端口，
    表现是 ConnectionRefused，看起来像「站点挂了」或「代理配错了」，
    实际只是把 127.0.0.1 也甩给了代理。主人的本机 mock 服务、
    局域网 NAS 都在这一类。
    """
    h = (host or "").strip().lower().strip("[]")
    if not h:
        return False
    if h in ("localhost", "localhost.localdomain", "::1", "0.0.0.0"):
        return True
    if h.endswith(".local") or h.endswith(".lan") or h.endswith(".localhost"):
        return True
    # 不带点的主机名（http://nas/xxx）几乎一定是内网机器
    if "." not in h and ":" not in h:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def host_of(url: str) -> str:
    """从 URL 里取主机名，去掉端口。"""
    s = (url or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    try:
        h = urllib.parse.urlparse(s).hostname or ""
    except Exception:
        return ""
    return h.lower()


def is_domestic(url_or_host: str) -> bool:
    """这个地址是不是国内的。

    判断顺序：已知国内平台域名 → 国别后缀 → 否则算国外。
    宁可把未知站点当国外（那样会走代理，最多慢一点），
    也不要把真的国外站当国内（那样直接连不上）。
    """
    host = url_or_host.lower().strip()
    if "://" in host or "/" in host:
        host = host_of(host)
    if not host:
        return False

    # 已知国内平台
    for d in resource.DOMESTIC_PLATFORMS:
        if host == d or host.endswith("." + d):
            return True

    # 国别后缀
    for suf in resource.DOMESTIC_SUFFIXES:
        if host.endswith(suf):
            return True

    # IP 直连国内网段不好判，一律当国外（走代理更安全）
    return False


def sanitize(raw: str) -> str:
    """把用户随手填的代理串补成合法 URL。

    `127.0.0.1:7890` 这种写法很常见，但它没有 scheme，
    urllib 会当成相对路径直接报错 —— 所以补一个 http://。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if "//" not in s:
        s = "http://" + s
    return s


def _split(proxy_url: str) -> Tuple[str, int]:
    u = urllib.parse.urlparse(sanitize(proxy_url))
    scheme = (u.scheme or "http").lower()
    host = u.hostname or ""
    port = u.port or _DEFAULT_PORTS.get(scheme, 8080)
    return host, int(port)


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------

def _tcp(host: str, port: int, timeout: float) -> Tuple[bool, float, str]:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, time.monotonic() - t0, ""
    except Exception as exc:  # noqa: BLE001
        return False, time.monotonic() - t0, f"{type(exc).__name__}: {exc}"


def test(proxy_url: str, timeout: float = 3.0) -> ProxyInfo:
    """测一个代理端点是否活着（TCP 能连上就算活）。"""
    raw = (proxy_url or "").strip()
    if not raw:
        # 空串是「明确不走代理」，不是错误
        return ProxyInfo(url="", source="直连", alive=True, checked=True)
    url = sanitize(raw)
    host, port = _split(url)
    if not host:
        return ProxyInfo(url=url, source="配置", alive=False, checked=True,
                         error="代理地址解析不出主机名")
    ok, dt, err = _tcp(host, port, timeout)
    return ProxyInfo(url=url, source="配置", alive=ok, checked=True,
                     latency=round(dt, 3), error=err)


def env_proxy() -> Tuple[str, str]:
    """返回 (代理地址, 来源变量名)。多个变量时取第一个非空的。"""
    for k in ENV_KEYS:
        v = (os.environ.get(k) or "").strip()
        if v and not v.lower().startswith(("0.0.0.0", "none")):
            return v, k
    return "", ""


# --------------------------------------------------------------------------
# 系统代理（Windows「Internet 选项」里的那份设置）
# --------------------------------------------------------------------------

# 注册表里 ProxyServer 的几种写法都见过，得都能认：
#   127.0.0.1:7890
#   http=127.0.0.1:7890;https=127.0.0.1:7891
#   socks=127.0.0.1:1080
_IE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

_sys_cache: Dict[str, Any] = {"t": 0.0, "val": None}
_SYS_TTL = 30.0


def _read_ie_settings() -> Dict[str, Any]:
    """读 Windows 的 Internet 选项代理设置。非 Windows / 读不到就返回空。"""
    out: Dict[str, Any] = {"enable": False, "server": "", "pac": "",
                           "bypass": "", "error": ""}
    if os.name != "nt":
        out["error"] = "非 Windows 系统"
        return out
    try:
        import winreg
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"winreg 不可用：{exc}"
        return out

    def _get(name: str):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _IE_KEY) as k:
                v, _ = winreg.QueryValueEx(k, name)
                return v
        except Exception:  # noqa: BLE001
            return None

    try:
        en = _get("ProxyEnable")
        srv = _get("ProxyServer") or ""
        pac = _get("AutoConfigURL") or ""
        byp = _get("ProxyOverride") or ""
        out.update(
            # 有时值是字符串 "0"/"1"
            enable=bool(int(en)) if en is not None else False,
            server=str(srv).strip(),
            pac=str(pac).strip(),
            bypass=str(byp).strip(),
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _parse_ie_server(server: str, pac: str) -> str:
    """把 ProxyServer 那串折腾成能用的代理 URL。"""
    s = (server or "").strip()
    if not s:
        # 只配了 PAC。PAC 要跑 JS 才能算出代理，这里不解释它 ——
        # 但也不能假装没代理，交给调用方决定（见 system_proxy 的 note）
        return ""
    if "=" not in s:
        # 直接就是 host:port
        return sanitize(s)

    parts: Dict[str, str] = {}
    for seg in s.split(";"):
        if "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        parts[k.strip().lower()] = v.strip()

    # 优先 https（我们的下载九成是 https），其次 http，再次 socks
    for key in ("https", "http", "socks", "socks5"):
        if parts.get(key):
            val = parts[key]
            if key.startswith("socks") and "://" not in val:
                return "socks5://" + val
            return sanitize(val)
    # 有 = 但没有我们认识的键，退回第一个值
    first = next(iter(parts.values()), "")
    return sanitize(first) if first else ""


def _bypass_match(host: str, bypass: str) -> bool:
    """域名命不命中系统代理的「不使用代理」列表。"""
    if not host or not bypass:
        return False
    h = host.lower().split(":")[0]
    for raw in bypass.replace(",", ";").split(";"):
        pat = raw.strip().lower()
        if not pat:
            continue
        if pat == "<local>":
            # IE 的约定：不带点的单段主机名（内网名）直连
            if "." not in h:
                return True
            continue
        if pat.startswith("*."):
            if h.endswith(pat[1:]) or h == pat[2:]:
                return True
            continue
        if h == pat or h.endswith("." + pat):
            return True
    return False


def system_proxy(timeout: float = 3.0, refresh: bool = False) -> ProxyInfo:
    """读系统代理（Internet 选项）并验活。

    这是「跟随系统代理」的实现。大多数下载软件默认就是这个行为 ——
    用户既然在系统里配好了代理，工具就不该另起一套。
    """
    now = time.monotonic()
    if (not refresh and _sys_cache["val"] is not None
            and now - _sys_cache["t"] < _SYS_TTL):
        return _sys_cache["val"]

    ie = _read_ie_settings()
    url = _parse_ie_server(ie.get("server", ""), ie.get("pac", ""))

    if not ie.get("enable") and not url:
        info = ProxyInfo(url="", source="系统代理未启用", alive=False,
                         checked=True, error="系统里没有启用代理")
    elif not url and ie.get("pac"):
        info = ProxyInfo(url="", source="仅 PAC 脚本", alive=False, checked=False,
                         error=f"系统只配了自动配置脚本（PAC），未解析：{ie['pac']}")
    else:
        info = test(url, timeout=timeout)
        info.source = "Windows 系统代理"
        if not info.alive:
            info.error = info.error or "系统代理连不上"

    _sys_cache.update(t=now, val=info)
    return info


def system_bypass(host: str) -> bool:
    """这个主机在不在系统的免代理列表里。"""
    ie = _read_ie_settings()
    return _bypass_match(host, ie.get("bypass", ""))


def invalidate_cache() -> None:
    _cache.update(t=0.0, key=None, val=None)
    _sys_cache.update(t=0.0, val=None)


def detect_env(timeout: float = 3.0, refresh: bool = False) -> ProxyInfo:
    """读环境变量里的代理，并**验证它是否还活着**。

    这一步是整个模块存在的主要理由：环境变量里的代理经常是死的。
    """
    raw, src = env_proxy()
    now = time.monotonic()
    key = (raw, src)
    if (not refresh and _cache["val"] is not None and _cache["key"] == key
            and now - _cache["t"] < _TTL):
        return _cache["val"]

    if not raw:
        info = ProxyInfo(url="", source="未设置", alive=False, checked=True,
                         error="环境变量里没有代理")
    else:
        info = test(raw, timeout=timeout)
        info.source = src
    _cache.update(t=now, key=key, val=info)
    return info


# --------------------------------------------------------------------------
# 决策
# --------------------------------------------------------------------------

def select(cfg: Dict[str, Any], url: str = "") -> str:
    """这个 URL 该用哪个代理。返回 "" 表示不走代理。

    决策顺序：模式 → 国内外分流 → 系统/环境/手动 取地址 → 验活。
    """
    cfg = cfg or {}
    mode = (cfg.get("proxy_mode") or "system").strip().lower()
    if mode in ("off", "none", "direct"):
        return ""
    # 本机与内网地址一律直连 —— 代理连不上它们，只会换来一个误导性的
    # ConnectionRefused（看着像站点挂了）
    if url and is_local_host(host_of(url)):
        return ""

    # 国内外分流：国内站默认直连，套代理反而更慢
    if url:
        domestic = is_domestic(url)
        if domestic and not cfg.get("proxy_domestic"):
            return ""
        if (not domestic) and cfg.get("proxy_foreign") is False:
            return ""

    if mode == "manual":
        raw = sanitize(cfg.get("proxy") or "")
        user = str(cfg.get("proxy_user") or "")
        pwd = str(cfg.get("proxy_pass") or "")
        if raw and user and "@" not in raw.split("://", 1)[-1]:
            # 带认证的代理以前配了等于没配：user/pass 两个键从未被使用
            scheme, _, rest = raw.partition("://")
            return (f"{scheme}://{urllib.parse.quote(user, safe='')}:"
                    f"{urllib.parse.quote(pwd, safe='')}@{rest}")
        return raw

    if mode == "system":
        host = host_of(url) if url else ""
        # 系统里标了「不使用代理」的域名，尊重它
        if host and system_bypass(host):
            return ""
        info = system_proxy()
        if info.alive:
            return info.url
        # 系统代理是死的：默认退回直连，否则所有请求都会撞死在一个
        # 已经不存在的代理上（用户往往不知道代理客户端已经关了）
        return "" if cfg.get("proxy_fallback_direct", True) else info.url

    # auto：环境变量里的，但必须验活。死了就当没有 —— 直连试试看，
    # 总好过拿着一个死代理把所有请求都撞死。
    info = detect_env()
    return info.url if info.alive else ""


def select_info(cfg: Dict[str, Any], url: str = "") -> ProxyInfo:
    """和 select 一样，但把「为什么这样选」也带出来，给界面显示。"""
    cfg = cfg or {}
    mode = (cfg.get("proxy_mode") or "system").strip().lower()
    if mode in ("off", "none", "direct"):
        return ProxyInfo(url="", source="已关闭", alive=True, checked=True)

    domestic = is_domestic(url) if url else False
    if url and domestic and not cfg.get("proxy_domestic"):
        return ProxyInfo(url="", source="国内站点直连", alive=True, checked=True)
    if url and not domestic and cfg.get("proxy_foreign") is False:
        return ProxyInfo(url="", source="国外站点不代理", alive=True, checked=True)

    if mode == "manual":
        info = test(cfg.get("proxy") or "")
        info.source = "手动配置"
        return info

    if mode == "system":
        host = host_of(url) if url else ""
        if host and system_bypass(host):
            return ProxyInfo(url="", source="系统免代理列表", alive=True, checked=True)
        info = system_proxy()
        if info.url and not info.alive and cfg.get("proxy_fallback_direct", True):
            info.source = (info.source or "系统代理") + "（连不上，已改直连）"
            info.url = ""
        return info

    info = detect_env()
    if not info.url:
        info.source = info.source or "环境变量未设置"
    return info


def ytdlp_args(cfg: Dict[str, Any], url: str = "") -> List[str]:
    """给 yt-dlp 的 --proxy 参数。

    注意一定要显式传：yt-dlp 自己也会读环境变量，
    我们不传它就照读那个可能已经死掉的代理。
    显式传空串等于「明确要走直连」。
    """
    p = select(cfg, url)
    return ["--proxy", p]


def opener(cfg: Dict[str, Any], url: str = "", timeout: float = 30.0):
    """给自研引擎（urllib）用的 opener。

    不这么做的话，urllib 会用 getproxies() 读环境变量 ——
    又是那个死代理的坑，而且是静默的。
    """
    p = select(cfg, url)
    if p:
        handler = urllib.request.ProxyHandler({"http": p, "https": p})
    else:
        handler = urllib.request.ProxyHandler({})   # 显式清空
    return urllib.request.build_opener(handler)


def describe(cfg: Dict[str, Any], url: str = "") -> Dict[str, Any]:
    """给界面看的代理状态。"""
    env = detect_env()
    mode = (cfg.get("proxy_mode") or "system").strip().lower()
    chosen = select_info(cfg, url)
    sysinfo = system_proxy()
    ie = _read_ie_settings()

    if mode == "manual":
        note = f"手动指定：{chosen.url or '（未填）'}"
        if chosen.url:
            note += "，可达" if chosen.alive else f"，连不上（{chosen.error}）"
    elif mode in ("off", "none", "direct"):
        note = "已关闭，全部直连"
    elif mode == "system":
        if ie.get("pac") and not ie.get("server"):
            note = f"系统只配了 PAC 自动脚本（{ie['pac']}），汲流不解析 PAC。请改用手动填地址。"
        elif not sysinfo.url:
            note = "系统没有启用代理（Internet 选项里是直连状态）"
        elif sysinfo.alive:
            note = f"跟随系统代理：{sysinfo.url}，正常"
        else:
            note = (f"系统代理 {sysinfo.url} 连不上（{sysinfo.error}）")
            if cfg.get("proxy_fallback_direct", True):
                note += " —— 已自动改为直连。要固定用它请到设置里改。"
    elif env.url:
        note = (f"取自环境变量 {env.source}：{env.url}"
                + ("，正常" if env.alive else f"，**连不上**（{env.error}）"))
        if not env.alive:
            note += " —— 已自动改为直连，避免所有请求都撞在这个死代理上"
    else:
        note = "环境变量里没有代理，全部直连"

    return {
        "mode": mode,
        "proxy": cfg.get("proxy") or "",
        "user": cfg.get("proxy_user") or "",
        "domestic": bool(cfg.get("proxy_domestic")),
        "foreign": cfg.get("proxy_foreign") is not False,
        "fallback_direct": cfg.get("proxy_fallback_direct", True),
        "env": env.to_dict(),
        "system": {**sysinfo.to_dict(),
                   "ie_enabled": bool(ie.get("enable")),
                   "ie_server": ie.get("server", ""),
                   "ie_pac": ie.get("pac", ""),
                   "ie_bypass": ie.get("bypass", "")},
        "chosen": chosen.to_dict(),
        "note": note,
        "modes": [
            {"key": "system", "label": "跟随系统代理",
             "desc": "用 Windows「Internet 选项」里的设置，多数下载软件的默认做法"},
            {"key": "manual", "label": "手动指定",
             "desc": "自己填地址，可单独设置、单独测速"},
            {"key": "auto", "label": "跟随环境变量",
             "desc": "读 HTTP_PROXY / HTTPS_PROXY，先用前会验活"},
            {"key": "off", "label": "全部直连",
             "desc": "完全不使用代理"},
        ],
        "presets": [
            {"url": "http://127.0.0.1:7890", "label": "Clash / Mihomo 默认"},
            {"url": "http://127.0.0.1:10809", "label": "v2rayN（HTTP）"},
            {"url": "socks5://127.0.0.1:10808", "label": "v2rayN（SOCKS5）"},
            {"url": "http://127.0.0.1:1080", "label": "通用 HTTP 代理"},
            {"url": "socks5://127.0.0.1:7891", "label": "Clash SOCKS5"},
            {"url": "http://127.0.0.1:10808", "label": "v2rayN 旧版（HTTP）"},
            {"url": "http://127.0.0.1:8080", "label": "通用 8080"},
            {"url": "http://127.0.0.1:33210", "label": "迅雷/其他下载器常用"},
        ],
    }
