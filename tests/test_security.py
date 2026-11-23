#!/usr/bin/env python3
"""服务端安全面回归（1.9.5 加固项）。

    python tests/test_security.py

覆盖：
  · Origin 只认本服务自己的端口（别的 localhost 页面不能建任务/改配置）
  · 扩展来源不能改 ytdlp_path / after_download_command 等代码执行键
  · /api/state 的配置脱敏（proxy_pass / rpc_secret 不回明文）
  · /api/checksum 限定在下载目录内
  · /rpc 在配置了 rpc_secret 时真的要求 token
  · POST /api/update/check 不再 404
"""
import io
import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_TMP = tempfile.mkdtemp(prefix="jiliu-sec-")
os.environ["JILIU_HOME"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import config as _config, server  # noqa: E402

DL = pathlib.Path(_TMP) / "downloads"
DL.mkdir(parents=True, exist_ok=True)
CFG = _config.load()
CFG.update({
    "port": 0, "backend": "native", "auto_start": False, "prevent_sleep": False,
    "notify_enabled": False, "update_check": False, "tray_enabled": False,
    "download_dir": str(DL), "proxy_pass": "topsecret", "rpc_secret": "tok123",
    "clipboard_watch": False,
})

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
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(method, path, body=None, headers=None):
    data = None
    h = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    h.update(headers or {})
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with OPENER.open(r, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:  # noqa: BLE001
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return exc.code, raw


print("== 1 Origin 白名单 ==")
st, _ = req("POST", "/api/config", {"threads": 8},
            {"Origin": f"http://127.0.0.1:{PORT}"})
check("同源面板可以写配置", st == 200, st)
st, j = req("POST", "/api/config", {"threads": 9},
            {"Origin": "http://127.0.0.1:9999"})
check("别的 localhost 端口被拒", st == 403, st)
st, _ = req("GET", "/api/state", headers={"Origin": "http://evil.example.com"})
check("外部域名被拒", st == 403, st)
st, _ = req("GET", "/api/state")
check("无 Origin（CLI/扩展后台）放行", st == 200, st)

print("== 2 危险配置键 ==")
before = _config.load().get("ytdlp_path", "")
st, _ = req("POST", "/api/config", {"ytdlp_path": r"C:\Windows\System32\calc.exe"},
            {"Origin": "chrome-extension://abcdefg"})
check("扩展改不了 ytdlp_path", _config.load().get("ytdlp_path", "") == before,
      _config.load().get("ytdlp_path", ""))
st, _ = req("POST", "/api/config", {"after_download_command": "calc.exe"},
            {"Origin": "chrome-extension://abcdefg"})
check("扩展改不了 after_download_command",
      not _config.load().get("after_download_command"), "blocked")

print("== 3 配置脱敏 ==")
st, j = req("GET", "/api/state")
cfg = (j or {}).get("config") or {}
check("proxy_pass 不回明文", cfg.get("proxy_pass") not in ("topsecret",), cfg.get("proxy_pass"))
check("rpc_secret 不回明文", cfg.get("rpc_secret") not in ("tok123",), cfg.get("rpc_secret"))
st, j = req("GET", "/api/config")
check("GET /api/config 同样脱敏",
      ((j or {}).get("config") or {}).get("proxy_pass") != "topsecret")

print("== 4 checksum 路径限制 ==")
inside = DL / "a.bin"
inside.write_bytes(b"hello")
st, j = req("GET", f"/api/checksum?path={urllib.parse.quote(str(inside))}")
check("下载目录内可校验", st == 200 and (j or {}).get("ok") is not False, st)
st, j = req("GET", "/api/checksum?path=C%3A%5CWindows%5Cwin.ini")
check("目录外被拒", st == 403, st)

print("== 5 /rpc 鉴权 ==")
# aria2 的约定：token 是 params 的第一个元素（不是 Authorization 头）
body = {"jsonrpc": "2.0", "id": "t1", "method": "aria2.getVersion", "params": []}
st, j = req("POST", "/rpc", body)
check("无 token 被拒", isinstance(j, dict) and bool((j.get("error"))), j)
good = {"jsonrpc": "2.0", "id": "t1", "method": "aria2.getVersion",
        "params": ["token:tok123"]}
st, j = req("POST", "/rpc", good)
check("正确 token 通过", st == 200 and isinstance(j, dict) and not j.get("error"), j)

print("== 6 更新接口方法 ==")
st, j = req("POST", "/api/update/check", {})
check("POST check 不再 404", st == 200 and isinstance(j, dict), st)
st, j = req("POST", "/api/update/apply", {})
check("apply 返回结构化结果", isinstance(j, dict) and "ok" in j, j)

print("== 收尾 ==")
httpd.shutdown()
tm.shutdown()
print("-" * 60)
print(f"结果: {ok}/{ok + fail} 通过")
sys.exit(0 if fail == 0 else 1)
