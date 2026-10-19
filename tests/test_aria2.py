"""aria2 后端测试。

用的是真实的 aria2c（scoop 安装的 1.37.0），不是 mock —— 端口、RPC 协议、
进程生命周期都是真的。需要先起测试服务器：

    python tests/test_server.py <样本目录> 8899
    python tests/test_server.py <样本目录> 8901 --rate 3145728
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jiliu import config as jcfg  # noqa: E402
from jiliu import tools  # noqa: E402
from jiliu.backends import aria2 as A  # noqa: E402
from jiliu.engine import human_size  # noqa: E402

BASE = "http://127.0.0.1:8899"
BASE_SLOW = "http://127.0.0.1:8901"
SRC = pathlib.Path(tempfile.gettempdir()) / "jl_srv"
WORK = pathlib.Path(tempfile.gettempdir()) / "jl_work_aria2"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(cond)


def sha(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    cfg = dict(jcfg.load())

    print("=" * 70)
    print("汲流 aria2 后端测试")
    print("=" * 70)

    # ---------------- T1 探测 ----------------
    print("\nT1  探测")
    info = A.detect(cfg)
    check("T1.1 找到 aria2c", bool(info["binary"]), info["binary"])
    check("T1.2 拿到版本", "1.37" in info["binary_version"], info["binary_version"])

    if not info["binary"]:
        print("\n未安装 aria2c，跳过后续测试。装一个：scoop install aria2")
        return 1

    # ---------------- T2 拉起 RPC 服务 ----------------
    print("\nT2  拉起 aria2c RPC 服务（真进程）")
    cfg["aria2_port"] = 6899
    svc = A.Aria2Service(cfg)
    try:
        client = svc.start()
        ver = client.version()
        check("T2.1 RPC 可用", bool(ver.get("version")), f"aria2 {ver.get('version')}")
        check("T2.2 进程已启动", svc.proc is not None and svc.proc.poll() is None)
        gs = client.global_stat()
        check("T2.3 能读全局状态", "downloadSpeed" in gs, str(gs)[:70])

        # aria2 遇到**不认的选项**不会警告，而是打印 usage 直接退出（码 28），
        # RPC 整条起不来；上层还会悄悄回退到自研引擎，看着只像「aria2 没生效」。
        # 踩过一次：BT 参数里写了 --bt-load-error-ignore（aria2 根本没这选项）。
        # 所以这里拿 aria2 自己的选项表把每个 --xxx 都核一遍 ——
        # 这一类 bug 以后不用靠人去记。
        raw = subprocess.run([info["binary"], "-h#all"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace").stdout
        valid = set(re.findall(r"--([a-z0-9][a-z0-9-]*)", raw))
        check("T2.4 拿到 aria2 选项表", len(valid) > 100, f"{len(valid)} 个选项")

        for label, c2 in [("默认（BT 开）", dict(cfg)),
                          ("BT 关", {**cfg, "bt_enabled": False}),
                          ("BT 开 + 额外 tracker", {**cfg, "bt_trackers": "udp://a:1/announce"}),
                          ("BT 开 + 限速 + UA/Referer",
                           {**cfg, "speed_limit": 1024 * 512,
                            "user_agent": "UA/1", "referer": "https://x/"})]:
            bad = []
            for a in A.Aria2Service(c2).build_args()[1:]:
                if a.startswith("--"):
                    name = a[2:].split("=", 1)[0]
                    if name not in valid:
                        bad.append(name)
            check(f"T2.5 参数名全被 aria2 认（{label}）", not bad, "不认的：" + str(bad))

        # 默认必须把找源的三件套打开，否则 Windows 上跑 BT 基本没源
        joined = " ".join(A.Aria2Service(dict(cfg)).build_args())
        check("T2.6 默认打开了 DHT / PEX / LPD",
              all(k in joined for k in ("--enable-dht=true", "--enable-peer-exchange=true",
                                        "--bt-enable-lpd=true")))
        check("T2.7 默认不做种（用户要的是下文件，不是占上行）",
              "--seed-time=0" in joined, joined[-120:])
        check("T2.8 关掉 BT 时不带 DHT 参数",
              "--enable-dht" not in " ".join(
                  A.Aria2Service({**cfg, "bt_enabled": False}).build_args()))

        # ---- T2.9~T2.11 BT / 磁力 的 RPC 通路 ----
        # 这里**验不到真正的 P2P 传输** —— 那需要一个活着的种子群，
        # 自动化测试里既慢又不稳。能验的是：磁力能被 aria2 接住、
        # gid 拿得到、BT 专属状态字段读得出来、任务能被干净地撤掉。
        MAGNET = ("magnet:?xt=urn:btih:0000000000000000000000000000000000000000"
                  "&dn=jiliu-bt-smoke&tr=udp%3A%2F%2F127.0.0.1%3A1%2Fannounce")
        try:
            gid = client.add_uri([MAGNET], {"dir": str(WORK), "seed-time": "0",
                                            "bt-save-metadata": "true"})
            check("T2.9 磁力能被 aria2 接住并返回 gid", bool(gid), str(gid))
            st = client.tell_status(gid, ["gid", "status", "infoHash", "bittorrent",
                                          "numSeeders", "connections"])
            check("T2.10 BT 任务状态可读", st.get("gid") == gid, str(st)[:80])
            check("T2.11 tellStatus 带 bittorrent 段",
                  "bittorrent" in st or st.get("infoHash") is not None, str(st)[:120])
            client.remove(gid)
        except Exception as exc:  # noqa: BLE001
            check("T2.9 磁力能被 aria2 接住并返回 gid", False, f"{type(exc).__name__}: {exc}")

        # ---- T2.12 运行中改选项 ----
        try:
            gid2 = client.add_uri([f"{BASE}/big.bin"], {"dir": str(WORK)})
            client.change_option(gid2, {"max-download-limit": "1048576"})
            check("T2.12 运行中能改选项（临时限速）", True)
            client.remove(gid2)
        except Exception as exc:  # noqa: BLE001
            check("T2.12 运行中能改选项（临时限速）", False, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        check("T2.1 RPC 可用", False, str(exc))
        svc.stop()
        return 1

    expected = sha(SRC / "big.bin")

    # ---------------- T3 下载 ----------------
    print("\nT3  通过 aria2 下载 24 MB")
    dst = WORK / "big_aria2.bin"
    if dst.exists():
        dst.unlink()
    events: list[dict] = []

    def on_p(s):
        events.append(s)

    dl = A.Aria2Downloader(
        f"{BASE}/big.bin", dst, cfg=cfg, client=client, threads=16, progress_cb=on_p
    )
    t0 = time.time()
    res = dl.run()
    dt = time.time() - t0
    check("T3.1 下载成功", res["ok"], str(res.get("error")))
    check("T3.2 文件存在且大小正确", dst.exists() and dst.stat().st_size == (SRC / "big.bin").stat().st_size,
          f"{dst.stat().st_size if dst.exists() else 0} bytes")
    check("T3.3 sha256 一致", sha(dst) == expected)
    check("T3.4 有进度事件", len(events) > 0, f"{len(events)} 次回调")
    if events:
        check("T3.5 事件里带 gid 与 aria2 状态", bool(events[-1].get("gid")), str(events[-1].get("aria2_status")))
    print(f"       耗时 {dt:.2f}s，avg {human_size(res['bytes'] / max(dt, 1e-9))}/s")

    # ---------------- T4 暂停 / 恢复 ----------------
    print("\nT4  暂停 / 恢复（限速 2 MB/s 便于观测）")
    dst = WORK / "pause_aria2.bin"
    if dst.exists():
        dst.unlink()
    dl = A.Aria2Downloader(
        f"{BASE_SLOW}/big.bin", dst, cfg=cfg, client=client, threads=4, speed_limit=2 * 1024 * 1024
    )
    box: dict = {}
    th = threading.Thread(target=lambda: box.update(dl.run()), daemon=True)
    th.start()
    deadline = time.time() + 25
    while time.time() < deadline and dl.done_bytes() < 2 * 1024 * 1024:
        time.sleep(0.1)
    before = dl.done_bytes()
    dl.pause()
    time.sleep(1.5)
    p1 = dl.done_bytes()
    time.sleep(1.5)
    p2 = dl.done_bytes()
    check("T4.0 暂停前有进度", before > 0, human_size(before))
    check("T4.1 暂停后不再增长", abs(p2 - p1) < 512 * 1024, f"{p1} -> {p2}")
    dl.resume()
    th.join(timeout=180)
    check("T4.2 恢复后完成", box.get("ok") is True, str(box.get("error")))
    check("T4.3 sha256 一致", sha(dst) == expected)

    # ---------------- T5 404 ----------------
    print("\nT5  404 错误处理")
    dst = WORK / "nope_aria2.bin"
    if dst.exists():
        dst.unlink()
    dl = A.Aria2Downloader(f"{BASE}/missing-file.bin", dst, cfg=cfg, client=client, threads=4)
    res = dl.run()
    check("T5.1 返回 ok=False", res["ok"] is False)
    check("T5.2 错误信息非空", bool(res.get("error")), str(res.get("error"))[:90])

    # ---------------- T6 关停 ----------------
    print("\nT6  关停服务")
    pid = svc.proc.pid if svc.proc else None
    svc.stop()
    time.sleep(0.5)
    check("T6.1 aria2c 进程已退出", svc.proc is None or svc.proc.poll() is not None, f"pid={pid}")

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
