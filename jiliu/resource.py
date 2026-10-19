"""资源识别与接管策略。

下载器最容易犯的错是「一视同仁」—— 拿 8 线程去分片一个 40KB 的图标，
或者把 .m3u8 流当普通文件硬拉下来得到一堆碎片。不同类型的东西，
该用不同的方式拿。

这里做两件事：
    1. 认出这是什么（应用 / 压缩包 / 音频 / 视频 / 文档 / 镜像 / 图片 / 字体）
    2. 给出这一类该怎么下的策略（走哪个引擎、几线程、多大分片、
       要不要验哈希、能不能自动解压）

识别走四条线索，按可靠度从高到低互相印证：
    ① Content-Disposition 里的文件名  —— 服务端自己声明的，最可信
    ② Content-Type                    —— 服务端声明的 MIME，偶有撒谎
    ③ URL 路径后缀                     —— 最省事，但 CDN 常带假后缀或没后缀
    ④ 文件头魔数（magic bytes）        —— 拿到实打实的前几 KB 就骗不了人
"""
from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from . import protocols

# ==========================================================================
# 分类定义
# ==========================================================================

CATEGORIES: Dict[str, Dict[str, Any]] = {
    "video": {
        "label": "视频",
        "engine": "auto",          # 页面/流走 yt-dlp，直链走多线程
        "concurrency": 8,
        "chunk_size": 1 << 20,     # 1 MiB
        "verify_hash": False,
        "can_extract": False,
        "can_transcode": True,
        "min_multithread": 1 << 20,
        "note": "支持边下边播的容器建议整段下；HLS/DASH 流交给 yt-dlp 合流",
    },
    "audio": {
        "label": "音频",
        "engine": "auto",
        "concurrency": 8,
        "chunk_size": 1 << 20,
        "verify_hash": False,
        "can_extract": False,
        "can_transcode": True,
        "min_multithread": 1 << 20,
        "note": "可从视频里抽音轨（需 ffmpeg）",
    },
    "playlist": {
        "label": "合集 / 播放列表",
        "engine": "ytdlp",         # 只有 yt-dlp 能展开合集
        "concurrency": 1,          # 逐条下，别把站点惹急
        "chunk_size": 0,
        "verify_hash": False,
        "can_extract": False,
        "can_transcode": False,
        "min_multithread": 0,
        "note": "整季/整合集：默认按合集建子目录、文件名带序号；"
                "可只挑其中几集；开启「跳过已下载」后同一集不会重复下",
    },
    "app": {
        "label": "应用",
        "engine": "multithread",
        "concurrency": 8,
        "chunk_size": 1 << 20,
        "verify_hash": True,       # 安装包下坏了最坑，必须校验
        "can_extract": False,
        "can_transcode": False,
        "min_multithread": 1 << 20,
        "note": "下完先对哈希再装；Windows 安装包还要看数字签名",
    },
    "archive": {
        "label": "压缩包",
        "engine": "multithread",
        "concurrency": 8,
        "chunk_size": 1 << 20,
        "verify_hash": True,
        "can_extract": True,
        "can_transcode": False,
        "min_multithread": 1 << 20,
        "note": "分卷包要全部下齐才能解压；只下一卷等于没下",
    },
    "document": {
        "label": "文档",
        "engine": "multithread",
        "concurrency": 6,
        "chunk_size": 512 << 10,
        "verify_hash": False,
        "can_extract": False,
        "can_transcode": False,
        "min_multithread": 4 << 20,
        "note": "体积普遍不大，小文件单线程反而更快",
    },
    "disk": {
        "label": "光盘镜像",
        "engine": "multithread",
        "concurrency": 8,
        "chunk_size": 4 << 20,     # 镜像动辄几 GB，大分片省开销
        "verify_hash": True,
        "can_extract": True,
        "can_transcode": False,
        "min_multithread": 8 << 20,
        "note": "几 GB 起，务必留足磁盘和时间；建议校验 SHA256",
    },
    "image": {
        "label": "图片",
        "engine": "multithread",
        "concurrency": 4,
        "chunk_size": 256 << 10,
        "verify_hash": False,
        "can_extract": False,
        "can_transcode": True,
        "min_multithread": 2 << 20,
        "note": "多为小文件，多数情况单线程直下更划算",
    },
    "font": {
        "label": "字体",
        "engine": "multithread",
        "concurrency": 4,
        "chunk_size": 256 << 10,
        "verify_hash": False,
        "can_extract": False,
        "can_transcode": False,
        "min_multithread": 2 << 20,
        "note": "小文件，同上",
    },
    "dataset": {
        "label": "数据集/模型",
        "engine": "multithread",
        "concurrency": 8,
        "chunk_size": 4 << 20,
        "verify_hash": True,
        "can_extract": True,
        "can_transcode": False,
        "min_multithread": 4 << 20,
        "note": "体积大且常有官方哈希，下完必对",
    },
    # —— 下面两类不是「文件类型」，是「取件方式」——
    #   它们与 video/archive 那些不在一个维度上，所以要排在分类表里，
    #   让「磁力链接」这种 URL 也有个正经归属，别落到 other。
    "bt": {
        "label": "BT / 磁力",
        "engine": "aria2",         # P2P 只能交给 aria2，自研引擎不碰
        "concurrency": 0,          # 连接数由 BT 协议自己和 tracker/peer 谈，不由我们定
        "chunk_size": 0,           # 分片由 BT 协议自己切，外部指定没有意义
        "verify_hash": False,      # BT 自带分片校验，再叠一层是重复劳动
        "can_extract": False,
        "can_transcode": False,
        "min_multithread": 0,
        "note": "磁力/种子/ed2k 走 aria2 的 BT 引擎（开 DHT/PEX/LPD 找源）；"
                "下完会做种一段时间，时长与分享率在设置里改",
    },
    "netdisk": {
        "label": "网盘链接",
        "engine": "native",
        "concurrency": 1,
        "chunk_size": 1 << 20,
        "verify_hash": False,
        "can_extract": False,
        "can_transcode": False,
        "min_multithread": 1 << 20,
        "note": "网盘分享页要先转成直链；汲流不逆向私有接口（违反条款且接口一改即废）。"
                "拿到 http 直链后走普通多线程下载",
    },
    "other": {
        "label": "其他文件",
        "engine": "multithread",
        "concurrency": 8,
        "chunk_size": 1 << 20,
        "verify_hash": False,
        "can_extract": False,
        "can_transcode": False,
        "min_multithread": 2 << 20,
        "note": "按通用策略处理",
    },
}

# ==========================================================================
# ① 后缀表：扩展名 -> (分类, 更细的说明)
# ==========================================================================

EXT_MAP: Dict[str, Tuple[str, str]] = {}


