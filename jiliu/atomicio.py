"""原子写盘 + 损坏恢复。

状态文件（config / tasks / history / subscriptions）有一个共同要求：
**任何时刻被杀，磁盘上都必须是一份完整可解析的旧内容或新内容**，
不能出现写了一半的 JSON。项目里四份文件以前各写各的，水平参差：

    tasks.json           tmp + replace，但 tmp 名固定、写盘在锁外 → 并发写会交错
    config.json          直接 write_text → 断电即半截
    history.json         直接 write_text
    subscriptions.json   tmp + replace，但异常被吞

这里统一成一套：

  · 临时文件名带 pid/线程/随机串 —— 两个线程同时写不会踩同一个 tmp；
  · 同一路径一把进程内锁 —— ThreadingHTTPServer 并发保存时串行落盘；
  · fsync 后再 os.replace —— 断电也只会看到旧版或新版；
  · 保留一份 .bak（上一次成功内容），读取失败时自动回退并修复；
  · 写失败抛异常，由调用方决定记日志还是向上报 —— 不再静默吞掉。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time
import uuid
from typing import Any, Dict, Optional

_LOCKS: Dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()

# 错误留痕有上限，避免一个坏循环把磁盘写满
_ERR_LOG = "errors.log"
_ERR_LOG_MAX = 256 * 1024


def _lock_for(path: pathlib.Path) -> threading.RLock:
    key = str(path.resolve()).lower()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def warn(msg: str) -> None:
    """写一条可见的告警：stderr + <配置目录>/errors.log（有上限轮换）。"""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        if sys.stderr is not None:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import config as _config  # 延迟导入，避免循环依赖
        home = pathlib.Path(_config.HOME)
        home.mkdir(parents=True, exist_ok=True)
        p = home / _ERR_LOG
        if p.is_file() and p.stat().st_size > _ERR_LOG_MAX:
            p.replace(p.with_suffix(".log.1"))
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def write_json(path, data: Any, *, indent: int = 1, backup: bool = True) -> None:
    """原子写 JSON。失败抛 OSError/TypeError，绝不静默。"""
    path = pathlib.Path(path)
    with _lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=indent)
                fh.flush()
                os.fsync(fh.fileno())
            if backup and path.is_file():
                # .bak 保存「上一份成功内容」；它自己的替换也必须是原子的
                try:
                    bak = path.with_suffix(path.suffix + ".bak")
                    btmp = bak.with_name(
                        f".{bak.name}.{os.getpid()}.{threading.get_ident()}.tmp"
                    )
                    with open(path, "rb") as src, open(btmp, "wb") as dst:
                        dst.write(src.read())
                        dst.flush()
                        os.fsync(dst.fileno())
                    os.replace(btmp, bak)
                except OSError:
                    pass  # 备份失败不该挡住主写入
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


def read_json(path, default: Any = None, *, recover: bool = True) -> Any:
    """读 JSON；主文件损坏时自动回退 .bak 并修复主文件。

    返回 default 表示「没有可读内容」。损坏事件会写进 errors.log，
    不再像以前那样静默变成空表。
    """
    path = pathlib.Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        if not recover:
            return default
        bak = path.with_suffix(path.suffix + ".bak")
        if bak.is_file():
            try:
                data = json.loads(bak.read_text(encoding="utf-8"))
                warn(f"{path.name} 损坏（{type(exc).__name__}），已从 .bak 恢复")
                try:
                    write_json(path, data, backup=False)
                except OSError:
                    pass
                return data
            except Exception:  # noqa: BLE001
                pass
        # 主文件与备份都不可用：留档，别让下一次写操作用空表覆盖证据
        try:
            dead = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            path.replace(dead)
            warn(f"{path.name} 无法解析且无可用备份，已留档为 {dead.name}")
        except OSError:
            pass
        return default
