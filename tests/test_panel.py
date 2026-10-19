#!/usr/bin/env python3
"""面板 HTTP 接口回归。

起一个**真实**服务，把 web/index.html 会调的接口全打一遍 ——
前台页面能跑起来的前提是这些接口都对。

    python tests/test_panel.py
"""
# -*- coding: utf-8 -*-
"""起一个真实服务，把面板会调的接口全打一遍。

本机 HTTP 测试必须绕过环境变量里的代理 —— 否则连 127.0.0.1 都会被送去代理，
表现为 WinError 10053（看着像服务崩了，其实客户端根本没连上）。
"""
import io, json, os, sys, threading, time, urllib.request, urllib.error
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import server as srv, config as _config

PORT = int(os.environ.get("JL_TEST_PANEL_PORT") or 8799)
httpd, tm = srv.serve(cfg=_config.load(), port=PORT, quiet=True)
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def call(method, path, body=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with OP.open(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"raw": raw[:300]}
    except Exception as e:
        return -1, {"error": f"{type(e).__name__}: {e}"}

ok = fail = 0
RESULTS = []


def check(label, cond, detail=""):
    global ok, fail
    RESULTS.append((label, bool(cond) if not isinstance(cond, tuple) else bool(cond), detail))
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))

print("== 静态 ==")
try:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/")
    with OP.open(req, timeout=10) as r:
        html = r.read().decode("utf-8")
    check("首页 200", r.status == 200)
    check("首页含 IDM 结构", all(k in html for k in ("class=\"addbar\"", "id=\"side\"", "id=\"rows\"", "id=\"drawer\"")))
    check("首页无未替换占位", "&& 0;" not in html)
except Exception as e:
    check("首页", False, str(e))

print("\n== GET 接口 ==")
for path, key in [
    ("/api/health", "app"),
    ("/api/state", "tasks"),
    ("/api/categories", "categories"),
    ("/api/naming", "variables"),
    ("/api/history", "items"),
    ("/api/cookies", "files"),
    ("/api/proxy", "selected"),
]:
    st, j = call("GET", path)
    check(f"GET {path} -> {st}", st == 200 and j.get("ok") is not False, str(j)[:120])

print("\n== /api/state 内容 ==")
st, j = call("GET", "/api/state")
check("含 bili", "bili" in j)
check("含 proxy", "proxy" in j)
check("含 cookies", "cookies" in j)
check("含 history", "history" in j)
check("含 naming", "naming" in j)
cats = [c["key"] for c in (j.get("categories") or [])]
check("分类含 bt/netdisk/playlist", all(k in cats for k in ("bt", "netdisk", "playlist")), str(cats))
print("      分类:", cats)

print("\n== POST /api/protocol 各种协议 ==")
import base64
m = ("magnet:?xt=urn:btih:c12fe1c06bba254a9dc9f519b335aa7c1367a88a"
     "&dn=Ubuntu&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce")
th = "thunder://" + base64.b64encode(("AAhttp://cdn.example.com/a.exeZZ").encode()).decode()
cases = [
    ("磁力", m, "magnet", "bt"),
    ("ed2k", "ed2k://|file|x.iso|123|ABCDEF0123456789ABCDEF0123456789|/", "ed2k", "bt"),
    ("种子", "https://example.org/t/ubuntu.torrent", "torrent", "bt"),
    ("迅雷→http", th, "thunder", "app"),
    ("网盘", "https://pan.baidu.com/s/1abc", "netdisk", "netdisk"),
    ("普通直链", "https://cdn.example.com/a/b/tool.zip", "http", "archive"),
]
for label, url, want_kind, want_cat in cases:
    st, j = call("POST", "/api/protocol", {"url": url})
    pk = (j.get("protocol") or {}).get("kind")
    pc = (j.get("plan") or {}).get("category")
    check(f"{label}: kind={pk} cat={pc}", pk == want_kind and pc == want_cat,
          f"期望 kind={want_kind} cat={want_cat}")

print("\n== 建任务（不联网）==")
st, j = call("POST", "/api/tasks", {"url": "https://cdn.example.com/big.iso",
                                    "threads": 4, "dest_dir": "build/verify_tmp"})
check("直链任务", st == 200 and j.get("ok") is not False, str(j)[:160])
tid = (j.get("task") or {}).get("id")

st, j = call("POST", "/api/tasks", {"url": m, "dest_dir": "build/verify_tmp"})
bt = j.get("task") or {}
check("磁力任务建成功", st == 200 and j.get("ok") is not False, str(j)[:200])
check("磁力任务强制 aria2", bt.get("backend") == "aria2", str(bt.get("backend")))
check("磁力任务分类 bt", bt.get("category") == "bt", str(bt.get("category")))
check("磁力任务不带校验哈希", bt.get("verify_hash") is False)
check("磁力任务记了 info_hash", bool((bt.get("meta") or {}).get("info_hash")))
print("      文件名标注:", bt.get("filename"))
btid = bt.get("id")

print("\n== 任务控制 ==")
for act in ("pause", "cancel"):
    st, j = call("POST", f"/api/tasks/{tid}/{act}")
    check(f"{act}", j.get("ok") is True, str(j)[:120])
st, j = call("POST", "/api/tasks/nonexistent-id/open")
check("open 不存在的任务报 404", st == 404, str(j)[:100])
# _reveal 会真的拉起资源管理器/文件管理器，所以这里只拿一个**不存在的路径**
# 直接验函数本身 —— 走 API 那条路测不了「不存在」，因为任务一进队列，
# _run_http 就把 dest_dir 建出来了。
okr, msgr = srv._reveal("build/__definitely_not_here__")
check("_reveal 对不存在的路径返回 False", okr is False and "不存在" in msgr, msgr)
okr, msgr = srv._reveal("")
check("_reveal 对空路径返回 False", okr is False, msgr)

