"""本地嗅探代理（IDM 网络驱动的不装驱动版）。

IDM 用 WFP 内核驱动截获全系统流量，所以不装扩展的浏览器、Electron 客户端、
内嵌 WebView 都能被接管。抄驱动不值当（要签名、天天被杀软拦），
折中方案是**本地 HTTP 代理**：把浏览器/客户端的代理指过来，命中媒体就记一笔。

能力边界（写在前面，免得误会）：
  - HTTP：能看到完整 URL + Content-Type，判断最准；
  - HTTPS：只能 CONNECT 隧道，看得到域名看不到路径 —— 所以只能靠"CONNECT 之后
    出现的大流量"这种弱信号，默认不报（误报比漏报更烦人）。
  - 只监听 127.0.0.1，默认关闭。
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

MEDIA_TYPES = ("video/", "audio/", "application/vnd.apple.mpegurl",
               "application/x-mpegurl", "application/dash+xml")
MEDIA_EXT = (".mp4", ".m4v", ".webm", ".mkv", ".flv", ".ts", ".m3u8", ".mpd",
             ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus")

_recent: List[Dict[str, Any]] = []
_lock = threading.Lock()
_server: Optional[ThreadingHTTPServer] = None
_thread: Optional[threading.Thread] = None


def _is_media(url: str, ctype: str, size: int) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    if any(t in (ctype or "").lower() for t in MEDIA_TYPES):
        return True
    if path.endswith(MEDIA_EXT):
        return True
    # 没有后缀、也没有 Content-Type 的大响应：可能是分片，先记下来
    return size >= 2 * 1024 * 1024 and not path.endswith((".html", ".htm", ".js", ".css", ".json"))


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cfg: Dict[str, Any] = {}

    def log_message(self, fmt, *args):  # noqa: D401
        pass

    def do_GET(self):
        url = self.path
        if url.startswith("/"):
            self.send_error(400, "只接受代理请求（绝对 URL）")
            return
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": self.headers.get("User-Agent") or "Mozilla/5.0",
                "Accept": self.headers.get("Accept") or "*/*",
            })
            # 不跟着别人的 Cookie 走：代理只搬字节，不做登录态中转
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(req, timeout=60) as r:
                ctype = r.headers.get("Content-Type") or ""
                length = int(r.headers.get("Content-Length") or 0)
                if _is_media(url, ctype, length):
                    _record(url, ctype, length)
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("transfer-encoding", "connection", "content-encoding"):
                        continue
                    self.send_header(k, v)
                if not r.headers.get("Content-Length"):
                    self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception:  # noqa: BLE001
            try:
                self.send_error(502, "上游请求失败")
            except Exception:  # noqa: BLE001
                pass

    def do_CONNECT(self):
        """HTTPS 隧道：只转发，不解析（见模块开头的能力边界）。"""
        host, _, port = self.path.partition(":")
        port = int(port or 443)
        try:
            upstream = socket.create_connection((host, port), timeout=15)
        except Exception:  # noqa: BLE001
            self.send_error(502, "连接目标失败")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        try:
            client = self.connection
            client.setblocking(True)

            def pump(src, dst):
                try:
                    while True:
                        data = src.recv(64 * 1024)
                        if not data:
                            break
                        dst.sendall(data)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except Exception:  # noqa: BLE001
                        pass

            t = threading.Thread(target=pump, args=(client, upstream), daemon=True)
            t.start()
            pump(upstream, client)
            t.join(timeout=1)
        finally:
            try:
                upstream.close()
            except Exception:  # noqa: BLE001
                pass


def _record(url: str, ctype: str, size: int) -> None:
    try:
        from . import capture_rules
        ok, reason = capture_rules.should_capture(url, _Handler.cfg, size=size or None,
                                                  mime=ctype)
        if not ok:
            return
    except Exception:  # noqa: BLE001
        pass
    with _lock:
        if any(x.get("url") == url for x in _recent[-30:]):
            return
        _recent.append({"url": url, "type": ctype or "", "size": int(size or 0),
                        "at": time.time()})
        del _recent[:-100]


def start(cfg: Dict[str, Any], port: int = 0) -> Dict[str, Any]:
    global _server, _thread
    if _server is not None:
        return {"ok": True, "port": _server.server_address[1], "note": "已在运行"}
    port = int(port or cfg.get("sniff_proxy_port", 8899) or 8899)
    handler = type("SniffHandler", (_Handler,), {"cfg": cfg})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    _server = srv
    _thread = threading.Thread(target=srv.serve_forever, daemon=True, name="jiliu-sniff")
    _thread.start()
    return {"ok": True, "port": port,
            "note": f"把浏览器/客户端的 HTTP 代理指到 127.0.0.1:{port}"}


def stop() -> bool:
    global _server, _thread
    if _server is None:
        return False
    try:
        _server.shutdown()
        _server.server_close()
    except Exception:  # noqa: BLE001
        pass
    _server = None
    _thread = None
    return True


def recent(limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        return list(_recent[-limit:])


def clear() -> None:
    with _lock:
        _recent.clear()


def status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "enabled": _server is not None,
        "port": (_server.server_address[1] if _server else int(cfg.get("sniff_proxy_port", 8899))),
        "captured": len(recent()),
        "https_note": "HTTPS 只能隧道转发，看不到路径；要精确嗅探 HTTPS 媒体请用浏览器扩展",
    }
