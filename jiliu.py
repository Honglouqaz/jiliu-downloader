#!/usr/bin/env python3
"""汲流 · JiLiu —— 自建多线程下载器

入口：
    python jiliu.py                     启动面板（默认行为）
    python jiliu.py serve               启动面板与 API
    python jiliu.py add <url>           下载一个直链
    python jiliu.py video <url>         解析并下载视频
    python jiliu.py classify <url>      识别资源类型与接管策略
    python jiliu.py tools               查看外部工具状态

打包成单文件 exe 后，这个文件同时也是 yt-dlp 的宿主：
`jiliu.exe --run-ytdlp <args>` 会把参数原样转给内置的 yt_dlp 模块。
这样便携版不需要用户另外装 Python，也不必依赖外部的 yt-dlp.exe。
"""
import os
import sys
import time
import pathlib

# 允许直接 `python jiliu.py` 跑，不需要先 pip install。
# 冻结（PyInstaller）后 __file__ 在临时解包目录里，模块已在 sys.path 上，跳过。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def _run_ytdlp(argv):
    """把自己当成 yt-dlp 用。给 tools.ytdlp_command() 冻结分支调用。"""
    try:
        import yt_dlp
    except ImportError as exc:
        sys.stderr.write(
            f"这个构建没有内置 yt-dlp（{exc}）。\n"
            "请把 yt-dlp.exe 放到程序目录的 bin/ 下，或在 PATH 里装一个。\n"
        )
        return 9
    try:
        return int(yt_dlp.main(argv) or 0)
    except SystemExit as exc:  # yt-dlp 内部会 sys.exit
        return int(exc.code or 0)


def _windowed_stdio_guard() -> None:
    """windowed（console=False）冻结态的流兜底。

    PyInstaller windowed 进程没有控制台，sys.stdout / sys.stderr / sys.stdin
    全是 None。内置 print() 对 None 会静默跳过（CPython 特判），但代码里
    大量 `sys.stdout.write(...)`（进度条）和第三方库的 stderr.write 会直接
    AttributeError —— yt-dlp 的宿主模式（--run-ytdlp）就靠这些流工作。
    统一换成哑设备：功能照常，输出不可见（windowed 的本意）。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
    if getattr(sys, "stdin", None) is None:
        try:
            sys.stdin = open(os.devnull, "r", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def _fix_console() -> None:
    """把标准输出统一成 UTF-8。

    Windows 上有个专门的坑：stdout 连到管道或文件时，Python 用 locale 编码
    （简体中文机器上是 GBK）。而 CLI 的告警行用了 ⚠ 这类符号 —— GBK 编不出来，
    print 会直接抛 UnicodeEncodeError，整个命令带着 traceback 挂掉。

    连真控制台时 Python 本来就是 UTF-8（内部走 WriteConsoleW），所以这一句
    只影响管道/重定向，不会把正常终端搞乱。errors="replace" 是兜底：
    万一还有编不出的字符，退化成 ? 也比整个命令崩掉好。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _console_handle():
    """拿到当前控制台窗口句柄。"""
    try:
        import ctypes
        return ctypes.windll.kernel32.GetConsoleWindow()
    except Exception:  # noqa: BLE001
        return 0


