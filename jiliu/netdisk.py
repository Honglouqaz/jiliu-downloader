"""网盘公开分享页解析 —— 目前支持蓝奏云。

为什么单独一层（而不塞进 protocols.py）：
    protocols 只做「链接格式解码」，是纯本地的、不联网的；
    网盘解析要真的去请求分享页、过反爬、换直链，是网络操作，
    失败模式完全不同（挑战页、风控、接口改版）。混在一起两边都难测。

合规边界（跟主人的基线一致，写清楚免得误期待）：
    只解析「免登录即可访问的公开分享页」——浏览器能直接打开就能解析，
    没有绕过任何登录态、会员体系或加密。需要登录/会员的私有接口
    （百度/夸克/阿里这类）不在范围内，见 protocols.NETDISK 的说明。

蓝奏云解析链（2026-09-29 实测有效，四层）：
    1) 分享页 /<id>  —— 有阿里云 WAF 挑战（acw_sc__v2），纯 Python 解算
    2) iframe /fn?<加密参数> —— 拿 wp_sign / ajaxdata / ajaxfile 地址
    3) POST ajaxfile.php (action=downprocess) —— 拿 {dom, url} 拼出直链
    4) GET 直链 —— CDN 域还有一层同款挑战 + 「验证并下载」页，
       POST 同目录 ajax.php (el=2) 换出干净的最终直链
    最终直链是无挑战的普通文件 URL（实测支持 Range 断点续传）。

为什么挑战求解不写死算法：WAF 的字符串表/置换表都下发在页面里，
本模块从页面**动态抠参数**再执行固定结构 —— 表换了照样活；
整个页面结构换了才挂，那时会返回明确的「接口可能改版」错误。
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import proxy as _proxy

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 蓝奏云域名族。历史上换过十几个域名，活着的随时在变；
# 识别从宽（带 lanzou 字样就算），解析失败时逐个试池子里的域名。
# search 配 $：词根+后缀必须是域名的最后两段，www. 不影响，
# lanzoui.evil.com 这类挂羊头的不中。
LANZOU_HOST_RE = re.compile(
    r"(?:^|\.)(?:[a-z0-9-]*lanzou[a-z0-9]*|lanzn|lanzv|lanpv|lanrar|lanosso|woozooo)"
    r"\.(?:com|net|org|cc|pw|me|top|xyz)$", re.I)
# 已验证可用的解析域名池（2026-09-29 实测 DNS 正常的）
LANZOU_POOL = ("lanzou.com", "lanzoui.com", "lanzouw.com")

_TIMEOUT = 15


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class NetdiskFile:
    """解析成功的产物：一个干净的直链 + 文件信息。"""
    name: str = ""
    size: int = 0                  # 字节；拿不到为 0
    size_text: str = ""
    url: str = ""                  # 最终直链（无挑战，可直接下）
    headers: Dict[str, str] = field(default_factory=dict)  # 下载时附带
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": True, "kind": "file", "name": self.name,
                "size": self.size, "size_text": self.size_text,
                "url": self.url, "headers": self.headers, "note": self.note}


@dataclass
class NetdiskError(Exception):
    """解析失败。error 是给人看的一句话；hint 是下一步建议。"""
    error: str = ""
    hint: str = ""
    dead: bool = False       # 分享已失效（链接不会再好）
    need_pwd: bool = False

    def __post_init__(self):
        super().__init__(self.error)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": False, "error": self.error, "hint": self.hint,
                "dead": self.dead, "need_pwd": self.need_pwd}


# ---------------------------------------------------------------------------
# acw_sc__v2 挑战求解（纯 Python 复现 WAF 的混淆 JS）
# ---------------------------------------------------------------------------

# WAF 自定义 base64 字母表：小写在前（与标准表相反）
_ACW_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/="
_STD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _acw_b64decode(s: str) -> str:
    """WAF 变体 base64 解码：自定义字母表 + UTF-8 还原。"""
    s2 = "".join(_STD_B64[_ACW_ALPHABET.index(c)] if c in _ACW_ALPHABET else "="
                 for c in s.rstrip("="))
    pad = (-len(s2)) % 4
    raw = base64.b64decode(s2 + "=" * pad)
    return raw.decode("utf-8", "replace")


def _js_int(s: str) -> float:
    """parseInt 语义：取前导整数，取不到是 NaN。"""
    m = re.match(r"\s*[+-]?\d+", s or "")
    return float(m.group()) if m else float("nan")


class _AcwSolver:
    """执行一页挑战脚本的等价 Python。每个页面实例化一次。"""

    def __init__(self, html: str):
        m = re.search(r"var arg1='([0-9A-Fa-f]+)'", html)
        n_arr = re.search(r"var N=(\[[^\]]*\])", html)
        m_arr = re.search(r"for\(var m=\[([^\]]+)\]", html)
        if not (m and n_arr and m_arr):
            raise ValueError("页面里找不到挑战参数（WAF 结构可能已改版）")
        self.arg1 = m.group(1)
        self.N = re.findall(r"'([^']*)'", n_arr.group(1))
        self.m = [int(x, 16) for x in m_arr.group(1).split(",")]
        # rotate 前先确认结构
        if len(self.m) < 8 or not self.N:
            raise ValueError("挑战参数结构异常")
        self._table = self._rotate_table()

    def _get(self, table: List[str], idx: int) -> str:
        d = idx - 0xFB
        if not (0 <= d < len(table)):
            raise ValueError(f"查表越界 0x{idx:x}")
        return _acw_b64decode(table[d])

    def _rotate_table(self) -> List[str]:
        """复现页面顶部 IIFE：左旋字符串表直到校验算式 === 0x760BF。

        算式里的操作数全部来自查表 —— 表转对了几位，算式才对得上。
        JS 的除法是浮点除，这里保持一致。
        """
        c = list(self.N)
        target = 0x760BF
        for _ in range(len(c) + 50):
            try:
                e = (-_js_int(self._get(c, 0x117)) * (_js_int(self._get(c, 0x111)) / 2)
                     + -_js_int(self._get(c, 0xFB)) / 3 * (_js_int(self._get(c, 0x10E)) / 4)
                     + -_js_int(self._get(c, 0x101)) / 5 * (-_js_int(self._get(c, 0xFD)) / 6)
                     + -_js_int(self._get(c, 0x102)) / 7 * (_js_int(self._get(c, 0x122)) / 8)
                     + _js_int(self._get(c, 0x112)) / 9
                     + _js_int(self._get(c, 0x11D)) / 10 * (_js_int(self._get(c, 0x11C)) / 11)
                     + _js_int(self._get(c, 0x114)) / 12)
            except (ValueError, UnicodeDecodeError):
                c.append(c.pop(0))
                continue
            if e == target:
                return c
            c.append(c.pop(0))
        raise ValueError("字符串表旋转校验失败（WAF 算式可能已改版）")

    def solve(self) -> str:
        """算出 acw_sc__v2 的值。"""
        p = self._get(self._table, 0x115)   # XOR key
        # 置换：q[m[z]-1] = arg1[x] (x+1 == m[z])
        q = [""] * len(self.m)
        for x, ch in enumerate(self.arg1):
            for z, mv in enumerate(self.m):
                if mv == x + 1:
                    q[z] = ch
        u = "".join(q)
        v = []
        for i in range(0, min(len(u), len(p)), 2):
            a = int(u[i:i + 2], 16)
            b = int(p[i:i + 2], 16)
            v.append(format(a ^ b, "02x"))
        return "".join(v)


def solve_acw(html: str) -> str:
    """挑战页 → acw_sc__v2 cookie 值。不是挑战页返回空串。"""
    if "arg1=" not in html:
        return ""
    return _AcwSolver(html).solve()


# ---------------------------------------------------------------------------
# HTTP 会话：挑战感知的 GET/POST
# ---------------------------------------------------------------------------

class _Session:
    """挑战感知的会话：遇到 acw 挑战页自动算 cookie 重放。

    cookie 按域名各存一份 —— 分享域和 CDN 域是两套挑战。
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.cookies: Dict[str, str] = {}   # host -> "k=v; k=v"
        self.challenges_solved = 0

    def _cookie_for(self, host: str) -> str:
        return self.cookies.get(host, "")

    def _merge_cookie(self, host: str, set_cookie_values: List[str]) -> None:
        # Set-Cookie 的 k=v 直接并进去（acw_tc 这类也要，服务端会查）
        cur = self.cookies.get(host, "")
        have = dict(kv.split("=", 1) for kv in cur.split("; ") if "=" in kv)
        for sc in set_cookie_values:
            kv = sc.split(";")[0]
            if "=" in kv:
                k, v = kv.split("=", 1)
                have[k.strip()] = v.strip()
        self.cookies[host] = "; ".join(f"{k}={v}" for k, v in have.items())

    def request(self, url: str, *, data=None, referer: str = "",
                xhr: bool = False, timeout: int = _TIMEOUT):
        """返回 (status, final_url, raw_bytes, headers)。挑战自动重放一次。

        注意挑战页的 HTTP 状态是 200 —— 所以检测必须看内容，不能看状态码。
        """
        op = _proxy.opener(self.cfg, url, timeout=timeout)
        host = urllib.parse.urlparse(url).hostname or ""
        err: Optional[Exception] = None
        result = None
        for _ in range(2):
            h = {"User-Agent": UA, "Accept-Encoding": "gzip"}
            ck = self._cookie_for(host)
            if ck:
                h["Cookie"] = ck
            if referer:
                h["Referer"] = referer
            if xhr:
                h["X-Requested-With"] = "XMLHttpRequest"
                h["Accept"] = "*/*"
            try:
                req = urllib.request.Request(url, data=data, headers=h)
                with op.open(req, timeout=timeout) as r:
                    raw = r.read()
                    if (r.headers.get("Content-Encoding") == "gzip"
                            or raw[:2] == b"\x1f\x8b"):
                        try:
                            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                        except OSError:
                            pass
                    sc = r.headers.get_all("Set-Cookie") or []
                    if sc:
                        self._merge_cookie(host, sc)
                    result = (r.status, r.geturl(), raw, dict(r.headers))
                    err = None
            except urllib.error.HTTPError as e:
                # 4xx/5xx 也读 body —— 挑战页有时裹在非 200 状态里
                err = e
                try:
                    raw = e.read()
                    if raw[:2] == b"\x1f\x8b":
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                except Exception:  # noqa: BLE001
                    raise
            # 挑战检测（看内容不看状态码）：算出 token 就种 cookie 重放
            body = result[2] if result else raw
            txt = body.decode("utf-8", "replace")
            try:
                token = solve_acw(txt)
            except ValueError as exc:
                raise NetdiskError(
                    f"反爬挑战解不开：{exc}",
                    hint="蓝奏云的反爬可能改版了，等上游适配") from exc
            if token:
                self.challenges_solved += 1
                self._merge_cookie(host, [f"acw_sc__v2={token}; path=/"])
                continue
            break
        if result is not None:
            return result
        if isinstance(err, urllib.error.HTTPError):
            # 底层统一包装：上层（resolve 的换域名重试、直链的重试）
            # 都只需要认 NetdiskError 一种异常
            raise NetdiskError(
                f"HTTP {err.code}（{host}）",
                hint="站点暂时拒绝访问，稍后重试") from err
        raise err or NetdiskError("请求失败")


