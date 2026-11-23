#!/usr/bin/env python3
"""B 站弹幕回归（P1-11）。

    python tests/test_danmaku.py

不联网：XML 用本地构造，cid 解析用假 bili 模块替身。
重点验证「转 ASS 真的能播」那几件事：时间轴格式、颜色字节序、
轨道不重叠、花括号被吃掉会毁行。
"""
import io
import os
import pathlib
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import config as _config, danmaku  # noqa: E402

CFG = dict(_config.DEFAULTS)

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<i>
  <chatid>123</chatid>
  <d p="1.500,1,25,16777215,1500000000,0,abc,0">第一条 普通弹幕</d>
  <d p="2.000,5,25,16711680,1500000000,0,abc,0">顶部红字</d>
  <d p="2.500,4,18,65280,1500000000,0,abc,0">底部绿字</d>
  <d p="3.000,1,25,255,1500000000,0,abc,0">蓝色滚动</d>
  <d p="3.200,6,25,16777215,1500000000,0,abc,0">逆向弹幕</d>
  <d p="4.000,7,25,16777215,1500000000,0,abc,0">高级弹幕（应被丢弃）</d>
  <d p="5.000,1,25,16777215,1500000000,0,abc,0">带 &amp; 实体 &lt;tag&gt;</d>
  <d p="6.000,1,25,16777215,1500000000,0,abc,0">花括号 {攻击} 测试</d>
</i>
"""

print("== 解析 ==")
items = danmaku.parse_xml(SAMPLE)
check("解析出 8 条", len(items) == 8, str(len(items)))
check("按时间排序", [x["t"] for x in items] == sorted(x["t"] for x in items))
check("mode 正确", items[0]["mode"] == 1 and items[1]["mode"] == 5, str([x["mode"] for x in items[:3]]))
check("颜色解析", items[1]["color"] == 16711680, str(items[1]["color"]))
check("实体已还原", items[6]["text"] == "带 & 实体 <tag>", items[6]["text"])
check("空 XML 不炸", danmaku.parse_xml("") == [])

print("== XML 回写 ==")
back = danmaku.to_xml(items)
check("回写有声明", back.startswith("<?xml"))
check("回写条目数一致", back.count("<d p=") == 8, str(back.count("<d p=")))
check("实体被正确转义", "&amp;" in back)
_re = danmaku.parse_xml(back)
check("回写可再解析", len(_re) == 8 and _re[0]["t"] == 1.5)

print("== 转 ASS ==")
ass = danmaku.to_ass(items)
check("有 Script Info", "[Script Info]" in ass)
check("PlayRes 写了", "PlayResX: 1920" in ass and "PlayResY: 1080" in ass)
check("有 Events", "[Events]" in ass)
check("样式含字体", "Style: Default,Microsoft YaHei" in ass)

# 时间轴：H:MM:SS.cc
times = re.findall(r"Dialogue: \d+,(\d+:\d\d:\d\d\.\d\d),(\d+:\d\d:\d\d\.\d\d)", ass)
check("时间轴格式正确", len(times) >= 6, f"{len(times)} 行")
check("起始时间对得上", times[0][0] == "0:00:01.50", times[0][0])
check("结束时间晚于起始", times[0][1] > times[0][0], f"{times[0][0]} → {times[0][1]}")

# 颜色字节序：16711680 = 0xFF0000 纯红 → ASS &H0000FF&
check("红色转 BGR", "\\c&H0000FF&" in ass)
check("蓝色 255 转 BGR", "\\c&HFF0000&" in ass)
check("绿色 65280 转 BGR", "\\c&H00FF00&" in ass)

# 高级弹幕
check("高级弹幕被丢弃", "高级弹幕" not in ass)
ass_keep = danmaku.to_ass(items, skip_special=False)
check("关掉过滤后保留", "高级弹幕" in ass_keep)

# 花括号：进入 ASS 前必须被替换，否则整行失效
check("花括号已替换", "{攻击}" not in ass and "｛攻击｝" in ass)

# 轨道分配：同一时刻的弹幕 y 坐标不能全一样
y_all = [int(m) for m in re.findall(r"\\move\(\d+,(\d+),", ass)]
check("滚动弹幕分了多条轨道", len(set(y_all)) >= 3, f"{len(set(y_all))} 条轨道 / {len(y_all)} 条")
check("轨道 y 在画面内", all(0 < y < 1080 for y in y_all), str(sorted(set(y_all))[:5]))

# 固定弹幕用 pos 而不是 move
tops = re.findall(r"\\an8\\pos\(\d+,(\d+)\)", ass)
check("顶部弹幕有 pos", len(tops) == 1, str(tops))
bots = re.findall(r"\\an2\\pos\(\d+,(\d+)\)", ass)
check("底部弹幕有 pos", len(bots) == 1, str(bots))
check("底部 y 靠下", int(bots[0]) > 540, bots[0])

# 分辨率变化时字号跟着缩放
small = danmaku.to_ass(items, width=1280, height=720)
check("720p 字号更小", ",28," in small or ",24," in small, small.split("Style: Default,")[1][:30])

# 全透明 / 满不透明都合法
for a in (0.0, 1.0):
    s = danmaku.to_ass(items, alpha=a)
    check(f"alpha={a} 可生成", "\\alpha&H" in s)

# 空列表
check("空列表也能出骨架", "Dialogue:" not in danmaku.to_ass([]))

print("== 落盘 ==")
TMP = pathlib.Path(tempfile.mkdtemp(prefix="jiliu-dm-"))
base = TMP / "某视频 S01E01.1080p"
r = danmaku.save(SAMPLE, base, formats=("xml", "ass", "json"))
check("保存成功", r["ok"] is True and r["count"] == 8, str(r.get("error")))
check("xml 文件名带 danmaku", pathlib.Path(r["files"]["xml"]).name.endswith(".danmaku.xml"),
      pathlib.Path(r["files"]["xml"]).name)
check("ass 与视频同名", pathlib.Path(r["files"]["ass"]).name == "某视频 S01E01.1080p.ass",
      pathlib.Path(r["files"]["ass"]).name)
check("json 也写了", "json" in r["files"])
check("ass 内容有画面行", "Dialogue:" in pathlib.Path(r["files"]["ass"]).read_text(encoding="utf-8"))
check("xml 内容可再解析",
      len(danmaku.parse_xml(pathlib.Path(r["files"]["xml"]).read_text(encoding="utf-8"))) == 8)

# 只写 xml
r2 = danmaku.save(SAMPLE, TMP / "only", formats=("xml",))
check("只写 xml 时不产 ass", "ass" not in r2["files"] and "xml" in r2["files"])

# 空弹幕
r3 = danmaku.save("", TMP / "empty")
check("空弹幕报错而不写空文件", r3["ok"] is False and r3["count"] == 0, str(r3.get("error")))

print("== max_items 截断 ==")
big = danmaku.parse_xml(SAMPLE) * 3
cut = danmaku.to_ass(big, max_items=5)
check("截到 5 条", cut.count("Dialogue:") == 5, str(cut.count("Dialogue:")))

print("== 描述 ==")
d = danmaku.describe(CFG)
check("describe 有格式列表", d["formats"] == ["xml", "ass", "json"], str(d["formats"]))

print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
