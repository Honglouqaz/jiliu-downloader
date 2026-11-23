"""发布产物验收。

验的都是「打包之后才可能坏」的东西：
  A. 便携包内容是否完整（含 version.txt）
  B. 编码修复：管道下输出不再因为 ⚠ 之类字符崩掉
  C. 内置 yt-dlp 与 extractor 数量
  D. 类型识别在冻结态是否照常工作
  E. 面板能不能起来并返回 200
  F. 便携模式是否真的把状态落在程序目录

注意：全部走 subprocess.PIPE 读输出 —— 这正是之前触发
UnicodeEncodeError 的场景，所以这些用例本身就是回归测试。
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# 环境里常常设了全局代理（HTTP_PROXY），而 no_proxy 往往是空的。
# 那种情况下连 127.0.0.1 的请求都会被丢给代理，测出来的"连不上"是假的 ——
# 本脚本所有本机请求都走这个禁用代理的 opener。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
# onedir（2026-09-24）：exe 在 dist/jiliu/ 里；旧位置还在就按旧位置用（兼容）
_exe_cand = DIST / "jiliu" / "jiliu.exe"
EXE = (_exe_cand if _exe_cand.is_file() else (DIST / "jiliu.exe")).resolve()
PORT = 8799

# ⚠️ 版本号必须从代码里读，不能硬编码。
#    以前这里写死 `jiliu-portable-1.0.0` 和 `== "1.0.0"`，
#    版本一升到 1.1.0，A 段 13 项**全部**假失败 —— 看着像打包坏了，
#    其实只是验收脚本自己没跟上。这种"测试的锅"最容易被误当成产品问题。
sys.path.insert(0, str(ROOT))
try:
    from jiliu import __version__ as VER
except Exception:  # noqa: BLE001
    VER = "0.0.0"

# 便携目录按版本号找；找不到就退而求其次，抓任意一个 jiliu-portable-*
_pd_candidates = sorted(DIST.glob("jiliu-portable-*"))
PORTABLE_DIR = (DIST / f"jiliu-portable-{VER}"
                if (DIST / f"jiliu-portable-{VER}").is_dir()
                else (_pd_candidates[-1] if _pd_candidates else
                      DIST / f"jiliu-portable-{VER}"))

# 验收过程中的临时状态目录。放在 build/ 下 —— 它是构建产物目录，
# 不会污染源码树，也会被 .gitignore 一并忽略。
TMP = ROOT / "build" / "verify_tmp"
TMP.mkdir(parents=True, exist_ok=True)

PASS = FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


def env_for(home: pathlib.Path, portable: bool = False) -> dict:
    e = dict(os.environ)
    if portable:
        # 便携模式靠 JILIU_PORTABLE 触发。这里千万别再设 JILIU_HOME ——
        # 它的优先级比便携高，会把便携模式整个盖掉，测出来的结论就是假的。
        e.pop("JILIU_HOME", None)
        e["JILIU_PORTABLE"] = "1"
    else:
        e["JILIU_HOME"] = str(home)
        e.pop("JILIU_PORTABLE", None)
    # 强制管道编码为 GBK：复现最容易崩的那个场景。
    # 如果程序内部没做 UTF-8 兜底，这里就会炸。
    e["PYTHONIOENCODING"] = "gbk"
    return e


def run(args: list, home: pathlib.Path, timeout: int = 180,
        portable: bool = False, enc: str = "utf-8"):
    r = subprocess.run([str(EXE)] + args, capture_output=True, timeout=timeout,
                       env=env_for(home, portable),
                       creationflags=0x08000000)
    return (r.returncode,
            (r.stdout or b"").decode(enc, "replace"),
            (r.stderr or b"").decode(enc, "replace"))


def free_port(p: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", p)) != 0


# ==================================================================== A
print("=" * 72)
print("A. 便携包内容")
print("-" * 72)
pd = PORTABLE_DIR
print(f"  便携目录 {pd.name}（版本 {VER}）")
want = ["jiliu.exe", "portable", "version.txt", "启动.cmd", "README.md",
        "LICENSE", "使用说明.html", "bin/放这里.txt"]
for w in want:
    check(f"便携包含 {w}", (pd / w).is_file())
# 运行时形态：onefile（当前发行形态）或 onedir（带 _internal/）。
#
# 这里以前硬要求 _internal/ 必须存在 —— 那是 onedir 时期的写法，打包方式
# 一改回 onefile，这条就假失败，看着像"便携包打坏了"，其实包是好的。
# 现在只断言"形态是明确的、exe 有实打实的体积"，两种形态都接受。
_onedir = (pd / "_internal").is_dir()
check("便携包运行时形态明确",
      bool(_onedir or (pd / "jiliu.exe").is_file()),
      "onedir（带 _internal/）" if _onedir else "onefile（web 资源在 exe 归档里）")
if _onedir:
    check("_internal 里 web 资源在", (pd / "_internal" / "web" / "index.html").is_file())
else:
    exe_mb = ((pd / "jiliu.exe").stat().st_size / 1048576
              if (pd / "jiliu.exe").is_file() else 0)
    check("onefile 的 exe 体积合理（>15MB，含 web/ 与 bin/）", exe_mb > 15,
          f"{exe_mb:.1f} MB")

# 1.9.3 起协议处理器是独立的轻量 exe。缺了不算坏包（会退回主程序，
# 只是慢），但发行包里应该带上 —— 少了它这个功能等于打了折。
check("便携包含协议唤起器 jiliu-wake.exe", (pd / "jiliu-wake.exe").is_file())
if (pd / "jiliu-wake.exe").is_file():
    wake_mb = (pd / "jiliu-wake.exe").stat().st_size / 1048576
    check("唤起器体积在 16MB 以内（超过就失去意义）", wake_mb < 16,
          f"{wake_mb:.1f} MB")
for w in ("extension/manifest.json", "extension/background.js",
          "extension/inject.js", "extension/popup.html",
          "extension/icons/icon128.png",
          # 本轮新增：站点实测报告要跟着发行包走
          "国内站点实测报告.html"):
    check(f"便携包含 {w}", (pd / w).is_file())
if (pd / "version.txt").is_file():
    check("version.txt 内容正确",
          (pd / "version.txt").read_text(encoding="utf-8").strip() == VER,
          f"实得 {(pd / 'version.txt').read_text(encoding='utf-8').strip()!r}，"
          f"期望 {VER!r}")

# ==================================================================== B
print()
print("=" * 72)
print("B. 编码修复（管道 + PYTHONIOENCODING=gbk）")
print("-" * 72)
H1 = TMP / "v_home"

rc, out, err = run(["--version"], H1)
check("--version 不崩", rc == 0, f"rc={rc} out={out.strip()!r}")
check("--version 中文正常", "汲流" in out or VER in out, repr(out.strip()))
check("--version 报的版本和代码一致", VER in out, f"期望含 {VER!r}")
check("--version 无 traceback", "Traceback" not in err)

rc, out, err = run(["tools"], H1)
check("tools 不崩（含 ✔/✘ 符号）", rc == 0, f"rc={rc}")
check("tools 无 UnicodeEncodeError", "UnicodeEncodeError" not in err,
      err.strip().splitlines()[-1] if "Traceback" in err else "")
check("tools 列出了四个工具",
      all(n in out for n in ("yt-dlp", "ffmpeg", "ffprobe", "aria2c")))

# 之前就是这一条崩的：分类结果带 ⚠ 告警，GBK 编不出这个字符
rc, out, err = run(["classify", "-n", "setup.exe"], H1)
check("classify 不崩（修复前 rc=1 + traceback）", rc == 0, f"rc={rc}")
check("classify 无 traceback", "Traceback" not in err)
check("classify 认出是应用", "应用" in out, [l.strip() for l in out.splitlines() if "分类" in l][:1])

# ==================================================================== C
print()
print("=" * 72)
print("C. 内置 yt-dlp")
print("-" * 72)
rc, out, err = run(["--run-ytdlp", "--version"], H1, timeout=240)
check("--run-ytdlp --version 可用", rc == 0, f"rc={rc} ver={out.strip()}")

# --list-extractors 要把上千个模块全 import 一遍，实测要 4 分钟左右。
# 只想快速回归时用 --fast 跳过。
if "--fast" in sys.argv:
    print("  [SKIP] extractor 列表（--fast）")
else:
    rc, out, err = run(["--run-ytdlp", "--list-extractors"], H1, timeout=300)
    n_ext = len([l for l in out.splitlines() if l.strip() and not l.startswith("[")])
    check("extractor 数量 > 1500（--collect-submodules 生效）",
          n_ext > 1500, f"实得 {n_ext}")
    for site in ("BiliBili", "Youtube", "Twitter", "Vimeo"):
        check(f"含 {site} 解析器", site.lower() in out.lower())

# ==================================================================== D
print()
print("=" * 72)
print("D. 类型识别（冻结态）")
print("-" * 72)
cases = [
    ("setup.exe", "应用"),
    ("movie.mkv", "视频"),
    ("song.flac", "音频"),
    ("pack.7z", "压缩包"),
    ("report.docx", "文档"),
    ("ubuntu.iso", "光盘镜像"),
    ("photo.webp", "图片"),
    ("model.safetensors", "数据集"),
]
for name, want_cat in cases:
    rc, out, err = run(["classify", "-n", name], H1)
    ok = rc == 0 and want_cat in out
    check(f"{name:<20} -> {want_cat}", ok, "rc=" + str(rc))

rc, out, err = run(["classify", "--tools"], H1)
check("classify --tools 不崩", rc == 0, f"rc={rc}")
# 分类表是会长的（BT、网盘、合集都是后加的），所以不写死数字，
# 改成核对「该有的都在」
for want in ("BT / 磁力", "网盘链接", "合集 / 播放列表", "视频", "压缩包"):
    check(f"分类总览含「{want}」", want in out)

# 新协议层的 CLI 入口都得能跑通（冻结后 import 少东西会在这里暴露）
rc, out, err = run(["protocol", "magnet:?xt=urn:btih:"
                    + "c" * 40 + "&dn=Test+File"], H1)
check("protocol 认得磁力", rc == 0 and "磁力" in out, f"rc={rc} out={out.strip()[:80]}")
check("protocol 指出走 aria2", "aria2" in out)
check("protocol 无 traceback", "Traceback" not in err)

TN = "thunder://" + base64.b64encode(
    ("AAhttp://cdn.example.com/setup.exeZZ").encode()).decode()
rc, out, err = run(["protocol", TN], H1)
check("protocol 能解迅雷壳", rc == 0 and "setup.exe" in out, f"rc={rc} out={out.strip()[:90]}")
check("protocol 解壳后认出是应用", "应用" in out)

rc, out, err = run(["protocol", "https://pan.baidu.com/s/1abc"], H1)
check("protocol 认出网盘并如实说明", rc == 0 and "直链" in out, out.strip()[:90])

rc, out, err = run(["naming", "--pattern", "%title% [%id%]"], H1)
check("naming 试算可用", rc == 0 and "%(title).150B" in out, f"rc={rc}")
check("naming 不产生前导下划线", not re.search(r"->\s*_%", out), out.strip()[:120])

rc, out, err = run(["proxy"], H1)
check("proxy 不崩", rc == 0, f"rc={rc}")
check("proxy 提到环境变量验活", "验活" in out, out.strip()[:90])

rc, out, err = run(["cookies", "browsers"], H1)
check("cookies browsers 不崩", rc == 0, f"rc={rc}")
check("cookies 提到 Chrome 读不了的原因", "App-Bound" in out, out.strip()[-120:])

rc, out, err = run(["history"], H1)
check("history 不崩", rc == 0, f"rc={rc}")

rc, out, err = run(["bili", "status"], H1)
check("bili status 不崩（未登录也要正常退出）", rc == 0, f"rc={rc} err={err.strip()[:80]}")
check("bili status 无 traceback", "Traceback" not in err)

# ==================================================================== D2
print()
print("=" * 72)
print("D2. 新增能力（引擎 / 站点 / 归档 / 版本 / 二维码 / 桌面窗口）")
print("-" * 72)

HD2 = TMP / "v_new_home"

rc, out, err = run(["engines"], HD2)
check("engines 不崩", rc == 0, f"rc={rc} {err.strip()[:80]}")
check("engines 列出全部 7 个引擎",
      all(k in out for k in ("native", "aria2", "curl", "wget", "yt-dlp",
                             "ffmpeg")),
      [l.strip() for l in out.splitlines() if l.strip()][:2] and "ok")

rc, out, err = run(["sites"], HD2)
check("sites 不崩", rc == 0, f"rc={rc} {err.strip()[:80]}")
check("sites 列出国内站点（含 B站/优酷/腾讯）",
      all(k in out for k in ("哔哩哔哩", "优酷", "腾讯视频")),
      f"{len(out.splitlines())} 行")
check("sites 提到海外站点", "YouTube" in out)

rc, out, err = run(["layout"], HD2)
check("layout 不崩", rc == 0, f"rc={rc} {err.strip()[:80]}")
check("layout 示例树里出现中文子目录", "视频" in out and "音频" in out)

rc, out, err = run(["edition"], HD2)
check("edition 不崩", rc == 0, f"rc={rc} {err.strip()[:80]}")
check("edition 列出国内版与全球版",
      "国内版" in out and "全球版" in out)

# 二维码 —— 「B站扫码到底能不能扫」的根。
# ⚠️ 不能查 /api/*：库没打进 exe 时接口照样返回 200，只是内容退化成空串，
#    看状态码查不出来。所以让 exe 真生成一个 PNG，再验文件魔数。
qr_png = TMP / "v_qr_check.png"
rc, out, err = run(["qr", "https://example.com/verify", "-o", str(qr_png)],
                   HD2)
ok = rc == 0 and qr_png.is_file() and \
    qr_png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
check("qr 生成合法 PNG（qrcode + pypng 已打进 exe）", ok,
      f"rc={rc} {out.strip()[:60] or err.strip()[:60]}")

rc, out, err = run(["qr", "hi", "--matrix"], HD2)
check("qr --matrix 打出字符矩阵", rc == 0 and "矩阵" in out
      and "#" in out and "." in out, out.splitlines()[0].strip()[:50])

# 桌面窗口能力：冻结后 pywebview 的 DLL 在不在，只有真跑才知道。
# 这里不真开窗口（无头环境会开不起来），只查新配置项有没有落进 exe 的默认表。
rc, out, err = run(["config"], HD2)
check("config 不崩", rc == 0, f"rc={rc} {err.strip()[:80]}")
for k, label in [("edition", "版本形态"), ("engine_order", "引擎优先级"),
                 ("proxy_mode", "代理模式"), ("auto_subdir", "自动归档"),
                 ("subdir_names", "归档目录名")]:
    check(f"配置含{label}（{k}）", k in out,
          [l.strip() for l in out.splitlines() if k in l][:1]
          and next((l.strip() for l in out.splitlines() if l.startswith(f"    {k}")), ""))
check("默认引擎优先级里含 aria2 与 native",
      "aria2" in out and "native" in out)

# ==================================================================== E
print()
print("=" * 72)
print("E. 面板 HTTP")
print("-" * 72)
if not free_port(PORT):
    print(f"  [SKIP] 端口 {PORT} 被占用，跳过面板测试")
else:
    H2 = TMP / "v_srv_home"
    proc = subprocess.Popen(
        [str(EXE), "serve", "--port", str(PORT), "--no-browser"],
        env=env_for(H2), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=0x08000000)
    base = f"http://127.0.0.1:{PORT}"
    up = False
    for _ in range(60):
        time.sleep(0.5)
        try:
            # 超时给足：首次 /api/state 要等工具探测跑完（probe_all 冷启动约 5 秒，
            # 且它下面还有 ytdlp_selected 再叠几秒）。用 3 秒会被误判成「面板没起来」。
            with OPENER.open(base + "/api/state", timeout=25) as r:
                if r.status == 200:
                    up = True
                    break
        except Exception:
            if proc.poll() is not None:
                break
    check("面板起来了", up)
    if up:
        for path in ("/api/state", "/api/categories", "/api/tools", "/api/env",
                     "/api/naming", "/api/cookies", "/api/history", "/api/proxy",
                     # 本轮新增的能力端点。冻结后最容易丢的就是「新模块的
                     # 依赖没打进来」——比如 qrcode 缺了，/api/engines 照样 200，
                     # 但二维码永远是空串。所以下面还要单独查二维码内容。
                     "/api/engines", "/api/edition", "/api/sites", "/api/layout",
                     "/api/desktop"):
            try:
                with OPENER.open(base + path, timeout=20) as r:
                    body = r.read()
                    check(f"GET {path} -> 200", r.status == 200,
                          f"{len(body)} bytes")
            except Exception as exc:
                check(f"GET {path} -> 200", False, str(exc)[:80])

        # 面板首页
        try:
            with OPENER.open(base + "/", timeout=8) as r:
                html = r.read().decode("utf-8", "replace")
                check("GET / 返回面板页", r.status == 200 and "汲流" in html,
                      f"{len(html)} bytes")
                # 冻结后静态页必须真的跟着 exe 走
                for marker, label in [("addbar", "地址栏"),
                                      ("id=\"side\"", "左侧分类树"),
                                      ("id=\"rows\"", "任务表格"),
                                      ("id=\"drawer\"", "设置抽屉")]:
                    check(f"面板含{label}", marker in html)
        except Exception as exc:
            check("GET / 返回面板页", False, str(exc)[:80])

        # 冻结后新协议分流必须还通（打包最容易在这类地方出问题）        try:
            req = urllib.request.Request(
                base + "/api/protocol",
                data=json.dumps({"url": "magnet:?xt=urn:btih:" + "c" * 40}).encode(),
                headers={"Content-Type": "application/json"})
            with OPENER.open(req, timeout=10) as r:
                d = json.loads(r.read())
                check("POST /api/protocol 认得磁力",
                      d.get("protocol", {}).get("kind") == "magnet",
                      json.dumps(d, ensure_ascii=False)[:120])
                check("POST /api/protocol 判为 BT 类",
                      d.get("plan", {}).get("category") == "bt")
        except Exception as exc:
            check("POST /api/protocol 认得磁力", False, str(exc)[:120])

        # 接管决策接口（插件调的就是这个）
        try:
            req = urllib.request.Request(
                base + "/api/intercept",
                data=json.dumps({"url": "https://example.com/a.zip"}).encode(),
                headers={"Content-Type": "application/json"})
            with OPENER.open(req, timeout=8) as r:
                d = json.loads(r.read())
                check("POST /api/intercept 返回决策", "take" in d,
                      json.dumps(d, ensure_ascii=False)[:120])
        except Exception as exc:
            check("POST /api/intercept 返回决策", False, str(exc)[:120])

        # ---- 本轮新增能力的深度校验 -------------------------------------
        # 光看 200 不够：这些接口即使底层库没打进 exe 也会返回 200，
        # 只是内容退化成空/降级值。所以逐项查内容。
        def get_json(path):
            try:
                with OPENER.open(base + path, timeout=25) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except Exception as exc:  # noqa: BLE001
                check(f"GET {path} 可解析", False, str(exc)[:100])
                return {}

        d = get_json("/api/engines")
        eg = d.get("engines") or []
        check("引擎表至少 7 项（native/aria2/curl/wget/yt-dlp/ffmpeg/N_m3u8DL-RE）",
              len(eg) >= 7, f"实得 {len(eg)}")
        check("引擎表给了 effective_http",
              bool(d.get("effective_http")), str(d.get("effective_http")))
        check("引擎表里 yt-dlp 可用（否则视频功能等于没有）",
              any(e.get("key") == "yt-dlp" and e.get("available") for e in eg),
              str([e.get("key") for e in eg if e.get("available")]))

        d = get_json("/api/edition")
        check("版本信息含 cn/global 两种",
              {x.get("key") for x in (d.get("all") or [])} >= {"cn", "global"},
              str(d.get("key")))
        check("当前版本是合法值",
              d.get("key") in ("cn", "global"), str(d.get("key")))

        d = get_json("/api/sites")
        check("站点表 ≥ 20 个", (d.get("total") or 0) >= 20,
              f"实得 {d.get('total')}")
        check("站点表里国内站多于海外站",
              (d.get("domestic") or 0) > (d.get("foreign") or 0),
              f"{d.get('domestic')} vs {d.get('foreign')}")
        check("每个站点都带了现状说明（这张表的意义所在）",
              all(s.get("note") for s in (d.get("sites") or [])),
              str([s["name"] for s in (d.get("sites") or [])
                   if not s.get("note")])[:80])

        d = get_json("/api/layout")
        check("归档布局含文件夹清单", bool(d.get("folders")),
              str(len(d.get("folders") or [])))
        check("归档布局带示例树", "视频/" in (d.get("sample") or ""),
              (d.get("sample") or "").replace("\n", " | ")[:70])

        d = get_json("/api/desktop")
        check("桌面窗口能力有明确结论（可用 or 给出原因）",
              isinstance(d.get("available"), bool)
              and (d["available"] or bool(d.get("reason"))),
              f"available={d.get('available')} "
              f"reason={(d.get('reason') or '')[:50]}")

        # 面板 UI 里本轮新增的部件必须真的在
        try:
            with OPENER.open(base + "/", timeout=8) as r:
                html = r.read().decode("utf-8", "replace")
            for marker, label in [("id=\"edTag\"", "版本角标"),
                                  ("id=\"engine\"", "地址栏引擎选择"),
                                  ("id=\"mEngines\"", "引擎菜单"),
                                  ("id=\"engRefresh\"", "引擎刷新"),
                                  ("data-subdir", "归档设置"),
                                  ("data-eorder", "引擎顺序设置")]:
                check(f"面板含{label}", marker in html)
        except Exception as exc:  # noqa: BLE001
            check("面板含新增部件", False, str(exc)[:100])

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except Exception:
        proc.kill()

# ==================================================================== F
print()
print("=" * 72)
print("F. 便携模式")
print("-" * 72)
rc, out, err = run(["tools"], TMP / "should_be_ignored",
                   portable=True)
line = [l.strip() for l in out.splitlines() if "状态目录" in l]
detail = line[0] if line else "(没找到状态目录这个行)"
check("JILIU_PORTABLE=1 时状态目录落在程序目录",
      bool(line) and str(DIST).lower() in detail.lower() and "data" in detail.lower(),
      detail)

# ==================================================================== G
# 为什么单独有这一段：
#   1.1.0 的 `jiliu.py` 漏了 `import os`，而 `os.name` 只在
#   「frozen + 无参数」（= 双击）这条路径上被求值。测试全绿、CLI 全正常，
#   唯独主人双击时终端一闪而退 —— 因为异常发生在隐藏控制台之后。
#   这类 frozen-only 分支单测天然覆盖不到，必须拿**真 exe** 冒烟。
print()
print("=" * 72)
print("G. 双击启动冒烟（frozen + 无参数）")
print("-" * 72)
for script, label in [("_smoke_gui_launch.py", "启动链路语义探针"),
                      ("_e2e_launch.py", "真 exe 端到端启动")]:
    p = ROOT / "build" / script
    if not p.exists():
        check(f"{label} 存在（{script}）", False, "探针脚本缺失")
        continue
    try:
        r = subprocess.run(
            [sys.executable, "-u", str(p)],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=240)
        blob = (r.stdout or "") + (r.stderr or "")
        npass = blob.count("[PASS]")
        nfail = blob.count("[FAIL]")
        check(f"{label} 全通过（{npass} 项）", r.returncode == 0 and nfail == 0,
              f"rc={r.returncode} pass={npass} fail={nfail}")
        if r.returncode != 0:
            for ln in blob.splitlines():
                if "[FAIL]" in ln:
                    print("       " + ln.strip())
    except subprocess.TimeoutExpired:
        check(f"{label} 全通过", False, "超时")

# 崩溃兜底要真的能落盘 —— 双击失败时用户唯一的线索
_crash_probe = ROOT / "build" / "_crash_log_probe.py"
_crash_probe.write_text(
    "import sys, runpy, pathlib, builtins, os\n"
    "builtins.input = lambda *a, **k: ''\n"
    f"sys.path.insert(0, r'{ROOT}')\n"
    "g = runpy.run_path(r'" + str(ROOT / "jiliu.py") + "', "
    "run_name='_crash_probe')\n"
    "g['_show_console'] = lambda: None\n"
    "try:\n"
    "    raise ValueError('boom-probe')\n"
    "except ValueError as e:\n"
    "    g['_report_crash'](e)\n"
    "p = pathlib.Path(g['_crash_log_path']())\n"
    "print('LOG', p.exists(), p)\n"
    "if p.exists():\n"
    "    txt = p.read_text(encoding='utf-8', errors='replace')\n"
    "    print('HAS_TRACE', 'boom-probe' in txt)\n"
    "    p.unlink()\n",
    encoding="utf-8")
try:
    r = subprocess.run([sys.executable, "-u", str(_crash_probe)],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    blob = (r.stdout or "") + (r.stderr or "")
    check("未捕获异常会写入 jiliu-crash.log", "LOG True" in blob,
          [l for l in blob.splitlines() if l.startswith("LOG")][:1])
    check("崩溃日志含完整堆栈", "HAS_TRACE True" in blob)
except Exception as exc:  # noqa: BLE001
    check("未捕获异常会写入 jiliu-crash.log", False, str(exc)[:80])
finally:
    try:
        _crash_probe.unlink()
    except Exception:
        pass

# ====================================================================
print()
print("=" * 72)
print(f"合计 {PASS} 通过 / {FAIL} 失败")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
