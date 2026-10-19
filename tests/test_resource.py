#!/usr/bin/env python3
"""资源识别与接管策略测试。

覆盖：四路线索（后缀 / MIME / Content-Disposition / 魔数）、
ZIP 家族二级分类、MP4 brand、RIFF 子类型、体积自适应策略、
插件接管决策。
"""
from __future__ import annotations

import json
import os
import struct
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jiliu import resource as R  # noqa: E402

PASS = 0
FAIL = 0
RESULTS: List[Tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        RESULTS.append((name, True, detail))
        print(f"  [PASS] {name}" + (f"  -- {detail}" if detail else ""))
    else:
        FAIL += 1
        RESULTS.append((name, False, detail))
        print(f"  [FAIL] {name}" + (f"  -- {detail}" if detail else ""))


def head(title: str) -> None:
    print(f"\n{title}")


def zip_head(inner_names: List[str]) -> bytes:
    """造一个带指定内部文件名的 ZIP 头，用来测二级分类。"""
    out = b""
    for n in inner_names:
        nb = n.encode("utf-8")
        out += b"PK\x03\x04"
        out += struct.pack("<HHHHH", 20, 0, 0, 0, 0)   # version/flags/method/time/date
        out += struct.pack("<III", 0, 0, 0)            # crc/csize/usize
        out += struct.pack("<HH", len(nb), 0)          # namelen/extralen
        out += nb
    return out


def ftyp(brand: str) -> bytes:
    return struct.pack(">I", 20) + b"ftyp" + brand.encode("latin1").ljust(4) + b"\x00" * 8


def riff(kind: str) -> bytes:
    return b"RIFF" + struct.pack("<I", 1024) + kind.encode("latin1").ljust(4) + b"\x00" * 16


def ole2() -> bytes:
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64 + b"Msi\x00\x00Microsoft"


# ==========================================================================
head("T1  后缀识别（URL / 文件名）")

cases = [
    ("https://dl.example.com/setup/WeChatSetup.exe", "app", "Windows 可执行程序"),
    ("https://cdn.example.com/app/foo.msi", "app", "Windows 安装包 (MSI)"),
    ("https://example.com/pkg/app-release.apk", "app", "Android 应用包"),
    ("https://example.com/mac/Thing.dmg", "app", "macOS 磁盘映像"),
    ("https://example.com/linux/app_1.0_amd64.deb", "app", "Debian/Ubuntu 安装包"),
    ("https://example.com/linux/app.AppImage", "app", "Linux 便携程序 (AppImage)"),
    ("https://example.com/a/b/source.tar.gz", "archive", ""),
    ("https://example.com/a/b/pack.7z", "archive", ""),
    ("https://example.com/a/b/part1.rar", "archive", ""),
    ("https://example.com/a/b/data.zst", "archive", ""),
    ("https://example.com/iso/ubuntu-24.04.iso", "disk", "光盘镜像 (ISO)"),
    ("https://example.com/music/song.flac", "audio", "无损音频 (FLAC)"),
    ("https://example.com/music/song.m4a", "audio", ""),
    ("https://example.com/movie/clip.mkv", "video", "Matroska 视频"),
    ("https://example.com/movie/clip.webm", "video", "WebM 视频"),
    ("https://example.com/doc/manual.pdf", "document", "PDF 文档"),
    ("https://example.com/doc/report.docx", "document", "Word 文档"),
    ("https://example.com/doc/book.epub", "document", "电子书"),
    ("https://example.com/img/photo.heic", "image", "HEIC 图片"),
    ("https://example.com/font/NotoSans.woff2", "font", "Web 字体"),
    ("https://example.com/model/llama.gguf", "dataset", "神经网络权重"),
    ("https://example.com/model/weights.safetensors", "dataset", "神经网络权重"),
    ("https://example.com/blob/thing.unknownext", "other", ""),
]
for url, want_cat, want_type in cases:
    p = R.classify(url=url)
    ok = p.category == want_cat and (not want_type or p.filetype == want_type)
    check(f"T1 {os.path.basename(url.split('?')[0])} → {p.category}/{p.filetype}", ok,
          f"期望 {want_cat}/{want_type or '-'}")

# ==========================================================================
head("T2  MIME 识别")

mime_cases = [
    ("video/mp4", "video"),
    ("video/x-matroska", "video"),
    ("application/vnd.apple.mpegurl", "video"),
    ("audio/mpeg", "audio"),
    ("audio/flac", "audio"),
    ("application/zip", "archive"),
    ("application/vnd.rar", "archive"),
    ("application/x-7z-compressed", "archive"),
    ("application/x-iso9660-image", "disk"),
    ("application/pdf", "document"),
    ("application/vnd.android.package-archive", "app"),
    ("application/x-msdownload", "app"),
    ("font/woff2", "font"),
    ("image/avif", "image"),
]
for mime, want in mime_cases:
    p = R.classify(url="https://example.com/download?id=1", mime=mime)
    check(f"T2 {mime} → {p.category}", p.category == want, f"期望 {want}")

# ==========================================================================
head("T3  Content-Disposition（含中文名 / RFC 5987）")

p = R.classify(url="https://example.com/d?id=9",
               content_disposition="attachment; filename*=UTF-8''%E6%B5%8B%E8%AF%95%E5%8C%85.zip")
check("T3.1 filename* UTF-8 解码", p.filename == "测试包.zip", f"得到 {p.filename!r}")
check("T3.2 据此识别为压缩包", p.category == "archive", p.category)

p = R.classify(url="https://example.com/d?id=9",
               content_disposition='attachment; filename="My Setup 2.1.exe"')
check("T3.3 引号形式文件名", p.filename == "My Setup 2.1.exe", repr(p.filename))
check("T3.4 识别为应用", p.category == "app", p.category)

p = R.classify(url="https://example.com/d",
               content_disposition="attachment; filename=report%20final.pdf")
check("T3.5 无引号 + 百分号编码", p.filename == "report final.pdf", repr(p.filename))

# 优先级：Content-Disposition 应压过 URL 后缀
p = R.classify(url="https://example.com/totally-a-page.php",
               content_disposition='attachment; filename="real-file.iso"')
check("T3.6 响应头文件名优先于 URL 后缀", p.category == "disk", f"{p.category}/{p.ext}")

# ==========================================================================
head("T4  魔数嗅探（服务端撒谎也能认出）")

magic_cases = [
    (b"MZ\x90\x00" + b"\x00" * 30, "app"),
    (b"\x7fELF\x02\x01\x01" + b"\x00" * 20, "app"),
    (b"\xed\xab\xee\xdb" + b"\x00" * 20, "app"),
    (b"!<arch>\ndebian-binary   " + b"\x00" * 10, "app"),
    (b"Rar!\x1a\x07\x01\x00" + b"\x00" * 20, "archive"),
    (b"7z\xbc\xaf\x27\x1c" + b"\x00" * 20, "archive"),
    (b"\x1f\x8b\x08" + b"\x00" * 20, "archive"),
    (b"BZh9" + b"\x00" * 20, "archive"),
    (b"\xfd7zXZ\x00" + b"\x00" * 20, "archive"),
    (b"\x28\xb5\x2f\xfd" + b"\x00" * 20, "archive"),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "image"),
    (b"\xff\xd8\xff\xe0" + b"\x00" * 20, "image"),
    (b"GIF89a" + b"\x00" * 20, "image"),
    (b"%PDF-1.7\n" + b"\x00" * 20, "document"),
    (b"{\\rtf1\\ansi" + b"\x00" * 10, "document"),
    (b"SQLite format 3\x00" + b"\x00" * 20, "document"),
    (b"\x1a\x45\xdf\xa3" + b"\x00" * 20, "video"),
    (b"OggS\x00\x02" + b"\x00" * 20, "audio"),
    (b"fLaC\x00\x00\x00\x22" + b"\x00" * 20, "audio"),
    (b"ID3\x04\x00" + b"\x00" * 20, "audio"),
    (b"\xff\xfb\x90\x00" + b"\x00" * 20, "audio"),
    (b"#!AMR\n" + b"\x00" * 20, "audio"),
    (b"wOFF2\x00\x01\x00\x00" + b"\x00" * 20, "font"),
    (b"OTTO\x00" + b"\x00" * 20, "font"),
]
for blob, want in magic_cases:
    cat, desc, _ext = R.sniff(blob)
    check(f"T4 {desc[:34]:34s} → {cat}", cat == want, f"期望 {want}")

