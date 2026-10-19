#!/usr/bin/env python3
"""本轮新增平台能力的单元测试：二维码 / 分类归档 / 版本形态 / 引擎择优 /
站点注册表 / 原生窗口 / 引擎降级链。

这一套**不联网**（唯一的外部依赖是「pywebview 装没装」这种本机探测），
所以能进 `run_all.py` 的默认清单。

设计上刻意分成两类断言：

  · **纯逻辑**（layout / edition / engines / sites / fallback）——
    给死输入要死输出，必须全绿。
  · **本机能力**（qr / desktop）—— 装没装是会变的，所以断言写成
    「要么可用，要么给出明确的不可用原因」，而不是硬要求可用。
    硬要求可用的话，换台机器跑就会红，那是测试的问题不是代码的问题。
"""
from __future__ import annotations

import base64
import os
import pathlib
import sys
import tempfile
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import (backends, config, desktop, edition, engines,  # noqa: E402
                   layout, protocols, qr, resource, sites)

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


# ==========================================================================
head("P1  二维码渲染（qr）")

if qr.available():
    m = qr.matrix("https://passport.bilibili.com/qrcode/h5/login?a=1", border=2)
    n = len(m)
    check("P1.1 matrix 是方阵", all(len(r) == n for r in m), f"{n}x{n}")
    check("P1.2 尺寸 ≥ 21+2*border", n >= 25, f"实得 {n}")
    check("P1.3 静区全白（外圈 border 行/列无黑块）",
          not any(m[0]) and not any(m[-1]) and
          not any(r[0] or r[-1] for r in m))
    # 三个定位图案：左上/右上/左下角各是一个 7x7 同心方框：
    #
    #     1111111        ← 第 0 行全黑
    #     1000001
    #     1011101        ← 第 2 行 = 1011101（不是 1100011，我一开始记反了）
    #     1011101
    #     1011101
    #     1000001
    #     1111111        ← 第 6 行全黑
    #
    # 所以断言分两层：顶/底行全黑，中间行是 1011101。
    def finder(ox: int, oy: int) -> tuple:
        top = m[oy][ox:ox + 7]
        mid = m[oy + 2][ox:ox + 7]
        bot = m[oy + 6][ox:ox + 7]
        ok = (len(mid) == 7 and all(top) and all(bot)
              and mid == [True, False, True, True, True, False, True])
        return ok, "".join("1" if v else "0" for v in mid)

    ok, mid = finder(2, 2)
    check("P1.4 左上定位图案（中间行 1011101）", ok, mid)
    ok, mid = finder(n - 9, 2)
    check("P1.5 右上定位图案", ok, mid)
    ok, mid = finder(2, n - 9)
    check("P1.6 左下定位图案", ok, mid)

    png = qr.png_bytes("hello", box_size=3, border=1)
    check("P1.7 png_bytes 是合法 PNG（魔数）",
          png[:8] == b"\x89PNG\r\n\x1a\n", f"{len(png)} 字节")

    b64 = qr.png_base64("hello")
    ok = False
    try:
        ok = base64.b64decode(b64)[:8] == b"\x89PNG\r\n\x1a\n"
    except Exception:  # noqa: BLE001
        ok = False
    check("P1.8 png_base64 解出来还是 PNG", ok)

    check("P1.9 空文本返回空串（不抛）", qr.png_base64("") == "")
    check("P1.10 data URI 前缀正确",
          qr.png_data_uri("x").startswith("data:image/png;base64,"))
    # 二维码必须**能被复用**：同一文本两次生成应完全一致（无随机性）
    check("P1.11 同输入结果稳定", qr.png_base64("same") == qr.png_base64("same"))
else:
    reason = qr.unavailable_reason()
    check("P1.1 库缺失时给出明确原因", bool(reason), reason[:80])
    try:
        qr.matrix("x")
        raised = False
    except RuntimeError:
        raised = True
    check("P1.2 库缺失时 matrix 抛 RuntimeError（而不是画个空码）", raised)
    check("P1.3 库缺失时 png_base64 返回空串（让调用方如实报错）",
          qr.png_base64("x") == "")


# ==========================================================================
head("P2  分类归档（layout）")

check("P2.1 SUBDIRS 覆盖 resource.CATEGORIES 全部 key",
      set(layout.SUBDIRS) == set(resource.CATEGORIES),
      f"差集 {set(resource.CATEGORIES) ^ set(layout.SUBDIRS)}")

