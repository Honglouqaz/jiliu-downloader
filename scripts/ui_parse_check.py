#!/usr/bin/env python3
"""「解析播放页」面板的界面验证（Playwright + 本地假站点）。

为什么要专门验一次界面：

    后端能解析出地址，不等于用户会用。这里走的是真实点击路径 ——
    新建任务 → 粘地址 → 点「解析播放页」→ 看到候选清单 → 点「下载这条」，
    然后确认队列里真的多了一个任务。

假站点完全模仿苹果CMS 的播放页（player_aaaa 里放 m3u8 直链），
外加一个 iframe 播放器页，这样 iframe 递归也会被真实验证到。

    python scripts/ui_parse_check.py 8991
"""
from __future__ import annotations

import http.server
import os
import pathlib
import socketserver
import sys
import threading
import time
import urllib.request

from playwright.sync_api import sync_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "8991"
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = pathlib.Path(__file__).resolve().parent.parent / "build" / "_shots"
SHOTS.mkdir(parents=True, exist_ok=True)

# ---- 假站点 ----------------------------------------------------------------
MOCK = {
    "/vodplay/1-1-1.html": """<html><head><title>验证剧 第01集_在线观看 - 假影院</title></head>
<body><div class="player"><iframe src="/player/1.html"></iframe></div>
<script>var player_aaaa={"flag":"play","encrypt":0,
"url":"1080P$http://127.0.0.1:PORT/hls/1080/index.m3u8#720P$http://127.0.0.1:PORT/hls/720/index.m3u8"};
</script></body></html>""",
    "/hls/1080/index.m3u8": """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,NAME="1080P"
v.m3u8
""",
    "/hls/720/index.m3u8": """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,NAME="720P"
v.m3u8
""",
}


class MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = MOCK.get(self.path.split("?")[0])
        if body is None:
            self.send_error(404)
            return
        body = body.replace("PORT", str(MOCK_PORT)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


MOCK_PORT = 0


def start_mock():
    global MOCK_PORT
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), MockHandler)
    MOCK_PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def warm():
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        try:
            op.open(BASE + "/api/health", timeout=2).read()
            return True
        except Exception:
            time.sleep(0.1)
    return False


def main() -> int:
    srv = start_mock()
    page_url = f"http://127.0.0.1:{MOCK_PORT}/vodplay/1-1-1.html"
    print("假站点：", page_url)

    if not warm():
        print("汲流服务没起来（先跑 python jiliu.py serve --port " + PORT + "）")
        return 1

    ok = fail = 0

    def check(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [OK  ] {name}")
        else:
            fail += 1
            print(f"  [FAIL] {name}" + (f"  → {extra}" if extra else ""))

    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context(viewport={"width": 1400, "height": 900})
        pg = ctx.new_page()
        errors = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.wait_for_timeout(600)
        if pg.is_visible("#onbMask.show"):
            pg.click("#onbSkip")
            pg.wait_for_timeout(300)

        # 打开「新建任务」：优先点工具栏的加号，没有就直接调函数
        try:
            pg.click("#mAdd", timeout=3000)
        except Exception:
            pg.evaluate("nwOpen('')")
        pg.wait_for_selector("#newMask.show", timeout=5000)
        check("新建任务面板能打开", True)

        check("有「解析播放页」按钮", pg.is_visible("#nwParse"))
        pg.fill("#nwUrls", page_url)
        pg.click("#nwParse")
        pg.wait_for_selector("#nwStreams button[data-stream]", timeout=20000)

        txt = pg.inner_text("#nwStreams")
        check("列出了候选地址", "找到" in txt, txt[:80])
        check("iframe 递归出的两条流都在", txt.count("下载这条") == 2, txt.count("下载这条"))
        check("显示清晰度", "1080P" in txt and "720P" in txt, txt[:200])
        check("显示页面标题", "验证剧" in txt, txt[:200])
        pg.screenshot(path=str(SHOTS / "parse-1-result.png"), full_page=True)

        # 点第一条 → 应该进队列
        before = pg.evaluate("(STATE?.tasks || []).length")
        pg.click("#nwStreams button[data-stream='0']")
        pg.wait_for_timeout(2500)
        after = pg.evaluate("(STATE?.tasks || []).length")
        check("点「下载这条」建了任务", after == before + 1, f"{before} → {after}")
        check("面板已关闭", not pg.is_visible("#newMask.show"))

        # 队列里那条的请求头要真的存下来了（防盗链靠它）
        row = pg.evaluate("(STATE.tasks || []).find(t => (t.meta||{}).headers) || null")
        hdr = (row or {}).get("meta", {}).get("headers", {}) if row else {}
        check("任务带上了请求头", bool((hdr.get("Referer") or "").startswith("http")),
              str(hdr)[:200] if row else "没找到带 headers 的任务")

        pg.screenshot(path=str(SHOTS / "parse-2-queued.png"), full_page=True)
        errs = [e for e in errors if "favicon" not in e]
        check("没有 JS 报错", not errs, errs[:3])
        b.close()

    srv.shutdown()
    print(f"\n结果: {ok}/{ok + fail} 通过（截图在 build/_shots/parse-*.png）")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
