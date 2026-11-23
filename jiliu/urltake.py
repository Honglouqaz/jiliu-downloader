"""URL 协议唤起（jiliu://）—— 应用没开着也能被网页/浏览器拉起来。

为什么需要这一层
    浏览器扩展能直接调本地 HTTP API，前提是汲流**已经在跑**。
    用户点下载链接的时候，汲流十次有九次是关着的 —— 这时候扩展的
    fetch 会直接 ECONNREFUSED，界面上什么都不会发生，用户只会觉得
    「这扩展没用」。

    唯一可靠的办法是走操作系统的协议处理器：网页里一个
    <a href="jiliu://download?url=..."> 点下去，Windows 按注册表找到
    exe 并把它拉起来，URL 作为参数传进来。这跟迅雷的 thunder://、
    电驴的 ed2k:// 是同一套机制。

两条路径，同一个投递函数
    热：已有实例 → 读运行时端口 → POST /api/tasks → 新进程立刻退出
    冷：没有实例 → 起完整 GUI → serve 起来后自己投递

    两条路都调 deliver()，保证「协议唤起」和「界面里新建任务」走
    完全相同的校验、门禁、去重逻辑。不复制一份建任务的代码，是因为
    那种复制迟早会和主干逻辑分叉（比如加了 edition.gate 却漏了这一处）。

注册表为什么在 HKCU
    写 HKLM 要 UAC。一个下载器为了能用协议唤起而弹权限框，用户会
    直接关掉。HKCU\\Software\\Classes 的优先级比 HKLM 高，够用。

边界
    不做「把自己注册成浏览器的默认下载器」—— 浏览器没有这个扩展点，
    能做到那件事的只有接管浏览器请求（扩展或嗅探代理），已经在做了。
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

SCHEME = "jiliu"
FLAG = "--open-url"
PENDING_ENV = "JILIU_OPEN_URL"
RUNTIME_NAME = "runtime.json"

# 服务默认端口。desktop.pick_port 以它为起点，被占就往后顺延 ——
# 扫描兜底（scan_instance）也以它为起点，两边必须是同一个数。
DEFAULT_PORT = 8788

# 轻量唤起器的主程序名（见 launcher_path 的说明）
LAUNCHER_NAME = "jiliu-wake.exe"

CLASS_KEY = r"Software\Classes\jiliu"
PROTO_LABEL = "URL:汲流下载协议"

# 测试专用后缀。测试会把它设成 "__test"，用完清掉再置空。
#
# 为什么要这个后门：注册表是**全局的**，测试一旦操作真键，就只能靠
# "跑完记得恢复"。上一版正是栽在这里 —— 测试里注销了协议，收尾只在
# "本来就没注册"时才兜底，于是每跑一次测试，本机的 jiliu:// 就没了。
# 症状是"跑完测试，点网页链接彻底没反应"，而且完全看不出是谁干的。
#
# 环境变量那条是给"另起一个进程"的测试用的（verify/ui 脚本都是这样：
# 服务在子进程里，测试改不到它的模块变量）。
_KEY_SUFFIX = os.environ.get("JILIU_URIKEY_SUFFIX", "")


def _key(sub: str = "") -> str:
    """注册表路径。生产路径永远等于 CLASS_KEY，测试挂到后缀键上。"""
    base = CLASS_KEY + _KEY_SUFFIX
    return base + ("\\" + sub if sub else "")

# 只认这些 scheme。别的（尤其 javascript: / data:）从网页点进来就是
# 攻击面 —— 协议参数是外部可控输入，必须白名单。
_ALLOWED = ("http", "https", "ftp", "magnet", "ed2k", "thunder",
            "qqdl", "flashget", "file")


# ----------------------------------------------------------------------
# 运行时端口记录：给「已有实例」这条热路径用
# ----------------------------------------------------------------------

def runtime_path() -> pathlib.Path:
    from . import config as _config
    return _config.HOME / RUNTIME_NAME


def write_runtime(port: int, pid: Optional[int] = None) -> Dict[str, Any]:
    """服务起来之后落一份「我在哪」的凭据。

    用临时文件 + replace 保证原子性：写到一半被读到会得到半截 JSON，
    热路径就会误判「没有实例」然后开出第二个窗口。
    """
    path = runtime_path()
    info = {"port": int(port), "pid": int(pid or os.getpid()),
            "version": "", "at": time.time()}
    try:
        from . import __version__
        info["version"] = __version__
    except Exception:      # noqa: BLE001
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:      # noqa: BLE001
        return {"ok": False, "error": "写不了运行时文件"}
    return {"ok": True, **info}


def read_runtime() -> Optional[Dict[str, Any]]:
    """读端口记录。文件不存在 / 内容坏了 / 进程已死都返回 None。"""
    try:
        raw = runtime_path().read_text(encoding="utf-8")
        info = json.loads(raw)
        port = int(info.get("port") or 0)
        pid = int(info.get("pid") or 0)
    except Exception:      # noqa: BLE001
        return None
    if not port:
        return None
    # pid 还活着吗？活着才值得复用 —— 否则是上次异常退出留下的残骸，
    # 照着它转发只会等一个 1 秒超时，白白拖慢冷启动。
    if pid and not _pid_alive(pid):
        return None
    return info


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)     # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_ulong(0)
                if k.GetExitCodeProcess(h, ctypes.byref(code)):
                    # 259 = STILL_ACTIVE
                    return code.value == 259
                return True
            finally:
                k.CloseHandle(h)
        except Exception:      # noqa: BLE001
            return True        # 查不了就当活着，交给 HTTP 探测兜底
    try:
        os.kill(pid, 0)
        return True
    except Exception:          # noqa: BLE001
        return False


def clear_runtime(pid: Optional[int] = None) -> None:
    """清掉端口记录。

    带 pid 时只在「记录的是我自己」才删：同一台机器开两个实例是允许的
    （端口自动顺延），否则先退出的那个会把另一个的记录删掉，
    之后的协议唤起就找不到活着的实例了。
    """
    if pid is not None:
        try:
            raw = json.loads(runtime_path().read_text(encoding="utf-8"))
            if int(raw.get("pid") or 0) not in (0, int(pid)):
                return
        except Exception:      # noqa: BLE001
            return
    try:
        runtime_path().unlink()
    except Exception:          # noqa: BLE001
        pass


# ----------------------------------------------------------------------
# 协议串解析
# ----------------------------------------------------------------------

def build(url: str, **extra: Any) -> str:
    """把链接包成 jiliu:// 串。扩展和文档里的示例都用这个，别手拼。"""
    q = {"url": url}
    for k, v in extra.items():
        if v not in (None, ""):
            q[k] = str(v)
    return f"{SCHEME}://download?{urllib.parse.urlencode(q)}"