# ---------------------------------------------------------------------------
# 蓝奏云解析
# ---------------------------------------------------------------------------

def is_lanzou(url: str) -> bool:
    host = urllib.parse.urlparse(
        url if "://" in (url or "") else "http://" + (url or "")).hostname or ""
    host = host.lower()
    # search + $：只认「lanzou 族词根 + 白名单后缀」作为域名的**最后两段**，
    # www. 前缀不影响；lanzoui.evil.com 这类挂羊头的不会中
    return bool(LANZOU_HOST_RE.search(host))


def is_netdisk_share(url: str) -> bool:
    return is_lanzou(url)


def _alternates(url: str) -> List[str]:
    """原域名失败时的备选域名（分享 id 在蓝奏各域等价）。"""
    p = urllib.parse.urlparse(url if "://" in url else "http://" + url)
    host = (p.hostname or "").lower()
    out = []
    for cand in LANZOU_POOL:
        if host != cand:
            out.append(f"{p.scheme or 'https'}://{cand}{p.path}"
                       + (f"?{p.query}" if p.query else ""))
    return out


def _looks_dead(page: str) -> str:
    """失效/受限页面的特征。返回人话原因，活着返回空串。"""
    if "文件取消分享了" in page or "来晚啦" in page:
        return "分享已失效（文件被取消分享或已删除）"
    if "文件已经被取消分享" in page or "文件不存在" in page:
        return "分享已失效（文件不存在）"
    return ""