def _reg(cat: str, desc: str, exts: str) -> None:
    for e in exts.split():
        EXT_MAP[e.lower().lstrip(".")] = (cat, desc)


# —— 视频 ——
_reg("video", "MP4 视频", "mp4 m4v")
_reg("video", "Matroska 视频", "mkv")
_reg("video", "WebM 视频", "webm")
_reg("video", "AVI 视频", "avi")
_reg("video", "QuickTime 视频", "mov qt")
_reg("video", "Flash 视频", "flv f4v")
_reg("video", "MPEG 传输流", "ts m2ts mts")
_reg("video", "HLS 播放列表", "m3u8 m3u")
_reg("video", "DASH 清单", "mpd")
_reg("video", "Microsoft 视频", "wmv asf")
_reg("video", "MPEG 视频", "mpg mpeg vob")
_reg("video", "RealMedia 视频", "rm rmvb")
_reg("video", "3GPP 视频", "3gp 3g2")
_reg("video", "Ogg 视频", "ogv")
_reg("video", "Adobe Flash 媒体清单", "f4m")
_reg("video", "Smooth Streaming 清单", "ism")

# —— 音频 ——
_reg("audio", "MP3 音频", "mp3")
_reg("audio", "无损音频 (FLAC)", "flac")
_reg("audio", "无损音频 (ALAC/APE)", "alac ape wv")
_reg("audio", "WAV 音频", "wav wave")
_reg("audio", "AAC 音频", "aac m4a m4b")
_reg("audio", "Ogg 音频", "ogg oga")
_reg("audio", "Opus 音频", "opus")
_reg("audio", "Windows Media 音频", "wma")
_reg("audio", "AIFF 音频", "aiff aif aifc")
_reg("audio", "MIDI", "mid midi")
_reg("audio", "AMR 语音", "amr")
_reg("audio", "DTS 音频", "dts")
_reg("audio", "AC-3 音频", "ac3")

# —— 应用 ——
_reg("app", "Windows 可执行程序", "exe com scr")
_reg("app", "Windows 安装包 (MSI)", "msi msp")
_reg("app", "Windows 应用包 (MSIX/APPX)", "msix msixbundle appx appxbundle")
_reg("app", "Windows 补丁", "msp msu")
_reg("app", "Windows 脚本", "bat cmd ps1 vbs js")
_reg("app", "macOS 磁盘映像", "dmg")
_reg("app", "macOS 安装包", "pkg mpkg")
_reg("app", "macOS 应用包", "app")
_reg("app", "Debian/Ubuntu 安装包", "deb")
_reg("app", "RedHat/Fedora 安装包", "rpm")
_reg("app", "Linux 便携程序 (AppImage)", "appimage")
_reg("app", "Linux 快照包", "snap flatpak flatpakref")
_reg("app", "Arch 安装包", "pkg.tar.zst pkg.tar.xz")
_reg("app", "Android 应用包", "apk apks xapk aab")
_reg("app", "iOS 应用包", "ipa")
_reg("app", "Java 程序包", "jar war ear")
_reg("app", "安卓固件/刷机包", "img hex")

# —— 压缩包 ——
_reg("archive", "ZIP 压缩包", "zip zipx")
_reg("archive", "RAR 压缩包", "rar r00")
_reg("archive", "7-Zip 压缩包", "7z")
_reg("archive", "TAR 归档", "tar")
_reg("archive", "Gzip 压缩", "gz tgz")
_reg("archive", "Bzip2 压缩", "bz2 tbz tbz2")
_reg("archive", "XZ 压缩", "xz txz lzma")
_reg("archive", "Zstandard 压缩", "zst tzst")
_reg("archive", "LZ4 压缩", "lz4")
_reg("archive", "Lzip 压缩", "lz")
_reg("archive", "Windows 压缩包 (CAB)", "cab")
_reg("archive", "Unix compress", "z")
_reg("archive", "Zoo 压缩", "zoo")

# —— 光盘镜像 ——
_reg("disk", "光盘镜像 (ISO)", "iso")
_reg("disk", "磁盘映像", "img ima vhd vhdx vmdk qcow2")
_reg("disk", "CD 镜像", "bin cue nrg mdf")
_reg("disk", "UDF 映像", "udf")

# —— 文档 ——
_reg("document", "PDF 文档", "pdf")
_reg("document", "Word 文档", "doc docx docm dot dotx odt rtf")
_reg("document", "Excel 表格", "xls xlsx xlsm xlsb ods csv tsv")
_reg("document", "PowerPoint 演示", "ppt pptx pptm odp")
_reg("document", "纯文本", "txt text log md markdown rst")
_reg("document", "电子书", "epub mobi azw azw3 fb2 djvu cbz cbr")
_reg("document", "网页存档", "html htm mhtml mht xhtml")
_reg("document", "数据文件", "json xml yaml yml toml ini cfg sql db sqlite")
_reg("document", "书籍/资料", "chm hlp")

# —— 数据集 / 模型 ——
_reg("dataset", "神经网络权重", "pt pth ckpt safetensors onnx pb h5 keras tflite gguf ggml")
_reg("dataset", "词向量/模型文件", "vec model vocab spm bpe")
_reg("dataset", "科学数据", "hdf5 h5 nc mat parquet arrow feat npy npz")
_reg("dataset", "数据分片", "index data shard")

# —— 图片 ——
_reg("image", "JPEG 图片", "jpg jpeg jpe jfif")
_reg("image", "PNG 图片", "png")
_reg("image", "GIF 图片", "gif")
_reg("image", "WebP 图片", "webp")
_reg("image", "BMP 图片", "bmp dib")
_reg("image", "TIFF 图片", "tif tiff")
_reg("image", "矢量图", "svg svgz")
_reg("image", "AVIF 图片", "avif")
_reg("image", "HEIC 图片", "heic heif")
_reg("image", "图标", "ico icns")
_reg("image", "设计源文件", "psd psb ai xcf sketch fig")
_reg("image", "RAW 照片", "raw cr2 cr3 nef arw dng orf rw2")

# —— 字体 ——
_reg("font", "TrueType 字体", "ttf ttc")
_reg("font", "OpenType 字体", "otf")
_reg("font", "Web 字体", "woff woff2 eot")
_reg("font", "位图字体", "fon fnt pfb pfm")

# —— 其他 ——
# .bin 是纯占位后缀：固件、模型分片、磁盘镜像、随便什么二进制都可能叫它。
# 不猜，标成 other，等文件头魔数来说话。
_reg("other", "通用二进制文件（后缀不足以判断）", "bin")
_reg("other", "下载中间文件", "part tmp partial crdownload download")

# —— 流媒体清单（必须走 yt-dlp）——
MANIFEST_EXTS = {"m3u8", "m3u", "mpd", "f4m", "ism"}

