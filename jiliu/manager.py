"""任务管理器：调度、并发控制、状态持久化。

    tm = TaskManager()
    t  = tm.create_http("https://example.com/big.iso")
    tm.pause(t.id); tm.resume(t.id); tm.cancel(t.id)
    tm.list()
"""
from __future__ import annotations

import json
import pathlib
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from . import (backends, config as _config, edition, engine, layout, naming,
               notify, protocols, resource, schedule, video)
from .engine import human_size, unique_path
from .task import RESUMABLE, Task

MAX_CONCURRENT_DEFAULT = 3
# 同站视频任务的最小起跑间隔（秒）。合集一次勾 20 集 = 同一 IP 短时间内
# 连续 20 轮「解析 API + CDN」，这是各平台风控最爱抓的形态。
VIDEO_GAP_BASE = 3.0
VIDEO_GAP_JITTER = 2.0


def _engine_name(dl) -> str:
    """取下载器用的引擎名。

    优先用它自己声明的 `engine`（native / aria2 / curl / wget），
    没有再退回模块名 —— 早期版本只看模块名，任务里会显示成
    "engine" 这种没意义的东西。
    """
    name = getattr(dl, "engine", "")
    if name:
        return str(name)
    mod = dl.__class__.__module__.split(".")[-1]
    return {"engine": "native", "aria2": "aria2", "external": "curl"}.get(mod, mod)