def _looks_pwd(page: str) -> bool:
    # 带密码的分享页特征：密码输入框 + down_p 提交函数
    # （只认硬特征，避免正文里出现「提取码」三个字的普通页误判）
    return bool(re.search(r'id="pwd"|passwdinput|function down_p', page))


def _iframe_url(page: str, base: str) -> str:
    mi = re.search(r'src="(/[^"]*(?:fn|file)\?[^"]*)"', page)
    if not mi:
        return ""
    return base.rstrip("/") + mi.group(1)


def _parse_iframe(page: str) -> Dict[str, Any]:
    """从 iframe 页抠 wp_sign / ajaxdata / POST 地址 / kdns。"""
    sign = re.search(r"wp_sign\s*=\s*'([^']+)'", page)
    ajaxdata = re.search(r"ajaxdata\s*=\s*'([^']+)'", page)
    post_url = re.search(r"url\s*:\s*'([^']+)'", page)
    kdns = re.search(r"kdns\s*=\s*(\d+)", page)
    if not (sign and post_url):
        raise NetdiskError("iframe 页里找不到签名参数",
                           hint="蓝奏云接口可能改版了，等上游适配")
    return {
        "sign": sign.group(1),
        "ajaxdata": ajaxdata.group(1) if ajaxdata else "",
        "post_url": post_url.group(1),
        "kd": int(kdns.group(1)) if kdns else 1,
    }


