#!/usr/bin/env python3
"""minipip —— 一个够用就行的 wheel 安装器（零依赖，只用标准库）。

为什么要自己写一个：
    某些受限环境里 pip 的 HTTP 走不通（代理 / 证书 / 沙箱过滤），但
    裸的 urllib 能通。这时候要装几个纯 Python 的包，与其干瞪眼，
    不如自己从 PyPI 镜像把 wheel 拉下来解开 —— wheel 本质上就是个 zip。

能力边界（故意的，别当通用 pip 用）：
    * 只装 wheel，不处理 sdist（要编译的场景请用真 pip）
    * 依赖解析是最朴素的「按 Requires-Dist 递归」，不回溯、不解冲突
    * 不认识 extras 组（带 extra 标记的依赖会被跳过）

用法：
    python scripts/minipip.py --target buildtools pyinstaller
    python scripts/minipip.py --list pyinstaller          # 只看有哪些版本
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from typing import Dict, List, Optional, Tuple

DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 当前解释器能装的标签
def _tags() -> Tuple[str, List[str], str]:
    ver = f"{sys.version_info.major}{sys.version_info.minor}"
    pys = [f"cp{ver}", "py3", f"py{ver}", "py2.py3"]
    if os.name == "nt":
        import platform
        mach = platform.machine().lower()
        plat = "win_amd64" if mach in ("amd64", "x86_64") else ("win_arm64" if "arm" in mach else "win32")
        plats = [plat, "any"]
    else:
        plats = ["any"]
    return ver, pys, plats and plats[0]


def norm(name: str) -> str:
    """PEP 503 归一化。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


class Index:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self._cache: Dict[str, str] = {}

    def page(self, project: str) -> str:
        if project in self._cache:
            return self._cache[project]
        url = f"{self.base}/{norm(project)}/"
        req = urllib.request.Request(url, headers={"User-Agent": "minipip/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "replace")
        self._cache[project] = body
        return body

    def links(self, project: str) -> List[Tuple[str, str]]:
        """返回 [(文件名, 绝对URL), ...]。"""
        body = self.page(project)
        out: List[Tuple[str, str]] = []
        for m in re.finditer(r'<a\s[^>]*href="([^"]+)"[^>]*>([^<]*)</a>', body):
            href, text = m.group(1), m.group(2).strip()
            name = text or os.path.basename(urllib.parse.urlparse(href).path)
            if not name.endswith(".whl"):
                continue
            url = urllib.parse.urljoin(f"{self.base}/{norm(project)}/", href)
            out.append((name, url))
        return out


_WHEEL_RE = re.compile(r"^(?P<name>[^-]+)-(?P<ver>[^-]+)(?:-(?P<build>\d[^-]*))?-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$")


def pick_wheel(links: List[Tuple[str, str]], want_version: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """在候选里挑一个当前解释器能装的、版本最高的。"""
    _, pys, plat = _tags()
    cands = []
    for name, url in links:
        m = _WHEEL_RE.match(name)
        if not m:
            continue
        ptag, atag, plattag = m.group("py"), m.group("abi"), m.group("plat")
        py_ok = any(p in ptag.split(".") for p in pys)
        plat_ok = plattag == "any" or plattag == plat
        if not (py_ok and plat_ok):
            continue
        if want_version and m.group("ver") != want_version:
            continue
        cands.append((_verkey(m.group("ver")), name, url))
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1], cands[0][2]


def _verkey(v: str) -> tuple:
    parts = []
    for chunk in re.split(r"[.\-+]", v):
        num = re.match(r"\d+", chunk)
        parts.append((1, int(num.group())) if num else (0, chunk))
    return tuple(parts)


def metadata_of(zf: zipfile.ZipFile) -> Dict[str, object]:
    """从 wheel 里读 METADATA，拿到名字/版本/依赖。"""
    name = ver = ""
    reqs: List[str] = []
    for n in zf.namelist():
        if n.endswith(".dist-info/METADATA"):
            text = zf.read(n).decode("utf-8", "replace")
            for line in text.splitlines():
                if line.startswith("Name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("Version:"):
                    ver = line.split(":", 1)[1].strip()
                elif line.startswith("Requires-Dist:"):
                    reqs.append(line.split(":", 1)[1].strip())
            break
    return {"name": name, "version": ver, "requires": reqs}


def parse_req(spec: str) -> Optional[str]:
    """把 'urllib3 (<3,>=1.26)' 剥成项目名；带 extra 标记的返回 None。"""
    if ";" in spec:
        marker = spec.split(";", 1)[1]
        if "extra" in marker:
            return None
        spec = spec.split(";", 1)[0]
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return m.group(1) if m else None


def already_present(name: str, target: str) -> bool:
    d = os.path.join(target, norm(name).replace("-", "_"))
    if os.path.isdir(d):
        return True
    for entry in os.listdir(target):
        if entry.endswith(".dist-info") and entry.lower().startswith(norm(name).replace("-", "_") + "-"):
            return True
    return False


def install_one(idx: Index, project: str, target: str, seen: set,
                version: Optional[str] = None) -> List[str]:
    """装一个包，递归装它的依赖。返回安装清单。"""
    key = norm(project)
    if key in seen:
        return []
    if already_present(project, target):
        seen.add(key)
        return [f"{project} (已存在)"]
    seen.add(key)

    picked = pick_wheel(idx.links(project), version)
    if not picked:
        raise RuntimeError(f"{project}: 镜像里没有适配当前解释器的 wheel"
                           f"（python {sys.version_info.major}.{sys.version_info.minor} / {_tags()[2]}）")
    fname, url = picked
    req = urllib.request.Request(url, headers={"User-Agent": "minipip/1.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        blob = r.read()

    zf = zipfile.ZipFile(io.BytesIO(blob))
    meta = metadata_of(zf)
    zf.extractall(target)

    done = [f"{meta['name'] or project} {meta['version']}  <- {fname}"]
    for spec in meta["requires"]:  # type: ignore[union-attr]
        dep = parse_req(spec)  # type: ignore[arg-type]
        if dep and not already_present(dep, target):
            done += install_one(idx, dep, target, seen)
    return done


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="minipip —— 零依赖 wheel 安装器")
    ap.add_argument("packages", nargs="+", help="要装的包名")
    ap.add_argument("--target", default="vendor", help="装到哪个目录")
    ap.add_argument("--index", default=DEFAULT_INDEX, help="PyPI 镜像地址")
    ap.add_argument("--version", default=None, help="指定版本")
    ap.add_argument("--list", action="store_true", help="只列出可用版本，不安装")
    args = ap.parse_args(argv)

    idx = Index(args.index)
    if args.list:
        for p in args.packages:
            links = idx.links(p)
            vers = sorted({_WHEEL_RE.match(n).group("ver")  # type: ignore[union-attr]
                           for n, _ in links if _WHEEL_RE.match(n)}, key=_verkey)
            print(f"{p}: {len(links)} 个 wheel，版本 {vers[-8:] if vers else '无'}")
        return 0

    os.makedirs(args.target, exist_ok=True)
    problems: List[str] = []
    for p in args.packages:
        try:
            for line in install_one(idx, p, args.target, set(), args.version):
                print("  +", line)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{p}: {exc}")
            print("  !", p, "失败:", exc)

    print(json.dumps({"target": os.path.abspath(args.target),
                      "failed": problems}, ensure_ascii=False))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