# 这些后缀是「占位型」的，服务端经常拿它们糊弄人。
# 一旦文件头魔数给出更具体的判断，就该让魔数覆盖掉它。
GENERIC_EXTS = {
    "bin", "dat", "data", "download", "tmp", "part", "partial", "crdownload",
    "file", "octet-stream", "raw", "unknown", "blob", "get", "php", "asp",
    "aspx", "jsp", "do", "cgi", "action",
}

# ==========================================================================
# ② MIME 表
# ==========================================================================

MIME_MAP: Dict[str, str] = {}


def _mime(cat: str, mimes: str) -> None:
    for m in mimes.split():
        MIME_MAP[m.lower()] = cat


_mime("video", "video/mp4 video/x-matroska video/webm video/quicktime video/x-msvideo "
              "video/x-flv video/mp2t video/mpeg video/x-ms-wmv video/3gpp video/ogg "
              "video/x-ms-asf video/vnd.dlna.mpeg-tts")
_mime("video", "application/vnd.apple.mpegurl application/x-mpegurl audio/mpegurl "
               "application/dash+xml application/vnd.ms-sstr+xml")
_mime("audio", "audio/mpeg audio/flac audio/x-flac audio/wav audio/x-wav audio/aac "
               "audio/mp4 audio/ogg audio/opus audio/webm audio/x-ms-wma audio/aiff "
               "audio/x-aiff audio/midi audio/amr audio/ac3 audio/x-ape")
_mime("app", "application/vnd.microsoft.portable-executable application/x-msdownload "
             "application/x-msi application/x-msdos-program application/x-apple-diskimage "
             "application/vnd.android.package-archive application/java-archive "
             "application/vnd.debian.binary-package application/x-rpm application/x-msi "
             "application/vnd.ms-cab-compressed")
_mime("archive", "application/zip application/x-zip-compressed application/x-rar-compressed "
                 "application/vnd.rar application/x-7z-compressed application/x-tar "
                 "application/gzip application/x-gzip application/x-bzip2 "
                 "application/x-xz application/zstd application/x-lz4 application/x-compress")
_mime("disk", "application/x-iso9660-image application/x-raw-disk-image")
_mime("document", "application/pdf application/msword "
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document "
                  "application/vnd.ms-excel "
                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet "
                  "application/vnd.ms-powerpoint "
                  "application/vnd.openxmlformats-officedocument.presentationml.presentation "
                  "application/epub+zip application/rtf text/plain text/markdown text/csv "
                  "application/json application/xml text/xml")
_mime("image", "image/jpeg image/png image/gif image/webp image/bmp image/tiff image/svg+xml "
               "image/avif image/heic image/heif image/x-icon image/vnd.adobe.photoshop")
_mime("font", "font/ttf font/otf font/woff font/woff2 application/font-woff "
              "application/x-font-ttf application/x-font-otf")
_mime("dataset", "application/x-hdf5 application/x-netcdf application/x-numpy")

# ==========================================================================
# ③ 魔数表：文件头 -> (分类, 说明)
#     按签名长度从长到短匹配，避免短的抢先命中
# ==========================================================================

MAGIC: List[Tuple[bytes, str, str]] = [
    # 视频 / 音频容器
    (b"\x1a\x45\xdf\xa3", "video", "Matroska / WebM"),
    (b"RIFF", "video", "RIFF 容器（AVI / WAV / WebP，看偏移 8）"),
    (b"OggS", "audio", "Ogg 容器"),
    (b"fLaC", "audio", "FLAC 无损音频"),
    (b"ID3", "audio", "MP3（带 ID3 标签）"),
    (b"\xff\xfb", "audio", "MP3"),
    (b"\xff\xf3", "audio", "MP3"),
    (b"\xff\xf2", "audio", "MP3"),
    (b"\xff\xf1", "audio", "AAC (ADTS)"),
    (b"\xff\xf9", "audio", "AAC (ADTS)"),
    (b"FORM", "audio", "AIFF 音频"),
    (b".snd", "audio", "AU 音频"),
    (b"#!AMR", "audio", "AMR 语音"),
    (b"Mac ", "audio", "Monkey's Audio"),
    (b"wvpk", "audio", "WavPack 无损音频"),
    # 图片
    (b"\x89PNG\r\n\x1a\n", "image", "PNG 图片"),
    (b"\xff\xd8\xff", "image", "JPEG 图片"),
    (b"GIF87a", "image", "GIF 图片"),
    (b"GIF89a", "image", "GIF 图片"),
    (b"BM", "image", "BMP 图片"),
    (b"II*\x00", "image", "TIFF 图片（小端）"),
    (b"MM\x00*", "image", "TIFF 图片（大端）"),
    (b"8BPS", "image", "Photoshop 文档"),
    (b"\x00\x00\x01\x00", "image", "ICO 图标"),
    # 应用
    (b"MZ", "app", "Windows 可执行程序 / 安装包 (PE)"),
    (b"\x7fELF", "app", "Linux 可执行程序 (ELF)"),
    (b"\xca\xfe\xba\xbe", "app", "Java class / Mach-O 胖二进制"),
    (b"\xcf\xfa\xed\xfe", "app", "macOS 可执行程序 (Mach-O)"),
    (b"\xce\xfa\xed\xfe", "app", "macOS 可执行程序 (Mach-O)"),
    (b"\xfe\xed\xfa\xce", "app", "macOS 可执行程序 (Mach-O)"),
    (b"\xed\xab\xee\xdb", "app", "RPM 安装包"),
    (b"!<arch>", "app", "ar 归档 / Debian 包"),
    (b"\x00\x00\x00\x14ftyp", "app", "MP4 家族容器（也可能是视频，看 brand）"),
    # 压缩 / 归档
    (b"PK\x03\x04", "archive", "ZIP 家族（也含 docx/xlsx/jar/apk）"),
    (b"PK\x05\x06", "archive", "空 ZIP 归档"),
    (b"PK\x07\x08", "archive", "ZIP 分段归档"),
    (b"Rar!\x1a\x07\x01\x00", "archive", "RAR5 压缩包"),
    (b"Rar!\x1a\x07\x00", "archive", "RAR 压缩包"),
    (b"7z\xbc\xaf\x27\x1c", "archive", "7-Zip 压缩包"),
    (b"\x1f\x8b", "archive", "Gzip 压缩"),
    (b"BZh", "archive", "Bzip2 压缩"),
    (b"\xfd7zXZ\x00", "archive", "XZ 压缩"),
    (b"\x28\xb5\x2f\xfd", "archive", "Zstandard 压缩"),
    (b"\x04\x22\x4d\x18", "archive", "LZ4 压缩"),
    (b"LZIP", "archive", "Lzip 压缩"),
    (b"\x1f\x9d", "archive", "Unix compress"),
    (b"MSCF", "archive", "Windows CAB 压缩包"),
    (b"\x37\x7a\xbc\xaf\x27\x1c", "archive", "7-Zip 压缩包"),
    # 光盘镜像
    (b"\x00" * 32, "disk", "全零开头，疑似 ISO/IMG 镜像（前 32KB 为保留区）"),
    # 文档 / 数据（带 BOM 的文本）
    (b"%PDF", "document", "PDF 文档"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "document", "OLE2 复合文档（老版 Office / MSI）"),
    (b"{\\rtf", "document", "RTF 文档"),
    (b"SQLite format 3\x00", "document", "SQLite 数据库"),
    (b"\xff\xfe", "document", "UTF-16 文本（小端 BOM）"),
    (b"\xfe\xff", "document", "UTF-16 文本（大端 BOM）"),
    (b"\xef\xbb\xbf", "document", "UTF-8 文本（BOM）"),
    # 字体
    (b"\x00\x01\x00\x00\x00", "font", "TrueType 字体"),
    (b"OTTO", "font", "OpenType 字体 (CFF)"),
    (b"ttcf", "font", "TrueType 字体集合"),
    (b"wOFF", "font", "WOFF 字体"),
    (b"wOF2", "font", "WOFF2 字体"),
    # 其他明确结构
    (b"-----BEGIN", "document", "PEM 证书 / 密钥"),
    (b"{\n", "document", "可能是 JSON"),
    (b"<?xml", "document", "XML 文档"),
]

