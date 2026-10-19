"""多平台登录态。

B站扫码登录（bili.py）做好之后主人问：别的平台能不能也这样？

实话是：**各家门槛参差不齐**，不能都做成一个模子：

  - **B站**：官方有扫码 web 接口，不要求签名，纯 HTTP 就能走通。
    完整实现（直接委托给 bili 模块）。
  - **抖音**：也有扫码接口（passport.douyin.com），实测不强制签名就能
    换到登录 cookie。但抖音风控收紧的速度是出了名的 —— 哪天开始要
    msToken / verify_fp 校验，这套就会失效。所以：接口实现照做，
    **任何一步失败都如实报错并自动降级到 Cookie 导入引导**，绝不假装能用。
  - **快手 / 小红书 / 腾讯视频 / 优酷 / 爱奇艺 / 芒果TV / 微博 / 西瓜**：
    扫码接口要么不公开，要么要签名（小红书的 x-s、快手的 client_key）。
    逆向这些既不稳（站点一改版就死）也不合适（违反服务条款）。
    所以只提供 **Cookie 导入引导**：把获取步骤写给用户看，
    cookie 存到 `cookies/<域名>.txt`，yt-dlp 下载时自动带上 ——
    与 B站扫码登录落盘的是同一套文件，下载那条线天然打通。

登录态文件与 bili 模块共用 `cookies/` 目录，命名规则也一致
（`save_text(text, "douyin.com")` → `douyin.com.txt`）。
"""
from __future__ import annotations

import http.cookiejar
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import cookies as _cookies, proxy as _proxy, qr as _qr

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 抖音扫码轮询状态码（data.status）
_DY_WAIT_SCAN = 1
_DY_SCANNED = 2
_DY_CONFIRMED = 3
_DY_EXPIRED = 5

# 各平台怎么把 Cookie 导进来。写步骤而不是写模糊的「去拿 cookie」，
# 用户是照着这一行行操作的。
COOKIE_STEPS: Dict[str, List[str]] = {
    "douyin": [
        "电脑浏览器打开 www.douyin.com 并登录",
        "按 F12 打开开发者工具 → Network（网络）标签 → 刷新页面",
        "点第一个请求 → Request Headers（请求标头）→ 复制整行 Cookie 的值",
        "在汲流「环境体检 → Cookie」里粘贴导入，域名填 douyin.com",
    ],
    "kuaishou": [
        "电脑浏览器打开 www.kuaishou.com 并登录",
        "F12 → Network → 刷新 → 复制请求头里整行 Cookie 的值",
        "在汲流「环境体检 → Cookie」里粘贴导入，域名填 kuaishou.com",
    ],
    "xiaohongshu": [
        "电脑浏览器打开 www.xiaohongshu.com 并登录（网页版）",
        "F12 → Network → 刷新 → 复制请求头里整行 Cookie 的值",
        "在汲流「环境体检 → Cookie」里粘贴导入，域名填 xiaohongshu.com",
    ],
    "qq": [
        "电脑浏览器打开 v.qq.com 并登录",
        "F12 → Network → 刷新 → 复制请求头里整行 Cookie 的值",
        "在汲流「环境体检 → Cookie」里粘贴导入，域名填 v.qq.com",
        "注意：VIP 内容只能下到试看片段，会员画质需要会员 Cookie",
    ],
    "youku": [
        "电脑浏览器打开 www.youku.com 并登录",
        "F12 → Network → 刷新 → 复制请求头里整行 Cookie 的值",
        "在汲流「环境体检 → Cookie」里粘贴导入，域名填 youku.com",
    ],
    "iqiyi": [
        "电脑浏览器打开 www.iqiyi.com 并登录",
        "F12 → Network → 刷新 → 复制请求头里整行 Cookie 的值",
        "在汲流「环境体检 → Cookie」里粘贴导入，域名填 iqiyi.com",
        "注意：解析器目前对爱奇艺新版页面支持有限，导入后也未必能下",
    ],
    "mgtv": [
        "电脑浏览器打开 www.mgtv.com 并登录",
        "F12 → Network → 刷新 → 复制请求头里整行 Cookie 的值",
        "在汲流「环境体检 → Cookie」里粘贴导入，域名填 mgtv.com",
    ],
    "weibo": [
        "电脑浏览器打开 weibo.com 并登录",
        "F12 → Network → 刷新 → 复制请求头里整行 Cookie 的值",
        "在汲流「环境体检 → Cookie」里粘贴导入，域名填 weibo.com",
    ],
    "xigua": [
        "电脑浏览器打开 www.ixigua.com（不必登录，只要有有效 Cookie）",
        "F12 → Network → 刷新 → 复制请求头里整行 Cookie 的值",
        "在汲流「环境体检 → Cookie」里粘贴导入，域名填 ixigua.com",
    ],
}

