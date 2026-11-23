# -*- coding: utf-8 -*-
"""把本地 dist/ 的产物推到 GitHub Releases。

用法：
    set GH_TOKEN=ghp_xxx
    python scripts/release_github.py --repo owner/name --version 1.9.6

行为：
    1. 校验 token 与仓库写权限；
    2. 已存在同 tag 的 Release 就更新说明，不存在就新建（顺带创建 tag）；
    3. 逐个上传资产，已存在同名资产则跳过（加 --force 覆盖重传）；
    4. 打印最终地址。

不依赖 gh / PyGithub，只用标准库。走直连，不吃 HTTP_PROXY 的环境变量
（本机代理会劫持 127.0.0.1 且对上传大文件不友好）。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method: str, url: str, token: str, payload=None, *, raw=None,
         ctype="application/json", timeout=60):
    data = None
    if raw is not None:
        data = raw
        ctype = ctype or "application/octet-stream"
    elif payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "jiliu-release-script")
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body.decode("utf-8")) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"message": body[:400]}
        return e.code, parsed


def upload_asset(owner: str, repo: str, rid: int, token: str, path: pathlib.Path):
    size = path.stat().st_size
    print(f"  上传 {path.name}（{size / 1048576:.1f} MB）…", end="", flush=True)
    t0 = time.time()
    url = f"{UPLOADS}/repos/{owner}/{repo}/releases/{rid}/assets?name={urllib.parse.quote(path.name)}"
    code, body = call("POST", url, token, raw=path.read_bytes(),
                      ctype="application/octet-stream", timeout=900)
    if code == 201:
        print(f" 完成（{time.time() - t0:.0f}s）  {body.get('browser_download_url', '')}")
        return True
    print(f" 失败 {code}: {body.get('message')}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--version", required=True, help="例如 1.9.6")
    ap.add_argument("--body", default="", help="Release 正文文件，默认 dist/_release-body-<ver>.md")
    ap.add_argument("--assets", default="", help="逗号分隔的资产路径，默认标准三件套 + 校验清单")
    ap.add_argument("--draft", action="store_true")
    ap.add_argument("--prerelease", action="store_true")
    ap.add_argument("--force", action="store_true", help="同名资产已存在时删掉重传")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        print("缺少 GH_TOKEN 环境变量", file=sys.stderr)
        return 2

    root = pathlib.Path(__file__).resolve().parent.parent
    dist = root / "dist"
    ver = args.version
    owner, _, repo = args.repo.partition("/")
    tag = f"v{ver}"

    body_path = pathlib.Path(args.body) if args.body else dist / f"_release-body-{ver}.md"
    if not body_path.exists():
        print(f"找不到 Release 正文：{body_path}", file=sys.stderr)
        return 2
    body = body_path.read_text(encoding="utf-8")

    if args.assets:
        assets = [pathlib.Path(p.strip()) for p in args.assets.split(",") if p.strip()]
    else:
        assets = [
            dist / f"jiliu-portable-{ver}.zip",
            dist / f"jiliu-{ver}.exe",
            dist / f"jiliu-extension-{ver}.zip",
            dist / f"SHA256SUMS-{ver}.txt",
        ]
    missing = [str(a) for a in assets if not a.exists()]
    if missing:
        print("缺少资产：" + ", ".join(missing), file=sys.stderr)
        return 2

    # 1. 身份与权限
    code, me = call("GET", f"{API}/user", token)
    if code != 200:
        print(f"token 无效：{code} {me.get('message')}", file=sys.stderr)
        return 3
    print(f"token 身份：{me.get('login')}")

    code, info = call("GET", f"{API}/repos/{owner}/{repo}", token)
    if code != 200:
        print(f"仓库不可访问：{code} {info.get('message')}", file=sys.stderr)
        return 3
    perms = info.get("permissions") or {}
    print(f"仓库：{info.get('full_name')}  私有={info.get('private')}  "
          f"push={perms.get('push')}")
    if not perms.get("push"):
        print("这个 token 对该仓库没有写权限", file=sys.stderr)
        return 3

    # 2. Release
    code, existing = call("GET", f"{API}/repos/{owner}/{repo}/releases/tags/{tag}", token)
    if code == 200:
        rid = existing["id"]
        print(f"Release {tag} 已存在（id={rid}），更新说明")
        if not args.dry_run:
            code, rel = call("PATCH", f"{API}/repos/{owner}/{repo}/releases/{rid}",
                             token, {"name": f"汲流 Jiliu {ver}", "body": body})
            if code != 200:
                print(f"更新失败 {code}: {rel.get('message')}", file=sys.stderr)
                return 4
    else:
        print(f"创建 Release {tag}")
        if args.dry_run:
            rid = 0
        else:
            code, rel = call("POST", f"{API}/repos/{owner}/{repo}/releases", token, {
                "tag_name": tag,
                "target_commitish": "main",
                "name": f"汲流 Jiliu {ver}",
                "body": body,
                "draft": args.draft,
                "prerelease": args.prerelease,
            })
            if code not in (200, 201):
                print(f"创建失败 {code}: {rel.get('message')}", file=sys.stderr)
                return 4
            rid = rel["id"]
            print(f"  已创建：{rel.get('html_url')}")

    # 3. 资产
    code, have = call("GET", f"{API}/repos/{owner}/{repo}/releases/{rid}/assets?per_page=100", token)
    have = have if isinstance(have, list) else []
    by_name = {a["name"]: a for a in have}

    ok = True
    for a in assets:
        if a.name in by_name and not args.force:
            print(f"  跳过 {a.name}（已存在）")
            continue
        if a.name in by_name and args.force:
            if args.dry_run:
                print(f"  将删除并重传 {a.name}")
            else:
                call("DELETE", f"{API}/repos/{owner}/{repo}/releases/assets/{by_name[a.name]['id']}", token)
        if args.dry_run:
            print(f"  将上传 {a.name}（{a.stat().st_size / 1048576:.1f} MB）")
            continue
        ok = upload_asset(owner, repo, rid, token, a) and ok

    print()
    if args.dry_run:
        print("[dry-run] 未做任何修改")
    else:
        print(f"完成：https://github.com/{owner}/{repo}/releases/tag/{tag}")
    return 0 if ok else 5


if __name__ == "__main__":
    sys.exit(main())
