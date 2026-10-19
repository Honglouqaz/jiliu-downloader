#!/usr/bin/env python3
"""把「原生窗口运行时」搬进 buildtools。

为什么需要这个脚本
------------------
汲流 1.7.0 及以前的 exe 里**根本没有 pywebview** —— 打包时 `build_release.py`
里那句

    try: __import__(pkg)
    except Exception: continue

把它静默跳过了。症状大家都知道：双击 exe 先闪一个黑框，然后浏览器打开面板，
而不是一个桌面窗口。

根因是「构建解释器」和「运行依赖」不是同一套环境：本机打包用的是系统里的
Python venv，那里没装 pywebview；而 pywebview 装在另一个隔离环境里。

为什么不直接 `pip install` 到打包用的解释器：
    那会把依赖混进用户自己的项目环境，换台机器又要重来一遍。
    buildtools/ 本来就是"构建专用第三方目录"（PyInstaller 就住这儿），
    把 pywebview 也放进来，任何解释器来打包都能用同一份。

为什么不用 scripts/minipip.py：
    pywebview 的 metadata 里写了 `pyobjc-core`（macOS 才有），minipip 的
    朴素依赖解析不做环境标记过滤，直接判定"镜像里没有适配当前解释器的
    wheel"然后整体失败。这里改成按已知清单复制，反而更可靠。

用法
----
    python scripts/bootstrap_buildtools.py
    python scripts/bootstrap_buildtools.py --python <某个装了 pywebview 的 python>
    python scripts/bootstrap_buildtools.py --check        # 只检查，不动文件
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "buildtools"

# 原生窗口要用到的东西。webview 是壳，pythonnet/clr_loader/cffi 是它
# 在 Windows 上调 WinForms 的那条链，bottle 是 pywebview 内部起的本地服务。
PACKAGES = [
    ("webview", "webview"),
    ("pythonnet", "pythonnet"),
    ("clr_loader", "clr_loader"),
    ("cffi", "cffi"),
    ("pycparser", "pycparser"),
    ("proxy_tools", "proxy_tools"),
]

# 单文件包（非目录）
MODULES = ["bottle.py", "typing_extensions.py"]

# 带 ABI 后缀的扩展模块，按解释器版本挑
EXT_GLOBS = ["_cffi_backend.*.pyd"]


def site_packages_of(python: str) -> str:
    """问一个解释器：你的 site-packages 在哪。"""
    code = "import sysconfig;print(sysconfig.get_paths()['purelib'])"
    out = subprocess.run([python, "-c", code], capture_output=True, text=True)
    return (out.stdout or "").strip()


def candidates() -> list:
    """可能装着 pywebview 的解释器，按可信度排序。"""
    items = []
    # 0. 上次成功用过的那个（写在这里，省得每次再指一遍）
    memo = ROOT / "build" / ".buildpython"
    try:
        remembered = memo.read_text(encoding="utf-8").strip()
        if remembered:
            items.append(remembered)
    except OSError:
        pass
    # 1. 环境变量显式指定
    import os
    env = os.environ.get("JILIU_BUILD_PYTHON")
    if env:
        items.insert(0, env)
    # 2. 当前解释器
    items.append(sys.executable)
    # 3. 项目内虚拟环境
    for name in (".venv", "venv"):
        p = ROOT / name / "Scripts" / "python.exe"
        if p.is_file():
            items.append(str(p))
    return items


def remember(python: str) -> None:
    """记住这次用的解释器，下次自动复用。"""
    try:
        memo = ROOT / "build" / ".buildpython"
        memo.parent.mkdir(parents=True, exist_ok=True)
        memo.write_text(python, encoding="utf-8")
    except OSError:
        pass


def find_source(explicit: str | None) -> tuple[str, str]:
    """返回 (site-packages 路径, 来源说明)。"""
    for py in ([explicit] if explicit else candidates()):
        if not py or not pathlib.Path(py).is_file():
            continue
        sp = site_packages_of(py)
        if sp and (pathlib.Path(sp) / "webview").is_dir():
            return sp, py
    return "", ""


def copy_tree(src: pathlib.Path, dst: pathlib.Path) -> int:
    """复制目录，跳过 __pycache__。返回文件数。"""
    n = 0
    for p in src.rglob("*"):
        if p.is_dir() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="把 pywebview 运行时搬进 buildtools")
    ap.add_argument("--python", help="从哪个解释器取包（默认自动探测）")
    ap.add_argument("--check", action="store_true", help="只检查，不复制")
    args = ap.parse_args()

    sp, source = find_source(args.python)
    if not sp:
        print("  找不到装着 pywebview 的解释器。")
        print("  先在一个环境里装好：  <python> -m pip install pywebview")
        print("  再用 --python 指过来：  python scripts/bootstrap_buildtools.py --python <python>")
        return 1

    print(f"  来源 {sp}")
    print(f"  解释器 {source}")
    print(f"  目标 {TARGET}")
    remember(source)

    have = TARGET / "webview" / "__init__.py"
    if args.check:
        print("  检查结果：" + ("已就位" if have.is_file() else "缺失"))
        return 0 if have.is_file() else 1

    TARGET.mkdir(parents=True, exist_ok=True)
    total = 0
    for name, rel in PACKAGES:
        src = pathlib.Path(sp) / name
        if not src.is_dir():
            print(f"  -- 跳过 {name}（来源里没有）")
            continue
        n = copy_tree(src, TARGET / rel)
        total += n
        print(f"  OK {name:<14} {n} 个文件")

    for mod in MODULES:
        src = pathlib.Path(sp) / mod
        if src.is_file():
            shutil.copy2(src, TARGET / mod)
            total += 1
            print(f"  OK {mod}")

    for pattern in EXT_GLOBS:
        for src in pathlib.Path(sp).glob(pattern):
            shutil.copy2(src, TARGET / src.name)
            total += 1
            print(f"  OK {src.name}")

    # dist-info 也要带上：PyInstaller 的 hook 靠它定位版本与文件清单
    for entry in pathlib.Path(sp).glob("*.dist-info"):
        if entry.name.split("-")[0].lower() in {n for n, _ in PACKAGES} | {"pywebview"}:
            copy_tree(entry, TARGET / entry.name)
            print(f"  OK {entry.name}")

    print(f"\n  共 {total} 个文件。校验：")
    sys.path.insert(0, str(TARGET))
    for mod in ("webview", "clr_loader", "bottle"):
        try:
            __import__(mod)
            print(f"  OK  import {mod}")
        except Exception as exc:  # noqa: BLE001
            print(f"  !!  import {mod} 失败：{type(exc).__name__}: {exc}")
            return 1
    print("  原生窗口运行时已就位，可以打包了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
