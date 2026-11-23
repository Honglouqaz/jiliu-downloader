#!/usr/bin/env python3
"""URL 协议唤起（urltake）的回归测试。

这一组守四件事：

  1) **协议串解析不能丢参数**：链接本身带 `?` 和 `&`，不 percent 编码
     就会被协议串自己的 query 解析吃掉后半段 —— 症状是"签名地址被截断，
     下载 403"，而看着像别的问题。
  2) **外部输入必须白名单**：`jiliu://` 是任何网页都能点的，
     `javascript:` / `data:` 这类必须拒掉。
  3) **单实例判定不能误判**：runtime 文件是崩溃残留时，照它投递会等满
     超时；pid 死了要立刻认出来，否则冷启动白慢 8 秒。
  4) **注册表项的结构**：`URL Protocol` 这个空值少写，注册表看着有项、
     点链接却没反应 —— 最难查的一种。

不启动窗口、不发网络请求（deliver 的失败路径用死端口验证）。
真实唤起的端到端验证由 scripts/verify_urltake.py 负责。

    python tests/test_urltake.py
"""
# -*- coding: utf-8 -*-
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import urllib.parse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 隔离状态目录：这一步必须在 import jiliu 之前，因为 config.HOME 是模块级常量
_TMP = tempfile.mkdtemp(prefix="jiliu-urt-")
os.environ["JILIU_HOME"] = _TMP

from jiliu import urltake as u  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {name}")
    else:
        fail += 1
        print(f"  FAIL {name}   {extra}")


# ================================================================ 1 解析
print("\n[1] 协议串解析")

spec = u.build("https://cdn.a.com/a.zip?x=1&y=2",
               referer="https://site.com/page?a=1&b=2", title="某 标题")
p = u.parse(spec)
check("build→parse 往返", p["ok"], p)
check("query 里的 & 没被截断", p["url"] == "https://cdn.a.com/a.zip?x=1&y=2", p["url"])
check("referer 完整", p["referer"] == "https://site.com/page?a=1&b=2", p["referer"])
check("中文标题", p["title"] == "某 标题", p["title"])
check("referer 自动进 headers", p["headers"].get("Referer") == p["referer"], p["headers"])

# 签名链接：这类最长最容易出问题
sign = ("https://cdn.b.com/v/1.m3u8?sign=abc123&expire=1999999999"
        "&token=ZZZ%2B%2F%3D")
sp = u.parse(u.build(sign))
check("带签名的长链接一字不差", sp["url"] == sign, sp["url"])
# 裸 + 是 URL 里的合法字符（GitHub release 文件名常有）。
# parse_qs 的表单语义会把它当空格 → 文件名多个空格、改名后对不上服务器。
plus = "https://github.com/o/r/releases/download/v1/a+b+c.zip"
check("裸 + 原样保留", u.parse(u.build(plus))["url"] == plus,
      u.parse(u.build(plus))["url"])
check("手动拼接的裸 + 也保留",
      u.parse("jiliu://download?url=https%3A%2F%2Fa.com%2Fx+y.zip")["url"]
      == "https://a.com/x+y.zip",
      u.parse("jiliu://download?url=https%3A%2F%2Fa.com%2Fx+y.zip")["url"])
check("title 里的 + 仍按空格处理（浏览器就这么编）",
      u.parse("jiliu://download?url=https%3A%2F%2Fa.com%2Fx.zip&title=A+B")["title"]
      == "A B")

check("opaque 形式 jiliu:https://…",
      u.parse("jiliu:https://e.com/b.mp4")["url"] == "https://e.com/b.mp4")
check("双斜杠空主机形式",
      u.parse("jiliu://?url=https%3A%2F%2Fe.com%2Fc.iso")["ok"])
check("裸链接直接收", u.parse("https://e.com/d.iso")["ok"])
check("磁力链接", u.parse("magnet:?xt=urn:btih:" + "a" * 40)["ok"])
check("ed2k 链接", u.parse("ed2k://|file|a.avi|100|ABCDEF|/")["ok"])