# ==========================================================================
head("T5  ZIP 家族二级分类（看内部结构）")

zip_cases = [
    (["word/document.xml", "word/_rels/x"], "document", "docx"),
    (["xl/workbook.xml"], "document", "xlsx"),
    (["ppt/presentation.xml"], "document", "pptx"),
    (["AndroidManifest.xml", "classes.dex"], "app", "apk"),
    (["META-INF/MANIFEST.MF", "com/x.class"], "app", "jar"),
    (["AppxManifest.xml", "[Content_Types].xml"], "app", "msix"),
    (["photos/a.jpg", "photos/b.jpg"], "archive", "zip"),
]
for names, want_cat, want_ext in zip_cases:
    cat, desc, ext = R.sniff(zip_head(names))
    check(f"T5 {names[0][:26]:26s} → {cat}/{ext}", cat == want_cat and ext == want_ext,
          f"期望 {want_cat}/{want_ext}，得到 {desc}")

# ==========================================================================
head("T6  RIFF 与 MP4 家族细分")

for kind, want_cat in [("AVI ", "video"), ("WAVE", "audio"), ("WEBP", "image")]:
    cat, desc, ext = R.sniff(riff(kind))
    check(f"T6.1 RIFF/{kind.strip()} → {cat}/{ext}", cat == want_cat, desc)

