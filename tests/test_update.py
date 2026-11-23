#!/usr/bin/env python3
"""自动更新回归（P0-1）。

    python tests/test_update.py

用本地 HTTP 服务器冒充 GitHub Releases API，跑完检查：
版本比较 / 资产挑选 / 检查缓存 / 下载校验 / 暂存 / 延迟脚本内容。
"""
import hashlib
import http.server
import io
import json
import os
import pathlib
import socketserver
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 状态目录隔离，别碰真实 ~/.jiliu
_TMP = tempfile.mkdtemp(prefix="jiliu-upd-")
os.environ["JILIU_HOME"] = _TMP
os.environ["JILIU_NO_NOTIFY"] = "1"

from jiliu import config as _config, update  # noqa: E402

CFG = dict(_config.DEFAULTS)
CFG["proxy_mode"] = "off"          # 本机测试绝不能被环境代理劫持
CFG["update_check"] = True

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
# 1. 版本号
# ----------------------------------------------------------------------
print("== 版本比较 ==")
cases = [
    ("1.7.1", "1.7.0", True),
    ("v1.7.1", "1.7.1", False),
    ("1.8.0", "1.7.9", True),
    ("1.10.0", "1.9.9", True),
    ("1.8.0-beta.2", "1.7.1", True),
    ("1.8.0", "1.8.0-beta.2", True),
    ("1.8.0-beta.1", "1.8.0-beta.2", False),
    ("", "1.7.1", False),
    ("不是版本", "1.7.1", False),
]
for latest, current, want in cases:
    got = update.is_newer(latest, current)
    check(f"{latest or '(空)'} > {current} ? {want}", got == want, f"got={got}")

check("parse_version 排序稳定",
      update.parse_version("1.7.1") < update.parse_version("1.10.0")
      and update.parse_version("2.0.0-beta.1") < update.parse_version("2.0.0"))

# ----------------------------------------------------------------------
# 2. 资产挑选
# ----------------------------------------------------------------------
print("== 资产挑选 ==")
ASSETS = [
    {"name": "jiliu-setup-1.8.0.exe", "size": 30, "browser_download_url": "https://github.com/x/setup"},
    {"name": "jiliu.exe", "size": 20, "browser_download_url": "https://github.com/x/exe"},
    {"name": "jiliu-portable-1.8.0.zip", "size": 25, "browser_download_url": "https://github.com/x/zip"},
]
check("安装版拿 setup", update.pick_asset(ASSETS, "installed")["name"].startswith("jiliu-setup"))
check("便携版拿单文件", update.pick_asset(ASSETS, "portable")["name"] == "jiliu.exe")
check("只有 zip 时退化到 zip",
      update.pick_asset([ASSETS[2]], "portable")["name"].endswith(".zip"))
check("都没有返回 None", update.pick_asset([], "portable") is None)
check("digest 解析",
      update._asset_sha({"digest": "sha256:ABC123"}) == "abc123")
check("无 digest 返回空", update._asset_sha({}) == "")
check("非法域名被拒", update._allowed_url("https://evil.example.com/jiliu.exe") is False)
check("GitHub 域名放行",
      update._allowed_url("https://github.com/a/b/releases/download/v1/jiliu.exe") is True)

# ----------------------------------------------------------------------
# 3. 本地假 API
# ----------------------------------------------------------------------
BLOB = b"JILIU-FAKE-BINARY-" + bytes(range(256)) * 64
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()
PORT = 0


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = f"http://127.0.0.1:{PORT}"
        if self.path == "/api":
            self._json({
                "tag_name": "v9.9.9",
                "name": "汲流 9.9.9",
                "body": "测试版本说明",
                "html_url": "https://github.com/Honglouqaz/jiliu-downloader/releases/tag/v9.9.9",
                "published_at": "2026-10-20T00:00:00Z",
                "prerelease": False,
                "assets": [
                    {"name": "jiliu.exe", "size": len(BLOB),
                     "browser_download_url": f"{base}/bin",
                     "digest": f"sha256:{BLOB_SHA}"},
                    {"name": "jiliu-setup-9.9.9.exe", "size": len(BLOB),
                     "browser_download_url": f"{base}/bin",
                     "digest": f"sha256:{BLOB_SHA}"},
                ],
            })
        elif self.path == "/api-404":
            self._json({"message": "Not Found"}, 404)
        elif self.path == "/bin":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(BLOB)))
            self.end_headers()
            self.wfile.write(BLOB)
        elif self.path == "/bin-wrong":
            bad = BLOB + b"x"
            self.send_response(200)
            self.send_header("Content-Length", str(len(bad)))
            self.end_headers()
            self.wfile.write(bad)
        else:
            self._json({"error": "nope"}, 404)


class Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


