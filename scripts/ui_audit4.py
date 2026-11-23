"""第四轮：验证新加的入口。"""
import json
import pathlib
import threading
import time
import http.server
import socketserver

FILES = pathlib.Path("build/_ui_home/files")


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(FILES), **kw)

    def log_message(self, *a):
        pass


httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 8997), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = pathlib.Path("build/_shots")
BASE = "http://127.0.0.1:8972/"

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True, args=["--hide-scrollbars"])
    ctx = br.new_context(viewport={"width": 1500, "height": 940}, locale="zh-CN",
                         color_scheme="dark")
    page = ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(BASE, wait_until="networkidle")
    time.sleep(1.0)
    print("版本:", page.inner_text("#ver"))

    # 建一个任务
    page.click("#mAdd")
    time.sleep(0.6)
    page.fill("#nwUrls", "http://127.0.0.1:8997/sample-b.bin")
    page.dispatch_event("#nwUrls", "input")
    time.sleep(0.3)
    page.click("#nwGo")
    time.sleep(4.0)

    # 打开详情
    page.locator("#rows tr").first.click()
    time.sleep(0.8)
    page.screenshot(path=str(OUT / "60-detail-queue.png"))
    print("有队列下拉:", page.locator("#dqSel").count())

    # 点校验和
    page.click("button[data-dtab='sum']")
    time.sleep(2.5)
    page.screenshot(path=str(OUT / "61-detail-checksum.png"))
    print("校验和面板:", page.inner_text("#detail")[:220].replace("\n", " | "))

    # 站点预设
    page.click("#mSettings")
    time.sleep(0.5)
    page.click("#drawerTabs button[data-tab='extras']")
    time.sleep(0.6)
    page.fill("#psHost", "bilibili.com")
    page.click("#psAdd")
    time.sleep(1.2)
    has = page.evaluate("() => (STATE?.features?.presets?.presets || []).map(x => x.key)")
    print("预设:", has)
    page.evaluate("() => { const b = document.querySelector('#drawerBody'); b.scrollTop = 900; }")
    time.sleep(0.4)
    page.screenshot(path=str(OUT / "62-extras-presets.png"))

    br.close()
print("pageerror:", errs)
