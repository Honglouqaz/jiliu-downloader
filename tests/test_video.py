"""yt-dlp 集成测试。

外网视频站访问受限，所以这里用 ffmpeg 现场造一个真 MP4，
放在本地 HTTP 服务器上，让 yt-dlp 走 generic 提取器完整跑一遍。
这样验证的是「参数拼装 + 进度解析 + 进程管理」这条真实链路，
不是 mock。

    python tests/test_server.py <样本目录> 8899   # 目录里要有 test.mp4
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jiliu import config as jcfg  # noqa: E402
from jiliu import tools, video  # noqa: E402

BASE = "http://127.0.0.1:8899"
SRC = pathlib.Path(tempfile.gettempdir()) / "jl_srv"
WORK = pathlib.Path(tempfile.gettempdir()) / "jl_work_video"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(cond)


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    cfg = dict(jcfg.load())

    print("=" * 70)
    print("汲流 yt-dlp 集成测试")
    print("=" * 70)

    # ---------------- T1 可用性 ----------------
    print("\nT1  yt-dlp 可用性")
    av = video.available(cfg)
    check("T1.1 找到 yt-dlp", av["available"], av["path"])
    check("T1.2 拿到版本", bool(av["version"]), av["version"])
    check("T1.3 找到 ffmpeg（合并需要）", bool(av["ffmpeg_dir"]), av["ffmpeg_dir"])
    if not av["available"]:
        return 1

    # ---------------- T2 进度行解析（纯函数） ----------------
    print("\nT2  进度行解析")
    cases = [
        ("[download]  45.2% of   12.34MiB at    1.23MiB/s ETA 00:05",
         {"percent": 45.2, "speed_text": "1.23MiB/s", "eta_text": "00:05"}),
        ("[download] 100% of 3.00MiB in 00:02", {"percent": 100.0}),
        ("JLPROG:1048576|4194304|524288|6|25.0%",
         {"done": 1048576.0, "total": 4194304.0, "speed": 524288.0, "eta": 6.0}),
    ]
    for raw, want in cases:
        ev = video.parse_line(raw)
        ok = ev is not None and all(abs(ev.get(k, -999) - v) < 0.01 if isinstance(v, float) else ev.get(k) == v
                                    for k, v in want.items())
        check(f"T2 解析 {raw[:38]}...", ok, str(ev)[:80] if ev else "None")

    check("T2.4 非进度行返回 None", video.parse_line("[info] Downloading webpage") is None)

    # ---------------- T3 探测本地视频 ----------------
    print("\nT3  探测（对本地 MP4 走 generic 提取器）")
    try:
        info = video.probe(f"{BASE}/test.mp4", cfg, timeout=60)
        check("T3.1 探测成功", bool(info.get("id") or info.get("title")), str(info.get("title"))[:50])
        check("T3.2 返回格式列表（直链也要有合成条目）", len(info.get("formats") or []) > 0,
              f"{len(info.get('formats') or [])} 个格式")
        check("T3.5 直链条目标记为 direct",
              bool((info.get("formats") or [{}])[0].get("direct")),
              str((info.get("formats") or [{}])[0].get("note")))
        check("T3.6 DRM 字段存在（用于边界提示）", "has_drm" in info, f"has_drm={info.get('has_drm')}")
        check("T3.3 提取器识别", bool(info.get("extractor")), str(info.get("extractor")))
        check("T3.4 元数据结构完整",
              all(k in info for k in ("title", "duration_text", "thumbnail", "formats")))
    except Exception as exc:  # noqa: BLE001
        check("T3.1 探测成功", False, str(exc)[:120])

    # ---------------- T4 下载 + 进度回调 ----------------
    print("\nT4  下载（含进度回调）")
    events: list[dict] = []

    def on_p(s):
        events.append(s)

    logs: list[str] = []
    try:
        res = video.download(
            f"{BASE}/test.mp4", WORK, cfg,
            fmt="b", progress_cb=on_p, log_cb=logs.append,
        )
        check("T4.1 下载成功", res["ok"], str(res.get("error")))
        files = list(WORK.glob("*"))
        check("T4.2 产物落盘", len(files) > 0, ", ".join(f.name for f in files[:3]))
        if files:
            biggest = max(files, key=lambda p: p.stat().st_size)
            check("T4.3 文件非空", biggest.stat().st_size > 1000, f"{biggest.name} {biggest.stat().st_size} bytes")
        check("T4.4 有进度事件", len(events) > 0, f"{len(events)} 次回调")
        check("T4.5 返回 runner 标记", res.get("runner") == "yt-dlp", str(res.get("runner")))
    except Exception as exc:  # noqa: BLE001
        check("T4.1 下载成功", False, str(exc)[:120])

    # ---------------- T5 取消 ----------------
    print("\nT5  取消（进程终止）")
    ev_cancel = threading.Event()
    box: dict = {}

    def run():
        try:
            box.update(video.download(f"{BASE}/big.bin", WORK / "cancel", cfg,
                                      fmt="b", cancel_event=ev_cancel))
        except Exception as exc:  # noqa: BLE001
            box.update({"ok": False, "error": str(exc)})

    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(1.0)
    ev_cancel.set()
    th.join(timeout=40)
    check("T5.1 取消后返回 cancelled 或 ok=False",
          box.get("cancelled") is True or box.get("ok") is False, str(box)[:90])

    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"结果: {passed}/{len(RESULTS)} 通过")
    for n in failed:
        print("  失败: " + n)
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
