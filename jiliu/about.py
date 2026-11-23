"""关于页的数据：版本、第三方组件许可、诊断包。

为什么单独一个模块：

  这三件事都**只有在出问题的时候才被想起来**，但恰恰那时候最需要准。
  版本号写错、许可漏标、诊断包里混进 Cookie —— 任何一个都会让
  「找客服」这一步变成第二轮排查。所以集中在一处生成，界面只负责显示。

第三方许可不是形式主义：随包分发 aria2c（GPLv2+）和 ffmpeg（LGPL/GPL）
就必须提供对应源码或书面要约。这里把清单明写在界面上、许可全文放
licenses/ 目录，是履行分发义务，也是「这软件敢让人查」的信号。
"""
from __future__ import annotations

import io
import json
import os
import platform
import sys
import time
import zipfile
from typing import Any, Dict, List, Optional

from . import __version__, config as _config, edition, engines, tools, update

# 随包/常用外部组件的许可。缺失的组件不会出现在界面上，但清单要全 ——
# 用户查的是「你带了什么」，不是「你现在装了什么」。
LICENSES: Dict[str, Dict[str, str]] = {
    "aria2": {"name": "aria2 (aria2c)", "license": "GPL-2.0-or-later",
              "use": "P2P / 磁力 / 多源下载引擎"},
    "ffmpeg": {"name": "FFmpeg", "license": "LGPL-2.1-or-later（本包为 LGPL 构建）",
               "use": "音视频合流、转码、HLS 分片"},
    "yt-dlp": {"name": "yt-dlp", "license": "Unlicense",
               "use": "视频页面解析"},
    # 纯 Python 轮子，随 vendor/ 打包（不是外部程序）
    "Cryptodome": {"name": "PyCryptodome (pycryptodomex)", "license": "BSD-2-Clause / Public Domain",
                   "use": "HLS AES-128 分片原生解密", "bundled": True},
    "N_m3u8DL-RE": {"name": "N_m3u8DL-RE", "license": "MIT",
                    "use": "HLS / DASH 分片下载（可选加强）"},
    "curl": {"name": "curl", "license": "curl（MIT 类）",
             "use": "兼容性兜底引擎（Windows 自带）"},
    "wget": {"name": "GNU Wget", "license": "GPL-3.0-or-later",
             "use": "断流续传兜底引擎"},
}

SUPPORT = "GitHub Issues"
SUPPORT_URL = "https://github.com/jiliu-downloader/jiliu-downloader/issues"


def components() -> List[Dict[str, str]]:
    """随包带了哪些外部程序（只看 bin/ 里真实存在的，不列「可能装了」的）。"""
    out: List[Dict[str, str]] = []
    try:
        bin_dir = tools.BIN_DIR
        if not bin_dir or not bin_dir.exists():
            return out
        names = {p.name.lower() for p in bin_dir.iterdir() if p.is_file()}
    except Exception:
        return out
    for key, meta in LICENSES.items():
        base = key.lower().replace("-", "")
        hit = any(n.startswith(base) or base in n for n in names)
        # bundled=True 的是随 vendor/ 打进去的 Python 库（不是 bin/ 里的 exe），
        # 靠文件名匹配永远命中不了 —— 但它们同样是分发义务，必须列出来。
        if hit or meta.get("bundled"):
            out.append({"key": key, **meta})
    return out


