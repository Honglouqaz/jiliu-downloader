"""测试用的本地 HTTP 服务器：支持 Range 请求与可选的慢速模拟。

Python 自带的 http.server 不支持 Range，所以多线程分片测不出来。
这个服务器补齐了 Range，并且可以让某一段变慢 —— 用来验证
「动态分片」是否真的能把慢线程的活分给别人。

用法:
    python tests/test_server.py <目录> [端口] [--slow-start 字节 --slow-rate 字节/秒]
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(".")
SLOW_START = 0
SLOW_RATE = 0
RATE = 0
NO_RANGE = False
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音
        pass

    def _resolve(self):
        rel = self.path.split("?", 1)[0].lstrip("/")
        if not rel:
            rel = "index.html"
        p = (ROOT / rel).resolve()
        try:
            p.relative_to(ROOT.resolve())
        except ValueError:
            return None
        return p if p.is_file() else None

    def do_HEAD(self):
        p = self._resolve()
        if not p:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(p.stat().st_size))
        if not NO_RANGE:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
        self.end_headers()

    def do_GET(self):
        p = self._resolve()
        if not p:
            self.send_error(404)
            return

        size = p.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200

        if rng and not NO_RANGE:
            m = RANGE_RE.search(rng)
            if m:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                else:
                    # bytes=-N 取尾部
                    start = max(0, size - int(e or 0))
                    end = size - 1
                # 关键：按 RFC 7233，请求区间要和文件长度取交集。
                # 少了这一步，请求 bytes=0-1023 而文件只有 261 字节时，
                # 会出现「声明 Content-Length: 1024 却只发 261 字节」，
                # 客户端会一直等剩下的字节直到超时。
                end = min(end, size - 1)
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        if not NO_RANGE:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with open(p, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                buf = f.read(min(256 * 1024, left))
                if not buf:
                    break
                # 全局限速：每条连接都限
                if RATE > 0:
                    time.sleep(len(buf) / RATE)
                # 慢速段：模拟"某个连接特别慢"，用来检验动态分片
                elif SLOW_RATE > 0 and start < SLOW_START <= end:
                    time.sleep(len(buf) / SLOW_RATE)
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("port", nargs="?", type=int, default=8899)
    ap.add_argument("--slow-start", type=int, default=0)
    ap.add_argument("--slow-rate", type=int, default=0)
    ap.add_argument("--rate", type=int, default=0, help="所有响应统一限速（字节/秒）")
    ap.add_argument("--no-range", action="store_true", help="假装不支持 Range，用来测退化路径")
    args = ap.parse_args()

    global ROOT, SLOW_START, SLOW_RATE, NO_RANGE, RATE
    ROOT = pathlib.Path(args.root).resolve()
    SLOW_START = args.slow_start
    SLOW_RATE = args.slow_rate
    NO_RANGE = args.no_range
    RATE = args.rate

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[test-server] {ROOT}  http://127.0.0.1:{args.port}", flush=True)
    if RATE:
        print(f"[test-server] 全局限速 {RATE} B/s", flush=True)
    if SLOW_RATE:
        print(f"[test-server] 慢速段: >= {SLOW_START} 字节后限速 {SLOW_RATE} B/s", flush=True)
    if NO_RANGE:
        print("[test-server] 已禁用 Range（Accept-Ranges 不下发，Range 头被忽略）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
