#!/usr/bin/env python3
"""自适应并发 + 多镜像回归（P1-7）。

    python tests/test_adaptive.py
"""
import http.server
import io
import os
import pathlib
import socketserver
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import config as _config  # noqa: E402
from jiliu.engine import RangeDownloader, sha256_file  # noqa: E402

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


TMP = pathlib.Path(tempfile.mkdtemp(prefix="jiliu-adapt-"))
DATA = bytes(range(256)) * (8 * 1024)     # 2 MB
SRC = TMP / "src.bin"
SRC.write_bytes(DATA)
WANT = sha256_file(SRC)

CFG = dict(_config.DEFAULTS)
CFG.update({"proxy_mode": "off", "timeout": 8, "max_retries": 2,
            "chunk_min": 128 * 1024, "chunk_max": 512 * 1024,
            "min_multipart": 64 * 1024, "adaptive_conn": True})


class FileH(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _head(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(DATA)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", 'attachment; filename="src.bin"')
        self.end_headers()

    def do_HEAD(self):
        self._head()

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            try:
                start = int(a)
                end = int(b) if b else len(DATA) - 1
            except Exception:
                start, end = 0, len(DATA) - 1
            end = min(end, len(DATA) - 1)
            body = DATA[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(DATA)}")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)
            return
        self._head()
        self.wfile.write(DATA)


class Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


srv = Srv(("127.0.0.1", 0), FileH)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
GOOD = f"http://127.0.0.1:{PORT}/src.bin"
# 一个必然连不上的源（端口 1）
BAD = "http://127.0.0.1:1/src.bin"

# ----------------------------------------------------------------------
# 1. 多镜像：坏源被剔除，任务仍成功
# ----------------------------------------------------------------------
print("== 多镜像 ==")
logs = []
out = TMP / "mirror.bin"
dl = RangeDownloader(GOOD, out, cfg=CFG, threads=4, mirrors=[BAD],
                     log_cb=lambda m: logs.append(m))
res = dl.run()
check("有坏源也能下完", res.get("ok") is True, res.get("error", ""))
check("内容哈希一致", sha256_file(out) == WANT)
check("坏源被剔除并留日志",
      any("剔除" in m for m in logs), str([m for m in logs if "剔除" in m])[:120])
check("剔除后只剩好源", GOOD in dl._sources() and BAD not in dl._sources(),
      str(dl._sources()))

# 同一个 URL 重复传进 mirrors 不该被当成两个源
logs2 = []
out2 = TMP / "dup.bin"
dl2 = RangeDownloader(GOOD, out2, cfg=CFG, threads=2, mirrors=[GOOD],
                      log_cb=lambda m: logs2.append(m))
res2 = dl2.run()
check("mirrors 与主 URL 重复不影响", res2.get("ok") is True and len(dl2._sources()) == 1,
      str(dl2._sources()))

# ----------------------------------------------------------------------
# 2. 自适应：纯逻辑
# ----------------------------------------------------------------------
print("== 自适应判定 ==")


def mk(threads=4, adaptive=True, speeds=None, pending=5, alive=4, maxw=8):
    d = RangeDownloader(GOOD, TMP / "x.bin", cfg=dict(CFG, adaptive_conn=adaptive),
                        threads=threads)
    d.size = 100 * 1024 * 1024
    d.accept_ranges = True
    d._worker_speed = dict(speeds or {})
    d._pending = [(i * 1024, i * 1024 + 1023) for i in range(pending)]
    d._alive = alive
    d._max_workers = maxw
    d._last_scale = 0.0
    d._last_429 = 0.0
    spawned = []
    d._spawn_worker = lambda: spawned.append(1)   # 不真的起线程
    return d, spawned


d, sp = mk(speeds={1: 100 * 1024, 2: 90 * 1024, 3: 800 * 1024})
d._maybe_scale()
check("快慢差 8 倍 → 加连接", len(sp) == 1, str(sp))

d, sp = mk(speeds={1: 100 * 1024, 2: 120 * 1024, 3: 150 * 1024})
d._maybe_scale()
check("速度接近 → 不加", len(sp) == 0)

d, sp = mk(speeds={1: 100 * 1024, 2: 900 * 1024, 3: 800 * 1024})
d._last_429 = time.monotonic()
d._maybe_scale()
check("刚被 429 过 → 不加", len(sp) == 0)

d, sp = mk(speeds={1: 100 * 1024, 2: 900 * 1024, 3: 800 * 1024}, alive=8, maxw=8)
d._maybe_scale()
check("到上限 → 不加", len(sp) == 0)

d, sp = mk(speeds={1: 100 * 1024, 2: 900 * 1024, 3: 800 * 1024}, pending=1)
d._maybe_scale()
check("待办不足 → 不加", len(sp) == 0)

d, sp = mk(speeds={1: 1024, 2: 900 * 1024, 3: 800 * 1024})
d._maybe_scale()
check("样本太少（<1KB/s 的忽略）→ 不加", len(sp) == 0, str(len(sp)))

d, sp = mk(speeds={1: 100 * 1024, 2: 900 * 1024, 3: 800 * 1024}, adaptive=False)
d._maybe_scale()
check("关掉自适应 → 不加", len(sp) == 0)

# 429 之后上限被砍半
d, sp = mk(threads=4, maxw=8)
d._throttled(0.01)
check("限速退避后上限减半", d._max_workers == 4, str(d._max_workers))
check("记录了限速时间", d._last_429 > 0)

# ----------------------------------------------------------------------
# 3. 真实下载仍正确（回归：改了线程收尾逻辑）
# ----------------------------------------------------------------------
print("== 回归 ==")
out3 = TMP / "plain.bin"
dl3 = RangeDownloader(GOOD, out3, cfg=CFG, threads=8)
res3 = dl3.run()
check("普通 8 线程下载仍正确",
      res3.get("ok") is True and sha256_file(out3) == WANT, res3.get("error", ""))
check("并发数不会超过 2 倍初始",
      len(dl3._threads) <= dl3.threads * 2, f"{len(dl3._threads)} 个")

srv.shutdown()
print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
