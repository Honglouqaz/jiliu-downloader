"""逐页截图 + 收集控制台错误 + 量布局（汲流 1.8.0 UI 审查）。"""
import json
import pathlib
import threading
import time
import http.server
import socketserver
import sys

BASE = "http://127.0.0.1:8971/"
OUT = pathlib.Path("build/_shots")
OUT.mkdir(parents=True, exist_ok=True)

# ---------- 本地文件服务，给任务列表造真实数据 ----------
FILES = pathlib.Path("build/_ui_home/files")
FILES.mkdir(parents=True, exist_ok=True)
(FILES / "sample-a.bin").write_bytes(b"A" * (2 * 1024 * 1024))
(FILES / "sample-b.bin").write_bytes(b"B" * (700 * 1024))
(FILES / "sample-c.bin").write_bytes(b"C" * (5 * 1024 * 1024))


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(FILES), **kw)

    def log_message(self, *a):
        pass


httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 8998), H)
httpd.allow_reuse_address = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()

from playwright.sync_api import sync_playwright  # noqa: E402

errors = []
report = {}


def shot(page, name, full=False):
    p = OUT / f"{name}.png"
    page.screenshot(path=str(p), full_page=full)
    print(f"  shot {name} -> {p.stat().st_size // 1024} KB")


def metrics(page, label):
    """量一下关键元素尺寸，找出溢出/挤压。"""
    r = page.evaluate("""() => {
      const out = {};
      const pick = (sel) => {
        const el = document.querySelector(sel);
        if(!el) return null;
        const b = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return {w: Math.round(b.width), h: Math.round(b.height),
                sw: el.scrollWidth, ch: el.clientHeight, sh: el.scrollHeight,
                ovf: cs.overflow, fs: cs.fontSize};
      };
      out.menubar = pick(".menubar");
      out.addbar = pick(".addbar");
      out.actionbar = pick(".actionbar");
      out.side = pick("#side");
      out.table = pick(".tblwrap");
      out.drawer = pick("#drawer");
      out.body = pick("#drawerBody");
      out.docW = document.documentElement.scrollWidth;
      out.winW = innerWidth;
      out.hOverflow = document.documentElement.scrollWidth > innerWidth + 1;
      // 找出所有超出视口右边界的可见元素
      const bad = [];
      document.querySelectorAll("body *").forEach(el => {
        const b = el.getBoundingClientRect();
        if (b.width > 0 && b.height > 0 && b.right > innerWidth + 2 && getComputedStyle(el).position !== "fixed") {
          bad.push((el.id || el.className || el.tagName) + " → " + Math.round(b.right));
        }
      });
      out.overflowEls = bad.slice(0, 12);
      return out;
    }""")
    report[label] = r
    return r


with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True,
                            args=["--force-device-scale-factor=1", "--hide-scrollbars"])
    ctx = br.new_context(viewport={"width": 1500, "height": 940},
                         locale="zh-CN", color_scheme="dark")
    page = ctx.new_page()
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

    page.goto(BASE, wait_until="networkidle")
    time.sleep(1.2)
    shot(page, "01-main-empty")
    m = metrics(page, "main-empty")
    print("  empty:", json.dumps(m, ensure_ascii=False)[:300])

    # ---- 造任务：1 个正常下、1 个 404、1 个磁力（走 BT 无网络） ----
    page.fill("#url", "http://127.0.0.1:8998/sample-a.bin")
    page.click("#btnAdd")
    time.sleep(0.6)
    page.fill("#url", "http://127.0.0.1:8998/sample-c.bin")
    page.click("#btnAdd")
    time.sleep(0.6)
    page.fill("#url", "http://127.0.0.1:8998/nope-404.bin")
    page.click("#btnAdd")
    time.sleep(0.6)
    page.fill("#url", "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=test")
    page.click("#btnAdd")
    time.sleep(2.5)
    shot(page, "02-main-tasks")

    # ---- 探测按钮 ----
    page.fill("#url", "http://127.0.0.1:8998/sample-b.bin")
    page.click("#btnProbe")
    time.sleep(1.0)
    shot(page, "03-probe")

    # ---- 新建任务面板 ----
    page.click("#mAdd")
    time.sleep(0.8)
    shot(page, "04-new-task")
    print("  new-task dialogs:", page.evaluate("() => document.querySelectorAll('.drawer.show, .modal, dialog[open]').length"))
    page.keyboard.press("Escape")
    time.sleep(0.3)

    # ---- 菜单项逐个点开 ----
    for btn, name in [("#mPlaylist", "05-playlist"), ("#mBili", "06-account"),
                      ("#mEnv", "07-envdo"), ("#mHelp", "08-protocols")]:
        try:
            page.click(btn)
            time.sleep(0.9)
            shot(page, name)
            page.keyboard.press("Escape")
            time.sleep(0.3)
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {name}: {exc}")

    # ---- 设置抽屉全 tab ----
    page.click("#mSettings")
    time.sleep(0.8)
    tabs = page.evaluate("() => [...document.querySelectorAll('#drawerTabs button')].map(b => [b.dataset.tab, b.textContent])")
    print("  tabs:", tabs)
    for key, label in tabs:
        page.click(f"#drawerTabs button[data-tab='{key}']")
        time.sleep(0.5)
        shot(page, f"10-cfg-{key}")
        mm = metrics(page, f"cfg-{key}")
        if mm["body"] and mm["body"]["sh"] > mm["body"]["ch"]:
            print(f"    {key}: 内容 {mm['body']['sh']}px > 视口 {mm['body']['ch']}px（需滚动）")

    # ---- 扩展页交互探测 ----
    page.click("#drawerTabs button[data-tab='extras']")
    time.sleep(0.6)
    shot(page, "20-extras-top", full=False)
    page.evaluate("() => { const b = document.querySelector('#drawerBody'); b.scrollTop = b.scrollHeight; }")
    time.sleep(0.4)
    shot(page, "21-extras-bottom")

    page.click("#drawerTabs button[data-tab='look']")
    time.sleep(0.5)
    shot(page, "22-look")

    page.evaluate("() => document.querySelector('#drawer').classList.remove('show')")
    time.sleep(0.3)

    # ---- 空列表/窄窗（看 1024 宽是否挤） ----
    page.set_viewport_size({"width": 1024, "height": 768})
    time.sleep(0.8)
    shot(page, "30-narrow-1024")
    m = metrics(page, "narrow")
    if m["overflowEls"]:
        print("  1024 溢出元素:", m["overflowEls"])

    br.close()

print("\n== console errors ==")
for e in errors[:25]:
    print(" ", e[:200])
print("total:", len(errors))

pathlib.Path("build/_ui_report.json").write_text(
    json.dumps({"metrics": report, "errors": errors}, ensure_ascii=False, indent=1),
    encoding="utf-8")
