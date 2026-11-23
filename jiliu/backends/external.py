"""外部命令行下载引擎：curl / wget。

为什么要这两个「比自研弱的」引擎也接进来 —— 它们的价值不在速度，
在**兼容性**：

  - 有的服务端对自研引擎的请求头组合挑三拣四，curl 却能下。
    curl 是 HTTP 实现里最不吃亏的一个（HTTP/2、TLS 细节、畸形响应都能吞）。
  - wget 对「服务端中途断流」的容忍度极高，会自己重连续传。
  - 内网/老系统上，用户可能只有这两个之一，而装不上 aria2。

所以定位是**兜底**：多线程优先，遇到自研引擎搞不定的再切过来。

实现上的两个关键决定：

  1）**进度靠轮询文件大小，不解析各家进度条输出。**
     curl 和 wget 的进度输出格式不一样，还随版本变；而"目标文件现在多大"
     是唯一稳定的事实。速度用增量自己算。少解析一份会变的东西。

  2）**先下到 `.part`，完成再改名。**
     两个程序都是直接往目标路径写。下到一半取消，留下一个残缺文件
     长得跟下完了一样 —— 用户会以为成功了。用 `.part` 就不会误认，
     续传也是对这个文件做。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .. import proxy as _proxy
from .. import tools as _tools
from ..engine import human_size, human_speed, probe as _probe

# 轮询文件大小的间隔。0.4 秒足够让界面看起来是连续的，
# 又不至于把 CPU 烧在一个 stat 调用上。
_POLL = 0.4
# 采样窗口：算速度用最近 5 个样本，避免被一次卡顿带偏
_SAMPLES = 5


def _rate_text(bps: int) -> str:
    """把字节/秒转成 curl/wget 认的限速写法。"""
    if bps <= 0:
        return ""
    for unit, scale in (("G", 1024 ** 3), ("M", 1024 ** 2), ("K", 1024)):
        if bps >= scale:
            return f"{max(1, round(bps / scale))}{unit}"
    return str(bps)


class ExternalDownloader:
    """curl / wget 的共用实现。子类只需给出命令行。"""

    engine = "external"
    label = "外部引擎"

    def __init__(
        self,
        url: str,
        dest: pathlib.Path,
        *,
        cfg: Optional[Dict[str, Any]] = None,
        threads: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
        speed_limit: Optional[int] = None,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        verify_hash: bool = False,
        expected_sha256: str = "",
        **_ignored: Any,
    ):
        self.url = url
        self.dest = pathlib.Path(dest)
        self.cfg = cfg or {}
        self.headers = dict(headers or {})
        self.speed_limit = int(
            speed_limit if speed_limit is not None
            else (self.cfg.get("speed_limit") or 0)
        )
        self.progress_cb = progress_cb
        self.log_cb = log_cb
        self.verify_hash = bool(verify_hash)
        self.expected_sha256 = expected_sha256

        # 真实产出路径与临时路径
        self.part = self.dest.with_name(self.dest.name + ".part")
        self.filename = self.dest.name
        self.threads = 1

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._resume_evt = threading.Event()
        self._samples: List[tuple] = []
        self._started_at = 0.0
        self._finished_at = 0.0
        self._paused = False
        self._cancelled = False
        self._done = False
        self._error = ""
        self._size = 0
        self._sha256 = ""
        self._hash_ok: Optional[bool] = None
        self._tail: List[str] = []
        self._write_poll: Optional[threading.Thread] = None

    # ---------------- 命令行 ----------------

    def binary(self) -> str:
        raise NotImplementedError

    def build_cmd(self, *, resume: bool) -> List[str]:
        raise NotImplementedError

    def _proxy_arg(self) -> str:
        return _proxy.select(self.cfg, self.url)

    def _common_env(self) -> Dict[str, str]:
        """给子进程的环境。关键是清掉代理变量。

        不清的话，curl/wget 会自己读 HTTP_PROXY —— 而那份设置可能是个
        死代理（本机就长期躺着一个），于是「明明配了系统代理却连不上」。
        我们用 -x 显式传，环境里的必须清掉，避免两套设置打架。
        """
        env = os.environ.copy()
        for k in _proxy.ENV_KEYS:
            env.pop(k, None)
        return env

    # ---------------- 生命周期 ----------------

    def run(self) -> Dict[str, Any]:
        self._stop.clear()
        self._cancelled = False
        self._paused = False
        self._done = False
        self._error = ""
        self._sha256 = ""
        self._hash_ok = None
        self._started_at = time.monotonic()
        self._finished_at = 0.0
        self.dest.parent.mkdir(parents=True, exist_ok=True)

        # 总量先探一次，界面才有百分比可言。探不到就退化成「只显示已下载」
        try:
            info = _probe(self.url, self.cfg)
            self._size = int(getattr(info, "size", 0) or 0)
        except Exception:  # noqa: BLE001
            self._size = 0

        self._start_poller()
        try:
            while True:
                # 进程起来之前就被暂停了
                if self._paused:
                    self._wait_resume()
                    if self._cancelled:
                        break
                    continue

                self._launch(resume=self.part.exists())
                proc = self._proc
                try:
                    code = proc.wait() if proc else -1
                except Exception:  # noqa: BLE001
                    code = -1

                if self._cancelled:
                    break
                if self._paused:
                    # 暂停的本质是「把进程停掉，留着 .part 等下次续」。
                    # 这里**不能 return** —— 一返回，管理器就会把这次 run
                    # 当成本轮结束，暂停会被记成失败。所以阻塞等 resume。
                    self._wait_resume()
                    if self._cancelled:
                        break
                    continue

                if code == 0 and self.part.exists():
                    self._finalize()
                elif not self._error:
                    tail = f"：{self._tail[-1]}" if self._tail else ""
                    self._error = f"{self.label} 退出码 {code}{tail}"
                break
        finally:
            self._finished_at = time.monotonic()
            self._stop.set()
            self._join_poller()
            self._emit()

        return {
            "ok": bool(self._done and not self._error),
            "path": str(self.dest),
            "bytes": self.done_bytes(),
            "seconds": max(0.0, self._finished_at - self._started_at),
            "cancelled": self._cancelled,
            "error": self._error or None,
            "sha256": self._sha256,
            "hash_ok": self._hash_ok,
        }

    def _wait_resume(self) -> None:
        """阻塞到 resume() 被调用。cancel() 也会放开它。"""
        while not self._stop.is_set() and not self._cancelled:
            if self._resume_evt.wait(timeout=0.3):
                self._resume_evt.clear()
                return

    def _launch(self, *, resume: bool) -> None:
        exe = self.binary()
        cmd = self.build_cmd(resume=resume)
        self._append_log(f"$ {' '.join(cmd)}")
        kwargs: Dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "env": self._common_env(),
        }
        if os.name == "nt":
            kwargs["creationflags"] = _tools._no_window()
        self._proc = subprocess.Popen(cmd, **kwargs)
        # 起线程读 stderr：既能让用户看到原因，也避免管道写满把进程卡死
        th = threading.Thread(target=self._drain, args=(self._proc,),
                              daemon=True, name=f"jiliu-{self.engine}-log")
        th.start()

    def _drain(self, proc: subprocess.Popen) -> None:
        try:
            stream = proc.stderr
            if stream is None:
                return
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                # curl 的进度条会把同一行刷很多遍，留着只会刷屏
                if line.startswith(("\r",)) or "%" in line and " " in line[:4]:
                    continue
                self._append_log(line)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass

    def _append_log(self, line: str) -> None:
        with self._lock:
            self._tail.append(line)
            if len(self._tail) > 40:
                del self._tail[:-40]
        if self.log_cb:
            try:
                self.log_cb(line)
            except Exception:  # noqa: BLE001
                pass

    def _start_poller(self) -> None:
        self._write_poll = threading.Thread(
            target=self._poll_loop, daemon=True, name=f"jiliu-{self.engine}-poll")
        self._write_poll.start()

    def _join_poller(self) -> None:
        if self._write_poll is not None:
            self._write_poll.join(timeout=2)
            self._write_poll = None

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            cur = self.done_bytes()
            with self._lock:
                self._samples.append((time.monotonic(), cur))
                if len(self._samples) > _SAMPLES:
                    del self._samples[:-_SAMPLES]
            self._emit()
            self._stop.wait(_POLL)

    def _finalize(self) -> None:
        """下完把 .part 改成正式名字，并对哈希（如果要求）。"""
        try:
            if self.dest.exists():
                self.dest.unlink()
            self.part.replace(self.dest)
        except Exception as exc:  # noqa: BLE001
            self._error = f"改名失败：{exc}"
            return
        self._done = True
        if self.verify_hash and self.expected_sha256:
            got = _sha256(self.dest)
            self._sha256 = got
            if got.lower() != self.expected_sha256.strip().lower():
                self._hash_ok = False
                self._error = (f"哈希不匹配：期望 {self.expected_sha256[:12]}… "
                               f"实得 {got[:12]}…")
            else:
                self._hash_ok = True
                self._append_log("哈希校验通过")

    def pause(self) -> bool:
        """暂停 = 把子进程停掉，已下载部分留在 .part。

        和自研引擎不一样（那个是让线程等在事件上）。子进程没法「暂停」，
        只能杀掉再续传 —— 但对外表现是一致的：run 不返回、状态变 paused。
        """
        with self._lock:
            self._paused = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                return False
        self._append_log(f"已暂停（已下载部分保留在 {self.part.name}）")
        return True

    def resume(self) -> bool:
        with self._lock:
            if not self._paused:
                return False
            self._paused = False
        self._resume_evt.set()
        self._append_log("继续下载")
        return True

    def cancel(self) -> bool:
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._resume_evt.set()          # 唤醒可能正等着的 run
        self._stop.set()
        # 只删 .part。dest 可能是用户原有的同名文件（overwrite 策略下
        # 重新下载途中取消），删掉等于毁用户数据。
        try:
            if self.part.exists() and not self._done:
                self.part.unlink()
        except Exception:  # noqa: BLE001
            pass
        return True

    # ---------------- 状态 ----------------

    def done_bytes(self) -> int:
        """已下载字节数 = .part 的大小（下完则取正式文件）。"""
        for f in (self.part, self.dest):
            try:
                if f.exists():
                    return f.stat().st_size
            except Exception:  # noqa: BLE001
                continue
        return 0

    def _speed(self) -> float:
        with self._lock:
            s = list(self._samples)
        for i in range(len(s) - 1, 0, -1):
            (t1, b1), (t0, b0) = s[i], s[i - 1]
            if t1 > t0 and b1 >= b0:
                return (b1 - b0) / (t1 - t0)
        return 0.0

    def snapshot(self) -> Dict[str, Any]:
        done = self.done_bytes()
        total = self._size
        speed = self._speed()
        elapsed = ((self._finished_at or time.monotonic()) - self._started_at
                   if self._started_at else 0.0)
        eta = ((total - done) / speed) if (total and speed > 0 and done < total) else None
        live = self._proc is not None and self._proc.poll() is None
        return {
            "url": self.url,
            "dest": str(self.dest),
            "filename": self.filename,
            "size": total,
            "size_text": human_size(total),
            "done": done,
            "done_text": human_size(done),
            "percent": round(done * 100.0 / total, 2) if total else 0.0,
            "speed": speed,
            "speed_text": human_speed(speed),
            "eta": eta,
            "eta_text": _fmt_eta(eta),
            "elapsed": elapsed,
            "threads": 1,
            "segments_pending": 0,
            "segments_inflight": 1 if live else 0,
            "segments_total": 1 if live else 0,
            "segments_created": 1,
            "accept_ranges": True,
            "paused": self._paused,
            "cancelled": self._cancelled,
            "plan": {},
            "category": "",
            "sha256": "",
            "hash_ok": None,
            "error": self._error,
            "engine": self.engine,
        }

    def progress_factory(self):  # 与 manager 的调用约定保持一致
        return self.snapshot

    def _emit(self) -> None:
        if self.progress_cb:
            try:
                self.progress_cb(self.snapshot())
            except Exception:  # noqa: BLE001
                pass


class CurlDownloader(ExternalDownloader):
    engine = "curl"
    label = "curl"

    def binary(self) -> str:
        return _tools.find("curl", self.cfg.get("curl_path", "")) or "curl"

    def build_cmd(self, *, resume: bool) -> List[str]:
        cmd = [self.binary(), "-L",           # 跟重定向
               "--fail-with-body",            # 4xx/5xx 要报错而不是存页面
               "--retry", "5", "--retry-delay", "2",
               "--connect-timeout", str(int(self.cfg.get("timeout", 30))),
               "-o", str(self.part)]
        if resume:
            cmd += ["-C", "-"]                # 断点续传
        rate = _rate_text(self.speed_limit)
        if rate:
            cmd += ["--limit-rate", rate]
        p = self._proxy_arg()
        if p:
            cmd += ["-x", p]
        for k, v in self.headers.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(self.url)
        return cmd


class WgetDownloader(ExternalDownloader):
    engine = "wget"
    label = "wget"

    def binary(self) -> str:
        return _tools.find("wget", self.cfg.get("wget_path", "")) or "wget"

    def build_cmd(self, *, resume: bool) -> List[str]:
        # --progress=dot:giga 输出少、适合我们只想要原因不要刷屏的用法
        cmd = [self.binary(), "--tries=5", "--timeout",
               str(int(self.cfg.get("timeout", 30))),
               "--progress=dot:giga",
               "-O", str(self.part)]
        if resume:
            cmd.append("-c")
        rate = _rate_text(self.speed_limit)
        if rate:
            cmd.append(f"--limit-rate={rate}")
        p = self._proxy_arg()
        env_args = ["-e", "use_proxy=yes"]
        if p:
            env_args += ["-e", f"http_proxy={p}", "-e", f"https_proxy={p}"]
        else:
            env_args += ["-e", "use_proxy=no"]
        for k, v in self.headers.items():
            cmd += [f"--header={k}: {v}"]
        cmd += env_args
        cmd.append(self.url)
        return cmd


def _sha256(path: pathlib.Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fmt_eta(sec: Optional[float]) -> str:
    """自己实现一份，不去 import engine 的私有函数 —— 私有函数随时会变。"""
    if sec is None or sec != sec or sec <= 0:
        return "-"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