# ================================================================ 2 安全
print("\n[2] 外部输入白名单（网页点什么都会传进来）")

for bad in ("javascript:alert(1)", "data:text/html,<script>x</script>",
            "vbscript:msgbox", "file:///C:/Windows/System32/calc.exe"):
    # file: 是白名单里的（右键菜单用），但 javascript/data 必须拒
    if bad.startswith("file:"):
        continue
    r = u.parse(u.build(bad))
    check(f"拒绝 {bad.split(':')[0]}:", not r["ok"], r.get("url"))

check("编码过的 javascript: 也拒",
      not u.parse("jiliu://download?url=javascript%3Aalert%281%29")["ok"])
check("大小写混写也拒",
      not u.parse("jiliu://download?url=JaVaScRiPt%3Aalert%281%29")["ok"])
check("缺 url 参数报错明确",
      "url" in u.parse("jiliu://download")["error"],
      u.parse("jiliu://download")["error"])
check("空串不崩", not u.parse("")["ok"])
check("乱码不崩", not u.parse("jiliu://%zz%")["ok"])


# ================================================================ 3 wake
print("\n[3] 只唤醒（扩展用它拉起应用，不带链接）")

for kw in ("wake", "open", "launch", "WAKE", "wake/"):
    r = u.parse("jiliu://" + kw)
    check(f"jiliu://{kw} 识别为唤醒", r.get("wake") is True and not r["ok"], r)
check("唤醒不带 url（不会误建任务）",
      u.parse("jiliu://wake")["url"] == "", u.parse("jiliu://wake")["url"])
check("普通链接不算唤醒", u.parse(u.build("https://a.com/x.zip"))["wake"] is False)


# ================================================================ 4 runtime
print("\n[4] 运行时端口记录（单实例判定）")

rt = u.write_runtime(18788, os.getpid())
check("写入成功", rt.get("ok"), rt)
got = u.read_runtime()
check("读回来端口对", got and got["port"] == 18788, got)
check("读回来 pid 对", got and got["pid"] == os.getpid(), got)

# pid 已死的残留
dead = u.write_runtime(18789, 999999)
check("写残留成功", dead.get("ok"), dead)
check("pid 死了 → 视为没有实例", u.read_runtime() is None,
      "残留会让冷启动白等一个超时")

# 损坏的 json
u.runtime_path().write_text("{不是 json", encoding="utf-8")
check("内容坏了 → 视为没有实例", u.read_runtime() is None)

# 没有 port
u.runtime_path().write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
check("缺 port → 视为没有实例", u.read_runtime() is None)

# clear 带 pid 的隔离性
u.write_runtime(18790, os.getpid())
u.clear_runtime(123456)          # 假装是别的实例退出
check("别的 pid 退出不删我的记录", u.read_runtime() is not None)
u.clear_runtime(os.getpid())
check("自己的 pid 才删", u.read_runtime() is None)


# ================================================================ 5 投递
print("\n[5] 投递与单实例交接")

# scan=False：本机可能真的开着汲流（8788），端口扫描会命中，
# 那样就永远测不到「没有实例」这条冷路径。
check("没有实例 → no-instance",
      u.handoff({"ok": True, "url": "https://a.com/x.zip"}, scan=False)["reason"] == "no-instance")

res = u.deliver({"ok": True, "url": "https://a.com/x.zip"}, 1, timeout=1.0)
check("端口没人听 → 失败但不抛", res.get("ok") is False, res)
check("失败带错误信息", bool(res.get("error")), res)

check("ping 死端口 = False", u.ping(1, timeout=0.5) is False)
check("ping 保留端口不抛", isinstance(u.ping(18799, timeout=0.3), bool))

check("无效链接直接拒（不尝试投递）",
      u.deliver({"ok": False, "error": "x"}, 18788)["error"] == "x")

pl = u.payload_of({"ok": True, "url": "https://a.com/x.zip",
                   "referer": "https://a.com/p", "title": "T", "kind": "video",
                   "headers": {"Referer": "https://a.com/p"}})
