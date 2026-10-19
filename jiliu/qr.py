"""二维码渲染。

为什么自己生成，而不是丢个链接给前端：

  1）B站扫码接口返回的是一串 **URL 文本**（`https://passport.bilibili.com/...`），
     不是图片。前端拿到字符串没法直接塞进 `<img>`，必须要有人编码成二维码。
  2）用在线二维码服务（api.qrserver.com 之类）是国内可用性最差的方案 ——
     恰恰在最需要扫码登录的时候（没登录 = 拿不到高清画质）依赖一个外部服务。
  3）纯前端方案要内联一个几十 KB 的 JS 二维码库；而这里本来就有 Python，
     `qrcode` 库是纯 Python 的，用 `PyPNGImage` 输出 PNG 时**连 Pillow 都不需要**。

所以：后端编码，前端只负责把 data URI 塞进 `<img>`。

降级策略：库缺失不让整个登录流程崩掉 —— 返回空串，由调用方如实告诉用户
「本机没装二维码库」，而不是给一个永远扫不出来的空白框。
"""
from __future__ import annotations

import base64
import io
from typing import Any, List, Optional

_DEFAULT_BOX = 6      # 每个模块画几个像素
_DEFAULT_BORDER = 2   # 静区，规范要求 >= 4 个模块；B站的码本身带留白，2 够

_unavailable_reason = ""


def available() -> bool:
    """本机能不能生成二维码。"""
    return _load() is not None


def unavailable_reason() -> str:
    return _unavailable_reason


def _load() -> Optional[Any]:
    """惰性导入 qrcode。缺失时记住原因，避免反复 import 失败。"""
    global _unavailable_reason
    try:
        import qrcode  # noqa: F401
        import qrcode.image.pure  # noqa: F401
        return qrcode
    except Exception as exc:  # noqa: BLE001
        _unavailable_reason = f"{type(exc).__name__}: {exc}"
        return None


def matrix(text: str, *, border: int = _DEFAULT_BORDER) -> List[List[bool]]:
    """返回二维码的布尔矩阵（True = 黑块）。主要用于测试与自检。

    不经过 PNG 编码，可以直接断言模块分布对不对 —— 测「生成的图是不是
    一个合法二维码」，看矩阵比解码 PNG 容易得多。
    """
    qrcode = _load()
    if qrcode is None:
        raise RuntimeError("没有二维码库：" + _unavailable_reason)
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=border,
    )
    qr.add_data(text or "")
    qr.make(fit=True)
    return qr.get_matrix()


def png_bytes(text: str, *, box_size: int = _DEFAULT_BOX,
              border: int = _DEFAULT_BORDER) -> bytes:
    """把文本编码成 PNG 字节。"""
    qrcode = _load()
    if qrcode is None:
        raise RuntimeError("没有二维码库：" + _unavailable_reason)
    from qrcode.image.pure import PyPNGImage

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=max(1, int(box_size)),
        border=max(0, int(border)),
    )
    qr.add_data(text or "")
    qr.make(fit=True)
    img = qr.make_image(image_factory=PyPNGImage)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def png_base64(text: str, *, box_size: int = _DEFAULT_BOX,
               border: int = _DEFAULT_BORDER) -> str:
    """纯 base64（不带 data: 前缀），与面板里 `"data:image/png;base64," + x` 对齐。

    生成失败返回空串 —— 调用方据此显示真实原因，而不是画一个空白二维码。
    """
    if not text:
        return ""
    try:
        return base64.b64encode(png_bytes(text, box_size=box_size,
                                          border=border)).decode("ascii")
    except Exception:  # noqa: BLE001
        return ""


def png_data_uri(text: str, *, box_size: int = _DEFAULT_BOX,
                 border: int = _DEFAULT_BORDER) -> str:
    """带 `data:image/png;base64,` 前缀的完整 URI。"""
    b64 = png_base64(text, box_size=box_size, border=border)
    return f"data:image/png;base64,{b64}" if b64 else ""
