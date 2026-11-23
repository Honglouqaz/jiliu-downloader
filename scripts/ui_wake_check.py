#!/usr/bin/env python3
"""协议唤起设置页的界面验证（Playwright 真实点击）。

后端通了不代表用户会用。这里验的是设置页那一块：

    设置 → 自动化 → 协议唤起（jiliu://）
    → 看到注册状态 → 点「立即注册」→ 状态变绿
    → 点「只解析」→ 弹窗显示解析结果
    → 点「取消注册」→ 状态变灰（测完还原，不留痕）

    python scripts/ui_wake_check.py 8996
"""
from __future__ import annotations

import pathlib
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "8996"
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = pathlib.Path(__file__).resolve().parent.parent / "build" / "_shots"
SHOTS.mkdir(parents=True, exist_ok=True)


def warm() -> bool:
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        try:
            op.open(BASE + "/api/health", timeout=2).read()
            return True
        except Exception:      # noqa: BLE001
            time.sleep(0.1)
    return False


def main() -> int:
    if not warm():
        print(f"汲流服务没起来（先跑 python jiliu.py serve --port {PORT}）")
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
        ctx = b.new_context(viewport={"width": 1400, "height": 980})
        pg = ctx.new_page()
        errors = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.wait_for_timeout(600)
        if pg.is_visible("#onbMask.show"):
            pg.click("#onbSkip")
            pg.wait_for_timeout(300)

        # 直接跳页：比点两级菜单稳，且不依赖工具栏布局改动
        pg.evaluate("gotoTab('auto')")
        pg.wait_for_selector("#drawer.show", timeout=5000)
        pg.wait_for_timeout(700)

        body = pg.inner_text("#drawerBody")
        # .sect 有 text-transform:uppercase，渲染后 jiliu:// 会变大写，
        # 所以只比对中文部分 —— 别为了这个去改产品样式。
        check("有「协议唤起」区块", "协议唤起" in body, body[:120])
        check("有自动注册开关", pg.is_visible("#c_url_protocol_auto"))
        check("有注册状态", "注册状态" in body, body[:200])
        check("有两个按钮", pg.is_visible("button[data-ut=register]")
              and pg.is_visible("button[data-ut=unregister]"))
        check("有试试看输入框", pg.is_visible("#utSpec"))
        check("示例填的是真协议串",
              (pg.input_value("#utSpec") or "").startswith("jiliu://"),
              pg.input_value("#utSpec"))
        pg.screenshot(path=str(SHOTS / "wake-1-settings.png"), full_page=True)

        # ---- 立即注册 ----
        state_before = pg.inner_text("#drawerBody")
        pg.click("button[data-ut=register]")
        pg.wait_for_timeout(1800)
        body2 = pg.inner_text("#drawerBody")
        check("点注册后状态变了", body2 != state_before, "文案没变")
        # /api/state 有 3 秒状态缓存 + 2 秒轮询，给它一点时间；
        # 直接断言会在"刚注册完、轮询还没跑到"的窗口里误判成失败。
        try:
            pg.wait_for_function(
                "() => (STATE?.urltake?.status?.registered) === true", timeout=6000)
            reg_ok = True
        except Exception:      # noqa: BLE001
            reg_ok = False
        check("STATE 里的注册状态已刷新", reg_ok,
              pg.evaluate("JSON.stringify(STATE?.urltake?.status || {})"))
        pg.wait_for_timeout(300)
        body2 = pg.inner_text("#drawerBody")
        check("界面显示已注册", "未注册" not in body2.split("注册状态")[-1][:40],
              body2.split("注册状态")[-1][:80])
        check("显示了指向的命令行", "--open-url" in body2, body2[-200:])
        pg.screenshot(path=str(SHOTS / "wake-2-registered.png"), full_page=True)

        # ---- 只解析 ----
        spec = ("jiliu://download?url=https%3A%2F%2Fexample.com%2Fa.zip"
                "&referer=https%3A%2F%2Fexample.com%2Fp&title=%E9%AA%8C%E8%AF%81")
        pg.fill("#utSpec", spec)
        pg.click("button[data-ut=parse]")
        pg.wait_for_selector("#anyMask.show", timeout=6000)
        anytxt = pg.inner_text("#anyBody")
        check("解析弹窗打开了", "链接" in anytxt, anytxt[:120])
        check("弹窗里有完整链接", "example.com/a.zip" in anytxt, anytxt[:200])
        check("弹窗里有来源页", "example.com/p" in anytxt, anytxt[:260])
        check("弹窗里有标题", "验证" in anytxt, anytxt[:260])
        pg.screenshot(path=str(SHOTS / "wake-3-parse.png"), full_page=True)
        pg.evaluate("document.getElementById('anyMask').classList.remove('show')")
        pg.wait_for_timeout(200)

        # ---- 取消注册（还原） ----
        pg.click("button[data-ut=unregister]")
        pg.wait_for_timeout(1800)
        body3 = pg.inner_text("#drawerBody")
        check("取消后回到未注册", "未注册" in body3, body3.split("注册状态")[-1][:60])

        # ---- 非法链接要有明确提示 ----
        pg.fill("#utSpec", "jiliu://download?url=javascript%3Aalert%281%29")
        pg.click("button[data-ut=parse]")
        pg.wait_for_timeout(1200)
        toast = ""
        try:
            toast = pg.inner_text("#toast")
        except Exception:      # noqa: BLE001
            pass
        check("非法链接有提示（不是静默失败）", "不支持" in toast or "失败" in toast, toast)

        # 故意测非法链接会拿到 400，浏览器会记一条"Failed to load resource"。
        # 这是预期内的，不算前端报错。
        real_errors = [e for e in errors
                       if "favicon" not in e.lower()
                       and not ("400" in e and "Failed to load resource" in e)]
        check("控制台无报错", not real_errors, real_errors[:2])

        b.close()

    print(f"\n结果: {ok}/{ok + fail} 通过")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
