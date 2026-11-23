#!/usr/bin/env python3
"""任务导入导出回归（P1-12）。

    python tests/test_transfer.py

三种格式都做「导出 → 再导入」的闭环，重点看：
aria2 的选项缩进与 checksum 写法、txt 里的用户名/文件名、JSON 的字段保真。
"""
import io
import os
import pathlib
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import config as _config, transfer  # noqa: E402
from jiliu.task import Task  # noqa: E402

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


def mk(url, **kw):
    t = Task(url=url, kind="http", threads=8, dest_dir="D:/dl")
    for k, v in kw.items():
        setattr(t, k, v)
    return t


SHA = "a" * 64
T1 = mk("https://e.com/a.zip", filename="a.zip", queue="video", chunk_size=4194304,
        verify_hash=True, expected_sha256=SHA, category="archive")
T1.meta = {"headers": {"Referer": "https://e.com/", "User-Agent": "Jiliu/1.8"},
           "mirrors": ["https://m1.e.com/a.zip"]}
T2 = mk("https://e.com/b.iso", filename="b.iso", queue="default")
T2.size = 12345
T3 = mk("magnet:?xt=urn:btih:deadbeef&dn=test", filename="test")
T4 = mk("https://www.bilibili.com/video/BV1xx411c7mD", kind="video", threads=1,
        queue="video")
T4.meta = {"playlist": False, "audio_only": True}
TASKS = [T1, T2, T3, T4]

print("== 导出 JSON ==")
r = transfer.export(TASKS, "json")
check("导出成功", r["ok"] is True, str(r.get("error")))
check("计数正确", r["count"] == 4, str(r["count"]))
import json  # noqa: E402
env = json.loads(r["text"])
check("带 magic", env["magic"] == transfer.MAGIC, env.get("magic"))
check("带版本", env["version"] == 1)
check("不含运行态", "status" not in r["text"] and "percent" not in r["text"])
check("队列保留", env["tasks"][0]["queue"] == "video")
check("请求头保留", env["tasks"][0]["meta"]["headers"]["Referer"] == "https://e.com/")
check("哈希保留", env["tasks"][0]["expected_sha256"] == SHA)
check("线程保留", env["tasks"][0]["threads"] == 8)

print("== 回读 JSON ==")
back = transfer.import_text(r["text"])
check("识别为 json", back["fmt"] == "json", back["fmt"])
check("条数一致", back["count"] == 4, str(back["count"]))
check("url 一致", [x["url"] for x in back["tasks"]] == [t.url for t in TASKS])
b0 = back["tasks"][0]
check("队列回读", b0.get("queue") == "video", str(b0.get("queue")))
check("线程回读", b0.get("threads") == 8, str(b0.get("threads")))
check("请求头回读", b0["meta"]["headers"]["User-Agent"] == "Jiliu/1.8")
check("video 类型回读", back["tasks"][3].get("kind") == "video")
check("video meta 回读", back["tasks"][3]["meta"].get("audio_only") is True)

print("== 导出 aria2 ==")
a = transfer.export(TASKS, "aria2")
check("导出成功", a["ok"] is True)
check("视频任务被跳过", a["skipped"] == 1, str(a["skipped"]))
check("用了 3 条", a["count"] == 3, str(a["count"]))
lines = a["text"].splitlines()
check("有注释头", any(x.startswith("#") for x in lines))
check("URI 顶格", any(x.startswith("https://e.com/a.zip") for x in lines))
check("选项有缩进", any(x.startswith("  dir=") for x in lines))
check("split 写了", "  split=8" in a["text"])
check("header 写了", "  header=Referer: https://e.com/" in a["text"])
check("checksum 带算法前缀", f"  checksum=sha-256={SHA}" in a["text"])
check("磁力也在里面", "magnet:?xt=urn:btih:deadbeef&dn=test" in a["text"])
check("视频被注释说明", "# 跳过视频任务" in a["text"])
check("header 里的换行被压平", "\r" not in a["text"])

print("== 回读 aria2 ==")
ai = transfer.import_text(a["text"])
check("识别为 aria2", ai["fmt"] == "aria2", ai["fmt"])
check("条数一致", ai["count"] == 3, str(ai["count"]))
u = {x["url"]: x for x in ai["tasks"]}
check("dir 回读", u["https://e.com/a.zip"].get("dest_dir") == "D:/dl",
      str(u["https://e.com/a.zip"].get("dest_dir")))
