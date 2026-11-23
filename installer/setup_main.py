#!/usr/bin/env python3
"""汲流安装器（被打成 jiliu-setup.exe）。

做的事：
    1. 把负载解到安装目录（默认 %LOCALAPPDATA%\\Jiliu）
    2. 建开始菜单 / 桌面快捷方式
    3. 写卸载项，让它出现在「应用和功能」里
    4. 在安装目录放一个卸载脚本

为什么不用 NSIS / Inno Setup：
    那些工具能把安装包做得更小更专业，但要在构建机上装一遍工具链。
    这里的负载本来就是一个自包含的 exe，安装动作只是复制 + 建快捷方式，
    用标准库写清楚比引入外部依赖更可控，也更好审。

支持 --dry-run：只打印要做什么，不落盘。CI 与本地验证都靠它。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

APP_NAME = "汲流"
APP_ID = "Jiliu"
APP_EXE = "jiliu.exe"
PUBLISHER = "JiLiu"
URL = "http://127.0.0.1:8788/"

DEFAULT_TARGET = pathlib.Path(
    os.environ.get("LOCALAPPDATA") or (pathlib.Path.home() / "AppData" / "Local")
) / APP_ID

# 快捷方式启动参数。
#
# 以前这里是 "serve" —— 那是「只起 HTTP 服务」的命令，不带窗口。
# 装完双击快捷方式，用户看到的是一个终端窗口在跑服务，然后（旧版默认
# 开浏览器时）跳去浏览器；就算不开浏览器，也只有个黑框杵在那里。
# 现在统一走 "gui"：开原生桌面窗口，和双击 exe 完全一样。
LAUNCH_ARGS = "gui"


def payload_dir() -> pathlib.Path:
    """打包进来时在 _MEIPASS/payload；直接跑源码时在仓库的 dist/setup_payload。"""
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        p = pathlib.Path(meipass) / "payload"
        if p.is_dir():
            return p
    here = pathlib.Path(__file__).resolve().parent.parent
    p = here / "dist" / "setup_payload"
    return p if p.is_dir() else here


def log(msg: str) -> None:
    print("  " + msg, flush=True)


def need_admin(target: pathlib.Path) -> bool:
    if os.name != "nt":
        return False
    try:
        probe = target / ".jiliu_write_test"
        target.mkdir(parents=True, exist_ok=True)
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return False
    except Exception:
        return True


# --------------------------------------------------------------------------
# 快捷方式（走 PowerShell 的 WScript.Shell COM，不需要额外依赖）
# --------------------------------------------------------------------------

def powershell_exe() -> str:
    for c in ("powershell.exe", "pwsh.exe"):
        p = shutil.which(c)
        if p:
            return p
    return ""


def make_shortcut(lnk: pathlib.Path, target_exe: pathlib.Path,
                  workdir: pathlib.Path, args: str = "",
                  description: str = "", dry: bool = False) -> bool:
    """建一个 .lnk。建不了就退化成 .cmd 启动器（不会让安装失败）。"""
    if dry:
        log(f"[dry-run] 建快捷方式 {lnk}")
        return True

    ps = powershell_exe()
    if ps:
        lnk.parent.mkdir(parents=True, exist_ok=True)
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut({0});"
            "$s.TargetPath = {1};"
            "$s.WorkingDirectory = {2};"
            "$s.Arguments = {3};"
            "$s.Description = {4};"
            "$s.IconLocation = {1};"
            "$s.Save()"
        ).format(
            _psq(str(lnk)), _psq(str(target_exe)), _psq(str(workdir)),
            _psq(args), _psq(description or APP_NAME),
        )
        try:
            r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, timeout=60,
                               creationflags=0x08000000)
            if r.returncode == 0 and lnk.exists():
                log(f"快捷方式 {lnk}")
                return True
            err = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()
            log(f"PowerShell 建快捷方式失败：{err.splitlines()[-1] if err else r.returncode}")
        except Exception as exc:  # noqa: BLE001
            log(f"PowerShell 调用失败：{exc}")

    # 退化方案：写一个 .cmd，双击一样能起
    fallback = lnk.with_suffix(".cmd")
    try:
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(
            "@echo off\r\n"
            f'cd /d "{workdir}"\r\n'
            f'start "" "{target_exe}" {args}\r\n',
            encoding="utf-8")
        log(f"快捷方式不可用，改用 {fallback.name}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"连 .cmd 也没写成：{exc}")
        return False


def _psq(s: str) -> str:
    """PowerShell 单引号字符串转义（'' 表示一个 '）。"""
    return "'" + str(s).replace("'", "''") + "'"


