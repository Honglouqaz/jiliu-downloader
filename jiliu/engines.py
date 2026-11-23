"""下载引擎总览与择优。

这个模块回答两个问题：

  1）**这台机器上能用哪些引擎？** —— 给界面和「环境体检」用。
  2）**这个链接该用哪个？** —— 给管理器用。

引擎分三类，职责不同，不能互相替代：

    HTTP 下载   native（自研多线程）/ aria2 / curl / wget
                都是「把字节搬下来」，区别在并发数、稳定性、依赖。
                native 最快最可控（零依赖、按类型定制分片）；
                aria2 多源与 BT；curl/wget 是兼容性兜底。

    视频解析    yt-dlp
                只有它认识几千个站点的页面结构。它不是「下载器」，
                是「解析器 + 下载器」，装了它才谈得上下 B站/YouTube。

    流处理      ffmpeg / N_m3u8DL-RE
                HLS/DASH 的分片要合流。yt-dlp 内部会调 ffmpeg；
                单独下 m3u8 直链时可以直接用。

择优规则（`recommend`）：

    P2P（磁力/种子/ed2k）       → aria2（别无选择，自研不做 P2P）
    视频页链接                 → yt-dlp
    m3u8 / mpd 直链            → yt-dlp（有 ffmpeg 时）
    普通 HTTP                  → 按 cfg['engine_order'] 挑第一个可用的
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from . import backends as _backends
from . import edition as _edition
from . import protocols as _protocols
from . import sites as _sites
from . import tools as _tools

VALID_HTTP = ("native", "aria2", "curl", "wget")

# 引擎元数据。这里写的是「定位」，不是「能力大小」——
# 界面上要让用户看懂该选哪个，而不是罗列参数。
#
# install 字段给「环境体检」用：检测到没装时，界面上要能一键拿到它。
#   https  直接下官方预编译包（Windows）
#   page   没有稳定的直接下载地址，给官网让用户自己挑（不骗人）
#   hints  各平台包管理器命令，用户愿意用包管理时更快
CATALOG: List[Dict[str, Any]] = [
    {
        "key": "native",
        "label": "自研多线程",
        "kind": "http",
        "always": True,
        "tagline": "零依赖 · 多线程分片 · 按资源类型定制",
        "detail": "内置引擎，不需要任何外部程序。按识别出的类型决定线程数与分片大小，"
                  "支持断点续传、限速、暂停、下完校验哈希。",
    },
    {
        "key": "aria2",
        "label": "aria2",
        "kind": "http",
        "binary": "aria2c",
        "tagline": "多源 · 支持 BT / 磁力 / ed2k",
        "detail": "P2P 与多源下载的唯一选择（磁力、种子、ed2k 必须用它）。"
                  "也能下普通 HTTP：本版已随包附带 aria2c，装上就能用，无需另装。",
        "install": {
            "https": "https://github.com/aria2/aria2/releases/latest",
            "page": "https://github.com/aria2/aria2/releases",
            "hints": {"win": "scoop install aria2 / winget install aria2.aria2",
                      "mac": "brew install aria2",
                      "linux": "apt install aria2"},
            "note": "解压后把 aria2c.exe 放进程序目录的 bin/，或加进 PATH",
        },
    },
    {
        "key": "curl",
        "label": "curl",
        "kind": "http",
        "binary": "curl",
        "tagline": "兼容性最好 · 单连接",
        "detail": "Windows 10 以上系统自带。遇到自研引擎下不动的服务端时，"
                  "换它往往就过了（HTTP/2、TLS 细节、畸形响应都能吞）。单连接，速度不如多线程。",
        "install": {
            "page": "https://curl.se/windows/",
            "hints": {"win": "Windows 10+ 自带，一般不用装",
                      "mac": "brew install curl",
                      "linux": "apt install curl"},
            "note": "Windows 10 1803 以后自带 curl.exe，通常无需安装",
        },
    },
    {
        "key": "wget",
        "label": "wget",
        "kind": "http",
        "binary": "wget",
        "tagline": "断流续传最稳 · 单连接",
        "detail": "对「服务端中途断流」容忍度极高，会自己重连续传。"
                  "老系统或内网机器上常是唯一可装的选择。",
        "install": {
            "page": "https://eternallybored.org/misc/wget/",
            "hints": {"win": "scoop install wget / winget install GNU.Wget",
                      "mac": "brew install wget",
                      "linux": "apt install wget"},
            "note": "Windows 不自带，从上面的页面下 wget.exe 放进 bin/",
        },
    },
    {
        "key": "yt-dlp",
        "label": "yt-dlp",
        "kind": "video",
        "binary": "yt-dlp",
        "tagline": "视频解析 · 覆盖数千站点",
        "detail": "国内 B站/优酷/腾讯/爱奇艺/芒果，海外 YouTube 等，靠它解析页面取真实流地址。"
                  "它不是可选功能 —— 没有它就只能下直链。",
        "install": {
            "https": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
            "page": "https://github.com/yt-dlp/yt-dlp/releases",
            "hints": {"win": "scoop install yt-dlp / winget install yt-dlp.yt-dlp",
                      "mac": "brew install yt-dlp",
                      "linux": "pip install -U yt-dlp"},
            "note": "便携版已内置 yt-dlp，通常无需单独安装",
        },
    },
    {
        "key": "ffmpeg",
        "label": "ffmpeg",
        "kind": "muxer",
        "binary": "ffmpeg",
        "tagline": "音视频合流 · 格式转换",
        "detail": "视频画质与音轨分离下载时必须靠它合并；转 mp3、嵌字幕也靠它。",
        "install": {
            "page": "https://www.gyan.dev/ffmpeg/builds/",
            "hints": {"win": "scoop install ffmpeg / winget install Gyan.FFmpeg",
                      "mac": "brew install ffmpeg",
                      "linux": "apt install ffmpeg"},
            "note": "下载后把 bin/ffmpeg.exe 放进程序目录的 bin/，或加进 PATH",
        },
    },
    {
        "key": "N_m3u8DL-RE",
        "label": "N_m3u8DL-RE",
        "kind": "hls",
        "binary": "N_m3u8DL-RE",
        "tagline": "专业 HLS/DASH 下载器",
        "detail": "分片加密、多音轨的流比 ffmpeg 更稳更快。可选的加强项，没有也能下（走 ffmpeg）。",
        "install": {
            "https": "https://github.com/nilaoda/N_m3u8DL-RE/releases/latest",
            "page": "https://github.com/nilaoda/N_m3u8DL-RE/releases",
            "hints": {"win": "scoop install n-m3u8dl-re",
                      "mac": "brew install n-m3u8dl-re"},
            "note": "解压后把 N_m3u8DL-RE.exe 放进程序目录的 bin/",
        },
    },
]


def _by_key(key: str) -> Optional[Dict[str, Any]]:
    for e in CATALOG:
        if e["key"] == key:
            return e
    return None


def status(cfg: Dict[str, Any], *, refresh: bool = False) -> Dict[str, Any]:
    """每个引擎现在能不能用。

    两条探测并行跑：外部工具要 spawn 一堆进程问版本，aria2 那边要连
    RPC 且带 1.5 秒超时。它们之间没有依赖，串行等于把两个等待相加 ——
    冷启动时这就是首屏白等的两秒。
    """
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_tl = ex.submit(_tools.probe_all, cfg, refresh=refresh)
        f_be = ex.submit(_backends.describe, cfg, refresh=refresh)
        tl = f_tl.result()
        be = f_be.result()
    out: Dict[str, Any] = {}

    for e in CATALOG:
        k = e["key"]
        if e.get("always"):
            out[k] = {"available": True, "path": "", "version": "内置",
                      "note": "随程序提供，无需安装"}
            continue
        if k == "aria2":
            a = be.get("aria2") or {}
            ok = bool(a.get("rpc_ok") or a.get("binary"))
            out[k] = {
                "available": ok,
                "path": a.get("rpc_url") or a.get("binary") or "",
                "version": a.get("binary_version", "") or "",
                "note": a.get("note", ""),
            }
            continue
        if k == "yt-dlp":
            y = tl.get("yt-dlp") or {}
            out[k] = {"available": y.get("available") == "yes",
                      "path": y.get("path", ""),
                      "version": y.get("version", ""),
                      "note": y.get("source", "")}
            continue
        info = tl.get(k) or {}
        out[k] = {
            "available": info.get("available") == "yes",
            "path": info.get("path", ""),
            "version": info.get("version", ""),
            "note": info.get("source", ""),
        }
    return out


def install_info(key: str, *, platform: Optional[str] = None) -> Dict[str, Any]:
    """某个引擎没装时，怎么装。

    给「环境体检」的下载按钮用。故意不返回「一键静默安装」那种东西 ——
    下载站给的包格式各不相同（zip/tar.xz/msi），静默装会变成不可控的
    提权操作。这里给的是**直达官方下载**与包管理器命令，用户自己点。
    """
    e = _by_key(key)
    if not e:
        return {"ok": False, "error": f"没有这个引擎：{key}"}
    ins = e.get("install") or {}
    if not ins:
        return {"ok": False, "error": f"{e['label']} 是内置引擎，无需安装"}

    import sys as _sys
    if platform is None:
        if _sys.platform.startswith("win"):
            platform = "win"
        elif _sys.platform == "darwin":
            platform = "mac"
        else:
            platform = "linux"

    hints = ins.get("hints") or {}
    return {
        "ok": True,
        "key": key,
        "label": e.get("label") or key,
        # https 是能直接喂给下载器的地址；page 是给人看的页面
        "https": ins.get("https") or "",
        "page": ins.get("page") or "",
        "hint": hints.get(platform) or hints.get("win") or "",
        "all_hints": hints,
        "note": ins.get("note") or "",
        "platform": platform,
    }


def describe(cfg: Dict[str, Any], *, refresh: bool = False) -> Dict[str, Any]:
    """给界面用的完整引擎表（元数据 + 可用性）。"""
    st = status(cfg, refresh=refresh)
    # 哪些能由程序自己下回来（引擎页据此决定显示「下载安装」还是「需手动安装」）。
    # 局部导入：fetch_tools 反过来要用 tools，放在模块顶部容易踩加载顺序。
    try:
        from . import fetch_tools as _fetch
        fetchable = set(_fetch.SOURCES)
    except Exception:
        fetchable = set()
    rows = []
    for e in CATALOG:
        s = st.get(e["key"], {})
        rows.append({**e, **s, "fetchable": e["key"] in fetchable})
    http_avail = [k for k in (cfg.get("engine_order") or VALID_HTTP)
                  if k in VALID_HTTP and st.get(k, {}).get("available")]
    return {
        "engines": rows,
        "http_available": http_avail,
        "order": list(cfg.get("engine_order") or VALID_HTTP),
        "effective_http": recommend_http(cfg, st=st),
        "video_ready": bool(st.get("yt-dlp", {}).get("available")),
        "mux_ready": bool(st.get("ffmpeg", {}).get("available")),
    }


def placeholder(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """探测还没回来时给界面的空壳。

    结构必须和 describe() 逐字段一致 —— 前端按同一套字段渲染，少一个键
    就是一处 undefined。多出来的 `pending` 是给界面看的：显示「正在探测」
    而不是「未安装」，这两句话对用户的意思完全不同。

    唯一可以现在就确定的是 native（内置，永远可用）。
    """
    order = [k for k in (cfg.get("engine_order") or VALID_HTTP)
             if k in VALID_HTTP]
    rows = []
    for e in CATALOG:
        if e.get("always"):
            rows.append({**e, "available": True, "path": "", "version": "内置",
                         "note": "", "fetchable": False, "pending": False})
        else:
            rows.append({**e, "available": False, "path": "", "version": "",
                         "note": "", "fetchable": False, "pending": True})
    return {
        "engines": rows,
        "http_available": [k for k in order if k == "native"],
        "order": order,
        "effective_http": "native",
        "video_ready": False,
        "mux_ready": False,
        "probing": True,
    }


def recommend_http(cfg: Dict[str, Any],
                   st: Optional[Dict[str, Any]] = None) -> str:
    """普通 HTTP 下载该用哪个引擎。"""
    st = st or status(cfg)
    want = (cfg.get("backend") or "auto").lower()
    # 用户明确指定了后端就听他的（auto 除外）
    if want in VALID_HTTP:
        if st.get(want, {}).get("available", want == "native"):
            return want
        if want == "native":
            return "native"
        # 指定的不可用：单选的 curl/wget 也允许硬上（可能 PATH 里有但探不到）
        if want in ("curl", "wget"):
            return want
    order = [k for k in (cfg.get("engine_order") or VALID_HTTP) if k in VALID_HTTP]
    for k in order:
        if st.get(k, {}).get("available"):
            return k
    return "native"


def recommend(url: str, cfg: Dict[str, Any], *,
              kind: str = "", st: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """这个链接该走哪条路。返回 {engine, why, blocked}。"""
    gate = _edition.gate(url, cfg)
    if not gate["allowed"]:
        return {"engine": "", "why": gate["reason"], "blocked": True}

    if kind == "video":
        return {"engine": "yt-dlp", "why": "视频页/流：需要解析出真实媒体地址",
                "blocked": False}

    # 视频站的播放页不能当普通 HTTP 下 —— 那样只会存下来一个 HTML。
    # 判定落在站点维度（见 jiliu/sites.py），不看扩展名。
    site = _sites.match(url)
    if site and (kind or _sites.is_video_page(url)):
        return {"engine": "yt-dlp",
                "why": f"{site['name']}播放页：需要 yt-dlp 解析真实流地址",
                "blocked": False, "site": site["key"]}

    proto = _protocols.detect(url)
    if getattr(proto, "p2p", False):
        return {"engine": "aria2", "why": "P2P（磁力/种子/ed2k）：只有 aria2 能做",
                "blocked": False}

    eng = recommend_http(cfg, st=st)
    label = (_by_key(eng) or {}).get("label", eng)
    return {"engine": eng, "why": f"普通 HTTP：按引擎优先级选中 {label}",
            "blocked": False}