def parse(spec: str) -> Dict[str, Any]:
    """解析协议串，返回 {ok, url, referer, title, kind, headers}。

    兼容三种写法：
        jiliu://download?url=<enc>&referer=<enc>&title=<enc>
        jiliu://?url=<enc>
        jiliu:https://example.com/a.zip     （opaque，直接跟原链接）

    参数必须 percent 解码：链接本身带 ? 和 &，不编码的话会被协议串
    自己的 query 解析吃掉后半段（真实踩过的坑）。
    """
    out: Dict[str, Any] = {"ok": False, "url": "", "referer": "", "title": "",
                           "kind": "", "headers": {}, "raw": spec, "error": "",
                           "wake": False}
    raw = (spec or "").strip().strip('"')
    if not raw:
        out["error"] = "空链接"
        return out

    # 裸链接（不经过协议）：右键菜单、命令行直接喂进来时就是这种
    low = raw.lower()
    if not low.startswith(f"{SCHEME}:"):
        if _scheme_of(raw) in _ALLOWED:
            out.update(ok=True, url=raw)
            return out
        out["error"] = f"不认识的链接：{raw[:60]}"
        return out

    body = raw[len(SCHEME) + 1:]
    # jiliu:https://... —— 冒号后直接就是原链接
    if body[:2] == "//":
        body = body[2:]
    if not body:
        out["error"] = "协议串里没有内容"
        return out

    # 只唤醒：jiliu://wake。
    # 扩展用它把应用拉起来，但不带任何链接 —— 真正的任务由扩展在
    # 服务起来之后走本地 HTTP 补发，那样 Cookie 不必写进协议串
    #（协议串会进浏览器历史和系统日志）。
    if body.strip("/").lower() in ("wake", "open", "launch", "wakeup"):
        out.update(ok=False, wake=True, error="")
        return out

    # opaque 形式：jiliu:https://x.com/a
    if _scheme_of(body) in _ALLOWED:
        out.update(ok=True, url=body)
        return out

    # 标准形式：authority[/path]?query
    qs = ""
    if "?" in body:
        _, qs = body.split("?", 1)

    # 自己拆 query，不用 parse_qs —— 它对 `+` 的处理是表单语义
    #（把 + 当空格），而 URL 里的 + 是合法字符（GitHub release 的
    # 文件名里就常有）。用 parse_qs 的话：
    #   ...?url=https%3A%2F%2Fa.com%2Fx%2By.zip   会变成  x y.zip
    # 下下来的文件名莫名其妙多个空格，而且改名后对不上服务器。
    raw: Dict[str, List[str]] = {}
    for part in qs.split("&"):
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        raw.setdefault(urllib.parse.unquote(k), []).append(v)

    def _one(name: str, *, space_plus: bool = True) -> str:
        v = raw.get(name) or []
        if not v:
            return ""
        dec = urllib.parse.unquote_plus if space_plus else urllib.parse.unquote
        return (dec(v[0]) or "").strip()

    url = (_one("url", space_plus=False) or _one("u", space_plus=False)
           or _one("href", space_plus=False))
    if not url:
        out["error"] = "协议串里没有 url 参数"
        return out
    if _scheme_of(url) not in _ALLOWED:
        out["error"] = f"不支持的链接类型：{_scheme_of(url) or '?'}"
        return out

    out.update(ok=True, url=url,
               referer=_one("referer") or _one("ref"),
               title=_one("title") or _one("name"),
               kind=_one("kind"))
    hdrs: Dict[str, str] = {}
    for k in ("cookie", "user_agent", "origin"):
        v = _one(k)
        if v:
            hdrs["User-Agent" if k == "user_agent" else k.title()] = v
    if out["referer"]:
        hdrs.setdefault("Referer", out["referer"])
    out["headers"] = hdrs
    return out