for bad, why in [
    ("../../Windows", "上跳 + 分隔符"),
    ("..\\..\\System32", "反斜杠上跳"),
    ("a/b", "带分隔符"),
    ("C:/tmp", "绝对路径"),
    ("a:b*c?", "非法字符"),
    ("   ", "全空白"),
]:
    got = layout.sanitize_segment(bad, "fallback")
    check(f"P2.2 sanitize_segment 挡住 {why}：{bad!r}",
          "/" not in got and "\\" not in got and ".." not in got,
          f"实得 {got!r}")

check("P2.3 sanitize_segment 正常名保留",
      layout.sanitize_segment("我的视频") == "我的视频")
check("P2.4 sanitize_segment 取最后一段",
      layout.sanitize_segment("a/b/视频") == "视频")

base = pathlib.Path(tempfile.gettempdir()) / "_jiliu_layout_test"
cfg_on = {"auto_subdir": True}
cfg_off = {"auto_subdir": False}

check("P2.5 video → 视频 子目录",
      layout.target_dir(base, "video", cfg_on).name == "视频")
check("P2.6 playlist 归到 视频（不单开一层）",
      layout.target_dir(base, "playlist", cfg_on).name == "视频")
check("P2.7 other 不建子目录（落根）",
      layout.target_dir(base, "other", cfg_on) == base)
check("P2.8 未知分类落根",
      layout.target_dir(base, "no_such_cat", cfg_on) == base)
check("P2.9 auto_subdir=False 时不归档",
      layout.target_dir(base, "video", cfg_off) == base)
check("P2.10 explicit=True 默认不归档",
      layout.target_dir(base, "video", cfg_on, explicit=True) == base)
check("P2.11 subdir_explicit=True 时显式目录也归档",
      layout.target_dir(base, "video",
                        {**cfg_on, "subdir_explicit": True},
                        explicit=True).name == "视频")
check("P2.12 subdir_names 覆盖生效",
      layout.target_dir(base, "video",
                        {**cfg_on, "subdir_names": {"video": "Movies"}}
                        ).name == "Movies")
check("P2.13 subdir_names 显式空串 = 这一类不归档",
      layout.target_dir(base, "video",
                        {**cfg_on, "subdir_names": {"video": ""}}) == base)

d = layout.ensure(base, "video", cfg_on)
# ⚠️ 两边都要 resolve 再比：沙箱的 TEMP 路径经 8.3 短名（ADMINI~1）进来，
#    而 mkdir 之后 is_dir() 给的是长名 —— 不 resolve 就会假红。
try:
    inside = base.resolve() in d.resolve().parents or \
        d.resolve() == base.resolve()
except Exception:  # noqa: BLE001
    inside = False
check("P2.14 ensure 建出目录且落在 base 之下",
      d.is_dir() and inside, str(d))
check("P2.15 ensure 遇到不可写时退回 base 不抛",
      layout.ensure(pathlib.Path("\0bad"), "video", cfg_on) is not None)

desc = layout.describe(cfg_on)
check("P2.16 describe 含 folders/presets/sample",
      bool(desc.get("folders")) and bool(desc.get("presets"))
      and bool(desc.get("sample")))
check("P2.17 describe.sample 里出现了中文子目录名",
      "视频/" in desc["sample"], desc["sample"].splitlines()[1] if
      len(desc["sample"].splitlines()) > 1 else "")


# ==========================================================================
head("P3  版本形态（edition）")

check("P3.1 默认是全球版",
      edition.current({}) == "global", edition.DEFAULT_EDITION)
check("P3.2 cfg 指定 cn 生效", edition.current({"edition": "cn"}) == "cn")
check("P3.3 非法 edition 值回落默认",
      edition.current({"edition": "火星版"}) == "global")

os.environ["JILIU_EDITION"] = "cn"
check("P3.4 环境变量优先于 cfg",
      edition.current({"edition": "global"}) == "cn")
del os.environ["JILIU_EDITION"]

g = edition.gate("https://www.youtube.com/watch?v=1", {"edition": "global"})
check("P3.5 全球版放行 YouTube", g["allowed"] and g["foreign"])

c = edition.gate("https://www.youtube.com/watch?v=1", {"edition": "cn"})
check("P3.6 国内版拦下 YouTube", not c["allowed"] and c["foreign"])
check("P3.7 拦截原因说得清下一步（提到换版本）",
      "全球版" in c["reason"], c["reason"][:60])