def describe(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or _config.load()
    ed = edition.describe(cfg)
    up = update.describe(cfg)
    return {
        "version": __version__,
        "build": os.environ.get("JILIU_BUILD", ""),
        "edition_key": ed.get("key"),
        "edition_name": ed.get("name"),
        "form": ed.get("form"),
        "form_name": ed.get("form_name"),
        "install_kind": up.get("kind"),
        "state_dir": str(_config.HOME),
        "app_dir": up.get("dir"),
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "components": components(),
        "support": SUPPORT,
        "support_url": SUPPORT_URL,
        "licenses_dir": str(_config.HOME.parent / "licenses"),
    }


# ---------------------------------------------------------------------------
# 诊断包
# ---------------------------------------------------------------------------
_SECRET_HINTS = ("cookie", "token", "password", "secret", "key", "passwd", "auth")


def _scrub(d: Any, depth: int = 0) -> Any:
    """把配置里可能带凭据的字段打码。

    诊断包会被贴进聊天窗口、邮件、issue 里 —— 一旦带出 cookie，
    用户丢的是账号而不只是隐私。所以宁可多打码几个字段。
    """
    if depth > 6:
        return "…"
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            kl = str(k).lower()
            if any(h in kl for h in _SECRET_HINTS) and not isinstance(v, (int, float, bool)):
                out[k] = "***（已隐去）"
            else:
                out[k] = _scrub(v, depth + 1)
        return out
    if isinstance(d, list):
        return [_scrub(x, depth + 1) for x in d[:50]]
    if isinstance(d, str) and len(d) > 400:
        return d[:400] + "…（截断）"
    return d


def _tail(path: Any, lines: int = 300) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-lines:])
    except Exception:
        return ""


def export_diagnostics(cfg: Optional[Dict[str, Any]] = None,
                       out_dir: Optional[str] = None) -> Dict[str, Any]:
    """打一个 zip：版本、脱敏配置、引擎探测、日志尾部。

    不含下载的文件、不含 cookie 值、不含许可证全文（只留前 12 位，
    够客服判断是哪张证，不够拿去盗用）。
    """
    cfg = cfg or _config.load()
    info = describe(cfg)
    buf = io.BytesIO()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    lic = ""
    try:
        from . import license as _lic
        lic = str(_lic.status().get("license_id") or "")
    except Exception:
        pass

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("about.json", json.dumps(info, ensure_ascii=False, indent=2))
        z.writestr("config.json", json.dumps(_scrub(cfg), ensure_ascii=False, indent=2))

        try:
            z.writestr("engines.json", json.dumps(
                engines.describe(cfg, refresh=True), ensure_ascii=False, indent=2))
        except Exception as e:
            z.writestr("engines.error.txt", repr(e))

        try:
            from . import license as _lic
            s = dict(_lic.status())
            # 只留标识，不留能用它激活的东西
            z.writestr("license.json", json.dumps(
                {k: v for k, v in s.items() if k in
                 ("state", "plan", "label", "device", "expires_at", "license_id", "name")},
                ensure_ascii=False, indent=2))
        except Exception:
            pass

        logs = []
        for cand in ("app.log", "jiliu.log", "server.log"):
            p = _config.HOME / "logs" / cand
            if p.exists():
                logs.append(f"=== {cand} ===\n" + _tail(p))
        if logs:
            z.writestr("logs.txt", "\n\n".join(logs))
        else:
            z.writestr("logs.txt", "（没有日志文件；README 里说明了日志位置）")

        z.writestr("README.txt",
                   f"""汲流诊断包 · {stamp}

这个包里有什么：
  about.json    版本、系统、安装形态
  config.json   设置（Cookie / 密钥 / 代理密码已隐去）
  engines.json  本机引擎探测结果
  license.json  授权状态（不含可用许可证全文）
  logs.txt      最近日志

这个包里没有什么：
  你下载的文件、Cookie 的取值、许可证全文。

怎么用：把整个 zip 发到 {SUPPORT_URL}，说明「哪一步不对、报了什么错」。
""")

    data = buf.getvalue()
    dest_dir = out_dir or str(_config.HOME / "diagnostics")
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        dest_dir = os.environ.get("TEMP") or "."
    path = os.path.join(dest_dir, f"jiliu-diag-{stamp}.zip")
    try:
        with open(path, "wb") as f:
            f.write(data)
    except Exception as e:
        return {"ok": False, "error": f"写文件失败：{e}"}
    return {"ok": True, "path": path, "size": len(data), "license_hint": lic[:12]}
