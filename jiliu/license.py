"""离线许可证：Ed25519 签名 + 设备绑定 + 30 天试用。

为什么是「离线验签」而不是「联网激活」：

  联网激活要养服务器、要有可用性承诺、断网就得挂。而这个软件的用户
  有相当一部分在内网机器或常年挂代理的环境里 —— 一旦激活服务连不上，
  用户手里那份是正版也开不了，这是最伤口碑的失败模式。

  Ed25519 的验签只要 64 字节签名和 32 字节公钥，纯 Python 实现不到
  200 行，不引第三方依赖。签发在我们手里，验证在用户机器上，
  全程不联网。代价是**没法远程吊销** —— 对单价几十块的工具来说，
  这个代价可以接受（真要管，靠版本升级换公钥）。

试用与降级的边界（这条比签名算法重要）：

  试用到期**不锁下载**。核心的「粘链接、点下载」永远免费可用，
  到期只是把 Pro 的调度类能力收回去。理由不是仁慈，是生意：
  锁死核心功能会把人逼去找破解版，而一个还能用的免费版才是
  持续曝光和转化的入口。收回去的东西必须**提前说清是哪几项**，
  不能到期那天突然弹一句「请购买」。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

from . import config as _config

__all__ = [
    "status", "activate", "deactivate", "guard", "requires",
    "device_id", "PRO_FEATURES", "TRIAL_DAYS",
]

TRIAL_DAYS = 30
LICENSE_PATH = _config.HOME / "license.json"

# 签发公钥（ed25519，32 字节）。私钥不在仓库里，见 scripts/license_tool.py。
# 换公钥 = 让所有旧许可证失效，只在大版本升级时做。
PUBLIC_KEY_HEX = "3848cfa97ce7ee91d3c33fa224496fcc7e5a71d306dc2d45e7f49a963c17858b"

# ---------------------------------------------------------------------------
# Pro 功能清单
# ---------------------------------------------------------------------------
# key 会被后端 guard() 和前端标记共用。描述写成「用户得到什么」，
# 不是「我们做了什么」—— 付费页上「多队列并发调度」比「queue scheduler」有用。
PRO_FEATURES: Dict[str, str] = {
    "queues":   "多队列并发调度（不同任务各自排队、限速、优先级）",
    "schedule": "定时下载窗口（只在你指定的时段跑）",
    "rss":      "RSS / Atom 订阅自动入队",
    "live":     "直播流录制",
    "batch":    "批量导入（单次超过 5 条）",
    "channels": "通知渠道与 Webhook（下完自动通知 / 交给别的程序）",
    "sniff":    "站点嗅探代理（接管没装扩展的客户端）",
}

# 免费版也给的额度：批量一次最多几条
FREE_BATCH_MAX = 5


# ---------------------------------------------------------------------------
# Ed25519（RFC 8032）—— 只做验签，够用就行
# ---------------------------------------------------------------------------
_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = 4 * _inv(5) % _P
_BX = _xrecover(_BY)
_B = (_BX % _P, _BY % _P, 1, (_BX * _BY) % _P)


def _add(P, Q):
    a = (P[1] - P[0]) * (Q[1] - Q[0]) % _P
    b = (P[1] + P[0]) * (Q[1] + Q[0]) % _P
    c = 2 * P[3] * Q[3] * _D % _P
    d = 2 * P[2] * Q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(P, e: int):
    if e == 0:
        return (0, 1, 1, 0)
    Q = _mul(P, e // 2)
    Q = _add(Q, Q)
    if e & 1:
        Q = _add(Q, P)
    return Q


def _encode_point(P) -> bytes:
    zi = _inv(P[2])
    x = P[0] * zi % _P
    y = P[1] * zi % _P
    bits = [(y >> i) & 1 for i in range(255)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(32))


def _decode_point(s: bytes):
    if len(s) != 32:
        raise ValueError("点长度不对")
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    sign = s[31] >> 7
    x = _xrecover(y)
    if x & 1 != sign:
        x = _P - x
    P = (x, y, 1, x * y % _P)
    if not _on_curve(P):
        raise ValueError("点不在曲线上")
    return P


def _on_curve(P) -> bool:
    x, y, z, t = P
    if (y * y - x * x - z * z - _D * t * t) % _P != 0:
        return False
    if (x * y - z * t) % _P != 0:
        return False
    return True


def verify(public: bytes, signature: bytes, msg: bytes) -> bool:
    """验签。任何畸形输入都返回 False，不抛异常 —— 许可证来自外部文件，
    不能因为里面写了垃圾字节就让整个启动流程崩掉。"""
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        A = _decode_point(public)
        R = _decode_point(signature[:32])
        S = int.from_bytes(signature[32:], "little")
        if S >= _Q:
            return False
        h = int.from_bytes(
            hashlib.sha512(signature[:32] + public + msg).digest(), "little") % _Q
        # 必须比较**编码后**的点，不能直接比坐标元组：
        # 群元素有无穷多种射影表示（同一个点、不同的 Z），
        # 直接比元组会把合法的签名判成假的。
        return _encode_point(_mul(_B, S)) == _encode_point(_add(R, _mul(A, h)))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 设备指纹
# ---------------------------------------------------------------------------
def _machine_seed() -> str:
    """尽量稳定、尽量不敏感的本机标识。

    用 Windows 的 MachineGuid：装机时生成、不随用户名/网络变化，
    重装系统才会变（那时候本来也该重新激活）。
    取不到就退回「主机名 + 网卡」，再取不到用随机值（只影响绑定精度）。
    """
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
            ) as k:
                v = str(winreg.QueryValueEx(k, "MachineGuid")[0]).strip()
                if v:
                    return "mg:" + v
        except Exception:
            pass
    try:
        node = platform.node() or ""
        mac = uuid.getnode()
        if node or mac:
            return f"nm:{node}|{mac:x}"
    except Exception:
        pass
    return "rnd:" + uuid.uuid4().hex


def device_id() -> str:
    """给人看的设备标识：4-4-4-4 十六进制。

    加盐哈希过，**不含**原始 MachineGuid —— 这个字符串会出现在
    许可证文件、客服对话和截图里，不该把机器指纹原样散出去。
    """
    seed = "jiliu-1|" + _machine_seed()
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16].upper()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


# ---------------------------------------------------------------------------
# 许可证文件
# ---------------------------------------------------------------------------
def _b64u_dec(s: str) -> bytes:
    s = s.strip().replace("\n", "").replace("\r", "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _read() -> Dict[str, Any]:
    try:
        raw = LICENSE_PATH.read_text(encoding="utf-8")
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(d: Dict[str, Any]) -> None:
    try:
        LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = LICENSE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, LICENSE_PATH)
    except Exception:
        pass


def _trial_anchor(store: Dict[str, Any]) -> float:
    """试用起算时间：文件里记的、注册表镜像里记的，取**更早**的那个。

    只存文件的话，删掉文件就重置试用；只存注册表的话，换个用户又重来。
    两处取最早是成本最低的折中：不装驱动、不驻留、卸载能清干净，
    也拦得住「删了重装」这种最常见的顺手操作。
    """
    cands: List[float] = []
    v = store.get("first_run")
    if isinstance(v, (int, float)) and v > 0:
        cands.append(float(v))
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Jiliu") as k:
                v2 = winreg.QueryValueEx(k, "first_run")[0]
                if isinstance(v2, (int, float)) and v2 > 0:
                    cands.append(float(v2))
        except Exception:
            pass
    if not cands:
        return 0.0
    return min(cands)


def _stamp_first_run(ts: float, store: Dict[str, Any]) -> None:
    if store.get("first_run"):
        return
    store["first_run"] = ts
    _write(store)
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Jiliu") as k:
                winreg.SetValueEx(k, "first_run", 0, winreg.REG_QWORD, int(ts))
        except Exception:
            pass


def parse_license(text: str) -> Dict[str, Any]:
    """解析并验签。返回 {ok, payload, error}。

    格式：JILIU1.<base64url(payload json)>.<base64url(sig)>
    用点号分段而不是纯 base64，是为了让人肉眼能看出「这是一张证」，
    复制时少一位也能立刻发现是哪一段坏了。
    """
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "许可证是空的"}
    # 允许用户把整段带换行的内容粘进来
    t = "".join(t.split())
    parts = t.split(".")
    if len(parts) != 3 or parts[0] != "JILIU1":
        return {"ok": False, "error": "格式不对：应当以 JILIU1. 开头，共三段"}
    try:
        payload_bytes = _b64u_dec(parts[1])
        sig = _b64u_dec(parts[2])
    except Exception:
        return {"ok": False, "error": "内容损坏：复制时丢字符了，请重新完整复制"}
    pub = bytes.fromhex(PUBLIC_KEY_HEX)
    if not verify(pub, sig, payload_bytes):
        return {"ok": False, "error": "签名校验失败：这张许可证不是本软件签发的，或已被改动"}
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return {"ok": False, "error": "许可证内容无法解析"}
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return {"ok": False, "error": "许可证版本不认识，请升级软件"}
    return {"ok": True, "payload": payload}


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _days_between(a: str, b: str) -> int:
    """b - a，按日期算（不做时区换算，许可证精确到天就够了）。"""
    try:
        ta = time.mktime(time.strptime(a, "%Y-%m-%d"))
        tb = time.mktime(time.strptime(b, "%Y-%m-%d"))
        return int(round((tb - ta) / 86400))
    except Exception:
        return 9999


def _now() -> float:
    """当前时间，带一点防回拨：比上次运行时间还早就按上次算。

    不追求绝对可靠（改系统时间总能骗过离线校验），只要让「把表拨回去
    续试用」这个操作变得麻烦、且不会越拨越多就行。
    """
    now = time.time()
    store = _read()
    last = store.get("last_seen")
    if isinstance(last, (int, float)) and last - now > 86400:
        return float(last)
    return now


def activate(key: str) -> Dict[str, Any]:
    r = parse_license(key)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "许可证无效"}
    p = r["payload"]
    dev = str(p.get("dev") or "*").strip().upper()
    mine = device_id()
    if dev not in ("*", "") and dev != mine:
        return {"ok": False,
                "error": f"这张许可证绑的是另一台机器（{dev}），本机是 {mine}。"
                         f"换机请用本机标识重新签发。"}
    exp = str(p.get("exp") or "").strip()
    if exp and _days_between(_today(), exp) < 0:
        return {"ok": False, "error": f"这张许可证已于 {exp} 到期"}
    store = _read()
    _stamp_first_run(_trial_anchor(store) or time.time(), store)
    store["key"] = "".join((key or "").split())
    store["activated_at"] = int(time.time())
    store["last_seen"] = int(time.time())
    _write(store)
    return {"ok": True, "payload": p}


def deactivate() -> Dict[str, Any]:
    """移除本机许可证，回到试用状态（试用期不重置）。"""
    store = _read()
    store.pop("key", None)
    store.pop("activated_at", None)
    _write(store)
    return {"ok": True}


def _current() -> Dict[str, Any]:
    """算出当前授权状态。所有对外函数都走这里，保证口径一致。"""
    store = _read()
    now = _now()
    _stamp_first_run(now, store)
    store["last_seen"] = int(now)
    _write(store)

    first = _trial_anchor(store) or now
    trial_end = first + TRIAL_DAYS * 86400
    days_left = max(0, int((trial_end - now + 86399) // 86400))

    dev = device_id()
    key = str(store.get("key") or "").strip()
    if key:
        r = parse_license(key)
        if r.get("ok"):
            p = r["payload"]
            exp = str(p.get("exp") or "").strip()
            if not exp or _days_between(_today(), exp) >= 0:
                return {
                    "state": "pro",
                    "plan": "pro",
                    "label": "正式版",
                    "device": dev,
                    "expires_at": exp or "永久",
                    "days_left": _days_between(_today(), exp) if exp else None,
                    "license_id": p.get("id") or "",
                    "name": p.get("name") or "",
                    "features": dict(PRO_FEATURES),
                    "batch_max": None,
                }
            return {
                "state": "expired", "plan": "free", "label": "许可证已到期",
                "device": dev, "expires_at": exp, "days_left": 0,
                "license_id": p.get("id") or "", "name": p.get("name") or "",
                "features": {}, "batch_max": FREE_BATCH_MAX,
                "note": "许可证到期了，Pro 功能已收回，下载仍可用。",
            }
        # 有 key 但验不过：可能是换了机器或文件被改
        return {
            "state": "invalid", "plan": "free", "label": "许可证无效",
            "device": dev, "expires_at": "", "days_left": 0,
            "features": {}, "batch_max": FREE_BATCH_MAX,
            "note": r.get("error") or "许可证无法验证",
        }

    if now < trial_end:
        return {
            "state": "trial", "plan": "pro", "label": "试用中",
            "device": dev, "expires_at": time.strftime("%Y-%m-%d", time.localtime(trial_end)),
            "days_left": days_left, "license_id": "", "name": "",
            "features": dict(PRO_FEATURES), "batch_max": None,
            "note": f"试用期 {TRIAL_DAYS} 天，Pro 功能全部可用。",
        }
    return {
        "state": "expired", "plan": "free", "label": "试用已到期",
        "device": dev, "expires_at": time.strftime("%Y-%m-%d", time.localtime(trial_end)),
        "days_left": 0, "features": {}, "batch_max": FREE_BATCH_MAX,
        "note": "试用结束了，下载功能不受影响；调度类功能需要正式版。",
    }


def status() -> Dict[str, Any]:
    s = _current()
    s["trial_days"] = TRIAL_DAYS
    s["pro_features"] = dict(PRO_FEATURES)
    s["free_batch_max"] = FREE_BATCH_MAX
    return s


def requires(feature: str) -> bool:
    """当前授权下，这个功能开不开。未知 feature 一律放行（宁可少收钱，
    也不要因为写错一个 key 把正常功能锁掉）。"""
    if feature not in PRO_FEATURES:
        return True
    return _current().get("plan") == "pro"


def guard(feature: str) -> Dict[str, Any]:
    """给后端入口用：{ok, reason}。reason 直接展示给用户，
    所以要说清「哪一项」和「怎么解决」，不能只写「无权限」。"""
    if requires(feature):
        return {"ok": True, "reason": ""}
    s = _current()
    what = PRO_FEATURES.get(feature, feature)
    if s.get("state") == "expired":
        why = "试用已结束" if not s.get("license_id") else "许可证已到期"
    else:
        why = "当前是免费版"
    return {
        "ok": False,
        "feature": feature,
        "reason": f"「{what}」属于 Pro 功能，{why}。"
                  f"核心下载不受影响，随时可以继续用；需要这项能力可在"
                  f"「设置 › 版本与授权」里激活。",
    }