def _scheme_of(url: str) -> str:
    m = url.split(":", 1)
    if len(m) < 2 or "/" in m[0] or not m[0].strip():
        return ""
    return m[0].strip().lower()


def split_argv(argv: List[str]) -> Tuple[str, List[str]]:
    """从命令行参数里摘出 --open-url 的目标，返回 (目标, 其余参数)。

    不能用 argparse：值是 `jiliu://download?url=...`，带 `?` 和 `&`，
    交给 argparse 会被当成位置参数，或者直接报"无此选项"。

    为什么放在 urltake 而不是各自入口里：主程序（jiliu.py）和轻量
    唤起器（jiliu_wake.py）解析的是同一个参数。两份实现迟早分叉 ——
    比如以后加个 `--open-url=` 的等价写法，只改了一处，另一处就
    静默失效，表现是"从网页点能唤起，从命令行点不行"。
    """
    rest: List[str] = []
    spec = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == FLAG and i + 1 < len(argv):
            spec = argv[i + 1]
            i += 2
            continue
        if a.startswith(FLAG + "="):
            spec = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    return spec, rest


# ----------------------------------------------------------------------
# 投递：热路径（已有实例）与冷路径（刚起来）共用
# ----------------------------------------------------------------------

def payload_of(info: Dict[str, Any], *, source: str = "protocol") -> Dict[str, Any]:
    p: Dict[str, Any] = {"url": info["url"], "from": source}
    if info.get("title"):
        p["title"] = info["title"]
    if info.get("kind"):
        p["kind"] = info["kind"]
    if info.get("referer"):
        p["referer"] = info["referer"]
    if info.get("headers"):
        p["headers"] = info["headers"]
    return p


def deliver(info: Dict[str, Any], port: int, *, timeout: float = 8.0,
            source: str = "protocol") -> Dict[str, Any]:
    """POST /api/tasks 到指定端口。返回服务端的响应（含 ok）。"""
    if not info.get("ok"):
        return {"ok": False, "error": info.get("error") or "链接无效"}
    body = json.dumps(payload_of(info, source=source), ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{int(port)}/api/tasks", data=body,
        headers={"Content-Type": "application/json"})
    # 关掉代理：本机请求走代理是经典坑（环境里 HTTP_PROXY 常常是死的），
    # 表现是 WinError 10053，看着像服务端崩了。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}")
        except Exception:      # noqa: BLE001
            return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:   # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def ping(port: int, timeout: float = 1.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{int(port)}/api/health",
                         timeout=timeout) as r:
            return r.status == 200
    except Exception:          # noqa: BLE001
        return False


