#!/usr/bin/env python3
"""多队列调度回归（P0-3）。

    python tests/test_queues.py

不启动真实下载：用 object.__new__ 绕过 TaskManager.__init__（它一构造
就起调度线程），只测 _pick_next 的挑选逻辑与 queues 的收敛逻辑。
"""
import io
import os
import sys
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JILIU_HOME", os.path.join(os.environ.get("TEMP", "."), "jiliu-qt"))

from jiliu import config as _config, queues  # noqa: E402
from jiliu.manager import TaskManager  # noqa: E402
from jiliu.task import Task  # noqa: E402

ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {label}" + (f"  -- {detail}" if detail else ""))
    else:
        fail += 1
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))


def make_tm(cfg):
    tm = object.__new__(TaskManager)
    tm.cfg = cfg
    tm._lock = threading.RLock()
    tm._tasks = {}
    tm._order = []
    tm._rr = 0
    tm._live = {}
    return tm


def add(tm, tid, qid, prio=0):
    t = Task(url=f"http://x/{tid}", id=tid, queue=qid, priority=prio)
    tm._tasks[tid] = t
    tm._order.append(tid)
    return t


# ----------------------------------------------------------------------
# 1. normalize
# ----------------------------------------------------------------------
print("== 队列收敛 ==")
cfg = {"queues": []}
qs = queues.normalize(cfg)
check("空配置给两条默认队列", len(qs) == 2, [q["id"] for q in qs])
check("必有 default", any(q["id"] == "default" for q in qs))
check("default 并发 3", queues.get(cfg, "default")["max_concurrent"] == 3)

qs = queues.normalize({"queues": [
    {"id": "a b!!", "name": "带空格", "max_concurrent": 999, "speed_limit": -5},
    {"id": "a b!!", "name": "重复"},
    "垃圾",
    {"name": "只有名字"},
]})
ids = [q["id"] for q in qs]
check("非法字符被清洗", "ab" in ids, str(ids))
check("重复 id 只留一个", ids.count("ab") == 1)
check("非 dict 被丢弃", len([q for q in qs if not isinstance(q, dict)]) == 0)
check("并发被钳到上限", queues.get({"queues": qs}, "ab")["max_concurrent"] == queues.MAX_CONCURRENT_LIMIT)
check("负限速归零", queues.get({"queues": qs}, "ab")["speed_limit"] == 0)
check("缺 default 时自动补", "default" in ids, str(ids))

check("resolve 未知队列落到 default",
      queues.resolve({"queues": []}, "nope") == "default")
check("speed_limit_for 未知队列返回 0",
      queues.speed_limit_for({"queues": []}, "nope") == 0)

# ----------------------------------------------------------------------
# 2. 单队列配额
# ----------------------------------------------------------------------
print("== 队列配额 ==")
cfg = {"queues": [
    {"id": "default", "name": "默认", "max_concurrent": 1},
    {"id": "video", "name": "视频", "max_concurrent": 2},
], "max_concurrent": 0}
tm = make_tm(cfg)
for i in range(4):
    add(tm, f"d{i}", "default")
for i in range(4):
    add(tm, f"v{i}", "video")

picked = []
for i in range(8):
    t = tm._pick_next()
    if t is None:
        break
    t.status = "running"
    picked.append((t.id, t.queue))

d_run = sum(1 for _, q in picked if q == "default")
v_run = sum(1 for _, q in picked if q == "video")
check("default 只起 1 个", d_run == 1, f"default={d_run}")
check("video 起满 2 个", v_run == 2, f"video={v_run}")
check("总数 = 配额之和", len(picked) == 3, str(picked))
check("满了以后返回 None", tm._pick_next() is None)

# 放掉一个 default，应该立刻补进
for t in tm._tasks.values():
    if t.queue == "default" and t.status == "running":
        t.status = "done"
        break