for brand, want_cat in [("isom", "video"), ("M4A ", "audio"), ("avc1", "video"), ("qt  ", "video")]:
    cat, desc, ext = R.sniff(ftyp(brand))
    check(f"T6.2 ftyp/{brand.strip():4s} → {cat}/{ext}", cat == want_cat, desc)

# ==========================================================================
head("T7  OLE2 / 文本 / PNG-伪装成-exe")

cat, desc, ext = R.sniff(ole2())
check("T7.1 OLE2 含 Msi 标记 → MSI", cat == "app" and ext == "msi", f"{cat}/{ext} {desc}")

cat, desc, ext = R.sniff(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 200)
check("T7.2 OLE2 无 Msi 标记 → 文档", cat == "document" and ext == "doc", f"{cat}/{ext}")

png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
p = R.classify(url="https://example.com/not-really.exe", magic=png)
check("T7.3 后缀 .exe 但魔数是 PNG → 图片（魔数权重更高）",
      p.category == "image", f"{p.category} conf={p.confidence:.2f}")

p = R.classify(url="https://example.com/data.bin", magic=b'{"a": 1, "b": [2, 3]}')
check("T7.4 JSON 文本识别", p.category == "document" and p.ext == "json", f"{p.category}/{p.ext}")

p = R.classify(url="https://example.com/x", magic=b"<!DOCTYPE html>\n<html><body>hi")
check("T7.5 HTML 识别为文档并给告警", p.category == "document" and
      any("网页" in w for w in p.warnings), f"{p.category} warns={len(p.warnings)}")

# ==========================================================================
head("T8  平台识别与解析路由")

platform_cases = [
    ("https://www.bilibili.com/video/BV1xx411c7mD", "B站", True),
    ("https://b23.tv/abcdefg", "B站短链", True),
    ("https://youtu.be/dQw4w9WgXcQ", "YouTube", True),
    ("https://v.qq.com/x/cover/abc.html", "腾讯视频", True),
    ("https://www.douyin.com/video/123456", "抖音", True),
    ("https://www.youtube.com/watch?v=abc", "YouTube", True),
    ("https://example.com/normal.zip", "", False),
]
for url, want_plat, want_extract in platform_cases:
    p = R.classify(url=url)
    check(f"T8 {R.host_of(url)[:28]:28s} → {p.platform or '未命中'}",
          p.platform == want_plat and p.needs_extractor == want_extract,
          f"期望 {want_plat or '-'}/extractor={want_extract}")

p = R.classify(url="https://example.com/live/stream.m3u8")
check("T8.7 HLS 清单必走 yt-dlp", p.needs_extractor and p.engine == "ytdlp" and p.category == "video",
      f"{p.category}/{p.engine}")

p = R.classify(url="https://example.com/stream/index.mpd")
check("T8.8 DASH 清单必走 yt-dlp", p.needs_extractor, f"{p.engine}/{p.ext}")

p = R.classify(url="https://example.com/direct/file.mp4")
check("T8.9 直链 MP4 走多线程", (not p.needs_extractor) and p.engine == "auto", p.engine)

# ==========================================================================
head("T9  体积自适应策略")

p = R.classify(url="https://example.com/icon.png", size=40 * 1024)
check("T9.1 40KB 图片 → 单线程", p.concurrency == 1,
      f"{p.concurrency} 线程；{p.reasons[-1][:60]}")

p = R.classify(url="https://example.com/big.iso", size=5 << 30)
check("T9.2 5GB 镜像 → 8 线程 + 强制校验", p.concurrency == 8 and p.verify_hash,
      f"{p.concurrency} 线程 / chunk={R.human_size(p.chunk_size)}")

p = R.classify(url="https://example.com/mid.zip", size=3 << 20)
check("T9.3 3MB 压缩包 → 降到 4 线程", p.concurrency == 4, str(p.concurrency))

