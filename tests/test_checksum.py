#!/usr/bin/env python3
"""校验和回归（P0-5）。

    python tests/test_checksum.py
"""
import hashlib
import http.server
import io
import os
import pathlib
import socketserver
import sys
import tempfile
import threading
import tracemalloc

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import checksum, config as _config  # noqa: E402

CFG = dict(_config.DEFAULTS)
CFG["proxy_mode"] = "off"

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


TMP = pathlib.Path(tempfile.mkdtemp(prefix="jiliu-sum-"))
sample = TMP / "abc.bin"
sample.write_bytes(b"abc")

# ----------------------------------------------------------------------
# 1. 计算
# ----------------------------------------------------------------------
print("== 计算 ==")
res = checksum.compute(sample, "sha256")
check("sha256 正确",
      res["ok"] and res["hex"] == hashlib.sha256(b"abc").hexdigest(), res["hex"][:16])
res = checksum.compute(sample, "md5")
check("md5 正确", res["ok"] and res["hex"] == hashlib.md5(b"abc").hexdigest())
check("sha1 长度 40", len(checksum.compute(sample, "sha1")["hex"]) == 40)
check("sha512 长度 128", len(checksum.compute(sample, "sha512")["hex"]) == 128)
check("不支持的算法被拒", checksum.compute(sample, "crc32")["ok"] is False)
check("文件不存在被拒",
      checksum.compute(TMP / "nope.bin", "sha256")["ok"] is False)

multi = checksum.compute_all(sample)
check("一次算四种", multi["ok"] and len(multi["hashes"]) == 4, str(list(multi["hashes"])))
check("多算法结果与单算一致",
      multi["hashes"]["sha256"] == hashlib.sha256(b"abc").hexdigest())

# 进度回调
seen = []
checksum.compute(sample, "sha256", progress=lambda a, b: seen.append((a, b)))
check("有进度回调", len(seen) >= 1, str(seen))

# 取消
import threading as _th
ev = _th.Event()
ev.set()
check("取消生效", checksum.compute(sample, "sha256", cancel=ev)["ok"] is False)

# ----------------------------------------------------------------------
# 2. 内存：32 MB 文件峰值增量应远小于文件本身
# ----------------------------------------------------------------------
print("== 内存 ==")
big = TMP / "big.bin"
with open(big, "wb") as f:
    for _ in range(32):
        f.write(b"\x00" * (1024 * 1024))
tracemalloc.start()
checksum.compute(big, "sha256")
cur, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
check("32MB 文件峰值 < 6MB", peak < 6 * 1024 * 1024, f"peak={peak / 1048576:.1f}MB")

# ----------------------------------------------------------------------
# 3. 期望值解析
# ----------------------------------------------------------------------
print("== 期望值解析 ==")
H64 = "a" * 64
H32 = "b" * 32
cases = [
    (H64, ("sha256", H64)),
    ("sha256:" + H64, ("sha256", H64)),
    (H64 + "  jiliu.exe", ("sha256", H64)),
    ("SHA256 (jiliu.exe) = " + H64, ("sha256", H64)),
    (H32, ("md5", H32)),
    ("", ("", "")),
    ("不是哈希", ("", "")),
]
for raw, want in cases:
    got = checksum.normalize_expected(raw)
    check(f"解析 {raw[:24] or '(空)'}", got == want, f"{got}")

cmp = checksum.compare(H64.upper(), H64)
check("大小写不同也算一致", cmp["ok"] and cmp["match"] is True)
cmp2 = checksum.compare("c" * 64, H64)
check("错一个字符报不一致", cmp2["ok"] and cmp2["match"] is False)
cmp3 = checksum.compare("d" * 32, H64)
check("长度不符带说明", cmp3["ok"] is False and "长度" in cmp3["error"])
check("认不出期望值时给出原因",
      checksum.compare(H64, "abc")["ok"] is False)

# ----------------------------------------------------------------------
# 4. 校验文件解析
# ----------------------------------------------------------------------
print("== 校验文件 ==")
sums_text = (
    "# comment\n"
    f"{H64}  jiliu-portable-1.7.1/jiliu.exe\n"
    f"{'e' * 64}  *other.zip\n"
    f"SHA256 (third.bin) = {'f' * 64}\n"
)
rows = checksum.parse_sums(sums_text)
check("解析出 3 行", len(rows) == 3, str(len(rows)))
check("识别 * 二进制标记", rows[1]["name"] == "other.zip", rows[1]["name"])
check("识别 BSD 写法", rows[2]["algo"] == "sha256" and rows[2]["name"] == "third.bin")
pick = checksum.parse_sums(sums_text, "jiliu.exe")
check("按文件名过滤到 1 条", len(pick) == 1 and pick[0]["hex"] == H64)

check("裸哈希文件也能认", bool(checksum.parse_sums(H64)[0]["hex"]))
cands = checksum.sidecar_candidates("https://x.com/dir/a.zip")
check("候选里含 a.zip.sha256", any(c.endswith("/a.zip.sha256") for c in cands))
check("候选里含 SHA256SUMS", any(c.endswith("/SHA256SUMS") for c in cands))
check("同目录不是同主机", all(c.startswith("https://x.com/dir/") for c in cands))
check("非 http 不做候选", checksum.sidecar_candidates("magnet:?xt=1") == [])

# ----------------------------------------------------------------------
# 5. 联网取校验文件
# ----------------------------------------------------------------------
print("== 拉取校验文件 ==")
PORT = 0


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/d/a.zip.sha256":
            body = f"{H64}  a.zip\n".encode()
        elif self.path == "/d/b.zip":
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


srv = Srv(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

got = checksum.fetch_sidecar(f"{BASE}/d/a.zip", CFG, "a.zip")
check("找到并解析出哈希", got["ok"] and got["hex"] == H64, got.get("error", ""))
check("记录来源", got["source"].endswith("a.zip.sha256"), got["source"])

miss = checksum.fetch_sidecar(f"{BASE}/d/b.zip", CFG, "b.zip")
check("找不到时给出原因", miss["ok"] is False and "没有找到" in miss["error"])
check("尝试过的地址都被记录", len(miss["tried"]) > 5, str(len(miss["tried"])))

# ----------------------------------------------------------------------
# 6. 本地校验文件
# ----------------------------------------------------------------------
print("== 本地校验 ==")
local = TMP / "local.bin"
local.write_bytes(b"hello")
(local.parent / "local.bin.sha256").write_text(
    hashlib.sha256(b"hello").hexdigest() + "  local.bin\n", encoding="utf-8")
v = checksum.verify_local(local)
check("本地校验一致", v["ok"] and v["match"] is True, str(v))
(local.parent / "local.bin.sha256").write_text("f" * 64 + "  local.bin\n", encoding="utf-8")
v2 = checksum.verify_local(local)
check("不一致能报出来", v2["ok"] and v2["match"] is False, str(v2))
(local.parent / "local.bin.sha256").unlink()
v3 = checksum.verify_local(local)
check("没有校验文件时 skipped", v3.get("skipped") is True, str(v3))

srv.shutdown()
print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