def probe_app(port: int, timeout: float = 0.3) -> bool:
    """这个端口上跑的是不是汲流。

    只看 HTTP 200 不够：8788 这一段一堆本地工具都爱用，认错了会把
    用户的下载任务 POST 给一个不相干的程序 —— 人家照样回 200，
    任务却凭空消失。所以读 /api/health 里的 app 字段确认身份。
    """
    # 先裸 socket 试一下通不通。本机未监听端口在部分机器上不是立刻
    # 拒绝而是**等超时**（实测 1 秒），走 urllib 还要多背一层 opener
    # 的开销；连不上就直接返回，省掉这部分。
    try:
        s = socket.socket()
        s.settimeout(min(0.12, timeout))
        try:
            s.connect(("127.0.0.1", int(port)))
        finally:
            try:
                s.close()
            except OSError:
                pass
    except OSError:
        return False

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{int(port)}/api/health",
                         timeout=timeout) as r:
            if r.status != 200:
                return False
            raw = r.read(4096).decode("utf-8", "replace")
    except Exception:          # noqa: BLE001
        return False
    try:
        info = json.loads(raw)
    except Exception:          # noqa: BLE001
        return False
    app = str(info.get("app") or "")
    return bool(info.get("ok")) and ("汲" in app or "jiliu" in app.lower())


def scan_instance(span: int = 16, start: int = 0) -> int:
    """扫一段端口把活着的实例找出来，找不到返回 0。

    两个场景需要它：
      - runtime.json 丢了（用户清过 data 目录，或从没写过）
      - runtime.json 里的 pid 已经变了（被强杀后重启，pid 对不上）

    这两种情况下实例其实在跑，只认 runtime.json 就会再开一个 ——
    两个进程抢同一份 tasks.json，后写的覆盖先写的，用户看到的是
    「任务凭空消失」。desktop.pick_port 在 8788 被占时会往后顺延，
    所以还得往里扫一段，不能只试 8788。

    **并行探，不串行。** 装了些安全软件的机器上，连本机未监听端口
    不是立刻拒绝而是**等超时**（实测一个端口 1 秒）；串行扫 16 个就是
    十几秒白等，用户看到的是"点了链接半天没反应"。并行之后总耗时等于
    最慢的那一个，命中的那个一回来就立刻返回。

    start=0 表示"从默认端口扫，配置端口非默认时也一起扫"。
    两个起点而不是一个：配置读失败、或者进程里的 cfg 被别的代码改过，
    只认一个起点就是单点故障 —— 兜底的代码自己再出一次错，整条路就没了。
    """
    todo = _scan_candidates(span, start)
    if not todo:
        return 0
    winner: List[int] = []
    stop = threading.Event()

    def _probe(p: int) -> None:
        if stop.is_set():
            return
        try:
            if probe_app(p):
                winner.append(p)
                stop.set()
        except Exception:      # noqa: BLE001
            pass

    threads = []
    for p in todo:
        t = threading.Thread(target=_probe, args=(p,), daemon=True)
        t.start()
        threads.append(t)

    # 命中立刻走；全都没命中就等最后一个探完（都失败时不该再干等满超时）
    deadline = time.time() + 1.2
    while time.time() < deadline:
        if winner:
            break
        if not any(t.is_alive() for t in threads):
            break
        time.sleep(0.02)
    stop.set()
    return min(winner) if winner else 0


def _scan_candidates(span: int, start: int) -> List[int]:
    """要探哪些端口。去重 + 保持顺序（端口小的先返回，结果稳定）。"""
    if start:
        todo = [int(start) + i for i in range(max(1, int(span)))]
    else:
        starts = [DEFAULT_PORT]
        try:
            from . import config as _config
            p = int((_config.load() or {}).get("port") or DEFAULT_PORT)
            if p != DEFAULT_PORT:
                starts.append(p)
        except Exception:      # noqa: BLE001
            pass
        todo = []
        for s in starts:
            todo += [s + i for i in range(max(1, int(span)))]
    out: List[int] = []
    for p in todo:
        if p not in out:
            out.append(p)
    return out