def _post_ajaxfile(sess: _Session, info: Dict[str, Any], referer: str
                   ) -> Dict[str, Any]:
    form = urllib.parse.urlencode({
        "action": "downprocess",
        "websignkey": info["ajaxdata"], "signs": info["ajaxdata"],
        "sign": info["sign"], "websign": "",
        "kd": info["kd"], "ves": 1,
    }).encode()
    st, _, raw, _ = sess.request(info["post_url"], data=form, referer=referer,
                                 xhr=True)
    txt = raw.decode("utf-8", "replace")
    try:
        import json as _json
        return _json.loads(txt)
    except ValueError:
        raise NetdiskError("ajaxfile 响应不是 JSON",
                           hint="蓝奏云接口可能改版了") from None


def _is_bad_sign(msg: Dict[str, Any]) -> bool:
    """签名被拒的响应长这样：dom 空、url 是 '?xxx' 一小段。"""
    u = str(msg.get("url") or "")
    return bool(msg.get("dom")) is False and u.startswith("?") and "/" not in u


# ---------------------------------------------------------------------------
# 文件夹分享
# ---------------------------------------------------------------------------

def _looks_folder(page: str) -> bool:
    """文件夹分享页特征：有分页列表接口。"""
    return bool(re.search(r'filemoreajax|id="filemore"', page))