check("payload 带 url", pl["url"] == "https://a.com/x.zip", pl)
check("payload 标记来源", pl["from"] == "protocol", pl)
check("payload 带 headers", pl["headers"]["Referer"] == "https://a.com/p", pl)
check("payload 带 title/kind", pl["title"] == "T" and pl["kind"] == "video", pl)


# ================================================================ 6 注册表
print("\n[6] 注册表（HKCU，不需要管理员）")

if os.name != "nt":
    check("非 Windows 优雅降级", u.status()["supported"] is False)
else:
    import winreg

    # 关键：**绝不碰真的 jiliu:// 键**。
    # 上一版直接操作 CLASS_KEY，收尾只在"本来没注册"时才兜底 ——
    # 于是每跑一次测试，本机的协议注册就没了，点网页链接彻底没反应。
    # 现在整个这段挂到 jiliu__test 上，跑完删干净，与真实注册项无关。
    _real_before = u.status()["registered"]
    u._KEY_SUFFIX = "__test"
    u._STATUS_CACHE.update(at=0.0, data=None)
    try:
        r = u.register()
        check("注册成功", r.get("ok"), r)
        st = u.status()
        check("status 说已注册", st["registered"], st)
        check("命令里带 --open-url", u.FLAG in st["command"], st["command"])
        check("命令里带 %1 占位", '"%1"' in st["command"], st["command"])
        check("指向的就是当前 exe", not st["stale"], st)

        # URL Protocol 这个空值是关键：少了它点链接没反应
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, u._key()) as k:
            v, _ = winreg.QueryValueEx(k, "URL Protocol")
        check("URL Protocol 空值存在", v == "", repr(v))

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            u._key("DefaultIcon")) as k:
            check("DefaultIcon 指到 exe", "jiliu" in str(winreg.QueryValueEx(k, "")[0]).lower(),
                  winreg.QueryValueEx(k, "")[0])

        # 模拟 exe 换目录：把 JiliuPath 改成假的，应被判 stale
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, u._key(), 0,
                            winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "JiliuPath", 0, winreg.REG_SZ, r"C:\old\jiliu.exe")
        u._STATUS_CACHE.update(at=0.0, data=None)
        check("指向旧路径 → stale", u.status()["stale"], u.status())

        # 注意：这里**不测** refresh_if_registered 的"自动修好"。
        # 它现在只在打包形态下动作，而源码态硬装成 frozen 会连带把
        # exe_path() 也切到 sys.executable，写出来的命令是
        # `python.exe --open-url "%1"`（丢了 jiliu.py），
        # 断言只能得到一条假失败。源码态不动作这件事在 [8] 段验。
        u.unregister()
        check("注销后 status 说未注册", not u.status()["registered"], u.status())
    finally:
        # 测试键删干净，然后回到生产路径
        for p in (u._key(r"shell\open\command"), u._key(r"shell\open"),
                  u._key(r"shell"), u._key("DefaultIcon"), u._key()):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, p)
            except OSError:
                pass
        u._KEY_SUFFIX = ""
        u._STATUS_CACHE.update(at=0.0, data=None)

    # 这条是给"测试自己"设的护栏：跑完之后本机真实的 jiliu:// 必须原样还在。
    # 它不属于被测代码，但正是上一版出过的事 —— 留一条断言比留一段注释管用。
    check("跑完测试，真实的 jiliu:// 注册项没被动过",
          u.status()["registered"] == _real_before,
          f"测试前={_real_before} 测试后={u.status()['registered']}")


# ================================================================ 7 说明文案
print("\n[7] 界面文案来源")

d = u.describe()
check("有 scheme", d["scheme"] == "jiliu://", d.get("scheme"))
check("三种写法示例", len(d["examples"]) == 3, len(d["examples"]))
check("示例都真能解析", all(u.parse(x["text"])["ok"] or "wake" in x["text"]
                            for x in d["examples"]), d["examples"])
check("写了限制", len(d["limits"]) >= 2, d["limits"])
check("带状态", "status" in d, list(d))


# ================================================================ 8 ensure
print("\n[8] 启动时自动注册的开关")

