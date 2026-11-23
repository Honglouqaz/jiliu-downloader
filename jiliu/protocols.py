"""各种下载协议的识别与解码：磁力、BT 种子、迅雷、旋风、快车、ed2k。

为什么值得单独一层：
    用户从各处复制来的「链接」并不都是 http:// 开头的。
      - `thunder://` 迅雷链接，里面其实裹着一个普通 URL（base64 套壳）
      - `qqdl://` QQ 旋风，同理
      - `flashget://` 快车，base64 外面还包一层 [FLASHGET] 标记
      - `magnet:?xt=urn:btih:...` 磁力，是 BT 的入口
      - `ed2k://|file|...` eDonkey
      - `.torrent` 种子文件
    这些得分流给不同引擎：套壳的解出来交给多线程 HTTP 引擎，
    磁力/种子/ed2k 交给 aria2 的 BT 引擎（自研引擎不做 P2P）。

边界（写清楚，免得误期待）：
    本模块只做「格式解码」——把套壳链接还原成它本来的地址。
    加速下载网盘私有接口（百度网盘、夸克这类要靠逆向来提速的）不在
    范围内：那既违反服务条款，维护成本也高得离谱，接口一改就废。
    网盘能拿到**直链**的，汲流的普通多线程引擎本来就能下。
"""
from __future__ import annotations

import base64
import binascii
import re
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# 磁力链接里的 info hash
_BTIH_RE = re.compile(r"btih:([0-9a-fA-F]{40}|[2-7A-Za-z]{32})")
# 磁力的 tracker 参数
_TR_RE = re.compile(r"[?&]tr=([^&]+)")
# 磁力里带的显示名
_DN_RE = re.compile(r"[?&]dn=([^&]+)")

_BTIH_B32 = re.compile(r"^[2-7A-Za-z]{32}$")


@dataclass
class Protocol:
    kind: str = "http"          # http / thunder / qqdl / flashget / magnet / ed2k / torrent
    label: str = "普通链接"
    url: str = ""               # 解壳之后的真实地址
    original: str = ""
    engine: str = "native"      # native / aria2
    p2p: bool = False           # 是否需要 BT/P2P 引擎
    info_hash: str = ""         # 磁力 / BT
    name: str = ""              # 磁力里的 dn
    trackers: List[str] = field(default_factory=list)
    note: str = ""
    decoded: bool = False       # 是否发生过解壳
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _b64(s: str) -> bytes:
    """宽容一点的 base64 解码。

    这类链接是早年客户端生成的，padding 经常缺、偶尔混 urlsafe 字符，
    严格解码会直接抛异常。补齐到 4 的倍数再解。
    """
    t = (s or "").strip()
    t = t.replace("-", "+").replace("_", "/")
    pad = (-len(t)) % 4
    t += "=" * pad
    try:
        return base64.b64decode(t)
    except (binascii.Error, ValueError):
        return b""


def _strip_wrapper(text: str, head: str = "AA", tail: str = "ZZ") -> str:
    """去掉首尾的标记字节。"""
    if text.startswith(head) and text.endswith(tail) and len(text) > len(head) + len(tail):
        return text[len(head):-len(tail)]
    return text


def decode_thunder(raw: str) -> Optional[str]:
    """`thunder://` → 真实 URL。

    格式：thunder:// + base64("AA" + url + "ZZ")
    早年迅雷为了防止链接被直接识别，加了两字节标记和一层 base64。
    解开就是一个普通 http/ftp/ed2k 地址。
    """
    s = (raw or "").strip()
    i = s.lower().find("thunder://")
    if i < 0:
        return None
    body = s[i + len("thunder://"):].strip().strip("/")
    if not body:
        return None
    decoded = _b64(body).decode("utf-8", "replace")
    if not decoded:
        return None
    decoded = _strip_wrapper(decoded)
    return decoded.strip() or None


def decode_qqdl(raw: str) -> Optional[str]:
    """`qqdl://` → 真实 URL。旋风的套壳只有一层 base64（有的版本也带标记）。"""
    s = (raw or "").strip()
    i = s.lower().find("qqdl://")
    if i < 0:
        return None
    body = s[i + len("qqdl://"):].strip().strip("/")
    decoded = _b64(body).decode("utf-8", "replace")
    if not decoded:
        return None
    return _strip_wrapper(decoded).strip() or None