def _detach_console() -> bool:
    """把控制台从本进程上**摘掉**，而不是把窗口藏起来。

    为什么不用 ShowWindow(SW_HIDE)：
        控制台子系统（PyInstaller 默认 console=True）的程序，Windows 会
        给它挂一个 console。`ShowWindow(hwnd, 0)` 在不少机器上只是把窗口
        **最小化**，任务栏/Alt+Tab 里仍留一条 —— 用户看到的就是"自动最小化
        的终端窗口"。
        更麻烦的是：那个 console 还**属于本进程**。用户手一抖把它关掉，
        进程就收到 CTRL_CLOSE_EVENT，默认行为是直接终止 —— 于是
        "关掉终端窗口程序也退出"。

    正确做法是 FreeConsole()：把进程与控制台的关联切断。
    切完窗口自然消失、任务栏干净，而且**关它也不影响我们** ——
    因为那个 console 已经不再属于我们了，我们收不到它的关闭事件。

    切之前把 stdout/stderr 换成哑设备，否则后续 print 会抛
    OSError（句柄已失效）。GUI 模式下本来也不该往控制台写东西。

    为什么现在不太依赖它了（1.7.1 起）：
        打包改成了 windowed（`--noconsole`），双击启动时 Windows 压根不给
        进程建控制台，黑框从根上没了，也就不用再"先建一个再藏起来"。
        但 windowed 又会让命令行用法丢失输出，于是有了配套的
        `_attach_console()`：**只有真的需要时才补一个**控制台。

        保留这个函数是为了兼容源码运行、以及有人拿旧参数自己打的
        console 版 exe —— 那些场景下 FreeConsole 仍然是正确的收尾动作。

    2026-09-24 补（实测真机结论）：
        打包成 onefile 后，控制台属于**父 bootloader 进程**——PyInstaller
        的父进程也是一个 console 程序，它握着控制台等子进程退出。所以：
        - 子进程光 FreeConsole **窗口不会消失**（爹还挂着）——用户看到的
          「启动后多一个终端」就是这个；
        - 必须在 FreeConsole **之前**先 ShowWindow(SW_HIDE) 把窗口藏掉，
          对窗口本身隐藏不受「谁拥有」影响。
        实测：父进程控制台可见=True（class PseudoConsoleWindow），
        加 hide 后可见=False。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    hwnd = _console_handle()
    if not hwnd:
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32

        # 先断流：FreeConsole 之后旧句柄就废了，print 会炸
        try:
            devnull = open(os.devnull, "w", encoding="utf-8")
            sys.stdout = devnull
            sys.stderr = devnull
        except Exception:  # noqa: BLE001
            pass

        # 顺手关掉控制台里可能挂着的关闭处理器（保险，FreeConsole 后
        # 理论上已收不到，但多一层不吃亏）
        try:
            k32.SetConsoleCtrlHandler(None, True)
        except Exception:  # noqa: BLE001
            pass

        # onefile 下控制台窗口归父 bootloader，FreeConsole 后窗口不会消失
        # —— 必须先藏窗口再摘控制台，顺序不能反
        try:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
        except Exception:  # noqa: BLE001
            pass

        k32.FreeConsole()
        return True
    except Exception:  # noqa: BLE001
        # FreeConsole 万一失败，退回"藏窗口"的老办法
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
            return True
        except Exception:  # noqa: BLE001
            return False


# 兼容旧名字：外部（测试、探针）仍按 _hide_console 调用
_hide_console = _detach_console


def _show_console() -> None:
    """把控制台要回来。窗口起不来时得让用户看见错误。

    FreeConsole 之后控制台已经脱离，只能重新分配一个（AllocConsole）。
    新开的窗口标题会带上程序名，够用 —— 反正是出错场景。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        # 先试旧路径：窗口只是被藏起来的情况
        hwnd = _console_handle()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 5)   # SW_SHOW
            return
        # 已经 FreeConsole 过了：重新分配
        if k32.AllocConsole():
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            hwnd = _console_handle()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 5)
    except Exception:  # noqa: BLE001
        pass


def _has_console() -> bool:
    """本进程当前是否挂着一个控制台。

    windowed 打包 + 双击 → 没有控制台；从 cmd 启动 → 有（继承的）。
    用来区分"用户从快捷方式点的"和"用户在命令行敲的"。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001
        return True


def _attach_console(title: str = "汲流 · 命令行") -> bool:
    """**按需**给命令行用法开一个控制台。

    为什么现在要这一步：
        1.7.1 起 exe 改成 windowed（`--noconsole`）打包 —— 双击启动时
        Windows 根本不会给进程建控制台，所以那个「先闪一个黑框，
        然后跳浏览器」的观感从根上没了。

        但命令行用法（`jiliu.exe add <url>`、`tools`、`doctor`）还得有输出。
        从 cmd / PowerShell 里启动时，父进程的控制台句柄会被继承，
        sys.stdout 本身可用，什么都不用做；从快捷方式、资源管理器
        带参数启动时才没有句柄，这时补一个 AllocConsole。

    不碰 `--run-ytdlp`：那是我们自己 Popen 出来的宿主子进程，
    给它开控制台会在屏幕上弹黑框。它的 stdout 是父进程给的管道。

    返回是否新开了窗口（仅用于日志/调试，不影响流程）。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    # 已经有能用的 stdout（典型是从 cmd 启动）—— 直接用，别多弹一个窗。
    # 注意要排掉 _windowed_stdio_guard() 塞进来的哑设备：那是"没有输出"
    # 的占位，不能当成可用终端。
    try:
        name = getattr(sys.stdout, "name", None)
        if (sys.stdout is not None and not getattr(sys.stdout, "closed", False)
                and name != os.devnull):
            sys.stdout.write("")
            sys.stdout.flush()
            return False
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        if not k32.AllocConsole():
            return False
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        try:
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        try:
            k32.SetConsoleTitleW(title)
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


