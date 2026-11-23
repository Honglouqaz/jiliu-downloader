"""视频解析与下载 —— 基于 yt-dlp。

为什么用 yt-dlp 而不是自己写解析：
    自己维护各平台解析规则是纯粹的技术债。B 站改一次播放页结构、
    YouTube 换一次签名算法，你的解析就全废。yt-dlp 是这个领域唯一
    被持续维护的开源方案，覆盖 1800+ 站点，国内外主流平台都有。

这一层额外扛了三件事（都是踩过才加的）：

1）**合集 / 播放列表**
   不自己解析 URL 形态 —— B站多P、UP合集、收藏夹、番剧、YouTube
   playlist、频道，全交给 yt-dlp 判 `_type == 'playlist'`。
   我们只负责展开条目让用户勾选，再用 `--playlist-items` 精确下载。
   这样 yt-dlp 支持多少种合集形态，汲流就支持多少种，不用跟着改。

2）**Cookie 与代理按站点分流**
   都收敛到 cookies.py / proxy.py，这里只做参数拼装。

3）**失败原因翻译**
   `Sign in to confirm you're not a bot` 这种报错，用户看不懂也不知道
   该怎么办。humanize_error() 负责把它翻成「下一步做什么」。

边界（这条要写清楚）：
    只处理公开可得的内容。DRM 加密流（Netflix / Disney+ 这类）不在
    支持范围内，yt-dlp 本身也不做这个。请自行确认内容的使用授权。
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import cookies, naming, proxy, tools

PROG_PREFIX = "JLPROG:"
_NO_WINDOW = 0x08000000

# 流直链判定（只要后缀，不看站点）：分片并发只对这些开
_STREAM_RE = re.compile(r"\.(m3u8|m3u|mpd)$")


class VideoError(Exception):
    pass


# --------------------------------------------------------------------------
# 参数拼装
# --------------------------------------------------------------------------

_JS_CACHE: Dict[str, Any] = {"probed": False, "found": []}


def js_runtimes(refresh: bool = False) -> List[Dict[str, str]]:
    """找本机可用的 JS 运行时。

    为什么需要：YouTube 的 n-sig 签名要用 JS 解，yt-dlp 从 2025 年起
    把这件事交给外部 JS 引擎。默认只启用 deno —— 没装 deno 就解析失败，
    报错还很晦涩（"No supported JavaScript runtime could be found"）。
    主人机器上有 node，所以这里主动去认 node。
    """
    if _JS_CACHE["probed"] and not refresh:
        return _JS_CACHE["found"]
    found: List[Dict[str, str]] = []
    # deno 是 yt-dlp 官方推荐，优先；node 次之
    for name in ("deno", "node", "bun", "qjs"):
        try:
            p = shutil.which(name)
        except Exception:
            p = None
        if not p:
            continue
        ver = ""
        try:
            r = subprocess.run([p, "--version"], capture_output=True,
                               timeout=6, creationflags=_NO_WINDOW)
            ver = ((r.stdout or b"") + (r.stderr or b"")).decode(
                "utf-8", "replace").strip().splitlines()[0][:40]
        except Exception:
            pass
        found.append({"name": name, "path": p, "version": ver})
    _JS_CACHE.update(probed=True, found=found)
    return found


def _js_args(cfg: Dict[str, Any]) -> List[str]:
    mode = str(cfg.get("js_runtimes") or "auto").strip()
    if mode.lower() in ("off", "none", ""):
        return []
    if mode.lower() != "auto":
        # 用户显式指定，支持 "node" 或 "node:C:\path\node.exe"
        return ["--js-runtimes", mode]
    for rt in js_runtimes():
        if rt["name"] == "deno":
            return ["--js-runtimes", "deno"]
        return ["--js-runtimes", f"{rt['name']}:{rt['path']}"]
    return []


def _cmd_prefix(cfg: Dict[str, Any]) -> List[str]:
    """yt-dlp 的可执行前缀（exe 路径，或 [python, -m, yt_dlp]）。"""
    cmd = tools.ytdlp_command(cfg.get("ytdlp_path", ""))
    if not cmd:
        raise VideoError(
            "找不到 yt-dlp。三种装法任选：\n"
            "    独立可执行文件：scoop install yt-dlp（或放到项目的 bin/ 目录）\n"
            "    Python 模块   ：pip install --target vendor yt-dlp\n"
            "    已在 vendor/ 内置：直接可用，无需安装"
        )
    return list(cmd)


def _base_args(cfg: Dict[str, Any], url: str = "",
               headers: Optional[Dict[str, str]] = None) -> List[str]:
    """只要选项部分（不含可执行前缀）—— 交给 tools.ytdlp_run 统一执行。

    url 要传进来：cookie 和代理都得按站点分流，不能一视同仁。

    headers 是**任务级**请求头，来自三处：网页解析（pagesniff）、
    浏览器扩展（嗅探到的媒体连同 cookie/referer 一起发来）、插件。
    CDN 防盗链只查三样：Referer / UA / Cookie，少一样就是 403 ——
    而用户看到的只是「地址没错却下不动」，所以必须原样透传给 yt-dlp，
    连分片请求和 m3u8 的 key 请求也要带上（--add-header 是全局生效的）。
    """
    args = ["--no-warnings", "--no-color", "--ignore-config"]
    # 保留调用方给的原始大小写（HTTP 头不区分大小写，但透传原样更省事，
    # 有些 WAF 会拿头名做白名单匹配）；取值时一律按小写找。
    hdrs = {str(k): str(v) for k, v in (headers or {}).items() if v}

    def take(name: str) -> str:
        for k in list(hdrs):
            if k.lower() == name:
                return hdrs.pop(k)
        return ""

    # cookie：按域名自动挑该站点的文件（见 cookies.ytdlp_args）
    args += cookies.ytdlp_args(cfg, url)

    # 代理：必须显式传。不传的话 yt-dlp 会去读环境变量里那个
    # 可能已经死掉的代理，然后所有请求静默失败。
    args += proxy.ytdlp_args(cfg, url)

    # JS 运行时：YouTube 的 n-sig 要用它解
    args += _js_args(cfg)

    # 注意这里用的是 video_user_agent，不是 user_agent。
    # 后者带 "Jiliu/1.0" 后缀，拿去请求 YouTube 等于自报家门是爬虫。
    # 任务级 UA 优先：扩展发来的就是真实浏览器的 UA，冒充不了。
    ua = take("user-agent") or str(cfg.get("video_user_agent") or "").strip()
    if ua:
        args += ["--user-agent", ua]

    ref = take("referer") or str(cfg.get("referer") or "").strip()
    if ref:
        args += ["--referer", ref]

    # Cookie 用 --add-header 而不是写临时 cookie 文件：
    # 扩展给的是「当前会话的 cookie 串」，落盘就多了个要清理的敏感文件。
    for k in ("cookie", "origin"):
        v = take(k)
        if v:
            args += ["--add-header", f"{k.title()}: {v}"]
    for k, v in hdrs.items():
        args += ["--add-header", f"{k}: {v}"]

    ffdir = tools.which_ffmpeg_dir()
    if ffdir:
        args += ["--ffmpeg-location", ffdir]

    args += ["--socket-timeout", str(int(cfg.get("timeout", 30)))]
    return args


def available(cfg: Dict[str, Any]) -> Dict[str, Any]:
    yi = tools.ytdlp_info(cfg.get("ytdlp_path", ""))
    js = js_runtimes()
    return {
        "available": yi["available"],
        "mode": yi["mode"],
        "path": yi["path"],
        "version": yi["version"],
        "source": tools.ytdlp_source(yi),
        "ffmpeg_dir": tools.which_ffmpeg_dir(),
        "js_runtimes": js,
        "js_ok": bool(js),
        "cookies": cookies.describe(cfg),
        "note": "yt-dlp 是独立开源项目，负责各平台的解析规则维护",
    }


# --------------------------------------------------------------------------
# 报错翻译
# --------------------------------------------------------------------------

def humanize_error(err: str, cfg: Optional[Dict[str, Any]] = None,
                   url: str = "") -> Dict[str, Any]:
    """把 yt-dlp 的英文报错翻成「什么原因 + 下一步做什么」。

    这是这个模块最值钱的部分之一。用户拿到一屏英文，
    既不知道是网络问题还是缺 cookie，也不知道该点哪里。
    """
    e = (err or "").strip()
    low = e.lower()
    tail = e.splitlines()[-1][:300] if e else ""

    def hit(*keys: str) -> bool:
        return any(k in low for k in keys)

    # ---- 登录态 ----
    # 抖音的 "Fresh cookies (not necessarily logged in) are needed"：
    # 2026-09 实测它要的不是登录态，是**新鲜的游客 cookie**（访问一次
    # 抖音页面就有）。单独一个分支，提示和 YouTube 的风控不一样。
    if hit("fresh cookies"):
        return {
            "kind": "cookie",
            "title": "抖音要一份新鲜的 Cookie（不用登录账号）",
            "detail": "yt-dlp 解析抖音要求带一份**新鲜的游客 Cookie** —— "
                      "浏览器正常打开过 www.douyin.com 就会有。过期或缺失都会报这个。",
            "fixes": [
                "账号登录 → 扫码登录抖音（推荐，之后全自动）",
                "或在「环境体检 → Cookie」粘贴导入 douyin.com 的 Cookie",
                "导入后重新点下载即可，Cookie 按站点自动带上",
            ],
            "raw": tail,
        }
    if hit("sign in to confirm", "not a bot", "confirm you're not a bot",
           "confirm you’re not a bot"):        return {
            "kind": "cookie",
            "title": "YouTube 要求出示登录态（风控，不是程序坏了）",
            "detail": "Google 对匿名请求做了风控。从数据中心或共享出口 IP 过来的"
                      "访问会被要求登录验证，跟 yt-dlp 版本无关。",
            "fixes": [
                "用浏览器扩展「把 cookie 交给汲流」——用你现在浏览器里已登录的状态，一步到位",
                "或在设置里选「从浏览器读取 cookie」（Firefox 成功率最高）",
                "或手动导出 cookies.txt 后导入",
            ],
            "raw": tail,
        }
    if hit("private video", "login required", "this video is only available",
           "members-only", "需要登录"):
        return {
            "kind": "cookie",
            "title": "这个视频需要登录才能看",
            "detail": "私有视频、会员专享或需要关注的内容，必须带上你账号的 cookie。",
            "fixes": ["用扩展或浏览器导出 cookie 后重试"],
            "raw": tail,
        }
    if hit("http error 403", "forbidden") and url:
        return {
            "kind": "cookie",
            "title": "服务器拒绝（403）——多半是 cookie 过期了",
            "detail": "凭证失效，或该站点启用了新的风控（UA / Referer 也可能被查）。",
            "fixes": ["重新导出一次 cookie（登录态会过期，YouTube 大约几个月）"],
            "raw": tail,
        }

    # ---- JS 运行时 ----
    if hit("javascript runtime", "no supported javascript", "nsig", "n-sig",
           "player js", "sig extraction"):
        return {
            "kind": "js",
            "title": "缺少 JS 运行时，YouTube 签名解不开",
            "detail": "新版 yt-dlp 要用外部 JS 引擎（deno 或 node）解 YouTube 的 "
                      "n-sig 签名。系统里一个都没有。",
            "fixes": [
                "装 Node.js（最省事，本机可能已经有了）",
                "或装 deno：scoop install deno",
                "装完在设置里把「JS 运行时」保持 auto，汲流会自动认出来",
            ],
            "raw": tail,
        }

    # ---- 代理 / 网络 ----
    if hit("connection refused", "10061"):
        p = (cfg or {}).get("proxy_mode", "auto")
        return {
            "kind": "proxy",
            "title": "连不上——像是代理地址不对或代理没开",
            "detail": f"当前代理模式：{p}。ConnectionRefused 通常是指定了一个"
                      f"没人监听的端口。",
            "fixes": [
                "确认代理客户端在跑，并且端口对得上",
                "或把代理模式改成「关闭」，直连试试",
                "设置 → 网络 → 有「测试」按钮可以一键验活",
            ],
            "raw": tail,
        }
    if hit("timed out", "timeout", "read operation timed out"):
        return {
            "kind": "network",
            "title": "请求超时",
            "detail": "目标站点连不上。国内站直连，国外站要代理 —— 两头都不通就会出现这个。",
            "fixes": [
                "国外站点（YouTube 等）需要在设置里配好代理并确认可达",
                "把超时时间调大一点（设置 → 超时）",
                "换个网络环境再试",
            ],
            "raw": tail,
        }
    if hit("unable to download webpage", "failed to resolve", "getaddrinfo"):
        return {
            "kind": "network",
            "title": "域名解析或连接失败",
            "detail": "网络层就没通。",
            "fixes": ["检查代理设置", "确认该站点在你的网络环境下可访问"],
            "raw": tail,
        }

    # ---- 地区限制 ----
    if hit("not available in your country", "geo", "region", "地域"):
        return {
            "kind": "region",
            "title": "该内容在当前地区不可用",
            "detail": "站点按 IP 做了地区限制。",
            "fixes": ["换到该内容可用的地区节点"],
            "raw": tail,
        }

    # ---- yt-dlp 版本 ----
    if hit("unable to extract", "unable to parse", "please report this issue",
           "no video formats found", "signature"):
        return {
            "kind": "version",
            "title": "解析规则失效——大概率是 yt-dlp 版本太旧",
            "detail": "站点改了页面结构或签名算法，旧版 yt-dlp 跟不上。"
                      "这个领域两天一小改，版本差几个月就会大面积失败。",
            "fixes": [
                "升级 yt-dlp（汲流会自动挑版本最新的那一份）",
                "scoop update yt-dlp  /  pip install -U yt-dlp",
                "设置 → 工具 → 点「重新扫描」验证版本",
            ],
            "raw": tail,
        }

    if hit("unsupported url"):
        return {
            "kind": "unsupported",
            "title": "这个地址 yt-dlp 不认识",
            "detail": "可能是链接不完整、不是视频页，或者该站点不在支持范围。",
            "fixes": ["确认复制的是视频页面地址，而不是分享短链或首页"],
            "raw": tail,
        }

    if hit("drm"):
        return {
            "kind": "drm",
            "title": "这是 DRM 加密内容",
            "detail": "腾讯视频 / 爱奇艺 / 芒果TV 的会员与部分免费内容，"
                      " Netflix / Disney+ 这类海外站，都用 DRM（Widevine 等）"
                      "加密：正片分片是密文，拿到地址也解不开，浏览器播放"
                      "本身就要向版权方要密钥。汲流不做第三方「VIP 破解解析」"
                      "—— 那是盗链服务，侵权且随时失效。",
            "fixes": [
                "免费 / 试看内容仍可解析：换一集非 VIP 的试试",
                "带自己的登录态（Cookie）能拿到你能看的最高清晰度",
                "会员正片是硬边界，没有可行的办法",
            ],
            "raw": tail,
        }

    _low = tail.lower()
    if ("iqiyi" in _low or "iq.com" in _low) and (
            hit("can't find any video") or hit("unable to extract")):
        return {
            "kind": "extractor-broken",
            "title": "爱奇艺解析器当前失效",
            "detail": "爱奇艺改版后 yt-dlp 上游还没跟上（实测多个有效播放页"
                      "均报 Can't find any video，与链接、Cookie 无关）。",
            "fixes": [
                "等 yt-dlp 更新后重试（设置里可以检查版本）",
                "其他平台不受影响：B站/优酷/微博/AcFun 实测可用",
            ],
            "raw": tail,
        }

    return {
        "kind": "other",
        "title": "解析失败",
        "detail": tail or "yt-dlp 没有给出更多信息。",
        "fixes": ["点「诊断」看逐项检查结果"],
        "raw": tail,
    }


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

def _run_json(args: List[str], cfg: Dict[str, Any], timeout: int,
              url: str = "") -> Dict[str, Any]:
    try:
        out = tools.ytdlp_run(args, cfg.get("ytdlp_path", ""), timeout=timeout)
    except FileNotFoundError as exc:
        raise VideoError(f"无法执行 yt-dlp：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"解析超时（{timeout} 秒）。慢站点或网络不通都会这样。") from exc

    if out.returncode != 0:
        err = (out.stderr or b"").decode("utf-8", "replace").strip()
        # 403 的修复建议分支要求知道是哪个站点（YouTube 看 cookie，
        # B 站看风控），解析阶段也必须把 url 传进来 —— 以前没传，
        # 403 永远落到兜底文案，用户拿不到"重新导出 cookie"这一步。
        info = humanize_error(err, cfg, url)
        raise VideoError(f"{info['title']}\n  {info['detail']}")
    try:
        return json.loads((out.stdout or b"{}").decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise VideoError("yt-dlp 返回的不是合法 JSON") from exc


def probe(url: str, cfg: Dict[str, Any], timeout: int = 60,
          *, playlist: bool = False, limit: int = 0,
          headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """拿到视频元信息与可选格式列表。

    playlist=False（默认）时只取当前这一集 —— 哪怕 URL 是 B站多P，
    也只拿 ?p= 指的那一集，不会一下子展开成几十集。
    """
    args = _base_args(cfg, url, headers) + ["-J"]
    if playlist:
        args += ["--yes-playlist"]
        if limit > 0:
            args += ["--playlist-end", str(int(limit))]
    else:
        args += ["--no-playlist"]
    args.append(url)
    return normalize_info(_run_json(args, cfg, timeout, url))


_P_RE = re.compile(r"[?&]p=(\d+)")


def _entry_title(e: Dict[str, Any], url: str, idx: int) -> str:
    """条目标题。

    B站的 --flat-playlist 只回一个 url，没有标题 —— 直接取 title 会是
    一列空白。这里退化成「P3 / 第 3 集」这种占位；真正下载时
    yt-dlp 会自己解析出正确标题来命名文件，所以占位不影响产物。
    """
    t = (e.get("title") or "").strip()
    if t:
        return t
    m = _P_RE.search(url or "")
    if m:
        return f"P{m.group(1)}"
    return f"第 {idx} 集"


def normalize_info(info: Dict[str, Any]) -> Dict[str, Any]:
    """把 yt-dlp 的原始结构整理成界面好用的形态。"""
    if info.get("_type") == "playlist" or (
            isinstance(info.get("entries"), list) and info.get("_type") != "video"):
        return normalize_playlist(info)

    fmts = []
    for f in info.get("formats") or []:
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        if vcodec == "none" and acodec == "none":
            continue
        kind = "video" if vcodec != "none" else "audio"
        fmts.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "kind": kind,
            "resolution": f.get("resolution") or ("audio" if kind == "audio" else ""),
            "height": f.get("height"),
            "fps": f.get("fps"),
            "abr": f.get("abr"),
            "vbr": f.get("vbr"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "note": f.get("format_note") or "",
            "has_audio": acodec != "none",
            "has_video": vcodec != "none",
            "proto": f.get("protocol") or "",
        })
    fmts.sort(key=lambda x: (x["kind"] != "video", -(x["height"] or 0), -(x["filesize"] or 0)))

    # 直链媒体（http.../xxx.mp4 这类）yt-dlp 不会给 formats 数组，
    # 只在顶层给 format_id / url / ext。这里补一个合成条目，
    # 否则用户粘一个直链进来，界面会是空的。
    if not fmts and info.get("format_id") and info.get("url"):
        fmts.append({
            "format_id": info.get("format_id"),
            "ext": info.get("ext"),
            "kind": "video",
            "resolution": info.get("resolution") or "",
            "height": info.get("height"),
            "fps": info.get("fps"),
            "abr": info.get("abr"),
            "vbr": info.get("vbr"),
            "filesize": info.get("filesize") or info.get("filesize_approx"),
            "note": info.get("format") or "直链单文件",
            "has_audio": True,
            "has_video": True,
            "proto": info.get("protocol") or "",
            "direct": True,
        })

    return {
        "is_playlist": False,
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "duration_text": _fmt_dur(info.get("duration")),
        "thumbnail": _thumb(info),
        "webpage_url": info.get("webpage_url"),
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        "formats": fmts,
        "best_hint": "bv*+ba/b",
        "subtitles": list((info.get("subtitles") or {}).keys())[:20],
        "automatic_captions": list((info.get("automatic_captions") or {}).keys())[:20],
        "has_drm": bool(info.get("_has_drm")),
        "is_live": bool(info.get("is_live")),
        "filesize_approx": info.get("filesize_approx"),
    }


def normalize_playlist(info: Dict[str, Any]) -> Dict[str, Any]:
    """整理播放列表 / 合集。"""
    entries: List[Dict[str, Any]] = []
    for i, e in enumerate(info.get("entries") or [], 1):
        if not isinstance(e, dict):
            continue
        u = e.get("url") or e.get("webpage_url") or ""
        entries.append({
            "index": i,
            "id": e.get("id") or "",
            "title": _entry_title(e, u, i),
            "url": u,
            "duration": e.get("duration"),
            "duration_text": _fmt_dur(e.get("duration")),
            "thumbnail": _thumb(e),
            "uploader": e.get("uploader") or "",
            "has_detail": bool(e.get("title")),
        })
    return {
        "is_playlist": True,
        "kind": "playlist",
        "id": info.get("id") or "",
        "title": info.get("title") or info.get("playlist_title") or "播放列表",
        "uploader": info.get("uploader") or info.get("playlist_uploader")
                    or info.get("channel") or "",
        "count": len(entries),
        "declared_count": info.get("playlist_count") or len(entries),
        "entries": entries,
        "thumbnail": _thumb(info),
        "webpage_url": info.get("webpage_url"),
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        # flat 模式下 B站不给标题，界面要用「补齐标题」的异步流程
        "detail_loaded": all(e["has_detail"] for e in entries) if entries else False,
        "truncated": bool(info.get("_truncated")),
    }


def probe_playlist(url: str, cfg: Dict[str, Any], timeout: int = 180,
                   *, fast: bool = True, limit: int = 0) -> Dict[str, Any]:
    """专门探合集。

    fast=True 用 --flat-playlist：只列条目，2~5 秒就回，但 B站 拿不到标题
    （只给 url）。所以默认走 fast 先把条目数和进度反馈出来，界面再异步
    拉一次 fast=False 把标题补齐 —— 等完整解析 18 集要 30 秒，干等
    会让人以为卡死了。
    """
    lim = int(limit or cfg.get("playlist_limit") or 200)
    args = _base_args(cfg, url) + ["-J", "--yes-playlist"]
    if fast:
        args.append("--flat-playlist")
    if lim > 0:
        args += ["--playlist-end", str(lim)]
    args.append(url)
    info = _run_json(args, cfg, timeout, url)
    norm = normalize_info(info)
    norm["fast"] = bool(fast)
    if not norm.get("is_playlist"):
        # 有些站点的单视频在 --yes-playlist 下也返回单个条目，
        # 这种就当普通视频处理，别让界面以为是合集
        norm["is_playlist"] = False
        norm["kind"] = "video"
    return norm


def inspect(url: str, cfg: Dict[str, Any], timeout: int = 180) -> Dict[str, Any]:
    """一步到位：这到底是单个视频还是合集？

    先快探（flat）—— 秒回，能立刻告诉用户「这是 18 集」。
    只有 1 集才继续拉完整格式列表（那步慢）。
    """
    from . import resource
    guessed = any(h in (url or "") for h in resource.PLAYLIST_HINTS)

    pl = probe_playlist(url, cfg, timeout=timeout, fast=True)
    count = int(pl.get("count") or 0)

    if count > 1:
        return {"kind": "playlist", "playlist": pl,
                "guessed_from_url": guessed, "value": None}

    # 单集：拉完整信息（含格式）
    try:
        full = probe(url, cfg, timeout=timeout)
    except VideoError as exc:
        raise VideoError(str(exc)) from exc
    if full.get("is_playlist"):
        # 兜底：平时走 flat 拿不到详情，这里反而拿到了
        return {"kind": "playlist", "playlist": full,
                "guessed_from_url": guessed, "value": None}
    return {"kind": "video", "value": full, "playlist": None,
            "guessed_from_url": guessed}


def _thumb(o: Dict[str, Any]) -> str:
    t = o.get("thumbnail")
    if t:
        return t
    thumbs = o.get("thumbnails") or []
    if thumbs and isinstance(thumbs, list):
        return thumbs[-1].get("url", "") or ""
    return ""


def _fmt_dur(sec: Any) -> str:
    try:
        s = int(sec)
    except Exception:
        return "-"
    if s < 0:
        return "-"
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------

_PROG_RE = re.compile(
    r"^\[download\]\s+(?P<pct>\d+(?:\.\d+)?)%\s+of\s+(?P<size>\S+)(?:\s+at\s+(?P<speed>\S+))?"
    r"(?:\s+ETA\s+(?P<eta>\S+))?"
)
# 从 yt-dlp 输出里定位最终产物。
# 不同阶段用的前缀不一样，所以分几条规则按顺序匹配，取最后命中的那个。
# 字幕那一行是纯字幕模式找产物的关键：--skip-download --write-subs 时
# yt-dlp 打的是 `[info] Writing video subtitles to: xxx.vtt`。
_DEST_PATTERNS = [
    re.compile(r'^\[Merger\]\s+Merging formats into\s+"(.+?)"\s*$'),
    re.compile(r"^\[[\w]+\]\s+Destination:\s*(.+?)\s*$"),
    re.compile(r"^\[download\]\s+(.+?)\s+has already been downloaded"),
    # 音频提取后会有这一步
    re.compile(r'^\[ExtractAudio\]\s+Destination:\s*(.+?)\s*$'),
    re.compile(r"^\[info\]\s+Writing video subtitles to:\s*(.+?)\s*$"),
]


def _extract_dest(line: str) -> str:
    for pat in _DEST_PATTERNS:
        m = pat.match(line)
        if m:
            return m.group(1).strip().strip('"')
    return ""


_TMPL_RE = re.compile(
    r"JLPROG:(?P<done>[^|]*)\|(?P<total>[^|]*)\|(?P<speed>[^|]*)\|(?P<eta>[^|]*)\|(?P<pct>[^|]*)"
)


def parse_line(line: str) -> Optional[Dict[str, Any]]:
    """把 yt-dlp 的一行输出解析成进度事件。"""
    if line.startswith(PROG_PREFIX):
        m = _TMPL_RE.match(line)
        if m:
            def num(x: str) -> Optional[float]:
                try:
                    return float(x)
                except Exception:
                    return None
            return {
                "done": num(m.group("done")),
                "total": num(m.group("total")),
                "speed": num(m.group("speed")),
                "eta": num(m.group("eta")),
                "percent_text": m.group("pct").strip(),
                "raw": line,
            }
    m = _PROG_RE.match(line)
    if m:
        return {
            "percent": float(m.group("pct")),
            "size_text": m.group("size"),
            "speed_text": m.group("speed") or "",
            "eta_text": m.group("eta") or "",
            "raw": line,
        }
    return None


def playlist_item_spec(indexes: List[int]) -> str:
    """把 [1,2,3,7,8,9,15] 压成 yt-dlp 认的 "1-3,7-9,15"。

    几十集的合集要是逐条写 "1,2,3,..."，命令行会长到离谱，
    而且 yt-dlp 解析起来也慢。
    """
    nums = sorted({int(i) for i in indexes if int(i) > 0})
    if not nums:
        return ""
    parts: List[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def _out_template(outdir: pathlib.Path, cfg: Dict[str, Any],
                  is_playlist: bool) -> str:
    """输出文件名模板。

    用设置里的命名模板（`%title% [%id%]` 这类变量，见 jiliu/naming.py），
    翻译成 yt-dlp 认的 outtmpl。用户没改过的话，默认值与旧行为完全一致。

    合集默认建一层子目录（playlist_folder），否则几十个视频会和
    你下载目录里原有的东西糊在一起，事后没法收拾；
    并且默认在文件名前加序号 —— 站点给的标题多数不带序号，
    不加的话资源管理器里排序是乱的，看不出第几集。
    """
    file_pat = str(cfg.get("name_pattern") or naming.DEFAULT_FILE_PATTERN)
    dir_pat = str(cfg.get("dir_pattern") or "")
    use_pl_folder = bool(cfg.get("playlist_folder", True))
    if is_playlist and naming.VAR_INDEX not in file_pat:
        file_pat = naming.DEFAULT_PLAYLIST_PREFIX + file_pat
    rel = naming.build_output(dir_pat, file_pat, is_playlist=is_playlist,
                              use_playlist_folder=use_pl_folder)
    return str(outdir / rel)


def _fragment_args(url: str, cfg: Dict[str, Any]) -> List[str]:
    """流直链的分片并发参数。非流直链返回空。

    单独拎出来是为了能单测 —— 这段逻辑的参数拼法一改就影响所有 HLS 下载，
    但它在 download() 里，跑一次真下载才能验证太贵。
    """
    if not _STREAM_RE.search((url or "").split("?")[0].split("#")[0].lower()):
        return []
    try:
        n = int(cfg.get("stream_concurrency") or 8)
    except (TypeError, ValueError):
        n = 8
    if n <= 1:
        return []
    return ["--concurrent-fragments", str(min(n, 16))]


def download(
    url: str,
    outdir: pathlib.Path,
    cfg: Dict[str, Any],
    *,
    fmt: str = "bv*+ba/b",
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    extra_args: Optional[List[str]] = None,
    playlist: bool = False,
    items: str = "",
    group_title: str = "",
    audio_only: bool = False,
    subs_only: bool = False,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """调 yt-dlp 下载。含音视频合并、进度解析、可取消。

    items: 形如 "1-3,7"，只下合集里指定的几集（yt-dlp 的 --playlist-items）。
    subs_only: 纯字幕模式 —— 只下字幕（转 SRT），视频一个字节都不碰。
    headers: 任务级请求头（Referer/UA/Cookie），防盗链 CDN 全靠它。
    """
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    do_audio = bool(audio_only or cfg.get("audio_only"))
    do_subs_only = bool(subs_only)
    args = _cmd_prefix(cfg) + _base_args(cfg, url, headers) + [
        "-o", _out_template(outdir, cfg, playlist),
        "--newline",
        "--no-color",
    ]

    if do_subs_only:
        # 纯字幕模式：--skip-download 让 yt-dlp 完全不碰视频流，
        # 只把字幕轨道写出来并转成 SRT。
        # --write-auto-subs 必须带上：YouTube 的大多数字幕是自动生成的，
        # 只开 --write-subs 会以「没有字幕」收场，用户会以为是坏了。
        args += [
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", str(cfg.get("sub_langs") or "zh-Hans,zh-CN,en"),
            "--convert-subs", "srt",
        ]
    else:
        args += [
            "-f", fmt or cfg.get("video_format", "bv*+ba/b"),
            "--progress",
            "--progress-template",
            f"download:{PROG_PREFIX}%(progress.downloaded_bytes)s|"
            "%(progress.total_bytes_estimate)s|%(progress.speed)s|"
            "%(progress.eta)s|%(progress._percent_str)s",
        ]

        if do_audio:
            args += ["-x", "--audio-format", str(cfg.get("audio_format") or "mp3")]
        else:
            args += ["--merge-output-format", str(cfg.get("video_container") or "mp4")]

    if playlist:
        args.append("--yes-playlist")
        if items:
            args += ["--playlist-items", items]
        if cfg.get("playlist_reverse"):
            args.append("--playlist-reverse")
    else:
        args.append("--no-playlist")

    # 字幕（纯字幕模式上面已经拼过完整参数，这里只管「顺带字幕」）
    if cfg.get("write_subs") and not do_subs_only:
        args.append("--write-subs")
        langs = str(cfg.get("sub_langs") or "").strip()
        if langs:
            args += ["--sub-langs", langs]
        if cfg.get("embed_subs") and not do_audio:
            args.append("--embed-subs")

    if cfg.get("speed_limit"):
        args += ["--limit-rate", str(int(cfg["speed_limit"]))]

    # HLS/DASH 直链的分片并发。
    #
    # 一集 45 分钟的片子是 400~900 个 ts/m4s 分片，串行下只能跑满一条连接，
    # 而单连接在多数 CDN 上被限到 2~4MB/s。IDM 的「多线程」在家里就是把
    # 分片并发拉起来。这里只对**明确的流直链**开（页面解析出来的视频页
    # 不开：普通站点并发高了会被风控，收益也不在分片上）。
    if not do_subs_only:
        args += _fragment_args(url, cfg)

    if extra_args:
        args += list(extra_args)
    args.append(url)

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            env=tools.tool_env(),
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        raise VideoError(f"启动 yt-dlp 失败: {exc}") from exc
    tools.track_proc(proc)      # 主程序退出时统一清杀，不留孤儿

    last: Dict[str, Any] = {}
    last_emit = 0.0
    lines_tail: List[str] = []
    produced: List[str] = []

    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\r\n")
            if not line:
                continue
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                break

            ev = parse_line(line)
            if ev:
                last = ev
                now = time.monotonic()
                if progress_cb and now - last_emit > 0.25:
                    last_emit = now
                    snap = _yt_snapshot(url, outdir, ev, started)
                    # 合集下载时把合集名和进度带出去，界面好显示
                    # 「第 3/18 集」
                    if group_title:
                        snap["group_title"] = group_title
                    try:
                        progress_cb(snap)
                    except Exception:
                        pass
            else:
                lines_tail.append(line)
                if len(lines_tail) > 60:
                    lines_tail.pop(0)
                dest = _extract_dest(line)
                if dest:
                    produced.append(dest)
                if log_cb:
                    try:
                        log_cb(line)
                    except Exception:
                        pass
    finally:
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        tools.untrack_proc(proc)

    elapsed = time.monotonic() - started
    cancelled = bool(cancel_event and cancel_event.is_set())
    ok = (not cancelled) and proc.returncode == 0

    # 纯字幕模式的兜底：不同版本 yt-dlp 对字幕落盘打的行不一样，
    # 行解析没抓到就直接看目录里有没有这轮新生成的字幕文件。
    # 只认 mtime 晚于起跑的，不会把用户早先下的旧字幕算进这次产物。
    if do_subs_only and ok and not produced:
        for f in outdir.iterdir():
            try:
                if (f.suffix.lower() in (".srt", ".vtt", ".ass", ".ssa")
                        and f.is_file() and f.stat().st_mtime >= started - 1):
                    produced.append(str(f))
            except OSError:
                continue
        produced.sort()

    err_text = "" if ok else (_last_error(lines_tail) or f"yt-dlp 退出码 {proc.returncode}")
    out: Dict[str, Any] = {
        "ok": ok,
        "cancelled": cancelled,
        "returncode": proc.returncode,
        "seconds": elapsed,
        "outdir": str(outdir),
        "path": produced[-1] if produced else "",
        "produced": produced,
        "error": None if ok else err_text,
        "runner": "yt-dlp",
    }
    if not ok and err_text:
        out["hint"] = humanize_error(err_text, cfg, url)
    return out


def _last_error(lines: List[str]) -> str:
    for line in reversed(lines):
        if "ERROR" in line:
            return line.strip()
    return lines[-1].strip() if lines else ""


def _yt_snapshot(url: str, outdir: pathlib.Path, ev: Dict[str, Any], started: float) -> Dict[str, Any]:
    done = ev.get("done") or 0
    total = ev.get("total") or 0
    speed = ev.get("speed") or 0
    return {
        "url": url,
        "dest": str(outdir),
        "filename": "",
        "size": int(total) if total else None,
        "done": int(done) if done else 0,
        "percent": round(done * 100.0 / total, 2) if total else _pct_from_text(ev.get("percent_text")),
        "speed": float(speed or 0),
        "eta": ev.get("eta"),
        "elapsed": time.monotonic() - started,
        "backend": "yt-dlp",
        "phase": "downloading",
    }


def _pct_from_text(t: Any) -> float:
    if not t:
        return 0.0
    try:
        return float(str(t).strip().rstrip("%"))
    except Exception:
        return 0.0


# --------------------------------------------------------------------------
# 诊断
# --------------------------------------------------------------------------

def diagnose(url: str, cfg: Dict[str, Any], timeout: int = 90) -> Dict[str, Any]:
    """逐项体检，最后给出「结论 + 该做什么」。

    比只丢一句 ERROR 有用得多：用户能看出到底是网络、代理、cookie
    还是版本的问题，也能知道点哪里修。
    """
    steps: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, hint: str = "") -> None:
        steps.append({"name": name, "ok": bool(ok), "detail": detail, "hint": hint})

    # 1. yt-dlp
    yi = tools.ytdlp_info(cfg.get("ytdlp_path", ""))
    add("yt-dlp", yi["available"],
        f"{yi['version'] or '未找到'}（{yi['mode']}，{yi['source']}）",
        "" if yi["available"] else "装一个：scoop install yt-dlp 或放进 bin/ 目录")

    # 2. ffmpeg
    ff = tools.which_ffmpeg_dir()
    add("ffmpeg（合并音视频要用）", bool(ff), ff or "未找到",
        "" if ff else "没有它就只能下到分离的音轨和视频轨")

    # 3. JS 运行时
    js = js_runtimes(refresh=True)
    add("JS 运行时（YouTube 签名要用）", bool(js),
        "、".join(f"{j['name']} {j['version']}" for j in js) or "一个都没有",
        "" if js else "装 Node.js 或 deno，YouTube 才能解开 n-sig 签名")

    # 4. cookie
    cargs = cookies.ytdlp_args(cfg, url)
    if cargs:
        f = cargs[-1]
        ck = cookies.validate(
            pathlib.Path(f).read_text(encoding="utf-8", errors="replace")
        ) if pathlib.Path(f).is_file() else {"count": 0, "domains": [], "expired": 0}
        add("Cookie", ck["count"] > 0,
            f"{pathlib.Path(f).name}，{ck['count']} 条"
            + (f"，其中 {ck['expired']} 条已过期" if ck.get("expired") else ""),
            "" if ck["count"] else "这个文件是空的")
    else:
        add("Cookie", True, "未配置（YouTube 这类站点需要）",
            "如果解析报「not a bot」，就来配一个")

    # 5. 代理
    pinfo = proxy.select_info(cfg, url)
    if proxy.is_domestic(url):
        add("代理", True, f"国内站点直连（{pinfo.source}）")
    else:
        add("代理", pinfo.alive, f"{pinfo.url or '不走代理'}（{pinfo.source}）",
            "" if pinfo.alive else "国外站点需要代理，海外内容才连得上")

    # 6. 实际解析
    t0 = time.monotonic()
    try:
        info = probe(url, cfg, timeout=timeout)
        dt = time.monotonic() - t0
        title = info.get("title") or ""
        add("实际解析", True,
            f"{dt:.1f} 秒，{(title or '(无标题)')[:48]}"
            f"，{len(info.get('formats') or [])} 个格式")
        verdict = "一切正常，可以下载"
        fixes: List[str] = []
    except VideoError as exc:
        dt = time.monotonic() - t0
        raw = str(exc)
        hum = humanize_error(raw, cfg, url)
        add("实际解析", False, f"{dt:.1f} 秒后失败")
        verdict = hum["title"]
        fixes = hum["fixes"]
        steps.append({"name": "详细原因", "ok": False,
                      "detail": hum["detail"], "hint": ""})

    ok_n = sum(1 for s in steps if s["ok"] and s["name"] != "Cookie")
    return {
        "url": url,
        "steps": steps,
        "verdict": verdict,
        "fixes": fixes,
        "ok_count": ok_n,
        "total": len(steps),
    }
