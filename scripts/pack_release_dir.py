#!/usr/bin/env python3
"""把 dist/ 下的 1.7.1 产物整理成一份可发布的目录。

只做「复制 + 重命名 + 写清单」，不改任何二进制。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import time
import zipfile

ROOT = pathlib.Path(r"D:\workbuddy\2026-09-08-08-48-15\jiliu-downloader")
DIST = ROOT / "dist"
VER = "1.7.1"
OUT = pathlib.Path(r"D:\workbuddy\ERA-PJ\release") / f"jiliu-{VER}"

SRC_EXCLUDE_DIRS = {"build", "dist", "downloads", "data", "__pycache__",
                    "buildtools", ".git", ".idea", "_setaside"}
SRC_EXCLUDE_SUFFIX = {".pyc", ".log", ".part", ".tmp"}


def sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_tree(src: pathlib.Path, dst: pathlib.Path) -> int:
    n = 0
    for p in src.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)
        n += 1
    return n


def make_source_zip(dst: pathlib.Path) -> pathlib.Path:
    """源码包放 source/ 子目录，和 1.7.0 的布局保持一致。"""
    name = f"jiliu-src-{VER}.zip"
    (dst / "source").mkdir(parents=True, exist_ok=True)
    zp = dst / "source" / name
    files = 0
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(ROOT.rglob("*")):
            if p.is_dir():
                continue
            rel = p.relative_to(ROOT)
            if any(part in SRC_EXCLUDE_DIRS for part in rel.parts):
                continue
            if p.suffix.lower() in SRC_EXCLUDE_SUFFIX:
                continue
            if p.name.startswith("_") and p.suffix == ".py":
                continue        # 临时探针脚本
            z.write(p, pathlib.Path(f"jiliu-{VER}") / rel)
            files += 1
    print(f"  源码包 {name}  {zp.stat().st_size/1048576:.2f} MB  {files} 个文件")
    return zp


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    print("  复制便携目录…")
    n = copy_tree(DIST / f"jiliu-portable-{VER}", OUT / f"jiliu-portable-{VER}")
    print(f"    {n} 个文件")

    print("  复制产物…")
    shutil.copy2(DIST / f"jiliu-portable-{VER}.zip", OUT / f"jiliu-portable-{VER}.zip")
    shutil.copy2(DIST / "jiliu-setup.exe", OUT / f"jiliu-setup-{VER}.exe")
    shutil.copy2(DIST / "jiliu.exe", OUT / "jiliu.exe")

    print("  打源码包…")
    src_zip = make_source_zip(OUT)

    items = [
        ("主程序", OUT / "jiliu.exe"),
        ("便携版", OUT / f"jiliu-portable-{VER}.zip"),
        ("安装版", OUT / f"jiliu-setup-{VER}.exe"),
        ("源码", src_zip),
    ]
    arts = []
    for label, p in items:
        arts.append({"name": label, "file": str(p.relative_to(OUT)).replace("\\", "/"),
                     "sha256": sha256(p), "bytes": p.stat().st_size})

    (OUT / "manifest.json").write_text(
        json.dumps({"version": VER, "artifacts": arts}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    lines = [f"汲流 v{VER} 构建清单",
             f"构建时间 {time.strftime('%Y-%m-%d %H:%M:%S')}",
             "",]
    for a in arts:
        lines.append(f"  {a['name']:<10} {a['bytes']/1048576:>7.2f} MB  {a['sha256'][:16]}")
    (OUT / "构建清单.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    sums = []
    for p in sorted(OUT.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS.txt":
            sums.append(f"{sha256(p)}  {str(p.relative_to(OUT)).replace(chr(92), '/')}")
    (OUT / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")

    print("\n" + "\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