srv = Srv(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"
# 本地地址要进白名单才能被 download() 接受
update._ALLOWED_HOSTS = update._ALLOWED_HOSTS + ("127.0.0.1",)

# ----------------------------------------------------------------------
# 4. check
# ----------------------------------------------------------------------
print("== 检查 ==")
os.environ["JILIU_UPDATE_API"] = f"{BASE}/api"
info = update.check(CFG, force=True)
check("check 成功", info["ok"], info.get("error", ""))
check("识别出版本", info["latest"] == "9.9.9", info["latest"])
check("判定有新版本", info["has_update"])
check("带出更新说明", info["notes"] == "测试版本说明")
check("选中单文件资产", (info.get("asset") or {}).get("name") == "jiliu.exe")
check("带出 sha256", (info.get("asset") or {}).get("sha256") == BLOB_SHA)

# 缓存：改环境变量后再查，不该重新请求（返回同一份）
t0 = time.monotonic()
again = update.check(CFG)
check("命中缓存", again["latest"] == "9.9.9" and time.monotonic() - t0 < 0.5)

# 404 不该抛异常
update.invalidate()
os.environ["JILIU_UPDATE_API"] = f"{BASE}/api-404"
bad = update.check(CFG, force=True)
check("404 不抛异常且给出提示", bad["ok"] is False and "404" in bad["error"], bad.get("error", ""))

# 关闭检查时直接跳过
update.invalidate()
os.environ["JILIU_UPDATE_API"] = f"{BASE}/api"
off = update.check(dict(CFG, update_check=False))
check("关闭检查时跳过", off["ok"] is False and "关闭" in off["reason"])

# beta 通道
update.invalidate()
_orig = H.do_GET


def _prerelease_get(self):
    if self.path == "/api":
        self._json({
            "tag_name": "v9.9.9-beta.1", "name": "beta", "body": "",
            "html_url": "", "prerelease": True,
            "assets": [{"name": "jiliu.exe", "size": len(BLOB),
                        "browser_download_url": f"{BASE}/bin", "digest": ""}],
        })
    else:
        _orig(self)


H.do_GET = _prerelease_get
stable = update.check(CFG, force=True)
check("稳定通道忽略预发布版", stable["has_update"] is False, stable.get("reason", ""))
update.invalidate()
beta = update.check(dict(CFG, update_channel="beta"), force=True)
check("beta 通道接受预发布版", beta["has_update"] is True)
H.do_GET = _orig

# ----------------------------------------------------------------------
# 5. 下载与校验
# ----------------------------------------------------------------------
print("== 下载校验 ==")
update.invalidate()
os.environ["JILIU_UPDATE_API"] = f"{BASE}/api"
info = update.check(CFG, force=True)

seen = []
dest = pathlib.Path(_TMP) / "dl" / "jiliu-new.exe"
res = update.download(f"{BASE}/bin", dest, CFG, expect_sha=BLOB_SHA,
                      progress=lambda a, b: seen.append(a))
check("下载成功", res["ok"], res.get("error", ""))
check("字节数一致", res["bytes"] == len(BLOB), f"{res.get('bytes')} vs {len(BLOB)}")
check("sha256 一致", res["sha256"] == BLOB_SHA)
check("有进度回调", len(seen) > 0, f"{len(seen)} 次")
check("无残留 .part", not pathlib.Path(str(dest) + ".part").exists())

bad_dest = pathlib.Path(_TMP) / "dl" / "bad.exe"
res2 = update.download(f"{BASE}/bin-wrong", bad_dest, CFG, expect_sha=BLOB_SHA)
check("校验失败被拒", res2["ok"] is False and "校验失败" in res2["error"], res2.get("error", ""))
check("校验失败不留文件", not bad_dest.exists())

res3 = update.download("https://evil.example.com/x.exe", pathlib.Path(_TMP) / "e.exe", CFG)
check("非白名单域名被拒", res3["ok"] is False and "允许列表" in res3["error"])

# ----------------------------------------------------------------------
# 6. 暂存
# ----------------------------------------------------------------------
print("== 暂存 ==")
update.discard()
st = update.stage(info, CFG)
check("暂存成功", st["ok"], st.get("error", ""))
pend = update.pending()
check("pending 可读", pend is not None and pend["version"] == "9.9.9",
      str((pend or {}).get("version")))
check("暂存文件存在", pathlib.Path(pend["file"]).is_file())
check("暂存文件 sha 正确", update.sha256_file(pathlib.Path(pend["file"])) == BLOB_SHA)

bat = update.write_apply_script("portable", pend)
text = bat.read_text(encoding="ascii", errors="replace")
check("脚本等待进程退出", "Wait-Process" in text and str(os.getpid()) in text)
check("脚本 move 覆盖 exe", "move /y" in text and "jiliu.exe" in text)
check("脚本会重启新版", 'start ""' in text)
check("脚本自删", 'del "%~f0"' in text)

bat2 = update.write_apply_script("installed", pend)
t2 = bat2.read_text(encoding="ascii", errors="replace")
check("安装版脚本调 setup --quiet", "--quiet" in t2 and "--target" in t2)

update.discard()
check("discard 清掉 pending", update.pending() is None)

# ----------------------------------------------------------------------
# 7. 形态判定
# ----------------------------------------------------------------------
print("== 形态 ==")
check("源码运行不自动替换", update.install_kind() == "source")
check("源码模式 can_self_update=False", update.can_self_update()[0] is False)
d = update.describe(CFG)
check("describe 字段齐全", {"current", "kind", "can_update", "pending"} <= set(d))

srv.shutdown()
print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
