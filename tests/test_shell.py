#!/usr/bin/env python3
"""右键菜单 + 拖拽投递回归（P0-2）。

    python tests/test_shell.py

注册表部分用测试专用键名（JiliuTest），跑完删干净，不动真实菜单。
窗口部分只验证「能创建、可见、能销毁」——拖放的 WM_DROPFILES 需要
真实鼠标，交给人工验收。
"""
import io
import os
import pathlib
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import dropwin, shellmenu  # noqa: E402

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


# ----------------------------------------------------------------------
# 1. 拖拽解析
# ----------------------------------------------------------------------
print("== 拖拽解析 ==")
tmp = pathlib.Path(tempfile.mkdtemp(prefix="jiliu-drop-"))
f1 = tmp / "a.zip"
f1.write_bytes(b"x" * 10)
urlf = tmp / "link.url"
urlf.write_text("[InternetShortcut]\r\nURL=https://example.com/v.mp4\r\n",
                encoding="utf-8")
torrentish = tmp / "b.torrent"
torrentish.write_bytes(b"d4:infod")
sub = tmp / "sub"
sub.mkdir()
(sub / "c.torrent").write_bytes(b"d")

urls = dropwin.urls_from_paths([
    "https://example.com/x",
    str(f1),
    str(urlf),
])
check("HTTP 原样保留", "https://example.com/x" in urls)
check("本地文件转 file://", any(u.startswith("file://") and u.endswith("a.zip") for u in urls),
      str(urls)[:80])
check("浏览器 .url 还原成 URL", "https://example.com/v.mp4" in urls)

urls2 = dropwin.urls_from_paths([str(sub)])
check("拖目录只收种子/清单", any(u.endswith("c.torrent") for u in urls2), str(urls2)[:60])
check("空输入不炸", dropwin.urls_from_paths(["", "  "]) == [])

# ----------------------------------------------------------------------
# 2. 悬浮窗
# ----------------------------------------------------------------------
print("== 悬浮窗 ==")
got = []
win = dropwin.DropWindow(lambda u: got.extend(u), x=200, y=200)
started = win.start()
check("窗口创建成功", started, win.error or f"hwnd={win.hwnd}")
if started:
    check("窗口可见", win.visible())
    win.hide()
    check("隐藏生效", not win.visible())
    win.show()
    check("再显示生效", win.visible())
    hwnd = win.hwnd
    win.stop()
    time.sleep(0.3)
    # 真实检查：IsWindow 应该返回 0
    import ctypes
    alive = bool(ctypes.windll.user32.IsWindow(ctypes.c_void_p(hwnd or 0)))
    check("IsWindow 为假（已销毁）", alive is False, f"alive={alive}")

# ----------------------------------------------------------------------
# 3. 右键菜单（测试键名）
# ----------------------------------------------------------------------
print("== 右键菜单 ==")
if os.name == "nt":
    import winreg

    TEST_ROOT = r"Software\Classes\*\shell\JiliuTest"
    REAL = shellmenu._SCOPES
    shellmenu._SCOPES = [
        (TEST_ROOT, "测试文件", '"%1"'),
        (r"Software\Classes\Directory\shell\JiliuTest", "测试目录", '"%1"'),
        (r"Software\Classes\Directory\Background\shell\JiliuTest", "测试空白", "--clipboard"),
    ]
    try:
        r = shellmenu.register()
        check("注册成功", r["ok"], r.get("error", ""))
        st = shellmenu.status()
        check("status 认为已注册", st["registered"])
        check("路径不为 stale", st["stale"] is False, str(st.get("exe")))
        check("菜单文字正确", st["scopes"][0]["registered"])
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_ROOT + r"\command") as k:
            cmd = str(winreg.QueryValueEx(k, "")[0])
        check("命令含 add 与 %1", "add" in cmd and "%1" in cmd, cmd[:70])
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Classes\Directory\Background\shell\JiliuTest\command") as k:
            cmd2 = str(winreg.QueryValueEx(k, "")[0])
        check("空白处命令走 --clipboard", "--clipboard" in cmd2, cmd2[:70])

        # 改掉 JiliuPath 模拟"exe 换了目录"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_ROOT, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "JiliuPath", 0, winreg.REG_SZ, r"C:\old\jiliu.exe")
        check("旧路径被识别为 stale", shellmenu.status()["stale"] is True)
        shellmenu.refresh_if_registered()
        check("refresh 后自愈", shellmenu.status()["stale"] is False)

        u = shellmenu.unregister()
        check("注销清理了 3 项", u["removed"] == 3, str(u))
        check("注销后 status 为未注册", shellmenu.status()["registered"] is False)
    finally:
        shellmenu._SCOPES = REAL
        shellmenu.unregister()
else:
    check("非 Windows 跳过", True)

print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
