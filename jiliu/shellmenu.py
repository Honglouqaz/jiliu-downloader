"""系统右键菜单（资源管理器集成）。

为什么用 HKCU 而不是 HKLM
    HKLM 要管理员权限，而"装个下载器还要 UAC"是劝退第一步。
    资源管理器会合并 HKCU 与 HKLM 的 shell 项，效果一样。

注册了什么
    *\\shell\\Jiliu            任意文件右键 → 用汲流下载
    Directory\\shell\\Jiliu    文件夹右键 → 下载该文件夹里的直链清单？（不，是抓取）
    Directory\\Background\\shell\\Jiliu  目录空白处右键 → 下载剪贴板里的链接

    第三个最实用：用户在浏览器复制链接后在下载目录里右键一下就能下。
    它走 `add --clipboard`，不带参数时读剪贴板。

为什么还要记一份「注册时的 exe 路径」
    便携版换了目录（U 盘拔了插到另一台机）之后，注册表里躺着的是
    旧路径，右键点了会弹"找不到文件"。status() 比对当前路径就能报出来。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from typing import Any, Dict, List

MENU_KEY = "Jiliu"
MENU_LABEL = "用汲流下载"

# (根键路径, 显示名, 命令模板里的参数)
_SCOPES = [
    (r"Software\Classes\*\shell\Jiliu", "任意文件", '"%1"'),
    (r"Software\Classes\Directory\shell\Jiliu", "文件夹", '"%1"'),
    (r"Software\Classes\Directory\Background\shell\Jiliu", "目录空白处", "--clipboard"),
]


def exe_path() -> str:
    """右键菜单该调哪个可执行文件。

    冻结运行 = 自己；源码运行 = python + jiliu.py，这样开发时也能试。
    """
    if getattr(sys, "frozen", False):
        return str(pathlib.Path(sys.executable).resolve())
    root = pathlib.Path(__file__).resolve().parent.parent
    return f'"{sys.executable}" "{root / "jiliu.py"}"'


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _command(scope: str) -> str:
    exe = exe_path()
    extra = "--clipboard"
    for s, _, arg in _SCOPES:
        if s == scope:
            extra = arg
            break
    if _is_frozen():
        return f'"{exe}" add {extra}'
    # 源码运行：exe_path 已经带引号了
    return f'{exe} add {extra}'


def register(cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """写入三处右键菜单。返回 {ok, wrote, error}。"""
    if os.name != "nt":
        return {"ok": False, "wrote": 0, "error": "只有 Windows 支持"}
    import winreg

    wrote = 0
    errors: List[str] = []
    for scope, label, _ in _SCOPES:
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, scope, 0,
                                    winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, MENU_LABEL)
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, exe_path().strip('"'))
                winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
                # 记下注册时的目标，用来判断"注册项还指向现在这个 exe 吗"
                winreg.SetValueEx(k, "JiliuPath", 0, winreg.REG_SZ, exe_path())
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, scope + r"\command", 0,
                                    winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _command(scope))
            wrote += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")
    return {"ok": wrote == len(_SCOPES), "wrote": wrote,
            "error": "; ".join(errors)}


def unregister() -> Dict[str, Any]:
    import winreg

    removed = 0
    for scope, _, _ in _SCOPES:
        for path in (scope + r"\command", scope):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
                if not path.endswith("command"):
                    removed += 1
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return {"ok": True, "removed": removed}


def status() -> Dict[str, Any]:
    """注册状态。registered=三处都在；stale=在但指向别的 exe。"""
    if os.name != "nt":
        return {"supported": False, "registered": False, "stale": False,
                "scopes": [], "exe": "", "note": "只有 Windows 支持"}
    import winreg

    want = exe_path()
    want_norm = want.replace('"', "").lower()
    scopes: List[Dict[str, Any]] = []
    for scope, label, _ in _SCOPES:
        item: Dict[str, Any] = {"scope": scope, "label": label,
                                "registered": False, "command": "", "stale": False}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, scope + r"\command") as k:
                cmd = str(winreg.QueryValueEx(k, "")[0])
                item["registered"] = True
                item["command"] = cmd
        except Exception:
            pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, scope) as k:
                item["target"] = str(winreg.QueryValueEx(k, "JiliuPath")[0])
                item["stale"] = item["target"] != want
        except Exception:
            # 老版本写的注册项没有 JiliuPath，按命令首段比一比
            if item["registered"]:
                first = cmd.split('"')
                target = first[1] if len(first) > 1 else cmd.split()[0]
                item["stale"] = target.strip('"').lower() != want_norm
        scopes.append(item)

    reg = all(s["registered"] for s in scopes)
    return {
        "supported": True,
        "registered": reg,
        "stale": any(s["stale"] for s in scopes),
        "scopes": scopes,
        "exe": want,
        "note": "" if reg else "未注册",
    }


def refresh_if_registered() -> None:
    """exe 路径变了（更新/挪目录）时，把过期项重写成当前路径。"""
    st = status()
    if st.get("registered") and st.get("stale"):
        register()


# ----------------------------------------------------------------------
# 命令行入口：explorer 调用 `jiliu.exe add "%1"`
# ----------------------------------------------------------------------

def paths_from_args(args: List[str]) -> List[str]:
    """把右键菜单/拖拽传进来的参数变成 URL 或本地路径。

    本地路径转成 file:// —— 交给引擎时统一按 URL 处理，
    下载器的 probe 已经支持 file:// 之外 http(s)，本地文件走 copy 分支。
    """
    out: List[str] = []
    for a in args:
        a = (a or "").strip().strip('"')
        if not a or a.startswith("-"):
            continue
        out.append(a)
    return out


def reveal_in_explorer(path: str) -> bool:
    """打开资源管理器并选中该文件。"""
    p = pathlib.Path(path)
    try:
        if p.is_file():
            subprocess.Popen(["explorer", "/select,", str(p)])
        elif p.is_dir():
            subprocess.Popen(["explorer", str(p)])
        else:
            return False
        return True
    except Exception:
        return False
