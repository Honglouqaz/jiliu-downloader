"""第五轮：干净实例，全链路复验（含文件名策略、校验和、队列、预设）。"""
import json
import pathlib
import threading
import time
import http.server
import socketserver

FILES = pathlib.Path("build/_ui_home/files")
FILES.mkdir(parents=True, exist_ok=True)


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(FILES), **kw)

    def log_message(self, *a):
        pass


httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 8996), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = pathlib.Path("build/_shots")
BASE = "http://127.0.0.1:8973/"

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True, args=["--hide-scrollbars"])
    ctx = br.new_context(viewport={"width": 1500, "height": 940}, locale="zh-CN",
                         color_scheme="dark")
    page = ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(BASE, wait_until="networkidle")
    time.sleep(1.0)
    page.screenshot(path=str(OUT / "70-empty.png"))

    # 单条任务：.bin 应保持 .bin
    page.click("#mAdd")
    time.sleep(0.6)
    page.screenshot(path=str(OUT / "71-newtask.png"))
    page.fill("#nwUrls", "http://127.0.0.1:8996/sample-a.bin")
    page.dispatch_event("#nwUrls", "input")
    time.sleep(0.2)
    page.click("#nwGo")
    time.sleep(4.0)
    names = page.evaluate("() => (STATE.tasks||[]).map(t => t.filename + ' @' + t.queue)")
    print("任务名:", names)

    # 详情 + 队列 + 校验和
    page.locator("#rows tr").first.click()
    time.sleep(0.8)
    page.screenshot(path=str(OUT / "72-detail.png"))
    page.click("button[data-dtab='sum']")
    time.sleep(2.5)
    page.screenshot(path=str(OUT / "73-checksum.png"))
    txt = page.inner_text("#detail")
    print("校验和:", " | ".join([l for l in txt.splitlines() if l.strip()][:8]))

    # 队列移入
    if page.locator("#dqSel").count():
        page.select_option("#dqSel", "video")
        page.click("#dqGo")
        time.sleep(1.5)
        print("移入后:", page.evaluate("() => (STATE.tasks||[]).map(t => t.queue)"))

    # 批量两条
    page.click("#mAdd")
    time.sleep(0.5)
    page.fill("#nwUrls", "http://127.0.0.1:8996/sample-b.bin\nhttp://127.0.0.1:8996/nope-404.bin")
    page.dispatch_event("#nwUrls", "input")
    time.sleep(0.3)
    page.click("#nwGo")
    time.sleep(4.5)
    page.screenshot(path=str(OUT / "74-batch-done.png"))
    print("批量后任务数:", page.evaluate("() => (STATE.tasks||[]).length"))

    # 扩展开：预设 + 通知 + 插件 + 导入
    page.click("#mSettings")
    time.sleep(0.5)
    page.click("#drawerTabs button[data-tab='extras']")
    time.sleep(0.6)
    page.fill("#psHost", "bilibili.com")
    page.click("#psAdd")
    time.sleep(1.2)
    page.evaluate("() => { const b = document.querySelector('#drawerBody'); b.scrollTop = 520; }")
    time.sleep(0.4)
    page.screenshot(path=str(OUT / "75-extras-mid.png"))
    page.evaluate("() => { const b = document.querySelector('#drawerBody'); b.scrollTop = b.scrollHeight; }")
    time.sleep(0.4)
    page.screenshot(path=str(OUT / "76-extras-end.png"))

    br.close()
print("pageerror:", errs)
