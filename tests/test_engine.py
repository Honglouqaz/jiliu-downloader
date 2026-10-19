"""引擎测试套件。

用法:
    # 先起两个测试服务器
    python tests/test_server.py <样本目录> 8899
    python tests/test_server.py <样本目录> 8900 --no-range

    # 再跑测试
    python tests/test_engine.py

覆盖:
    T1  探测（Content-Length / Accept-Ranges / 文件名）
    T2  8 线程分片下载 —— sha256 必须一致
    T3  1 线程下载      —— sha256 必须一致
    T4  暂停 / 恢复      —— sha256 必须一致
    T5  取消 / 续传      —— sha256 必须一致（走 sidecar）
    T6  小文件退化单段
    T7  限速生效
    T8  404 返回错误而非抛异常
    T9  服务器不支持 Range 时退化并仍然正确
    T10 动态分片：分片总数必须超过线程数
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jiliu import config as jcfg  # noqa: E402
from jiliu.engine import RangeDownloader, human_size, probe, sha256_file  # noqa: E402

BASE = "http://127.0.0.1:8899"
BASE_NORANGE = "http://127.0.0.1:8900"
BASE_SLOW = "http://127.0.0.1:8901"
SRC = pathlib.Path(tempfile.gettempdir()) / "jl_srv"
WORK = pathlib.Path(tempfile.gettempdir()) / "jl_work"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(cond)


def workfile(name: str) -> pathlib.Path:
    p = WORK / name
    if p.exists():
        p.unlink()
    side = pathlib.Path(str(p) + ".jiliu.json")
    if side.exists():
        side.unlink()
    return p


def expected_sha(name: str) -> str:
    return hashlib.sha256((SRC / name).read_bytes()).hexdigest()


def run_async(dl: RangeDownloader):
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(dl.run()), daemon=True)
    t.start()
    return t, box


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    cfg = jcfg.load()
    cfg = dict(cfg)
    cfg["timeout"] = 15
    cfg["max_retries"] = 3
    # 测试时用小分片，让动态切分更容易观察到
    cfg["chunk_min"] = 256 * 1024
    cfg["chunk_max"] = 1024 * 1024

    print("=" * 70)
    print("汲流 引擎测试套件")
    print("=" * 70)
    print(f"样本目录: {SRC}")
    print(f"工作目录: {WORK}")
    print()

    # ---------------- T1 探测 ----------------
    print("T1  探测")
    pr = probe(f"{BASE}/big.bin", cfg)
    check("T1.1 拿到 Content-Length", pr.size == (SRC / "big.bin").stat().st_size, f"size={pr.size}")
    check("T1.2 识别 Accept-Ranges", pr.accept_ranges is True)
    check("T1.3 文件名解析", pr.filename == "big.bin", pr.filename)

    # ---------------- T2 8 线程 ----------------
    print("\nT2  8 线程分片下载（24 MB）")
    dst = workfile("big_8t.bin")
    max_seg = {"n": 0}

    def on_p(s):
        max_seg["n"] = max(max_seg["n"], s["segments_total"])

    dl = RangeDownloader(f"{BASE}/big.bin", dst, cfg=cfg, threads=8, progress_cb=on_p)
    t0 = time.time()
    res = dl.run()
    dt = time.time() - t0
    check("T2.1 下载成功", res["ok"], str(res.get("error")))
    check(
        "T2.2 文件大小正确",
        dst.exists() and dst.stat().st_size == (SRC / "big.bin").stat().st_size,
        f"{dst.stat().st_size if dst.exists() else 0} bytes",
    )
    check("T2.3 sha256 一致", sha256_file(dst) == expected_sha("big.bin"))
    check("T2.4 sidecar 已清理", not pathlib.Path(str(dst) + ".jiliu.json").exists())
    print(f"       耗时 {dt:.2f}s，{human_size(res['bytes'] / max(dt, 1e-9))}/s")

    # ---------------- T10 动态分片 ----------------
    print("\nT10 动态分片")
    created = dl.snapshot()["segments_created"]
    check(
        "T10.1 累计切出的分片数 > 线程数（说明发生了动态切分）",
        created > 8,
        f"共切出 {created} 个分片 / 8 线程",
    )

    # ---------------- T3 1 线程 ----------------
    print("\nT3  1 线程下载")
    dst = workfile("big_1t.bin")
    dl = RangeDownloader(f"{BASE}/big.bin", dst, cfg=cfg, threads=1)
    res = dl.run()
    check("T3.1 下载成功", res["ok"], str(res.get("error")))
    check("T3.2 sha256 一致", sha256_file(dst) == expected_sha("big.bin"))

    # ---------------- T4 暂停 / 恢复 ----------------
    print("\nT4  暂停 / 恢复（走限速服务器，便于观测）")
    dst = workfile("big_pause.bin")
    dl = RangeDownloader(f"{BASE_SLOW}/big.bin", dst, cfg=cfg, threads=4)
    t, box = run_async(dl)
    # 等到有进度再暂停
    deadline = time.time() + 20
    while time.time() < deadline and dl.done_bytes() < 2 * 1024 * 1024:
        time.sleep(0.05)
    progress_before_pause = dl.done_bytes()
    dl.pause()
    time.sleep(0.8)
    d1 = dl.done_bytes()
    time.sleep(0.8)
    d2 = dl.done_bytes()
    check("T4.0 暂停前确实有进度", progress_before_pause > 0, human_size(progress_before_pause))
    check("T4.1 暂停后字节数基本停止增长", abs(d2 - d1) < 512 * 1024, f"{d1} -> {d2}")
    dl.resume()
    t.join(timeout=120)
    check("T4.2 恢复后下载完成", box.get("ok") is True, str(box.get("error")))
    check("T4.3 sha256 一致", sha256_file(dst) == expected_sha("big.bin"))

    # ---------------- T5 取消 / 续传 ----------------
    print("\nT5  取消 / 断点续传")
    dst = workfile("big_cancel.bin")
    dl = RangeDownloader(f"{BASE_SLOW}/big.bin", dst, cfg=cfg, threads=4)
    t, box = run_async(dl)
    deadline = time.time() + 20
    while time.time() < deadline and dl.done_bytes() < 3 * 1024 * 1024:
        time.sleep(0.05)
    dl.cancel()
    t.join(timeout=30)
    side = pathlib.Path(str(dst) + ".jiliu.json")
    check("T5.1 取消后 sidecar 被保留", side.exists())
    partial = dl.done_bytes()
    check("T5.2 取消时已有实质进度", partial > 1024 * 1024, human_size(partial))
    print(f"       取消时已完成 {human_size(partial)}")

    dl2 = RangeDownloader(f"{BASE_SLOW}/big.bin", dst, cfg=cfg, threads=4)
    resume_logs: list[str] = []
    dl2.log_cb = resume_logs.append
    res2 = dl2.run()
    check("T5.3 续传成功", res2["ok"], str(res2.get("error")))
    check("T5.4 sha256 一致（说明没有空洞）", sha256_file(dst) == expected_sha("big.bin"))
    rb = res2.get("resumed_bytes", 0)
    check(
        "T5.5 确实复用了已下载的字节（不是重下）",
        rb > 1024 * 1024,
        f"续传复用 {human_size(rb)}，日志: {' | '.join(resume_logs[:1])}",
    )

    # ---------------- T6 小文件 ----------------
    print("\nT6  小文件退化单段")
    dst = workfile("small.bin")
    dl = RangeDownloader(f"{BASE}/small.bin", dst, cfg=cfg, threads=8)
    res = dl.run()
    check("T6.1 下载成功", res["ok"], str(res.get("error")))
    check("T6.2 内容一致", dst.read_bytes() == (SRC / "small.bin").read_bytes())

    # ---------------- T7 限速 ----------------
    print("\nT7  限速（8 MB 文件 @ 2 MB/s）")
    dst = workfile("mid_limited.bin")
    limit = 2 * 1024 * 1024
    dl = RangeDownloader(f"{BASE}/mid.bin", dst, cfg=cfg, threads=4, speed_limit=limit)
    t0 = time.time()
    res = dl.run()
    dt = time.time() - t0
    size = (SRC / "mid.bin").stat().st_size
    measured = size / max(dt, 1e-9)
    # 令牌桶允许一次性突发一个"桶容量"（= 1 秒的量），所以平均速率会略高于限速
    check("T7.1 下载成功", res["ok"], str(res.get("error")))
    check(
        "T7.2 平均速率不超过限速的 1.45 倍（令牌桶突发）",
        measured <= limit * 1.45,
        f"实测 {human_size(measured)}/s，限速 {human_size(limit)}/s",
    )
    check(
        "T7.3 没有瞬间跑完（限速确实生效）",
        dt >= size / limit * 0.7,
        f"实际 {dt:.2f}s，理论下限 {size / limit:.2f}s",
    )
    check("T7.4 sha256 一致", sha256_file(dst) == expected_sha("mid.bin"))

    # ---------------- T8 404 ----------------
    print("\nT8  404 错误处理")
    dst = workfile("nope.bin")
    dl = RangeDownloader(f"{BASE}/definitely-missing.bin", dst, cfg=cfg, threads=4)
    res = dl.run()
    check("T8.1 返回 ok=False 而不是抛异常", res["ok"] is False)
    check("T8.2 错误信息里带 HTTP 状态", "404" in str(res.get("error")), str(res.get("error")))

    # ---------------- T9 不支持 Range ----------------
    print("\nT9  服务器不支持 Range 时退化")
    dst = workfile("big_norange.bin")
    dl = RangeDownloader(f"{BASE_NORANGE}/big.bin", dst, cfg=cfg, threads=8)
    logs: list[str] = []
    dl.log_cb = logs.append
    res = dl.run()
    check("T9.1 仍然下载成功", res["ok"], str(res.get("error")))
    check("T9.2 sha256 一致", sha256_file(dst) == expected_sha("big.bin"))
    check("T9.3 记录了退化日志", any("Range" in m for m in logs), " | ".join(logs[:2]))

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"结果: {passed}/{len(RESULTS)} 通过")
    if failed:
        print("失败项:")
        for n in failed:
            print("  - " + n)
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