def decode_flashget(raw: str) -> Optional[str]:
    """`flashget://` → 真实 URL。外层是 base64，里面用 [FLASHGET] 包着。"""
    s = (raw or "").strip()
    i = s.lower().find("flashget://")
    if i < 0:
        return None
    body = s[i + len("flashget://"):].strip().strip("/")
    decoded = _b64(body).decode("utf-8", "replace")
    if not decoded:
        return None
    # 去掉 [FLASHGET]...[FLASHGET] 包裹
    m = re.search(r"\[FLASHGET\](.+?)\[FLASHGET\]", decoded, re.I | re.S)
    if m:
        return m.group(1).strip()
    return _strip_wrapper(decoded).strip() or None


def _decode_dn(raw: str) -> str:
    """磁力里 dn（显示名）的解码。

    麻烦在空格：各家站点的编码不统一 —— 有的写 %20，有的写 +。
    而 `+` 本身又是个合法文件名字符（C++ 之类不能变成 "C  "）。
    折中规则：先解 %XX，再把**剩下的裸 +** 当空格；
    但如果原始串里出现过 %2B，说明作者明确表达了「这里是加号」，那就不动它。
    """
    s = urllib.parse.unquote(raw or "")
    if "%2b" not in (raw or "").lower():
        s = s.replace("+", " ")
    return s.strip()


def decode_magnet(raw: str) -> Dict[str, Any]:
    """解析磁力链接，抽出 info hash 与 tracker。"""
    s = (raw or "").strip()
    out: Dict[str, Any] = {"info_hash": "", "name": "", "trackers": []}
    m = _BTIH_RE.search(s)
    if m:
        out["info_hash"] = m.group(1)
    for tr in _TR_RE.findall(s):
        t = urllib.parse.unquote(tr).strip()
        if t and t not in out["trackers"]:
            out["trackers"].append(t)
    m = _DN_RE.search(s)
    if m:
        out["name"] = _decode_dn(m.group(1))
    return out


def is_torrent_file(url: str) -> bool:
    """看着像种子文件吗（.torrent 后缀或 torrent MIME 前置判断）。"""
    u = (url or "").strip().lower()
    if not u:
        return False
    path = urllib.parse.urlparse(u).path if "://" in u else u
    return path.endswith(".torrent")


def detect(raw: str) -> Protocol:
    """认这个链接是什么协议，并尽量解壳。"""
    s = (raw or "").strip()
    p = Protocol(original=s, url=s)

    if not s:
        p.kind, p.error = "unknown", "空链接"
        return p

    low = s.lower()

    # ---- 磁力 ----
    if low.startswith("magnet:") or "magnet:?xt=" in low:
        info = decode_magnet(s)
        p.kind = "magnet"
        p.label = "磁力链接 (BT)"
        p.engine = "aria2"
        p.p2p = True
        p.url = s
        p.info_hash = info["info_hash"]
        p.name = info["name"]
        p.trackers = info["trackers"]
        if not info["info_hash"]:
            p.error = "磁力链接里没找到 info hash，可能是不完整的链接"
        p.note = ("磁力要 P2P 引擎（aria2）。info hash %s"
                  % (info["info_hash"][:16] + "…" if info["info_hash"] else "缺失"))
        return p

    # ---- ed2k ----
    if low.startswith("ed2k://"):
        p.kind = "ed2k"
        p.label = "eDonkey 链接"
        p.engine = "aria2"
        p.p2p = True
        p.url = s
        p.note = "eDonkey 由 aria2 处理。这类资源较老，源可能已经很少了。"
        return p

    # ---- 迅雷 / 旋风 / 快车 ----
    for prefix, dec, label in (
        ("thunder://", decode_thunder, "迅雷链接"),
        ("qqdl://", decode_qqdl, "QQ 旋风链接"),
        ("flashget://", decode_flashget, "快车链接"),
    ):
        if low.startswith(prefix) or prefix in low[:200]:
            real = dec(s)
            p.kind = prefix.rstrip("/:").replace("://", "")
            p.label = label
            p.decoded = True
            if real:
                p.url = real
                p.note = f"已解壳：{real[:90]}"
                # 解出来可能还是磁力/ed2k，继续认一次
                inner = detect(real)
                if inner.kind in ("magnet", "ed2k") or inner.p2p:
                    p.engine = "aria2"
                    p.p2p = True
                    p.info_hash = inner.info_hash
                    # 让界面一眼看出「迅雷壳里装的是磁力」，
                    # 否则分流到 BT 引擎会显得莫名其妙
                    p.label = f"{label}（内含{inner.label}）"
                else:
                    p.engine = "native"
            else:
                p.error = f"{label}解不开，内容可能被截断了"
                p.note = p.error
            return p

    # ---- 种子文件 ----
    if is_torrent_file(s):
        p.kind = "torrent"
        p.label = "BT 种子文件"
        p.engine = "aria2"
        p.p2p = True
        p.note = "种子文件要 P2P 引擎（aria2），自研引擎不做 BT。"
        return p

    # ---- 网盘（只识别，不逆向）----
    netdisk = detect_netdisk(s)
    if netdisk:
        return netdisk

    # ---- 普通 ----
    p.kind = "http"
    p.label = "普通链接"
    return p