class TaskManager:
    def __init__(self, cfg: Optional[Dict[str, Any]] = None, autoload: bool = True):
        self.cfg = cfg or _config.load()
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._tasks: Dict[str, Task] = {}
        self._order: List[str] = []
        self._live: Dict[str, Any] = {}          # 正在跑的下载器 / 取消事件
        self._threads: Dict[str, threading.Thread] = {}
        self._stopping = False
        self._listeners: List[Callable[[str, Task], None]] = []
        # 视频防风控：host -> 该站下一次允许的起跑时刻（monotonic）
        self._video_gate_lock = threading.Lock()
        self._video_next_start: Dict[str, float] = {}

        if autoload:
            self._load()

        self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True, name="jiliu-dispatch")
        self._dispatcher.start()

    # ------------------------------------------------------------------
    # 监听（给 WebSocket / SSE 用；现在 server 走轮询，保留接口）
    # ------------------------------------------------------------------

    def add_listener(self, fn: Callable[[str, Task], None]) -> None:
        self._listeners.append(fn)

    def _notify(self, event: str, task: Task) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, task)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        p = _config.TASKS_PATH
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        for d in data.get("tasks", []):
            try:
                t = Task.from_dict(d)
            except Exception:
                continue
            if t.status in ("running", "queued"):
                # 进程重启：正在跑的都断了
                t.status = "interrupted"
                t.speed = 0
                t.eta = None
                t.add_log("程序重启，任务中断（可恢复）")
            self._tasks[t.id] = t
            self._order.append(t.id)

    def save(self) -> None:
        with self._lock:
            payload = {
                "updated": time.time(),
                "tasks": [
                    {**self._tasks[i].to_dict(log_tail=40)}
                    for i in self._order if i in self._tasks
                ],
            }
        try:
            _config.HOME.mkdir(parents=True, exist_ok=True)
            tmp = pathlib.Path(str(_config.TASKS_PATH) + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(_config.TASKS_PATH)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list(self, log_tail: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            return [self._tasks[i].to_dict(log_tail=log_tail)
                    for i in self._order if i in self._tasks]

    def get(self, tid: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(tid)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            out: Dict[str, int] = {}
            total_speed = 0.0
            for i in self._order:
                t = self._tasks.get(i)
                if not t:
                    continue
                out[t.status] = out.get(t.status, 0) + 1
                if t.status == "running":
                    total_speed += t.speed
            return {
                "counts": out,
                "total": len(self._order),
                "active": out.get("running", 0),
                "total_speed": total_speed,
                "total_speed_text": (human_size(total_speed) + "/s") if total_speed else "-",
                "max_concurrent": int(self.cfg.get("max_concurrent", MAX_CONCURRENT_DEFAULT)),
            }

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def _new(self, task: Task) -> Task:
        with self._lock:
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._cv.notify_all()
        self._notify("created", task)
        self.save()
        return task

    def create_http(
        self,
        url: str,
        *,
        threads: Optional[int] = None,
        filename: str = "",
        dest_dir: str = "",
        backend: str = "",
        meta: Optional[Dict[str, Any]] = None,
        category: str = "",
        chunk_size: int = 0,
        verify_hash: bool = False,
        expected_sha256: str = "",
        auto_classify: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> Task:
        """建一个下载任务。

        auto_classify=True 时会先探测一次，认出这是什么再定策略 ——
        图片少开线程、镜像用大分片、安装包下完对哈希。
        想自己说了算就把 threads / chunk_size 显式传进来。

        headers 是本任务专属的请求头（网盘解析出来的直链有时要带
        Cookie/Referer 才给下）。存进 meta 随任务持久化，重启续传也在。
        """
        # 版本门禁放这里，而不是只放在 HTTP 接口上 ——
        # CLI、插件、管理器内部调用都绕不过去。国内版就是不下海外站。
        g = edition.gate(url, self.cfg)
        if not g["allowed"]:
            raise TaskError(g["reason"])

        d = pathlib.Path(dest_dir).expanduser() if dest_dir else _config.download_dir(self.cfg)
        m = dict(meta or {})
        if headers:
            m["headers"] = {k: v for k, v in headers.items() if v}
        # 用户既然给了预期哈希，那就默认要验 —— 否则给这个值没有意义
        if expected_sha256:
            verify_hash = True

        # 离线识别永远先做一遍（不联网、不花钱）：
        # 就算用户关了自动分类，任务上也该有个像样的类型标签。
        plan: Dict[str, Any] = resource.classify(url=url, filename=filename).to_dict()
        if auto_classify and not (threads and chunk_size):
            live = self._classify_quiet(url)
            if live:
                plan = live
                if plan.get("suggest") == "video":
                    m["suggest"] = "video"

        m["resource"] = plan
        if threads is None:
            threads = int(plan.get("concurrency") or 0) or None
        if not chunk_size:
            chunk_size = int(plan.get("chunk_size") or 0)
        if not verify_hash:
            verify_hash = bool(plan.get("verify_hash"))

        # —— 分类归档 ——
        # 类型认出来之后才知道该放进哪个子目录，所以这一步必须在 plan 之后。
        # 用户显式指定了目录就不套（手动选 = 明确意图），除非开了 subdir_explicit。
        # P2P（category=bt）同样归档，落在 <下载目录>/BT/。
        d = layout.ensure(d, plan.get("category") or "", self.cfg,
                          explicit=bool(dest_dir))

        # —— P2P（磁力 / 种子 / ed2k / 迅雷壳里的磁力）——
        # 这类只能走 aria2：自研引擎不做 P2P。而且必须**强制**指定后端，
        # 不能让 auto 去挑 —— 挑到 native 就是直接失败。
        if plan.get("p2p"):
            backend = "aria2"
            m["p2p"] = True
            if plan.get("info_hash"):
                m["info_hash"] = str(plan["info_hash"])
            if plan.get("trackers"):
                m["trackers"] = list(plan["trackers"])
            threads = None
            chunk_size = 0
            verify_hash = False
            if not filename:
                # 磁力的 dn 就是资源名；种子的名字要等元数据，先给个占位
                guess = str(plan.get("filename") or "").strip()
                filename = naming.sanitize(guess) if guess else "BT 下载"

        t = Task(
            url=url,
            kind="http",
            threads=int(threads or self.cfg.get("threads", 8)),
            dest_dir=str(d),
            filename=filename or "",
            backend=backend,
            category=category or (plan.get("category") or ""),
            chunk_size=int(chunk_size or 0),
            verify_hash=bool(verify_hash),
            expected_sha256=expected_sha256,
            meta=m,
        )
        if filename:
            t.dest = str(d / filename)
        return self._new(t)

    def _classify_quiet(self, url: str) -> Dict[str, Any]:
        """联网探测 + 识别，失败就返回空字典（不能因为识别不了就不让下载）。"""
        try:
            pr = engine.probe(url, self.cfg)
            plan = dict(pr.plan or {})
            if plan:
                plan["probe"] = {
                    "size": pr.size,
                    "size_text": human_size(pr.size),
                    "accept_ranges": pr.accept_ranges,
                    "content_type": pr.content_type,
                    "filename": pr.filename,
                }
            return plan
        except Exception:
            return {}

    def classify(self, url: str) -> Dict[str, Any]:
        """对外暴露的识别接口：只探测不下载，给面板预览用。"""
        pr = engine.probe(url, self.cfg)
        out: Dict[str, Any] = {
            "url": url,
            "final_url": pr.final_url,
            "status": pr.status,
            "error": pr.error,
            "size": pr.size,
            "size_text": human_size(pr.size),
            "accept_ranges": pr.accept_ranges,
            "content_type": pr.content_type,
            "content_disposition": pr.content_disposition,
            "filename": pr.filename,
            "magic_hex": pr.magic[:16].hex(),
            "plan": pr.plan,
        }
        return out

    def create_video(
        self,
        url: str,
        *,
        fmt: str = "",
        dest_dir: str = "",
        title: str = "",
        meta: Optional[Dict[str, Any]] = None,
        playlist: bool = False,
        items: str = "",
        group_title: str = "",
        audio_only: bool = False,
        subs_only: bool = False,
    ) -> Task:
        """建一个视频/合集任务。

        playlist=True 时按合集处理：产物进子目录、文件名带序号；
        items 形如 "1-3,7"（yt-dlp 的 --playlist-items），只下其中几集。
        audio_only=True 时抽音轨（需 ffmpeg）。
        subs_only=True 时纯字幕模式：只下字幕（转 SRT），不下视频。
        """
        g = edition.gate(url, self.cfg)
        if not g["allowed"]:
            raise TaskError(g["reason"])

        base = pathlib.Path(dest_dir).expanduser() if dest_dir else _config.download_dir(self.cfg)
        m = dict(meta or {})
        m.update({
            "playlist": bool(playlist),
            "playlist_items": (items or "").strip(),
            "group_title": (group_title or "").strip(),
            "audio_only": bool(audio_only or self.cfg.get("audio_only")),
            "subs_only": bool(subs_only),
            "resource": {
                "category": "playlist" if playlist else "video",
                "label": "合集 / 播放列表" if playlist else (
                    "字幕" if subs_only else ("音频" if audio_only else "视频")),
                "engine": "ytdlp",
                "filetype": "yt-dlp 解析",
                "concurrency": 1,
                "chunk_size": 0,
                "verify_hash": False,
                "protocol": "http",
                "note": ("按合集展开：产物进子目录、文件名带序号"
                         if playlist else ""),
            },
        })
        # 目录落位：老的 video_dir 设置优先（用户明确填了就听他的），
        # 否则交给分类归档（视频 → <下载目录>/视频/）
        sub = (self.cfg.get("video_dir") or "").strip()
        if sub:
            d = base / sub
        else:
            d = layout.ensure(base, "video", self.cfg, explicit=bool(dest_dir))
        t = Task(
            url=url,
            kind="video",
            backend="yt-dlp",
            threads=1,
            dest_dir=str(d),
            format=fmt or self.cfg.get("video_format", "bv*+ba/b"),
            title=title or group_title or "",
            category="playlist" if playlist else "video",
            meta=m,
        )
        return self._new(t)

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------

    def pause(self, tid: str) -> bool:
        t = self.get(tid)
        if not t or t.status != "running":
            return False
        live = self._live.get(tid)
        if live is not None and hasattr(live, "pause"):
            try:
                live.pause()
            except Exception:
                pass
        t.status = "paused"
        t.add_log("已暂停")
        self._notify("paused", t)
        self.save()
        return True

    def resume(self, tid: str) -> bool:
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t.status not in RESUMABLE:
                return False
            t.status = "queued"
            t.error = ""
            t.add_log("已加入队列（续传）")
            self._cv.notify_all()
        self._notify("queued", t)
        self.save()
        return True

    def cancel(self, tid: str) -> bool:
        t = self.get(tid)
        if not t or t.status in ("done",):
            return False
        live = self._live.get(tid)
        if live is not None:
            try:
                if hasattr(live, "cancel"):
                    live.cancel()
                elif hasattr(live, "set"):
                    live.set()
            except Exception:
                pass
        t.status = "cancelled"
        t.speed = 0
        t.eta = None
        t.add_log("已取消")
        self._notify("cancelled", t)
        self.save()
        return True

    def remove(self, tid: str) -> bool:
        self.cancel(tid)
        with self._lock:
            if tid not in self._tasks:
                return False
            self._tasks.pop(tid, None)
            if tid in self._order:
                self._order.remove(tid)
            self._live.pop(tid, None)
        self.save()
        return True

    def clear_finished(self) -> int:
        with self._lock:
            victims = [i for i in self._order
                       if self._tasks.get(i) and self._tasks[i].status in ("done", "cancelled")]
            for i in victims:
                self._tasks.pop(i, None)
                self._order.remove(i)
        self.save()
        return len(victims)

    def set_priority(self, tid: str, value: Any = None, top: bool = False) -> Optional[Task]:
        """调整排队任务的优先级。

        - `value`：直接设优先级（int，越大越先跑）。
        - `top=True`：置顶 —— 抬到当前队列最高（max+1），下次调度第一个跑。
        只对 queued 状态有意义；running/done 等改了也不影响顺序，照常接受但立即生效无感。
        """
        with self._lock:
            t = self._tasks.get(tid)
            if t is None:
                return None
            if top:
                q = [x.priority for x in self._tasks.values() if x.status == "queued"]
                t.priority = (max(q) + 1) if q else 1
            else:
                try:
                    t.priority = max(0, int(value or 0))
                except Exception:
                    return None
            t.add_log(f"优先级调整为 {t.priority}"
                      + ("（置顶，下个空位即开跑）" if t.status == "queued" else ""))
            self._cv.notify_all()
            return t

    def restart(self, tid: str) -> Optional[Task]:
        """重新下载：清掉产物与续传状态，从零重新入队。

        与 resume（续传）相对 —— resume 接着断点下，restart 推倒重来。
        - 单文件产物：连产物带续传文件（.jiliu.json / .part / .aria2）一起删。
        - BT 多文件产物是目录：不在原地硬删（大目录递归删除会被安全钩子
          拦住挂死），改用新的唯一目录名重下，旧目录留给用户自己清理。
        """
        with self._lock:
            t = self._tasks.get(tid)
            if t is None or t.status == "running":
                return None
        # paused/queued 状态的先真正停掉（aria2 侧可能有活）
        if t.status in ("paused", "queued"):
            self.cancel(tid)

        import pathlib as _p
        dest = _p.Path(t.dest) if t.dest else None
        if dest and dest.is_file():
            for p in (dest,
                      _p.Path(str(dest) + ".jiliu.json"),
                      _p.Path(str(dest) + ".part"),
                      _p.Path(str(dest) + ".aria2")):
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass

        with self._lock:
            if dest and dest.is_dir():
                old = dest
                t.dest = str(unique_path(dest))
                t.add_log(f"重新下载：产物是目录，改用新目录 {t.dest}"
                          f"（旧目录 {old} 保留，可手动清理）")
            else:
                t.dest = str(dest) if dest else ""
                t.add_log("重新下载：已清除产物与续传状态")
            t.status = "queued"
            t.done = 0
            t.percent = 0.0
            t.speed = 0.0
            t.eta = None
            t.error = ""
            t.actual_sha256 = ""
            t.hash_ok = None
            t.finished = 0.0
            # filename 保留（重下同名文件），dest 若是空目录由运行器重算
            self._cv.notify_all()
        self._notify("queued", t)
        self.save()
        return t

    def shutdown(self) -> None:
        self._stopping = True
        with self._lock:
            self._cv.notify_all()
        for tid in list(self._live.keys()):
            live = self._live.get(tid)
            try:
                if hasattr(live, "cancel"):
                    live.cancel()
                elif hasattr(live, "set"):
                    live.set()
            except Exception:
                pass
        backends.shutdown()

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------

    def _pick_next(self) -> Optional[Task]:
        """从排队任务里挑下一个：优先级高的先跑，同级按创建顺序。"""
        best_key = None
        nxt = None
        for pos, i in enumerate(self._order):
            t = self._tasks.get(i)
            if t and t.status == "queued":
                key = (-t.priority, pos)
                if best_key is None or key < best_key:
                    best_key = key
                    nxt = t
        return nxt

    def _dispatch_loop(self) -> None:
        while not self._stopping:
            self._schedule_tick()
            if self._schedule_closed():
                with self._cv:
                    self._cv.wait(timeout=2.0)
                continue
            with self._lock:
                max_c = int(self.cfg.get("max_concurrent", MAX_CONCURRENT_DEFAULT))
                active = sum(1 for t in self._tasks.values() if t.status == "running")
                nxt = self._pick_next() if active < max_c else None
                if nxt is None:
                    self._cv.wait(timeout=2.0)
                    continue
                nxt.status = "running"
                nxt.started = time.time()
                th = threading.Thread(target=self._run_task, args=(nxt.id,), daemon=True,
                                      name=f"jiliu-task-{nxt.id}")
                self._threads[nxt.id] = th
                th.start()
        # 收尾
        for th in list(self._threads.values()):
            if th.is_alive():
                th.join(timeout=2)

    def _schedule_closed(self) -> bool:
        try:
            st = schedule.evaluate(self.cfg)
        except Exception:  # noqa: BLE001
            return False
        return bool(st.get("enabled") and not st.get("open"))

    def _schedule_tick(self) -> None:
        """按计划任务开/关队列，并在时段切换时改限速。"""
        try:
            st = schedule.evaluate(self.cfg)
        except Exception:  # noqa: BLE001
            return
        if not st.get("enabled"):
            # 关掉计划任务时，把之前被它暂停的任务放回来
            for t in list(self._tasks.values()):
                if t.status == "paused" and t.meta.pop("sched_paused", False):
                    self.resume(t.id)
            return

        if not st.get("open"):
            for t in list(self._tasks.values()):
                if t.status == "running" and not t.meta.get("sched_paused"):
                    t.meta["sched_paused"] = True
                    t.add_log(f"计划任务：{st.get('note')}，暂停")
                    self.pause(t.id)
        else:
            for t in list(self._tasks.values()):
                if t.status == "paused" and t.meta.pop("sched_paused", False):
                    t.add_log("计划任务：进入允许时段，继续")
                    self.resume(t.id)

        lim = int(st.get("speed_limit") or 0)
        if lim != getattr(self, "_sched_limit", -1):
            self._sched_limit = lim
            if lim:
                self.set_speed_limit(lim)
                for t in list(self._tasks.values()):
                    if t.status == "running":
                        t.add_log(f"计划任务：时段限速 {human_size(lim)}/s")

    def pause_all(self) -> int:
        """暂停所有正在跑的（托盘菜单用）。返回暂停了几个。"""
        n = 0
        for t in list(self._tasks.values()):
            if t.status == "running" and self.pause(t.id):
                n += 1
        return n

    def resume_all(self) -> int:
        """继续所有被暂停的（含计划任务暂停的）。"""
        n = 0
        for t in list(self._tasks.values()):
            if t.status == "paused" and not t.meta.get("sched_paused"):
                if self.resume(t.id):
                    n += 1
        return n

    def set_task_speed(self, tid: str, bps: int) -> Optional[Task]:
        """单任务限速：0 = 跟随全局。运行中也能改。"""
        t = self.get(tid)
        if t is None:
            return None
        bps = max(0, int(bps or 0))
        t.meta["speed_limit"] = bps
        dl = self._live.get(tid)
        if hasattr(dl, "set_speed_limit"):
            try:
                dl.set_speed_limit(bps)
            except Exception:  # noqa: BLE001
                pass
        return t

    def set_speed_limit(self, bps: int, persist: bool = False) -> int:
        """改限速：0 = 不限。persist=True 时写回配置文件。"""
        bps = max(0, int(bps or 0))
        self.cfg["speed_limit"] = bps
        if persist:
            _config.update({"speed_limit": bps})
        for dl in list(self._live.values()):
            if hasattr(dl, "set_speed_limit"):
                try:
                    dl.set_speed_limit(bps)
                except Exception:  # noqa: BLE001
                    pass
        return bps

    def _write_m3u(self, outdir: str, produced: List[str]) -> Optional[str]:
        """合集下载完生成 .m3u（按文件名自然序，不是字典序）。"""
        try:
            media = [p for p in produced
                     if p.lower().endswith((".mp4", ".mkv", ".webm", ".flv", ".mp3", ".m4a", ".ts"))]
            if len(media) < 2:
                return None
            import re as _re

            def key(p: str):
                name = pathlib.Path(p).name
                m = _re.match(r"^(\d+)", name)
                return (0, int(m.group(1)), name) if m else (1, 0, name)

            media.sort(key=key)
            out = pathlib.Path(outdir)
            m3u = out / (out.name + ".m3u")
            lines = ["#EXTM3U"]
            for p in media:
                fp = pathlib.Path(p)
                lines.append(f"#EXTINF:-1,{fp.stem}")
                lines.append(fp.name)
            m3u.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return str(m3u)
        except Exception:  # noqa: BLE001
            return None

    def _pad_produced(self, produced: List[str]) -> List[str]:
        """合集序号补零：2 → 02。返回改名后的新列表。

        只在文件名以数字开头时动手，且已补过零的不再动。
        """
        pad = int(self.cfg.get("number_pad", 2) or 0)
        if pad <= 1:
            return produced
        import re as _re

        out: List[str] = []
        for p in produced:
            path = pathlib.Path(p)
            m = _re.match(r"^(\d+)(?=\D|$)", path.name)
            if not m or len(m.group(1)) >= pad:
                out.append(p)
                continue
            new_name = f"{int(m.group(1)):0{pad}d}" + path.name[m.end():]
            new_path = path.with_name(new_name)
            try:
                if not new_path.exists():
                    path.rename(new_path)
                    out.append(str(new_path))
                    continue
            except Exception:  # noqa: BLE001
                pass
            out.append(p)
        return out

    def _progress_factory(self, t: Task):
        def cb(snap: Dict[str, Any]) -> None:
            if not isinstance(snap, dict):
                return
            if snap.get("size"):
                t.size = int(snap["size"])
            if snap.get("done") is not None:
                t.done = int(snap["done"])
            if snap.get("percent") is not None:
                t.percent = float(snap["percent"])
            if snap.get("speed") is not None:
                t.speed = float(snap["speed"])
            t.eta = snap.get("eta")
            if snap.get("dest"):
                t.dest = str(snap["dest"])
            if snap.get("filename"):
                t.filename = str(snap["filename"])
            if snap.get("backend"):
                t.meta["effective_backend"] = snap["backend"]
            seg = snap.get("segments_created") or snap.get("segments_total")
            if seg:
                t.meta["segments"] = seg
            if snap.get("p2p"):
                # BT 的「做种数 / 连接数」比速度更能解释「为什么这么慢」，
                # 单拎出来给界面显示
                t.meta["p2p"] = True
                bt = snap.get("bt") or {}
                if bt:
                    t.meta["bt"] = {
                        "info_hash": bt.get("info_hash") or "",
                        "seeders": bt.get("num_seeders") or 0,
                        "connections": bt.get("connections") or 0,
                        "is_seeder": bool(bt.get("is_seeder")),
                    }
        return cb

    def _run_task(self, tid: str) -> None:
        t = self.get(tid)
        if t is None:
            return
        t.add_log(f"开始下载（{t.backend or self.cfg.get('backend', 'auto')} / {t.threads} 线程）")
        self._notify("started", t)
        if self.cfg.get("prevent_sleep", True):
            notify.prevent_sleep(True)

        try:
            if t.kind == "video":
                self._run_video(t)
            else:
                self._run_http(t)
        except Exception as exc:  # noqa: BLE001
            t.status = "error"
            t.error = f"{type(exc).__name__}: {exc}"
            t.add_log("异常: " + t.error)
        finally:
            t.finished = t.finished or time.time()
            t.speed = 0
            t.eta = None
            if self.cfg.get("prevent_sleep", True):
                notify.prevent_sleep(False)
            with self._lock:
                self._live.pop(tid, None)
                self._threads.pop(tid, None)
                self._cv.notify_all()
            self._notify("finished", t)
            self._after_task(t)
            self.save()

    def _after_task(self, t: Task) -> None:
        """完成/失败后的外部动作：通知 + 自定义命令。

        放在 finally 的末尾：即使 save 或通知出错，也不该影响任务状态。
        """
        try:
            if t.status == "done":
                if self.cfg.get("notify_enabled", True):
                    notify.notify("汲流 · 下载完成", t.filename or t.url)
                notify.run_after_command(self.cfg, t.dest)
                # 合集产物是目录，m3u 要等所有条目落地后再写（见 _run_video）
            elif t.status == "error" and self.cfg.get("notify_enabled", True):
                notify.notify("汲流 · 下载失败", f"{t.filename or t.url}：{t.error or ''}")
        except Exception:  # noqa: BLE001
            pass

    def _run_http(self, t: Task) -> None:
        import pathlib as _p

        d = _p.Path(t.dest_dir)
        d.mkdir(parents=True, exist_ok=True)
        is_p2p = bool((t.meta or {}).get("p2p")) or protocols.detect(t.url).p2p
        name = t.filename
        if not name and t.dest:
            name = _p.Path(t.dest).name
        if is_p2p:
            # 磁力/种子/ed2k 不能探测：magnet: 不是 HTTP，HEAD 过去只会报错。
            # 文件名也由 BT 元数据决定，这里给个占位就行 —— 反正 aria2
            # 收到的只有 dir（多文件场景下不能套 out）。
            if not name:
                name = "BT 下载"
            t.filename = t.filename or name
            t.add_log("BT 任务：交给 aria2（开 DHT/PEX/LPD 找源）")
        elif not name:
            from .engine import probe as _probe
            pr = _probe(t.url, self.cfg)
            name = pr.filename or "download.bin"
            t.filename = name
            t.size = pr.size or 0
        target = _p.Path(t.dest) if t.dest else (d / name)

        # 重复链接策略（对齐 IDM 的「续传 / 另存 / 跳过」三选）：
        #   rename   = 默认，自动加 (1)
        #   skip     = 已有完整文件就当作完成，不再占带宽
        #   overwrite= 覆盖重下
        #   resume   = 有 sidecar 就接着下，没有就当覆盖
        policy = str(self.cfg.get("duplicate_policy") or "rename").strip().lower()
        if target.exists():
            if policy == "skip":
                t.status = "done"
                t.percent = 100.0
                t.dest = str(target)
                t.filename = target.name
                try:
                    t.size = t.done = int(target.stat().st_size or 0)
                except Exception:  # noqa: BLE001
                    pass
                t.add_log("目标文件已存在，按「跳过」策略直接完成")
                return
            if policy in ("rename", "new", ""):
                target = unique_path(target)
        dest = target
        t.dest = str(dest)
        t.filename = dest.name

        # 任务专属请求头（网盘解析产物有时要带 Cookie/Referer 才给下）
        task_headers = dict((t.meta or {}).get("headers") or {})
        # 单任务限速：0 表示跟随全局（传 None 才会落回 cfg）
        per_speed = int((t.meta or {}).get("speed_limit") or 0) or None

        dl = backends.make_downloader(
            t.url, dest,
            cfg=self.cfg,
            threads=t.threads,
            headers=task_headers or None,
            speed_limit=per_speed,
            prefer=t.backend if t.backend in ("native", "aria2", "curl", "wget")
            else None,
            progress_cb=self._progress_factory(t),
            log_cb=lambda m: t.add_log(m),
            chunk_size=t.chunk_size or None,
            verify_hash=t.verify_hash,
            expected_sha256=t.expected_sha256,
        )
        t.backend = _engine_name(dl)
        self._live[t.id] = dl

        res = dl.run()

        # —— 换引擎重试 ——
        # 兜底引擎的价值就在这里：自研引擎偶尔会撞上挑食的服务端，
        # 静默换 curl 重来一遍，比让用户对着一个错误码去设置里翻强。
        # 只在**真失败**时触发 —— 取消和暂停不是失败。
        # P2P 任务绝不回退：磁力/种子只有 aria2 能接，回退到 native/curl
        # 会把 .torrent 文件本身当成产物下载（实测踩过：任务「成功」，
        # 落盘的是一份种子文件）。失败就如实报错。
        if (not res.get("ok") and not res.get("cancelled")
                and res.get("hash_ok") is not False
                and not dl.snapshot().get("paused") and not is_p2p):
            tried = [t.backend]
            for eng in backends.fallback_chain(self.cfg, t.backend):
                if eng in tried:
                    continue
                _err = str(res.get("error") or "")
                t.add_log(f"{t.backend} 失败"
                          + (f"（{_err}）" if _err else "")
                          + f"，自动改用 {eng} 重试一次")
                try:
                    dl2 = backends.make_downloader(
                        t.url, dest, cfg=self.cfg, threads=t.threads,
                        headers=task_headers or None,
                        speed_limit=per_speed,
                        prefer=eng,
                        progress_cb=self._progress_factory(t),
                        log_cb=lambda m: t.add_log(m),
                        verify_hash=t.verify_hash,
                        expected_sha256=t.expected_sha256,
                    )
                except Exception as exc:  # noqa: BLE001
                    t.add_log(f"{eng} 不可用：{exc}")
                    tried.append(eng)
                    continue
                t.backend = _engine_name(dl2)
                self._live[t.id] = dl2
                tried.append(eng)
                res = dl2.run()
                if res.get("ok") or res.get("cancelled"):
                    break

        t.actual_sha256 = str(res.get("sha256") or "")
        if res.get("hash_ok") is not None:
            t.hash_ok = bool(res["hash_ok"])
        if res.get("ok"):
            t.status = "done"
            t.percent = 100.0
            t.done = int(res.get("bytes") or t.size or 0)
            t.size = t.size or t.done
            t.dest = res.get("path") or t.dest
            t.filename = _p.Path(t.dest).name
            rb = int(res.get("resumed_bytes") or 0)
            extra = f"，其中续传复用 {human_size(rb)}" if rb else ""
            t.add_log(f"完成：{human_size(t.done)}，耗时 {res.get('seconds', 0):.1f}s{extra}")
            if t.hash_ok:
                t.add_log("哈希校验通过 ✓")
        elif res.get("cancelled"):
            t.status = "cancelled"
            t.add_log("已取消")
        else:
            t.status = "error"
            t.error = str(res.get("error") or "未知错误")
            t.add_log("失败: " + t.error)

    def _video_rate_gate(self, t: Task) -> bool:
        """同站视频起跑错峰。返回 True 表示等待期间被取消。

        为什么不做配置项：间隔值只有「风控视角」这一个正确来源，
        暴露给用户只会被改成 0 然后来问为什么账号被限流。批量合集
        （from=playlist）用完整间隔，单发视频只要和上一个同站任务
        错开半程即可。每次的间隔带随机抖动 —— 固定间隔本身也是
        机器行为特征。
        """
        try:
            host = (urlparse(t.url).hostname or "").lower() or "unknown"
        except Exception:  # noqa: BLE001
            return False
        batch = str((t.meta or {}).get("from") or "") == "playlist"
        base = VIDEO_GAP_BASE if batch else VIDEO_GAP_BASE / 2
        now = time.monotonic()
        with self._video_gate_lock:
            nxt = self._video_next_start.get(host, 0.0)
            wait = max(0.0, nxt - now)
            # 预占下一个起跑点：并发进来的任务排的是错开的队列，
            # 谁也不用真的持锁等
            self._video_next_start[host] = max(now, nxt) + base + random.random() * VIDEO_GAP_JITTER
        if wait < 0.5:
            return False
        t.add_log(f"防风控：同站间隔，{wait:.0f}s 后开始")
        ev = self._live.get(t.id)
        if isinstance(ev, threading.Event) and ev.wait(wait):
            return True          # 等待期间被取消
        time.sleep(0.0)
        return False

    def _try_bili_native(self, t: Task, cb, ev: threading.Event, is_pl: bool,
                         audio_only: bool, subs_only: bool) -> Optional[Dict[str, Any]]:
        """先试 B 站原生 DASH；不可用/失败就返回 None，让调用方走 yt-dlp。

        这里刻意把所有异常都吞掉并记日志：原生路径是"加速选项"，不是
        必需品。接口改签名、风控、没装 ffmpeg —— 任何一种都不该让下载失败。
        """
        try:
            from . import bili_dash
        except Exception:  # noqa: BLE001
            return None
        try:
            if not bili_dash.should_use(self.cfg, t.url, playlist=is_pl,
                                        audio_only=audio_only, subs_only=subs_only):
                return None
            t.add_log("使用 B 站原生 DASH 取流（playurl + WBI）")
            nb = bili_dash.download(
                t.url, pathlib.Path(t.dest_dir), self.cfg,
                progress_cb=cb, log_cb=lambda m: t.add_log(m),
                cancel_event=ev, audio_only=audio_only,
            )
            if nb.get("ok") or nb.get("cancelled"):
                if nb.get("ok"):
                    t.meta["effective_backend"] = "bili-dash"
                    t.meta["bili_qn"] = nb.get("quality")
                return nb
            t.add_log("原生取流不可用，回退 yt-dlp：" + str(nb.get("error") or "未知原因"))
            return None
        except Exception as exc:  # noqa: BLE001
            t.add_log(f"原生取流异常，回退 yt-dlp：{type(exc).__name__}: {exc}")
            return None

    def _run_video(self, t: Task) -> None:
        ev = threading.Event()
        self._live[t.id] = ev
        m = t.meta or {}
        is_pl = bool(m.get("playlist"))
        items = str(m.get("playlist_items") or "")
        group = str(m.get("group_title") or "")
        audio_only = bool(m.get("audio_only"))
        subs_only = bool(m.get("subs_only"))
        what = "合集" if is_pl else "视频"
        if subs_only:
            t.add_log("yt-dlp 下载字幕（纯字幕模式，不下视频）")
        elif is_pl and items:
            t.add_log(f"yt-dlp 解析并下载（合集 {items}，格式 {t.format}）")
        elif audio_only:
            t.add_log(f"yt-dlp 抽音轨（{self.cfg.get('audio_format') or 'mp3'}）")
        else:
            t.add_log(f"yt-dlp 解析并下载（{what}，格式 {t.format}）")

        # 防风控：同站起跑错峰。被取消就到此为止。
        if self._video_rate_gate(t):
            t.status = "cancelled"
            t.add_log("已取消")
            self._notify("cancelled", t)
            self.save()
            return

        def cb(snap: Dict[str, Any]) -> None:
            if snap.get("size"):
                t.size = int(snap["size"])
            if snap.get("done") is not None:
                t.done = int(snap["done"])
            if snap.get("percent") is not None:
                t.percent = float(snap["percent"])
            if snap.get("speed") is not None:
                t.speed = float(snap["speed"])
            t.eta = snap.get("eta")
            t.meta["effective_backend"] = "yt-dlp"
            # 合集时把「正在下第几集」透出来，界面才看得出进度
            if snap.get("group_title"):
                t.meta["group_title"] = snap["group_title"]

        res = self._try_bili_native(t, cb, ev, is_pl, audio_only, subs_only)
        if res is None:
            res = video.download(
                t.url, pathlib.Path(t.dest_dir), self.cfg,
                fmt=t.format, progress_cb=cb,
                log_cb=lambda m: t.add_log(m),
                cancel_event=ev,
                playlist=is_pl,
                items=items,
                group_title=group,
                audio_only=audio_only,
                subs_only=subs_only,
            )
        if res.get("ok"):
            t.status = "done"
            t.percent = 100.0
            import pathlib as _p
            produced = [str(x) for x in (res.get("produced") or [])]
            if is_pl:
                # 合集下来是一堆文件。报「最后一个文件的名字」没有意义，
                # 报条目数、并把目录指给大家看才是有用的信息。
                t.dest = res.get("outdir") or t.dest_dir
                t.filename = f"合集 · {len(produced) or '若干'} 个文件"
                t.add_log(f"合集下载完成，共 {len(produced)} 个文件 → {t.dest}")
                produced = self._pad_produced(produced)
                if self.cfg.get("m3u_enabled", True):
                    m3u = self._write_m3u(t.dest, produced)
                    if m3u:
                        t.add_log(f"已生成播放列表：{pathlib.Path(m3u).name}")
                if t.size:
                    t.done = t.size
            else:
                t.dest = res.get("path") or t.dest_dir
                t.filename = _p.Path(t.dest).name if t.dest else ""
                if t.dest and _p.Path(t.dest).exists():
                    t.size = _p.Path(t.dest).stat().st_size or t.size
                    t.done = t.size
                t.add_log(f"完成：{t.filename}，耗时 {res.get('seconds', 0):.1f}s")
        elif res.get("cancelled"):
            t.status = "cancelled"
            t.add_log("已取消")
        else:
            t.status = "error"
            t.error = str(res.get("error") or "未知错误")
            t.add_log("失败: " + t.error)
