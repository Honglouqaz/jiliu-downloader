"""测试前置条件：样本文件 + 本地测试服务器。

test_engine / test_aria2 / test_video 这三个套件都假定「8899/8900/8901 上
已经跑着 tests/test_server.py」，以前靠人手起。人手漏一步，整套就从全绿变成
满屏 WinError 10061（目标计算机积极拒绝），而且看起来特别像代码坏了 ——
实际只是服务没开。所以这里把它自动化。

用法::

    from _fixtures import ensure_samples, Servers

    ensure_samples()
    with Servers():
        ...  # 三个服务器都在跑，退出时自动收
"""
from __future__ import annotations

import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

SRC = pathlib.Path(tempfile.gettempdir()) / "jl_srv"

# 三个端口的用途（改这里就要同步改 test_engine.py 顶部的 BASE_* 常量）
#   8899  普通，支持 Range
#   8900  假装不支持 Range —— 测退化到单段的路径
#   8901  全局限速 3 MiB/s —— 让「暂停/取消」有足够长的窗口可以观察
PORT_PLAIN = 8899
PORT_NORANGE = 8900
PORT_SLOW = 8901
SLOW_RATE = 3 * 1024 * 1024

BIG = 24 * 1024 * 1024
MID = 8 * 1024 * 1024


def _seed_blob(n: int, seed: int = 20260923) -> bytes:
    """造一段可复现的伪随机数据。

    用固定种子而不是 os.urandom，是为了让样本每次跑都一样 ——
    出问题时能拿两次的 sha256 直接对比。
    """
    import random
    rng = random.Random(seed)
    return rng.randbytes(n)


def _need(path: pathlib.Path, size: int | None = None) -> bool:
    if not path.is_file():
        return True
    if size is not None and path.stat().st_size != size:
        return True
    return False


def ensure_samples(verbose: bool = True) -> list[str]:
    """把 jl_srv 下的样本备齐，返回这次新建了哪些。"""
    SRC.mkdir(parents=True, exist_ok=True)
    made: list[str] = []

    plan: list[tuple[str, int, int]] = [
        ("big.bin", BIG, 20260923),
        ("mid.bin", MID, 20260924),
        ("small.bin", 1100, 20260925),
    ]
    for name, size, seed in plan:
        p = SRC / name
        if _need(p, size):
            p.write_bytes(_seed_blob(size, seed))
            made.append(name)

    # test.mp4 必须是能被 yt-dlp 认出的真视频 —— 拿 ffmpeg 现造一个。
    # 这是 test_video.py 的前提（走 generic 提取器）。
    mp4 = SRC / "test.mp4"
    if _need(mp4):
        if _make_mp4(mp4):
            made.append("test.mp4")

    if verbose:
        if made:
            print(f"  新建样本: {', '.join(made)}")
        else:
            print(f"  样本已齐（{SRC}）")
    return made


def _make_mp4(dst: pathlib.Path) -> bool:
    """用 ffmpeg 造一个 2 秒的小视频。ffmpeg 不在就返回 False。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        sys.path.insert(0, str(ROOT))
        try:
            from jiliu import tools
            exe = tools.find("ffmpeg")
        except Exception:
            exe = None
    if not exe:
        print("  !! ffmpeg 不可用，造不出 test.mp4 —— test_video 会失败")
        return False
    cmd = [exe, "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=2",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not dst.is_file():
            err = (r.stderr or b"").decode("utf-8", "replace").strip()[-200:]
            print(f"  !! ffmpeg 造 mp4 失败: {err}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  !! ffmpeg 造 mp4 异常: {exc}")
        return False


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def _wait_port(port: int, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if port_open(port):
            return True
        time.sleep(0.15)
    return False


class Servers:
    """三个测试服务器的生命周期管理器。

    退出时一定收干净：这些服务器是常驻进程，漏一个就会占着端口，
    下次跑测试直接 10061，而且很难联想到是上一次的残留。
    """

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen] = []
        self.logs: dict[int, pathlib.Path] = {}

    def start(self, reuse: bool = True) -> "Servers":
        specs = [
            (PORT_PLAIN, []),
            (PORT_NORANGE, ["--no-range"]),
            (PORT_SLOW, ["--rate", str(SLOW_RATE)]),
        ]
        for port, extra in specs:
            if reuse and port_open(port):
                print(f"  {port} 已有服务在跑，沿用")
                continue
            log = pathlib.Path(tempfile.gettempdir()) / f"jl_srv_{port}.log"
            self.logs[port] = log
            lf = open(log, "wb")
            proc = subprocess.Popen(
                [sys.executable, "-u", str(HERE / "test_server.py"), str(SRC),
                 str(port)] + extra,
                stdout=lf, stderr=subprocess.STDOUT, cwd=str(ROOT),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.procs.append(proc)
            if not _wait_port(port, 20):
                tail = ""
                try:
                    tail = log.read_text(encoding="utf-8", errors="replace")[-400:]
                except Exception:
                    pass
                raise RuntimeError(f"测试服务器 {port} 起不来。rc={proc.poll()}\n{tail}")
            print(f"  {port} 已就绪{'（限速 %s）' % SLOW_RATE if port == PORT_SLOW else ''}")
        return self

    def stop(self) -> None:
        for p in self.procs:
            try:
                p.terminate()
                p.wait(timeout=6)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self.procs.clear()

    def __enter__(self) -> "Servers":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


if __name__ == "__main__":
    print(f"样本目录: {SRC}")
    ensure_samples()
    with Servers():
        print("  三个服务器都在跑。Ctrl+C 停。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
