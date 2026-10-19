#!/usr/bin/env python3
"""统一协议层 / 命名模板 / 代理分流 / Cookie / 已下载记录 的单元测试。

这一套**不联网**，全部是纯逻辑 —— 所以能放心进 run_all 的默认清单。
需要真进程真网络的部分（aria2 BT、yt-dlp 解析）在各自的套件里。

覆盖：
    · protocols.detect  磁力 / ed2k / 种子 / 三种壳 / 网盘 / 普通
    · resource.classify 与协议层的串联（分类、引擎、P2P、解壳后的地址）
    · naming            变量翻译、字面量清理、归档模板、开关、History
    · video._out_template 与旧行为的一致性
    · proxy             is_domestic / sanitize / select / ytdlp_args
    · cookies           Netscape 往返、validate、域名匹配
    · bili.parse_url    各种链接形态
    · task.CATEGORY_TEXT 与 resource.CATEGORIES 保持同步
"""
from __future__ import annotations

import base64
import os
import pathlib
import sys
import tempfile
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import (bili, cookies, naming, protocols, proxy,  # noqa: E402
                   resource as R, task, video)

PASS = 0
FAIL = 0
RESULTS: List[Tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        RESULTS.append((name, True, detail))
        print(f"  [PASS] {name}" + (f"  -- {detail}" if detail else ""))
    else:
        FAIL += 1
        RESULTS.append((name, False, detail))
        print(f"  [FAIL] {name}" + (f"  -- {detail}" if detail else ""))


def head(title: str) -> None:
    print(f"\n{title}")


MAGNET = ("magnet:?xt=urn:btih:c12fe1c06bba254a9dc9f519b335aa7c1367a88a"
          "&dn=Ubuntu+24.04&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce")


def wrap(scheme: str, inner: str, head: str = "AA", tail: str = "ZZ") -> str:
    return scheme + base64.b64encode((head + inner + tail).encode()).decode()


# ==========================================================================
head("T1  协议识别与解壳")

p = protocols.detect(MAGNET)
check("T1.1 磁力 → aria2 + P2P", p.kind == "magnet" and p.engine == "aria2" and p.p2p)
check("T1.2 磁力抽出 info hash", p.info_hash == "c12fe1c06bba254a9dc9f519b335aa7c1367a88a",
      p.info_hash)
check("T1.3 磁力抽出 dn 与 tracker",
      p.name == "Ubuntu 24.04" and len(p.trackers) == 1, f"{p.name} / {p.trackers}")
# dn 里的 + 到底算空格还是加号，各家站点不统一，规则见 protocols._decode_dn
check("T1.3b dn 里的 %2B 当成真的加号",
      protocols.decode_magnet("magnet:?xt=urn:btih:" + "a" * 40
                              + "&dn=C%2B%2B%20Primer")["name"] == "C++ Primer",
      protocols.decode_magnet("magnet:?xt=urn:btih:" + "a" * 40
                              + "&dn=C%2B%2B%20Primer")["name"])
check("T1.3c dn 里的裸 + 当成空格",
      protocols.decode_magnet("magnet:?xt=urn:btih:" + "a" * 40
                              + "&dn=Ubuntu+24.04")["name"] == "Ubuntu 24.04")

p = protocols.detect("ed2k://|file|x.iso|1234567|ABCDEF0123456789ABCDEF0123456789|/")
check("T1.4 ed2k → aria2", p.kind == "ed2k" and p.engine == "aria2" and p.p2p)

p = protocols.detect("https://example.org/t/ubuntu-24.04.torrent")
check("T1.5 .torrent → aria2", p.kind == "torrent" and p.engine == "aria2")

# 迅雷壳：里面是普通 URL
inner = "http://dl.example.com/files/setup_v2.1.exe"
p = protocols.detect(wrap("thunder://", inner))
check("T1.6 迅雷解壳出原地址", p.decoded and p.url == inner and p.engine == "native",
      p.url)
check("T1.7 迅雷壳内是普通地址时不走 P2P", p.p2p is False)

# 迅雷壳：里面是磁力 —— 必须继续分流到 aria2，不能当普通链接下
p = protocols.detect(wrap("thunder://", MAGNET))
check("T1.8 迅雷壳内是磁力 → 仍走 aria2", p.p2p and p.engine == "aria2", p.label)
check("T1.9 标签说明壳里装的什么", "内含" in p.label, p.label)

p = protocols.detect("qqdl://" + base64.b64encode(b"http://c.example.com/d.zip").decode())
check("T1.10 旋风解壳", p.kind == "qqdl" and p.url == "http://c.example.com/d.zip", p.url)

p = protocols.detect("flashget://" + base64.b64encode(
    b"[FLASHGET]http://c.example.com/m.mkv[FLASHGET]").decode())
check("T1.11 快车解壳（去 [FLASHGET] 标记）",
      p.kind == "flashget" and p.url == "http://c.example.com/m.mkv", p.url)

p = protocols.detect("thunder://!!!not-base64!!!")
check("T1.12 壳解不开时如实报错", p.error != "" or p.url == "", p.error or p.url)

p = protocols.detect("https://pan.baidu.com/s/1abcdef")
check("T1.13 网盘只识别不逆向", p.kind == "netdisk" and "直链" in p.note, p.note[:60])

p = protocols.detect("https://cdn.example.com/a/b/tool.zip")
check("T1.14 普通链接不受影响", p.kind == "http" and p.engine == "native" and not p.p2p)

p = protocols.detect("")
check("T1.15 空链接不炸", p.kind == "unknown" and p.error)

# ==========================================================================
head("T2  协议层与分类体系的串联")

for label, url, cat, eng, p2p in [
    ("磁力", MAGNET, "bt", "aria2", True),
    ("ed2k", "ed2k://|file|x.iso|1|ABCDEF0123456789ABCDEF0123456789|/", "bt", "aria2", True),
    ("种子", "https://e.org/t/x.torrent", "bt", "aria2", True),
    ("迅雷→磁力", wrap("thunder://", MAGNET), "bt", "aria2", True),
    ("迅雷→exe", wrap("thunder://", inner), "app", "multithread", False),
    ("旋风→zip", "qqdl://" + base64.b64encode(b"http://c.e.com/d.zip").decode(),
     "archive", "multithread", False),
    ("网盘", "https://pan.quark.cn/s/xyz", "netdisk", "native", False),
    ("普通zip", "https://c.e.com/a.zip", "archive", "multithread", False),
]:
    plan = R.classify(url=url)
    check(f"T2 {label}: 分类={plan.category} 引擎={plan.engine}",
          plan.category == cat and plan.engine == eng and plan.p2p == p2p,
          f"实得 分类={plan.category} 引擎={plan.engine} p2p={plan.p2p}")

plan = R.classify(url=MAGNET)
check("T2.9 磁力把 dn 当文件名", plan.filename == "Ubuntu 24.04", plan.filename)
check("T2.10 磁力带上 info hash 与 tracker",
      bool(plan.info_hash) and len(plan.trackers) == 1)
check("T2.11 BT 不给无意义的线程/分片数",
      plan.concurrency == 0 and plan.chunk_size == 0)
check("T2.12 BT 不叠一层哈希校验（BT 自带分片校验）", plan.verify_hash is False)
check("T2.13 BT 会提醒速度不由本地决定",
      any("别人" in w or "种子" in w for w in plan.warnings), str(plan.warnings)[:80])

plan = R.classify(url=wrap("thunder://", inner))
check("T2.14 解壳后按内层地址判类型", plan.category == "app" and plan.ext == "exe",
      f"{plan.category}/{plan.ext}")
check("T2.15 解壳后的地址记在 resolved_url", plan.resolved_url == inner, plan.resolved_url)
check("T2.16 summary 里能看出协议", "迅雷" in plan.summary() or "协议" in plan.summary(),
      plan.summary())

check("T2.17 to_dict 里协议字段可序列化",
      R.classify(url=MAGNET).to_dict()["protocol"] == "magnet")

d = R.intercept_decision(MAGNET)
check("T2.18 插件遇到磁力必接管", d["take"] is True and d["plan"]["category"] == "bt")
d = R.intercept_decision("blob:https://x/abc")
check("T2.19 blob: 不接管", d["take"] is False)

keys = {c["key"] for c in R.describe_categories()}
check("T2.20 分类总览含新三类", {"bt", "netdisk", "playlist"} <= keys, str(sorted(keys)))
check("T2.21 task.CATEGORY_TEXT 覆盖全部分类",
      set(R.CATEGORIES) <= set(task.CATEGORY_TEXT),
      str(set(R.CATEGORIES) - set(task.CATEGORY_TEXT)))

# ==========================================================================
head("T3  命名模板")

check("T3.1 默认单视频模板与旧行为逐字一致",
      naming.build_output("", naming.DEFAULT_FILE_PATTERN) == "%(title).150B [%(id)s].%(ext)s",
      naming.build_output("", naming.DEFAULT_FILE_PATTERN))
check("T3.2 合集模板带序号与子目录",
      naming.build_output("", "%pl_index% - %title% [%id%]", is_playlist=True)
      == "%(playlist_title,playlist|合集).120B/%(playlist_index)03d - %(title).150B [%(id)s].%(ext)s")
check("T3.3 关掉子目录就不建那层",
      "/" not in naming.build_output("", "%title%", is_playlist=True, use_playlist_folder=False))
check("T3.4 归档目录模板生效",
      naming.build_output("%up%/%pubyear%", "%title%").startswith("%(uploader).80B/%(upload_date>%Y)s/"))

# 字面量清理：关键是**别把变量一起吃掉**
check("T3.5 非法字符被替换",
      naming._sanitize_literals("a:b|c") == "a_b_c", naming._sanitize_literals("a:b|c"))
check("T3.6 变量里的 > 不被吃掉",
      naming._sanitize_literals("%(upload_date>%Y)s") == "%(upload_date>%Y)s",
      naming._sanitize_literals("%(upload_date>%Y)s"))
check("T3.7 变量里的 | 不被吃掉",
      naming._sanitize_literals("%(playlist_title,playlist|合集).120B")
      == "%(playlist_title,playlist|合集).120B")
check("T3.8 变量里的 .NB 后缀不被吃掉",
      naming._sanitize_literals("%(title).150B") == "%(title).150B")
check("T3.9 目录分隔符保留",
      naming._sanitize_literals("%(uploader).80B/%(title).150B")
      == "%(uploader).80B/%(title).150B")
check("T3.10 不认得 %xxx% 原样保留（宁可让 yt-dlp 报错）",
      naming.to_ytdlp("%notavar%") == "%notavar%", naming.to_ytdlp("%notavar%"))
check("T3.11 不产生前导下划线", not naming.to_ytdlp("%title%").startswith("_"),
      naming.to_ytdlp("%title%"))
check("T3.12 变量表里没有假的「下载日期」字段",
      not any("下载日期" in v["label"] for v in naming.variables()))

out = pathlib.Path("D:/dl")
cfg = {"name_pattern": "%title% [%id%]", "dir_pattern": "", "playlist_folder": True}
check("T3.13 video._out_template 单视频与旧行为一致",
      video._out_template(out, cfg, False) == str(out / "%(title).150B [%(id)s].%(ext)s"),
      video._out_template(out, cfg, False))
check("T3.14 video._out_template 合集带序号",
      "playlist_index" in video._out_template(out, cfg, True))
custom = {"name_pattern": "%up% - %title%", "dir_pattern": "%site%", "playlist_folder": False}
t = video._out_template(out, custom, True)
check("T3.15 自定义模板被采用且不重复加序号",
      "%(uploader).80B - %(title).150B" in t and t.count("playlist_index") == 1, t)

# ==========================================================================
head("T4  已下载记录")

tmp = pathlib.Path(tempfile.mkdtemp(prefix="jl_hist_"))
h = naming.History(tmp / "h.json")
h.add(site="bilibili.com", vid="BV1xx411c7mD", title="第一集", up="某UP", size=1024)
check("T4.1 加完就有", h.has("bilibili.com", "BV1xx411c7mD"))
check("T4.2 判重是「站点 + ID」", not h.has("bilibili.com", "BV1yy411c7mD"))
check("T4.3 站点名大小写不敏感", h.has("BiliBili.com", "BV1xx411c7mD"))
h.add(site="bilibili.com", vid="BV1xx411c7mD", title="第一集（重下）")
check("T4.4 同一条只留最新（不重复堆积）", h.stats()["total"] == 1, str(h.stats()))
check("T4.5 stats 给出 by_site", h.stats()["by_site"].get("bilibili.com") == 1)
check("T4.6 搜索能按标题命中", len(h.list(q="重下")) == 1)
h.add(site="youtube.com", vid="R1j8", title="YT 视频")
check("T4.7 跨站点互不影响", h.stats()["total"] == 2)
exp = h.export_to(tmp / "export.json")
check("T4.8 能导出", exp.is_file())
h2 = naming.History(tmp / "h2.json")
check("T4.9 导入条数正确", h2.import_from(exp) == 2)
check("T4.10 导入后能查到", h2.has("youtube.com", "R1j8"))
check("T4.11 能按 key 删", h2.remove(naming.History.key_of("youtube.com", "R1j8"))
      and not h2.has("youtube.com", "R1j8"))
check("T4.12 能清空", h2.clear() == 1 and h2.stats()["total"] == 0)
check("T4.13 删不存在的 key 返回 False", h2.remove("nope::nope") is False)

# ==========================================================================
head("T5  代理分流")

check("T5.1 国内站判为国内", proxy.is_domestic("https://www.bilibili.com/video/BV1"))
check("T5.2 .cn 后缀判为国内", proxy.is_domestic("https://example.cn/a"))
check("T5.3 YouTube 判为国外", not proxy.is_domestic("https://www.youtube.com/watch?v=x"))
check("T5.4 清理代理串去掉空白", proxy.sanitize("  http://127.0.0.1:7890 \n") == "http://127.0.0.1:7890")

cfg = {"proxy_mode": "off", "proxy": "", "proxy_domestic": False, "proxy_foreign": True}
check("T5.5 off 模式一律直连", proxy.select(cfg, "https://www.youtube.com/x") == "")
check("T5.6 off 模式下 ytdlp_args 清掉环境变量",
      proxy.ytdlp_args(cfg, "https://youtu.be/x") == ["--proxy", ""],
      str(proxy.ytdlp_args(cfg, "https://youtu.be/x")))

cfg = {"proxy_mode": "manual", "proxy": "http://127.0.0.1:7890",
       "proxy_domestic": False, "proxy_foreign": True}
check("T5.7 手动模式：国外站走代理",
      proxy.select(cfg, "https://www.youtube.com/x") == "http://127.0.0.1:7890")
check("T5.8 手动模式：国内站仍直连（默认不套）",
      proxy.select(cfg, "https://www.bilibili.com/x") == "")
cfg2 = dict(cfg, proxy_domestic=True)
check("T5.9 打开开关后国内站也走代理",
      proxy.select(cfg2, "https://www.bilibili.com/x") == "http://127.0.0.1:7890")

info = proxy.test("http://127.0.0.1:1", timeout=1.0)
check("T5.10 死端口如实报不通", info.alive is False and info.error != "", info.error[:60])
check("T5.11 detect_env 有结果且标了 checked",
      proxy.detect_env(timeout=1.0).checked is True)
d = proxy.describe(cfg, "https://youtu.be/x")
check("T5.12 describe 返回界面要的字段",
      {"mode", "chosen", "env", "note", "presets"} <= set(d), str(sorted(d)))

# ==========================================================================
head("T6  Cookie")

netscape = ("# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tdeadbeef\n"
            "#HttpOnly_.youtube.com\tTRUE\t/\tFALSE\t0\tHSID\tcafebabe\n")
info = cookies.validate(netscape)
check("T6.1 认出 Netscape 格式", info["ok"] is True, str(info)[:80])
check("T6.2 数对了两条", info["count"] == 2, str(info["count"]))
check("T6.3 认出登录态痕迹", info["has_login_hint"] is True)
check("T6.4 抽出域名", any("youtube.com" in x for x in info["domains"]), str(info["domains"]))

rows = cookies.parse_netscape(netscape)
check("T6.5 解析出 2 条", len(rows) == 2, str(len(rows)))
check("T6.6 #HttpOnly_ 前缀被剥掉且标了 httpOnly",
      any(r.get("httpOnly") for r in rows), str(rows[1])[:90])
back = cookies.to_netscape(rows)
check("T6.7 往返后仍能解析出 2 条", cookies.parse_netscape(back).__len__() == 2)
check("T6.8 往返后带上 #HttpOnly_ 前缀", "#HttpOnly_" in back)

info = cookies.validate("这不是 cookie")
check("T6.9 不认的内容如实报错", info["ok"] is False, str(info)[:80])
info = cookies.validate("")
check("T6.10 空内容不炸", info["ok"] is False)

cfg = {"cookie_mode": "auto", "cookie_file": "", "cookie_browser": ""}
check("T6.11 auto 模式下没有文件就不带 cookie 参数",
      cookies.ytdlp_args(cfg, "https://www.youtube.com/x") == [])
cfg = {"cookie_mode": "file", "cookie_file": str(tmp / "nope.txt"), "cookie_browser": ""}
check("T6.12 指定了不存在的文件也不带（不塞坏路径给 yt-dlp）",
      cookies.ytdlp_args(cfg, "https://x/y") == [])
check("T6.13 describe 返回界面要的字段",
      {"mode", "files", "dir", "site_file", "note"} <= set(cookies.describe(cfg, "")),
      str(sorted(cookies.describe(cfg, ""))))

# ==========================================================================
head("T7  B站链接形态")

for url, kind in [
    ("https://www.bilibili.com/video/BV1xx411c7mD", "video"),
    ("https://www.bilibili.com/video/av170001", "video"),
    ("https://www.bilibili.com/bangumi/play/ep123456", "bangumi_ep"),
    ("https://www.bilibili.com/bangumi/play/ss28747", "bangumi_season"),
    ("https://space.bilibili.com/12345/video", "up_videos"),
    ("https://www.bilibili.com/list/watchlater", "unknown"),
    ("https://b23.tv/abcdef", "shortlink"),
    ("https://www.example.com/x", "unknown"),
]:
    got = bili.parse_url(url).get("kind")
    check(f"T7 {url[:56]} → {kind}", got == kind, f"实得 {got}")

check("T7.9 抽出 BV 号", bili.parse_url("https://www.bilibili.com/video/BV1xx411c7mD?a=1")
      .get("bvid") == "BV1xx411c7mD")
check("T7.10 带分P参数时抽出页码",
      bili.parse_url("https://www.bilibili.com/video/BV1xx411c7mD?p=3").get("page") == 3)
check("T7.11 抽出 av 号",
      bili.parse_url("https://www.bilibili.com/video/av170001").get("aid") == 170001)

# ==========================================================================
print("\n" + "=" * 70)
print(f"结果: {PASS}/{PASS + FAIL} 通过")
print("=" * 70)
if FAIL:
    print("\n未通过项：")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  - {n}  {d}")
raise SystemExit(1 if FAIL else 0)