if os.name == "nt":
    # 同 [6]：挂到测试键上。ensure_registered 是"启动时自动注册"的入口，
    # 它写的是真键还是测试键，取决于 _KEY_SUFFIX —— 所以这里也要隔离，
    # 否则每次跑测试都会把用户的 jiliu:// 注销掉再重写一遍。
    #
    # 还要假装自己是打包态：源码态下 ensure_registered 直接跳过（写进去的
    # 会是 python 解释器路径），不装成 frozen 这三条断言就测不到东西。
    import sys as _sys
    _old_frozen = getattr(_sys, "frozen", None)
    _sys.frozen = True                       # type: ignore[attr-defined]
    u._KEY_SUFFIX = "__test"
    u._STATUS_CACHE.update(at=0.0, data=None)
    try:
        u.unregister()
        u._STATUS_CACHE.update(at=0.0, data=None)
        r = u.ensure_registered({"url_protocol_auto": False})
        check("配置关掉 → 不动注册表", r.get("skipped") and not u.status()["registered"], r)
        r = u.ensure_registered({"url_protocol_auto": True})
        check("配置开着 → 注册", r.get("ok") and u.status()["registered"], r)
        r2 = u.ensure_registered({"url_protocol_auto": True})
        check("已注册时不重复写", r2.get("already"), r2)
    finally:
        import winreg as _wr
        for p in (u._key(r"shell\open\command"), u._key(r"shell\open"),
                  u._key(r"shell"), u._key("DefaultIcon"), u._key()):
            try:
                _wr.DeleteKey(_wr.HKEY_CURRENT_USER, p)
            except OSError:
                pass
        u._KEY_SUFFIX = ""
        if _old_frozen is None:
            try:
                del _sys.frozen               # type: ignore[attr-defined]
            except AttributeError:
                pass
        else:
            _sys.frozen = _old_frozen         # type: ignore[attr-defined]
        u._STATUS_CACHE.update(at=0.0, data=None)

    # 源码态必须什么都不做 —— 这是防止"开发机上跑条命令就把用户
    # 的协议注册改成 python 路径"的那道闸门
    r = u.ensure_registered({"url_protocol_auto": True})
    check("源码态不自动注册", r.get("skipped") and r.get("reason") == "source-run", r)
    r = u.refresh_if_registered()
    check("源码态不校正注册表", r.get("skipped") and r.get("reason") == "source-run", r)

# ================================================================ 9 唤起器
print("\n[9] 轻量唤起器（jiliu-wake.exe）")

# 主程序和唤起器解析的是**同一个**参数。两处各写一份实现，分叉的表现
# 是「从网页点能唤起、从命令行点不行」—— 见 urltake.split_argv 的注释。
check("LAUNCHER_NAME 是 exe", u.LAUNCHER_NAME.endswith(".exe"), u.LAUNCHER_NAME)

spec, rest = u.split_argv(["--open-url", "jiliu://download?url=x", "gui"])
check("split：摘出目标", spec == "jiliu://download?url=x", spec)
check("split：其余参数保留", rest == ["gui"], rest)

spec, rest = u.split_argv(["--open-url=jiliu://wake"])
check("split：等号写法", spec == "jiliu://wake", spec)
check("split：等号写法不留尾巴", rest == [], rest)

# --open-url 后面没有值时，不能把下一个参数吃掉，更不能崩
spec, rest = u.split_argv(["--open-url"])
check("split：缺值不崩", spec == "" and rest == ["--open-url"], (spec, rest))
check("split：空参数", u.split_argv([]) == ("", []), u.split_argv([]))
check("split：不误伤普通参数",
      u.split_argv(["add", "http://a/b"]) == ("", ["add", "http://a/b"]),
      u.split_argv(["add", "http://a/b"]))

