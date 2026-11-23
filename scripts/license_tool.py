#!/usr/bin/env python3
"""许可证签发工具（**不要**把私钥和这个脚本打进发行包）。

用法：

    # 第一次：生成一对签名密钥（私钥写到家目录，绝不进仓库）
    python scripts/license_tool.py --new-key

    # 给客户签发。dev 写用户「设置 › 版本与授权」里显示的设备标识，
    # 写 * 表示不绑机器（适合企业批量、或用于渠道测试）
    python scripts/license_tool.py --issue --name "张三" --dev ABCD-1234-5678-9EF0 --days 365
    python scripts/license_tool.py --issue --name "Acme 公司" --dev "*" --days 3650 --id JL-B2B-0007

    # 检查一张证
    python scripts/license_tool.py --show "JILIU1.xxx.yyy"

私钥路径可以用 JILIU_SIGNING_KEY 覆盖；默认 ~/.jiliu-signing.key。
私钥丢了 = 所有已发出的证都无法再签新版（旧证仍可验证，因为公钥在客户端里）。
所以生成后**立刻备份**。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jiliu import license as L  # noqa: E402

KEY_PATH = pathlib.Path(
    os.environ.get("JILIU_SIGNING_KEY") or (pathlib.Path.home() / ".jiliu-signing.key")
)


def _h(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _scalar(seed: bytes) -> int:
    h = _h(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= (1 << 254)
    return a


def public_key(seed: bytes) -> bytes:
    return L._encode_point(L._mul(L._B, _scalar(seed)))


def sign(seed: bytes, msg: bytes) -> bytes:
    h = _h(seed)
    a = _scalar(seed)
    A = L._encode_point(L._mul(L._B, a))
    r = int.from_bytes(_h(h[32:] + msg), "little") % L._Q
    R = L._encode_point(L._mul(L._B, r))
    k = int.from_bytes(_h(R + A + msg), "little") % L._Q
    S = (r + k * a) % L._Q
    return R + S.to_bytes(32, "little")


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def load_seed() -> bytes:
    if not KEY_PATH.exists():
        sys.exit(f"找不到私钥：{KEY_PATH}\n先跑一次 --new-key 生成。")
    raw = KEY_PATH.read_text(encoding="utf-8").strip()
    seed = bytes.fromhex(raw)
    if len(seed) != 32:
        sys.exit("私钥文件格式不对（应当是 64 个十六进制字符）")
    return seed


def cmd_new_key() -> None:
    if KEY_PATH.exists():
        sys.exit(f"私钥已存在：{KEY_PATH}\n换密钥会让所有旧许可证失效，确认要换就先手动移走它。")
    seed = os.urandom(32)
    KEY_PATH.write_text(seed.hex(), encoding="utf-8")
    try:
        os.chmod(KEY_PATH, 0o600)
    except Exception:
        pass
    pub = public_key(seed).hex()
    print(f"私钥已写入：{KEY_PATH}")
    print("请立刻备份这个文件；丢了就无法再签发新证。")
    print()
    print("把下面这行填进 jiliu/license.py 的 PUBLIC_KEY_HEX：")
    print(f'PUBLIC_KEY_HEX = "{pub}"')


def cmd_issue(a) -> None:
    seed = load_seed()
    payload = {
        "v": 1,
        "id": a.id or ("JL-" + time.strftime("%Y%m%d") + "-" + os.urandom(2).hex().upper()),
        "plan": "pro",
        "name": a.name or "",
        "dev": (a.dev or "*").strip().upper(),
        "exp": a.exp or time.strftime("%Y-%m-%d", time.localtime(time.time() + a.days * 86400)),
        "feat": a.feature or [],
        "issued": time.strftime("%Y-%m-%d"),
    }
    msg = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    sig = sign(seed, msg)
    token = "JILIU1." + _b64u(msg) + "." + _b64u(sig)
    print(token)
    print()
    print(f"# 客户：{payload['name'] or '-'}  设备：{payload['dev']}  到期：{payload['exp']}",
          file=sys.stderr)
    if a.out:
        pathlib.Path(a.out).write_text(token + "\n", encoding="utf-8")
        print(f"# 已写入 {a.out}", file=sys.stderr)


def cmd_show(token: str) -> None:
    r = L.parse_license(token)
    if not r.get("ok"):
        sys.exit("验签失败：" + str(r.get("error")))
    print(json.dumps(r["payload"], ensure_ascii=False, indent=2))
    print("签名有效 ✓")
    print("本机设备标识：" + L.device_id())


def main() -> None:
    ap = argparse.ArgumentParser(description="汲流许可证签发工具")
    ap.add_argument("--new-key", action="store_true", help="生成签名密钥对")
    ap.add_argument("--issue", action="store_true", help="签发一张许可证")
    ap.add_argument("--show", metavar="TOKEN", help="检查一张许可证")
    ap.add_argument("--name", default="", help="客户名（会写进许可证）")
    ap.add_argument("--dev", default="*", help="绑定的设备标识，* = 不绑机器")
    ap.add_argument("--days", type=int, default=365, help="有效天数")
    ap.add_argument("--exp", default="", help="直接指定到期日 YYYY-MM-DD")
    ap.add_argument("--id", default="", help="许可证号（留空自动生成）")
    ap.add_argument("--feature", action="append", default=[], help="限定功能（可多次）")
    ap.add_argument("--out", default="", help="把许可证写入文件")
    a = ap.parse_args()
    if a.new_key:
        cmd_new_key()
    elif a.issue:
        cmd_issue(a)
    elif a.show:
        cmd_show(a.show)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
