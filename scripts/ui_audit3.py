"""第三轮：验证修复 + 看新建任务面板。"""
import json
import pathlib
import threading
import time
import http.server
import socketserver

FILES = pathlib.Path("build/_ui_home/files")
FILES.mkdir(parents=True, exist_ok=True)
for n, size in (("sample-a.bin", 2 * 1024 * 1024), ("sample-b.bin", 700 * 1024)):
    p = FILES / n
    if not p.exists():
        p.write_bytes(b"A" * size)


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(FILES), **kw)

    def log_message(self, *a):
        pass


httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 8998), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

from playwright.sync_api import sync_playwright  # noqa: E402

OUT = pathlib.Path("build/_shots")

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True, args=["--hide-scrollbars"])
    ctx = br.new_context(viewport={"width": 1500, "height": 940}, locale="zh-CN",
                         color_scheme="dark")
    page = ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto("http://127.0.0.1:8971/", wait_until="networkidle")
    time.sleep(1.2)

    print("版本标签:", page.inner_text("#ver"))

    # 新建任务面板
    page.click("#mAdd")
    time.sleep(0.8)
    page.screenshot(path=str(OUT / "50-newtask.png"))
    q = page.evaluate("() => { const s = document.querySelector('#nwQueue'); "
                      "return [...s.options].map(o => o.value + '|' + o.textContent); }")
    print("队列下拉:", q)

    # 填一条 + 校验 + 限速，提交
    page.fill("#nwUrls", "http://127.0.0.1:8998/sample-a.bin")
    page.fill("#nwSpeed", "1")
    page.fill("#nwName", "renamed-sample.bin")
    page.dispatch_event("#nwUrls", "input")
    time.sleep(0.3)
    page.click("#nwGo")
    time.sleep(3.0)
    page.screenshot(path=str(OUT / "51-after-newtask.png"))
    st = page.evaluate("""() => (STATE.tasks||[]).map(t => ({name: t.filename, q: t.queue,
                          st: t.status, meta: Object.keys(t.meta||{})}))""")
    print("任务:", json.dumps(st, ensure_ascii=False)[:400])

    # 批量：两行
    page.click("#mAdd")
    time.sleep(0.5)
    page.fill("#nwUrls", "http://127.0.0.1:8998/sample-a.bin\nhttp://127.0.0.1:8998/sample-b.bin")
    page.dispatch_event("#nwUrls", "input")
    time.sleep(0.3)
    print("批量按钮文案:", page.inner_text("#nwGo"), "| hint:", page.inner_text("#nwHint"))
    page.screenshot(path=str(OUT / "52-newtask-batch.png"))
    page.click("[data-close='newMask']")
    time.sleep(0.3)

    # 队列页并发值
    page.click("#mSettings")
    time.sleep(0.6)
    page.click("#drawerTabs button[data-tab='extras']")
    time.sleep(0.6)
    vals = page.evaluate("""() => [...document.querySelectorAll('#drawerBody [data-q]')]
        .map(e => e.dataset.q + '=' + e.value)""")
    print("队列控件值:", vals)
    page.screenshot(path=str(OUT / "53-extras-fixed.png"))
    page.evaluate("() => { const b = document.querySelector('#drawerBody'); b.scrollTop = b.scrollHeight; }")
    time.sleep(0.4)
    page.screenshot(path=str(OUT / "54-extras-bottom-fixed.png"))

    # 探测 toast
    page.evaluate("() => document.querySelector('#drawer').classList.remove('show')")
    page.fill("#url", "http://127.0.0.1:8998/sample-b.bin")
    page.click("#btnProbe")
    time.sleep(1.5)
    page.screenshot(path=str(OUT / "55-probe-fixed.png"))
    print("toast html:", page.evaluate("() => document.querySelector('#toast').innerHTML")[:200])

    # 合集对直链的错误文案
    page.click("#mPlaylist")
    time.sleep(6)
    print("合集提示:", page.inner_text("#plNote")[:160].replace("\n", " "))
    page.screenshot(path=str(OUT / "56-playlist-fixed.png"))
    page.click("[data-close='plMask']")

    br.close()
print("pageerror:", errs)