def handoff(info: Dict[str, Any], *, scan: bool = True) -> Dict[str, Any]:
    """交给已有实例。没有实例、或投递失败，都返回 ok=False 让调用方冷启动。

    scan=False 关掉端口扫描，只认 runtime.json。测试用它强制走冷路径 ——
    本机正好开着汲流时，扫描必然命中，那条分支就永远测不到。
    """
    port = 0
    rt = read_runtime()
    if rt:
        port = int(rt["port"])
        # 先 ping 确认真的活着：runtime 文件可能是崩溃残留，
        # 直接投递要等满 8 秒超时，冷启动就被拖成"点了没反应"。
        if not ping(port, timeout=0.8):
            clear_runtime()
            port = 0
    if not port and scan:
        port = scan_instance()
    if not port:
        return {"ok": False, "reason": "no-instance"}
    res = deliver(info, port, timeout=6.0, source="protocol")
    if res.get("ok"):
        return {"ok": True, "mode": "reused", "port": port, **res}
    return {"ok": False, "reason": "rejected", **res}


# ----------------------------------------------------------------------
# 冷路径：GUI 起来之后把待办投出去
# ----------------------------------------------------------------------

def pending_spec() -> str:
    return (os.environ.get(PENDING_ENV) or "").strip()


def consume_pending(port: int, *, delay: float = 0.6) -> Optional[threading.Thread]:
    """把 JILIU_OPEN_URL 里的待办投给刚起来的自己。

    起线程 + 轻微延迟，是为了别挡住窗口创建：窗口先出现、任务随后进队列，
    用户看到的是"应用开了并且在下载"；反过来先建任务再出窗口，
    那 1~2 秒里用户会盯着桌面以为没反应。
    """
    spec = pending_spec()
    if not spec:
        return None
    # 立刻清掉，避免同进程里再走一次 serve 时重复投递
    os.environ.pop(PENDING_ENV, None)

    def _run() -> None:
        if delay:
            time.sleep(delay)
        info = parse(spec)
        deliver(info, port, timeout=20.0, source="protocol")
        # 投递成功的记录由服务端 _json 出口统一写（那里才拿得到真实 task id）。
        # 只有"解析就失败"要在这里补记：那种请求根本没发出去，
        # 服务端无从知道，界面上就成了"点了链接没反应"。
        if not info.get("ok"):
            try:
                from . import server as _server
                _server.note_protocol_open(info, {"ok": False,
                                                 "error": info.get("error")})
            except Exception:      # noqa: BLE001
                pass

    th = threading.Thread(target=_run, daemon=True, name="jiliu-open-url")
    th.start()
    return th


# ----------------------------------------------------------------------
# 注册表
# ----------------------------------------------------------------------

def exe_path() -> str:
    """注册表里该指向谁。冻结 = 自己；源码 = python + jiliu.py。"""
    if getattr(sys, "frozen", False):
        return str(pathlib.Path(sys.executable).resolve())
    root = pathlib.Path(__file__).resolve().parent.parent
    return f'"{sys.executable}" "{root / "jiliu.py"}"'


def launcher_path() -> str:
    """同目录下的轻薄唤起器，有就返回绝对路径，没有返回 ""。

    为什么要有个单独的 exe
        注册表里登记的可执行文件，用户每点一次网页链接就要启动一次。
        主程序是约 25MB 的 onefile，PyInstaller 每次启动都得把内容解包到
        临时的 _MEI* 目录 —— 实测 4.1 秒（同一份内容打成 onedir 只要
        1.15 秒，差值就是解包）。再加上服务端探测慢站点还要两秒多，
        用户点完链接盯着浏览器等六秒，只会得出「这软件没反应」的结论。

        jiliu-wake.exe 只做「读 runtime.json → POST 一个任务 → 退出」，
        依赖只有 stdlib，打包 10.3MB，实测 2.5–2.9 秒。
    """
    if not getattr(sys, "frozen", False):
        return ""
    p = pathlib.Path(sys.executable).resolve().parent / LAUNCHER_NAME
    return str(p) if p.is_file() else ""


def handler_path() -> str:
    """注册表 shell\\open\\command 里真正该用的那个可执行文件。

    优先级：launcher > 主程序。退回主程序是为了兼容「手工从旧包里拷了
    主程序、没带 launcher」的目录 —— 那样虽然慢，但功能是通的。
    源码运行时没有 launcher 这回事，返回 ""（调用方按源码形态处理）。
    """
    if not getattr(sys, "frozen", False):
        return ""
    return launcher_path() or str(pathlib.Path(sys.executable).resolve())


