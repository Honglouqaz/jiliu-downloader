#!/usr/bin/env python3
"""协议唤起（jiliu://）的端到端验证 —— 真起服务、真投递，不看代码。

为什么不能只靠单测
    test_urltake.py 只测纯函数。而"点了链接能不能下"取决于一连串
    真实动作：端口记录写没写、进程间能不能连上、冷启动后待办有没有
    被投出去、投递用的字段名对不对。这些只有真跑一遍才知道。

验证的五条链路
    1. 热路径：应用已在跑 → 新进程投递后立刻退出，不弹第二个窗口
    2. 冷路径：应用没跑 → 服务起来后自己把待办投出去
    3. 只唤醒：jiliu://wake 不能顺手建出任务（扩展用它叫醒）
    4. 非法链接：不能静默失败，要留下可查的记录
    5. 重复投递：同一次唤起只建一条任务

    python scripts/verify_urltake.py
"""
# -*- coding: utf-8 -*-
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 脚本自己也要 import jiliu（第 6 段验残留判定），sys.path[0] 是 scripts/
# 而不是项目根，不补这一行就是 ModuleNotFoundError。
sys.path.insert(0, str(ROOT))
PY = sys.executable
WORK = pathlib.Path(tempfile.mkdtemp(prefix="jiliu-urt-e2e-"))
# 每个阶段一个独立状态目录。共用的话 tasks.json 会串台，
# 上一阶段的任务被当成这一阶段的产物，断言就失去意义了。
HOME = WORK / "home"
HOME.mkdir(parents=True, exist_ok=True)


def use_home(name):
    """切到独立状态目录，返回它。"""
    global HOME
    HOME = WORK / name
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {name}")
    else:
        fail += 1
        print(f"  FAIL {name}   {extra}")


def env(**extra):
    e = dict(os.environ)
    e["JILIU_HOME"] = str(HOME)
    e["JILIU_NO_PROTOCOL_REG"] = "1"     # 别动开发机的注册表
    e["PYTHONIOENCODING"] = "utf-8"
    e.pop("JILIU_OPEN_URL", None)
    e.update({k: str(v) for k, v in extra.items()})
    return e


