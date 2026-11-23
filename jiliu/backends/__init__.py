"""下载后端选择。

汲流有四个 HTTP 下载引擎：

    native   自研多线程分片引擎（零依赖，可断点续传、可限速、可暂停）
    aria2    交给 aria2c —— Motrix 用的就是它；支持 BT / 磁力 / 多源
    curl     系统自带的 curl，兼容性最好（自研下不动的服务端往往它能下）
    wget     断流续传最稳，内网/老系统上常是唯一可装的

选择策略（cfg['backend']）：
    native / aria2 / curl / wget   只用指定的那个
    auto                            按 cfg['engine_order'] 挑第一个可用的，都没有则 native

后两个是**兜底**定位，不是主力：单连接，速度不如多线程。什么时候值得用、
以及各引擎的取舍，统一在 jiliu/engines.py 里维护，这里只负责实例化。
"""
from __future__ import annotations

import pathlib
import threading
from typing import Any, Callable, Dict, List, Optional

from . import aria2 as _aria2
from . import external as _external

VALID = ("native", "aria2", "curl", "wget", "auto")

_service_lock = threading.Lock()
_service: Optional[_aria2.Aria2Service] = None


def describe(cfg: Dict[str, Any], *, refresh: bool = False) -> Dict[str, Any]:
    """给界面用：说明两个后端各自的可用状态。

    注意这里只探一次 aria2，并把结果透传给 resolve()。
    早先的写法是 describe 探一次、resolve 内部又探一次 —— 而 detect()
    含一次 1.5 秒超时的 RPC 连接尝试，面板轮询时等于白白翻倍。
    """
    a = _aria2.detect(cfg, refresh=refresh)
    native_ok = True
    reason = []
    if a["binary"]:
        reason.append(f"aria2c 已安装（{a['binary_version'][:40]}）")
    if a["rpc_ok"]:
        tag = "Motrix" if a.get("motrix") else "RPC"
        reason.append(f"{tag} RPC 可用：{a['rpc_url']}")
    if not reason:
        reason.append("未发现 aria2c，也没有可连接的 RPC 端点")

    return {
        "configured": cfg.get("backend", "auto"),
        "native": {"available": native_ok, "note": "自研多线程分片引擎（零依赖）"},
        "aria2": {**a, "note": "；".join(reason)},
        "effective": resolve(cfg, info=a),
    }


def invalidate_cache() -> None:
    """作废后端探测缓存（装了 aria2c 或刚启动 Motrix 之后调）。"""
    _aria2.invalidate_detect_cache()


def resolve(cfg: Dict[str, Any], info: Optional[Dict[str, Any]] = None) -> str:
    """决定这次实际用哪个后端。返回值一定是 VALID 里的具体引擎。"""
    want = (cfg.get("backend") or "auto").lower()
    if want not in VALID:
        want = "auto"

    if want == "native":
        return "native"
    if want in ("curl", "wget"):
        # 用户明确指定了兜底引擎。这里**不检查是否探测到** ——
        # 探测靠 PATH，而用户可能刚装好还没刷新缓存；让子进程直接试，
        # 真的没有会以「找不到程序」报出来，比在这里拦住更清楚。
        return want

    if info is None:
        info = _aria2.detect(cfg)
    usable = bool(info.get("rpc_ok") or info.get("binary"))

    if want == "aria2":
        if not usable:
            raise RuntimeError(
                "配置要求使用 aria2，但既没找到 aria2c 可执行文件，也连不上 RPC 端点。"
                " 装一个：scoop install aria2"
            )
        return "aria2"

    # auto：按 engine_order 挑第一个可用的。
    # native 永远可用，所以它排在第几，决定了前面那些能不能被自动选中。
    order = [k for k in (cfg.get("engine_order") or ("aria2", "native"))
             if k in ("native", "aria2", "curl", "wget")]
    for k in order:
        if k == "native":
            return "native"
        if k == "aria2" and usable:
            return "aria2"
        if k in ("curl", "wget"):
            from .. import tools as _tools
            if _tools.find(k, cfg.get(f"{k}_path", "")):
                return k
    return "native"


def get_service(cfg: Dict[str, Any]) -> _aria2.Aria2Service:
    """拿到（并在需要时启动）我们自己的 aria2c 服务。"""
    global _service
    with _service_lock:
        if _service is None:
            _service = _aria2.Aria2Service(cfg)
        return _service


def shutdown() -> None:
    global _service
    with _service_lock:
        if _service is not None:
            _service.stop()
            _service = None