def start_menu_dir() -> pathlib.Path:
    appdata = os.environ.get("APPDATA") or str(pathlib.Path.home() / "AppData" / "Roaming")
    return pathlib.Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir() -> pathlib.Path:
    p = pathlib.Path.home() / "Desktop"
    if p.exists():
        return p
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        cand = pathlib.Path(onedrive) / "Desktop"
        if cand.exists():
            return cand
    return p


def startup_dir() -> pathlib.Path:
    appdata = os.environ.get("APPDATA") or str(pathlib.Path.home() / "AppData" / "Roaming")
    return pathlib.Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------

def reg_write(target: pathlib.Path, uninstall_cmd: str, version: str = "",
              dry: bool = False) -> bool:
    key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
    if dry:
        log(f"[dry-run] 写注册表 HKCU\\{key}")
        return True
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ,
                              version or _version(target))
            winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
            winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, str(target / APP_EXE))
            winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, str(target))
            winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, uninstall_cmd)
            winreg.SetValueEx(k, "QuietUninstallString", 0, winreg.REG_SZ,
                              uninstall_cmd + " --quiet")
            winreg.SetValueEx(k, "URLInfoAbout", 0, winreg.REG_SZ, URL)
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        log(f"已注册卸载项（出现在「应用和功能」里）")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"写注册表失败（不影响使用）：{exc}")
        return False


# ===================== 右键菜单 =====================
# 安装版默认注册右键菜单：装完就能在资源管理器里「用汲流下载」，
# 这是下载器的常识性期待。写 HKCU，不需要管理员；卸载时一并清掉。
_SHELL_SCOPES = [
    (r"Software\Classes\*\shell\Jiliu", '"%1"'),
    (r"Software\Classes\Directory\shell\Jiliu", '"%1"'),
    (r"Software\Classes\Directory\Background\shell\Jiliu", "--clipboard"),
]
_SHELL_LABEL = "用汲流下载"


def shell_menu_write(exe: pathlib.Path, dry: bool) -> int:
    """写三处右键菜单，返回写成功的处数。"""
    if os.name != "nt":
        return 0
    if dry:
        for scope, arg in _SHELL_SCOPES:
            print(f"  [dry] HKCU\\{scope} → \"{exe}\" add {arg}")
        return len(_SHELL_SCOPES)
    try:
        import winreg
    except ImportError:
        return 0
    wrote = 0
    for scope, arg in _SHELL_SCOPES:
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, scope, 0,
                                    winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _SHELL_LABEL)
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, str(exe))
                winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
                winreg.SetValueEx(k, "JiliuPath", 0, winreg.REG_SZ, f'"{exe}"')
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, scope + r"\command",
                                    0, winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" add {arg}')
            wrote += 1
        except OSError as exc:
            log(f"右键菜单写入失败（{scope}）：{exc}")
    return wrote


def shell_menu_delete(dry: bool) -> int:
    if os.name != "nt":
        return 0
    if dry:
        print("  [dry] 删除 HKCU 下的 Jiliu 右键菜单")
        return len(_SHELL_SCOPES)
    try:
        import winreg
    except ImportError:
        return 0
    removed = 0
    for scope, _ in _SHELL_SCOPES:
        for path in (scope + r"\command", scope):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
                if not path.endswith("command"):
                    removed += 1
            except OSError:
                pass
    return removed


def reg_delete(dry: bool = False) -> bool:
    key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
    if dry:
        log(f"[dry-run] 删注册表 HKCU\\{key}")
        return True
    try:
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            log("已移除卸载项")
        except FileNotFoundError:
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"删注册表失败：{exc}")
        return False


def _version(src: pathlib.Path | None = None) -> str:
    """版本号来源：负载里的 version.txt 优先，其次试着 import jiliu，最后兜底。

    负载是单文件 exe，里面没有 jiliu/ 源码目录，所以 import 那条基本不会命中 ——
    打包时写进 payload 的 version.txt 才是正常路径。
    """
    cands = []
    if src is not None:
        cands.append(src / "version.txt")
    try:
        cands.append(payload_dir() / "version.txt")
    except Exception:  # noqa: BLE001
        pass
    for cand in cands:
        try:
            if cand.is_file():
                txt = cand.read_text(encoding="utf-8").strip()
                if txt:
                    return txt.splitlines()[0].strip()
        except Exception:  # noqa: BLE001
            pass
    if src is not None:
        try:
            sys.path.insert(0, str(src))
            from jiliu import __version__  # type: ignore
            return __version__
        except Exception:  # noqa: BLE001
            pass
    return "1.0.0"


