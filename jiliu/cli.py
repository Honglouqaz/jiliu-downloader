"""命令行界面。

    python jiliu.py serve                      启动面板与 API
    python jiliu.py add <url>                  直接下载（不开服务）
    python jiliu.py video <url>                解析并下载视频
    python jiliu.py playlist <url>             展开合集，列出条目
    python jiliu.py protocol <url>              认协议（磁力/迅雷/ed2k/种子/网盘）
    python jiliu.py bili [status|login|logout]  B站登录态
    python jiliu.py cookies [list|import|...]  Cookie 管理
    python jiliu.py history [list|rm|clear]    已下载记录
    python jiliu.py naming [--pattern ...]     命名模板
    python jiliu.py proxy [--test URL]         代理状态
    python jiliu.py classify <url>              识别资源类型与接管策略
    python jiliu.py probe <url>                探测资源信息
    python jiliu.py tools                       查看外部工具状态
    python jiliu.py config                      查看/修改配置
    python jiliu.py list                        看已保存的任务
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any, Dict, List

from . import (APP_NAME, __version__, backends, bili, config as _config,
               cookies, desktop, edition, engines, layout, naming, protocols,
               proxy, qr, resource, sites, tools, video)
from .engine import human_size, probe as http_probe, unique_path


# --------------------------------------------------------------------------
# 提示
# --------------------------------------------------------------------------

def _inform(title: str, text: str) -> None:
    """给用户一句话提示。

    windowed 打包（1.7.1 起）下没有控制台，`print` 是写进哑设备的 ——
    用户什么都看不到。所以能弹窗就弹窗，弹不出来才退回 print。
    只用在「本来该发生的事没发生」这种必须让用户知道的场合。
    """
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x00000040)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        print(text)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 进度打印
# --------------------------------------------------------------------------

class LineProgress:
    """单行原地刷新的进度条。"""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix
        self.last = 0.0
        self.peak = 0.0

    def __call__(self, s: Dict[str, Any]) -> None:
        now = time.time()
        if now - self.last < 0.15 and (s.get("percent") or 0) < 100:
            return
        self.last = now
        pct = s.get("percent") or 0
        speed = s.get("speed") or 0
        self.peak = max(self.peak, speed)
        size = s.get("size")
        done = s.get("done") or 0
        eta = s.get("eta_text") or s.get("eta") or "-"
        bar_w = 26
        filled = int(bar_w * min(pct, 100) / 100)
        bar = "█" * filled + "·" * (bar_w - filled)
        total = human_size(size) if size else "未知"
        seg = s.get("segments_created") or s.get("segments_inflight")
        segtxt = f" 分片{seg}" if seg else ""
        sys.stdout.write(
            f"\r  {self.prefix}[{bar}] {pct:6.2f}%  {human_size(done)}/{total}"
            f"  {human_size(speed)}/s  ETA {eta}{segtxt}   "
        )
        sys.stdout.flush()

    def finish(self) -> None:
        sys.stdout.write("\r" + " " * 110 + "\r")
        sys.stdout.flush()


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def cmd_serve(args) -> int:
    from . import server

    cfg = _config.load()
    if args.port:
        cfg["port"] = args.port
    # serve 的定位是「显式要一个面板服务」。默认入口已经是 gui（桌面窗口），
    # 所以这里不再自动开浏览器 —— 想开就显式 --open-browser。
    open_browser = bool(args.open_browser)
    try:
        httpd, mgr = server.serve(cfg, open_browser=open_browser)
    except RuntimeError as exc:
        print(f"  启动失败：{exc}")
        return 1
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n  正在停止…")
        mgr.shutdown()
        httpd.shutdown()
    return 0


def cmd_gui(args) -> int:
    """开桌面窗口。这是打包后的默认入口。"""
    cfg = _config.load()
    if getattr(args, "port", None):
        cfg["port"] = args.port
    mode = (cfg.get("window_mode") or "auto").strip().lower()

    # 明确要求浏览器，或者这台机器开不了原生窗口 —— 退回 serve。
    # 注意「要求 native 但不可用」是错误，不能悄悄降级：
    # 用户选了 native 就是不想看到浏览器。
    if getattr(args, "browser", False):
        return cmd_serve(argparse.Namespace(
            port=cfg.get("port"), open_browser=True, no_browser=False))

    # frozen（双击 exe）路径：不做 eager 的 available() 检查 ——
    # 那会让 webview 的导入（frozen 下 ~1.8s）挡在 HTTP 服务前面，
    # 面板晚 1.8s 才能响应。改成：先在后台预热 webview 导入，
    # 直接进 run()（run 自己先起服务再等预热结果）。
    # webview 真不可用（缺 WebView2 等）时 run 会抛 RuntimeError，
    # 在这里兜底降级到浏览器 —— 行为和原来的 available() 分支一致。
    if getattr(sys, "frozen", False):
        desktop.prewarm()
        try:
            rc = desktop.run(cfg, port=cfg.get("port"))
        except RuntimeError:
            reason = desktop.unavailable_reason()
            if mode == "native":
                _inform("汲流 · 打不开桌面窗口",
                        f"配置要求用原生窗口，但本机开不了：\n\n{reason}")
                return 1
            # windowed 打包没有控制台，一定要弹窗说清楚 —— 否则用户只看到
            # "什么都没发生，浏览器自己开了"，完全不知道为什么。
            _inform("汲流 · 改用浏览器打开",
                    f"本机开不了桌面窗口，已改用浏览器打开面板。\n\n原因：{reason}")
            return cmd_serve(argparse.Namespace(
                port=cfg.get("port"), open_browser=True, no_browser=False))
        return int(rc or 0)

    # 源码模式保留原来的可用性检查与打印（控制台还在，打印有人看）。
    if not desktop.available():
        if mode == "native":
            print("  配置要求用原生窗口，但本机开不了：")
            print("   " + desktop.unavailable_reason())
            return 1
        print("  本机开不了原生窗口：" + desktop.unavailable_reason())
        print("  改为用浏览器打开面板。")
        return cmd_serve(argparse.Namespace(
            port=cfg.get("port"), open_browser=True, no_browser=False))

    # backends.resolve 要 spawn aria2c/连 RPC（实测 ~1.6s），
    # 纯粹为了打印一行字去拖慢启动不值得 —— 引擎信息面板自己会拉。
    print(f"  {APP_NAME} v{__version__}  正在打开桌面窗口…")
    print(f"  状态目录 {_config.HOME}"
          + ("   （便携模式）" if _config.is_portable() else ""))
    print(f"  版本形态 {edition.caps(cfg)['name']} · 下载引擎 "
          f"{backends.resolve(cfg)}")
    rc = desktop.run(cfg, port=cfg.get("port"))
    print("  已退出")
    return int(rc or 0)


def cmd_engines(args) -> int:
    cfg = _config.load()
    info = engines.describe(cfg, refresh=getattr(args, "refresh", False))
    if getattr(args, "json", False):
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    print(f"  HTTP 引擎优先级：{' → '.join(info['order'])}")
    print(f"  当前实际使用：{info['effective_http']}")
    print(f"  视频解析：{'可用' if info['video_ready'] else '不可用（需要 yt-dlp）'}"
          f"   合流：{'可用' if info['mux_ready'] else '不可用（需要 ffmpeg）'}")
    print()
    for e in info["engines"]:
        mark = "OK " if e.get("available") else "-- "
        ver = (e.get("version") or "")[:40]
        print(f"  {mark}{e['label']:<14} {e.get('tagline', '')}")
        if ver:
            print(f"      {ver}")
        if e.get("path"):
            print(f"      {e['path']}")
    return 0


def cmd_sites(args) -> int:
    if getattr(args, "json", False):
        print(json.dumps(sites.describe(), ensure_ascii=False, indent=2))
        return 0
    rows = sites.SITES
    if getattr(args, "domestic", False) and not getattr(args, "foreign", False):
        rows = sites.domestic_sites()
    elif getattr(args, "foreign", False) and not getattr(args, "domestic", False):
        rows = sites.foreign_sites()
    print(f"  共 {len(rows)} 个站点"
          f"（国内 {len(sites.domestic_sites())} / 海外 {len(sites.foreign_sites())}）")
    print()
    for s in rows:
        flag = "国内" if s.get("domestic") else "海外"
        print(f"  [{flag}] {s['name']:<12} {'、'.join(s['hosts'][:2])}")
        if s.get("auth"):
            print(f"          登录态：{s['auth']}")
        if s.get("note"):
            print(f"          说明：{s['note']}")
    return 0


def cmd_layout(args) -> int:
    cfg = _config.load()
    base = _config.download_dir(cfg)
    if getattr(args, "json", False):
        out = layout.describe(cfg)
        out["base"] = str(base)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"  下载目录 {base}")
    print(f"  自动归档 {'已开启' if layout.enabled(cfg) else '已关闭'}")
    print()
    print("  " + layout.describe(cfg)["sample"].replace("\n", "\n  "))
    print()
    url = getattr(args, "url", None)
    if url:
        plan = resource.classify(url=url).to_dict()
        cat = plan.get("category") or "other"
        print(f"  链接 {url[:60]}")
        print(f"  识别为 {plan.get('label')}（{cat}）")
        print(f"  将保存到 {layout.target_dir(base, cat, cfg)}")
    elif getattr(args, "category", None):
        cat = args.category
        print(f"  分类 {cat} → {layout.target_dir(base, cat, cfg)}")
    return 0


def cmd_qr(args) -> int:
    """把一段文本渲染成二维码 PNG。

    存在的意义有两个：
      1）调试用 —— B站扫码登录出问题时，可以直接看后端到底编码出了什么；
      2）**打包校验用** —— `qrcode` 库有没有真的进 exe，只有真跑一次才知道。
         `/api/*` 接口在库缺失时照样返回 200（只是内容是空串），
         光看状态码是查不出来的。
    """
    text = args.text
    if not qr.available():
        print(f"  本机不能生成二维码：{qr.unavailable_reason()}")
        print("  装一个：pip install qrcode pypng")
        return 1

    if getattr(args, "matrix", False):
        m = qr.matrix(text, border=int(args.border))
        print(f"  矩阵 {len(m)}x{len(m)}（# = 黑块，. = 白）")
        for row in m:
            print("  " + "".join("##" if v else ".." for v in row))
        return 0

    png = qr.png_bytes(text, box_size=int(args.box), border=int(args.border))
    if args.out:
        path = pathlib.Path(args.out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        print(f"  已写入 {path}（{len(png)} 字节）")
    else:
        b64 = qr.png_base64(text, box_size=int(args.box),
                                border=int(args.border))
        print(b64)
    return 0


def cmd_edition(args) -> int:
    cfg = _config.load()
    want = getattr(args, "set", None)
    if want:
        if want not in edition.EDITIONS:
            print(f"  未知版本 {want}，可选：{'、'.join(edition.EDITIONS)}")
            return 1
        cfg = _config.update({"edition": want})
        print(f"  已切换到 {edition.caps(cfg)['name']}（重启程序后完全生效）")

    cur = edition.current(cfg)
    for k, v in edition.EDITIONS.items():
        mark = "▶" if k == cur else " "
        caps = ["海外站点" if v["allow_foreign"] else "仅国内站点"]
        if v["show_proxy"]:
            caps.append("代理设置")
        print(f"  {mark} {v['name']:<8} {v['tagline']}")
        print(f"      {'、'.join(caps)}")
        if k == cur:
            print(f"      {v['note']}")
    return 0


def cmd_add(args) -> int:
    cfg = _config.load()
    outdir = pathlib.Path(args.out).expanduser() if args.out else _config.download_dir(cfg)
    outdir.mkdir(parents=True, exist_ok=True)

    # ---- 先认协议 ----
    # magnet: / ed2k: 不是 HTTP，拿去 probe 只会报错。
    # 迅雷壳要剥开，网盘要如实说明，别让人以为下不动是 bug。
    pinfo = protocols.detect(args.url)
    in_url = pinfo.url or args.url
    if pinfo.kind not in ("http",):
        print(f"  协议   {pinfo.label}" + ("（已解壳）" if pinfo.decoded else ""))
        if pinfo.decoded:
            print(f"  解壳后 {in_url[:110]}")
        if pinfo.name:
            print(f"  资源名 {pinfo.name}")
        if pinfo.info_hash:
            print(f"  hash   {pinfo.info_hash}")
        for t in (pinfo.trackers or [])[:4]:
            print(f"  tracker {t}")
        if pinfo.note:
            print(f"  说明   {pinfo.note}")
        if pinfo.error:
            print(f"  ⚠      {pinfo.error}")

    plan = None
    name = args.name or ""

    if pinfo.p2p:
        # BT：不能套统一文件名（多文件），产物交给 aria2 按元数据命名
        name = name or pinfo.name or "BT 下载"
        plan = resource.classify(url=args.url)
        print(f"  识别   {plan.summary()}")
        for w in plan.warnings:
            print(f"  ⚠ {w}")
        dest = outdir / name            # 只用到它的 parent 作为 aria2 的 dir
    elif not name:
        pr = http_probe(in_url, cfg)
        if pr.status >= 400:
            print(f"  探测失败：{pr.error}")
            return 1
        name = pr.filename or "download.bin"
        plan = resource.classify_probe(pr)
        print(f"  文件名 {name}   大小 {human_size(pr.size)}"
              f"   Range {'支持（可多线程）' if pr.accept_ranges else '不支持（单线程）'}")
        print(f"  识别   {plan.summary()}")
        for w in plan.warnings:
            print(f"  ⚠ {w}")
    if name and not pinfo.p2p:
        dest = unique_path(outdir / name)
    elif name:
        pass

    # 线程数优先级：命令行 > 类型策略 > 全局配置
    threads = args.threads or (plan.concurrency if plan else 0) or int(cfg.get("threads", 8))
    chunk = (plan.chunk_size if plan else 0) or None
    want_hash = plan.verify_hash if plan else False
    if args.sha256:
        want_hash = True

    prog = LineProgress(f"{dest.name}  ")
    prefer = args.backend or ("aria2" if pinfo.p2p else None)
    if pinfo.p2p and (chunk or want_hash):
        # P2P 的分片由 BT 协议自己管，哈希由 BT 自带分片校验兜底
        chunk, want_hash = None, False
    dl = backends.make_downloader(
        in_url, dest, cfg=cfg, threads=threads,
        speed_limit=args.speed_limit,
        progress_cb=prog,
        log_cb=lambda m: (prog.finish(), print("  [log] " + m))[1],
        prefer=prefer,
        chunk_size=chunk,
        verify_hash=want_hash,
        expected_sha256=args.sha256 or "",
    )
    print(f"  引擎 {dl.__class__.__module__.split('.')[-1]}   线程 {threads}"
          + (f"   分片 {human_size(chunk)}" if chunk else "")
          + ("   下完校验 SHA256" if want_hash else ""))
    res = dl.run()
    prog.finish()

    if res.get("ok"):
        print(f"  完成 {human_size(res.get('bytes') or 0)}  "
              f"耗时 {res.get('seconds', 0):.1f}s  ->  {res.get('path')}")
        if res.get("sha256"):
            print(f"  SHA256 {res['sha256']}")
        return 0
    if res.get("cancelled"):
        print("  已取消（可用相同命令续传）")
        return 130
    print(f"  失败：{res.get('error')}")
    return 1


def cmd_classify(args) -> int:
    """只识别，不下载。给 url 会联网探测；也可纯靠 filename/mime 离线判断。"""
    cfg = _config.load()

    if args.tools:
        print(f"  {APP_NAME} 资源分类总览")
        print()
        print(f"  {'分类':<12}{'引擎':<12}{'线程':>5}{'分片':>9}  {'校验':<5}{'可解压':<7}后缀数")
        print("  " + "-" * 74)
        for c in resource.describe_categories():
            print(f"  {c['label']:<12}{c['engine']:<12}{c['concurrency']:>5}"
                  f"{c['chunk']:>9}  {'是' if c['verify_hash'] else '-':<5}"
                  f"{'是' if c['can_extract'] else '-':<7}{c['exts']}")
        print()
        print("  说明：")
        for c in resource.describe_categories():
            print(f"    {c['label']:<10}{c['note']}")
        return 0

    if args.url:
        pr = http_probe(args.url, cfg)
        plan = resource.classify_probe(pr)
        print(f"  URL      {pr.final_url or args.url}")
        print(f"  状态     {pr.status}" + (f"   {pr.error}" if pr.error else ""))
        print(f"  文件名   {pr.filename}")
        print(f"  大小     {human_size(pr.size)}")
        print(f"  可分段   {'是（支持 Range）' if pr.accept_ranges else '否'}")
        print(f"  MIME     {pr.content_type or '-'}")
        if pr.magic:
            print(f"  文件头   {pr.magic[:16].hex(' ')}")
    else:
        plan = resource.classify(
            url=args.filename or "",
            filename=args.filename or "",
            mime=args.mime or "",
            size=args.size,
        )

    print()
    print(f"  分类     {plan.label}   ({plan.category})")
    print(f"  具体类型 {plan.filetype or '-'}")
    if plan.platform:
        print(f"  平台     {plan.platform}")
    print(f"  引擎     {plan.engine}"
          + ("（需 yt-dlp 解析）" if plan.needs_extractor else ""))
    print(f"  并发     {plan.concurrency} 线程")
    print(f"  分片     {human_size(plan.chunk_size)}")
    print(f"  多线程门槛 {human_size(plan.min_multithread)}（低于则单线程）")
    print(f"  下完校验 {'SHA256' if plan.verify_hash else '否'}")
    print(f"  可解压   {'是' if plan.can_extract else '否'}"
          f"   可转码 {'是' if plan.can_transcode else '否'}")
    print(f"  置信度   {plan.confidence:.2f}")

    if args.reasons:
        print()
        print("  判定依据：")
        for r in plan.reasons:
            print(f"    · {r}")
    if plan.warnings:
        print()
        print("  注意：")
        for w in plan.warnings:
            print(f"    ⚠ {w}")

    if args.json:
        print()
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_video(args) -> int:
    cfg = _config.load()
    base = pathlib.Path(args.out).expanduser() if args.out else _config.download_dir(cfg)
    sub = cfg.get("video_dir", "videos")
    outdir = base / sub if sub else base

    if args.info:
        info = video.probe(args.url, cfg)
        print(json.dumps(info, ensure_ascii=False, indent=2)[:4000])
        return 0

    # 是不是合集：B站先问官方接口（快且标题全），其余交给 yt-dlp
    is_pl = bool(args.playlist)
    group = ""
    if not is_pl:
        try:
            if bili.parse_url(args.url).get("kind") not in (None, "", "unknown"):
                r0 = bili.resolve(cfg, args.url)
                if r0.get("ok") and r0.get("kind") in ("pages", "season"):
                    is_pl, group = True, r0.get("title") or ""
            if not is_pl:
                pl = video.probe_playlist(args.url, cfg, fast=True)
                if pl.get("is_playlist") and int(pl.get("count") or 0) > 1:
                    is_pl, group = True, pl.get("title") or ""
        except Exception:
            pass   # 探不出合集就当单集下，不要因为这个把下载挡了

    print(f"  解析 {args.url}")
    if is_pl:
        print(f"  合集：{group or '（未命名）'}" + (f"，只下 {args.items}" if args.items else "，整份下载"))
        print("  用 jiliu playlist 可以先看条目再挑")
    else:
        try:
            info = video.probe(args.url, cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"  解析失败：{exc}")
            return 1
        print(f"  {info.get('title')}  ·  {info.get('uploader')}  ·  {info.get('duration_text')}"
              f"  ·  {info.get('extractor')}")
        if info.get("has_drm"):
            print("  ⚠ 检测到 DRM 保护，无法下载")
    print(f"  输出目录 {outdir}")

    prog = LineProgress("视频  ")
    res = video.download(
        args.url, outdir, cfg,
        fmt=args.format or cfg.get("video_format", "bv*+ba/b"),
        progress_cb=prog,
        log_cb=None,
        playlist=is_pl,
        items=args.items or "",
        group_title=group,
        audio_only=bool(args.audio),
    )
    prog.finish()
    if res.get("ok"):
        print(f"  完成 耗时 {res.get('seconds', 0):.1f}s  ->  {res.get('path') or res.get('outdir')}")
        return 0
    print(f"  失败：{res.get('error')}")
    return 1


def cmd_probe(args) -> int:
    cfg = _config.load()
    kind = args.kind
    if kind == "video":
        info = video.probe(args.url, cfg)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    pr = http_probe(args.url, cfg)
    print(f"  文件名   {pr.filename}")
    print(f"  大小     {human_size(pr.size)}")
    print(f"  可分段   {'是（支持 Range）' if pr.accept_ranges else '否'}")
    print(f"  类型     {pr.content_type}")
    print(f"  ETag     {pr.etag or '-'}")
    print(f"  状态     {pr.status}")
    plan = resource.classify_probe(pr)
    print(f"  识别     {plan.label} / {plan.filetype or '-'}")
    print(f"  策略     {plan.summary()}")
    if pr.error:
        print(f"  错误     {pr.error}")
    return 0


def cmd_tools(args) -> int:
    cfg = _config.load()
    print(f"  {APP_NAME} v{__version__}")
    print()
    # 这是用户主动查工具，必须现探 —— 不能给它看缓存，
    # 不然刚装的 yt-dlp 会被说成「未找到」，显得像坏了
    tools.invalidate_probe_cache()
    backends.invalidate_cache()
    info = tools.probe_all(cfg, refresh=True)
    for name in ("yt-dlp", "ffmpeg", "ffprobe", "aria2c"):
        d = info.get(name, {})
        ok = d.get("available") == "yes"
        print(f"  {'✔' if ok else '✘'} {name:<8} {d.get('version', '')[:52]}")
        print(f"      {d.get('path') or '未找到'}   ({d.get('source')})")
    print()
    b = backends.describe(cfg)
    print(f"  下载后端：配置 {b['configured']}  →  实际 {b['effective']}")
    print(f"    native  {b['native']['note']}")
    print(f"    aria2   {b['aria2']['note']}")
    print()
    print(f"  状态目录 {_config.HOME}")
    return 0


def cmd_config(args) -> int:
    cfg = _config.load()
    if args.set:
        patch: Dict[str, Any] = {}
        for kv in args.set:
            if "=" not in kv:
                print(f"  忽略（需要 k=v 形式）：{kv}")
                continue
            k, v = kv.split("=", 1)
            k = k.strip()
            if k not in _config.DEFAULTS:
                print(f"  忽略未知键：{k}")
                continue
            dv = _config.DEFAULTS[k]
            if isinstance(dv, int) and not isinstance(dv, bool):
                try:
                    v = int(v)
                except Exception:
                    print(f"  {k} 需要整数，忽略")
                    continue
            elif isinstance(dv, list):
                v = [x for x in v.split(",") if x.strip()]
            patch[k] = v
        cfg = _config.update(patch)
        print("  已更新：")
        for k in patch:
            print(f"    {k} = {cfg[k]}")
        return 0

    print(f"  配置文件 {_config.CONFIG_PATH}")
    for k, v in cfg.items():
        print(f"    {k:<22} {v}")
    return 0


def cmd_list(args) -> int:
    p = _config.TASKS_PATH
    if not p.exists():
        print("  还没有保存的任务")
        return 0
    data = json.loads(p.read_text(encoding="utf-8"))
    tasks: List[Dict[str, Any]] = data.get("tasks", [])
    if not tasks:
        print("  还没有保存的任务")
        return 0
    print(f"  {'状态':<8}{'进度':>8}  {'大小':>10}  {'类型':<8}{'后端':<8} 标题/URL")
    print("  " + "-" * 84)
    for t in tasks:
        name = t.get("title") or t.get("filename") or t.get("url", "")
        cat = t.get("category_label") or t.get("category") or "-"
        print(f"  {t.get('status_text', t.get('status','')):<8}"
              f"{t.get('percent', 0):>7.1f}%  {t.get('size_text','-'):>10}  "
              f"{cat:<8}"
              f"{str((t.get('meta') or {}).get('effective_backend') or t.get('backend') or '-'):<8} "
              f"{name[:40]}")
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="jiliu",
        description=f"{APP_NAME} —— 自建多线程下载器（多线程分片 / yt-dlp 视频解析 / aria2 后端）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="  yt-dlp 与 ffmpeg 负责视频，aria2 可选作后端。下载引擎默认 auto。",
    )
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("gui", help="打开桌面窗口（不弹终端、不开浏览器）")
    p.add_argument("--port", type=int)
    p.add_argument("--browser", action="store_true",
                   help="改用系统默认浏览器打开（不推荐，仅兜底）")
    p.set_defaults(fn=cmd_gui)

    p = sub.add_parser("serve", help="启动 Web 面板与 API")
    p.add_argument("--port", type=int)
    p.add_argument("--open-browser", action="store_true", help="启动后自动打开面板")
    p.add_argument("--no-browser", action="store_true", help="打包版默认开浏览器，此参数可关掉")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("engines", help="列出所有下载引擎与可用状态")
    p.add_argument("--refresh", action="store_true", help="强制重新探测")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_engines)

    p = sub.add_parser("sites", help="列出支持的视频站点（国内/海外）")
    p.add_argument("--foreign", action="store_true", help="只看海外站点")
    p.add_argument("--domestic", action="store_true", help="只看国内站点")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_sites)

    p = sub.add_parser("layout", help="查看/预览下载目录的分类归档布局")
    p.add_argument("--url", help="按这个链接的识别结果预览落位")
    p.add_argument("--category", help="直接指定分类 key")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_layout)

    p = sub.add_parser("edition", help="查看或切换版本形态（国内版 / 全球版）")
    p.add_argument("set", nargs="?", choices=list(edition.EDITIONS),
                   help="切到哪个版本，留空则只查看")
    p.set_defaults(fn=cmd_edition)

    p = sub.add_parser("qr", help="把文本渲染成二维码（调试 + 打包自检）")
    p.add_argument("text", help="要编码的文本/链接")
    p.add_argument("-o", "--out", help="写到这个 PNG 文件；不给则打印 base64")
    p.add_argument("--box", type=int, default=qr._DEFAULT_BOX,
                   help="每个模块几个像素（默认 6）")
    p.add_argument("--border", type=int, default=qr._DEFAULT_BORDER,
                   help="静区宽度，单位是模块（默认 2）")
    p.add_argument("--matrix", action="store_true",
                   help="打印字符矩阵而不是 PNG（终端上看效果）")
    p.set_defaults(fn=cmd_qr)

    p = sub.add_parser("add", help="下载一个直链")
    p.add_argument("url")
    p.add_argument("-t", "--threads", type=int)
    p.add_argument("-o", "--out", help="输出目录")
    p.add_argument("-n", "--name", help="保存文件名")
    p.add_argument("-b", "--backend", choices=["native", "aria2", "curl", "wget"])
    p.add_argument("-s", "--speed-limit", type=int, help="字节/秒")
    p.add_argument("--sha256", help="预期 SHA256，下完校验")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("video", help="解析并下载视频")
    p.add_argument("url")
    p.add_argument("-f", "--format", help="格式选择串，如 bv*+ba/b 或 137+140")
    p.add_argument("-o", "--out", help="输出根目录")
    p.add_argument("-i", "--info", action="store_true", help="只打印元信息与格式列表")
    p.add_argument("--playlist", action="store_true", help="按合集处理（建子目录、文件名带序号）")
    p.add_argument("--items", metavar="SPEC",
                   help="只下合集里的这几集，如 1-3,7（yt-dlp 的 --playlist-items）")
    p.add_argument("-x", "--audio", action="store_true", help="只抽音轨（需 ffmpeg）")
    p.set_defaults(fn=cmd_video)

    p = sub.add_parser("playlist", help="展开合集 / 播放列表，列出条目")
    p.add_argument("url")
    p.add_argument("--limit", type=int, default=200, help="最多列多少条")
    p.add_argument("--full", action="store_true", help="完整解析（慢，但标题一定全）")
    p.add_argument("--bili", action="store_true", help="强制走 B站官方接口")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_playlist)

    p = sub.add_parser("protocol", help="识别链接协议（磁力/迅雷/ed2k/种子/网盘）")
    p.add_argument("url")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_protocol)

    p = sub.add_parser("bili", help="B站登录状态 / 扫码登录 / 退出")
    p.add_argument("action", nargs="?", choices=["status", "login", "logout"], default="status")
    p.set_defaults(fn=cmd_bili)

    p = sub.add_parser("cookies", help="Cookie 文件管理")
    p.add_argument("action", nargs="?", choices=["list", "browsers", "import", "export", "rm"],
                   default="list")
    p.add_argument("file", nargs="?", help="import 时的源文件 / rm 时的目标")
    p.add_argument("--label", help="导入时的标签，决定存成哪个域名")
    p.add_argument("--browser", help="export 时从哪个浏览器读")
    p.add_argument("--profile", help="浏览器配置目录")
    p.set_defaults(fn=cmd_cookies)

    p = sub.add_parser("history", help="已下载记录")
    p.add_argument("action", nargs="?", choices=["list", "rm", "clear", "export", "import"],
                   default="list")
    p.add_argument("key", nargs="?", help="rm 时的 key（形如 bilibili.com::BV1xx）")
    p.add_argument("--query", "-q", help="按标题 / UP / ID 搜")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--file", help="export / import 的文件路径")
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("naming", help="命名模板变量表与试算")
    p.add_argument("--pattern", help="试算一个模板，看翻译成 yt-dlp 后长什么样")
    p.set_defaults(fn=cmd_naming)

    p = sub.add_parser("proxy", help="代理状态与连通性测试")
    p.add_argument("--test", metavar="URL", help="测一个代理端点是否可达")
    p.add_argument("--timeout", type=float, help="测试超时（秒）")
    p.add_argument("--url", help="按某个目标 URL 判断会走哪条路")
    p.set_defaults(fn=cmd_proxy)

    p = sub.add_parser("probe", help="探测资源")
    p.add_argument("url")
    p.add_argument("-k", "--kind", choices=["http", "video"], default="http")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("classify", help="识别资源类型与接管策略（不下载）")
    p.add_argument("url", nargs="?", help="给 URL 会联网探测；不给则靠下面的参数离线判断")
    p.add_argument("-n", "--filename", help="离线判断用的文件名")
    p.add_argument("-m", "--mime", help="离线判断用的 MIME")
    p.add_argument("-s", "--size", type=int, help="字节数，用于自适应并发")
    p.add_argument("-r", "--reasons", action="store_true", help="打印判定依据")
    p.add_argument("--json", action="store_true", help="附完整 JSON")
    p.add_argument("--tools", action="store_true", help="列出所有分类与策略总览")
    p.set_defaults(fn=cmd_classify)

    p = sub.add_parser("tools", help="查看外部工具与后端状态")
    p.set_defaults(fn=cmd_tools)

    p = sub.add_parser("config", help="查看或修改配置")
    p.add_argument("--set", nargs="*", metavar="k=v", help="例：--set threads=16 backend=aria2")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("list", help="列出已保存的任务")
    p.set_defaults(fn=cmd_list)

    return ap


def cmd_protocol(args) -> int:
    """认一条链接是什么协议，把壳剥开看看里面是什么。"""
    pinfo = protocols.detect(args.url)
    print(f"  原始     {args.url[:100]}")
    print(f"  协议     {pinfo.label}（kind={pinfo.kind}）")
    if pinfo.decoded:
        print(f"  解壳后   {pinfo.url[:100]}")
    print(f"  引擎     {pinfo.engine}" + ("（P2P）" if pinfo.p2p else ""))
    if pinfo.info_hash:
        print(f"  info hash {pinfo.info_hash}")
    if pinfo.name:
        print(f"  资源名   {pinfo.name}")
    if pinfo.trackers:
        print(f"  tracker  {len(pinfo.trackers)} 个")
        for t in pinfo.trackers[:5]:
            print(f"           {t}")
    if pinfo.note:
        print(f"  说明     {pinfo.note}")
    if pinfo.error:
        print(f"  ⚠        {pinfo.error}")

    plan = resource.classify(url=args.url)
    print()
    print(f"  分类     {plan.label} / {plan.filetype or '-'}")
    print(f"  策略     {plan.summary()}")
    for w in plan.warnings:
        print(f"  ⚠        {w}")
    if args.json:
        print()
        print(json.dumps({"protocol": pinfo.to_dict(), "plan": plan.to_dict()},
                         ensure_ascii=False, indent=2))
    return 1 if pinfo.error else 0


def cmd_playlist(args) -> int:
    """展开合集 / 播放列表，列出条目。"""
    cfg = _config.load()
    # B 站优先走官方接口 —— 快且标题不是空的
    if args.bili or bili.parse_url(args.url).get("kind") not in (None, "", "unknown"):
        res = bili.resolve(cfg, args.url, limit=args.limit)
        if res.get("ok"):
            res["via"] = "bilibili-api"
        elif not args.bili:
            res = None
    else:
        res = None
    if res is None:
        res = video.probe_playlist(args.url, cfg, limit=args.limit, fast=not args.full)
        res["via"] = "yt-dlp"

    if not res.get("ok", True) and res.get("error"):
        print(f"  解析失败：{res['error']}")
        return 1

    items = res.get("items") or res.get("entries") or []
    print(f"  {res.get('title') or '(无标题)'}")
    if res.get("uploader"):
        print(f"  UP / 作者：{res['uploader']}")
    print(f"  条目 {len(items)} 个   来源 {res.get('via')}")
    print()
    for it in items:
        idx = it.get("index") or "?"
        dur = f"  {it.get('duration_text')}" if it.get("duration_text") else ""
        print(f"    [{idx:>3}] {it.get('title') or it.get('url') or '(无标题)'}{dur}")
    if not items:
        print("    （没有展开出多个条目，可能是单集）")
    if args.json:
        print()
        print(json.dumps(res, ensure_ascii=False, indent=2)[:8000])
    return 0


def cmd_bili(args) -> int:
    """B 站登录 / 状态 / 退出。"""
    cfg = _config.load()
    if args.action == "status":
        d = bili.describe(cfg)
        print(f"  登录状态 {'已登录' if d['logged_in'] else '未登录'}")
        if d.get("user"):
            u = d["user"]
            print(f"  账号     {u.get('name')}（mid {u.get('mid')}）")
            print(f"  等级     Lv{u.get('level')}   {u.get('max_quality_hint')}")
        print(f"  Cookie   {d.get('cookie_file') or '（无）'}")
        if d.get("error"):
            print(f"  ⚠         {d['error']}")
        print(f"  说明     {d['note']}")
        return 0

    if args.action == "logout":
        ok = bili.logout()
        print("  已退出登录" if ok else "  本来就没登录")
        return 0

    # login：扫码
    st = bili.qr_start(cfg)
    if not st.get("qrcode_key"):
        print(f"  申请二维码失败：{st.get('error') or st}")
        return 1
    print("  用 B站 App 扫码登录")
    print()
    # 终端里画不出二维码就用字符块画 —— 免得还要用户去开文件
    _print_qr(st.get("url") or "", st.get("qrcode") or "")
    key = st["qrcode_key"]
    last = ""
    for _ in range(90):          # 最多等 3 分钟
        time.sleep(2)
        r = bili.qr_poll(cfg, key)
        s = r.get("state")       # 注意是 state：服务端从不返回 status
        if s != last:
            print(f"  [{s}] {r.get('message') or ''}")
            last = s
        if s == "ok":
            u = r.get("user") or {}
            print(f"  登录成功：{u.get('name') or ''}")
            print(f"  凭据已写入 {r.get('cookie_file') or '（见说明）'}")
            return 0
        if s == "expired":
            print("  二维码过期了，重新跑一次 jiliu bili login")
            return 1
    print("  等超时了")
    return 1


def _print_qr(text: str, b64: str) -> None:
    """在终端里把二维码画出来 —— 手机对着屏幕就能扫。

    用 `qr.matrix()` 直接拿模块矩阵自己画，而不是去解 base64 PNG：
    同一条编码路径，少一次编解码，终端宽度也能自己控制。

    降级顺序（终端编码是 cp936 时 `█` 会 UnicodeEncodeError）：
        终端画 → 存 PNG 文件 → 打印原始链接
    """
    # 1) 终端直接画
    if text:
        try:
            mat = qr.matrix(text, border=2)
            # 一个模块用两个字符宽，方块的宽高比才不至于拉变形
            blank = "  "
            solid = "██"
            lines = ["  " + "".join(solid if c else blank for c in row) for row in mat]
            print()
            print("\n".join(lines))
            print()
        except Exception:  # noqa: BLE001
            pass

    # 2) 存成 PNG，扫不了屏还能扫文件
    try:
        import base64 as _b64
        raw = _b64.b64decode(b64) if b64 else b""
        if raw:
            path = _config.HOME / "qrcode.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            print(f"  二维码图片已保存：{path}")
    except Exception:  # noqa: BLE001
        pass

    # 3) 实在不行，至少把内容打出来
    if text and not b64:
        print(f"  二维码内容：{text}")


def cmd_cookies(args) -> int:
    cfg = _config.load()
    if args.action == "list":
        files = cookies.list_files()
        if not files:
            print("  还没有任何 cookie 文件。")
            print(f"  目录：{cookies.cookie_dir()}")
            return 0
        print(f"  目录：{cookies.cookie_dir()}")
        print()
        for f in files:
            print(f"    {f['file']}")
            print(f"      {f['count']} 条 · {human_size(f['size'])} · 域名 {f['domains']}")
        return 0

    if args.action == "browsers":
        found = cookies.available_browsers()
        if not found:
            print("  没找到任何浏览器。")
            return 0
        for b in found:
            tag = "（读不了）" if b.get("hard") else ""
            print(f"    {b['label']:<12} {b.get('key') or '':<10} {tag}")
            if b.get("profiles"):
                print(f"      配置目录：{', '.join(b['profiles'][:6])}")
        print()
        print("  说明：Chrome / Edge 127+ 给 cookie 库加了 App-Bound 加密，")
        print("        纯 Python 解不开（yt-dlp 也一样）。Firefox 不受影响。")
        print("        能用 Firefox 就用它，否则用浏览器扩展导出 cookies.txt 再 import。")
        return 0

    if args.action == "export":
        try:
            p = cookies.from_browser(args.browser or "firefox", args.profile or "")
        except Exception as exc:  # noqa: BLE001
            print(f"  失败：{exc}")
            return 1
        print(f"  已导出：{p}")
        return 0

    if args.action == "import":
        text = pathlib.Path(args.file).read_text(encoding="utf-8", errors="replace")
        info = cookies.validate(text)
        if not info["ok"]:
            print(f"  内容不认（{info['count']} 条）：{args.file}")
            return 1
        p = cookies.save_text(text, args.label or "imported")
        print(f"  已保存 {info['count']} 条 → {p}")
        return 0

    if args.action == "rm":
        ok = cookies.remove(args.file)
        print("  已删除" if ok else "  没找到")
        return 0 if ok else 1
    return 0


def cmd_history(args) -> int:
    h = naming.History()
    if args.action == "clear":
        print(f"  清掉了 {h.clear()} 条")
        return 0
    if args.action == "rm":
        ok = h.remove(args.key)
        print("  已删除" if ok else "  没找到这条")
        return 0 if ok else 1
    if args.action == "export":
        print(f"  已导出 → {h.export_to(pathlib.Path(args.file))}")
        return 0
    if args.action == "import":
        print(f"  导入 {h.import_from(pathlib.Path(args.file))} 条")
        return 0

    rows = h.list(q=args.query or "", limit=args.limit)
    st = h.stats()
    print(f"  共 {st['total']} 条 · {human_size(st['bytes'])}")
    if st["by_site"]:
        print("  " + "  ".join(f"{k}:{v}" for k, v in
                               sorted(st["by_site"].items(), key=lambda kv: -kv[1])[:8]))
    print()
    for r in rows:
        print(f"    {r.get('site','?'):<16} {r.get('vid',''):<18} {(r.get('title') or '')[:46]}")
    if not rows:
        print("    （空）")
    return 0


def cmd_naming(args) -> int:
    if args.pattern is not None:
        info = naming.check(args.pattern)
        print(f"  模板     {args.pattern}")
        print(f"  翻译后   {naming.to_ytdlp(args.pattern)}")
        if info.get("unknown"):
            print(f"  ⚠ 认不出的变量：{info['unknown']}")
        if info.get("suggest"):
            print(f"  猜你是想写：{info['suggest']}")
        print()
    print("  可用变量：")
    group = ""
    for v in naming.variables():
        if v["group"] != group:
            group = v["group"]
            print(f"    —— {group} ——")
        print(f"      {v['key']:<14} {v['label']:<24} → {v['yt']}")
    print()
    print(f"  当前配置：{_config.load().get('name_pattern')}")
    return 0


def cmd_proxy(args) -> int:
    cfg = _config.load()
    if args.test:
        info = proxy.test(args.test, timeout=args.timeout or 3.0)
        print(f"  {args.test}")
        print(f"  结果 {'通' if info.alive else '不通'}"
              + (f"  握手 {info.latency * 1000:.0f} ms" if info.alive else ""))
        if info.error:
            print(f"  错误 {info.error}")
        return 0 if info.alive else 1

    env = proxy.detect_env(refresh=True)
    print(f"  环境变量 {env.source}：{env.url or '（无）'}")
    if env.url:
        print(f"           {'正常' if env.alive else '连不上 —— ' + env.error}")
    d = proxy.describe(cfg, args.url or "")
    print(f"  配置模式 {d['mode']}")
    print(f"  本次选用 {d['chosen']['url'] or '直连'}"
          + ("（P2P/直连判定见 classify）" if not d["chosen"]["url"] else ""))
    print(f"  国内站   {'走代理' if d['domestic'] else '直连'}")
    print(f"  国外站   {'走代理' if d['foreign'] else '直连'}")
    print(f"  说明     {d['note']}")
    print()
    print("  如果环境变量里的代理已经停了，所有请求都会**静静地失败**。")
    print("  汲流读它之前会先做一次 TCP 验活，死了就自动改直连并提示。")
    return 0


def main(argv: List[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        print("\n  已中断")
        return 130