# RIFF 容器需要看第 8~12 字节才知道是 AVI / WAV / WebP
RIFF_KINDS = {
    b"AVI ": ("video", "AVI 视频"),
    b"WAVE": ("audio", "WAV 音频"),
    b"WEBP": ("image", "WebP 图片"),
    b"CDDA": ("audio", "CD 音轨"),
}

# MP4 家族的 brand -> 分类
FTYP_BRANDS = {
    "isom": ("video", "MP4 视频"),
    "mp41": ("video", "MP4 视频"),
    "mp42": ("video", "MP4 视频"),
    "avc1": ("video", "MP4 视频 (H.264)"),
    "hvc1": ("video", "MP4 视频 (H.265)"),
    "hev1": ("video", "MP4 视频 (H.265)"),
    "vp09": ("video", "MP4 视频 (VP9)"),
    "av01": ("video", "MP4 视频 (AV1)"),
    "qt  ": ("video", "QuickTime 视频"),
    "M4V ": ("video", "M4V 视频"),
    "M4A ": ("audio", "M4A 音频"),
    "M4B ": ("audio", "M4B 有声书"),
    "dash": ("video", "DASH 分片"),
    "iso5": ("video", "MP4 分片 (fMP4)"),
    "iso6": ("video", "MP4 分片 (fMP4)"),
    "msdh": ("video", "DASH 分片"),
}

# ==========================================================================
# 主流平台识别（决定要不要交给 yt-dlp）
# ==========================================================================

# 国内平台域名 —— 给代理分流用。
# 这些站直连最快；套上代理反而慢、甚至被风控拦。（见 jiliu/proxy.py）
DOMESTIC_PLATFORMS = frozenset({
    "bilibili.com", "b23.tv", "acfun.cn",
    "youku.com", "iqiyi.com", "qq.com", "mgtv.com", "le.com",
    "douyin.com", "iesdouyin.com", "kuaishou.com", "ixigua.com",
    "weibo.com", "weibo.cn", "xiaohongshu.com", "xhslink.com",
    "zhihu.com", "douban.com", "tieba.baidu.com", "baidu.com",
    "huya.com", "douyu.com", "163.com", "126.net",
    "kugou.com", "kuwo.cn", "migu.cn", "pptv.com",
    "cctv.com", "cntv.cn", "people.com.cn", "xinhuanet.com",
    "jd.com", "taobao.com", "tmall.com", "alipay.com",
    "aliyuncs.com", "myqcloud.com", "bcebos.com",
    # 蓝奏云家族（域名换来换去，按词根认）。国内站，走代理反而 403/404
    "lanzou.com", "lanzoui.com", "lanzouw.com", "lanzoue.com",
    "lanzoux.com", "lanzouc.com", "lanzn.com", "lanzv.com",
    "lanpv.com", "lanrar.com", "lanosso.com", "woozooo.com",
    "dmpdmp.com", "lanosso.com", "bakstotre.com",
})

# 国别后缀：命中就当国内
DOMESTIC_SUFFIXES = (".cn", ".com.cn", ".net.cn", ".org.cn",
                     ".gov.cn", ".edu.cn", ".中国")

# 合集 / 播放列表的 URL 特征。
# 只用来「提前猜一下，好把提示语写对」——真正算不算合集，
# 以 yt-dlp 返回的 _type == 'playlist' 为准，别拿这个当判断依据。
PLAYLIST_HINTS = (
    "/channel/seriesdetail", "/channel/collectiondetail",   # B站 系列 / 合集
    "/favlist", "/medialist/play", "/list/watchlater",      # B站 收藏夹 / 稍后再看
    "/bangumi/play/ss", "/bangumi/play/ep",                 # B站 番剧整季
    "/playlist", "/playlists", "/album/", "/sets/",         # YouTube / 网易云 等
    "/videos", "/streams", "/shorts",                       # 频道投稿列表
    "/user/", "/channel/", "/c/", "/@",                     # 频道主页
)

PLATFORMS: List[Tuple[str, str]] = [
    # —— 国内 ——
    ("bilibili.com", "B站"), ("b23.tv", "B站短链"),
    ("youku.com", "优酷"), ("iqiyi.com", "爱奇艺"), ("v.qq.com", "腾讯视频"),
    ("mgtv.com", "芒果TV"), ("le.com", "乐视"),
    ("douyin.com", "抖音"), ("iesdouyin.com", "抖音"),
    ("kuaishou.com", "快手"), ("ixigua.com", "西瓜视频"),
    ("weibo.com", "微博"), ("weibo.cn", "微博"),
    ("xiaohongshu.com", "小红书"), ("xhslink.com", "小红书"),
    ("zhihu.com", "知乎"), ("douban.com", "豆瓣"),
    ("acfun.cn", "A站"), ("huya.com", "虎牙"), ("douyu.com", "斗鱼"),
    ("163.com", "网易"), ("music.163.com", "网易云音乐"),
    ("y.qq.com", "QQ音乐"), ("kugou.com", "酷狗"), ("kuwo.cn", "酷我"),
    ("migu.cn", "咪咕"), ("pptv.com", "PPTV"),
    ("cctv.com", "央视网"), ("cntv.cn", "央视网"),
    # —— 国外 ——
    ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
    ("twitter.com", "X / Twitter"), ("x.com", "X / Twitter"),
    ("instagram.com", "Instagram"), ("facebook.com", "Facebook"),
    ("fb.watch", "Facebook"), ("tiktok.com", "TikTok"),
    ("vimeo.com", "Vimeo"), ("dailymotion.com", "Dailymotion"),
    ("twitch.tv", "Twitch"), ("nicovideo.jp", "niconico"),
    ("soundcloud.com", "SoundCloud"), ("bandcamp.com", "Bandcamp"),
    ("spotify.com", "Spotify"), ("music.apple.com", "Apple Music"),
    ("reddit.com", "Reddit"), ("tumblr.com", "Tumblr"),
    ("pinterest.com", "Pinterest"), ("linkedin.com", "LinkedIn"),
    ("ted.com", "TED"), ("bbc.co.uk", "BBC"), ("cnn.com", "CNN"),
    ("archive.org", "Internet Archive"),
    ("pornhub.com", "Pornhub"), ("xvideos.com", "XVideos"),
]