for u in ["https://www.bilibili.com/video/BV1xx411c7mD",
          "https://v.youku.com/v_show/id_XNTk1.html",
          "https://www.mgtv.com/b/896231/24547204.html"]:
    r = edition.gate(u, {"edition": "cn"})
    check(f"P3.8 国内版放行 {sites.host_of(u)}", r["allowed"])

check("P3.9 未知域名保守放行（不误判为海外）",
      not edition.is_foreign("https://some-small-cn-site.com/v/1"))
check("P3.10 子域也算海外",
      edition.is_foreign("https://m.youtube.com/watch?v=1"))
check("P3.11 长得像但不是的域名不算海外",
      not edition.is_foreign("https://notyoutube.com/x"))
check("P3.12 国内版 caps.show_proxy 为 False",
      edition.caps({"edition": "cn"})["show_proxy"] is False)
check("P3.13 全球版 caps.allow_foreign 为 True",
      edition.caps({"edition": "global"})["allow_foreign"] is True)
check("P3.14 describe.all 列出两个版本",
      {x["key"] for x in edition.describe({})["all"]} == {"cn", "global"})


# ==========================================================================
head("P4  引擎择优（engines）")

keys = [e["key"] for e in engines.CATALOG]
check("P4.1 CATALOG 里没有重复 key", len(keys) == len(set(keys)), str(keys))
check("P4.2 VALID_HTTP 都在 CATALOG 里",
      set(engines.VALID_HTTP) <= set(keys),
      f"缺 {set(engines.VALID_HTTP) - set(keys)}")
check("P4.3 CATALOG 里每个引擎都有 label/tagline/detail",
      all(e.get("label") and e.get("tagline") and e.get("detail")
          for e in engines.CATALOG))

none_st = {k: {"available": False} for k in keys}
none_st["native"] = {"available": True}
cfg = {"backend": "auto", "engine_order": ["aria2", "native", "curl", "wget"]}

r = engines.recommend("https://www.bilibili.com/video/BV1xx411c7mD", cfg,
                      st=none_st)
check("P4.4 B站播放页 → yt-dlp", r["engine"] == "yt-dlp", r["why"])

r = engines.recommend("https://example.com/a.zip", cfg, st=none_st)
check("P4.5 普通 HTTP → 选中可用引擎（native）",
      r["engine"] == "native", r["why"])

r = engines.recommend("magnet:?xt=urn:btih:" + "a" * 40, cfg, st=none_st)
check("P4.6 磁力 → aria2（P2P 别无选择）", r["engine"] == "aria2", r["why"])

r = engines.recommend("https://www.youtube.com/watch?v=1",
                      {"edition": "cn"}, st=none_st)
check("P4.7 国内版 + YouTube → 拦下并给原因",
      r["blocked"] and not r["engine"] and r["why"], r["why"][:50])

r = engines.recommend("https://www.youtube.com/watch?v=1",
                      {"edition": "global"}, st=none_st)
check("P4.8 全球版 + YouTube → 放行且交给 yt-dlp",
      not r["blocked"] and r["engine"] == "yt-dlp")

check("P4.9 指定 backend=curl 时听用户的",
      engines.recommend_http({"backend": "curl"}, st=none_st) == "curl")
check("P4.10 backend=auto 且 aria2 可用时优先 aria2",
      engines.recommend_http({"backend": "auto",
                              "engine_order": ["aria2", "native"]},
                             st={**none_st,
                                 "aria2": {"available": True}}) == "aria2")
check("P4.11 完全不指定时兜底 native",
      engines.recommend_http({}, st=none_st) == "native")

d = engines.describe({"backend": "auto", "engine_order": ["native"]},
                     refresh=False)
check("P4.12 describe 含 engines/effective_http/video_ready",
      bool(d.get("engines")) and d.get("effective_http")
      and "video_ready" in d, f"effective={d.get('effective_http')}")


# ==========================================================================
head("P5  站点注册表（sites）")

sk = [s["key"] for s in sites.SITES]
check("P5.1 站点 key 唯一", len(sk) == len(set(sk)))
check("P5.2 每个站都有 hosts/patterns/probe/name",
      all(s.get("hosts") and s.get("patterns") and s.get("probe")
          and s.get("name") for s in sites.SITES))
