#!/usr/bin/env python3
"""解析插件机制回归（P2-13）。

    python tests/test_plugins.py

插件是用户自己写的代码，所以这里的重点不是「功能对不对」，
而是「插件怎么烂都不能带崩主程序」：语法错、抛异常、卡死、
写个死循环 —— 四种情况逐一验。
"""
import io
import os
import pathlib
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import config as _config, plugins  # noqa: E402

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] " + label + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] " + label + (f"  -- {detail}" if detail else ""))


TMP = pathlib.Path(tempfile.mkdtemp(prefix="jiliu-plg-"))
HOME = TMP / "home"
(HOME / "plugins").mkdir(parents=True)

# 把 HOME 指到临时目录，别污染主人的真实插件目录
_orig_home = _config.HOME
_config.HOME = HOME

CFG = dict(_config.DEFAULTS)
CFG["plugins_enabled"] = True
CFG["plugins_disabled"] = []


def write(name, body):
    p = HOME / "plugins" / name
    p.write_text(body, encoding="utf-8")
    return p


print("== 空目录 ==")
r = plugins.load(CFG, force=True)
check("没有插件时返回空列表", r == [], str(r))
check("空目录下 resolve 返回 None", plugins.resolve("https://x.com/a", CFG) is None)

print("== 正常插件 ==")
write("good.py", '''
NAME = "示例站点"
VERSION = "1.2"
DESCRIPTION = "把 /page/xx 解析成直链"
DOMAINS = ["example.com"]

def match(url):
    return "/page/" in url

def resolve(url, cfg):
    n = url.rsplit("/", 1)[-1]
    return {"url": f"https://cdn.example.com/{n}.mp4",
            "urls": [f"https://cdn2.example.com/{n}.mp4"],
            "filename": f"{n}.mp4",
            "headers": {"Referer": "https://example.com/"},
            "meta": {"plugin_tag": "demo"}}

def on_event(event, task):
    EVENTS.append(event)

EVENTS = []
''')
recs = plugins.load(CFG, force=True)
check("加载到 1 个插件", len(recs) == 1, str(len(recs)))
g = recs[0]
check("读到了 NAME", g["name"] == "示例站点", g["name"])
check("读到了 VERSION", g["version"] == "1.2", g["version"])
check("读到了 DOMAINS", g["domains"] == ["example.com"], str(g["domains"]))
check("hooks 齐全", set(g["hooks"]) == {"match", "resolve", "on_event"}, str(g["hooks"]))
check("状态 ok", g["ok"] is True and not g["error"], str(g.get("error")))

hit = plugins.resolve("https://example.com/page/42", CFG)
check("命中插件", hit is not None and hit["plugin"] == "good",
      str(hit and hit.get("plugin")))
check("直链正确", hit["url"] == "https://cdn.example.com/42.mp4", hit["url"])
check("镜像收好", hit["mirrors"] == ["https://cdn2.example.com/42.mp4"], str(hit["mirrors"]))
check("文件名带过来", hit["filename"] == "42.mp4", hit["filename"])
check("请求头带过来", hit["meta"]["headers"]["Referer"] == "https://example.com/")
check("自定义 meta 也在", hit["meta"]["plugin_tag"] == "demo")

print("== 域名过滤 ==")
check("别的域名不认领", plugins.resolve("https://other.com/page/1", CFG) is None)
check("match 说不认就不认", plugins.resolve("https://example.com/other/1", CFG) is None)

print("== 事件 ==")
n = plugins.emit("done", {"id": "x"}, CFG)
check("事件送达", n == 1, str(n))
mod = sys.modules.get(plugins.MODULE_PREFIX + "good")
check("插件真的收到了事件", getattr(mod, "EVENTS", []) == ["done"],
      str(getattr(mod, "EVENTS", None)))

print("== 坏插件：语法错 ==")
write("broken.py", "def resolve(url, cfg)\n    return {}\n")
recs = plugins.load(CFG, force=True)
b = [x for x in recs if x["key"] == "broken"][0]
check("语法错被记下来", b["ok"] is False and "SyntaxError" in b["error"], b["error"][:40])
check("坏插件不影响好插件", any(x["ok"] and x["key"] == "good" for x in recs))
check("坏插件不参与 resolve",
      plugins.resolve("https://example.com/page/1", CFG) is not None)

print("== 坏插件：运行时抛异常 ==")
write("boom.py", '''
NAME = "会炸的"
DOMAINS = ["boom.com"]

def resolve(url, cfg):
    raise RuntimeError("插件自己炸了")
''')
plugins.load(CFG, force=True)
check("抛异常时不认领", plugins.resolve("https://boom.com/x", CFG) is None)
b2 = [x for x in plugins._cache if x["key"] == "boom"][0]
check("异常被记账", b2["errors"] >= 1 and "插件自己炸了" in b2["last_error"],
      b2.get("last_error", ""))
check("主程序还活着", plugins.resolve("https://example.com/page/2", CFG) is not None)