# 常见网盘域名。只用来**识别并给出提示**，不碰它们的私有接口。
NETDISK = {
    "pan.baidu.com": ("百度网盘", "百度网盘需要在客户端里登录并生成直链，"
                                "汲流无法直接下分享页。"
                                "如果你已经有直链（http 直链），汲流能多线程下。"),
    "yun.baidu.com": ("百度网盘", "同上：需要先拿到直链。"),
    "aliyundrive.com": ("阿里云盘", "阿里云盘的分享页需要先转存到自己的空间，"
                                 "再取直链。拿到直链后汲流可以直接下。"),
    "alipan.com": ("阿里云盘", "同上：先取直链。"),
    "pan.quark.cn": ("夸克网盘", "夸克网盘的分享需要登录。汲流不逆向它的私有接口"
                               "（既违反服务条款，接口一改就失效）。"),
    "cloud.189.cn": ("天翼云盘", "分享页需要登录。若能拿到直链，汲流可以直接下。"),
    "115.com": ("115 网盘", "115 的分享需要登录，汲流不逆向私有接口。"),
    "123pan.com": ("123 云盘", "分享页需要登录后转存，汲流不逆向它的私有接口。"
                               "拿到直链后汲流可以多线程下载。"),
    "lanzou": ("蓝奏云", "蓝奏云公开分享页汲流可以直接解析（免登录，"
                          "自动过反爬换直链），支持提取码和文件夹分享。"
                          "输入分享链接即可。"),
    "drive.google.com": ("Google Drive", "需要直链或用 gdown 类工具取直链。"),
    "mega.nz": ("MEGA", "MEGA 有端到端加密，需要通过它自己的客户端解密。"),
}


def detect_netdisk(url: str) -> Optional[Protocol]:
    """识别网盘链接，给出**诚实的**说明，而不是假装能下。"""
    host = urllib.parse.urlparse(
        url if "://" in (url or "") else "http://" + (url or "")).hostname or ""
    host = host.lower()
    for key, (name, tip) in NETDISK.items():
        # 不能用 `key in host`：那样 "115.com" 会命中
        # "evil-115.com.attacker.io"，把钓鱼链接认成网盘。
        if key == "lanzou":
            hit = host.startswith("lanzou") or ".lanzou" in host
        else:
            hit = host == key or host.endswith("." + key)
        if hit:
            return Protocol(
                kind="netdisk", label=f"{name}链接", url=url,
                engine="native", p2p=False,
                note=tip,
                error="" if "直链" in tip else "分享页不能直接下载",
            )
    return None


def summarize(p: Protocol) -> str:
    """一句话描述，给界面用。"""
    bits = [p.label]
    if p.decoded:
        bits.append("已解壳")
    if p.p2p:
        bits.append("需要 BT 引擎")
    if p.info_hash:
        bits.append(f"hash {p.info_hash[:12]}…")
    if p.error:
        bits.append(f"⚠ {p.error}")
    return " | ".join(bits)
