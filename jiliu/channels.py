"""通知渠道：Bark / Telegram / 企业微信 / 钉钉 / Gotify / ntfy / 自定义 POST。

为什么要有
    只有 Windows Toast 的话，人不在电脑前就完全不知道下完了 ——
    而「挂一晚下完」恰恰是下载器最典型的使用场景。

两条硬约束
  1. **绝不能阻塞下载线程**。所有发送都在独立守护线程里跑，
     超时 5 秒，失败重试 2 次，最后写日志。
  2. **密钥不能在界面上明文回显**。describe() 一律打码，
     只有写的时候才用完整值。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

CHANNEL_TYPES = ("bark", "telegram", "wecom", "dingtalk", "gotify",
                 "ntfy", "webhook")

TYPE_TEXT = {
    "bark": "Bark（iOS）",
    "telegram": "Telegram Bot",
    "wecom": "企业微信群机器人",
    "dingtalk": "钉钉群机器人",
    "gotify": "Gotify",
    "ntfy": "ntfy",
    "webhook": "自定义 POST",
}

# 哪些事件值得推送。默认只推「完成」和「失败」——
# 「开始下载」这种低频价值信息推多了会被用户关掉整个通知
EVENTS = ("done", "error", "playlist")
EVENT_TEXT = {"done": "单个任务完成", "error": "任务失败",
              "playlist": "合集全部完成"}

TIMEOUT = 5.0
RETRIES = 2


def _mask(s: str, keep: int = 4) -> str:
    s = str(s or "")
    if len(s) <= keep * 2:
        return "*" * len(s) if s else ""
    return s[:keep] + "*" * min(12, len(s) - keep * 2) + s[-keep:]


def _mask_url(url: str) -> str:
    """把 URL 里的 token/key 打码，路径本身保留（用户要能认出是哪个渠道）。"""
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return _mask(url, 6)
    q = urllib.parse.parse_qsl(p.query)
    q2 = [(k, _mask(v) if k.lower() in ("token", "key", "access_token",
                                        "secret", "apikey") else v)
          for k, v in q]
    path = p.path
    segs = [x for x in path.split("/") if x]
    if segs and len(segs[-1]) >= 12:
        segs[-1] = _mask(segs[-1], 3)
        path = "/" + "/".join(segs)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path,
                                    urllib.parse.urlencode(q2), ""))


def normalize(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """收敛渠道列表。"""
    raw = cfg.get("notify_channels")
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for i, ch in enumerate(raw):
        if not isinstance(ch, dict):
            continue
        ctype = str(ch.get("type") or "").strip().lower()
        if ctype not in CHANNEL_TYPES:
            continue
        item = {
            "id": str(ch.get("id") or f"ch{i + 1}")[:24],
            "type": ctype,
            "name": str(ch.get("name") or TYPE_TEXT.get(ctype, ctype))[:32],
            "url": str(ch.get("url") or "").strip(),
            "token": str(ch.get("token") or "").strip(),
            "chat_id": str(ch.get("chat_id") or "").strip(),
            "topic": str(ch.get("topic") or "").strip(),
            "enabled": bool(ch.get("enabled", True)),
        }
        ev = ch.get("events")
        item["events"] = [e for e in (ev if isinstance(ev, list) else ["done", "error"])
                          if e in EVENTS]
        out.append(item)
    return out


# ----------------------------------------------------------------------
# 各渠道的请求
# ----------------------------------------------------------------------

def _post_json(url: str, payload: Dict[str, Any], timeout: float = TIMEOUT) -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return 200 <= getattr(r, "status", 200) < 300


def _post_text(url: str, text: str, timeout: float = TIMEOUT) -> bool:
    body = text.encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "text/plain; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return 200 <= getattr(r, "status", 200) < 300


def build_request(ch: Dict[str, Any], title: str, body: str,
                  event: str = "done") -> Dict[str, Any]:
    """把一条通知翻译成具体请求。返回 {url, payload, mode}。

    单独抽出来是为了能不联网就测「URL 拼对没有」。
    """
    ctype = ch["type"]
    url = ch["url"]
    text = f"{title}\n{body}" if body else title

    if ctype == "bark":
        base = url.rstrip("/")
        # Bark 支持 /KEY/title/body，直接拼在路径上最省事
        u = f"{base}/{urllib.parse.quote(title[:64])}"
        if body:
            u += f"/{urllib.parse.quote(body[:300])}"
        return {"url": u, "payload": None, "mode": "get"}

    if ctype == "telegram":
        token = ch["token"]
        api = url or "https://api.telegram.org"
        u = f"{api.rstrip('/')}/bot{token}/sendMessage"
        return {"url": u, "mode": "json",
                "payload": {"chat_id": ch["chat_id"], "text": text,
                            "disable_web_page_preview": True}}

    if ctype == "wecom":
        u = url or "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
        if ch["token"] and "key=" not in u:
            u += ("&" if "?" in u else "?") + "key=" + urllib.parse.quote(ch["token"])
        return {"url": u, "mode": "json",
                "payload": {"msgtype": "text", "text": {"content": text}}}

    if ctype == "dingtalk":
        u = url or "https://oapi.dingtalk.com/robot/send"
        if ch["token"] and "access_token=" not in u:
            u += ("&" if "?" in u else "?") + "access_token=" + urllib.parse.quote(ch["token"])
        return {"url": u, "mode": "json",
                "payload": {"msgtype": "text", "text": {"content": text}}}

    if ctype == "gotify":
        u = (url or "").rstrip("/") + "/message"
        if ch["token"]:
            u += "?token=" + urllib.parse.quote(ch["token"])
        return {"url": u, "mode": "json",
                "payload": {"title": title, "message": body or title,
                            "priority": 4}}

    if ctype == "ntfy":
        base = (url or "https://ntfy.sh").rstrip("/")
        topic = ch["topic"] or ch["token"] or "jiliu"
        u = f"{base}/{urllib.parse.quote(topic)}"
        return {"url": u, "mode": "text", "payload": text}

    # webhook：自带格式，字段固定
    return {"url": url, "mode": "json",
            "payload": {"app": "jiliu", "event": event,
                        "title": title, "body": body,
                        "time": int(time.time())}}


def _scheme_ok(url: str) -> bool:
    """Webhook 地址只允许 https；本机调试才放行 http。

    token 常常直接拼在 URL 里（bark/gotify/ntfy），http 等于明文出网。
    """
    try:
        u = urllib.parse.urlparse(str(url or ""))
    except Exception:  # noqa: BLE001
        return False
    if u.scheme == "https":
        return True
    if u.scheme == "http" and (u.hostname or "") in ("127.0.0.1", "localhost", "::1"):
        return True
    return False


def send_one(ch: Dict[str, Any], title: str, body: str,
             event: str = "done") -> Dict[str, Any]:
    """发一条，带重试。返回 {ok, channel, error, tries}。"""
    name = ch.get("name") or TYPE_TEXT.get(ch["type"], ch["type"])
    try:
        req = build_request(ch, title, body, event)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "channel": name, "error": f"构造请求失败：{exc}",
                "tries": 0}
    if not req.get("url"):
        return {"ok": False, "channel": name, "error": "没填地址", "tries": 0}
    if not _scheme_ok(str(req["url"])):
        return {"ok": False, "channel": name,
                "error": "通知地址必须是 https（本机调试可用 http://127.0.0.1）",
                "tries": 0}

    last = ""
    for i in range(RETRIES + 1):
        try:
            if req["mode"] == "get":
                with urllib.request.urlopen(req["url"], timeout=TIMEOUT) as r:
                    ok = 200 <= getattr(r, "status", 200) < 300
            elif req["mode"] == "text":
                ok = _post_text(req["url"], str(req["payload"]))
            else:
                ok = _post_json(req["url"], req["payload"] or {})
            if ok:
                return {"ok": True, "channel": name, "error": "", "tries": i + 1}
            last = "返回非 2xx"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        if i < RETRIES:
            time.sleep(0.8 * (i + 1))
    return {"ok": False, "channel": name, "error": last, "tries": RETRIES + 1}


def targets_for(cfg: Dict[str, Any], event: str) -> List[Dict[str, Any]]:
    return [ch for ch in normalize(cfg)
            if ch["enabled"] and (not ch["events"] or event in ch["events"])]


def notify(cfg: Dict[str, Any], title: str, body: str = "",
           event: str = "done") -> Dict[str, Any]:
    """异步推给所有匹配的渠道。立即返回，不等结果。"""
    chans = targets_for(cfg, event)
    if not chans:
        return {"ok": True, "sent": 0, "channel_results": []}
    results: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def one(ch: Dict[str, Any]) -> None:
        r = send_one(ch, title, body, event)
        with lock:
            results.append(r)

    for ch in chans:
        threading.Thread(target=one, args=(ch,), daemon=True,
                         name=f"jiliu-notify-{ch['id']}").start()
    return {"ok": True, "sent": 0, "queued": len(chans),
            "channel_results": results}


def notify_sync(cfg: Dict[str, Any], title: str, body: str = "",
                event: str = "done") -> List[Dict[str, Any]]:
    """同步版（设置页「测试」按钮用，要立刻看到结果）。"""
    return [send_one(ch, title, body, event) for ch in targets_for(cfg, event)]


def describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    rows = []
    for ch in normalize(cfg):
        safe = dict(ch)
        if safe["token"]:
            safe["token"] = _mask(safe["token"])
        if safe["url"]:
            safe["url"] = _mask_url(safe["url"])
        safe["type_text"] = TYPE_TEXT.get(ch["type"], ch["type"])
        safe["events_text"] = "、".join(EVENT_TEXT.get(e, e) for e in ch["events"])
        rows.append(safe)
    return {
        "channels": rows,
        "types": [{"id": t, "text": TYPE_TEXT[t]} for t in CHANNEL_TYPES],
        "events": [{"id": e, "text": EVENT_TEXT[e]} for e in EVENTS],
        "count": len(rows),
    }


def add(cfg: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    ctype = str(data.get("type") or "").strip().lower()
    if ctype not in CHANNEL_TYPES:
        return {"ok": False, "error": f"不支持的渠道：{ctype}"}
    rows = normalize(cfg)
    cid = str(data.get("id") or "").strip() or f"{ctype}{int(time.time()) % 100000}"
    item = {
        "id": cid,
        "type": ctype,
        "name": str(data.get("name") or TYPE_TEXT[ctype])[:32],
        "url": str(data.get("url") or "").strip(),
        "token": str(data.get("token") or "").strip(),
        "chat_id": str(data.get("chat_id") or "").strip(),
        "topic": str(data.get("topic") or "").strip(),
        "enabled": bool(data.get("enabled", True)),
        "events": [e for e in (data.get("events") or ["done", "error"])
                   if e in EVENTS],
    }
    if ctype in ("telegram",) and not (item["token"] and item["chat_id"]):
        return {"ok": False, "error": "Telegram 需要 token 与 chat_id"}
    if ctype in ("bark", "gotify", "webhook", "ntfy") and not (item["url"] or item["topic"]):
        return {"ok": False, "error": f"{TYPE_TEXT[ctype]} 需要地址"}
    for i, r in enumerate(rows):
        if r["id"] == cid:
            rows[i] = item
            break
    else:
        rows.append(item)
    cfg["notify_channels"] = rows
    return {"ok": True, "channel": item, "channels": rows}


def remove(cfg: Dict[str, Any], cid: str) -> Dict[str, Any]:
    rows = normalize(cfg)
    left = [r for r in rows if r["id"] != cid]
    if len(left) == len(rows):
        return {"ok": False, "error": f"没有这个渠道：{cid}"}
    cfg["notify_channels"] = left
    return {"ok": True, "channels": left}
