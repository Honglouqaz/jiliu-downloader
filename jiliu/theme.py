"""主题与配色。

    from jiliu import theme
    theme.apply(cfg)          # {"mode": "dark", "tokens": {...}, "css": ":root{...}"}

设计约束：前端是一份**单文件** HTML（内联 CSS + 内联 JS），没有构建步骤，
所以主题不能靠换 CSS 文件 —— 只能是「往 :root 里塞一组 CSS 变量」。
前端所有颜色都从变量取，切主题 = 换一组变量值，不用重绘任何结构。

三套预设：
    ink    墨（默认，浅色水墨）
    paper  纸（更亮的浅色，长时间看不累）
    night  夜（深色，OLED 友好）

accent 是强调色，跟「涨红跌绿」那套无关 —— 它是按钮和进度的颜色。
"""
from __future__ import annotations

from typing import Any, Dict, List

MODES = ("auto", "light", "dark")
PRESETS = ("ink", "paper", "night")

# 每个预设两套（浅/深），auto 由前端按 prefers-color-scheme 选
TOKENS: Dict[str, Dict[str, Dict[str, str]]] = {
    # 变量名必须和 web/index.html 里 `:root{...}` 的**完全一致** ——
    # 前端那套 CSS 已经写好了，主题只负责换值，不改名字。
    "ink": {
        "light": {
            "bg": "#f6f5f2", "chrome": "#efece6", "panel": "#ffffff",
            "panel2": "#f7f5f1", "line": "#e3e0da", "line2": "#d7d3cb",
            "text": "#22201c", "dim": "#7c7770", "dim2": "#a29d95",
            "label": "#6d6862", "accent": "#2f6f66", "accent2": "#245a53",
            "ok": "#2f7d4f", "err": "#b03a34", "warn": "#a8752a",
            "blue": "#2b5f8a", "sel": "#dff0ec",
        },
        "dark": {
            "bg": "#0d1015", "chrome": "#151922", "panel": "#141821",
            "panel2": "#1b202a", "line": "#252b36", "line2": "#2e3646",
            "text": "#e6e9ef", "dim": "#8b93a3", "dim2": "#5f6877",
            "label": "#9aa4b4", "accent": "#3fd0c9", "accent2": "#2ba8a2",
            "ok": "#4ade80", "err": "#f87171", "warn": "#fbbf24",
            "blue": "#6ea8fe", "sel": "#1d3b3a",
        },
    },
    "paper": {
        "light": {
            "bg": "#fbfaf7", "chrome": "#f2f0ea", "panel": "#ffffff",
            "panel2": "#f6f4ef", "line": "#e8e5dd", "line2": "#dbd7cd",
            "text": "#1f1e1b", "dim": "#82807a", "dim2": "#a8a49c",
            "label": "#6f6d67", "accent": "#2b5f8a", "accent2": "#224b6d",
            "ok": "#2f7d4f", "err": "#b03a34", "warn": "#a8752a",
            "blue": "#2b5f8a", "sel": "#dfe9f3",
        },
        "dark": {
            "bg": "#1b1d1f", "chrome": "#212427", "panel": "#232629",
            "panel2": "#282c30", "line": "#363b40", "line2": "#41474d",
            "text": "#eceae6", "dim": "#9d9a94", "dim2": "#6b6862",
            "label": "#a5a29b", "accent": "#79b0d8", "accent2": "#5a91b9",
            "ok": "#63b184", "err": "#e2706a", "warn": "#d3a256",
            "blue": "#8ab6dd", "sel": "#26333d",
        },
    },
    "night": {
        "light": {
            # 预设叫「夜」，浅色态给低对比的灰蓝，避免刺眼
            "bg": "#eef1f4", "chrome": "#e5e9ee", "panel": "#ffffff",
            "panel2": "#e9edf1", "line": "#d5dbe2", "line2": "#c7ced7",
            "text": "#1b2026", "dim": "#6f7885", "dim2": "#98a1ad",
            "label": "#5f6874", "accent": "#3f6b9c", "accent2": "#33567d",
            "ok": "#2f7d4f", "err": "#b03a34", "warn": "#a8752a",
            "blue": "#3f6b9c", "sel": "#dde6ef",
        },
        "dark": {
            "bg": "#0d1013", "chrome": "#12161a", "panel": "#151a1f",
            "panel2": "#1a2026", "line": "#262e36", "line2": "#303a44",
            "text": "#dfe4ea", "dim": "#8b949f", "dim2": "#5b636d",
            "label": "#98a1ac", "accent": "#5f9cd8", "accent2": "#4a83ba",
            "ok": "#5fb383", "err": "#e0736d", "warn": "#d5a458",
            "blue": "#6b9fd6", "sel": "#1c2733",
        },
    },
}

ACCENTS = {
    "": "跟随预设",
    "jade": "#2f6f66",
    "rust": "#8a5a2b",
    "ink": "#2b3a4a",
    "plum": "#7a4a6a",
}


def preset_of(cfg: Dict[str, Any]) -> str:
    p = str((cfg or {}).get("ui_theme") or "ink").lower()
    return p if p in PRESETS else "ink"


def mode_of(cfg: Dict[str, Any]) -> str:
    m = str((cfg or {}).get("ui_mode") or "auto").lower()
    return m if m in MODES else "auto"


def tokens(cfg: Dict[str, Any], mode: str = "") -> Dict[str, str]:
    """取一组颜色。mode 是实际生效的 light/dark（auto 由调用方解析）。"""
    p = preset_of(cfg)
    m = (mode or mode_of(cfg)).lower()
    if m not in ("light", "dark"):
        m = "light"
    base = dict(TOKENS[p][m])
    accent = str((cfg or {}).get("ui_accent") or "").strip()
    if accent in ACCENTS and accent:
        base["accent"] = ACCENTS[accent]
    else:
        # 允许直接写 #rrggbb
        if accent.startswith("#") and len(accent) in (4, 7):
            base["accent"] = accent
    return base


def to_css(cfg: Dict[str, Any], mode: str = "") -> str:
    """生成一段 :root{...}，前端往 <style> 里一塞就完事。"""
    tk = tokens(cfg, mode)
    lines = [":root{"]
    for k, v in tk.items():
        lines.append(f"  --{k}:{v};")
    lines.append("}")
    return "\n".join(lines)


def apply(cfg: Dict[str, Any], mode: str = "") -> Dict[str, Any]:
    """给前端/桌面窗口用的一次性结果。"""
    p = preset_of(cfg)
    m = mode_of(cfg)
    return {
        "preset": p,
        "mode": m,
        "resolved": "dark" if m == "dark" else ("light" if m == "light" else "auto"),
        "tokens": tokens(cfg, "dark" if m == "dark" else "light"),
        "light": tokens(cfg, "light"),
        "dark": tokens(cfg, "dark"),
        "css": to_css(cfg, "dark" if m == "dark" else "light"),
        "presets": list(PRESETS),
        "modes": list(MODES),
        "accents": [{"key": k, "value": v} for k, v in ACCENTS.items()],
    }


def describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    d = apply(cfg)
    d["note"] = "前端所有颜色都取自 --bg/--panel/--text 等变量，切主题只换变量"
    return d
