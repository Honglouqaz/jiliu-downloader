#!/usr/bin/env python3
"""批量粘贴 / 通配符回归（P0-4 + P1-6）。

    python tests/test_batch.py

起一个本地 HTTP 服务做真实探测（支持 Range 与不支持 Range 两种），
验证并发探测的大小/类型/顺序，以及通配符展开的边界。
"""
import http.server
import io
import os
import socketserver
import sys
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import batch, config as _config  # noqa: E402

CFG = dict(_config.DEFAULTS)
CFG["proxy_mode"] = "off"
CFG["timeout"] = 4

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


# ----------------------------------------------------------------------
# 1. 链接提取
# ----------------------------------------------------------------------
print("== 链接提取 ==")
text = """
看看这几个：
https://a.com/x.zip 和 http://b.com/y.iso。
还有 (https://c.com/z?q=1&r=2)
magnet:?xt=urn:btih:abcdef
ftp://d.com/f.bin
重复一次 https://a.com/x.zip
"""
urls = batch.extract(text)
check("捞出 6 条（含重复）", len(urls) == 6, str(urls))
check("去掉句号", "https://a.com/x.zip" in urls)
check("去掉右括号", "https://c.com/z?q=1&r=2" in urls)
check("识别 magnet", any(u.startswith("magnet:") for u in urls))
check("识别 ftp", any(u.startswith("ftp://") for u in urls))

# ----------------------------------------------------------------------
# 2. 去重
# ----------------------------------------------------------------------
print("== 去重 ==")
uniq, dups = batch.dedupe([
    "https://A.com/x.zip",
    "https://a.com/x.zip#frag",
    "https://a.com:443/x.zip",
    "http://a.com/x.zip",
])
check("同一条只留一个", len(uniq) == 2, str(uniq) + f" dup={dups}")
check("去重计数正确", dups == 2, str(dups))
check("保留原始写法", uniq[0] == "https://A.com/x.zip", uniq[0])

# ----------------------------------------------------------------------
# 3. 通配符
# ----------------------------------------------------------------------
print("== 通配符 ==")
r = batch.expand("https://x.com/img-[001-005].jpg")
check("区间展开 5 条", r["ok"] and r["count"] == 5, r.get("error", ""))
check("补零位数跟随起点", r["lines"][0].endswith("img-001.jpg") and
      r["lines"][-1].endswith("img-005.jpg"), r["lines"][:2])

r = batch.expand("https://x.com/p-[1-5].jpg")
check("不补零", r["lines"][0].endswith("p-1.jpg"), r["lines"][0])

r = batch.expand("https://x.com/p-[1-9:3].jpg")
check("步长 3", r["count"] == 3 and r["lines"][1].endswith("p-4.jpg"), str(r["lines"]))

r = batch.expand("https://x.com/p-[1..3].jpg")
check("双点写法", r["count"] == 3, str(r["count"]))

r = batch.expand("https://x.com/{a,b,c}.txt")
check("枚举展开", r["count"] == 3 and r["lines"][1].endswith("b.txt"), str(r["lines"]))

r = batch.expand("https://x.com/[01-02]/{a,b}.png")
check("区间+枚举组合", r["count"] == 4, str(r["lines"]))

r = batch.expand("https://x.com/a-*.jpg")
check("拒绝 * 通配", r["ok"] is False and "不支持 *" in r["error"], r.get("error", ""))

r = batch.expand("https://x.com/[1-99999].jpg")
check("超上限明确拒绝", r["ok"] is False and "上限" in r["error"], r.get("error", ""))

r = batch.expand("https://x.com/[5-1].jpg")
check("倒序区间", r["ok"] and r["count"] == 5 and r["lines"][0].endswith("5.jpg"),
      str(r.get("lines", [])[:2]))

r = batch.expand("")
check("空文本返回空", r["ok"] and r["count"] == 0)

