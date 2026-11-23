"""慢探测的后台快照。

面板的 `/api/state` 是**每秒轮询**的接口，但里面塞着几件「必须问子进程
才知道」的事：七八个外部工具的版本号、yt-dlp 有哪几份能用、aria2 的
RPC 通不通。冷启动第一轮要 2.5–9 秒 —— scoop 装的 yt-dlp shim 单次就
1.7 秒，六个工具串着问就是五秒起步。

窗口是秒开的，页面却在这几秒里一片空白。用户看到的是「软件卡住了」，
而不是「正在探测」。

这里把「探测」和「取结果」拆开：

    取结果的路径永远快。有快照就给快照；没有就返回**占位**
    （带 probing 标记），同时后台开一趟探测。
    前端看见 probing 就写「正在探测」，下一秒轮询自然补上真值。

这样首屏的等待是**零**，准确性也不丢 —— 探测完成后的结果一样准确，
只是它晚半秒到达，而不是把页面按在原地等。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional


class Snapshot:
    """一个「读得快、算得慢」的值。

    线程安全，并且自带单飞：同一时刻只有一趟探测在跑。
    并发进来的读取者要么拿上一次的值，要么等这一趟跑完（有上限）。
    """

    def __init__(self, fn: Callable[[], Any], *, ttl: float = 60.0,
                 name: str = "probe") -> None:
        self._fn = fn
        self._ttl = float(ttl)
        self._name = name
        self._cv = threading.Condition()
        self._val: Optional[Any] = None
        self._t = 0.0
        self._busy = False
        self._error = ""

    # ---- 读 ----

    def age(self) -> float:
        """快照有多旧（秒）。还没算过返回 -1。"""
        with self._cv:
            return -1.0 if self._val is None else time.time() - self._t

    def fresh(self) -> bool:
        with self._cv:
            return (self._val is not None
                    and (time.time() - self._t) < self._ttl)

    def peek(self) -> Any:
        """看当前值，不触发探测。没有就 none。"""
        with self._cv:
            return self._val

    def error(self) -> str:
        with self._cv:
            return self._error

    def get(self, *, wait: float = 0.0, default: Any = None) -> Any:
        """取当前值。

        wait 是「最多为这一趟新值等多久」：

            0     一秒不等。有旧值给旧值，没旧值给 default。
            0.3   首屏用这个量级：热身已完成时几乎立刻返回，
                  没完成也不会把页面按住。
        """
        started = False
        with self._cv:
            if self._val is not None and (time.time() - self._t) < self._ttl:
                return self._val
            if not self._busy:
                self._busy = True
                started = True

        if started:
            self._spawn()

        if wait > 0:
            with self._cv:
                end = time.time() + wait
                # 只在这一趟还没出过东西的时候等；有旧值就不必等
                while self._busy and self._val is None:
                    left = end - time.time()
                    if left <= 0:
                        break
                    self._cv.wait(left)
                if self._val is not None:
                    return self._val

        with self._cv:
            return self._val if self._val is not None else default

    # ---- 控制 ----

    def refresh_async(self) -> None:
        """立刻开一趟新探测（已经有一趟在跑就直接返回，不排队）。"""
        with self._cv:
            if self._busy:
                return
            self._busy = True
        self._spawn()

    def invalidate(self) -> None:
        """作废旧值。下一次 get 会重新探测。"""
        with self._cv:
            self._val = None
            self._t = 0.0

    def set(self, val: Any) -> None:
        """直接写入一份刚算好的结果（用户点「重新探测」时用）。

        重算的人已经把结果握在手里了，没必要再让后台白跑一趟。
        """
        with self._cv:
            self._val = val
            self._t = time.time()
            self._error = ""
            self._cv.notify_all()

    def warm(self, timeout: float = 30.0) -> bool:
        """同步预热：等这一趟跑完。启动时用，测试里也用得上。"""
        self.refresh_async()
        with self._cv:
            end = time.time() + timeout
            while self._busy:
                left = end - time.time()
                if left <= 0:
                    return False
                self._cv.wait(left)
            return self._val is not None

    # ---- 内部 ----

    def _spawn(self) -> None:
        threading.Thread(target=self._run, name=self._name,
                         daemon=True).start()

    def _run(self) -> None:
        val: Any = None
        err = ""
        try:
            val = self._fn()
        except Exception as e:                       # 探测失败不该拖垮面板
            err = f"{type(e).__name__}: {e}"
        with self._cv:
            if val is not None:
                self._val = val
                self._t = time.time()
            self._error = err
            self._busy = False
            self._cv.notify_all()