def get_client(cfg: Dict[str, Any]) -> "_aria2.Aria2Client":
    """优先连已有 RPC（例如 Motrix）；连不上就自己拉起一个 aria2c。"""
    info = _aria2.detect(cfg)
    if info.get("rpc_ok") and info.get("rpc_url"):
        sec = ""
        if info.get("motrix"):
            sec = cfg.get("motrix_rpc_secret", "") or ""
        else:
            sec = cfg.get("aria2_rpc_secret", "") or ""
        return _aria2.Aria2Client(info["rpc_url"], sec)
    return get_service(cfg).start()


def make_downloader(
    url: str,
    dest: pathlib.Path,
    *,
    cfg: Dict[str, Any],
    threads: Optional[int] = None,
    headers: Optional[Dict[str, str]] = None,
    speed_limit: Optional[int] = None,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    prefer: Optional[str] = None,
    chunk_size: Optional[int] = None,
    verify_hash: bool = False,
    expected_sha256: str = "",
    mirrors: Optional[List[str]] = None,
):
    """按配置返回合适的下载器实例。两者的 run/pause/resume/cancel/snapshot 接口一致。

    注意 chunk_size / verify_hash 是自研引擎特有的能力（按资源类型定制分片、
    下完对哈希）。aria2 后端走它自己的 --split / --checksum，这两个参数会被忽略，
    但会记一条日志，免得用户以为是没生效的 bug。
    """
    effective = resolve(cfg) if not prefer else (
        prefer if prefer in ("aria2", "native", "curl", "wget") else "native"
    )
    if effective == "aria2":
        try:
            client = get_client(cfg)
            if (chunk_size or verify_hash) and log_cb:
                log_cb("aria2 后端下忽略分片策略（由 aria2 自行决定），"
                       "需要按类型定制请设 backend=native")
            return _aria2.Aria2Downloader(
                url, dest, cfg=cfg, client=client, threads=threads,
                headers=headers, speed_limit=speed_limit,
                progress_cb=progress_cb, log_cb=log_cb,
                # 镜像与预期哈希 aria2 原生支持（多 URI + --checksum），
                # 别让它白白丢掉这两个 1.8.0 才加的能力
                mirrors=mirrors,
                expected_sha256=expected_sha256,
            )
        except Exception as exc:  # noqa: BLE001
            if log_cb:
                log_cb(f"aria2 不可用（{exc}），回退到自研引擎")
            if (cfg.get("backend") or "auto").lower() == "aria2":
                raise

    if effective in ("curl", "wget"):
        cls = (_external.CurlDownloader if effective == "curl"
               else _external.WgetDownloader)
        if (chunk_size or verify_hash or expected_sha256) and log_cb:
            log_cb(f"{effective} 是单连接引擎，忽略分片策略；"
                   f"哈希校验会照做")
        return cls(
            url, dest, cfg=cfg, threads=threads, headers=headers,
            speed_limit=speed_limit, progress_cb=progress_cb, log_cb=log_cb,
            verify_hash=verify_hash, expected_sha256=expected_sha256,
        )

    from ..engine import RangeDownloader
    return RangeDownloader(
        url, dest, cfg=cfg, threads=threads, headers=headers,
        speed_limit=speed_limit, progress_cb=progress_cb, log_cb=log_cb,
        chunk_size=chunk_size, verify_hash=verify_hash,
        expected_sha256=expected_sha256, mirrors=mirrors,
    )


def fallback_chain(cfg: Dict[str, Any], first: str) -> list:
    """失败后可以再试哪些引擎（不含第一个）。

    「兜底引擎」的价值只有在这里才体现出来 —— 用户不必先碰到失败、
    再去设置里换一个引擎重试。自研引擎偶尔会撞上挑食的服务端，
    这时候静默换 curl 重来一遍，比让用户对着一个错误码发呆强。

    顺序是刻意的：**native（自研多线程）在 curl/wget 前面**。
    实测教训（2026-09-28 限速源对照）：aria2 失败后若先落到 curl，
    64MB 跑 70 秒（curl 是单连接引擎）；native 8 线程同样的活只要
    8 秒左右。curl/wget 只该做 native 也失败后的最后兜底。
    """
    if not cfg.get("auto_fallback_engine", True):
        return []
    chain = [k for k in ("native", "curl", "wget") if k != first]
    from .. import tools as _tools
    out = []
    for k in chain:
        if k == "native":
            out.append(k)
            continue
        if _tools.find(k, cfg.get(f"{k}_path", "")):
            out.append(k)
    return out
