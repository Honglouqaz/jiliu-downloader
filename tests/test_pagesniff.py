#!/usr/bin/env python3
"""网页视频解析（pagesniff）的回归测试。

这一组守的是三件容易被改坏的事：

  1) **覆盖面**：播放器变量 / <video> / iframe / 内联脚本 / og:video / 直链，
     六种形态各一条用例。少认一种，就有一批站点在用户那里"解析失败"。
  2) **不乱猜**：encrypt=1、DRM、blob 这三种必须明确说"做不到 + 为什么"，
     不能返回一个假地址让用户下到一半失败 —— 后者比不支持更伤。
  3) **请求头**：防盗链 CDN 只认 Referer/Origin/UA/Cookie，
     少一个就是 403，而用户看不到 403，只看到"下不动"。

不发真实网络请求：抓页面走 fetch 注入点，播放列表用假 master。
真实链路的验证由 scripts/ 里的手工脚本负责（mux 测试流）。

    python tests/test_pagesniff.py
"""
# -*- coding: utf-8 -*-
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import pagesniff as ps, video  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [OK  ] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  → {extra}" if extra else ""))


def urls(res):
    return [s["url"] for s in res["streams"]]


PAGE = "https://www.mysite.com/vodplay/1-1-1.html"

# ---------------------------------------------------------------- 六种形态
print("\n[1] 页面解析：六种可下载形态")

H_PLAYER = """<html><head><title>剧名 第01集_在线观看 - 某影院</title></head><body>
<script>var player_aaaa={"flag":"play","encrypt":0,"url":"https://cdn.a.com/hls/index.m3u8"};</script>
</body></html>"""
r = ps.parse_page(H_PLAYER, PAGE)
check("播放器变量 player_aaaa → m3u8", urls(r) == ["https://cdn.a.com/hls/index.m3u8"], urls(r))
check("来源标记为 player_aaaa", r["streams"] and r["streams"][0]["how"] == "player_aaaa")

H_MULTI = """<script>var player_aaaa={"encrypt":0,
"url":"4K$https://cdn.a.com/4k/index.m3u8#1080P$https://cdn.a.com/1080/index.m3u8#720P$https://cdn.a.com/720/index.m3u8"};
var player_bbbb={"encrypt":0,"url":"https://cdn.b.com/backup/index.m3u8"};</script>"""
r = ps.parse_page(H_MULTI, PAGE)
check("多清晰度 # / $ 拆分（3 档）", len(urls(r)) == 4, urls(r))
check("清晰度名称带到 label", [s["label"] for s in r["streams"][:3]] == ["4K", "1080P", "720P"],
      [s["label"] for s in r["streams"]])
check("多线路 player_bbbb 也收", "https://cdn.b.com/backup/index.m3u8" in urls(r))

H_TAGS = """<video id=v src="/static/hls/index.m3u8?t=1" poster="/a.jpg"></video>
<source src="//cdn.c.com/x.flv" title="高清">
<div data-url="https://cdn.c.com/data-src.mp4"></div>"""
r = ps.parse_page(H_TAGS, PAGE)
check("<video> 相对路径补全", "https://www.mysite.com/static/hls/index.m3u8?t=1" in urls(r), urls(r))
check("<source> 协议相对 // 补全", "https://cdn.c.com/x.flv" in urls(r), urls(r))
check("data-url 属性", "https://cdn.c.com/data-src.mp4" in urls(r), urls(r))
check("封面图不进候选", not any(u.endswith(".jpg") for u in urls(r)))

H_IFRAME = ('<iframe src="/player/?url=' +
            "https%3A%2F%2Fcdn.d.com%2Fplay%2Findex.m3u8" + '"></iframe>')
r = ps.parse_page(H_IFRAME, PAGE)
check("iframe 被收集", r["iframes"] == ["https://www.mysite.com/player/?url="
                                       "https%3A%2F%2Fcdn.d.com%2Fplay%2Findex.m3u8"], r["iframes"])
check("iframe 的 ?url= 参数解码成流地址", "https://cdn.d.com/play/index.m3u8" in urls(r), urls(r))

H_INLINE = r"""<script>
var cfg={"src":"https:\/\/cdn.e.com\/v\/index.m3u8?sign=abc\u0026e=1700000000"};
</script>"""
r = ps.parse_page(H_INLINE, PAGE)
check("内联脚本 \\/ 转义可识别", "https://cdn.e.com/v/index.m3u8?sign=abc&e=1700000000" in urls(r),
      urls(r))