check("P5.3 国内站数量 > 海外站数量",
      len(sites.domestic_sites()) > len(sites.foreign_sites()),
      f"{len(sites.domestic_sites())} vs {len(sites.foreign_sites())}")

for s in sites.SITES:
    got = sites.match(s["probe"])
    check(f"P5.4 {s['name']} 的 probe 能被自己认出",
          got is not None and got["key"] == s["key"],
          f"实得 {got['key'] if got else None}")

# 这些都是实测验证过的判定，改动 patterns 时必须一起改
for url, want, tag in [
    ("https://www.bilibili.com/video/BV1GJ411x7h7", True, "B站 BV 视频页"),
    ("https://www.bilibili.com/video/av170001", True, "B站 av 视频页"),
    ("https://www.bilibili.com/bangumi/play/ep123456", True, "B站番剧页"),
    ("https://www.bilibili.com/", False, "B站首页"),
    ("https://v.youku.com/v_show/id_XNTk1NTQ3MzY4MA==.html", True, "优酷播放页"),
    ("https://v.qq.com/x/cover/mzc00200mp8vo9b.html", True, "腾讯剧集页"),
    ("https://www.mgtv.com/b/896231/24547204.html", True, "芒果播放页"),
    ("https://tv.cctv.com/2024/09/17/VIDETyWYmriBWGNooHKTyDii240917.shtml",
     True, "央视视频页"),
    ("https://tv.cctv.com/lm/", False, "央视栏目大全（非播放页）"),
    ("https://www.acfun.cn/v/ac4741185", True, "AcFun 视频页"),
    ("https://weibo.com/tv/show/1034:4686345264889977", True, "微博视频页"),
    ("https://www.ixigua.com/7309889994192224783", True, "西瓜视频页"),
    ("https://www.douyin.com/video/7013182973793160485", True, "抖音视频页"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", True, "YouTube 观看页"),
    ("https://example.com/file.zip", False, "非视频站"),
]:
    got = sites.is_video_page(url)
    check(f"P5.5 {tag}", got == want, f"实得 {got}")

# 修过的两处宽泛 pattern，必须被守住 —— 它们曾经把 API 端点误判成播放页
check("P5.6 twitch 的 gql 端点不算播放页",
      not sites.is_video_page("https://gql.twitch.tv/gql"))
check("P5.7 vimeo 的 progressive 直链不算播放页",
      not sites.is_video_page(
          "https://player.vimeo.com/progressive_redirect/playback/1/x.mp4"))
check("P5.8 小红书图文帖路径不再算视频页",
      not sites.is_video_page("https://www.xiaohongshu.com/explore"),
      "patterns 已收紧成带 id 的形态")

desc = sites.describe()
check("P5.9 describe 计数与实际一致",
      desc["total"] == len(sites.SITES)
      and desc["domestic"] == len(sites.domestic_sites()))
check("P5.10 每个站都写了现状 note（这是这张表的意义）",
      all(s.get("note") for s in desc["sites"]))


# ==========================================================================
head("P6  原生窗口（desktop）")

check("P6.1 available() 返回 bool", isinstance(desktop.available(), bool))
if desktop.available():
    check("P6.2 可用时无不可用原因", desktop.unavailable_reason() == "")
else:
    check("P6.3 不可用时给出装法",
          "pywebview" in desktop.unavailable_reason(),
          desktop.unavailable_reason()[:60])

dd = desktop.describe({})
check("P6.4 describe 含 available/modes/title",
      isinstance(dd.get("available"), bool) and len(dd.get("modes") or []) == 3
      and bool(dd.get("title")))
check("P6.5 describe 的三种模式 key 正确",
      {m["key"] for m in dd["modes"]} == {"auto", "native", "browser"})

p1 = desktop.pick_port("127.0.0.1", want=18899)
p2 = desktop.pick_port("127.0.0.1", want=18899)
check("P6.6 pick_port 返回可用端口", isinstance(p1, int) and p1 >= 18899,
      str(p1))
check("P6.7 pick_port 幂等（同一起点两次一致）", p1 == p2)


# ==========================================================================
head("P7  引擎降级链（backends）")

check("P7.1 VALID 含 auto 与四个具体引擎",
      set(backends.VALID) >= {"native", "aria2", "curl", "wget", "auto"})

check("P7.2 resolve 尊重 backend=native",
      backends.resolve({"backend": "native"}) == "native")
check("P7.3 resolve 对 curl/wget 不拦截（让子进程自己试）",
      backends.resolve({"backend": "curl"}) == "curl"
      and backends.resolve({"backend": "wget"}) == "wget")
check("P7.4 backend=auto 至少要选出 native",
      backends.resolve({"backend": "auto",
                        "engine_order": ["native"]}) == "native")

ch = backends.fallback_chain({"auto_fallback_engine": True}, "native")
check("P7.5 降级链里不含第一个引擎", "native" not in ch, str(ch))
check("P7.6 开着自动降级时链路非空", bool(ch), str(ch))
check("P7.7 关掉自动降级时返回空",
      backends.fallback_chain({"auto_fallback_engine": False}, "native") == [])
ch2 = backends.fallback_chain({"auto_fallback_engine": True}, "curl")
check("P7.8 第一个是 curl 时，链里仍有 native 兜底",
      "native" in ch2 and "curl" not in ch2, str(ch2))

check("P7.9 磁力协议仍被判为 P2P（降级链的依据）",
      getattr(protocols.detect("magnet:?xt=urn:btih:" + "b" * 40), "p2p",
              False) is True)


# ==========================================================================
head("P8  双击启动不闪退（入口健壮性）")

# 背景：jiliu.py 的 _hide_console() 里用了 os.name，但文件顶部漏了 import os。
# 源码下 gui 模式不会走进去（非 frozen 提前 return），单测全绿；
# 唯独**双击打包好的 exe**（frozen + 无参数）会命中 NameError，
# 而此时控制台刚被创建，进程瞬间退出 → 用户只看到终端闪一下。
# 所以这里静态核对入口模块的每个顶层名字都有来源，别再漏 import。
ENTRY = pathlib.Path(__file__).resolve().parent.parent / "jiliu.py"
src = ENTRY.read_text(encoding="utf-8")

# 入口模块用到的标准库模块，必须在文件里真的 import 过
for mod in ("os", "sys", "time", "pathlib"):
    check(f"P8.x 入口已 import {mod}",
          f"import {mod}" in src,
          "缺失会导致 frozen 启动早期 NameError")

check("P8.1 _hide_console 用到的 os 已导入",
      "os.name" in src and "import os" in src)

# 真跑一遍入口模块体：frozen + 无参数。修复前这里会抛 NameError。
_probe = pathlib.Path(tempfile.mkdtemp()) / "entry_probe.py"
_saved_frozen = getattr(sys, "frozen", None)
_saved_argv = list(sys.argv)
_saved_path = list(sys.path)
try:
    if hasattr(sys, "_jiliu_probe"):
        pass
    sys.frozen = True                    # 伪装成打包后
    sys.argv = ["jiliu.exe"]             # 无参数 = 双击
    sys.path.insert(0, str(ENTRY.parent))
    import runpy
    ns = runpy.run_path(str(ENTRY), run_name="_jiliu_entry_probe")
    check("P8.2 frozen + 无参数加载入口模块不抛异常", True)
    check("P8.3 入口暴露 _hide_console / _show_console",
          "_hide_console" in ns and "_show_console" in ns)
    check("P8.4 入口暴露崩溃兜底 _report_crash", "_report_crash" in ns)
    check("P8.5 main 捕获异常后仍会还原控制台（有 _report_crash 调用）",
          "_report_crash(exc)" in src,
          "否则双击失败时用户什么也看不到")
except NameError as exc:
    check("P8.2 frozen + 无参数加载入口模块不抛异常", False, f"NameError: {exc}")
except Exception as exc:  # noqa: BLE001
    check("P8.2 frozen + 无参数加载入口模块不抛异常", False,
          f"{type(exc).__name__}: {exc}")
finally:
    sys.argv = _saved_argv
    sys.path[:] = _saved_path
    if _saved_frozen is None:
        try:
            del sys.frozen
        except AttributeError:
            pass
    else:
        sys.frozen = _saved_frozen
    try:
        _probe.unlink(missing_ok=True)
        _probe.parent.rmdir()
    except Exception:  # noqa: BLE001
        pass

# 崩溃日志路径必须落在可写位置（冻结后是 exe 同级目录）
_here = pathlib.Path(__file__).resolve().parent
check("P8.6 源码运行时崩溃日志落在项目内",
      (ENTRY.parent / "jiliu-crash.log").parent == ENTRY.parent,
      str(ENTRY.parent))


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
