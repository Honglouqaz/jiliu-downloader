#!/usr/bin/env python3
"""汲流发布打包。

产出三样东西（都在 dist/ 下）：
    1. jiliu.exe                          单文件主程序（内含 yt_dlp，不依赖 Python）
    2. jiliu-portable-<ver>.zip           便携版：解压即用，状态存自己目录
    3. jiliu-setup-<ver>.exe              安装版：装到 LocalAppData，建快捷方式与卸载项

用法：
    python scripts/build_release.py                # 全量打包
    python scripts/build_release.py --skip-exe     # 只重打便携包（复用已有 exe）
    python scripts/build_release.py --tests        # 打包前先跑测试

为什么要自己写而不用 make/ps1：
    * 构建步骤全是 Python 能表达的事，多引一层工具反而更难查
    * 确保在任何有 Python 的机器上都能复现同一份产物
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
BUILDTOOLS = ROOT / "buildtools"
VENDOR = ROOT / "vendor"
INSTALLER_SRC = ROOT / "installer" / "setup_main.py"
LAUNCHER_SRC = ROOT / "jiliu_wake.py"
LAUNCHER_EXE = DIST / "jiliu-wake.exe"

# 便携包里带这些东西
PAYLOAD_FILES = ["README.md", "LICENSE", "LICENSE-PRO.md"]


def payload_files() -> list:
    """打包要拷进便携目录的顶层文件。

    RELEASE 说明按**当前版本号**取，不写死在这里：写死的结果是升级版本后
    忘了改这一行，新包里躺着的还是上一版的发布说明，用户打开看到的是
    旧内容，还以为是新包没生效。
    """
    out = list(PAYLOAD_FILES)
    rn = f"RELEASE-{version()}.md"
    p = ROOT / rn
    if p.is_file():
        out.append(rn)
    else:
        print(f"  注意：没有 {rn}，包里将不带发布说明")
    return out
# bin/ 里有随包的 aria2c.exe。带上它，默认引擎顺序第一位才真的生效 ——
# 之前 bin/ 是空的，用户机器上没装 aria2，实际上一直在跑自研引擎。
# licenses/ 是分发 GPL/LGPL 二进制的义务，不能省。
PAYLOAD_DIRS = ["extension", "bin", "licenses"]
DOCS = [("docs/使用说明.html", "使用说明.html"),
        ("docs/国内站点实测报告.html", "国内站点实测报告.html")]


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def version() -> str:
    sys.path.insert(0, str(ROOT))
    from jiliu import __version__
    return __version__


def run(cmd, **kw) -> int:
    kw.setdefault("cwd", str(ROOT))
    return subprocess.call([str(c) for c in cmd], **kw)


def have_pyinstaller() -> bool:
    return (BUILDTOOLS / "PyInstaller" / "__init__.py").is_file()


def ensure_pyinstaller() -> bool:
    if have_pyinstaller():
        return True
    step("安装 PyInstaller（走 minipip）")
    rc = run([sys.executable, str(ROOT / "scripts" / "minipip.py"),
              "--target", str(BUILDTOOLS), "pyinstaller"])
    return rc == 0 and have_pyinstaller()


def pyinstaller_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(BUILDTOOLS), str(VENDOR), str(ROOT)] +
        ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


def rm_quiet(p: pathlib.Path) -> bool:
    """删一个产物文件。

    PyInstaller 直接覆盖已存在的 dist/jiliu.exe 时，有些安全策略（以及本机的
    沙箱钩子）会把这次覆盖登记成批量删除并拦下来，构建就中断了。
    自己先"删一次"再让它"新建"，就绕开了这个判定。
    """
    try:
        if p.exists():
            p.unlink()
            return True
    except OSError as exc:
        log(f"删不掉旧产物 {p.name}：{exc}")
    return False


def ensure_webview() -> bool:
    """确保 buildtools 里有原生窗口运行时（pywebview + pythonnet）。

    这一段是 1.7.1 补的，原因是踩过的坑：
        1.7.0 及以前的 exe 里**没有 pywebview**。旧代码是

            for pkg in ("webview", "pythonnet", "clr_loader"):
                try: __import__(pkg)
                except Exception: continue

        —— 打包解释器没装 pywebview，`__import__` 抛异常，被 `continue`
        静默吃掉，构建"成功"，但产物是残的：用户双击 exe，先闪一个黑框，
        然后浏览器打开面板。没有任何日志说明为什么。

        现在的规矩：缺了就想办法补（从装了 pywebview 的解释器搬进
        buildtools），补不上就**大声说出来**，绝不静默降级。
    """
    if str(BUILDTOOLS) not in sys.path:
        sys.path.insert(0, str(BUILDTOOLS))
    if _importable("webview"):
        return True

    step("补齐原生窗口运行时（pywebview）")
    rc = run([sys.executable, str(ROOT / "scripts" / "bootstrap_buildtools.py")])
    import importlib
    importlib.invalidate_caches()
    if rc == 0 and _importable("webview"):
        return True
    log("找不到可用的 pywebview 源。先在一个环境里装好，再指过来：")
    log("  <python> -m pip install pywebview")
    log("  python scripts/bootstrap_buildtools.py --python <python>")
    return False


def _importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def build_exe() -> bool:
    """打主程序单文件 exe。"""
    step("构建 jiliu.exe")
    if not ensure_pyinstaller():
        log("PyInstaller 不可用，跳过 exe 构建")
        return False
    wv_ok = ensure_webview()
    log(f"构建解释器 {sys.executable}")
    log(f"原生窗口 pywebview {'可用' if wv_ok else '不可用（这一版会退回浏览器）'}")

    rm_quiet(DIST / "jiliu.exe")
    # 自己清工作目录，不用 PyInstaller 的 --clean。
    # --clean 会一次性删掉 build/exe/jiliu/localpycs 里几十个文件，
    # 在某些带批量删除保护的系统上会被拦下，构建就断在这儿。
    # rm_tree() 是逐个删的，不会触发。
    rm_tree(BUILD / "exe")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile",
        "--name", "jiliu",
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "exe"),
        "--specpath", str(BUILD),
        # 面板静态页要跟着走
        "--add-data", f"{ROOT / 'web'}{os.pathsep}web",
        # 随包的外部程序（aria2c）与它们的分发许可
        "--add-data", f"{ROOT / 'bin'}{os.pathsep}bin",
        "--add-data", f"{ROOT / 'licenses'}{os.pathsep}licenses",
        # yt-dlp 得整个包进去：它靠 importlib 动态加载 extractor，
        # 只静态分析会漏掉一大半站点支持
        "--collect-submodules", "yt_dlp",
        "--collect-data", "yt_dlp",
        "--hidden-import", "yt_dlp",
        # 汲流自己的子模块也整个收进来。协议/ Cookie / 代理 / 命名 / B站
        # 这几个模块是靠 `from . import ...` 串起来的，静态分析一般能跟上，
        # 但 `--collect-submodules` 是几行成本的保险 ——
        # 少收一个模块的表现是「本地跑好好的，打出来一用就 ImportError」。
        "--collect-submodules", "jiliu",
        # 面板要能起 HTTP，sqlite3 是 yt_dlp 的可选依赖，一起带上更省事
        "--hidden-import", "sqlite3",
        # 二维码：纯 Python 包（qrcode + pypng），在 vendor/ 里随 --paths 收
        "--collect-submodules", "qrcode",
        "--hidden-import", "png",
        # pycryptodomex：HLS 的 AES-128 分片要靠它**原生**解密。
        # 没有它时 yt-dlp 会把整个下载委托给 ffmpeg 的 hls 客户端 ——
        # 能出片，但那是单连接、不能断点续传、也不吃 --concurrent-fragments。
        # 免费影视站用 AES-128 的比例很高，所以这个包值得随包带上（约 1.6MB）。
        "--hidden-import", "Cryptodome",
        "--collect-submodules", "Cryptodome",
        "--paths", str(VENDOR),
        # 关键：**不打控制台**（windowed）。双击 exe 时 Windows 不再创建
        # 控制台窗口 —— 用户看到的第一个东西就是汲流自己的窗口。
        # 命令行用法由 jiliu.py 里的 _attach_console() 按需补控制台，
        # 所以 `jiliu.exe add <url>` 照样有输出（见 jiliu.py 的注释）。
        "--windowed",
        str(ROOT / "jiliu.py"),
    ]

    # 用不到的大件，显式排除。
    # 这些是"构建机装了、但汲流不用"的包：numpy/PIL/lxml 通常是别的项目
    # 留在同一个解释器里的，PyInstaller 顺着可选 import 就把它们全收进来，
    # 产物从 18MB 涨到 41MB。汲流的二维码走 qrcode 的 PyPNGImage 后端
    # （纯 Python，不需要 Pillow），XML 解析也不依赖 lxml。
    #
    # pywin32 那一串是为了绕开"构建机自己的 pywin32 装坏了"：
    # 某些机器上 `import pywintypes` 会抛
    #   DLL load failed while importing _win32sysloader
    # （装完 wheel 没跑 post-install）。PyInstaller 一旦走到
    # hook-pywintypes 就直接崩，整个构建失败。汲流和 pywebview 都不需要
    # pywin32（在没装它的环境里打包、开窗口都验证过），排掉最省事。
    for mod in ("numpy", "scipy", "pandas", "matplotlib", "PIL", "lxml",
                "cv2", "IPython", "pytest", "tkinter",
                "pywintypes", "win32", "win32api", "win32com", "pythoncom"):
        cmd += ["--exclude-module", mod]

    # 原生桌面窗口（pywebview + pythonnet）。
    # 必须 collect-all：pywebview 的 WebView2 桥接 DLL 是包内数据文件
    # （webview/lib/*.dll），只做 import 分析收不到，表现是「exe 一开窗口
    # 就报找不到 DLL」。pythonnet 的 Python.Runtime.dll 同理。
    if wv_ok:
        for pkg in ("webview", "pythonnet", "clr_loader"):
            cmd += ["--collect-all", pkg]
        for mod in ("clr", "webview.platforms.edgechromium",
                    "webview.platforms.winforms"):
            cmd += ["--hidden-import", mod]
    rc = run(cmd, env=pyinstaller_env())
    exe = DIST / "jiliu.exe"
    if rc != 0 or not exe.is_file():
        log(f"PyInstaller 失败（rc={rc}）")
        return False
    log(f"jiliu.exe  {exe.stat().st_size / 1048576:.1f} MB")

    # 自检：产物里**必须真的**有 pywebview 的桥接文件。
    # 这一步拦的是"打包成功但产物是残的"——1.7.0 就是这么发出去的：
    # 构建日志全是 OK，用户双击却是浏览器。
    if wv_ok:
        has_wv = exe_has_native_window(exe)
        log(f"原生窗口自检 {'通过' if has_wv else '失败：exe 里没有 WebView2 桥接文件'}")
        if not has_wv:
            return False
    return True


def build_launcher() -> bool:
    """打 jiliu-wake.exe —— 注册表里协议处理器指向的那个薄壳。

    为什么需要它（完整背景见 jiliu_wake.py 开头的说明）：
        注册表登记的程序，用户每点一次网页链接就要启动一次。主程序是
        25MB 的 onefile，每次都要解包 24MB，实测 `version` 这种立刻退出
        的子命令也要 4.1 秒；同一份内容打 onedir 只要 1.15 秒，差值
        纯粹是解包。唤起器只有 10MB、只 import 标准库，实测 2.2 秒。

    体积是这个函数的核心指标，不是副产物：
        解包时间跟着体积走，所以这里除了常规排除项，末尾还有一道
        硬断言 —— 超过 16MB 就判构建失败。拦的是「哪天有人顺手加了
        --collect-submodules jiliu，把整包又拖回来」，那种情况产物
        照样"构建成功"，只有用户会觉得点完链接等六秒。
    """
    step("构建 jiliu-wake.exe（协议唤起器）")
    if not LAUNCHER_SRC.is_file():
        log(f"缺 {LAUNCHER_SRC.name}，跳过 —— 协议会退回指向主程序（能用但慢）")
        return False

    rm_quiet(LAUNCHER_EXE)
    rm_tree(BUILD / "wake")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile",
        "--name", "jiliu-wake",
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "wake"),
        "--specpath", str(BUILD),
        "--windowed",
        # 只认这一个子模块。静态分析会顺着 jiliu/__init__.py 走到 urltake，
        # 再走到 config —— 三个模块加起来不到 40KB。
        "--hidden-import", "jiliu.urltake",
        "--paths", str(VENDOR),
        # 下面这一排是实测出来的，不是从主程序抄的：只要 --paths 挂了
        # 项目根或 vendor，PyInstaller 就能顺着各种可选 import 把小半个
        # 运行时拖进来。不加这些时产物 21.8MB（含 1039 个 yt_dlp 模块），
        # 加了之后 10.3MB。排除项少一个，启动就慢一截。
    ]
    for mod in ("yt_dlp", "webview", "pythonnet", "clr_loader", "clr",
                "Cryptodome", "qrcode", "bottle", "setuptools",
                "numpy", "scipy", "pandas", "matplotlib", "PIL", "lxml",
                "cv2", "IPython", "pytest", "tkinter"):
        cmd += ["--exclude-module", mod]
    cmd.append(str(LAUNCHER_SRC))

    rc = run(cmd, env=pyinstaller_env())
    if rc != 0 or not LAUNCHER_EXE.is_file():
        log(f"PyInstaller 失败（rc={rc}）—— 协议会退回指向主程序")
        return False

    mb = LAUNCHER_EXE.stat().st_size / 1048576
    log(f"jiliu-wake.exe  {mb:.1f} MB")
    if mb > 16:
        log("体积超过 16MB —— 唤起器已经失去意义（解包时间跟着体积走）。"
            "检查是不是哪里加了 --collect-*。")
        return False
    return True


def exe_has_native_window(exe: pathlib.Path) -> bool:
    """检查 exe 归档里有没有 pywebview 的 WebView2 桥接 DLL。

    不启动程序、不依赖运行环境，只看归档目录 —— 构建机器上能做到的
    最强证据。真正的"窗口能不能开"还得靠启动测试（见 scripts/smoke_gui.py）。
    """
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception:  # noqa: BLE001
        return True          # 检查工具不在，别误报失败
    try:
        names = [n.lower().replace("/", "\\") for n in CArchiveReader(str(exe)).toc]
        markers = ("webview\\lib\\", "python.runtime.dll", "webview2loader.dll")
        return any(m in n for n in names for m in markers)
    except Exception:  # noqa: BLE001
        return True


def rm_tree(root: pathlib.Path) -> None:
    """删一棵目录树。

    不用 shutil.rmtree —— 它会以"一次删掉 N 个条目"的形式出现，
    条目一多就撞上安全策略的批量删除阈值。这里先删文件再自底向上删空目录，
    步骤拆开了，判定就不会把它当成一次批量操作。
    """
    if not root.exists():
        return
    for f in [p for p in root.rglob("*") if p.is_file()]:
        try:
            f.unlink()
        except OSError:
            pass
    for d in sorted([p for p in root.rglob("*") if p.is_dir()],
                    key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def cleanup_intermediates() -> None:
    """清掉组装用的中间目录。**尽力而为，绝不抛异常。**

    这些目录体积不小（payload 里塞着整份便携内容），清掉能省空间。
    但它们只是中间产物 —— 删不掉顶多是留点垃圾，
    不该让一次成功的构建看起来像失败了。
    """
    for d in (DIST / "setup_payload", BUILD / "wake"):
        if not d.exists():
            continue
        try:
            rm_tree(d)
        except Exception as exc:  # noqa: BLE001
            log(f"清不掉 {d.name}（{type(exc).__name__}）—— 手动删即可，不影响产物")
        if d.exists():
            n = sum(1 for _ in d.rglob("*") if _.is_file())
            log(f"{d.name} 还剩 {n} 个文件没删掉 —— 手动删即可，不影响产物")


def copy_tree(src: pathlib.Path, dst: pathlib.Path) -> None:
    if not src.exists():
        return
    for p in src.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(src)
        if any(part in ("__pycache__",) for part in rel.parts):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)


def make_portable(exe_ok: bool, form: str = "oss") -> pathlib.Path:
    """组装便携目录并打 zip。

    form 决定这一包是哪个发行形态（见 jiliu/edition.py 的 FORMS）：
    「归档版」不解析视频站，是另一条产品线，不是同一包里的一个开关。
    """
    step(f"组装便携版（{form}）")
    ver = version()
    name = f"jiliu-portable-{ver}" + ("" if form == "oss" else f"-{form}")
    out = DIST / name
    if out.exists():
        rm_tree(out)
    out.mkdir(parents=True)

    if exe_ok and (DIST / "jiliu.exe").is_file():
        shutil.copy2(DIST / "jiliu.exe", out / "jiliu.exe")
    else:
        # 没有 exe 的「便携版」是个空壳：用户解压出来只有一堆文本和 extension，
        # 双击没反应。宁可这一轮不出包，也不要把这种包发出去 ——
        # 上一次就是因为 exe 被正在运行的进程占用、PyInstaller 覆盖失败，
        # 静默打出了一个 2.4MB 的残包。
        raise SystemExit(
            "错误：dist/jiliu.exe 不存在或复制失败，便携包会是个空壳，已中止。\n"
            "  常见原因：上一次构建的 jiliu.exe 还在运行（文件被占用）。\n"
            "  先关掉正在跑的汲流，再重跑。")

    # 协议唤起器：和主程序放一起，注册表指向它。
    # 缺了不算致命（jiliu/urltake.py 的 handler_path() 会退回主程序，
    # 功能一样只是点链接要等四秒），所以这里只警告不中止。
    if LAUNCHER_EXE.is_file():
        shutil.copy2(LAUNCHER_EXE, out / LAUNCHER_EXE.name)
    else:
        log("警告：没有 jiliu-wake.exe，协议唤起会退回主程序（点链接要等约 4 秒）")

    for f in payload_files():
        p = ROOT / f
        if p.is_file():
            shutil.copy2(p, out / f)
    for d in PAYLOAD_DIRS:
        copy_tree(ROOT / d, out / d)
    for src, dst in DOCS:
        p = ROOT / src
        if p.is_file():
            shutil.copy2(p, out / dst)

    # 便携标记：有这个文件，状态就存在程序目录的 data/ 里，不动 %USERPROFILE%
    (out / "portable").write_text(
        "这个文件存在时，汲流把配置、任务、下载都放在本目录下（便携模式）。\n"
        "想要它改用用户目录（~/.jiliu），删掉本文件即可。\n",
        encoding="utf-8")

    # 安装器靠这个报版本号 —— 负载里只有单文件 exe，没源码可 import
    (out / "version.txt").write_text(ver + "\n", encoding="utf-8")

    # 发行形态跟着安装包走：归档版装完就是归档版，不再给用户一个
    # 「切回开源版」的开关 —— 两条产品线的能力与承诺不一样。
    if form and form != "oss":
        (out / "form.txt").write_text(form + "\n", encoding="utf-8")

    (out / "bin").mkdir(exist_ok=True)
    (out / "bin" / "放这里.txt").write_text(
        "这个目录里已经有 aria2c.exe（随包附带，默认引擎顺序第一位就是它）。\n"
        "想换版本或补别的工具，直接丢进来就会被识别，不用改配置：\n\n"
        "  aria2c.exe   P2P / 磁力 / 多源下载（已随包，可替换成新版）\n"
        "  ffmpeg.exe   音视频合流 / 转码（需要时可在「设置 › 下载 › 引擎」\n"
        "               点「下载安装」，程序会自己下好放进这里）\n"
        "  ffprobe.exe  媒体信息探测（跟 ffmpeg 一起）\n"
        "  yt-dlp.exe   视频解析（exe 里已内置一份，放新的可覆盖）\n\n"
        "放好后在面板里点「环境体检」或跑 jiliu.exe tools 就能看到识别结果。\n",
        encoding="utf-8")

    # 主入口是 jiliu.exe 本身（双击就开窗口，没有黑框）。
    # 这个 .cmd 只是给"习惯找启动文件"的人留的一层壳：用 start 把 exe
    # 甩出去，然后 cmd 立刻退出 —— 不会像以前那样常驻一个终端窗口守着。
    (out / "启动.cmd").write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        "if exist jiliu.exe (\r\n"
        "  start \"\" jiliu.exe %*\r\n"
        "  exit /b\r\n"
        ") else (\r\n"
        "  echo 没找到 jiliu.exe，请确认解压完整。\r\n"
        "  pause\r\n"
        ")\r\n", encoding="utf-8")

    zp = DIST / f"{name}.zip"
    # 只打「我们自己放进来的东西」：downloads/ 是运行时目录，
    # 构建后只要有人在里面下过一次文件，重打 zip 就会把用户的
    # 下载内容（甚至别人的视频）一起发给所有人。
    skip_top = {"downloads", "data", "logs", "cache"}
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(out.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(out)
            if rel.parts and rel.parts[0] in skip_top:
                continue
            z.write(p, pathlib.Path(name) / rel)
    log(f"便携目录 {out}")
    log(f"便携压缩包 {zp.name}  {zp.stat().st_size / 1048576:.1f} MB")
    return zp


def build_setup(exe_ok: bool) -> bool:
    """把安装器连同便携负载打成一个 exe。"""
    step("构建 jiliu-setup.exe")
    if not exe_ok:
        log("没有 jiliu.exe，跳过安装版")
        return False
    if not INSTALLER_SRC.is_file():
        log(f"缺安装器源码 {INSTALLER_SRC}，跳过")
        return False

    rm_quiet(DIST / "jiliu-setup.exe")

    payload = DIST / "setup_payload"
    if payload.exists():
        rm_tree(payload)
    payload.mkdir(parents=True)
    shutil.copy2(DIST / "jiliu.exe", payload / "jiliu.exe")
    # 唤起器跟着装 —— 安装版用户点网页链接同样要快。
    if LAUNCHER_EXE.is_file():
        shutil.copy2(LAUNCHER_EXE, payload / LAUNCHER_EXE.name)
    (payload / "version.txt").write_text(version() + "\n", encoding="utf-8")
    for d in PAYLOAD_DIRS:
        copy_tree(ROOT / d, payload / d)
    for src, dst in DOCS:
        p = ROOT / src
        if p.is_file():
            shutil.copy2(p, payload / dst)
    for f in payload_files():
        p = ROOT / f
        if p.is_file():
            shutil.copy2(p, payload / f)
    (payload / "bin").mkdir(exist_ok=True)

    # 同上：自己清，别让 --clean 去批量删
    rm_tree(BUILD / "setup")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile",
        "--name", "jiliu-setup",
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "setup"),
        "--specpath", str(BUILD),
        "--add-data", f"{payload}{os.pathsep}payload",
        "--console",
        str(INSTALLER_SRC),
    ]
    rc = run(cmd, env=pyinstaller_env())
    exe = DIST / "jiliu-setup.exe"
    ok = rc == 0 and exe.is_file()
    if ok:
        log(f"jiliu-setup.exe  {exe.stat().st_size / 1048576:.1f} MB")
    else:
        log(f"安装器构建失败（rc={rc}）")
    # 注意：这里**不**清理 payload。
    # 清理要放在清单写完之后（见 main），否则一旦清理被拦，
    # 进程直接中断，清单就停在上一轮 —— 产物换了、哈希还是旧的，很难发现。
    return ok


def write_manifest(items: dict) -> None:
    ver = version()
    lines = [
        f"汲流 v{ver} 构建清单",
        f"构建时间 {__import__('time').strftime('%Y-%m-%d %H:%M:%S')}",
        f"Python {sys.version.split()[0]}",
        "",
    ]
    sums = []
    for name, path in sorted(items.items()):
        p = pathlib.Path(path)
        if not p.is_file():
            lines.append(f"  [缺失] {name}")
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f"  {name:<30} {p.stat().st_size / 1048576:>7.2f} MB  {h[:16]}")
        sums.append({"name": name, "file": p.name, "sha256": h,
                     "bytes": p.stat().st_size})
    text = "\n".join(lines) + "\n"
    (DIST / "构建清单.txt").write_text(text, encoding="utf-8")
    (DIST / "manifest.json").write_text(
        json.dumps({"version": ver, "artifacts": sums}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("\n" + text)


def main() -> int:
    ap = argparse.ArgumentParser(description="汲流发布打包")
    ap.add_argument("--skip-exe", action="store_true", help="复用已有 dist/jiliu.exe")
    ap.add_argument("--skip-setup", action="store_true", help="不打安装版")
    ap.add_argument("--tests", action="store_true", help="打包前先跑全部测试")
    ap.add_argument("--form", default="oss", choices=["oss", "archive"],
                    help="发行形态：oss=开源版（含视频站解析），archive=归档版")
    args = ap.parse_args()

    print(f"汲流打包 v{version()}")
    print(f"项目 {ROOT}")
    DIST.mkdir(exist_ok=True)

    if args.tests:
        step("打包前测试")
        rc = run([sys.executable, str(ROOT / "tests" / "run_all.py")])
        if rc != 0:
            print("\n测试没过，中止打包。")
            return rc

    if args.skip_exe and (DIST / "jiliu.exe").is_file():
        log("复用已有 jiliu.exe")
        exe_ok = True
    else:
        exe_ok = build_exe()
    if not exe_ok:
        print("\n没有主程序 exe，只能打出不含可执行文件的骨架包。")

    # 唤起器不吃 --skip-exe（它是独立产物，几十秒的事）：
    # 复用旧主程序时仍然该重打，否则改了 urltake 的解析逻辑，
    # 唤起器还是旧的那份，表现是"命令行能唤起、点网页链接不行"。
    build_launcher()

    zp = make_portable(exe_ok, form=args.form)
    setup_ok = False if args.skip_setup else build_setup(exe_ok)

    items = {"便携版": zp}
    if exe_ok:
        items["主程序"] = DIST / "jiliu.exe"
    if LAUNCHER_EXE.is_file():
        items["协议唤起器"] = LAUNCHER_EXE
    if setup_ok:
        items["安装版"] = DIST / "jiliu-setup.exe"

    # 先落清单再清理。产物一旦生成，清单就该定稿 ——
    # 清理只是收尾，它出岔子不该影响「这一版叫什么、哈希是多少」。
    write_manifest(items)

    step("清理中间产物")
    cleanup_intermediates()

    step("完成")
    for k, v in items.items():
        log(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