H_META = """<meta property="og:video" content="https://cdn.f.com/og.mp4">
<script type="application/ld+json">{"@type":"VideoObject","contentUrl":"https://cdn.f.com/ld.m3u8"}</script>"""
r = ps.parse_page(H_META, PAGE)
check("og:video", "https://cdn.f.com/og.mp4" in urls(r), urls(r))
check("JSON-LD contentUrl", "https://cdn.f.com/ld.m3u8" in urls(r), urls(r))

check("流直链直接透传", ps.looks_media("https://x.com/a/index.m3u8?k=1"))
check("kind_of 区分 hls/dash/file",
      (ps.kind_of("a.m3u8"), ps.kind_of("a.mpd"), ps.kind_of("a.mp4")) == ("hls", "dash", "file"))

# ---------------------------------------------------------------- 去重与噪音
print("\n[2] 去重 / 噪音 / 排序")

r = ps.parse_page('<video src="/a.m3u8?t=1"></video><script>var u="/a.m3u8";</script>', PAGE)
check("同一条流只出现一次", len(r["streams"]) == 1, urls(r))
check("保留带查询串的那个", r["streams"][0]["url"].endswith("?t=1"), r["streams"][0]["url"])

r = ps.parse_page('<img src="https://hm.baidu.com/a.m3u8">', PAGE)
check("统计探针域名被过滤", urls(r) == [], urls(r))

r = ps.parse_page('<script>var a="https://cdn.g.com/1.mp4";var b="https://cdn.g.com/2.m3u8";</script>', PAGE)
check("HLS 排在整段文件前面", r["streams"][0]["kind"] == "hls", urls(r))

check("标题去掉站点尾巴",
      ps.page_title("<title>剧名 第01集_在线观看 - 某影院</title>") == "剧名 第01集",
      ps.page_title("<title>剧名 第01集_在线观看 - 某影院</title>"))

# ---------------------------------------------------------------- 明确不做
print("\n[3] 不做的事要说清楚（不能给假地址）")

r = ps.parse_page('<script>var player_aaaa={"encrypt":1,"url":"U2FsdGVkX19abc=="};</script>', PAGE)
check("encrypt=1 不进候选", urls(r) == [], urls(r))
check("encrypt=1 记进 encrypted", len(r["encrypted"]) == 1, r["encrypted"])

r = ps.parse_page('<script src="https://x/widevine.js"></script>', PAGE)
check("DRM 关键字被识别", r["drm"] is True)

r = ps.parse_page('<video src="blob:https://x/abc"></video>', PAGE)
check("blob: 不进候选", urls(r) == [], urls(r))
check("blob: 给出解释", any("blob" in n for n in r["notes"]), r["notes"])

# ---------------------------------------------------------------- 伪造网络
print("\n[4] resolve（用注入的 fetch，不发真实请求）")

PAGES = {
    "https://s1.com/play/1.html": """<html><head><title>第01集</title></head><body>
        <iframe src="https://s1.com/player/1.html"></iframe></body></html>""",
    "https://s1.com/player/1.html": """<script>var player_aaaa={"encrypt":0,
        "url":"https://cdn.s1.com/hls/1.m3u8"};</script>""",
    "https://cdn.s1.com/hls/1.m3u8": """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,NAME="360"
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,NAME="1080"
1080/index.m3u8
""",
}
CALLS = []


def fake_fetch(url, cfg, headers=None, timeout=15.0):
    CALLS.append((url, headers or {}))
    if url in PAGES:
        return 200, url, PAGES[url]
    raise RuntimeError("unexpected " + url)


cfg = {"proxy_mode": "off", "video_user_agent": "UA-TEST"}
res = ps.resolve("https://s1.com/play/1.html", cfg, fetch=fake_fetch)
check("iframe 递归到播放器页", any("player/1.html" in c[0] for c in CALLS), [c[0] for c in CALLS])
check("两层页面都能出流", len(res["streams"]) >= 1, res["streams"])
check("master 展开成多清晰度", len(res["streams"]) == 2, urls(res))
check("清晰度降序（1080 在前）", res["streams"][0].get("quality") == "1080P",
      [s.get("quality") for s in res["streams"]])
