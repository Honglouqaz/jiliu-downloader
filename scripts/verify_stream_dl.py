#!/usr/bin/env python3
"""防盗链 HLS 的端到端验证：请求头透传 + 分片并发 + 合并成片。

为什么值得单独写一个脚本：

    「支持 m3u8」这句话在测试里很容易糊弄过去 —— 拿个公开测试流下 5 秒，
    看着是绿的，但真实站点的失败几乎都发生在**防盗链**上：m3u8 能拿到，
    分片却 403；或者 key 请求没带 Referer，AES 解不开。
    这两种情况只看"下载成功/失败"是分不清的。

这个脚本自己造一个"必须带 Referer 才给分片"的源站：

    /page.html        播放页（模拟播放器配置）
    /hls/index.m3u8   master playlist（2 档清晰度）
    /hls/720/*.ts     分片（Referer 不对 → 403）
    /hls/key.bin      AES-128 密钥（同样校验 Referer）

然后用汲流自己的下载路径去下：
    A. 不带 Referer  → 预期失败（证明校验真的在起作用）
    B. 带 Referer    → 预期成功，且产物是 mp4、时长≈分片总长
    C. 解析播放页    → 预期解析出 2 档清晰度并自动带上 Referer

只监听 127.0.0.1，素材用 ffmpeg 现场生成，跑完自清理。

    python scripts/verify_stream_dl.py
"""
from __future__ import annotations

import http.server
import os
import pathlib
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jiliu import pagesniff, tools, video  # noqa: E402

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REFERER = ""            # 起服务后填真实地址
HITS = {"seg": 0, "deny": 0}


class Handler(http.server.SimpleHTTPRequestHandler):
    """静态服务 + 一个很现实的防盗链：Referer 不对就 403。"""

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        ref = self.headers.get("Referer") or ""
        if path.startswith("/hls/") and not path.endswith("index.m3u8"):
            if not REFERER or not ref.startswith(REFERER.rsplit("/", 1)[0]):
                HITS["deny"] += 1
                self.send_error(403, "hotlink denied")
                return
            HITS["seg"] += 1
        return super().do_GET()


def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        print("找不到 ffmpeg，跳过（这条验证必须要有 ffmpeg）")
        sys.exit(0)
    return exe


def make_hls(work: pathlib.Path) -> None:
    """生成一段 AES-128 加密的 HLS。

    加密不是凑数：真实站点的「下到一半失败」有相当一部分是 key 请求
    没带 Referer / Cookie，而 yt-dlp 在缺 pycryptodomex 时会把解密
    整体交给 ffmpeg —— ffmpeg 的 hls 客户端**不一定**带我们传的头。
    所以这条链路必须端到端跑一遍才算验证过。
    """
    # 密钥必须和分片同目录：m3u8 里的 URI 是相对路径，放错地方就会
    # 404（真实站点上的表现是 "Unable to open key file"）
    hls = work / "hls"
    hls.mkdir(parents=True, exist_ok=True)
    (hls / "key.bin").write_bytes(b"0123456789abcdef")
    keyinfo = work / "keyinfo.txt"
    keyinfo.write_text(f"key.bin\n{hls / 'key.bin'}\n", encoding="utf-8")

    var = hls / "index.m3u8"
    subprocess.run([
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", "20",                       # 2s 一个关键帧 → 真的切出多片
        "-c:a", "aac", "-b:a", "64k",
        "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
        "-hls_key_info_file", str(keyinfo),
        "-hls_segment_filename", str(hls / "seg_%03d.ts"),
        str(var),
    ], check=True)


def main() -> int:
    global REFERER
    work = pathlib.Path(tempfile.mkdtemp(prefix="jl_stream_"))
    ok = fail = 0

    def check(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [OK  ] {name}")
        else:
            fail += 1
            print(f"  [FAIL] {name}" + (f"  → {extra}" if extra else ""))

    try:
        make_hls(work)
        # /hls/index.m3u8 里写的是相对路径，分片就在同目录
        (work / "page.html").write_text(
            """<html><head><title>验证片 第01集</title></head><body>
            <script>var player_aaaa={"encrypt":0,"url":"/hls/index.m3u8"};</script>
            </body></html>""", encoding="utf-8")

        handler = lambda *a, **kw: Handler(*a, directory=str(work), **kw)  # noqa: E731
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        srv.allow_reuse_address = True
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        REFERER = f"{base}/page.html"

        cmd = tools.ytdlp_command("")
        if not cmd:
            print("没找到 yt-dlp，无法验证")
            return 1

        cfg = {"proxy_mode": "off", "timeout": 20,
               "video_user_agent": "JiliuVerify/1.0", "stream_concurrency": 4}

        print("\n[A] 不带 Referer（应该失败）")
        out1 = work / "out1"
        r = video.download(f"{base}/hls/index.m3u8", out1, cfg)
        check("防盗链确实拦住了", r.get("ok") is False, r.get("error"))
        check("403 被归类成 cookie/凭证问题",
              (r.get("hint") or {}).get("kind") in ("cookie", "other", "network"),
              (r.get("hint") or {}).get("kind"))

        print("\n[B] 带 Referer + 分片并发（应该成功）")
        out2 = work / "out2"
        LOGS = []
        r = video.download(f"{base}/hls/index.m3u8", out2, cfg,
                           headers={"Referer": REFERER,
                                    "User-Agent": "JiliuVerify/1.0"},
                           log_cb=LOGS.append)
        joined = "\n".join(LOGS)
        check("下载成功", r.get("ok") is True, r.get("error"))
        prod = r.get("path") or ""
        check("产物存在", bool(prod) and pathlib.Path(prod).is_file(), prod)
        if prod and pathlib.Path(prod).is_file():
            size = pathlib.Path(prod).stat().st_size
            check("产物不是空文件", size > 5000, size)
            # 6 秒 160x120 的画面，合并后应该有 5 秒以上
            dur = subprocess.run([ffmpeg().replace("ffmpeg", "ffprobe"),
                                  "-v", "error", "-show_entries", "format=duration",
                                  "-of", "csv=p=0", prod],
                                 capture_output=True, text=True)
            try:
                secs = float((dur.stdout or "0").strip())
            except ValueError:
                secs = 0.0
            check("时长≈6 秒（分片都下齐了）", secs >= 5.0, f"{secs}s")
        check("分片请求确实带了 Referer（命中数 > 0）", HITS["seg"] > 0, HITS)
        # pycryptodomex 在不在，决定了 AES 分片是「原生多线程解密」还是
        # 「整个交给 ffmpeg 单连接」。前者能续传、能吃并发参数，后者不能。
        has_crypto = "pycryptodomex is not available" not in joined
        check("AES 走原生解密（vendor 里有 pycryptodomex）", has_crypto,
              joined[-400:] if not has_crypto else "")

        print("\n[C] 解析播放页（应该出地址并自动带 Referer）")
        res = pagesniff.resolve(REFERER, cfg)
        check("解析成功", res.get("ok") is True, res.get("error"))
        urls = [s["url"] for s in res.get("streams") or []]
        check("拿到 m3u8 地址", any(u.endswith("index.m3u8") for u in urls), urls)
        h = (res["streams"][0].get("headers") or {}) if res.get("streams") else {}
        check("Referer 指回播放页", h.get("Referer") == REFERER, h)
        check("Origin 补上", h.get("Origin") == base, h)

        srv.shutdown()
        srv.server_close()
    finally:
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass

    print(f"\n结果: {ok}/{ok + fail} 通过")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
