"""量「从打开页面到看见数据」到底花了多久。

用户说的「页面加载慢」不是指 HTML 传输 —— 那 20 毫秒就到了。慢的是
页面在等 /api/state：冷启动第一轮它要问七个外部工具要版本号、问 aria2
要 RPC，五秒上下。这段时间界面上什么都没有。

所以这里量三件事：

    DCL       文档解析完（骨架出来了）
    painted   第一次画到屏幕（performance paint timing）
    data      首屏真的填上数据（顶栏版本号写出来）

用法：
    python scripts/ui_boot_time.py 8985 --label 优化前
    python scripts/ui_boot_time.py 8986 --cold     # 服务刚起来就打开，最坏情况
"""
import json
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "8985"
LABEL = sys.argv[3] if len(sys.argv) > 3 else ""

BASE = f"http://127.0.0.1:{PORT}"


def _warm(cfg_home_hint=""):
    """等服务真的在 listen（不是端口 bind 成功）。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(80):
        try:
            op.open(BASE + "/api/health", timeout=2).read()
            return True
        except Exception:
            time.sleep(0.1)
    return False


def main():
    if not _warm():
        print("服务没起来")
        return 1

    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context(viewport={"width": 1400, "height": 900})

        cold = "--cold" in sys.argv

        # 第一次访问：新配置会弹首启向导。先关掉，之后的重启才有对照意义。
        # --cold 时跳过这一步 —— 预热还没开始就打开页面，是最坏情况。
        if not cold:
            pg = ctx.new_page()
            pg.goto(BASE + "/", wait_until="domcontentloaded")
            pg.wait_for_timeout(400)
            if pg.is_visible("#onbMask.show"):
                pg.click("#onbSkip")
                pg.wait_for_timeout(300)
            pg.close()

        # 正式测量：冷缓存重新加载一次（模拟每次启动打开窗口）
        pg = ctx.new_page()
        t0 = time.time()
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        dcl = (time.time() - t0) * 1000
        if cold and pg.is_visible("#onbMask.show"):
            pg.click("#onbSkip")

        # 顶栏版本号是 refresh() 跑完才写进去的 —— 它就是「首屏有数据」的信号
        pg.wait_for_function(
            "() => {const e=document.querySelector('#ver');"
            "return e && /^v\\d/.test(e.textContent||'');}",
            timeout=60000)
        data_ms = (time.time() - t0) * 1000

        paint = pg.evaluate("""() => {
            const out = {};
            for (const e of performance.getEntriesByType('paint')) {
                out[e.name] = Math.round(e.startTime);
            }
            const nav = performance.getEntriesByType('navigation')[0] || {};
            out.domContentLoaded = Math.round(nav.domContentLoadedEventEnd || 0);
            out.loadEvent = Math.round(nav.loadEventEnd || 0);
            out.transferSize = nav.transferSize || 0;
            out.encodedBodySize = nav.encodedBodySize || 0;
            return out;
        }""")

        # 探测完成（顶栏从「探测中」变成真引擎名）要多久。
        # 这是「数据准确」的时间点，和「页面可用」不是一回事，两个都要量。
        try:
            pg.wait_for_function(
                "() => {const s=window.STATE||{};"
                "return s.engines && s.engines.probing !== true;}",
                timeout=30000)
            probe_ms = (time.time() - t0) * 1000
        except Exception:
            probe_ms = -1

        st = pg.evaluate("""() => {
            const s = window.STATE || {};
            return {
                probing: (s.engines || {}).probing === true,
                engine: (s.engines || {}).effective_http || '',
                rows: (s.tasks || []).length,
                tools: Object.keys(s.tools || {}).length,
            };
        }""")

        print(f"\n=== {LABEL or PORT} ===")
        print(f"  文档解析完        {dcl:7.0f} ms")
        print(f"  首次绘制          {paint.get('first-paint', -1):7.0f} ms")
        print(f"  首次内容绘制      {paint.get('first-contentful-paint', -1):7.0f} ms")
        print(f"  首屏填上数据      {data_ms:7.0f} ms   <-- 用户实际等的时间")
        print(f"  探测结果到位      {probe_ms:7.0f} ms   （占位换成真值）")
        print(f"  HTML 传输/解压后  {paint.get('transferSize', 0)} / {paint.get('encodedBodySize', 0)} bytes")
        print(f"  数据状态          probing={st['probing']} engine={st['engine']} "
              f"tools={st['tools']}")
        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