check("展开后保留 variants_of", res["streams"][0].get("variants_of") == "https://cdn.s1.com/hls/1.m3u8")
check("标题从页面取", res["page"]["title"] == "第01集", res["page"])
h = res["streams"][0].get("headers") or {}
check("Referer 指回播放器页", h.get("Referer", "").endswith("/player/1.html"), h)
check("Origin 补上", h.get("Origin") == "https://s1.com", h)
check("UA 透传", h.get("User-Agent") == "UA-TEST", h)

CALLS.clear()
res = ps.resolve("https://s1.com/play/1.html", cfg, depth=0, fetch=fake_fetch)
check("depth=0 不递归 iframe", not any("player" in c[0] for c in CALLS), [c[0] for c in CALLS])
check("depth=0 时如实报失败", res["ok"] is False and bool(res.get("error")), res.get("error"))

# 登录态：调用方带的 cookie 要进到 stream headers 里
CALLS.clear()
res = ps.resolve("https://cdn.s1.com/hls/1.m3u8", cfg,
                 page_headers={"Cookie": "sid=abc", "Referer": "https://s1.com/play/1.html"},
                 probe_quality=False, fetch=fake_fetch)
check("直链也带 Cookie", (res["streams"][0]["headers"] or {}).get("Cookie") == "sid=abc",
      res["streams"][0]["headers"])
check("probe_quality=False 不额外请求 playlist", [c[0] for c in CALLS] == [],
      [c[0] for c in CALLS])

# 全站 encrypt=1：必须给"换路子"的建议，而不是"失败"
ENC = {"https://s2.com/p/1.html": '<script>var player_aaaa={"encrypt":1,"url":"abc"};</script>'}
res = ps.resolve("https://s2.com/p/1.html", cfg,
                 fetch=lambda u, c, headers=None, timeout=15: (200, u, ENC[u]))
check("encrypt 站点 ok=False", res["ok"] is False)
check("encrypt 站点指向扩展嗅探", "扩展" in (res.get("hint") or ""), res.get("hint"))

# DRM 页面：结论要明确
DRM = {"https://s3.com/p/1.html": '<script src="/widevine.js"></script>'}
res = ps.resolve("https://s3.com/p/1.html", cfg,
                 fetch=lambda u, c, headers=None, timeout=15: (200, u, DRM[u]))
check("DRM 页面给出 DRM 结论", "DRM" in (res.get("error") or ""), res.get("error"))

# 坏地址
res = ps.resolve("ftp://x.com/a", cfg, fetch=fake_fetch)
check("非 http 地址直接拒绝", res["ok"] is False)

# ---------------------------------------------------------------- yt-dlp 参数
print("\n[5] 请求头透传给 yt-dlp / 分片并发")

args = video._base_args({"timeout": 30}, "https://cdn.a.com/1.m3u8",
                        {"Referer": "https://p.com/v/1", "Cookie": "sid=1",
                         "Origin": "https://p.com", "User-Agent": "UA-X",
                         "X-Token": "t1"})
joined = " ".join(args)
check("Referer 走 --referer", "--referer https://p.com/v/1" in joined, joined)
check("Cookie 走 --add-header", "--add-header Cookie: sid=1" in joined, joined)
check("Origin 走 --add-header", "--add-header Origin: https://p.com" in joined, joined)
check("自定义头也透传", "--add-header X-Token: t1" in joined, joined)
check("UA 覆盖", "--user-agent UA-X" in joined, joined)
check("headers=None 不产生空头", "--add-header" not in " ".join(
    video._base_args({"timeout": 30}, "https://a.com/x.mp4")))

check("m3u8 开分片并发", video._fragment_args("https://a.com/x.m3u8?sig=1", {}) ==
      ["--concurrent-fragments", "8"])
check("mpd 开分片并发", video._fragment_args("https://a.com/x.mpd", {}) != [])
check("普通文件不开", video._fragment_args("https://a.com/x.zip", {}) == [])
check("并发上限 16", video._fragment_args("https://a.com/x.m3u8", {"stream_concurrency": 64}) ==
      ["--concurrent-fragments", "16"])
check("并发可关", video._fragment_args("https://a.com/x.m3u8", {"stream_concurrency": 1}) == [])

# ---------------------------------------------------------------- 能力清单
print("\n[6] 能力清单（界面文案的唯一来源）")

d = ps.describe()
check("六种形态", len(d["forms"]) == 6, len(d["forms"]))
check("四类不做", len(d["unsupported"]) == 4, len(d["unsupported"]))
check("每条不做都有原因", all(x.get("why") for x in d["unsupported"]))

print(f"\n结果: {ok}/{ok + fail} 通过")
sys.exit(1 if fail else 0)