# 明显不是文件下载的路径（扩展名黑名单，避免把网页当文件）
PAGE_EXTS = {"html", "htm", "php", "asp", "aspx", "jsp", "do", "action", "shtml", "cgi"}


# ==========================================================================
# 结果结构
# ==========================================================================

@dataclass
class ResourcePlan:
    """识别结果 + 针对这一类的接管策略。"""
    category: str = "other"
    label: str = "其他文件"
    filetype: str = ""
    engine: str = "multithread"
    concurrency: int = 8
    chunk_size: int = 1 << 20
    verify_hash: bool = False
    can_extract: bool = False
    can_transcode: bool = False
    min_multithread: int = 1 << 20
    needs_extractor: bool = False
    platform: str = ""
    filename: str = ""
    ext: str = ""
    mime: str = ""
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    note: str = ""

    # —— 协议层（磁力 / 种子 / ed2k / 迅雷壳 / 网盘）——
    protocol: str = "http"          # 见 protocols.Protocol.kind
    protocol_label: str = ""
    resolved_url: str = ""          # 解壳后的真实地址；与 url 不同时才非空
    p2p: bool = False               # 是否需要 BT 引擎
    info_hash: str = ""
    trackers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        bits = [f"{self.label}", self.filetype or self.ext or "未知"]
        if self.platform:
            bits.append(f"站点={self.platform}")
        if self.protocol not in ("http", ""):
            bits.append(f"协议={self.protocol_label or self.protocol}")
        bits.append(f"引擎={self.engine}")
        if self.p2p:
            bits.append("P2P")
        elif self.concurrency:
            bits.append(f"{self.concurrency}线程/{human_size(self.chunk_size)}分片")
        if self.verify_hash:
            bits.append("校验哈希")
        return " | ".join(bits)


def human_size(n: Optional[float]) -> str:
    if not n:
        return "0 B"
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < step:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} PB"


# ==========================================================================
# 从响应头 / URL 里把线索挖出来
# ==========================================================================

_CD_STAR_RE = re.compile(r"filename\*\s*=\s*([^']*)''([^;]+)", re.I)
_CD_RE = re.compile(r'filename\s*=\s*"([^"]+)"', re.I)
_CD_RE2 = re.compile(r"filename\s*=\s*([^;]+)", re.I)


def filename_from_content_disposition(cd: str) -> str:
    """解析 Content-Disposition，优先 RFC 5987 的 filename*（含非 ASCII 名）。"""
    if not cd:
        return ""
    m = _CD_STAR_RE.search(cd)
    if m:
        raw = m.group(2).strip().strip('"')
        try:
            return urllib.parse.unquote(raw, encoding="utf-8", errors="replace")
        except Exception:
            return raw
    for pat in (_CD_RE, _CD_RE2):
        m = pat.search(cd)
        if m:
            raw = m.group(1).strip().strip('"').strip("'")
            try:
                return urllib.parse.unquote(raw)
            except Exception:
                return raw
    return ""


def filename_from_url(url: str) -> str:
    try:
        path = urllib.parse.urlparse(url).path
        name = os.path.basename(path)
        return urllib.parse.unquote(name)
    except Exception:
        return ""


def ext_of(name: str) -> str:
    """取扩展名。

    长度上限放到 12 是为了容下 safetensors / crdownload 这类真后缀；
    再长基本就是 URL slug 里的点了，当不了类型线索。
    """
    if not name:
        return ""
    base = name.split("?", 1)[0]
    _, dot, ext = base.rpartition(".")
    if not dot or len(ext) > 12 or not ext.isalnum():
        return ""
    return ext.lower()


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).netloc or "").lower().split(":")[0]
    except Exception:
        return ""


def match_platform(url_or_host: str) -> str:
    """命中已知平台就返回平台名，否则空串。"""
    h = url_or_host.lower()
    if "//" in h or "/" in h:
        h = host_of(url_or_host)
    if not h:
        return ""
    for suffix, name in PLATFORMS:
        if h == suffix or h.endswith("." + suffix):
            return name
    return ""


def is_page(url: str) -> bool:
    """看着像网页而不是文件（扩展名是 html/php 这类，或者压根没扩展名）。"""
    ext = ext_of(filename_from_url(url))
    if ext in PAGE_EXTS:
        return True
    return False


# ==========================================================================
# ④ 魔数嗅探
# ==========================================================================

SNIFF_BYTES = 512


def sniff(head: bytes) -> Tuple[str, str, str]:
    """看文件头判断类型。返回 (分类, 说明, 扩展名猜测)。

    拿不到明确结论时返回 ("", "", "")。
    """
    if not head:
        return "", "", ""

    # PK 开头要再分一层：docx/xlsx/apk/jar 都是 zip
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return _sniff_zip(head)

    if head[:4] == b"RIFF" and len(head) >= 12:
        kind = RIFF_KINDS.get(head[8:12])
        if kind:
            return kind[0], kind[1], {"AVI ": "avi", "WAVE": "wav", "WEBP": "webp"}.get(
                head[8:12].decode("latin1"), "")
        return "other", "RIFF 容器（未知子类型）", ""

    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12].decode("latin1", "replace")
        hit = FTYP_BRANDS.get(brand)
        if hit:
            return hit[0], hit[1], {"audio": "m4a", "video": "mp4"}[hit[0]]
        return "video", f"MP4 家族容器 (brand={brand})", "mp4"

    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        # OLE2：可能是老 doc/xls/ppt，也可能是 MSI
        if b"Msi" in head[:512] or b"Microsoft" in head[:512]:
            return "app", "Windows 安装包 (MSI，OLE2 结构)", "msi"
        return "document", "OLE2 复合文档（老版 Office）", "doc"

    for sig, cat, desc in MAGIC:
        if head.startswith(sig):
            return cat, desc, _ext_hint(cat, sig)

    # 兜底：看是不是可读文本
    sample = head[:256]
    if sample and sum(1 for b in sample if 9 <= b <= 13 or 32 <= b <= 126) / len(sample) > 0.92:
        s = sample.lstrip()
        if s[:1] in (b"{", b"["):
            return "document", "纯文本（JSON 结构）", "json"
        if b"<html" in head[:512].lower() or b"<!doctype html" in head[:512].lower():
            return "document", "HTML 网页", "html"
        return "document", "纯文本", "txt"
    return "", "", ""


