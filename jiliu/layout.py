"""下载目录的分类归档布局。

需求来自实际使用的痛点：所有东西一股脑倒进 `Downloads`，
下完想找昨天那个安装包，得在几百个文件里翻。

于是：**认出这是什么类型，就放进对应的子文件夹**。

    <下载目录>/
    ├── 视频/
    ├── 音频/
    ├── 软件/
    ├── 压缩包/
    ├── 文档/
    ├── 光盘镜像/
    ├── 图片/
    ├── 字体/
    ├── 数据集/
    ├── BT/
    └── 网盘/

几个刻意的决定：

  - **「其他文件」不建子目录**，直接落在下载目录根。
    认不出来的东西再塞进一个「其他」文件夹，等于把所有杂物堆在同一处，
    下次还是找不到 —— 不如让它在根目录显眼。
  - **用户显式指定了目录就不归档**。手动选了目录 = 明确意图，
    再自作主张套一层是添乱。（要强制归档可开 `subdir_explicit`。）
  - **只接受单个路径片段**。配置里写 `../../Windows` 这种必须被挡掉 ——
    这类配置项是本地接口能写进来的，路径穿越是实打实的风险。
"""
from __future__ import annotations

import pathlib
import re
from typing import Any, Dict, Optional

# 分类 key → 子目录名。key 与 resource.CATEGORIES 一一对应，
# 有一项对不上就说明分类体系改了而这个模块忘了跟 —— 由 tests 守着。
SUBDIRS: Dict[str, str] = {
    "video": "视频",
    "audio": "音频",
    "playlist": "视频",        # 合集本质还是视频，不再单开一层
    "app": "软件",
    "archive": "压缩包",
    "document": "文档",
    "disk": "光盘镜像",
    "image": "图片",
    "font": "字体",
    "dataset": "数据集",
    "bt": "BT",
    "netdisk": "网盘",
    "other": "",               # 空 = 不建子目录，落在下载目录根
}

# 面板上给用户挑的候选名（改动风险低，就不做自由输入了）
PRESETS = ["视频", "音频", "软件", "压缩包", "文档", "光盘镜像",
           "图片", "字体", "数据集", "BT", "网盘", "下载"]

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_segment(name: str, fallback: str = "") -> str:
    """把一个用户填的目录名收干净。

    只允许**单个**片段：去掉任何路径分隔符与上跳，防止 `..\\..\\Windows`
    这类穿越。Windows 非法字符一并清掉。
    """
    s = (name or "").strip()
    if not s:
        return fallback
    # 先按分隔符切，只取最后一段 —— 顺带干掉 ../ 这种
    s = s.replace("\\", "/").split("/")[-1]
    s = s.replace("..", "")
    s = _ILLEGAL.sub("", s).strip(" .")
    if s in ("", ".", ".."):
        return fallback
    return s[:64]


def folder_name(category: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    """这个分类该用哪个子目录名。空串 = 不建子目录。"""
    cfg = cfg or {}
    cat = (category or "other").strip() or "other"

    overrides = cfg.get("subdir_names") or {}
    if isinstance(overrides, dict) and cat in overrides:
        raw = overrides.get(cat)
        if raw == "":            # 显式空 = 这一类不归档
            return ""
        return sanitize_segment(str(raw), SUBDIRS.get(cat, ""))
    return SUBDIRS.get(cat, "")


def enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    cfg = cfg or {}
    return bool(cfg.get("auto_subdir", True))


def target_dir(base: pathlib.Path, category: str,
               cfg: Optional[Dict[str, Any]] = None, *,
               explicit: bool = False) -> pathlib.Path:
    """给定下载根目录与分类，返回文件该落到的目录。

    explicit=True 表示调用方已经显式指定了目录（新建任务时手填的），
    此时默认**不再套一层**，除非用户开了 subdir_explicit。
    """
    cfg = cfg or {}
    base = pathlib.Path(base)
    if not enabled(cfg):
        return base
    if explicit and not cfg.get("subdir_explicit"):
        return base

    name = folder_name(category, cfg)
    if not name:
        return base
    return base / name


def ensure(base: pathlib.Path, category: str,
           cfg: Optional[Dict[str, Any]] = None, *,
           explicit: bool = False) -> pathlib.Path:
    """同 target_dir，但顺手把目录建好。"""
    d = target_dir(base, category, cfg, explicit=explicit)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        # 建不出来就退回根目录 —— 归档失败不该让下载失败
        return pathlib.Path(base)
    return d


def describe(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给界面看的归档布局说明。"""
    cfg = cfg or {}
    return {
        "enabled": enabled(cfg),
        "explicit_also": bool(cfg.get("subdir_explicit")),
        "folders": [
            {"category": k, "label": _label(k), "folder": folder_name(k, cfg)}
            for k in SUBDIRS
        ],
        "presets": PRESETS,
        "sample": _sample(cfg),
    }


def _label(category: str) -> str:
    try:
        from . import resource
        c = resource.CATEGORIES.get(category) or {}
        return str(c.get("label") or category)
    except Exception:  # noqa: BLE001
        return category


def _sample(cfg: Dict[str, Any]) -> str:
    """生成一段示例树，设置页里直接给用户看效果。"""
    lines = ["<下载目录>/"]
    for cat in ("video", "audio", "app", "archive", "document", "image",
                "bt", "netdisk"):
        f = folder_name(cat, cfg)
        if f:
            lines.append(f"├── {f}/")
    lines.append("└── （其他文件直接放这里）")
    return "\n".join(lines)