def _folder_ajax(page: str) -> "Tuple[str, Dict[str, Any], str]":
    """抠出文件夹页里 filemoreajax 的 url、表单参数、页码键名。

    页面自己的 JS 已经把 lx/fid/uid/puid/t/k 全部备好了（t/k 是动态
    token，从 var 定义里查），照抄表单最抗改版 —— 只把页码换成我们要翻的页。
    """
    murl = re.search(r"url\s*:\s*'([^']*filemoreajax\.php[^']*)'", page)
    if not murl:
        raise NetdiskError("文件夹页里找不到列表接口",
                           hint="蓝奏云页面结构可能改版了，等上游适配")
    mdata = re.search(r"data\s*:\s*\{([^}]+)\}", page[murl.end():])
    if not mdata:
        raise NetdiskError("文件夹页里找不到列表参数",
                           hint="蓝奏云页面结构可能改版了，等上游适配")
    form: Dict[str, Any] = {}
    pg_key = "pg"
    for k, v in re.findall(r"'([^']+)'\s*:\s*([^,\n}]+)", mdata.group(1)):
        v = v.strip().rstrip(",").strip()
        if v.startswith("'") and v.endswith("'"):
            form[k] = v[1:-1]
        elif re.fullmatch(r"\d+", v):
            form[k] = int(v)
        elif v == "pgs":
            pg_key = k              # 页码占位符 —— 翻页时改这个键
            form[k] = 1
        else:
            # 标识符：查页面里的 var 定义（t/k 这类动态 token）
            vm = (re.search(rf"var\s+{re.escape(v)}\s*=\s*'([^']*)'", page)
                  or re.search(rf"\b{re.escape(v)}\s*=\s*'([^']*)'", page))
            form[k] = vm.group(1) if vm else v
    return murl.group(1), form, pg_key


def _list_folder(sess: _Session, page: str, base: str
                 ) -> "Tuple[List[Dict[str, str]], int]":
    """翻页取文件清单，返回 (文件列表, 子文件夹数)。

    文件条目 url 指向**各自的单文件分享页**（base + '/' + id，实测长 id
    有效、短 duan 已死），添加时逐个走单文件解析 —— 不在这里一次解析
    全部直链（十几个文件 × CDN 挑战，慢且容易触发风控）。
    """
    ajax, form, pg_key = _folder_ajax(page)
    if "://" not in ajax:
        ajax = base + ajax
    items: List[Dict[str, str]] = []
    subfolders = 0
    for pg in range(1, 31):          # 单页约 50 条，30 页封顶足够
        f = dict(form)
        f[pg_key] = pg
        st, _, raw, _ = sess.request(ajax,
                                     data=urllib.parse.urlencode(f).encode(),
                                     referer=base + "/", xhr=True)
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise NetdiskError("文件夹列表响应不是 JSON",
                               hint="蓝奏云接口可能改版了") from None
        zt = str(msg.get("zt"))
        if zt == "2":                # 没有更多文件
            break
        if zt != "1":
            reason = str(msg.get("info") or msg.get("inf") or f"zt={zt}")
            if pg == 1:
                raise NetdiskError(f"取文件夹列表失败（{reason}）",
                                   hint="链接可能已失效或接口改版")
            break                    # 后续页失败：已拿到的照常给
        text = msg.get("text") or []
        for it in text:
            iid = str(it.get("id") or "").strip()
            name = str(it.get("name_all") or "").strip()
            if not iid or not name:
                continue
            if str(it.get("icon")) == "folder":
                subfolders += 1
                continue
            items.append({"name": name, "size_text": str(it.get("size") or ""),
                          "url": f"{base}/{iid}"})
        if not text:
            break
    return items, subfolders