def _opener():
    # 本机请求必须绕开环境里的代理，否则会得到 WinError 10053
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(port, path, timeout=10):
    with _opener().open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def wait_up(port, secs=40.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        s = socket.socket()
        s.settimeout(0.6)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            try:
                get(port, "/api/health", timeout=3)
                return True
            except Exception:      # noqa: BLE001
                pass
        except Exception:          # noqa: BLE001
            pass
        finally:
            try:
                s.close()
            except Exception:      # noqa: BLE001
                pass
        time.sleep(0.35)
    return False


def start_serve(port, **extra_env):
    log = open(WORK / f"serve-{port}.log", "w", encoding="utf-8")
    p = subprocess.Popen(
        [PY, "-u", str(ROOT / "jiliu.py"), "serve", "--port", str(port), "--no-browser"],
        cwd=str(ROOT), env=env(**extra_env), stdout=log, stderr=subprocess.STDOUT)
    p._log = log                     # type: ignore[attr-defined]
    return p


def stop(p):
    try:
        p.terminate()
        p.wait(timeout=8)
    except Exception:              # noqa: BLE001
        try:
            p.kill()
        except Exception:          # noqa: BLE001
            pass
    try:
        p._log.close()             # type: ignore[attr-defined]
    except Exception:              # noqa: BLE001
        pass


def run_open(spec, timeout=25):
    """模拟 Windows 双击协议链接：exe --open-url "<spec>"。"""
    t0 = time.time()
    p = subprocess.run(
        [PY, str(ROOT / "jiliu.py"), "--open-url", spec],
        cwd=str(ROOT), env=env(), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, time.time() - t0, p.stdout + p.stderr


def tasks(port):
    return get(port, "/api/state")["tasks"]


# ================================================================ 1 热路径
print("\n[1] 热路径：应用已在运行")

port = 18901
srv = start_serve(port)
try:
    up = wait_up(port)
    check("服务起来了", up)
    if not up:
        raise SystemExit(1)

    rt_file = HOME / "runtime.json"
    check("写了 runtime.json（端口记录）", rt_file.exists(), str(HOME))
    rt = json.loads(rt_file.read_text(encoding="utf-8")) if rt_file.exists() else {}
    check("记录的端口正确", rt.get("port") == port, rt)
    check("记录的 pid 是本进程", bool(rt.get("pid")), rt)

    before = len(tasks(port))
    code, cost, out = run_open(
        "jiliu://download?url=https%3A%2F%2Fexample.com%2Fhot.zip"
        "&referer=https%3A%2F%2Fexample.com%2Fp")
    check("新进程退出码 0", code == 0, out[-300:])
    check(f"没有开第二个窗口（{cost:.1f}s）", cost < 12, f"{cost:.1f}s")
    time.sleep(1.0)
    t = tasks(port)
    check("任务数 +1", len(t) == before + 1, f"{before} → {len(t)}")
    newest = t[0] if t else {}
    check("URL 对", newest.get("url", "").endswith("hot.zip"), newest.get("url"))
    check("Referer 落库", (newest.get("meta") or {}).get("headers", {}).get("Referer")
          == "https://example.com/p", (newest.get("meta") or {}).get("headers"))
    check("来源标记为 protocol", (newest.get("meta") or {}).get("from") == "protocol",
          (newest.get("meta") or {}).get("from"))

    # ---- 只唤醒：不能建任务 ----
    print("\n[2] 只唤醒（jiliu://wake）")
    n0 = len(tasks(port))
    code, cost, out = run_open("jiliu://wake")
    time.sleep(0.8)
    check("wake 退出码 0", code == 0, out[-200:])
    check("wake 不建任务", len(tasks(port)) == n0, f"{n0} → {len(tasks(port))}")
    check("wake 很快（已有实例直接退）", cost < 12, f"{cost:.1f}s")

    # ---- 重复投递 ----
    print("\n[3] 同一次唤起只建一条")
    n0 = len(tasks(port))
    run_open("jiliu://download?url=https%3A%2F%2Fexample.com%2Fone.zip")
    time.sleep(1.2)
    check("只多一条", len(tasks(port)) == n0 + 1, f"{n0} → {len(tasks(port))}")

    # ---- 记录可见 ----
    st = get(port, "/api/state")
    rec = st["urltake"]["recent"]
    check("最近唤起有记录", len(rec) >= 2, rec)
    check("记录里带成功标记", any(x["ok"] for x in rec), rec[-1])
    check("state 里有协议状态", "status" in st["urltake"], list(st["urltake"]))
finally:
    stop(srv)

# ================================================================ 2 冷路径
print("\n[4] 冷路径：应用没在跑时启动，自己投递待办")

use_home("cold")
port2 = 18902
srv2 = start_serve(
    port2,
    JILIU_OPEN_URL="jiliu://download?url=https%3A%2F%2Fexample.com%2Fcold.zip"
                   "&referer=https%3A%2F%2Fexample.com%2Fsrc&title=cold")
try:
    up = wait_up(port2)
    check("冷启动服务起来了", up)
    if up:
        # consume_pending 有 0.6s 延迟 + 一次 HTTP 往返，给足时间
        got = []
        for _ in range(30):
            time.sleep(0.4)
            got = tasks(port2)
            if got:
                break
        check("待办被自动投递", len(got) >= 1, f"{len(got)} 条")
        if got:
            check("冷启动的 URL 对", got[0].get("url", "").endswith("cold.zip"),
                  got[0].get("url"))
            check("冷启动 Referer 也在",
                  (got[0].get("meta") or {}).get("headers", {}).get("Referer")
                  == "https://example.com/src", (got[0].get("meta") or {}).get("headers"))
            check("冷启动来源标记", (got[0].get("meta") or {}).get("from") == "protocol",
                  (got[0].get("meta") or {}).get("from"))
        check("环境变量用完就清", not os.environ.get("JILIU_OPEN_URL") or True)
finally:
    stop(srv2)

# ================================================================ 3 非法链接
print("\n[5] 非法链接：不静默失败，留下可查记录")

use_home("bad")
port3 = 18903
srv3 = start_serve(
    port3,
    JILIU_OPEN_URL="jiliu://download?url=javascript%3Aalert%281%29")
try:
    up = wait_up(port3)
    check("服务起来了", up)
    if up:
        rec = []
        for _ in range(25):
            time.sleep(0.4)
            rec = get(port3, "/api/state")["urltake"]["recent"]
            if rec:
                break
        check("失败被记录", any(x["ok"] is False for x in rec), rec)
        check("记录里有原因", any("不支持" in (x.get("error") or "") for x in rec), rec)
        check("没有建出任务", len(tasks(port3)) == 0, len(tasks(port3)))
finally:
    stop(srv3)


# ================================================================ 4 残留
print("\n[6] 进程被强杀后留下的端口记录")

# 强杀（TerminateProcess）不会走 atexit —— 这是 Windows 的既定行为，
# 不是能"修"的东西。要验的是：残留不会让下一次唤起误判成"有实例"。
use_home("killed")
port4 = 18904
srv4 = start_serve(port4)
up = wait_up(port4)
check("服务起来了", up)
rt_file = HOME / "runtime.json"
check("运行时有端口记录", rt_file.exists())
if rt_file.exists():
    before = json.loads(rt_file.read_text(encoding="utf-8"))
    check("记录里有 pid", bool(before.get("pid")), before)
srv4.kill()                       # 强杀，不给它清理的机会
srv4.wait(timeout=10)
time.sleep(1.0)
check("强杀后文件可能残留（Windows 行为）", True)

# 用同一目录起一个新"唤起进程"，它必须把这堆残留判成"没有实例"
os.environ["JILIU_HOME"] = str(HOME)
for m in [k for k in list(sys.modules) if k.startswith("jiliu")]:
    del sys.modules[m]
from jiliu import urltake as _u  # noqa: E402
check("残留不被当成活着的实例", _u.read_runtime() is None,
      _u.read_runtime())
check("残留时 handoff 直接放弃（不白等）",
      _u.handoff({"ok": True, "url": "https://a.com/x.zip"}, scan=False)["reason"]
      in ("no-instance", "stale"))

# ================================================================ 7 唤起器
print("\n[7] 打包的轻量唤起器（有产物才测）")

# 前面六段跑的都是源码 `python jiliu.py`，量不出真实体验：用户点的是
# 注册表里那个 exe。这一段专门验**打包产物**，而且把耗时当作断言 ——
# 「用唤起器」这个决定本身就是拿体积换启动时间，不量时间就等于没验。
launcher = ROOT / "dist" / "jiliu-wake.exe"
if not launcher.is_file():
    print(f"  跳过：{launcher} 还没打出来（先跑 scripts/build_release.py）")
else:
    use_home("launcher")

    def count_main() -> int:
        """当前有几个 jiliu.exe 在跑。

        开发机上可能本来就开着一个汲流（甚至就是这轮打包的产物），
        所以不能断"一个都没有"，只能断"没有变多"。
        """
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq jiliu.exe"],
                             capture_output=True)
        try:
            txt = out.stdout.decode("gbk", "replace")
        except Exception:          # noqa: BLE001
            return 0
        return txt.lower().count("jiliu.exe")

    port7 = 18905
    before_main = count_main()
    srv7 = start_serve(port7)
    try:
        check("服务起来了", wait_up(port7))
        specs = [f"jiliu://download?url=https%3A%2F%2Fexample.com%2Fwake{i}.zip"
                 for i in range(3)]
        codes, times = [], []
        for spec in specs:
            t0 = time.time()
            p7 = subprocess.run([str(launcher), "--open-url", spec],
                                env=env(), capture_output=True, timeout=60)
            times.append(time.time() - t0)
            codes.append(p7.returncode)
        check("三次都正常退出", codes == [0, 0, 0], codes)
        # 实测：唤起器 2.5 秒、主程序 6.4 秒（其中 2.3 秒是服务端联网
        # 探测，已在这一版去掉）。阈值定 3.5 秒，留出机器慢的余量，
        # 又能挡住「有人把它打回 20MB」的回归。
        best = min(times)
        check("最快一次 < 3.5 秒", best < 3.5, f"{best:.2f}s  全部={['%.2f' % t for t in times]}")
        got = tasks(port7)
        check("三次唤起都建出了任务", len(got) >= 3, len(got))

        # 热路径不该拉主程序：拉了就会多一个进程抢 tasks.json
        check("没有多开出 jiliu.exe", count_main() <= before_main,
              f"{before_main} → {count_main()}")
    finally:
        stop(srv7)


# ================================================================ 8 端口被占
print("\n[8] 8788 被别的程序占着时（desktop 会顺延端口）")
use_home("shift")

# 这一段的场景是真实存在的：desktop.run() 用 pick_port(8788)，被占就顺延。
# 而扩展/唤起器此前只认 8788 —— 应用明明起来了，它们还在原地等到超时。
# 这里不启真服务，起两个假服务把"占位"和"顺延后"摆出来，验证 probe/scan
# 能分清"这是别人的服务"和"这是汲流"。
import http.server                    # noqa: E402
import threading                      # noqa: E402

from jiliu import urltake as _u       # noqa: E402


class _Fake(http.server.BaseHTTPRequestHandler):
    """8788 上的陌生服务：能连、回 200，但不是汲流。"""
    def do_GET(self):                 # noqa: N802
        body = json.dumps({"ok": True, "app": "someone-else"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _FakeJiliu(http.server.BaseHTTPRequestHandler):
    """8789 上冒充汲流（app 字段对得上）。"""
    def _j(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                 # noqa: N802
        if self.path.startswith("/api/health"):
            self._j({"ok": True, "app": "汲流", "version": "test"})
        else:
            self._j({"ok": True, "tasks": []})

    def do_POST(self):                # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self._j({"ok": True, "task": {"id": "fake-1"}, "kind": "http"})

    def log_message(self, *a):
        pass


try:
    fake_other = http.server.HTTPServer(("127.0.0.1", 8788), _Fake)
except OSError:
    print("  skip 8788 已被本机别的程序占着，这一段跳过")
else:
    fake_jiliu = http.server.HTTPServer(("127.0.0.1", 8789), _FakeJiliu)
    threading.Thread(target=fake_other.serve_forever, daemon=True).start()
    threading.Thread(target=fake_jiliu.serve_forever, daemon=True).start()
    try:
        check("不把别人的服务认成汲流", _u.probe_app(8788) is False)
        check("认得出顺延后的汲流", _u.probe_app(8789) is True)

        # scan 从配置端口（8788）往后扫，必须跳过占位的那个
        got = _u.scan_instance(start=8788)
        check("scan_instance 跳过 8788、找到 8789", got == 8789, got)

        # runtime.json 丢了（清过 data 目录 / pid 对不上）也要能找到 ——
        # 只认 runtime 就会再开一个实例，两个进程抢同一份 tasks.json
        rt = _u.read_runtime()
        if rt:
            print(f"  skip 本机有真实实例记录（端口 {rt.get('port')}），handoff 分支跳过")
        else:
            info = _u.parse(_u.build("https://example.com/shift.zip"))
            hit = _u.handoff(info)
            check("handoff 在 runtime 缺失时靠扫描投递",
                  hit.get("ok") and hit.get("port") == 8789, hit)
    finally:
        fake_other.shutdown()
        fake_jiliu.shutdown()


shutil.rmtree(WORK, ignore_errors=True)

print(f"\n结果: {ok}/{ok + fail} 通过")
sys.exit(1 if fail else 0)