def _ext_hint(cat: str, sig: bytes) -> str:
    table = {
        b"\x1a\x45\xdf\xa3": "mkv", b"OggS": "ogg", b"fLaC": "flac", b"ID3": "mp3",
        b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"GIF87a": "gif",
        b"GIF89a": "gif", b"BM": "bmp", b"II*\x00": "tif", b"MM\x00*": "tif",
        b"8BPS": "psd", b"\x00\x00\x01\x00": "ico",
        b"MZ": "exe", b"\x7fELF": "elf", b"\xed\xab\xee\xdb": "rpm",
        b"!<arch>": "deb", b"Rar!\x1a\x07\x01\x00": "rar", b"Rar!\x1a\x07\x00": "rar",
        b"7z\xbc\xaf\x27\x1c": "7z", b"\x1f\x8b": "gz", b"BZh": "bz2",
        b"\xfd7zXZ\x00": "xz", b"\x28\xb5\x2f\xfd": "zst", b"\x04\x22\x4d\x18": "lz4",
        b"LZIP": "lz", b"\x1f\x9d": "Z", b"MSCF": "cab",
        b"%PDF": "pdf", b"{\\rtf": "rtf", b"SQLite format 3\x00": "sqlite",
        b"-----BEGIN": "pem", b"wOFF": "woff", b"wOF2": "woff2", b"OTTO": "otf",
        b"ttcf": "ttc", b"\x00\x01\x00\x00\x00": "ttf", b"#!AMR": "amr",
        b"FORM": "aiff", b".snd": "au", b"wvpk": "wv", b"Mac ": "ape",
    }
    if sig in table:
        return table[sig]
    return {"image": "img", "font": "ttf", "audio": "audio", "video": "video"}.get(cat, "")


def _zip_entry_names(head: bytes, limit: int = 24) -> str:
    """把 ZIP 里能看到的条目名拼成一串小写文本。

    注意要扫**全部** local file header，不能只看第一个 —— MSIX 的
    [Content_Types].xml 通常在第二个，只读首个就会漏判成普通 zip。
    """
    names = []
    pos = 0
    while len(names) < limit:
        idx = head.find(b"PK\x03\x04", pos)
        if idx < 0 or idx + 30 > len(head):
            break
        name_len = int.from_bytes(head[idx + 26:idx + 28], "little")
        start = idx + 30
        raw = head[start:start + name_len]
        if raw:
            names.append(raw.decode("utf-8", "replace"))
        # 跳过文件数据：至少往前挪 30+namelen，否则同一处会反复命中
        pos = max(start + name_len, idx + 4)
        if len(names) >= limit:
            break
    return "\n".join(names).lower()


def _sniff_zip(head: bytes) -> Tuple[str, str, str]:
    """ZIP 家族再分一层：看内部条目名。"""
    inner = _zip_entry_names(head)
    rules = [
        ("appxmanifest.xml", ("app", "Windows 应用包 (MSIX/APPX)", "msix")),
        ("content_types.xml", ("app", "Windows 应用包 (MSIX/APPX)", "msix")),
        ("androidmanifest.xml", ("app", "Android 应用包 (APK)", "apk")),
        ("meta-inf/manifest.mf", ("app", "Java 程序包 (JAR)", "jar")),
        ("word/", ("document", "Word 文档 (docx)", "docx")),
        ("xl/", ("document", "Excel 表格 (xlsx)", "xlsx")),
        ("ppt/", ("document", "PowerPoint 演示 (pptx)", "pptx")),
        ("mimetype", ("document", "EPUB 电子书（疑似）", "epub")),
    ]
    for pat, hit in rules:
        if pat in inner:
            return hit
    return "archive", "ZIP 压缩包", "zip"


# ==========================================================================
# 主入口：识别 + 出策略
# ==========================================================================

def _apply_protocol(plan: ResourcePlan, proto: protocols.Protocol,
                    reasons: List[str], warnings: List[str]) -> bool:
    """把协议识别结果落到 plan 上。

    返回 True 表示「这条链接的取件方式已经定了，不必再看文件线索」。
    返回 False 表示只是个套壳，壳里是普通地址，接着按文件线索识别。

    为什么要分这两条路：`magnet:` / `ed2k:` 根本不是「文件」，谈不上后缀和 MIME；
    而 `thunder://` 只是个 base64 壳，剥掉之后里面就是一个普通 URL，
    该走原来的四路线索流程，不能因为「它长得怪」就归到 other 去。
    """
    plan.protocol = proto.kind
    plan.protocol_label = proto.label
    plan.p2p = proto.p2p
    plan.info_hash = proto.info_hash
    plan.trackers = list(proto.trackers or [])
    if proto.url and proto.url != proto.original:
        plan.resolved_url = proto.url

    if proto.kind != "http":
        reasons.append(f"协议识别：{proto.label}")
    if proto.note and proto.kind != "http":
        reasons.append(proto.note)
    if proto.error:
        warnings.append(proto.error)

    def _adopt(cat_key: str, confidence: float) -> None:
        cat = CATEGORIES[cat_key]
        plan.category = cat_key
        plan.label = cat["label"]
        plan.engine = cat["engine"]
        plan.concurrency = cat["concurrency"]
        plan.chunk_size = cat["chunk_size"]
        plan.verify_hash = cat["verify_hash"]
        plan.can_extract = cat["can_extract"]
        plan.can_transcode = cat["can_transcode"]
        plan.min_multithread = cat["min_multithread"]
        plan.note = cat["note"]
        plan.filetype = proto.label
        plan.confidence = confidence

    # ---- 磁力 / 种子 / ed2k：只有 aria2 能接 ----
    if proto.kind in ("magnet", "torrent", "ed2k") or (proto.p2p and proto.kind != "http"):
        _adopt("bt", 0.95)
        if proto.name:
            plan.filename = proto.name
            plan.ext = ext_of(proto.name)
        if proto.kind == "magnet" and not proto.info_hash:
            warnings.append("磁力链接里没有 info hash，定位不到资源，多半是链接被截断了")
        warnings.append("BT 速度取决于别人愿不愿意传，不由本地带宽决定；"
                        "冷门种子可能长时间零速")
        return True

    # ---- 网盘：认得出，但要诚实 ----
    if proto.kind == "netdisk":
        _adopt("netdisk", 0.9)
        return True

    # ---- 迅雷 / 旋风 / 快车：壳已剥，里面是什么就按什么走 ----
    #      （壳里裹磁力的那种已在上面被 p2p 分支截住了）
    if proto.kind in ("thunder", "qqdl", "flashget"):
        if not proto.url:
            warnings.append(f"{proto.label}解壳失败，内容可能被截断")
            _adopt("other", 0.2)
            return True
        reasons.append(f"{proto.label}已解开，按里面的地址继续识别")
        return False

    return False