def _handler_or_exe() -> str:
    """写注册表用的那个路径（冻结态优先 launcher，源码态退回 exe_path()）。"""
    return handler_path() or exe_path()


def _command() -> str:
    if not getattr(sys, "frozen", False):
        root = pathlib.Path(__file__).resolve().parent.parent
        return f'"{sys.executable}" "{root / "jiliu.py"}" {FLAG} "%1"'
    return f'"{handler_path()}" {FLAG} "%1"'


def register() -> Dict[str, Any]:
    """写入 jiliu:// 协议处理器。返回 {ok, command, error}。"""
    if os.name != "nt":
        return {"ok": False, "error": "只有 Windows 支持协议唤起"}
    import winreg

    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _key(), 0,
                                winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, PROTO_LABEL)
            # 有这个空值，Windows 才认为它是"可被外部调起的协议"。
            # 少了它，注册表看着有项，点链接却没反应 —— 最难查的一种。
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
            # 两个值分开记，语义不能混：
            #   JiliuPath    主程序在哪 —— 唤起器找不到同目录的 jiliu.exe
            #                时会读它去启动主程序。这里如果写唤起器自己，
            #                唤起器就会启动自己，一路递归下去。
            #   JiliuHandler 处理程序是谁 —— 用来判定陈旧（1.9.3 之前
            #                写的是主程序，升级后应自动改写成 launcher）。
            winreg.SetValueEx(k, "JiliuPath", 0, winreg.REG_SZ, exe_path())
            winreg.SetValueEx(k, "JiliuHandler", 0, winreg.REG_SZ, _handler_or_exe())
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                _key(r"DefaultIcon"), 0,
                                winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ,
                              _handler_or_exe().strip('"') + ",0")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                _key(r"shell\open\command"), 0,
                                winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _command())
    except Exception as exc:   # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _STATUS_CACHE.update(at=0.0, data=None)      # 刚写完，下次读必须是新的
    return {"ok": True, "command": _command(), "exe": exe_path(),
            "handler": _handler_or_exe()}


def unregister() -> Dict[str, Any]:
    if os.name != "nt":
        return {"ok": False, "error": "只有 Windows 支持"}
    import winreg

    # 逐个删，由深到浅 —— DeleteKey 要求子键先空
    for path in (_key(r"shell\open\command"),
                 _key(r"shell\open"),
                 _key(r"shell"),
                 _key(r"DefaultIcon"),
                 _key()):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    _STATUS_CACHE.update(at=0.0, data=None)
    return {"ok": True}


_STATUS_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}


def status(*, cached: bool = False) -> Dict[str, Any]:
    """注册状态 + 是否过期（exe 换了位置/换了版本目录）。

    cached=True 走 3 秒缓存：/api/state 每 2 秒轮询一次，每次都读注册表
    属于无意义的开销（虽然单次只有微秒级，但没必要）。
    """
    if cached and _STATUS_CACHE["data"] and time.time() - _STATUS_CACHE["at"] < 3.0:
        return _STATUS_CACHE["data"]       # type: ignore[return-value]
    data = _status_uncached()
    _STATUS_CACHE.update(at=time.time(), data=data)
    return data