check("out 回读", u["https://e.com/a.zip"].get("filename") == "a.zip")
check("线程回读", u["https://e.com/a.zip"].get("threads") == 8)
check("header 回读", u["https://e.com/a.zip"]["meta"]["headers"]["Referer"] == "https://e.com/")
check("checksum 回读成 sha256", u["https://e.com/a.zip"].get("expected_sha256") == SHA)
check("磁力回读", "magnet:?xt=urn:btih:deadbeef&dn=test" in u)

print("== 镜像合并（同 dir+out 两个源）==")
mir = """https://a.com/f.bin
  dir=/data
  out=f.bin
https://b.com/mirror/f.bin
  dir=/data
  out=f.bin
https://c.com/other.bin
  dir=/data
  out=other.bin
"""
mi = transfer.import_text(mir)
check("合并成 2 条", mi["count"] == 2, str(mi["count"]))
first = mi["tasks"][0]
check("镜像收进 meta", first["meta"]["mirrors"] == ["https://b.com/mirror/f.bin"],
      str(first["meta"].get("mirrors")))
check("第二条独立", mi["tasks"][1]["url"] == "https://c.com/other.bin")

print("== 导出 txt ==")
t = transfer.export(TASKS, "txt")
check("导出成功", t["ok"] is True and t["count"] == 4)
check("一行一个链接", len([x for x in t["text"].splitlines() if x.strip()]) == 4)
check("带文件名用 Tab", "https://e.com/a.zip\ta.zip" in t["text"])

print("== 回读 txt ==")
ti = transfer.import_text(t["text"])
check("识别为 txt", ti["fmt"] == "txt", ti["fmt"])
check("条数一致", ti["count"] == 4, str(ti["count"]))
check("文件名回读", ti["tasks"][0].get("filename") == "a.zip",
      str(ti["tasks"][0].get("filename")))

print("== txt 的杂食性 ==")
messy = """我的下载清单
https://e.com/1.zip
  随便一句说明 https://e.com/2.zip 后面还有字
https://e.com/1.zip
# 注释行
magnet:?xt=urn:btih:abc
"""
mi2 = transfer.import_text(messy)
check("从散文里捞链接", mi2["count"] == 3, str([x["url"] for x in mi2["tasks"]]))
check("重复链接去重", mi2["count"] == 3)

print("== 格式识别 ==")
check("识别 json 数组", transfer.detect_format('[{"url":"http://x"}]') == "json")
check("识别 json 对象", transfer.detect_format('{"tasks":[]}') == "json")
check("识别 aria2", transfer.detect_format("http://x/a\n  out=a\n") == "aria2")
check("识别 txt", transfer.detect_format("http://x/a\nhttp://x/b\n") == "txt")
check("空文本", transfer.detect_format("   ") == "")

print("== 异常输入 ==")
bad = transfer.import_text("{不是 JSON")
check("坏 JSON 报错不炸", bad["ok"] is False and bad["tasks"] == [], str(bad.get("error")))
e = transfer.export(TASKS, "csv")
check("不支持的格式明确拒绝", e["ok"] is False and "不认识" in e["error"], str(e.get("error")))
check("空文本导入报错", transfer.import_text("")["ok"] is False)

print("== 文件读写 ==")
TMP = pathlib.Path(tempfile.mkdtemp(prefix="jiliu-tr-"))
p1 = TMP / "tasks.json"
s1 = transfer.save_file(TASKS, p1)
check("存 JSON 成功", s1["ok"] is True and p1.is_file(), str(s1.get("path")))
l1 = transfer.load_file(p1)
check("读 JSON 成功", l1["ok"] is True and l1["count"] == 4, str(l1.get("error")))

p2 = TMP / "tasks.txt"
s2 = transfer.save_file(TASKS, p2, "aria2")
check("存 aria2 成功", s2["ok"] is True, str(s2.get("error")))
check("aria2 后缀也是 txt 不冲突", l1["fmt"] == "json" and s2["fmt"] == "aria2")
p3 = TMP / "urls.txt"
s3 = transfer.save_file(TASKS, p3, "txt")
check("存 txt 成功", s3["ok"] is True)
l3 = transfer.load_file(p3)
check("读 txt 成功", l3["count"] == 4, str(l3["count"]))
check("读不存在的文件不炸", transfer.load_file(TMP / "nope.json")["ok"] is False)

print("== describe ==")
d = transfer.describe()
check("三种格式都在", d["formats"] == ["json", "aria2", "txt"], str(d["formats"]))

print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