def _post_pwd(sess: _Session, page: str, pwd: str, referer: str
              ) -> Dict[str, Any]:
    """带提取码的分享页：直接 POST ajaxfile（p=提取码），不走 iframe。

    页面里 down_p() 已经把要用的签名（isngis）、文件 id（ajaxfile URL 的
    file= 参数）都备好了，照抄它的表单即可 —— 和普通流程同一个接口，
    多一个 p 字段，响应同构 {zt, dom, url, inf}。
    """
    sign = re.search(r"isngis\s*=\s*'([^']+)'", page)
    post = re.search(r"url\s*:\s*'([^']*ajaxfile\.php[^']*)'", page)
    kdns = re.search(r"kdns\s*=\s*(\d+)", page)
    if not (sign and post):
        raise NetdiskError("密码页里找不到签名参数",
                           hint="蓝奏云接口可能改版了，等上游适配")
    form = urllib.parse.urlencode({
        "action": "downprocess", "sign": sign.group(1),
        "kd": int(kdns.group(1)) if kdns else 1, "p": pwd,
    }).encode()
    st, _, raw, _ = sess.request(post.group(1), data=form, referer=referer,
                                 xhr=True)
    txt = raw.decode("utf-8", "replace")
    try:
        import json as _json
        return _json.loads(txt)
    except ValueError:
        raise NetdiskError("密码接口响应不是 JSON",
                           hint="蓝奏云接口可能改版了") from None


def _finalize_direct(sess: _Session, direct: str, referer: str
                     ) -> "tuple[str, int, str, Dict[str, str]]":
    """直链的后处理：过 CDN 挑战/验证页，返回 (最终URL, 大小, 文件名, 头)。

    三种情况：
      a) 直接是文件        → 完事
      b) acw 挑战页        → 算 cookie 重放，回 a 或 c
      c) 「验证并下载」页   → POST 同目录 ajax.php (el=2) 换真链
    """
    headers: Dict[str, str] = {}
    url = direct
    _retry = 0
    for _ in range(5):     # 挑战→(400 重试)→验证→真链 最多五跳
        try:
            st, fin, raw, hh = sess.request(url, referer=referer)
        except urllib.error.HTTPError as e:
            code = e.code
        except NetdiskError as e:
            # _Session 底层把 HTTPError 包成了 NetdiskError，这里要一并接住
            m = re.match(r"HTTP (\d{3})（", e.error)
            if not m:
                raise
            code = int(m.group(1))
        else:
            code = 0
        if code:
            # CDN 偶发 400/403 —— 重试一两次常见就过（实测）
            if _retry < 2:
                _retry += 1
                time.sleep(0.8)
                continue
            raise NetdiskError(f"直链请求被拒（HTTP {code}）",
                               hint="稍后重试，或换网络环境") from None
        ct = (hh.get("Content-Type") or "").lower()
        if "text/html" not in ct:
            # 是文件了。fn= 参数里带原始文件名
            fn = ""
            q = urllib.parse.urlparse(url).query
            mfn = re.search(r"(?:^|&)fn=([^&]*)", q)
            if mfn:
                fn = urllib.parse.unquote(mfn.group(1))
            size = 0
            cl = hh.get("Content-Length")
            cr = hh.get("Content-Range") or ""
            if cr and "/" in cr:
                try:
                    size = int(cr.rsplit("/", 1)[1])
                except ValueError:
                    pass
            if not size and cl and cl.isdigit():
                size = int(cl)
            return url, size, fn, headers
        txt = raw.decode("utf-8", "replace")
        dead = _looks_dead(txt)
        if dead:
            raise NetdiskError(dead, dead=True)
        # c) 验证并下载页
        ftok = re.search(r"'file':'([^']+)'", txt)
        stok = re.search(r"'sign':'([^']+)'", txt)
        if ftok and stok:
            ajax = url.rsplit("/", 1)[0] + "/ajax.php"
            form = urllib.parse.urlencode(
                {"file": ftok.group(1), "el": 2, "sign": stok.group(1)}).encode()
            try:
                st2, _, raw2, _ = sess.request(ajax, data=form, referer=url,
                                               xhr=True)
            except NetdiskError:
                # 验证请求本身被 CDN 用 4xx/5xx 拒掉 —— IP 信誉差的典型表现
                raise NetdiskError(
                    "CDN 验证被拒绝（当前网络环境可能被风控）",
                    hint="换网络（如手机热点）再试") from None
            try:
                import json as _json
                msg = _json.loads(raw2.decode("utf-8", "replace"))
            except ValueError:
                # ajax.php 回了 HTML 而不是 JSON —— IP 信誉差时 CDN 会
                # 拒绝验证（回敬验证页本身）。真机家宽很少见到。
                raise NetdiskError(
                    "CDN 验证被拒绝（当前网络环境可能被风控）",
                    hint="换网络（如手机热点）再试") from None
            got = str(msg.get("url") or "")
            if str(msg.get("zt")) == "1" and got.startswith("http"):
                url = got
                continue
            # CDN 的 sign 校验偶发失败（'?SignError'）；直链会过期，
            # 这里重试没有意义 —— 从头再来一遍才有效，交给上层
            reason = got or str(msg.get("dom") or "")
            raise NetdiskError(f"CDN 验证未通过（{reason or 'zt != 1'}）",
                               hint="稍后重试，或换网络环境再试")
        # b) acw 挑战在 request() 里已经自动处理过一轮；
        #    到这里还是 HTML 且没有验证页特征 —— 认输
        raise NetdiskError("直链返回了无法识别的页面",
                           hint="蓝奏云 CDN 可能改版了，等上游适配")
    raise NetdiskError("直链跳转次数过多", hint="稍后重试")