print("== 坏插件：卡死 ==")
write("slow.py", '''
NAME = "卡死"
DOMAINS = ["slow.com"]

def resolve(url, cfg):
    import time as _t
    _t.sleep(5)
    return {"url": "https://never.example/x"}
''')
plugins.load(CFG, force=True)
old = plugins.RESOLVE_TIMEOUT
plugins.RESOLVE_TIMEOUT = 0.3
t0 = time.monotonic()
got = plugins.resolve("https://slow.com/x", CFG)
cost = time.monotonic() - t0
plugins.RESOLVE_TIMEOUT = old
check("卡死会被超时打断", got is None and cost < 2.0, f"{cost:.2f}s")
s = [x for x in plugins._cache if x["key"] == "slow"][0]
check("超时记在 last_error", "超时" in (s.get("last_error") or ""), s.get("last_error", ""))

print("== 停用与重新加载 ==")
res = plugins.set_enabled("good", False, CFG)
check("停用成功", res["ok"] is True and res["enabled"] is False)
check("停用后不认领", plugins.resolve("https://example.com/page/3", CFG) is None)
check("停用项进了配置", "good" in (CFG.get("plugins_disabled") or []),
      str(CFG.get("plugins_disabled")))
plugins.set_enabled("good", True, CFG)
check("重新启用后又认领", plugins.resolve("https://example.com/page/4", CFG) is not None)

# 改代码 → 重新加载 → 新逻辑生效
write("good.py", '''
NAME = "示例站点v2"
VERSION = "2.0"
DOMAINS = ["example.com"]

def match(url):
    return True

def resolve(url, cfg):
    return {"url": "https://cdn.example.com/v2.mp4"}
''')
plugins.reload(CFG)
hit2 = plugins.resolve("https://example.com/page/9", CFG)
check("改完代码立刻生效", hit2 and hit2["url"].endswith("v2.mp4"),
      str(hit2 and hit2["url"]))
check("新版本号读到了",
      any(x["version"] == "2.0" for x in plugins.list_plugins(CFG)),
      str([x.get("version") for x in plugins.list_plugins(CFG)]))

print("== 总开关 ==")
off = dict(CFG)
off["plugins_enabled"] = False
check("总开关关掉就不认领", plugins.resolve("https://example.com/page/5", off) is None)
check("总开关关掉也不广播事件", plugins.emit("done", {}, off) == 0)

print("== 没有 resolve 的插件 ==")
write("only_event.py", '''
NAME = "只听事件"
def on_event(event, task):
    pass
''')
recs = plugins.load(CFG, force=True)
oe = [x for x in recs if x["key"] == "only_event"][0]
check("仍然算加载成功", oe["ok"] is True)
check("提示缺 resolve", "resolve" in (oe.get("error") or ""), oe.get("error", ""))
check("hooks 只有 on_event", oe["hooks"] == ["on_event"], str(oe["hooks"]))

print("== 接进 manager ==")
from jiliu.manager import TaskManager   # noqa: E402
from jiliu import queues as _q          # noqa: E402

write("good.py", '''
NAME = "示例站点"
DOMAINS = ["example.com"]

def resolve(url, cfg):
    return {"url": "https://cdn.example.com/direct.mp4",
            "filename": "direct.mp4",
            "headers": {"Referer": "https://example.com/"},
            "meta": {"from_plugin": True}}
''')
plugins.load(CFG, force=True)
mcfg = dict(CFG)
mcfg["queues"] = _q.normalize(mcfg)
tm = TaskManager(cfg=mcfg, autoload=False)
t = tm.create_http("https://example.com/page/77", auto_classify=False)
check("URL 被改写成直链", t.url == "https://cdn.example.com/direct.mp4", t.url)
check("文件名用了插件给的", t.filename == "direct.mp4", t.filename)
check("请求头落库", (t.meta.get("headers") or {}).get("Referer") == "https://example.com/")
check("meta 标了插件来源", t.meta.get("plugin") == "示例站点", str(t.meta.get("plugin")))
check("插件自己的 meta 也在", t.meta.get("from_plugin") is True)
check("不会被重复问一次", (t.meta or {}).get("plugin_done") is True)

# 插件不认的链接，行为必须和没有插件时一样
t2 = tm.create_http("https://nothing.example.net/a.bin", auto_classify=False)
check("未命中时保持原样", t2.url == "https://nothing.example.net/a.bin", t2.url)
check("未命中时不写 plugin 字段", "plugin" not in (t2.meta or {}))
tm.shutdown()

print("== status / describe ==")
st = plugins.status(CFG)
check("status 有插件目录", pathlib.Path(st["dir"]).name == "plugins", st["dir"])
check("status 统计了总数", st["total"] >= 3, str(st["total"]))
check("status 不泄露 module 对象",
      all("module" not in p for p in st["plugins"]))
d = plugins.describe(CFG)
check("describe 列了事件", "done" in d["events"], str(d["events"]))

_config.HOME = _orig_home
print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