def _status_uncached() -> Dict[str, Any]:
    if os.name != "nt":
        return {"supported": False, "registered": False, "stale": False,
                "command": "", "exe": "", "scheme": SCHEME + "://"}
    import winreg

    want_exe = exe_path()
    want_handler = _handler_or_exe()
    out: Dict[str, Any] = {"supported": True, "registered": False,
                           "stale": False, "command": "", "exe": want_exe,
                           "handler": handler_path(),
                           "scheme": SCHEME + "://", "target": "",
                           "handler_registered": ""}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            _key(r"shell\open\command")) as k:
            out["command"] = str(winreg.QueryValueEx(k, "")[0])
            out["registered"] = True
    except Exception:          # noqa: BLE001
        return out

    # handler 用**注册表实际命令里的那个 exe**，不是"按当前运行形态
    # 算出来该指向谁"。源码态下后者恒为空（源码没有独立唤起器），
    # 界面于是显示"处理器：（空）" —— 用户看到就以为坏了，而实际上
    # 注册表里指向的是好好的 jiliu-wake.exe。
    _cmd = out["command"]
    _q = _cmd.split('"')
    if len(_q) >= 3:
        out["handler"] = _q[1]
    elif _cmd:
        out["handler"] = _cmd.split()[0]

    # 陈旧判定看两个值：主程序路径变了（挪目录/重装），或者处理程序
    # 还是老的主程序（1.9.3 之前的注册项没有 JiliuHandler，就用
    # JiliuPath 顶上）。任一不符就重写一遍，用户不用手动重新注册。
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _key()) as k:
            out["target"] = str(winreg.QueryValueEx(k, "JiliuPath")[0])
            try:
                old_handler = str(winreg.QueryValueEx(k, "JiliuHandler")[0])
            except OSError:
                old_handler = out["target"]      # 老版本：处理程序就是主程序
            out["handler_registered"] = old_handler
            out["stale"] = (out["target"] != want_exe) or (old_handler != want_handler)
    except Exception:          # noqa: BLE001
        first = out["command"].split('"')
        target = first[1] if len(first) > 1 else out["command"].split()[0]
        out["stale"] = target.strip('"').lower() != want_handler.replace('"', "").lower()
    return out


def refresh_if_registered() -> Dict[str, Any]:
    """启动时校正：注册过但指向旧路径（更新 / 挪目录），重写一遍。

    只在**打包形态**下校正。源码态跑 `python jiliu.py serve` 时，
    exe_path() 返回的是「python 解释器 + jiliu.py」，跟注册表里指向的
    jiliu-wake.exe 永远对不上 —— 一次 stale 判定就会把用户的注册项
    改写成源码路径，之后点网页链接去拉 Python 解释器，用户看到的是
    「点了闪一下什么都没发生」。开发机上跑一遍命令就把功能弄坏，
    成本太高，所以这里直接不动。
    """
    if not getattr(sys, "frozen", False):
        return {"ok": True, "skipped": True, "reason": "source-run"}
    st = status()
    if st.get("registered") and st.get("stale"):
        return register()
    return {"ok": True, "skipped": True}


def ensure_registered(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """首次启动自动注册，之后按配置尊重用户选择。

    默认开启：这个能力没有副作用（HKCU、不需要管理员），而关掉之后
    「点链接没反应」的体验极差 —— 用户不会去设置页翻开关，只会卸载。
    """
    if not cfg.get("url_protocol_auto", True):
        return {"ok": True, "skipped": True}
    if not getattr(sys, "frozen", False):
        # 源码态不自动注册（理由同 refresh_if_registered）：写进去的会是
        # 「python 解释器 + jiliu.py」，用户点链接拉起来的是解释器。
        # 开发机上跑一次 `python jiliu.py serve` 就把功能弄坏，太贵。
        return {"ok": True, "skipped": True, "reason": "source-run"}
    st = status()
    if not st.get("supported"):
        return {"ok": False, "error": "当前系统不支持"}
    if st.get("registered") and not st.get("stale"):
        return {"ok": True, "already": True}
    return register()


# ----------------------------------------------------------------------
# 给界面用的说明
# ----------------------------------------------------------------------

def describe() -> Dict[str, Any]:
    """给设置页与文档取文案，避免前后端各写一份说法。

    registered / stale 在顶层再铺一份：界面里到处写
    `UT.status.registered` 又长又容易漏问号，而且 status 是可选结构，
    某个分支拿不到时整块渲染就变成"未注册"（明明注册着）。
    """
    st = status(cached=True)
    return {
        "scheme": SCHEME + "://",
        "flag": FLAG,
        "examples": [
            {"title": "标准形式（推荐）",
             "text": build("https://example.com/file.zip",
                           referer="https://example.com/page")},
            {"title": "只带链接",
             "text": f"{SCHEME}://download?url=https%3A%2F%2Fexample.com%2Fa.zip"},
            {"title": "冒号直连",
             "text": f"{SCHEME}:https://example.com/a.zip"},
        ],
        "limits": [
            "浏览器首次调用会弹一次「是否打开汲流」，勾选记住之后不再问",
            "协议串是外部输入，只接受 http/https/ftp/magnet/ed2k 等白名单",
            "应用未运行时会冷启动，冷启动约 2~4 秒（含窗口初始化）",
        ],
        "status": st,
        "registered": bool(st.get("registered")),
        "stale": bool(st.get("stale")),
    }
