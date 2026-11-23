#!/usr/bin/env python3
"""原子写盘与损坏恢复的回归。

    python tests/test_atomicio.py

这些用例对应 1.9.5 修的 P0：
  · 多线程同时 save 不能写坏 tasks.json（以前固定 tmp 名 + 锁外写）
  · 断电/半截 JSON 要能从 .bak 恢复，而不是静默清空
  · 写失败要抛出来，不能 except: pass
"""
import io
import json
import os
import pathlib
import sys
import tempfile
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_TMP = tempfile.mkdtemp(prefix="jiliu-atomic-")
os.environ["JILIU_HOME"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import atomicio  # noqa: E402

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


print("== 1 基本写入 ==")
d = pathlib.Path(_TMP)
p = d / "state.json"
atomicio.write_json(p, {"a": 1, "中文": "值"})
check("写入后可解析", json.loads(p.read_text(encoding="utf-8"))["a"] == 1)
check("没有残留 tmp", not list(d.glob("*.tmp")) and not list(d.glob(".*.tmp")))

print("== 2 并发写（P0 回归） ==")
errors = []


def writer(idx):
    try:
        for j in range(40):
            atomicio.write_json(p, {"writer": idx, "j": j, "pad": "x" * 512})
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")


threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
check("8 线程 ×40 次无异常", not errors, errors[:1])
try:
    data = json.loads(p.read_text(encoding="utf-8"))
    check("最终文件是完整 JSON", isinstance(data.get("j"), int), data)
except Exception as exc:  # noqa: BLE001
    check("最终文件是完整 JSON", False, exc)
check("保留了 .bak", (d / "state.json.bak").is_file())

print("== 3 损坏自动恢复 ==")
atomicio.write_json(p, {"good": True})
backup_snapshot = (d / "state.json.bak").read_text(encoding="utf-8")
p.write_text('{"broken": ', encoding="utf-8")
got = atomicio.read_json(p, default=None)
check("从 .bak 恢复出上一份内容", isinstance(got, dict), got)
check("主文件被修复", json.loads(p.read_text(encoding="utf-8")) == got)
check("备份内容没被顺手破坏", (d / "state.json.bak").read_text(encoding="utf-8") != "")


print("== 4 无备份时留档，不静默清空 ==")
q = d / "only.json"
q.write_text("{not json", encoding="utf-8")
got = atomicio.read_json(q, default=[])
check("返回默认值", got == [])
check("坏文件被改名留档", any(x.name.startswith("only.json.corrupt-") for x in d.iterdir()))


print("== 5 不可写路径必须抛异常 ==")
try:
    atomicio.write_json(d / "no" / "\x00bad" / "x.json", {"a": 1})
    raised = False
except Exception:  # noqa: BLE001
    raised = True
check("写失败不吞异常", raised)

print("-" * 60)
print(f"结果: {ok}/{ok + fail} 通过")
sys.exit(0 if fail == 0 else 1)
