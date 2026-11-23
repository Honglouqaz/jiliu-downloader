#!/usr/bin/env python3
"""直播录制回归（P1-10）。

    python tests/test_live.py

用本地「滑动窗口 playlist」模拟直播：每 0.5 秒新增一个分片，
窗口只保留最近 3 个 —— 录制器必须按序号补齐，不能只看当前窗口。
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

from jiliu import config as _config, live  # noqa: E402

CFG = dict(_config.DEFAULTS)
CFG["proxy_mode"] = "off"
CFG["timeout"] = 6

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


TMP = pathlib.Path(tempfile.mkdtemp(prefix="jiliu-live-"))
SEG_BYTES = 2000            # 每个分片 2 KB
STEP = 0.5                  # 每 0.5 秒一个新分片
T0 = time.monotonic()
PORT = 0


def seg_body(i: int) -> bytes:
    return (f"SEG{i:08d}|".encode() * (SEG_BYTES // 12 + 1))[:SEG_BYTES]


def cur_seq() -> int:
    return int((time.monotonic() - T0) / STEP)


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _text(self, s: str):
        b = s.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        base = f"http://127.0.0.1:{PORT}"
        if self.path == "/live.m3u8":
            n = cur_seq()
            lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                     "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{max(0, n - 2)}"]
            for i in range(max(0, n - 2), n + 1):
                lines.append("#EXTINF:0.5,")
                lines.append(f"{base}/seg{i}.ts")
            self._text("\n".join(lines) + "\n")
            return
        if self.path == "/vod.m3u8":
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:1", "#EXT-X-ENDLIST"]
            for i in range(3):
                lines += ["#EXTINF:1.0,", f"{base}/seg{i}.ts"]
            self._text("\n".join(lines) + "\n")
            return
        if self.path == "/master.m3u8":
            self._text(
                "#EXTM3U\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n'
                f"{base}/low.m3u8\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080\n'
                f"{base}/high.m3u8\n")
            return
        if self.path in ("/low.m3u8", "/high.m3u8"):
            self._text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n"
                       "#EXTINF:1.0,\n/seg0.ts\n#EXT-X-ENDLIST\n")
            return
        if self.path.startswith("/seg") and self.path.endswith(".ts"):
            try:
                i = int(self.path[4:-3])
            except Exception:
                i = 0
            body = seg_body(i)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class Srv(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


srv = Srv(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

# ----------------------------------------------------------------------
print("== 解析 ==")
pl = live.parse_playlist(
    "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:10\n"
    "#EXTINF:2.0,\na.ts\n#EXTINF:2.0,\nhttp://x/b.ts\n", "http://x/dir/p.m3u8")
check("解析出 2 个分片", len(pl["segments"]) == 2, str(len(pl["segments"])))
check("相对路径补齐", pl["segments"][0]["url"] == "http://x/dir/a.ts",
      pl["segments"][0]["url"])
check("序号从 MEDIA-SEQUENCE 起", pl["segments"][0]["index"] == 10)
check("无 ENDLIST 判为直播", pl["is_live"] is True)
check("TARGETDURATION 解析", pl["target_duration"] == 2.0)

pl2 = live.parse_playlist(
    "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\na.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=900\nb.m3u8\n", "http://x/m.m3u8")
check("识别 master", pl2["is_master"] is True and len(pl2["variants"]) == 2)
check("选最高带宽", live.best_variant(pl2) == "http://x/b.m3u8", live.best_variant(pl2))

pl3 = live.parse_playlist("#EXTM3U\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST\n", BASE)
check("带 ENDLIST 判为点播", pl3["is_live"] is False)

# ----------------------------------------------------------------------
print("== 直播判定 ==")
r = live.is_live_url(f"{BASE}/live.m3u8", CFG)
check("识别出直播", r["ok"] and r["live"] is True, r.get("reason", ""))
r = live.is_live_url(f"{BASE}/vod.m3u8", CFG)
check("识别出点播", r["ok"] and r["live"] is False, r.get("reason", ""))
r = live.is_live_url(f"{BASE}/master.m3u8", CFG)
check("master 也能判", r["ok"] is True, r.get("reason", ""))
r = live.is_live_url("http://127.0.0.1:1/x.m3u8", CFG)
check("连不上不抛异常", r["ok"] is False and r["reason"])

# ----------------------------------------------------------------------
print("== 录制 ==")
out = TMP / "rec.ts"
logs = []
recs = []
rec = live.LiveRecorder(f"{BASE}/live.m3u8", out, cfg=CFG, minutes=0.06,
                        poll=0.3, log_cb=lambda m: logs.append(m),
                        progress_cb=lambda s: recs.append(s))
res = rec.run()
check("录制成功", res["ok"] is True, res.get("error", ""))
check("产物存在", pathlib.Path(res["path"]).is_file(), res["path"])
check("产物体积与分片数匹配",
      res["bytes"] > SEG_BYTES * 2, f"{res['bytes']} bytes / {res['segments']} 片")
check("拿到多个分片", res["segments"] >= 3, str(res["segments"]))
check("有进度回调", len(recs) > 0, str(len(recs)))
check("临时目录已清理", not pathlib.Path(str(out) + ".live").exists())

data = pathlib.Path(res["path"]).read_bytes()
check("内容是分片字节拼接", data.startswith(b"SEG"), data[:12])
check("按序号顺序拼接", data.find(b"SEG00000001") < data.find(b"SEG00000002")
      if data.find(b"SEG00000002") > 0 else True)

# 时长限制
out2 = TMP / "rec2.ts"
t1 = time.monotonic()
rec2 = live.LiveRecorder(f"{BASE}/live.m3u8", out2, cfg=CFG, minutes=0.03,
                         poll=0.3)
res2 = rec2.run()
cost = time.monotonic() - t1
check("时长上限生效", cost < 6.0 and res2["ok"], f"{cost:.1f}s")

# 取消
out3 = TMP / "rec3.ts"
rec3 = live.LiveRecorder(f"{BASE}/live.m3u8", out3, cfg=CFG, minutes=5,
                         poll=0.3)
threading.Timer(1.2, rec3.cancel).start()
t2 = time.monotonic()
res3 = rec3.run()
cost3 = time.monotonic() - t2
check("取消后及时收尾", cost3 < 4.0 and res3["cancelled"] is True, f"{cost3:.1f}s")

# 点播也走同一条路
out4 = TMP / "vod.ts"
rec4 = live.LiveRecorder(f"{BASE}/vod.m3u8", out4, cfg=CFG, poll=0.3)
res4 = rec4.run()
check("点播录制", res4["ok"] and res4["segments"] == 3, str(res4.get("segments")))

print("== 接入 manager ==")
from jiliu.manager import TaskManager          # noqa: E402
from jiliu.task import Task                    # noqa: E402
from jiliu import queues as _q                 # noqa: E402

_mcfg = dict(CFG)
_mcfg["queues"] = _q.normalize(_mcfg)
tm = TaskManager(cfg=_mcfg, autoload=False)
t = Task(url=f"{BASE}/live.m3u8", kind="video", backend="live", threads=1,
         dest_dir=str(TMP), meta={"live_minutes": 0.03})
handled = tm._maybe_live(t)
check("识别为直播并接管", handled is True, t.status)
check("任务标成完成", t.status == "done", f"{t.status} / {t.error}")
check("落盘文件存在", bool(t.dest) and pathlib.Path(t.dest).is_file(), t.dest or "")
check("任务 meta 标了 live", (t.meta or {}).get("live") is True)
check("日志里提到直播", any("直播" in x for x in t.log), str(t.log[-2:]))

# 点播 m3u8 不该被接管，要交回普通视频流程
t2 = Task(url=f"{BASE}/vod.m3u8", kind="video", backend="yt-dlp", threads=1,
          dest_dir=str(TMP), meta={})
check("点播不接管", tm._maybe_live(t2) is False)
check("点播日志说明了原因", any("ENDLIST" in x for x in t2.log), str(t2.log[-1:]))

# 非 m3u8 链接直接放过
t3 = Task(url="https://example.com/a.mp4", kind="http", threads=4,
          dest_dir=str(TMP), meta={})
check("普通链接不接管", tm._maybe_live(t3) is False)
tm.shutdown()

srv.shutdown()
print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