# 平台注册表。method 决定前端渲染哪种 UI：
#   qr     真扫码（bilibili 走 bili 模块，douyin 走本模块）
#   cookie 显示 Cookie 导入引导
ACCOUNTS: List[Dict[str, Any]] = [
    {"key": "bilibili", "name": "哔哩哔哩", "host": "bilibili.com",
     "method": "qr", "via": "bili",
     "note": "官方扫码接口，稳定可用；登录后大会员可下 4K"},
    {"key": "douyin", "name": "抖音", "host": "douyin.com",
     "method": "qr", "via": "douyin",
     "note": "扫码接口目前可用；抖音风控收紧后会自动降级为 Cookie 导入"},
    {"key": "kuaishou", "name": "快手", "host": "kuaishou.com",
     "method": "cookie", "login_url": "https://www.kuaishou.com/",
     "note": "扫码老接口已迁移（2026-09 实测 404/307），提供 Cookie 导入引导"},
    {"key": "xiaohongshu", "name": "小红书", "host": "xiaohongshu.com",
     "method": "cookie", "login_url": "https://www.xiaohongshu.com/",
     "note": "扫码接口有签名门槛（x-s），提供 Cookie 导入引导"},
    {"key": "qq", "name": "腾讯视频", "host": "v.qq.com",
     "method": "cookie", "login_url": "https://v.qq.com/",
     "note": "扫码走 QQ 体系换票链路，做不诚实就不做；提供 Cookie 导入引导"},
    {"key": "youku", "name": "优酷", "host": "youku.com",
     "method": "cookie", "login_url": "https://www.youku.com/",
     "note": "淘宝 SSO 能出码但轮询要隐藏配对参数（实测 EXPIRED），"
             "不给出扫不完的码；提供 Cookie 导入引导"},
    {"key": "iqiyi", "name": "爱奇艺", "host": "iqiyi.com",
     "method": "cookie", "login_url": "https://www.iqiyi.com/",
     "note": "提供 Cookie 导入引导"},
    {"key": "mgtv", "name": "芒果TV", "host": "mgtv.com",
     "method": "cookie", "login_url": "https://www.mgtv.com/",
     "note": "提供 Cookie 导入引导"},
    {"key": "weibo", "name": "微博", "host": "weibo.com",
     "method": "cookie", "login_url": "https://weibo.com/",
     "note": "扫码接口实测 403，提供 Cookie 导入引导"},
    {"key": "xigua", "name": "西瓜视频", "host": "ixigua.com",
     "method": "cookie", "login_url": "https://www.ixigua.com/",
     "note": "头条 SSO 按 aid 鉴权（实测全拒）；不登录也行，任何有效 Cookie 即可"},
]


# --------------------------------------------------------------------------
# 登录状态
# --------------------------------------------------------------------------

def _cookie_state(cfg: Dict[str, Any], acc: Dict[str, Any]) -> Dict[str, Any]:
    """这个平台当前的登录文件状态。

    bilibili 有 whoami（能查到账号名）；其余平台只看文件存在与否 ——
    「文件在」不等于「cookie 还新鲜」，这一点要在 note 里如实说。
    """
    host = acc["host"]
    if acc.get("key") == "bilibili":
        from . import bili
        d = bili.describe(cfg)
        return {"logged_in": bool(d.get("logged_in")),
                "user": d.get("user"),
                "cookie_file": d.get("cookie_file") or "",
                "note": d.get("note") or ""}
    f = _cookies.cookie_dir() / f"{host}.txt"
    ok = f.is_file() and f.stat().st_size > 80   # 80B 以下基本只有文件头
    return {"logged_in": ok, "user": None,
            "cookie_file": str(f) if ok else "",
            "note": ("已导入 Cookie 文件（是否仍有效以下载时为准）"
                     if ok else "未导入")}