def classify(*, url: str = "", filename: str = "", mime: str = "",
             content_disposition: str = "", magic: bytes = b"",
             size: Optional[int] = None) -> ResourcePlan:
    """把各路线索汇总，给出分类与接管策略。

    线索越多越准。最少给 url 也能出个大概结论。
    """
    plan = ResourcePlan()
    reasons: List[str] = []
    warnings: List[str] = []

    # —— ⓪ 协议层：先看这是不是「另一种取件方式」——
    #    磁力/种子/ed2k 走 aria2，迅雷壳剥开、网盘如实告知。
    #    放在最前面，是因为这些链接的「后缀」和「MIME」都不存在，
    #    让它们进后面的流程只会得到一堆无意义的判断。
    proto = protocols.detect(url) if url else protocols.Protocol()
    if _apply_protocol(plan, proto, reasons, warnings):
        plan.reasons = reasons
        plan.warnings = warnings
        return plan
    # 套壳已剥开：后面一律用真实地址来判断
    if proto.url and proto.url != url:
        url = proto.url
        plan.resolved_url = url

    # —— 收集候选文件名 ——
    cd_name = filename_from_content_disposition(content_disposition) if content_disposition else ""
    if cd_name:
        reasons.append(f"响应头声明文件名「{cd_name}」")
        plan.confidence += 0.35
    url_name = filename_from_url(url)
    name = filename or cd_name or url_name
    plan.filename = proto.name or name
    plan.ext = ext_of(plan.filename)
    plan.mime = (mime or "").split(";")[0].strip().lower()

    hits: Dict[str, float] = {}
    magic_desc = ""

    def vote(cat: str, weight: float, why: str) -> None:
        hits[cat] = hits.get(cat, 0.0) + weight
        reasons.append(why)

    # —— ③ URL 后缀 ——
    if plan.ext:
        hit = EXT_MAP.get(plan.ext)
        if hit:
            vote(hit[0], 0.5, f"URL/文件名后缀 .{plan.ext} → {hit[1]}")
    elif url and not is_page(url):
        reasons.append("URL 无扩展名，靠其他线索判断")

    # —— ② MIME ——
    if plan.mime:
        mcat = MIME_MAP.get(plan.mime)
        if mcat:
            vote(mcat, 0.6, f"Content-Type {plan.mime} → {CATEGORIES[mcat]['label']}")
        elif plan.mime.startswith("text/html"):
            reasons.append("Content-Type 是网页，多半不是直接文件")
            plan.confidence -= 0.1
        elif plan.mime.startswith(("video/", "audio/")):
            vote(plan.mime.split("/")[0], 0.5, f"Content-Type 大类 {plan.mime}")
        elif plan.mime in ("application/octet-stream", "binary/octet-stream"):
            reasons.append("Content-Type 是通用二进制，服务端没说清")

    # —— ④ 魔数（最硬的证据）——
    if magic:
        scat, sdesc, sext = sniff(magic)
        if scat:
            magic_desc = sdesc
            vote(scat, 1.0, f"文件头魔数 → {sdesc}")
            if sext and (not plan.ext or plan.ext in GENERIC_EXTS):
                # URL 写着 .bin/.dat 或者干脆没后缀时，以文件头为准
                if plan.ext and plan.ext != sext:
                    reasons.append(f"URL 后缀 .{plan.ext} 是占位符，按文件头改判为 .{sext}")
                plan.ext = sext
        else:
            reasons.append("文件头不在已知魔数表里")

    # —— 落地分类 ——
    if hits:
        plan.category = max(hits.items(), key=lambda kv: kv[1])[0]
        total = sum(hits.values())
        plan.confidence = min(0.99, plan.confidence + min(0.6, total / 2.2))
    else:
        plan.category = "other"
        plan.confidence = max(0.15, plan.confidence)

    cat = CATEGORIES[plan.category]
    plan.label = cat["label"]
    plan.filetype = EXT_MAP.get(plan.ext, ("", ""))[1] or _desc_from_reasons(reasons) or ""
    plan.engine = cat["engine"]
    plan.concurrency = cat["concurrency"]
    plan.chunk_size = cat["chunk_size"]
    plan.verify_hash = cat["verify_hash"]
    plan.can_extract = cat["can_extract"]
    plan.can_transcode = cat["can_transcode"]
    plan.min_multithread = cat["min_multithread"]
    plan.note = cat["note"]

    # —— 平台识别 ——
    plan.platform = match_platform(url)
    if plan.platform:
        reasons.append(f"命中已知平台：{plan.platform}")

    # —— 需不需要 yt-dlp 介入 ——
    if plan.ext in MANIFEST_EXTS:
        plan.needs_extractor = True
        reasons.append(f"是流媒体清单（.{plan.ext}），必须交给 yt-dlp 合流")
    elif plan.platform:
        if _looks_like_static_file(plan):
            # 视频站域名下也可能直接挂着一个 .png / .zip 的静态资源，
            # 有明确文件后缀就以文件为准，别硬塞给 yt-dlp。
            reasons.append(
                f"{plan.platform} 的域名，但 URL 带 .{plan.ext} 后缀，按直链处理"
                "（若是页面地址请改用视频解析）"
            )
        else:
            plan.needs_extractor = True
            reasons.append(f"{plan.platform} 的页面地址需要 yt-dlp 解析真实媒体地址")
    if plan.needs_extractor:
        plan.engine = "ytdlp"
        if plan.category in ("video", "other"):
            plan.label = "视频"
        if plan.category == "other":
            plan.category = "video"

    # —— 分类专属告警 ——
    if plan.category == "app":
        warnings.append("安装包请确认来源可信；Windows 可执行文件建议核对数字签名")
        if plan.verify_hash:
            warnings.append("下完先对哈希再运行，别急着双击")
    if plan.category == "archive":
        if plan.ext in ("zipx", "r00"):
            warnings.append("可能是分卷压缩包的一部分，需要所有分卷在同一目录才能解压")
        warnings.append("解压前先看清单，别解出个炸弹（压缩包嵌套炸弹）")
    if plan.category == "disk":
        warnings.append("镜像很大，注意磁盘余量与系统盘空间")
    if plan.mime.startswith("text/html") or "网页" in magic_desc or "HTML" in magic_desc:
        warnings.append("目标像是网页而不是文件，建议改用视频解析模式")
    if plan.category == "other" and not plan.ext:
        warnings.append("类型未能确定，将按通用策略下载；下完可手动核对文件头")

    # —— 体积相关建议 ——
    if size:
        if size < plan.min_multithread:
            plan.concurrency = 1
            reasons.append(f"体积仅 {human_size(size)}，低于多线程门槛 "
                           f"{human_size(plan.min_multithread)}，改单线程直下更划算")
        elif size < 8 << 20 and plan.concurrency > 4:
            plan.concurrency = max(4, plan.concurrency - 4)
            reasons.append(f"体积 {human_size(size)} 不大，线程数降到 {plan.concurrency} 减少开销")

    plan.reasons = reasons
    plan.warnings = warnings
    return plan


