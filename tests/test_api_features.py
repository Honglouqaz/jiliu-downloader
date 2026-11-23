#!/usr/bin/env python3
"""1.8.0 新接口的端到端回归（P0-P2 的服务端出口）。

    python tests/test_api_features.py

真的把服务起来，用 HTTP 打一遍 —— 只测模块函数不算数：
路由没挂上、字段名对不上、JSON 序列化炸掉，这些都只有走一遍才发现。
"""
import io
import json
import os
import pathlib
import sys
import tempfile
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 必须在 import jiliu 之前把家目录指到临时目录，
# 否则测试会读写主人真实的 config.json / tasks.json
_TMP = tempfile.mkdtemp(prefix="jiliu-api-")
os.environ["JILIU_HOME"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import config as _config, server  # noqa: E402

CFG = _config.load()
CFG["port"] = 0
CFG["backend"] = "native"
CFG["auto_start"] = False
CFG["prevent_sleep"] = False
CFG["notify_enabled"] = False
CFG["update_check"] = False
CFG["tray_enabled"] = False

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


print("== 启动服务 ==")
httpd, tm = server.serve(CFG, port=0, quiet=True)
PORT = httpd.server_address[1]
BASE = f"http://127.0.0.1:{PORT}"
# 本机请求必须绕过代理：环境里的 HTTP_PROXY 会把 127.0.0.1 送去代理
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
check("端口已监听", PORT > 0, str(PORT))


def get(path):
    with OPENER.open(BASE + path, timeout=20) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def post(path, body=None):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


try:
    print("== /api/features 全景 ==")
    st, d = get("/api/features")
    check("200", st == 200 and d.get("ok") is True, str(st))
    for key in ("version", "queues", "shell", "finish", "presets", "channels",
                "plugins", "danmaku", "transfer", "live", "stats"):
        check(f"带 {key}", key in d, str(list(d))[:80])
    check("队列有两条", len(d["queues"]) >= 2, str(len(d["queues"])))

    print("== /api/state 也带了新能力 ==")
    st, s0 = get("/api/state")
    check("state 200", st == 200 and s0.get("ok") is True, str(st))
    check("state 带 features", isinstance(s0.get("features"), dict)
          and "queues" in s0["features"], str(list(s0.get("features") or {}))[:60])
    check("state 带主题", (s0.get("theme") or {}).get("tokens", {}).get("bg", "").startswith("#"),
          str(((s0.get("theme") or {}).get("tokens") or {}).get("bg")))
    check("state 带语言", (s0.get("i18n") or {}).get("current") == "zh-CN",
          str((s0.get("i18n") or {}).get("current")))

    print("== 队列 ==")
    st, q = get("/api/queues")
    check("列出队列", st == 200 and len(q["queues"]) >= 2, str(len(q["queues"])))
    check("有并发总数", q["slots"] >= 1, str(q["slots"]))

    st, r = post("/api/queues", {"id": "audio", "name": "音频", "concurrency": 2,
                                "priority": 3, "speed_limit": 1048576})
    check("新建队列成功", r.get("ok") is True, str(r.get("error")))
    st, q = get("/api/queues")
    check("新队列出现", any(x["id"] == "audio" for x in q["queues"]),
          str([x["id"] for x in q["queues"]]))

    st, t = post("/api/tasks", {"url": "https://example.com/a.bin",
                               "filename": "a.bin", "auto_start": False})
    tid = (t.get("task") or {}).get("id") or t.get("id")
    check("建了个任务", bool(tid), str(t)[:120])
    if tid:
        st, r = post(f"/api/tasks/{tid}/queue", {"queue": "audio"})
        check("任务改队列", r.get("ok") is True, str(r.get("error")))
        check("改后队列正确",
              ((r.get("task") or {}).get("queue") == "audio"),
              str((r.get("task") or {}).get("queue")))

        print("== 校验和 ==")
        f = pathlib.Path(_TMP) / "sum.bin"
        f.write_bytes(b"hello jiliu" * 100)
        st, r = get(f"/api/checksum?path={urllib.parse.quote(str(f))}")
        check("算出哈希", st == 200 and len(r.get("sha256") or "") == 64,
              str(r.get("sha256"))[:16])
        st, r = get(f"/api/checksum?path={urllib.parse.quote(str(f))}&algo=md5")
        check("能算 md5", len(r.get("md5") or "") == 32, str(r.get("md5"))[:16])
        # 不存在的文件：走下载目录内的路径（1.9.5 起目录外一律 403，
        # 不再提供任意文件的哈希 oracle）
        ghost = pathlib.Path(_TMP) / "no-such-file.bin"
        st, r = get(f"/api/checksum?path={urllib.parse.quote(str(ghost))}")
        check("文件不存在时不 500", st == 200 or st == 400, str(st))
        # 目录外 403 的回归在 test_security.py（这里的 get() 不接 HTTPError）

    print("== 批量 ==")
    st, r = get("/api/batch/expand?text=" + urllib.parse.quote(
        "https://e.com/f[01-03].bin"))
    check("通配符展开", r.get("ok") is True and r.get("count") == 3,
          str(r.get("count")))
    st, r = post("/api/batch/probe", {"text": "这不是链接"})
    check("空内容也不炸", st == 200 and r.get("count") == 0, str(r)[:80])

    print("== 主题与语言 ==")
    st, r = get("/api/theme")
    check("取主题", st == 200 and "--bg:" in (r.get("css") or ""))
    st, r = post("/api/theme", {"ui_theme": "night", "ui_mode": "dark"})
    check("切深色", r.get("preset") == "night" and r.get("mode") == "dark",
          f"{r.get('preset')}/{r.get('mode')}")
    check("深色底真的暗", r["tokens"]["bg"].lower() < "#808080", r["tokens"]["bg"])
    st, r = get("/api/i18n")
    check("取中文文案", r.get("strings", {}).get("state.done") == "已完成",
          str(r.get("strings", {}).get("state.done")))
    st, r = get("/api/i18n?lang=en-US")
    check("取英文文案", r.get("strings", {}).get("state.done") == "Completed",
          str(r.get("strings", {}).get("state.done")))
    st, r = post("/api/i18n", {"lang": "en"})
    check("切语言并归一", r.get("current") == "en-US", str(r.get("current")))

    print("== 站点预设 ==")
    st, r = post("/api/presets", {"url": "https://www.bilibili.com/video/BV1xx",
                                 "opts": {"qn": 80, "container": "mp4"}})
    check("记住预设", r.get("ok") is True, str(r.get("error")))
    st, r = get("/api/presets")
    check("预设里有 bilibili",
          any(x.get("key") == "bilibili.com" for x in (r.get("presets") or [])),
          str([x.get("key") for x in (r.get("presets") or [])]))
    st, r = post("/api/presets", {"url": "bilibili.com", "forget": True})
    check("忘记预设", r.get("ok") is True, str(r.get("error")))

    print("== 通知渠道 ==")
    st, r = post("/api/channels", {"type": "gotify",
                                  "url": "https://gotify.example.com",
                                  "token": "abcdefghijkl"})
    check("加渠道", r.get("ok") is True, str(r.get("error")))
    st, r = get("/api/channels")
    chs = r.get("channels") or []
    check("渠道已列出", len(chs) == 1, str(len(chs)))
    check("token 已打码", "abcdefghijkl" not in json.dumps(chs, ensure_ascii=False),
          str(chs)[:100])
    cid = chs[0]["id"] if chs else ""
    st, r = post("/api/channels/test", {})
    check("测试通知不 500", st == 200, str(st))
    st, r = post("/api/channels/remove", {"id": cid})
    check("删渠道", r.get("ok") is True, str(r.get("error")))

    print("== 插件 ==")
    st, r = get("/api/plugins")
    check("插件目录存在", pathlib.Path(r["dir"]).is_dir(), r["dir"])
    check("默认没有插件", r["total"] == 0, str(r["total"]))
    (pathlib.Path(r["dir"]) / "demo.py").write_text(
        'NAME = "演示"\nDOMAINS = ["demo.test"]\n'
        'def resolve(url, cfg):\n    return {"url": "https://cdn.test/x.mp4"}\n',
        encoding="utf-8")
    st, r = post("/api/plugins/reload", {})
    check("重新加载后发现插件", any(p["key"] == "demo" for p in r["plugins"]),
          str([p["key"] for p in r["plugins"]]))
    st, r = post("/api/plugins/toggle", {"key": "demo", "on": False})
    check("可以停用", r.get("ok") is True and r.get("enabled") is False)

    print("== 完成动作 ==")
    st, r = post("/api/finish", {"action": "shutdown", "when_all_done": True,
                               "delay": 30})
    check("设置完成动作", r.get("ok") is True, str(r.get("error")))
    st, r = get("/api/finish")
    check("记住动作", r.get("action") == "shutdown", str(r.get("action")))
    st, r = post("/api/finish", {"action": "none"})
    check("改回不做", r.get("ok") is True)

    print("== 导出 / 导入 ==")
    st, r = post("/api/tasks/export", {"fmt": "json"})
    check("导出 JSON", r.get("ok") is True and r.get("count") >= 1,
          str(r.get("count")))
    text = r["text"]
    st, r = post("/api/tasks/import", {"text": text, "start": False})
    check("导入成功", r.get("ok") is True and r.get("created", 0) >= 1,
          str(r.get("created")))
    imported = r.get("ids") or []
    if imported:
        one = tm.get(imported[0])
        check("导入后是暂停态", one and one.status == "paused",
              one.status if one else "missing")
    st, r = post("/api/tasks/export", {"fmt": "aria2"})
    check("导出 aria2", "  dir=" in (r.get("text") or ""), str(r.get("error")))
    st, r = post("/api/tasks/export", {"fmt": "txt"})
    check("导出 txt", r.get("count") >= 1, str(r.get("count")))
    st, r = post("/api/tasks/import", {"text": "这不是链接"})
    check("坏内容明确报错", r.get("ok") is False, str(r.get("error")))

    print("== 直播 ==")
    st, r = post("/api/live", {"url": "http://127.0.0.1:1/x.m3u8", "minutes": 1})
    check("建直播任务", r.get("ok") is True, str(r.get("error")))
    check("标了 live", (r.get("task") or {}).get("meta", {}).get("live") is True,
          str((r.get("task") or {}).get("meta", {}))[:60])

    print("== 更新 / 右键菜单（只读）==")
    st, r = get("/api/update")
    check("更新状态可读", st == 200 and r.get("ok") is True, str(r.get("kind")))
    st, r = get("/api/shell")
    check("右键菜单状态可读", st == 200 and "registered" in r, str(list(r))[:80])

    print("== 进度快照（扩展悬浮窗用）==")
    st, r = get("/api/progress")
    check("接口可达", st == 200 and r.get("ok") is True)
    need = {"counts", "total", "active", "queued", "paused", "speed",
            "speed_text", "percent", "done_bytes", "size_bytes", "current"}
    check("字段齐全", need.issubset(set(r)), str(sorted(need - set(r))))
    check("active 与 counts 一致",
          r.get("active") == (r.get("counts") or {}).get("running", 0),
          f'{r.get("active")} vs {r.get("counts")}')
    check("queued 与 counts 一致",
          r.get("queued") == (r.get("counts") or {}).get("queued", 0),
          f'{r.get("queued")} vs {r.get("counts")}')
    check("百分比在 0~100", 0 <= float(r.get("percent") or 0) <= 100, str(r.get("percent")))
    check("速度文案是字符串", isinstance(r.get("speed_text"), str), r.get("speed_text"))
    # current 只在该有 running 任务时出现，且字段必须够悬浮窗渲染
    cur = r.get("current")
    if cur:
        check("current 字段够用",
              {"filename", "percent", "speed_text", "eta_text"}.issubset(set(cur)),
              str(sorted(cur)))
    else:
        check("无运行任务时 current 为空", r.get("active") == 0, str(r.get("active")))

finally:
    print("== 收尾 ==")
    try:
        tm.shutdown()
    except Exception:  # noqa: BLE001
        pass
    httpd.shutdown()
    check("服务已停止", True)

print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
