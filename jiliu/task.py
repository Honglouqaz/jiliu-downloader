"""任务模型。

一个任务从创建到结束的状态机：

    queued ──▶ running ──▶ done
       │          │  ▲
       │          │  └── paused（暂停后可恢复）
       │          ├──▶ error
       │          └──▶ cancelled
       └──▶ interrupted（进程重启时，未完成的会被标成这个，可恢复）
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .engine import human_size

STATUSES = ("queued", "running", "paused", "done", "error", "cancelled", "interrupted")

# 可以再次启动的状态
RESUMABLE = ("paused", "error", "cancelled", "interrupted")

STATUS_TEXT = {
    "queued": "排队中",
    "running": "下载中",
    "paused": "已暂停",
    "done": "已完成",
    "error": "出错",
    "cancelled": "已取消",
    "interrupted": "已中断",
}

# 分类键 -> 中文名（resource.CATEGORIES 的轻量镜像，避免 task 依赖 resource）
CATEGORY_TEXT = {
    "video": "视频", "audio": "音频", "playlist": "合集/播放列表",
    "app": "应用", "archive": "压缩包",
    "document": "文档", "disk": "光盘镜像", "image": "图片", "font": "字体",
    "dataset": "数据集/模型", "bt": "BT/磁力", "netdisk": "网盘链接",
    "other": "其他文件",
}


@dataclass
class Task:
    url: str
    kind: str = "http"            # http | video
    backend: str = ""             # native | aria2 | yt-dlp
    threads: int = 8
    dest_dir: str = ""
    dest: str = ""                # 最终文件路径
    filename: str = ""
    format: str = ""              # 视频任务用
    title: str = ""
    status: str = "queued"
    size: int = 0
    done: int = 0
    percent: float = 0.0
    speed: float = 0.0
    eta: Optional[float] = None
    error: str = ""
    # ---- 资源识别与类型策略（见 resource.py）----
    category: str = ""            # video/audio/app/archive/document/disk/image/font/dataset/other
    chunk_size: int = 0           # 按类型建议的分片大小，0 = 自适应
    verify_hash: bool = False     # 下完是否算 SHA256
    expected_sha256: str = ""     # 用户提供的预期哈希
    actual_sha256: str = ""       # 实际算出来的
    hash_ok: Optional[bool] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    # 队列优先级：越大越先被调度。0 = 普通；「置顶」= 当前最大 +1。
    priority: int = 0
    # 所属队列（见 queues.py）。空 = default，老任务自然落到默认队列
    queue: str = ""
    log: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    # ---- 对外序列化 ----

    def to_dict(self, log_tail: int = 0) -> Dict[str, Any]:
        plan = (self.meta or {}).get("resource") or {}
        if not plan.get("category"):
            # 老任务（本功能之前存下来的）或者关掉自动分类的，类型会是空的。
            # 这里按文件名/URL 做一次离线识别补上 —— 不联网、结果缓存进 meta，
            # 免得每秒刷一次面板就重算一次。
            plan = self._offline_plan() or plan
        d = {
            "id": self.id,
            "url": self.url,
            "kind": self.kind,
            "backend": self.backend,
            "threads": self.threads,
            "dest_dir": self.dest_dir,
            "dest": self.dest,
            "filename": self.filename,
            "format": self.format,
            "title": self.title,
            "status": self.status,
            "status_text": STATUS_TEXT.get(self.status, self.status),
            "resumable": self.status in RESUMABLE,
            "category": self.category or plan.get("category", ""),
            "category_label": plan.get("label") or CATEGORY_TEXT.get(self.category, ""),
            "category_type": plan.get("filetype") or "",
            "chunk_size": self.chunk_size,
            "chunk_text": human_size(self.chunk_size) if self.chunk_size else "自适应",
            "verify_hash": self.verify_hash,
            "sha256": self.actual_sha256,
            "hash_ok": self.hash_ok,
            "size": self.size,
            "size_text": human_size(self.size) if self.size else "-",
            "done": self.done,
            "done_text": human_size(self.done) if self.done else "-",
            "percent": round(self.percent, 2),
            "speed": self.speed,
            "speed_text": (human_size(self.speed) + "/s") if self.speed else "-",
            "eta": self.eta,
            "eta_text": _eta_text(self.eta),
            "error": self.error,
            "priority": self.priority,
            "queue": self.queue or "default",
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "elapsed": (self.finished or time.time()) - self.started if self.started else 0,
            "meta": self.meta,
            "log_lines": len(self.log),
        }
        if log_tail:
            d["log"] = self.log[-log_tail:]
        return d

    def add_log(self, line: str, keep: int = 200) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {line}")
        if len(self.log) > keep:
            del self.log[: len(self.log) - keep]

    def _offline_plan(self) -> Dict[str, Any]:
        """按文件名/URL 做一次不联网的类型识别，结果写回 meta 缓存。"""
        try:
            from . import resource as _res
            plan = _res.classify(url=self.url, filename=self.filename)
            d = plan.to_dict()
            self.meta["resource"] = d
            if not self.category:
                self.category = plan.category
            return d
        except Exception:
            return {}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        allowed = set(cls.__dataclass_fields__.keys())  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in allowed})


def _eta_text(sec: Any) -> str:
    try:
        s = int(sec)
    except Exception:
        return "-"
    if s <= 0:
        return "-"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"