# 源码态没有唤起器这回事：必须返回空串，让调用方按源码形态处理。
# 要是这儿瞎拼一个路径，注册表会被写成一个不存在的 exe，点链接直接报错。
if not getattr(sys, "frozen", False):
    check("源码态 launcher_path 为空", u.launcher_path() == "", u.launcher_path())
    check("源码态 handler_path 为空", u.handler_path() == "", u.handler_path())
    check("源码态退回 exe_path", u._handler_or_exe() == u.exe_path(),
          u._handler_or_exe())
    check("源码态命令用 python + jiliu.py", "jiliu.py" in u._command(), u._command())

# 冻结态的路径选择：有唤起器就指向它，没有就退回主程序。
# 这一条是这一版的核心决策（热路径从 6.4 秒降到 2.2 秒全靠它），
# 用假的目录树模拟，不依赖真打包产物。
_fake = pathlib.Path(_TMP) / "fakeapp"
_fake.mkdir(parents=True, exist_ok=True)
_main = _fake / "jiliu.exe"
_main.write_text("", encoding="utf-8")
_old_frozen = getattr(sys, "frozen", None)
_old_exec = sys.executable
try:
    sys.frozen = True                      # type: ignore[attr-defined]
    sys.executable = str(_main)
    check("没有唤起器 → 指向主程序",
          u.handler_path() == str(_main.resolve()), u.handler_path())
    check("没有唤起器 → 命令里是主程序", "jiliu.exe" in u._command(), u._command())

    (_fake / u.LAUNCHER_NAME).write_text("", encoding="utf-8")
    check("有唤起器 → 优先它",
          u.handler_path() == str((_fake / u.LAUNCHER_NAME).resolve()),
          u.handler_path())
    check("有唤起器 → 命令指向它", u.LAUNCHER_NAME in u._command(), u._command())
    check("exe_path 仍是主程序（JiliuPath 语义没变）",
          u.exe_path() == str(_main.resolve()), u.exe_path())
    check("DefaultIcon 也用唤起器", u.LAUNCHER_NAME in u._handler_or_exe(),
          u._handler_or_exe())
finally:
    sys.executable = _old_exec
    if _old_frozen is None:
        try:
            del sys.frozen                # type: ignore[attr-defined]
        except AttributeError:
            pass
    else:
        sys.frozen = _old_frozen           # type: ignore[attr-defined]

# ================================================================ 10 端口身份
print("\n[10] 端口身份识别（别把别人的服务当成汲流）")

# 只看 HTTP 200 会把 8788 上任何一个本地工具都当成汲流，任务 POST 过去
# 人家照样回 200，然后就没有然后了。所以必须读 app 字段认身份。
import http.server                     # noqa: E402
import threading                       # noqa: E402


def _serve(payload):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):              # noqa: N802
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    s = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


_s_other = _serve({"ok": True, "app": "someone-else"})
_s_jl = _serve({"ok": True, "app": "汲流", "version": "x"})
_s_jl_en = _serve({"ok": True, "app": "jiliu"})
_s_bad = _serve(b"<html>not json</html>")
_s_no = _serve({"ok": False, "app": "汲流"})
try:
    check("别人的服务 → 不认",
          u.probe_app(_s_other.server_address[1]) is False)
    check("汲流（中文 app）→ 认",
          u.probe_app(_s_jl.server_address[1]) is True)
    check("汲流（英文 app，大小写不敏感）→ 认",
          u.probe_app(_s_jl_en.server_address[1]) is True)
    check("回的不是 JSON → 不认",
          u.probe_app(_s_bad.server_address[1]) is False)
    check("ok=false → 不认",
          u.probe_app(_s_no.server_address[1]) is False)

    # 找个没人听的端口（先绑定再关，端口短时间内不会被别人抢）
    import socket                    # noqa: E402
    _t = socket.socket()
    _t.bind(("127.0.0.1", 0))
    _free = _t.getsockname()[1]
    _t.close()
    check("端口没人听 → 不认（且不抛异常）", u.probe_app(_free, timeout=0.2) is False)
finally:
    for _s in (_s_other, _s_jl, _s_jl_en, _s_bad, _s_no):
        _s.shutdown()


shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n结果: {ok}/{ok + fail} 通过")
sys.exit(1 if fail else 0)
