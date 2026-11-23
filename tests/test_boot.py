#!/usr/bin/env python3
"""首屏加载速度的回归测试。

这一组守的是「打开页面到看见东西」的这段时间 —— 之前它是 7.5 秒，
因为 /api/state 里串着几个要 spawn 子进程的探测（问 yt-dlp 版本、
试 aria2 的 RPC）。窗口秒开，页面却空着，用户以为软件卡死了。

现在拆成两半：接口立刻返回（有快照给快照，没有给占位），探测在后台跑。
这里把这两半都钉住 —— 谁再把慢活儿塞回同步路径，测试会红。

    python tests/test_boot.py
"""
# -*- coding: utf-8 -*-
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import config as _config, engines, server as srv, snapshot

PORT = int(os.environ.get("JL_TEST_BOOT_PORT") or 8811)
# 本机测试一律绕开环境变量里的代理（否则连 127.0.0.1 都要绕远路）
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [OK  ] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}   {extra}")


def head(title):
    print(f"\n== {title} ==")


# ==========================================================================
head("B1  快照：读得快，算得慢")

calls = {"n": 0}


def _slow():
    calls["n"] += 1
    time.sleep(0.35)
    return {"v": calls["n"]}


s1 = snapshot.Snapshot(_slow, ttl=60, name="t1")

t0 = time.time()
first = s1.get(wait=0.0)
dt = (time.time() - t0) * 1000
check("B1.1 没算过时不阻塞（giveUp 立刻返回）", first is None and dt < 50,
      f"{dt:.0f}ms")
check("B1.2 没算过时触发了一次后台探测", calls["n"] == 1, str(calls))

time.sleep(0.5)
check("B1.3 后台算完后能拿到值", (s1.get() or {}).get("v") == 1, str(s1.get()))
check("B1.4 命中快照不重复计算", calls["n"] == 1, str(calls))

# 并发进来只跑一趟
s2 = snapshot.Snapshot(_slow, ttl=60, name="t2")


def _hammer():
    s2.get(wait=0.0)


ts = [threading.Thread(target=_hammer) for _ in range(12)]
before = calls["n"]
for t in ts:
    t.start()
for t in ts:
    t.join()
time.sleep(0.5)
check("B1.5 12 个并发只触发一趟探测（单飞）", calls["n"] - before == 1,
      f"多跑了 {calls['n'] - before - 1} 趟")

# wait 有上限
s3 = snapshot.Snapshot(lambda: (time.sleep(0.8) or {"v": 3}), ttl=60, name="t3")
s3.refresh_async()
t0 = time.time()
s3.get(wait=0.1)
dt = (time.time() - t0) * 1000
check("B1.6 wait 是上限，不是「等到算完」", dt < 400, f"{dt:.0f}ms")

# 过期会重算
s4 = snapshot.Snapshot(lambda: {"v": time.time()}, ttl=0.2, name="t4")
a = s4.get(wait=2.0)
time.sleep(0.35)
s4.get(wait=0.0)
time.sleep(0.5)
b = s4.get(wait=2.0)
check("B1.7 过了 TTL 会重新算", a and b and b["v"] > a["v"], f"{a} {b}")

s4.invalidate()
check("B1.8 invalidate 之后旧值就没了", s4.peek() is None)

s5 = snapshot.Snapshot(lambda: {"bad": True}, ttl=60, name="t5")
s5.set({"good": True})
check("B1.9 set 直接写入（重算过的人不必再算一趟）",
      (s5.get() or {}).get("good") is True, str(s5.get()))


def _boom():
    raise RuntimeError("探测炸了")


s6 = snapshot.Snapshot(_boom, ttl=60, name="t6")
s6.refresh_async()
time.sleep(0.3)
check("B1.10 探测抛异常不拖垮读取", s6.get() is None and bool(s6.error()),
      s6.error())


# ==========================================================================
head("B2  占位结构与真实结构对齐")

cfg = _config.load()
ph = engines.placeholder(cfg)
real = engines.describe(cfg, refresh=False)

check("B2.1 顶层字段与 describe 一致",
      set(ph) == set(real) | {"probing"},
      f"占位多/少：{set(ph) ^ set(real)}")
check("B2.2 带 probing 标记", ph.get("probing") is True)
rows_ph, rows_real = ph["engines"], real["engines"]
check("B2.3 引擎条目数一致", len(rows_ph) == len(rows_real),
      f"{len(rows_ph)} vs {len(rows_real)}")
