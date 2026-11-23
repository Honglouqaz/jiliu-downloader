"""第二轮：长等待交互 + 控件宽度实测。"""
import json
import pathlib
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8971/"
OUT = pathlib.Path("build/_shots")

with sync_playwright() as pw:
    br = pw.chromium.launch(channel="chrome", headless=True, args=["--hide-scrollbars"])
    ctx = br.new_context(viewport={"width": 1500, "height": 940}, locale="zh-CN",
                         color_scheme="dark")
    page = ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(BASE, wait_until="networkidle")
    time.sleep(1.0)

    # 1) 环境体检等 12 秒
    page.click("#mEnv")
    time.sleep(12)
    shot = OUT / "40-envdo-wait12.png"
    page.screenshot(path=str(shot))
    print("env body:", page.inner_text("#anyBody")[:400].replace("\n", " | "))

    page.click("[data-close='anyMask']")
    time.sleep(0.3)

    # 2) 合集弹窗等 8 秒
    page.fill("#url", "http://127.0.0.1:8998/sample-b.bin")
    page.click("#mPlaylist")
    time.sleep(8)
    page.screenshot(path=str(OUT / "41-playlist-wait8.png"))
    print("playlist note:", page.inner_text("#plNote")[:200].replace("\n", " | "))

    page.click("[data-close='plMask']")
    time.sleep(0.3)

    # 3) 队列控件实测宽度
    page.click("#mSettings")
    time.sleep(0.5)
    page.click("#drawerTabs button[data-tab='extras']")
    time.sleep(0.6)
    w = page.evaluate("""() => {
      const r = [];
      document.querySelectorAll("#drawerBody [data-q]").forEach(el => {
        const b = el.getBoundingClientRect();
        r.push({k: el.dataset.q, w: Math.round(b.width), h: Math.round(b.height),
                v: el.value});
      });
      const ch = document.querySelector("#chName");
      const tk = document.querySelector("#chToken");
      const imp = document.querySelector("#impBox");
      return {queue: r.slice(0, 6),
              chName: ch ? Math.round(ch.getBoundingClientRect().width) : null,
              chToken: tk ? Math.round(tk.getBoundingClientRect().width) : null,
              impBox: imp ? [Math.round(imp.getBoundingClientRect().width),
                             Math.round(imp.getBoundingClientRect().height)] : null,
              drawerW: Math.round(document.querySelector("#drawer").getBoundingClientRect().width)};
    }""")
    print("widths:", json.dumps(w, ensure_ascii=False))

    # 4) 设置页是否有「主题切换后仍可读」的对比问题：抓两个颜色
    col = page.evaluate("""() => {
      const cs = getComputedStyle(document.documentElement);
      const inp = document.querySelector("#drawerBody input, #drawerBody select");
      const ics = inp ? getComputedStyle(inp) : null;
      return {panel: cs.getPropertyValue("--panel").trim(),
              line: cs.getPropertyValue("--line").trim(),
              text: cs.getPropertyValue("--text").trim(),
              dim: cs.getPropertyValue("--dim").trim(),
              inputBg: ics ? ics.backgroundColor : null,
              inputBorder: ics ? ics.borderColor : null,
              inputColor: ics ? ics.color : null,
              bodyFont: getComputedStyle(document.body).fontSize};
    }""")
    print("colors:", json.dumps(col, ensure_ascii=False))

    # 5) 「新建任务」点击后发生了什么
    page.evaluate("() => document.querySelector('#drawer').classList.remove('show')")
    before = page.evaluate("() => document.querySelectorAll('.mask.show, .drawer.show, .dlg').length")
    page.click("#mAdd")
    time.sleep(0.8)
    after = page.evaluate("() => document.querySelectorAll('.mask.show, .drawer.show').length")
    focus = page.evaluate("() => document.activeElement.id")
    print(f"mAdd: mask前={before} mask后={after} focus={focus}")

    # 6) 侧栏分类点击是否过滤
    page.click("#side >> text=视频")
    time.sleep(0.8)
    rows = page.evaluate("() => document.querySelectorAll('#rows tr').length")
    print("点「视频」后行数:", rows)
    page.screenshot(path=str(OUT / "42-filter-video.png"))

    # 7) 任务详情：点一行看是否展开
    page.click("#side >> text=全部任务")
    time.sleep(0.5)
    if page.locator("#rows tr").count():
        page.locator("#rows tr").first.click()
        time.sleep(0.8)
        det = page.inner_text("#detail")[:300].replace("\n", " | ")
        print("detail:", det or "(空)")
        page.screenshot(path=str(OUT / "43-detail.png"))

    # 8) 键盘/无障碍：tab 序列前几个
    page.keyboard.press("Escape")
    page.evaluate("() => document.querySelector('#url').focus()")
    seq = []
    for _ in range(8):
        page.keyboard.press("Tab")
        seq.append(page.evaluate("() => document.activeElement.id || document.activeElement.textContent.slice(0,8)"))
    print("tab 顺序:", seq)

    # 9) 1024 宽 + 抽屉打开（真测溢出）
    page.set_viewport_size({"width": 1024, "height": 720})
    page.click("#mSettings")
    time.sleep(0.8)
    page.screenshot(path=str(OUT / "44-1024-drawer.png"))
    ov = page.evaluate("""() => {
      const bad = [];
      document.querySelectorAll("body *").forEach(el => {
        const b = el.getBoundingClientRect();
        if (b.width > 0 && b.height > 0 && b.right > innerWidth + 2) {
          bad.push((el.id || el.className || el.tagName) + "→" + Math.round(b.right));
        }
      });
      return {docW: document.documentElement.scrollWidth, winW: innerWidth,
              bad: bad.slice(0, 8)};
    }""")
    print("1024 溢出:", json.dumps(ov, ensure_ascii=False))

    br.close()

print("pageerror:", errs)