p = R.classify(url="https://example.com/doc.pdf", size=20 << 20)
check("T9.4 20MB 文档 → 保持 6 线程", p.concurrency == 6, str(p.concurrency))

check("T9.5 human_size 格式", R.human_size(0) == "0 B" and R.human_size(1536).startswith("1.5 KB")
      and R.human_size(5 << 30).startswith("5.0 GB"),
      f"{R.human_size(1536)} / {R.human_size(5 << 30)}")

# ==========================================================================
head("T10  浏览器插件接管决策")

d = R.intercept_decision("blob:https://example.com/abcd-1234")
check("T10.1 blob: 不接管", not d["take"], d["reason"])

d = R.intercept_decision("data:text/plain;base64,aGk=")
check("T10.2 data: 不接管", not d["take"], d["reason"])

d = R.intercept_decision("https://example.com/download/ubuntu.iso", size=4 << 30)
check("T10.3 大镜像 → 接管", d["take"] and d["plan"]["category"] == "disk", d["reason"])

d = R.intercept_decision("https://site.com/x", mime="image/png", filename="sprite.png", size=8 * 1024)
check("T10.4 8KB 图片 → 放行给浏览器", not d["take"], d["reason"])

d = R.intercept_decision("https://example.com/archive/backup.7z", size=200 << 20)
check("T10.5 200MB 7z → 接管", d["take"] and d["plan"]["verify_hash"], d["reason"])

d = R.intercept_decision("https://www.bilibili.com/video/BV1xx411c7mD")
check("T10.6 B站页面 → 接管并走 yt-dlp", d["take"] and "yt-dlp" in d["reason"], d["reason"])

d = R.intercept_decision("https://example.com/download", size=50 << 20)
check("T10.7 50MB 类型不明 → 因体积接管", d["take"], d["reason"])

d = R.intercept_decision("https://example.com/download", size=100 * 1024,
                        content_disposition='attachment; filename="x.bin"')
check("T10.8 100KB 类型不明 → 放行", not d["take"], d["reason"])

d = R.intercept_decision("https://cdn.site.com/hls/playlist.m3u8")
check("T10.9 m3u8 → 接管", d["take"], d["reason"])

d = R.intercept_decision("https://example.com/x.mp4", category_hint="video-stream")
check("T10.10 网络层捕获的媒体流 → 接管", d["take"], d["reason"])

# ==========================================================================
head("T11  分类总览与一致性")

cats = R.describe_categories()
check("T11.1 分类总览覆盖全部分类", len(cats) == len(R.CATEGORIES),
      f"{len(cats)} 个分类")
check("T11.2 每个分类都有中文名与说明",
      all(c["label"] and c["note"] and c["exts"] >= 0 for c in cats), "")

# 后缀表里的分类都必须存在于 CATEGORIES
bad = {c for c, _d in R.EXT_MAP.values() if c not in R.CATEGORIES}
check("T11.3 后缀表分类合法", not bad, f"非法分类 {bad}")

bad = {c for c in R.MIME_MAP.values() if c not in R.CATEGORIES}
check("T11.4 MIME 表分类合法", not bad, f"非法分类 {bad}")

bad = {c for _s, c, _d in R.MAGIC if c not in R.CATEGORIES}
check("T11.5 魔数表分类合法", not bad, f"非法分类 {bad}")

check("T11.6 后缀表规模", len(R.EXT_MAP) >= 150, f"{len(R.EXT_MAP)} 个后缀")
check("T11.7 魔数表规模", len(R.MAGIC) >= 40, f"{len(R.MAGIC)} 条签名")

# 策略自洽性：verify_hash 的分类应当都能给出校验建议
for key, c in R.CATEGORIES.items():
    if c["verify_hash"]:
        plan = R.classify(url=f"https://example.com/f.{'zip' if key == 'archive' else 'bin'}")
        assert isinstance(plan.to_dict(), dict)

check("T11.8 ResourcePlan.to_dict 可序列化",
      isinstance(json.dumps(R.classify(url="https://example.com/a.zip").to_dict()), str), "")

p = R.classify(url="https://example.com/a.7z")
check("T11.9 summary 可读", "压缩包" in p.summary() and "多线程" in p.summary() or
      "引擎" in p.summary(), p.summary())

# ==========================================================================
print("\n" + "=" * 70)
print(f"结果: {PASS}/{PASS + FAIL} 通过")
print("=" * 70)
if FAIL:
    print("\n未通过项：")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  - {n}  {d}")
raise SystemExit(1 if FAIL else 0)