keys_ph = {k for r in rows_ph for k in r}
keys_real = {k for r in rows_real for k in r}
check("B2.4 每个条目的字段与真实条目对齐",
      keys_real - {"pending"} <= keys_ph,
      f"缺：{keys_real - keys_ph}")
check("B2.5 native 在占位里就是可用（内置，不必等探测）",
      any(r["key"] == "native" and r["available"] for r in rows_ph))
check("B2.6 依赖外部程序的引擎在占位里标 pending",
      all(r.get("pending") for r in rows_ph if r["key"] != "native"),
      str([r["key"] for r in rows_ph if not r.get("pending")]))


# ==========================================================================
head("B3  首页：gzip 与 304")

httpd, tm = srv.serve(cfg=cfg, port=PORT, quiet=True)


def get(path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 headers=headers or {})
    t0 = time.time()
    try:
        with OP.open(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers), (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers), (time.time() - t0) * 1000


try:
    time.sleep(0.2)
    st, body, hd, ms = get("/")
    check("B3.1 首页 200", st == 200)
    check("B3.2 带 ETag", bool(hd.get("ETag")), str(hd.get("ETag")))
    check("B3.3 Cache-Control 用 no-cache（要回源验证，但不重传）",
          (hd.get("Cache-Control") or "") == "no-cache", str(hd.get("Cache-Control")))
    raw_len = len(body)
    check("B3.4 未声明 gzip 时给原文", not hd.get("Content-Encoding"),
          str(hd.get("Content-Encoding")))

    st, gz, hd2, ms2 = get("/", {"Accept-Encoding": "gzip"})
    check("B3.5 声明 gzip 时压缩", hd2.get("Content-Encoding") == "gzip",
          str(hd2.get("Content-Encoding")))
    check("B3.6 压缩后明显更小", 0 < len(gz) < raw_len * 0.6,
          f"{len(gz)} vs {raw_len}")
    import gzip as _gz
    check("B3.7 解压回来内容一致", _gz.decompress(gz) == body)
    check("B3.8 带 Vary: Accept-Encoding", "Accept-Encoding" in (hd2.get("Vary") or ""),
          str(hd2.get("Vary")))

    etag = hd2.get("ETag")
    st, body3, hd3, ms3 = get("/", {"Accept-Encoding": "gzip",
                                    "If-None-Match": etag})
    check("B3.9 ETag 命中回 304", st == 304, f"status={st}")
    check("B3.10 304 不带正文", body3 == b"", f"{len(body3)} bytes")

    # ======================================================================
    head("B4  /api/state 不再被慢探测按住")

    t0 = time.time()
    st, raw, _, _ = get("/api/state")
    first_ms = (time.time() - t0) * 1000
    data = json.loads(raw.decode("utf-8"))
    check("B4.1 首请求就跑完", st == 200, f"status={st}")
    check("B4.2 首请求在 1 秒内返回（之前是 5–7 秒）", first_ms < 1000,
          f"{first_ms:.0f}ms")
    check("B4.3 结构完整（占位也不缺字段）",
          all(k in data for k in ("tools", "engines", "video", "backends")),
          str([k for k in ("tools", "engines", "video", "backends")
               if k not in data]))
    check("B4.4 引擎表始终有 native", any(
        e["key"] == "native" for e in data["engines"]["engines"]))

    # 探测完成后（后台线程）真值要能补上
    deadline = time.time() + 30
    got = None
    while time.time() < deadline:
        st, raw, _, _ = get("/api/state")
        d = json.loads(raw.decode("utf-8"))
        if not d["engines"].get("probing"):
            got = d
            break
        time.sleep(0.3)
    check("B4.5 探测完成后占位被真值替换", got is not None,
          "等了 30 秒还是 probing")
    if got:
        check("B4.6 真值里 tools 有内容", bool(got["tools"]), str(got["tools"])[:60])
        check("B4.7 真值里没有 pending 尾巴",
              not any(e.get("pending") for e in got["engines"]["engines"]))

        t0 = time.time()
        get("/api/state")
        warm_ms = (time.time() - t0) * 1000
        check("B4.8 命中快照时是毫秒级", warm_ms < 300, f"{warm_ms:.0f}ms")
finally:
    httpd.shutdown()
    httpd.server_close()


print(f"\n{'='*60}\n结果: {ok}/{ok+fail} 通过" +
      (f"，{fail} 失败" if fail else ""))
raise SystemExit(0 if fail == 0 else 1)
