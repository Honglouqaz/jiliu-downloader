#!/usr/bin/env python3
"""端到端联调测试：识别 → 建任务 → 下载 → 校验 → HTTP API。

自包含：自己拉起测试文件服务器和汲流 API 服务，不依赖外部进程。

    python tests/test_pipeline.py
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = pathlib.Path(tempfile.gettempdir()) / "jl_pipe_src"
WORK = pathlib.Path(tempfile.gettempdir()) / "jl_pipe_work"

# 把状态目录隔离到临时位置 —— 否则测试会污染用户真实的 ~/.jiliu/tasks.json，
# 而且上一轮遗留的任务会让断言变得不可重复（必须在 import jiliu 之前设好）。
os.environ["JILIU_HOME"] = str(pathlib.Path(tempfile.gettempdir()) / "jl_pipe_home")

PORT_FILES = 8905
PORT_API = 8906
BASE = f"http://127.0.0.1:{PORT_FILES}"
API = f"http://127.0.0.1:{PORT_API}"

RESULTS = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(cond)


def head(title: str) -> None:
    print(f"\n{title}")


def wait_port(port: int, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except Exception:
            time.sleep(0.15)
    return False


def make_samples() -> None:
    """造几个带真实文件头的样本，让识别有魔数可看。"""
    SRC.mkdir(parents=True, exist_ok=True)
    blob = os.urandom(256)
    samples = {
        "setup.exe": b"MZ\x90\x00\x03" + blob,
        "app-debug.apk": b"PK\x03\x04" + struct.pack("<HHHHH", 20, 0, 0, 0, 0)
                         + struct.pack("<III", 0, 0, 0)
                         + struct.pack("<HH", len(b"AndroidManifest.xml"), 0)
                         + b"AndroidManifest.xml" + blob,
        "data.zip": b"PK\x03\x04" + struct.pack("<HHHHH", 20, 0, 0, 0, 0)
                    + struct.pack("<III", 0, 0, 0)
                    + struct.pack("<HH", len(b"readme.txt"), 0)
                    + b"readme.txt" + blob,
        "song.mp3": b"ID3\x04\x00\x00" + blob,
        "clip.mp4": struct.pack(">I", 20) + b"ftypisom" + blob,
        "photo.png": b"\x89PNG\r\n\x1a\n" + blob,
        "ubuntu.iso": b"\x00" * 40 + blob,
        "weights.safetensors": blob,
        "payload.bin": b"%PDF-1.7\n" + blob,      # 后缀撒谎，实际是 PDF
    }
    for name, data in samples.items():
        (SRC / name).write_bytes(data)


def sha_of(name: str) -> str:
    return hashlib.sha256((SRC / name).read_bytes()).hexdigest()


def api_get(path: str) -> dict:
    with urllib.request.urlopen(API + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def api_post(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API + path, data=body,
                                headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {exc.code}"}


def wait_task(tid: str, timeout: float = 60.0) -> dict:
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        last = api_get(f"/api/tasks/{tid}").get("task", {})
        if last.get("status") in ("done", "error", "cancelled"):
            return last
        time.sleep(0.2)
    return last


def main() -> int:
    # 每轮跑都从干净状态开始，保证结果可重复
    import shutil
    for d in (WORK, SRC, pathlib.Path(os.environ["JILIU_HOME"])):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    make_samples()

    # ---- 拉起测试文件服务器 ----
    srv_proc = subprocess.Popen(
        [sys.executable, str(HERE / "test_server.py"), str(SRC), str(PORT_FILES)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT),
    )
    if not wait_port(PORT_FILES):
        print("  测试文件服务器没起来")
        srv_proc.kill()
        return 1

    # ---- 拉起汲流 API 服务 ----
    from jiliu import config as jcfg, server
    cfg = dict(jcfg.load())
    cfg["download_dir"] = str(WORK)
    cfg["backend"] = "native"
    cfg["max_concurrent"] = 3
    cfg["threads"] = 8
    httpd, mgr = None, None
    try:
        httpd, mgr = server.serve(cfg, port=PORT_API, quiet=True)
    except Exception as exc:  # noqa: BLE001
        print("  API 服务没起来:", exc)
        srv_proc.kill()
        return 1
    if not wait_port(PORT_API):
        print("  API 端口未就绪")
        srv_proc.kill()
        return 1

    try:
        # ==============================================================
        head("T1  /api/classify 离线识别（不发网络请求）")
        cases = [
            ({"url": "https://x.com/a.exe"}, "app"),
            ({"url": "https://x.com/a.7z"}, "archive"),
            ({"url": "https://x.com/a.flac"}, "audio"),
            ({"url": "https://x.com/live.m3u8"}, "video"),
            ({"url": "https://x.com/ubuntu.iso"}, "disk"),
            ({"url": "https://www.bilibili.com/video/BV1xx"}, "video"),
            ({"url": "https://x.com/a.gguf"}, "dataset"),
            ({"filename": "报告.docx"}, "document"),
            ({"filename": "a.tar.gz"}, "archive"),
        ]
        for payload, want in cases:
            r = api_post("/api/classify", payload)
            got = (r.get("plan") or {}).get("category")
            check(f"T1 {payload.get('url') or payload.get('filename')} → {got}",
                  r.get("ok") and got == want, f"期望 {want}")

        r = api_post("/api/classify", {"filename": "setup.exe", "size": 4096})
        plan = r.get("plan") or {}
        check("T1.10 小体积自动降到单线程", plan.get("concurrency") == 1, str(plan.get("concurrency")))
        check("T1.11 应用类强制哈希校验", plan.get("verify_hash") is True, str(plan.get("verify_hash")))

        # ==============================================================
        head("T2  /api/inspect 联网探测 + 识别")
        r = api_post("/api/inspect", {"url": f"{BASE}/setup.exe"})
        res = r.get("result") or {}
        plan = res.get("plan") or {}
        check("T2.1 拿到了大小", res.get("size") and res["size"] > 0, str(res.get("size")))
        check("T2.2 Content-Disposition 文件名", res.get("filename") == "setup.exe",
              repr(res.get("filename")))
        check("T2.3 识别为应用", plan.get("category") == "app", str(plan.get("category")))
        check("T2.4 文件头魔数抓到 MZ", str(res.get("magic_hex", "")).startswith("4d5a"),
              str(res.get("magic_hex")))

        # 后缀撒谎的样本：payload.bin 实际是 PDF
        r = api_post("/api/inspect", {"url": f"{BASE}/payload.bin"})
        res = r.get("result") or {}
        plan = res.get("plan") or {}
        check("T2.5 后缀 .bin 但魔数是 PDF → 文档",
              plan.get("category") == "document", f"{plan.get('category')} / {plan.get('filetype')}")
        check("T2.6 后缀被魔数改写为 pdf", plan.get("ext") == "pdf", str(plan.get("ext")))

        # ZIP 二级分类
        r = api_post("/api/inspect", {"url": f"{BASE}/app-debug.apk"})
        plan = (r.get("result") or {}).get("plan") or {}
        check("T2.7 APK 识别为应用", plan.get("category") == "app", str(plan.get("category")))

        r = api_post("/api/inspect", {"url": f"{BASE}/data.zip"})
        plan = (r.get("result") or {}).get("plan") or {}
        check("T2.8 普通 zip 仍是压缩包", plan.get("category") == "archive",
              f"{plan.get('category')} / {plan.get('filetype')}")

        r = api_post("/api/inspect", {"url": f"{BASE}/weights.safetensors"})
        plan = (r.get("result") or {}).get("plan") or {}
        check("T2.9 .safetensors 后缀被识别", plan.get("category") == "dataset",
              f"{plan.get('category')} / {plan.get('ext')}")

        r = api_post("/api/inspect", {"url": f"{BASE}/ubuntu.iso"})
        plan = (r.get("result") or {}).get("plan") or {}
        check("T2.10 ISO 全零头识别为镜像", plan.get("category") == "disk", str(plan.get("category")))

        # ==============================================================
        head("T3  /api/intercept 插件接管决策")
        r = api_post("/api/intercept", {"url": f"{BASE}/payload.bin", "size": 200 * 1024})
        check("T3.1 小体积未知类型 → 放行", r.get("take") is False, r.get("reason"))

        r = api_post("/api/intercept", {"url": f"{BASE}/setup.exe", "size": 900})
        check("T3.2 exe → 接管", r.get("take") is True, r.get("reason"))

        r = api_post("/api/intercept", {"url": "blob:https://x.com/uuid"})
        check("T3.3 blob: → 不接管", r.get("take") is False, r.get("reason"))

        r = api_post("/api/intercept", {"url": "https://www.youtube.com/watch?v=abc"})
        check("T3.4 YouTube → 接管走 yt-dlp", r.get("take") and "yt-dlp" in (r.get("reason") or ""),
              r.get("reason"))

        r = api_post("/api/intercept", {"url": "https://x.com/sprite.png",
                                        "mime": "image/png", "size": 8 * 1024})
        check("T3.5 小图 → 放行", r.get("take") is False, r.get("reason"))

        # ==============================================================
        head("T4  建任务：自动分类生效")
        r = api_post("/api/tasks", {"url": f"{BASE}/setup.exe", "dest_dir": str(WORK)})
        t = r.get("task") or {}
        check("T4.1 任务创建成功", r.get("ok") and t.get("id"), str(r.get("error", "")))
        check("T4.2 自动识别为应用", t.get("category") == "app",
              f"{t.get('category')} / {t.get('category_label')}")
        check("T4.3 应用开了哈希校验", t.get("verify_hash") is True, str(t.get("verify_hash")))
        check("T4.4 meta 里存了识别结果",
              bool((t.get("meta") or {}).get("resource")),
              str((t.get("meta") or {}).get("resource", {}).get("label")))
        done = wait_task(t["id"]) if t.get("id") else {}
        check("T4.5 下载完成", done.get("status") == "done",
              f"{done.get('status')} {done.get('error', '')}")
        check("T4.6 算出了 SHA256", bool(done.get("sha256")), str(done.get("sha256"))[:20])
        check("T4.7 与源文件哈希一致", done.get("sha256") == sha_of("setup.exe"))

        # ==============================================================
        head("T5  哈希校验：正确 / 错误")
        r = api_post("/api/tasks", {
            "url": f"{BASE}/data.zip", "dest_dir": str(WORK),
            "sha256": sha_of("data.zip"), "auto_classify": False,
        })
        t = r.get("task") or {}
        done = wait_task(t["id"]) if t.get("id") else {}
        check("T5.1 正确哈希 → 完成且 hash_ok", done.get("status") == "done"
              and done.get("hash_ok") is True, f"{done.get('status')} ok={done.get('hash_ok')}")

        r = api_post("/api/tasks", {
            "url": f"{BASE}/data.zip", "dest_dir": str(WORK), "filename": "wrong-hash.zip",
            "sha256": "0" * 64, "auto_classify": False,
        })
        t2 = r.get("task") or {}
        done2 = wait_task(t2["id"]) if t2.get("id") else {}
        check("T5.2 错误哈希 → 报错且 hash_ok=False",
              done2.get("status") == "error" and done2.get("hash_ok") is False,
              f"{done2.get('status')} ok={done2.get('hash_ok')} err={str(done2.get('error'))[:60]}")

        # ==============================================================
        head("T6  面板与状态接口")
        st = api_get("/api/state")
        check("T6.1 /api/state 可用", st.get("ok") is True)
        cats = st.get("categories") or []
        # 不写死数字 —— 分类表是会长的（BT、网盘、合集都是后加的）。
        # 这里真正要验的是「接口把 resource.CATEGORIES 全带出来了」。
        from jiliu import resource as _res
        check("T6.2 带出全部分类总览", len(cats) == len(_res.CATEGORIES),
              f"{len(cats)} 个 / 表里 {len(_res.CATEGORIES)} 个")
        check("T6.3 任务列表非空", len(st.get("tasks") or []) >= 3, str(len(st.get("tasks") or [])))
        check("T6.4 任务带分类标签",
              all(x.get("category_label") for x in st.get("tasks") or []),
              str([x.get("category_label") for x in st.get("tasks") or []][:3]))

        cs = api_get("/api/categories")
        check("T6.5 /api/categories 可用", len(cs.get("categories") or []) == len(_res.CATEGORIES))

        hl = api_get("/api/health")
        check("T6.6 /api/health 可用", hl.get("ok") is True)

        # ==============================================================
        head("T7  未知长度 / 不支持 Range 的兜底")
        r = api_post("/api/tasks", {"url": f"{BASE}/photo.png", "dest_dir": str(WORK)})
        t = r.get("task") or {}
        done = wait_task(t["id"]) if t.get("id") else {}
        check("T7.1 小图片仍能下完", done.get("status") == "done",
              f"{done.get('status')} {done.get('error', '')}")
        check("T7.2 图片哈希一致（未传预期值时不算哈希）",
              (SRC / "photo.png").read_bytes() == (WORK / "photo.png").read_bytes(),
              "字节级比对")

    finally:
        try:
            if mgr:
                mgr.shutdown()
            if httpd:
                httpd.shutdown()
        except Exception:
            pass
        srv_proc.terminate()
        try:
            srv_proc.wait(timeout=5)
        except Exception:
            srv_proc.kill()

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 70)
    print(f"结果: {passed}/{total} 通过")
    print("=" * 70)
    if passed != total:
        print("\n未通过项：")
        for n, ok, d in RESULTS:
            if not ok:
                print(f"  - {n}  {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
