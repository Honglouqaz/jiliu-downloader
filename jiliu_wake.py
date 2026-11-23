#!/usr/bin/env python3
"""汲流 · 协议唤起器（打包成 jiliu-wake.exe）

它存在的原因，一句话：**注册表里那个程序，用户每点一次链接就要启动一次。**

    主程序 jiliu.exe 是约 25MB 的 PyInstaller onefile，每次启动都要把
    内容解包到临时的 _MEI* 目录。实测 `jiliu.exe version`（一个立刻报错
    退出的子命令）就要 4.1 秒；同一份内容打 onedir 只要 1.15 秒 ——
    那 3 秒的差值纯粹是解包。再加上服务端探测慢站点还能花掉两秒多，
    用户点完网页链接盯着浏览器等六秒，只会得出「这软件没反应」。

    这个 exe 只做三件事：读 runtime.json、POST 一个任务、退出。
    依赖只有标准库 + jiliu.urltake + jiliu.config，打包 10.3MB，
    实测 2.5–2.9 秒（主程序要 5.8–6.1 秒）。用户感知的差别是
    「点了就有」和「点了像没点」。

它不做的事

    不建任务的逻辑（那是服务端的事，见 /api/tasks）、不做协议校验
    （urltake.parse 里已有白名单）、不开窗口、不常驻。
    没有实例时它只负责把主程序拉起来，把协议串原样传过去 ——
    真正的投递还是主程序自己做（服务起来后 consume_pending）。

失败怎么排查

    windowed 进程没有控制台，出错时用户看不到任何东西。所以任何
    异常都往 <HOME>/logs/wake.log 写一行。只写失败，成功不写 ——
    这个文件不该在正常情况下变大。
"""
import os
import pathlib
import subprocess
import sys
import time

# 源码运行（python jiliu_wake.py）时把仓库根挂到 sys.path。
# 冻结后模块已经在包内，跳过。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from jiliu import urltake   # noqa: E402


def _base_dir() -> pathlib.Path:
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent


def _log(text: str) -> None:
    """留一行痕迹。windowed 没有输出，不写盘就完全查不到。"""
    try:
        from jiliu import config as _config
        d = _config.HOME / "logs"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "wake.log"
        # 超过 64KB 就重开：长期使用不至于无限增长，出问题时又能看到最近一段
        try:
            if p.is_file() and p.stat().st_size > 65536:
                p.write_text("", encoding="utf-8")
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as fp:
            fp.write(time.strftime("%Y-%m-%d %H:%M:%S ")
                     + text.replace("\n", " ")[:500] + "\n")
    except Exception:          # noqa: BLE001
        pass


def _main_exe() -> pathlib.Path:
    """主程序在哪。同目录优先，其次问注册表。

    注册表那条兜底是为了「有人把 jiliu-wake.exe 单独拷到了别处」——
    便携包里两个 exe 永远在一起，但用户手工折腾过的目录不保证。

    读出来必须验一下**它真的是主程序**：1.9.3 的某个中间版本把
    launcher 自己的路径写进过 JiliuPath，照那条启动就是"唤起器启动
    唤起器"，一路递归下去，任务栏会刷出一排进程。
    """
    p = _base_dir() / "jiliu.exe"
    if p.is_file():
        return p
    if os.environ.get("JILIU_NO_REGISTRY"):
        # 测试/便携场景：不要拿本机已安装的 JiliuPath 兜底，
        # 否则"同目录没有主程序"会被误判成"有"
        return p
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, urltake.CLASS_KEY) as k:
            raw = str(winreg.QueryValueEx(k, "JiliuPath")[0] or "")
        q = pathlib.Path(raw.strip('"'))
        if (q.is_file()
                and q.name.lower() != urltake.LAUNCHER_NAME.lower()
                and q.parent != _base_dir()):
            return q
    except Exception:          # noqa: BLE001
        pass
    return p


def _launch(spec: str) -> int:
    """把主程序拉起来。spec 为空 = 只开窗口。"""
    exe = _main_exe()
    if not exe.is_file():
        _log(f"启动失败：找不到主程序（期望 {exe}）")
        return 3
    args = [str(exe)]
    if spec:
        args += [urltake.FLAG, spec]
    flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS：主程序是长时进程，不该跟着唤起器一起被
        # 父进程/控制台回收。CREATE_NEW_PROCESS_GROUP 顺带让它不接收
        # 唤起器所在进程组的 Ctrl+C —— 从 cmd 里点链接时才会遇到。
        flags = 0x00000008 | 0x00000200
    try:
        subprocess.Popen(args, cwd=str(exe.parent), close_fds=True,
                         creationflags=flags)
    except Exception as exc:   # noqa: BLE001
        _log(f"启动 {exe} 失败：{type(exc).__name__}: {exc}")
        return 4
    return 0


def main() -> int:
    spec, _rest = urltake.split_argv(sys.argv[1:])

    if not spec:
        # 直接双击了这个 exe（或快捷方式没带参数）：当"打开汲流"处理，
        # 免得用户双击后什么都没有发生。
        return _launch("")

    info = urltake.parse(spec)

    if info.get("wake"):
        # jiliu://wake 只负责叫醒。已经在跑就什么都不做 —— 不能因为
        # 用户点了两下就开出第二个窗口（两个实例抢 tasks.json）。
        # read_runtime() 为空不代表没在跑：清过 data 目录、或里面记的
        # 是上一个进程的 pid，都会让它返回 None。这时扫一眼端口再决定，
        # 否则用户每点一次就多一个实例。
        if urltake.read_runtime() or urltake.scan_instance():
            return 0
        return _launch("")

    if not info.get("ok"):
        # 链接不合法（javascript: 之类，或者缺 url）。不在这儿静默吞掉：
        # 交给主程序，界面里的"最近唤起"会把原因摆出来。用户至少能
        # 知道自己点到了什么、为什么没下。
        _log(f"链接被拒绝：{info.get('error')}  spec={spec}")
        return _launch(spec)

    hit = urltake.handoff(info)
    if hit.get("ok"):
        return 0

    # 没有实例 / 投递被拒：冷路径，起主程序，待办挂环境变量随它进去。
    _log(f"热投递未成功（{hit.get('reason')}），改为启动主程序")
    return _launch(spec)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:   # noqa: BLE001
        _log(f"未捕获异常：{type(exc).__name__}: {exc}")
        raise SystemExit(1)