# --------------------------------------------------------------------------
# 安装 / 卸载
# --------------------------------------------------------------------------

def copy_payload(src: pathlib.Path, target: pathlib.Path, dry: bool) -> bool:
    if dry:
        log(f"[dry-run] 复制 {src}  →  {target}")
        return True
    target.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in src.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(src)
        if "__pycache__" in rel.parts:
            continue
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        n += 1
    log(f"已复制 {n} 个文件到 {target}")
    return True


def write_uninstaller(target: pathlib.Path, dry: bool) -> pathlib.Path:
    """在安装目录里放一个卸载脚本（注册表指向它，也方便用户手动找）。"""
    bat = target / "卸载.cmd"
    ps1 = target / "uninstall.ps1"
    if dry:
        log(f"[dry-run] 写卸载脚本 {bat.name} / {ps1.name}")
        return bat

    ps1.write_text(
        "# 汲流卸载脚本\n"
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f'$dir = "{target}"\n'
        "Write-Host '正在卸载汲流…'\n"
        "# 1. 让主程序收尾（如果还开着）\n"
        "Get-Process jiliu -ErrorAction SilentlyContinue | Stop-Process -Force\n"
        "# 2. 去注册表\n"
        "Remove-Item -Path "
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Jiliu' -Recurse -Force\n"
        "# 3. 删快捷方式\n"
        "Remove-Item -Path (Join-Path $env:APPDATA "
        "'Microsoft\\Windows\\Start Menu\\Programs\\汲流.lnk') -Force\n"
        "Remove-Item -Path (Join-Path $env:APPDATA "
        "'Microsoft\\Windows\\Start Menu\\Programs\\Startup\\汲流.lnk') -Force\n"
        "Remove-Item -Path (Join-Path ([Environment]::GetFolderPath('Desktop')) '汲流.lnk') -Force\n"
        "# 4. 删程序目录（下次开机自删，避开正在占用的文件）\n"
        "$tmp = Join-Path $env:TEMP ('jiliu-del-' + [guid]::NewGuid().ToString('N') + '.cmd')\n"
        "'@echo off' | Out-File -Encoding ascii $tmp\n"
        "'timeout /t 2 /nobreak >nul' | Out-File -Append -Encoding ascii $tmp\n"
        "('rmdir /s /q \"' + $dir + '\"') | Out-File -Append -Encoding ascii $tmp\n"
        "Start-Process -WindowStyle Hidden -FilePath $tmp\n"
        "Write-Host '卸载完成。'\n",
        encoding="utf-8-sig")

    bat.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0{ps1.name}"\r\n',
        encoding="utf-8")
    log(f"卸载脚本 {bat.name}")
    return bat


def do_install(target: pathlib.Path, dry: bool, shortcuts: bool,
               autostart: bool, quiet: bool, desktop: bool = True,
               shell_menu: bool = True) -> int:
    src = payload_dir()
    print(f"\n  {APP_NAME} 安装")
    print(f"  负载目录 {src}")
    print(f"  安装目录 {target}")
    print()

    if not src.is_dir():
        log(f"找不到负载目录：{src}")
        return 2
    exe = src / APP_EXE
    if not exe.is_file():
        log(f"负载里没有 {APP_EXE}，这个安装包不完整")
        return 2

    # dry 要放在前面：need_admin() 会真的去 mkdir 并写一个测试文件，
    # 写成 `need_admin(target) and not dry` 的话，--dry-run 也已经在落盘了。
    if not dry and need_admin(target):
        log("安装目录不可写。请用管理员身份重新运行，")
        log(f"或用 --target 指一个能写的目录，例如：")
        log(f'  jiliu-setup.exe --target "%LOCALAPPDATA%\\{APP_ID}"')
        return 3

    # 已经在跑就先停掉，否则 exe 被占用换不掉
    if not dry:
        stop_running()

    copy_payload(src, target, dry)
    target_exe = target / APP_EXE

    if shortcuts:
        make_shortcut(start_menu_dir() / f"{APP_NAME}.lnk", target_exe, target,
                      LAUNCH_ARGS, f"{APP_NAME} 下载器", dry)
    # 桌面快捷方式是独立的开关：有人只要开始菜单，不想桌面多一个图标
    if shortcuts and desktop:
        make_shortcut(desktop_dir() / f"{APP_NAME}.lnk", target_exe, target,
                      LAUNCH_ARGS, f"{APP_NAME} 下载器", dry)
    if autostart:
        make_shortcut(startup_dir() / f"{APP_NAME}.lnk", target_exe, target,
                      LAUNCH_ARGS, f"{APP_NAME}（开机自启）", dry)

    if shell_menu:
        n = shell_menu_write(target_exe, dry)
        if n:
            log(f"右键菜单已注册（{n}/3 处，HKCU，免管理员）")

    unbat = write_uninstaller(target, dry)
    ver = _version(src)
    reg_write(target, f'"{unbat}"', ver, dry)

    print()
    log("装好了。")
    if not quiet:
        print(f"\n  开始菜单和桌面上有「{APP_NAME}」，双击即用。")
        print(f"  面板地址 {URL}")
        print(f"  浏览器插件在 {target / 'extension'} —— 在扩展管理页开「开发者模式」，")
        print(f"  选「加载已解压的扩展程序」，指向那个目录。")
        print(f"\n  卸载：{unbat}")
    return 0