nxt = tm._pick_next()
check("有名额时补位", nxt is not None and nxt.queue == "default",
      getattr(nxt, "id", "None"))

# ----------------------------------------------------------------------
# 3. 队列间轮转（不饿死）
# ----------------------------------------------------------------------
print("== 轮转公平 ==")
cfg2 = {"queues": [
    {"id": "default", "name": "默认", "max_concurrent": 5},
    {"id": "video", "name": "视频", "max_concurrent": 5},
], "max_concurrent": 0}
tm2 = make_tm(cfg2)
for i in range(3):
    add(tm2, f"d{i}", "default")
for i in range(3):
    add(tm2, f"v{i}", "video")

seq = []
for _ in range(6):
    t = tm2._pick_next()
    if t is None:
        break
    seq.append(t.queue)
    t.status = "done"
check("两队交替出场", seq == ["default", "video"] * 3, str(seq))

# ----------------------------------------------------------------------
# 4. 队列内部优先级
# ----------------------------------------------------------------------
print("== 队列内优先级 ==")
tm3 = make_tm({"queues": [{"id": "default", "name": "默认",
                           "max_concurrent": 5}], "max_concurrent": 0})
add(tm3, "low", "default", prio=0)
add(tm3, "high", "default", prio=9)
add(tm3, "mid", "default", prio=5)
order = []
for _ in range(3):
    t = tm3._pick_next()
    order.append(t.id)
    t.status = "done"
check("高优先级先跑", order == ["high", "mid", "low"], str(order))

# ----------------------------------------------------------------------
# 5. 任务指向已删队列
# ----------------------------------------------------------------------
print("== 已删队列兜底 ==")
tm4 = make_tm({"queues": [{"id": "default", "name": "默认",
                           "max_concurrent": 2}], "max_concurrent": 0})
add(tm4, "orphan", "ghost-queue")
t = tm4._pick_next()
check("孤儿任务归 default 并还能跑", t is not None and t.id == "orphan",
      getattr(t, "id", "None"))

# ----------------------------------------------------------------------
# 6. summary
# ----------------------------------------------------------------------
print("== 汇总 ==")
tasks = [
    Task(url="http://x/1", status="running", speed=1000, queue="default"),
    Task(url="http://x/2", status="queued", queue="default"),
    Task(url="http://x/3", status="running", speed=500, queue="video"),
    Task(url="http://x/4", status="done", queue="video"),
]
summ = queues.summary({"queues": []}, tasks)
by_id = {q["id"]: q for q in summ}
check("default 统计", by_id["default"]["running"] == 1
      and by_id["default"]["queued"] == 1, str(by_id["default"]))
check("video 统计", by_id["video"]["running"] == 1
      and by_id["video"]["done"] == 1, str(by_id["video"]))
check("速度累加", abs(by_id["video"]["speed"] - 500) < 1, str(by_id["video"]["speed"]))
check("total_slots = 5", queues.total_slots({"queues": []}) == 5)

# ----------------------------------------------------------------------
# 7. 增删改
# ----------------------------------------------------------------------
print("== 增删改 ==")
c = {"queues": []}
r = queues.upsert(c, {"id": "music", "name": "音乐", "max_concurrent": 2})
check("新增成功", r["ok"] and any(q["id"] == "music" for q in r["queues"]))
r = queues.upsert(c, {"id": "music", "name": "音乐2", "max_concurrent": 1})
check("修改成功", r["ok"] and queues.get(c, "music")["name"] == "音乐2")
r = queues.remove(c, "music")
check("删除成功", r["ok"] and "music" not in queues.ids(c))
r = queues.remove(c, "default")
check("default 不许删", r["ok"] is False and "不能删除" in r["error"])
r = queues.upsert(c, {"name": ""})
check("空 id 被拒", r["ok"] is False)

print("-" * 60)
print(f"通过 {ok} / 失败 {fail}")
sys.exit(0 if fail == 0 else 1)