# ----------------------------------------------------------------------
# 4. parse 混合场景
# ----------------------------------------------------------------------
print("== 混合解析 ==")
mixed = """
https://a.com/1.zip
https://a.com/2.zip
img-[01-02].jpg
example.com/bare.iso
https://a.com/1.zip
"""
p = batch.parse(mixed, CFG)
check("parse 成功", p["ok"], p.get("error", ""))
check("展开 + 提取共 5 条（去重后）", p["count"] == 5, str(p["urls"]))
check("模板补了协议", any(u.startswith("https://img-01.jpg") for u in p["urls"]),
      str(p["urls"]))
check("裸域名补协议", "https://example.com/bare.iso" in p["urls"], str(p["urls"]))
check("重复链接去掉一次", p["dups"] == 1, str(p["dups"]))

# ----------------------------------------------------------------------
# 5. 真实探测
# ----------------------------------------------------------------------
print("== 并发探测 ==")
BIG = b"Z" * 300000
SMALL = b"y" * 1234
PORT = 0


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        if self.path.startswith("/slow"):
            time.sleep(6)          # 模拟"连得上但一直不回包"
        if self.path.startswith("/delay"):
            time.sleep(0.5)
        if self.path.startswith("/norange"):
            # 不支持 HEAD 的站：直接 405
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = BIG if "big" in self.path else SMALL
        name = "big.iso" if "big" in self.path else "small.zip"
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(6)
        body = BIG if "big" in self.path else SMALL
        rng = self.headers.get("Range")
        if rng and not self.path.startswith("/norange"):
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-0/{len(body)}")
            self.send_header("Content-Length", "1")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body[:1])
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)


class Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


srv = Srv(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

existing = {batch.normalize_url(f"{BASE}/small.zip")}
t0 = time.monotonic()
rows = batch.probe_many(
    [f"{BASE}/big.iso", f"{BASE}/small.zip", f"{BASE}/norange.bin",
     "http://127.0.0.1:1/dead", f"{BASE}/slow.timeout"],
    CFG, workers=5, timeout=3, existing=existing)
cost = time.monotonic() - t0
by = {r["url"].rsplit("/", 1)[-1]: r for r in rows}

check("返回顺序与输入一致",
      [r["url"].rsplit("/", 1)[-1] for r in rows]
      == ["big.iso", "small.zip", "norange.bin", "dead", "slow.timeout"])
check("大文件识别出大小", by["big.iso"]["size"] == len(BIG), str(by["big.iso"]["size"]))
check("小文件识别出大小", by["small.zip"]["size"] == len(SMALL))
check("识别文件名", by["big.iso"]["filename"] == "big.iso", by["big.iso"]["filename"])
check("识别 Content-Type", "octet-stream" in by["big.iso"]["type"])
check("支持 Range", by["big.iso"]["accept_ranges"] is True)
check("已在列表里的被标出来", by["small.zip"]["already"] is True)
check("连接失败标未知而非崩溃", by["dead"]["known"] is False
      and by["dead"]["size_text"] == "未知", by["dead"]["error"][:50])
check("超时标未知", by["slow.timeout"]["known"] is False)
check("含慢响应的一组在 8 秒内收尾", cost < 8.0, f"{cost:.1f}s")

# 并发性单独验证：8 条各延迟 0.5s，串行要 4s，并发应 < 2s
t1 = time.monotonic()
batch.probe_many([f"{BASE}/delay{i}.bin" for i in range(8)], CFG, workers=8, timeout=5)
fast = time.monotonic() - t1
check("8 路确实并发", fast < 2.0, f"{fast:.1f}s（串行需 4s）")

summ = batch.summarize(rows)
check("汇总 total", summ["total"] == 5, str(summ))
check("汇总 known 数量", summ["known"] >= 3, str(summ["known"]))
check("汇总字节数", summ["bytes"] >= len(BIG) + len(SMALL), str(summ["bytes"]))
check("汇总带人类可读大小", summ["size_text"] not in ("", "-"), summ["size_text"])

srv.shutdown()
print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