def stop_running() -> None:
    if os.name != "nt":
        return
    try:
        subprocess.run(["taskkill", "/IM", APP_EXE, "/F"],
                       capture_output=True, timeout=15,
                       creationflags=0x08000000)
    except Exception:
        pass


def do_uninstall(target: pathlib.Path, dry: bool) -> int:
    shell_menu_delete(dry)
    print(f"\n  {APP_NAME} 卸载")
    print(f"  安装目录 {target}\n")

    if not dry:
        stop_running()

    # 快捷方式
    for lnk in [start_menu_dir() / f"{APP_NAME}.lnk",
                desktop_dir() / f"{APP_NAME}.lnk",
                startup_dir() / f"{APP_NAME}.lnk"]:
        if dry:
            log(f"[dry-run] 删 {lnk}")
            continue
        try:
            if lnk.exists():
                lnk.unlink()
                log(f"删除 {lnk.name}")
        except Exception as exc:  # noqa: BLE001
            log(f"删 {lnk.name} 失败：{exc}")

    reg_delete(dry)

    if dry:
        log(f"[dry-run] 删目录 {target}")
        return 0

    # 目录可能因为 exe 还在跑删不干净，退化成"下次开机自删"
    try:
        shutil.rmtree(target)
        log(f"已删除 {target}")
    except Exception:
        bat = pathlib.Path(tempfile.gettempdir()) / "jiliu-uninstall-self.cmd"
        bat.write_text(
            "@echo off\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'rmdir /s /q "{target}"\r\n'
            'del "%~f0"\r\n', encoding="ascii", errors="replace")
        try:
            subprocess.Popen(["cmd", "/c", str(bat)],
                             creationflags=0x08000000 | 0x00000008)
            log("程序目录被占用，已安排下次机会自动删除")
        except Exception as exc:  # noqa: BLE001
            log(f"删目录失败，请手动删除 {target}（{exc}）")

    log("卸载完成。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="jiliu-setup",
                                 description=f"{APP_NAME} 安装程序")
    ap.add_argument("--target", default="", help=f"安装目录，默认 {DEFAULT_TARGET}")
    ap.add_argument("--uninstall", action="store_true", help="卸载")
    ap.add_argument("--dry-run", action="store_true", help="只打印要做什么，不落盘")
    ap.add_argument("--no-shortcuts", action="store_true", help="不建快捷方式")
    ap.add_argument("--no-desktop", action="store_true", help="不建桌面快捷方式")
    ap.add_argument("--autostart", action="store_true", help="加开机自启")
    ap.add_argument("--no-shell-menu", action="store_true",
                    help="不注册资源管理器右键菜单（默认注册）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    target = pathlib.Path(args.target).expanduser() if args.target else DEFAULT_TARGET

    if args.uninstall:
        return do_uninstall(target, args.dry_run)

    return do_install(target, args.dry_run,
                      shortcuts=not args.no_shortcuts,
                      autostart=args.autostart,
                      quiet=args.quiet,
                      desktop=not args.no_desktop,
                      shell_menu=not args.no_shell_menu)


if __name__ == "__main__":
    raise SystemExit(main())