def _desc_from_reasons(reasons: List[str]) -> str:
    for r in reasons:
        if "→" in r:
            return r.split("→", 1)[1].strip()
    return ""


# 「一眼就是个静态文件」的后缀：视频站域名下也可能是这种东西，
# 这时不该因为域名而改走 yt-dlp。
STATIC_FILE_CATEGORIES = {"image", "font", "archive", "app", "document",
                          "dataset", "disk", "audio"}
PAGE_LIKE_EXTS = {"html", "htm", "php", "asp", "aspx", "jsp", "shtml", "do", "cgi"}


def _looks_like_static_file(plan: ResourcePlan) -> bool:
    if not plan.ext or plan.ext in PAGE_LIKE_EXTS or plan.ext in MANIFEST_EXTS:
        return False
    return plan.category in STATIC_FILE_CATEGORIES


# ==========================================================================
# 给浏览器插件的接管决策
# ==========================================================================

# 这些协议/地址永远不接管
SKIP_SCHEMES = ("blob:", "data:", "about:", "javascript:", "chrome:", "edge:",
                "moz-extension:", "chrome-extension:", "filesystem:")

# 太小的东西不值得转手，浏览器自己下更快
PASS_THROUGH_MAX = 1 << 20        # 1 MiB 以下的小图片/字体直接放行

# 不确定类型时，超过这个体积才值得接管
TAKE_OVER_MIN = 8 << 20           # 8 MiB


def intercept_decision(url: str, *, mime: str = "", filename: str = "",
                       content_disposition: str = "", size: Optional[int] = None,
                       category_hint: str = "") -> Dict[str, Any]:
    """插件拿到下载事件后问一句：这个我要不要接过来？

    返回 {take: bool, reason: str, plan: {...}}。

    设计取向是「别烦人」—— 小文件、已在汲流里的、明显是网页的，
    都放给浏览器自己处理。
    """
    low = (url or "").lower()
    for s in SKIP_SCHEMES:
        if low.startswith(s):
            return {"take": False, "reason": f"协议 {s} 不支持接管", "plan": None}

    # 磁力 / ed2k / 迅雷壳 / 迅雷种 —— 浏览器自己完全处理不了，
    # 不看体积直接接过来，没有「让浏览器自己下」这个选项。
    if low.startswith(("magnet:", "ed2k:")) or any(
            k in low[:200] for k in ("thunder://", "qqdl://", "flashget://")):
        plan = classify(url=url)
        return {"take": True, "reason": f"识别为{plan.label}，交给汲流下载",
                "plan": plan.to_dict()}

    if category_hint == "video-stream":
        return {"take": True, "reason": "网络层捕获到媒体流地址", "plan": None}

    plan = classify(url=url, filename=filename, mime=mime,
                    content_disposition=content_disposition, size=size)

    if plan.needs_extractor:
        return {"take": True, "reason": f"需要 yt-dlp 解析（{plan.platform or plan.ext}）",
                "plan": plan.to_dict()}

    # 小文件放行
    if size is not None and size < PASS_THROUGH_MAX and plan.category in (
            "image", "font", "document", "other"):
        return {"take": False,
                "reason": f"{human_size(size)} 的 {plan.label}，浏览器自己下更快",
                "plan": plan.to_dict()}

    if plan.category in ("video", "audio", "app", "archive", "disk", "dataset", "bt"):
        return {"take": True, "reason": f"识别为{plan.label}（{plan.filetype or plan.ext}）",
                "plan": plan.to_dict()}

    if size is not None and size >= TAKE_OVER_MIN:
        return {"take": True, "reason": f"{human_size(size)} 且类型未明确，转交多线程下载",
                "plan": plan.to_dict()}

    if plan.confidence < 0.4:
        return {"take": False, "reason": "类型不明确且体积不大，交给浏览器", "plan": plan.to_dict()}

    return {"take": True, "reason": f"识别为{plan.label}", "plan": plan.to_dict()}


def classify_probe(pr: Any) -> ResourcePlan:
    """直接吃一个 engine.ProbeResult（鸭子类型，避免循环依赖）。

    探测一次就把四路线索全拿到了：Content-Type、Content-Disposition、
    URL 后缀、前 1KB 文件头。这是最准的调用方式。
    """
    return classify(
        url=getattr(pr, "final_url", "") or getattr(pr, "url", ""),
        filename=getattr(pr, "filename", "") or "",
        mime=getattr(pr, "content_type", "") or "",
        content_disposition=getattr(pr, "content_disposition", "") or "",
        magic=getattr(pr, "magic", b"") or b"",
        size=getattr(pr, "size", None),
    )


def suggest_filename(plan: ResourcePlan, current: str) -> str:
    """探测出的文件名太糊（download.bin 之类）时，用识别结果补个像样的后缀。"""
    if not plan.ext:
        return current
    base = (current or "").strip()
    if not base:
        return f"{plan.category}{'.' + plan.ext if plan.ext else ''}"
    stem, _dot, ext = base.rpartition(".")
    have = ext.lower() if _dot else ""
    if have and have not in GENERIC_EXTS and have != "download":
        return base          # 已有明确后缀，尊重它
    stem = stem if _dot else base
    return f"{stem}.{plan.ext}"


def describe_categories() -> List[Dict[str, Any]]:
    """给面板/CLI 用的分类总览。"""
    out = []
    for key, c in CATEGORIES.items():
        out.append({
            "key": key,
            "label": c["label"],
            "engine": c["engine"],
            "concurrency": c["concurrency"],
            "chunk": human_size(c["chunk_size"]),
            "verify_hash": c["verify_hash"],
            "can_extract": c["can_extract"],
            "min_multithread": human_size(c["min_multithread"]),
            "note": c["note"],
            "exts": sum(1 for _e, (cc, _d) in EXT_MAP.items() if cc == key),
        })
    return out