def list_accounts(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """给界面的平台总览：每个平台用什么方式登录、现在的状态。"""
    out: List[Dict[str, Any]] = []
    for acc in ACCOUNTS:
        st = _cookie_state(cfg, acc)
        out.append({
            "key": acc["key"],
            "name": acc["name"],
            "host": acc["host"],
            "method": acc["method"],
            "note": acc.get("note", ""),
            "steps": COOKIE_STEPS.get(acc["key"]) or [],
            "login_url": acc.get("login_url", ""),
            **st,
        })
    return {"ok": True, "accounts": out}


def logout(cfg: Dict[str, Any], platform: str) -> Dict[str, Any]:
    """退出登录 = 删对应 cookie 文件。"""
    acc = next((a for a in ACCOUNTS if a["key"] == (platform or "").strip()),
               None)
    if not acc:
        return {"ok": False, "error": f"不认识的平台：{platform}"}
    if acc["key"] == "bilibili":
        from . import bili
        removed = bili.logout()
        return {"ok": True, "removed": removed}
    f = _cookies.cookie_dir() / f"{acc['host']}.txt"
    try:
        f.unlink()
        return {"ok": True, "removed": True}
    except Exception:
        return {"ok": True, "removed": False}


# --------------------------------------------------------------------------
# 抖音扫码
# --------------------------------------------------------------------------

_DY_SERVICE = "https%3A%2F%2Fwww.douyin.com"
_DY_GEN = "https://passport.douyin.com/x/passport-login/web/qrcode/generate/"
_DY_CHECK = "https://passport.douyin.com/x/passport-login/web/qrcode/check_qrconnect/"


def _dy_fp() -> str:
    """verify_fp：32 位小写十六进制。网页端就是一个随机的设备指纹。"""
    return secrets.token_hex(16)


def _dy_params(fp: str, extra: Optional[Dict[str, str]] = None) -> str:
    q = {
        "service": _DY_SERVICE,
        "need_logo": "false",
        "need_short_url": "false",
        "device_platform": "web_app",
        "account_platform": "facebook",
        "verify_fp": fp,
        "aid": "6383",
    }
    if extra:
        q.update(extra)
    return urllib.parse.urlencode(q)


def _dy_opener(cfg: Dict[str, Any], jar: http.cookiejar.CookieJar):
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    p = _proxy.select(cfg, "https://www.douyin.com")
    if p:
        handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _dy_get(cfg: Dict[str, Any], url: str,
            jar: http.cookiejar.CookieJar) -> Dict[str, Any]:
    op = _dy_opener(cfg, jar)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with op.open(req, timeout=15) as r:
            return json_loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return {"_error": f"HTTP {e.code}", "_body": body}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


def json_loads(raw: str) -> Dict[str, Any]:
    import json
    try:
        return json.loads(raw)
    except Exception:
        return {"_error": "返回不是 JSON", "_raw": raw[:200]}


def _dy_save(jar: http.cookiejar.CookieJar) -> str:
    """把抖音 jar 存成 Netscape 文件。"""
    rows: List[Dict[str, Any]] = []
    for c in jar:
        if not c.name or c.value is None:
            continue
        if c.domain and "douyin" not in c.domain:
            continue
        rows.append({
            "name": c.name, "value": c.value,
            "domain": c.domain or ".douyin.com",
            "path": c.path or "/", "secure": bool(c.secure),
            "httpOnly": c.has_nonstandard_attr("HttpOnly"),
            "expirationDate": c.expires,
        })
    if not rows:
        raise ValueError("确认成功但没有拿到任何 cookie（可能被风控拦截）")
    _cookies.save_text(_cookies.to_netscape(rows), "douyin.com")
    return f"{len(rows)} 条"


def douyin_qr_start(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """申请抖音登录二维码。

    返回结构与 bili.qr_start 对齐（qrcode 是渲染好的 PNG base64），
    前端不用为两个平台写两套渲染。
    """
    fp = _dy_fp()
    jar = http.cookiejar.CookieJar()
    data = _dy_get(cfg, f"{_DY_GEN}?{_dy_params(fp)}", jar)
    d = data.get("data") or {}
    if data.get("_error") or not d.get("qrcode") or not d.get("token"):
        # 如实降级：告诉用户扫码这条暂时走不通，Cookie 导入仍然可用
        return {
            "ok": False, "method": "qr",
            "error": ("抖音扫码接口当前不可用"
                      + (f"（{data.get('_error')}）" if data.get("_error") else "")
                      + "。请改用右侧的 Cookie 导入方式。"),
            "fallback": "cookie",
        }
    url = d["qrcode"]
    out: Dict[str, Any] = {
        "ok": True, "method": "qr", "url": url,
        "token": d["token"], "fp": fp, "expires_in": 180,
    }
    if _qr.available():
        b64 = _qr.png_base64(url)
        out["qrcode"] = b64
    else:
        out["ok"] = False
        out["error"] = ("本机缺少二维码库，无法渲染登录码。"
                        "运行 `pip install --target vendor qrcode pypng` 后重试。")
    return out


def douyin_qr_poll(cfg: Dict[str, Any], token: str, fp: str) -> Dict[str, Any]:
    """轮询抖音扫码状态。

    状态映射成与 B站一致的 state：scan / confirm / expired / ok。
    status=3（手机已确认）时还要请求 redirect_url —— cookie 在那一步
    的 Set-Cookie 里，只轮询是拿不到登录态的。
    """
    jar = http.cookiejar.CookieJar()
    q = _dy_params(fp, {"token": token, "state": fp})
    data = _dy_get(cfg, f"{_DY_CHECK}?{q}", jar)
    d = data.get("data") or {}
    if data.get("_error"):
        return {"ok": False, "state": "error",
                "error": f"轮询失败：{data.get('_error')}"}
    st = d.get("status")

    if st == _DY_CONFIRMED:
        redirect = d.get("redirect_url") or ""
        try:
            if redirect:
                if redirect.startswith("//"):
                    redirect = "https:" + redirect
                op = _dy_opener(cfg, jar)
                req = urllib.request.Request(redirect, headers={
                    "User-Agent": UA, "Referer": "https://www.douyin.com/"})
                with op.open(req, timeout=15) as r:
                    r.read()
            saved = _dy_save(jar)
            return {"ok": True, "state": "ok", "message": "登录成功",
                    "saved": saved}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "state": "error",
                    "error": f"已确认但 Cookie 落盘失败：{exc}"}
    if st == _DY_SCANNED:
        return {"ok": True, "state": "scan", "message": "已扫码，请在手机上确认"}
    if st == _DY_WAIT_SCAN:
        return {"ok": True, "state": "wait", "message": "等待扫码"}
    if st == _DY_EXPIRED:
        return {"ok": True, "state": "expired", "message": "二维码已过期，请重新获取"}
    return {"ok": True, "state": "unknown",
            "message": d.get("description") or "未知状态"}


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

def qr_start(cfg: Dict[str, Any], platform: str) -> Dict[str, Any]:
    p = (platform or "").strip()
    if p == "bilibili":
        from . import bili
        return bili.qr_start(cfg)
    if p == "douyin":
        return douyin_qr_start(cfg)
    acc = next((a for a in ACCOUNTS if a["key"] == p), None)
    if not acc:
        return {"ok": False, "error": f"不认识的平台：{platform}"}
    return {"ok": False, "method": "qr",
            "error": f"{acc['name']}没有可用的公开扫码接口，请用 Cookie 导入。",
            "fallback": "cookie"}


def qr_poll(cfg: Dict[str, Any], platform: str, key: str) -> Dict[str, Any]:
    p = (platform or "").strip()
    if p == "bilibili":
        from . import bili
        return bili.qr_poll(cfg, key)
    if p == "douyin":
        # key 里带 token 与 fp（generate 时一起返回），用 | 分隔
        token, _, fp = (key or "").partition("|")
        if not token:
            return {"ok": False, "state": "error", "error": "缺少 token"}
        return douyin_qr_poll(cfg, token, fp)
    return {"ok": False, "state": "error",
            "error": f"{p} 不支持扫码轮询"}
