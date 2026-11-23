"""设置页整页视图的截图审查。

检查四件事：
  1）四种宽度下侧栏导航与内容区是否稳定（1500 / 1280 / 1024 / 900）
  2）每个页面都渲染得出来，不报 JS 错
  3）引擎页、关于页这类带表格的页面，内容有没有被裁掉
  4）控制台有没有异常

用法：python scripts/ui_audit6.py [port]
"""
from __future__ import annotations

import pathlib
import sys

from playwright.sync_api import sync_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8971
OUT = pathlib.Path("build/_shots2")
OUT.mkdir(parents=True, exist_ok=True)

TABS = ["general", "queues", "engine", "layout", "naming",
        "proxy", "cookie", "bt",
        "video", "list", "auto", "extras",
        "look", "edition", "about"]

SIZES = [(1500, 950), (1280, 900), (1024, 768), (900, 720)]


def main() -> None:
    errors = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        for w, h in SIZES:
            pg = b.new_page(viewport={"width": w, "height": h})
            pg.on("console", lambda m: errors.append(f"{w}px console.{m.type}: {m.text}")
                  if m.type in ("error",) else None)
            pg.on("pageerror", lambda e: errors.append(f"{w}px pageerror: {e}"))
            pg.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
            pg.wait_for_timeout(600)
            # 干净配置会弹首次启动向导；先截它一张，再跳过
            if pg.is_visible("#onbMask.show"):
                pg.screenshot(path=str(OUT / f"onboard-{w}.png"))
                pg.click("#onbSkip")
                pg.wait_for_timeout(400)
            pg.click("#mSettings")
            pg.wait_for_timeout(350)
            pg.screenshot(path=str(OUT / f"set-{w}.png"))

            # 侧栏是否横向溢出 / 内容区是否被压
            info = pg.evaluate("""() => {
              const nav = document.querySelector('#setNav');
              const body = document.querySelector('#drawerBody');
              const dr = document.querySelector('#drawer');
              const wrap = document.querySelector('.setWrap');
              const r = n => n ? n.getBoundingClientRect() : null;
              return {
                navW: r(nav)?.width, bodyW: r(body)?.width, wrapW: r(wrap)?.width,
                drawerTop: r(dr)?.top,
                vScroll: document.querySelector('#drawerBody').scrollWidth >
                         document.querySelector('#drawerBody').clientWidth,
                navGroups: document.querySelectorAll('#setNav .grp').length,
                navItems: document.querySelectorAll('#setNav button').length,
              };
            }""")
            print(f"{w}x{h}: {info}")

            # 只在一个尺寸下遍历所有页，避免截图过多
            if w == 1500:
                for t in TABS:
                    pg.click(f'#setNav button[data-tab="{t}"]')
                    pg.wait_for_timeout(180)
                    pg.screenshot(path=str(OUT / f"tab-{t}.png"))
                    # 关键页：检查表格是否横向溢出
                    ov = pg.evaluate("""() => {
                      const b = document.querySelector('#drawerBody');
                      const t = b.querySelector('table');
                      if(!t) return null;
                      return {tbl: Math.round(t.getBoundingClientRect().width),
                              body: Math.round(b.getBoundingClientRect().width),
                              bodyScroll: b.scrollWidth > b.clientWidth};
                    }""")
                    print(f"  {t}: {ov}")
            pg.close()
        b.close()
    print("\n错误：", errors or "无")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