def _name_from_page(page: str) -> str:
    t = re.search(r"<title>([^<]*)</title>", page)
    if t:
        name = t.group(1).strip()
        name = re.sub(r"\s*[-–—]\s*蓝奏云\s*$", "", name)
        if name:
            return name
    return ""


def resolve(url: str, cfg: Dict[str, Any], *, pwd: str = ""
            ) -> Dict[str, Any]:
    """解析蓝奏云公开分享页。成功返回 NetdiskFile.to_dict()，失败抛 NetdiskError。"""
    url = (url or "").strip()
    if not is_lanzou(url):
        raise NetdiskError("不是蓝奏云链接")

    sess = _Session(cfg)
    candidates = [url] + _alternates(url)
    last_err: Optional[NetdiskError] = None

    for i, cand in enumerate(candidates):
        # 原域名给两次机会：CDN 的签名校验偶发失败（'?SignError'），
        # 但同一链接从头再来一遍实测就能过 —— 不是链接的问题，是风控抖动
        attempts = 2 if i == 0 else 1
        for attempt in range(attempts):
            try:
                return _resolve_one(sess, cand, cfg, pwd=pwd)
            except NetdiskError as e:
                if e.dead or e.need_pwd:
                    raise                  # 失效/要密码：重试也没用
                last_err = e               # 网络/挑战/风控：再来一次或换域名
                if attempt + 1 < attempts:
                    time.sleep(1.0)
    raise last_err or NetdiskError("解析失败")