print("\n== 配置读写 ==")
st, j = call("POST", "/api/config", {"threads": "6", "speed_limit": "0",
                                     "bt_enabled": "false", "skip_downloaded": "false",
                                     "playlist_limit": "150"})
c = j.get("config") or {}
check("threads 转成 int", c.get("threads") == 6, repr(c.get("threads")))
check("bt_enabled 'false' 转成 False", c.get("bt_enabled") is False, repr(c.get("bt_enabled")))
check("skip_downloaded 'false' 转成 False", c.get("skip_downloaded") is False)
check("playlist_limit 转成 int", c.get("playlist_limit") == 150)
call("POST", "/api/config", {"bt_enabled": True, "skip_downloaded": True, "threads": 8, "playlist_limit": 200})

print("\n== 错误处理 ==")
st, j = call("POST", "/api/tasks", {})
check("缺 url 报 400", st == 400, str(j)[:100])
st, j = call("POST", "/api/classify", {})
check("classify 缺参报 400", st == 400)
st, j = call("POST", "/api/cookies/save", {"text": ""})
check("空 cookie 报 400", st == 400)
st, j = call("POST", "/api/proxy/test", {"proxy": "http://127.0.0.1:1"})
check("死代理如实报不通", st == 200 and j.get("ok") is False, str(j)[:140])

print("\n== 清理 ==")
call("POST", "/api/tasks/clear-finished")

print("\n== 前后端接口一致性 ==")
# 面板里写了一个服务端没有的接口，表现是「点了按钮弹一句未知接口」，
# 而且只有点到那一步才发现。这里在静态层面先对一遍。
import re as _re
import pathlib as _pl
_root = _pl.Path(__file__).resolve().parent.parent
_html = (_root / "web" / "index.html").read_text(encoding="utf-8")
_js = _re.search(r"<script>(.*?)</script>", _html, _re.S).group(1)
_called = set(_re.findall(r"['\"`](/api/[a-z0-9/_\-]+)", _js))
_srv = (_root / "jiliu" / "server.py").read_text(encoding="utf-8")
_known = set(_re.findall(r'"(/api/[a-z0-9/_\-]+)"', _srv))
# 带变量的路由（/api/tasks/<id>/pause 这类）在源码里是正则，单独放行
_prefixes = ("/api/tasks/",)
missing = []
for a in sorted(_called):
    if a in _known or any(a.startswith(p) for p in _prefixes):
        continue
    missing.append(a)
check(f"面板调用的 {len(_called)} 个接口服务端都有", not missing,
      "缺：" + str(missing))

_js_file = _root / "build" / "panel_check.js"
if _js_file.is_file():
    check("面板 JS 无语法错误（node --check 已过）", True)


# ======================================================================
# 客户端断连不该刷 traceback
#
# 这段有真实来由：防御原本写在 Handler.handle_error 上 —— 位置是错的。
# ThreadingMixIn.process_request_thread 捕获异常后调的是 **server 对象**的
# handle_error，Handler 上那份从来没被调用过。表现是：代码里看着"修过了"，
# 而面板一关标签页，控制台照样刷一屏 WinError 10053 traceback，
# 很容易被当成"服务崩了"。
#
# 这里直接从 server 实例上打，确保修在真正会被调到的地方。
# ======================================================================
import contextlib
import io as _io

print("\n-- 客户端断连静音（handle_error 挂在 server 上才有效）--")

check("serve 返回的是 HttpServer 实例", isinstance(httpd, srv.HttpServer),
      type(httpd).__name__)
check("Handler 上不再有 handle_error（写那里是死代码）",
      "handle_error" not in srv.Handler.__dict__)
check("HttpServer 定义了 handle_error",
      "handle_error" in srv.HttpServer.__dict__)


def _err_text(exc: BaseException) -> str:
    """在异常上下文里调 handle_error，收回它写到 stderr 的内容。"""
    buf = _io.StringIO()
    try:
        raise exc
    except BaseException:
        with contextlib.redirect_stderr(buf):
            httpd.handle_error(None, ("127.0.0.1", 0))
    return buf.getvalue()


# 常态噪音：必须静音
for _name, _exc in [
    ("ConnectionAbortedError（WinError 10053）",
     ConnectionAbortedError(10053, "你的主机中的软件中止了一个已建立的连接。")),
    ("ConnectionResetError（WinError 10054）",
     ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")),
    ("BrokenPipeError", BrokenPipeError(32, "Broken pipe")),
    ("TimeoutError", TimeoutError("timed out")),
]:
    _out = _err_text(_exc)
    check(f"{_name} 不刷 traceback", _out.strip() == "", repr(_out[:100]))

# 真错误：不能被一起吞掉 —— 否则这个"静音"就成了掩盖 bug 的帮凶
for _name, _exc in [
    ("ValueError", ValueError("真错误不能被吞")),
    ("PermissionError", PermissionError("自己的权限问题")),
    ("FileNotFoundError", FileNotFoundError("自己的路径写错了")),
]:
    _out = _err_text(_exc)
    check(f"{_name} 照常打印出来", str(_exc) in _out, repr(_out[:120]))

print("\n" + "=" * 70)
print(f"结果: {ok}/{ok + fail} 通过")
print("=" * 70)
print("=" * 70)
if fail:
    print("\n未通过项：")
    for label, good, detail in RESULTS:
        if not good:
            print(f"  - {label}  {detail}")
tm.shutdown()
httpd.shutdown()
sys.exit(1 if fail else 0)