def _crash_log_path() -> pathlib.Path:
    """崩溃日志放哪。

    冻结后优先放 exe 同级目录（便携版的自然位置），目录不可写就退回
    用户目录 —— 装在 Program Files 里时同级目录是只读的。
    """
    if getattr(sys, "frozen", False):
        base = pathlib.Path(sys.executable).resolve().parent
    else:
        base = pathlib.Path(__file__).resolve().parent
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return base / "jiliu-crash.log"
    except Exception:  # noqa: BLE001
        home = pathlib.Path.home() / ".jiliu"
        try:
            home.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return home / "jiliu-crash.log"


def _report_crash(exc: BaseException) -> None:
    """把未捕获异常写进日志，并尽量让用户看见。

    双击启动时控制台已经摘掉了 —— 一旦启动早期抛异常，进程会瞬间退出，
    用户什么都看不到（这正是曾经踩过的坑）。所以这里三件事：
    落盘 + 恢复控制台 + 停住等回车。

    注意顺序：**必须先 _show_console()**。因为摘控制台时把 stdout/stderr
    换成了哑设备，先写的话那两行提示就丢了。
    """
    import traceback
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_path = _crash_log_path()

    # 先落盘（不依赖任何流，最可靠）
    try:
        with open(log_path, "a", encoding="utf-8") as fp:
            fp.write(f"\n===== {stamp} =====\n")
            fp.write(f"argv={sys.argv!r} frozen={getattr(sys, 'frozen', False)}\n")
            fp.write(detail)
    except Exception:  # noqa: BLE001
        pass

    # 再把控制台要回来，然后才输出
    _show_console()
    for stream in (sys.stderr, sys.stdout):
        try:
            stream.write(f"\n汲流启动失败：{type(exc).__name__}: {exc}\n")
            stream.write(f"完整堆栈已写入 {log_path}\n")
            stream.flush()
            break
        except Exception:  # noqa: BLE001
            continue

    try:
        input("\n按回车键关闭…")   # 不让黑框一闪而过
    except Exception:  # noqa: BLE001
        pass


def main():
    argv = sys.argv[1:]

    # 宿主子命令：父进程用管道喂它，绝不能给它开控制台（会弹黑框）
    is_ytdlp_host = bool(argv) and argv[0] == "--run-ytdlp"

    # 兼容旧安装器留下的快捷方式。
    # 1.7.0 及以前的安装器把快捷方式参数写成 `serve` —— 那是「只起 HTTP
    # 服务」。windowed 之后它既不开窗口也不开浏览器，用户双击会以为程序
    # 根本没启动（进程在后台，任务栏什么都没有）。
    # 判据：没有控制台 = 从快捷方式/资源管理器双击来的，而不是从 cmd 敲的。
    if (getattr(sys, "frozen", False) and argv == ["serve"]
            and not _has_console()):
        argv = ["gui"]

    # 开桌面窗口的三种写法：无参数、`gui`、以及上面兼容出来的 `gui`。
    # 它们都不需要控制台 —— 尤其是安装器新建的快捷方式带的就是 `gui`，
    # 要是这里误判成"命令行用法"，就会给用户弹一个黑框。
    desktop_mode = (not argv) or (argv[:1] == ["gui"])

    # windowed 打包下双击启动没有控制台，流全是 None —— 先兜底成哑设备，
    # 保证任何 print/sys.stdout.write 都不会 AttributeError。
    _windowed_stdio_guard()

    # 命令行用法（带参数、又不是宿主子命令、也不是开窗口）：没有真终端
    # 就补一个，否则用户在快捷方式里跑 `jiliu.exe tools` 看不到任何输出。
    console_opened = _attach_console() if (argv and not is_ytdlp_host
                                           and not desktop_mode) else False

    _fix_console()

    # 内部子命令：当作 yt-dlp 跑。必须在 argparse 之前拦下来，
    # 否则会被当成未知参数报错。
    if is_ytdlp_host:
        return _run_ytdlp(argv[1:])

    if not argv:
        argv = ["gui"]

    # 摘掉控制台（不是藏起来）：窗口不再残留，关它也不会把进程带走。
    # windowed 打包下本来就没有控制台，这一步会自己跳过。
    detached = _detach_console() if desktop_mode else False

    try:
        from jiliu.cli import main as cli_main
        rc = cli_main(argv)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        # 双击启动时把错误摊开给用户看，别让它闪退
        if desktop_mode:
            _report_crash(exc)
        raise SystemExit(1) from exc

    # GUI 没起来就把控制台放回来 —— 错误信息不能藏在一个看不见的窗口里
    if (detached or desktop_mode) and rc not in (0, None):
        _show_console()

    # 控制台是我们刚补出来的（双击/快捷方式跑命令行）：跑完别让它一闪就关，
    # 否则用户什么都来不及看。从 cmd 启动时不会走这里，不影响脚本调用。
    if console_opened:
        try:
            input("\n按回车键关闭…")
        except Exception:  # noqa: BLE001
            pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