def _resolve_one(sess: _Session, url: str, cfg: Dict[str, Any], *,
                 pwd: str = "") -> Dict[str, Any]:
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.hostname}"

    # 1) 分享页
    st, fin, raw, _ = sess.request(url)
    page = raw.decode("utf-8", "replace")
    dead = _looks_dead(page)
    if dead:
        raise NetdiskError(dead, dead=True)
    if _looks_folder(page):
        # 文件夹分享：列清单，不逐个解析（添加时按单文件走）
        if "pwdload" in page:
            raise NetdiskError("该文件夹分享带提取码，暂不支持",
                               hint="带提取码的**单文件**可以解析；"
                                    "文件夹请用蓝奏云客户端或逐个分享文件")
        name = _name_from_page(page) or "蓝奏云文件夹"
        items, subfolders = _list_folder(sess, page, base)
        if not items and not subfolders:
            raise NetdiskError("文件夹是空的", hint="分享者还没往里放文件")
        return {
            "ok": True, "kind": "folder", "name": name,
            "items": items, "total": len(items),
            "note": (f"蓝奏云文件夹解析：{len(items)} 个文件"
                     + (f"（另有 {subfolders} 个子文件夹未展开）"
                        if subfolders else "")),
        }
    if _looks_pwd(page):
        # 带提取码：POST 直接换直链，跳过 iframe（页面自备签名）
        if not pwd:
            raise NetdiskError("该分享需要提取码", need_pwd=True,
                               hint="添加任务时在提取码栏填入分享者给的码")
        name = _name_from_page(page) or "download.bin"
        msg = _post_pwd(sess, page, pwd, referer=base + "/")
        if str(msg.get("zt")) != "1" or not msg.get("url"):
            reason = str(msg.get("info") or msg.get("inf") or "验证失败")
            raise NetdiskError(f"提取码不对（{reason}）", need_pwd=True,
                               hint="确认分享者给的提取码后重试")
        # inf 字段是真实文件名（成功响应里由前端写进页面标题的那个）
        name = str(msg.get("inf") or "").strip() or name
        dom = str(msg.get("dom") or "").rstrip("/")
        if not dom:
            raise NetdiskError("取直链失败（服务端没给下载域）",
                               hint="稍后再试")
        direct = dom + "/file/" + str(msg["url"])
        final_url, size, fn, headers = _finalize_direct(
            sess, direct, referer=base + "/")
        name = fn or name
        return NetdiskFile(
            name=name, size=size,
            size_text=f"{size/1048576:.1f} MB" if size >= 1048576
            else (f"{size/1024:.0f} KB" if size else ""),
            url=final_url, headers=headers,
            note=f"蓝奏云提取码分享解析（解了 {sess.challenges_solved} 层反爬挑战）",
        ).to_dict()
    name = _name_from_page(page)
    iframe = _iframe_url(page, base)
    if not iframe:
        raise NetdiskError("分享页里找不到下载 iframe",
                           hint="蓝奏云页面结构可能改版了，等上游适配")

    # 2) iframe 页 → 签名
    st2, fin2, raw2, _ = sess.request(iframe, referer=base + "/")
    fpage = raw2.decode("utf-8", "replace")
    info = _parse_iframe(fpage)

    # 3) POST ajaxfile → {zt, dom, url}。签名偶尔被拒（响应只有 '?SignError'
    #    一小段），重取一次 iframe 页拿新签名再试 —— 实测第二次就过。
    msg: Dict[str, Any] = {}
    for attempt in range(2):
        if attempt == 1:
            st2, _, raw2, _ = sess.request(iframe, referer=base + "/")
            info = _parse_iframe(raw2.decode("utf-8", "replace"))
        msg = _post_ajaxfile(sess, info, referer=iframe)
        if str(msg.get("zt")) == "1" and msg.get("url") and not _is_bad_sign(msg):
            break
        reason = str(msg.get("info") or msg.get("url") or "")
        if attempt == 1:
            raise NetdiskError(f"取直链失败（{reason or 'zt != 1'}）",
                               hint="链接可能已失效或接口改版，稍后再试")
    else:  # pragma: no cover - 循环必然 break 或 raise
        raise NetdiskError("取直链失败", hint="稍后再试")

    dom = str(msg.get("dom") or "").rstrip("/")
    if not dom:
        raise NetdiskError("取直链失败（服务端没给下载域）",
                           hint="稍后再试")
    direct = dom + "/file/" + str(msg["url"])

    # 4) CDN 直链后处理（挑战/验证页 → 干净直链）
    final_url, size, fn, headers = _finalize_direct(sess, direct, referer=base + "/")
    name = fn or name or "download.bin"

    return NetdiskFile(
        name=name, size=size,
        size_text=f"{size/1048576:.1f} MB" if size >= 1048576
        else (f"{size/1024:.0f} KB" if size else ""),
        url=final_url, headers=headers,
        note=f"蓝奏云公开分享解析（解了 {sess.challenges_solved} 层反爬挑战）",
    ).to_dict()


def try_resolve(url: str, cfg: Dict[str, Any], *, pwd: str = ""
                ) -> Dict[str, Any]:
    """给 server 用的宽容入口：不是蓝奏云返回 {ok:False, skip:True}。"""
    if not is_lanzou(url):
        return {"ok": False, "skip": True}
    try:
        return resolve(url, cfg, pwd=pwd)
    except NetdiskError as e:
        return e.to_dict()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "hint": ""}
