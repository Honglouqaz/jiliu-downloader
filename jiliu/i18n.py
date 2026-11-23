"""界面语言（i18n）。

    from jiliu import i18n
    i18n.t("queued", "en-US")        # "Queued"
    i18n.bundle("en-US")             # 整包，一次发给前端

为什么不是 gettext：汲流是单文件打包的桌面程序，没有 .mo 的落脚处，
而字符串量级也就几百条 —— 一张表比一套工具链便宜得多。

键的命名规则：`区` + `.` + `语义`，例如 `queue.default`、`update.checking`。
找不到的键**原样返回键名**，不会变成空字符串：界面上看见 "update.checking"
就知道该补哪一条，比看见一片空白好查。
"""
from __future__ import annotations

from typing import Any, Dict, List

DEFAULT_LANG = "zh-CN"

LANGS: List[Dict[str, str]] = [
    {"code": "zh-CN", "name": "简体中文", "short": "中"},
    {"code": "en-US", "name": "English", "short": "EN"},
]

# 键 → {语言: 文案}
STRINGS: Dict[str, Dict[str, str]] = {
    # —— 通用 ——
    "ok": {"zh-CN": "完成", "en-US": "Done"},
    "cancel": {"zh-CN": "取消", "en-US": "Cancel"},
    "save": {"zh-CN": "保存", "en-US": "Save"},
    "reload": {"zh-CN": "重新加载", "en-US": "Reload"},
    "copy": {"zh-CN": "复制", "en-US": "Copy"},
    "none": {"zh-CN": "无", "en-US": "None"},
    "unknown": {"zh-CN": "未知", "en-US": "Unknown"},
    "failed": {"zh-CN": "失败", "en-US": "Failed"},

    # —— 任务状态 ——
    "state.queued": {"zh-CN": "排队中", "en-US": "Queued"},
    "state.running": {"zh-CN": "下载中", "en-US": "Downloading"},
    "state.paused": {"zh-CN": "已暂停", "en-US": "Paused"},
    "state.done": {"zh-CN": "已完成", "en-US": "Completed"},
    "state.error": {"zh-CN": "失败", "en-US": "Error"},
    "state.cancelled": {"zh-CN": "已取消", "en-US": "Cancelled"},
    "state.live": {"zh-CN": "录制中", "en-US": "Recording"},

    # —— 队列 ——
    "queue.title": {"zh-CN": "队列", "en-US": "Queues"},
    "queue.default": {"zh-CN": "默认", "en-US": "Default"},
    "queue.video": {"zh-CN": "视频", "en-US": "Video"},
    "queue.slots": {"zh-CN": "并发", "en-US": "Slots"},
    "queue.limit": {"zh-CN": "限速", "en-US": "Limit"},
    "queue.priority": {"zh-CN": "优先级", "en-US": "Priority"},

    # —— 批量 ——
    "batch.title": {"zh-CN": "批量粘贴", "en-US": "Bulk paste"},
    "batch.probe": {"zh-CN": "预探测", "en-US": "Preflight"},
    "batch.expand": {"zh-CN": "展开通配符", "en-US": "Expand wildcards"},
    "batch.found": {"zh-CN": "识别到 {n} 条链接", "en-US": "{n} links found"},

    # —— 校验 ——
    "checksum.title": {"zh-CN": "校验和", "en-US": "Checksums"},
    "checksum.match": {"zh-CN": "哈希一致", "en-US": "Checksum matches"},
    "checksum.mismatch": {"zh-CN": "哈希不一致", "en-US": "Checksum mismatch"},

    # —— 更新 ——
    "update.title": {"zh-CN": "检查更新", "en-US": "Check for updates"},
    "update.checking": {"zh-CN": "正在检查更新…", "en-US": "Checking for updates…"},
    "update.latest": {"zh-CN": "已是最新版本", "en-US": "You're up to date"},
    "update.available": {"zh-CN": "发现新版本 {v}", "en-US": "Version {v} available"},
    "update.download": {"zh-CN": "下载更新", "en-US": "Download update"},
    "update.ready": {"zh-CN": "更新已就绪，重启后生效", "en-US": "Update ready — restart to apply"},

    # —— 系统集成 ——
    "shell.title": {"zh-CN": "右键菜单", "en-US": "Context menu"},
    "shell.register": {"zh-CN": "注册到系统右键菜单", "en-US": "Add to context menu"},
    "drop.title": {"zh-CN": "拖拽悬浮窗", "en-US": "Drop window"},

    # —— 完成动作 ——
    "finish.title": {"zh-CN": "全部完成后", "en-US": "When all finished"},
    "finish.none": {"zh-CN": "不做任何事", "en-US": "Do nothing"},
    "finish.shutdown": {"zh-CN": "关机", "en-US": "Shut down"},
    "finish.hibernate": {"zh-CN": "休眠", "en-US": "Hibernate"},
    "finish.sleep": {"zh-CN": "睡眠", "en-US": "Sleep"},
    "finish.lock": {"zh-CN": "锁定屏幕", "en-US": "Lock screen"},
    "finish.exit": {"zh-CN": "退出汲流", "en-US": "Quit Jiliu"},

    # —— 弹幕 / 直播 ——
    "danmaku.title": {"zh-CN": "下载弹幕", "en-US": "Download danmaku"},
    "live.title": {"zh-CN": "直播录制", "en-US": "Live recording"},
    "live.recording": {"zh-CN": "录制中，已收 {n} 片", "en-US": "Recording, {n} segments"},

    # —— 导入导出 ——
    "transfer.export": {"zh-CN": "导出任务", "en-US": "Export tasks"},
    "transfer.import": {"zh-CN": "导入任务", "en-US": "Import tasks"},
    "transfer.json": {"zh-CN": "JSON（完整，可迁移）", "en-US": "JSON (full)"},
    "transfer.aria2": {"zh-CN": "aria2 输入文件", "en-US": "aria2 input file"},
    "transfer.txt": {"zh-CN": "纯文本链接列表", "en-US": "Plain URL list"},

    # —— 插件 ——
    "plugin.title": {"zh-CN": "解析插件", "en-US": "Resolver plugins"},
    "plugin.dir": {"zh-CN": "插件目录", "en-US": "Plugin folder"},
    "plugin.none": {"zh-CN": "还没有插件", "en-US": "No plugins yet"},
    "plugin.error": {"zh-CN": "加载失败", "en-US": "Failed to load"},

    # —— 主题 ——
    "theme.title": {"zh-CN": "主题", "en-US": "Theme"},
    "theme.auto": {"zh-CN": "跟随系统", "en-US": "System"},
    "theme.light": {"zh-CN": "浅色", "en-US": "Light"},
    "theme.dark": {"zh-CN": "深色", "en-US": "Dark"},
    "lang.title": {"zh-CN": "界面语言", "en-US": "Language"},
}


def normalize(lang: str) -> str:
    """把 `en`、`en-us`、`zh_CN` 这类写法归一到表里的 code。"""
    s = str(lang or "").strip().replace("_", "-").lower()
    if not s:
        return DEFAULT_LANG
    for item in LANGS:
        if s == item["code"].lower():
            return item["code"]
    head = s.split("-")[0]
    for item in LANGS:
        if item["code"].lower().split("-")[0] == head:
            return item["code"]
    return DEFAULT_LANG


def t(key: str, lang: str = "", **fmt: Any) -> str:
    """取一条文案。缺键返回键名；`{n}` 这类占位符按 fmt 填。"""
    code = normalize(lang)
    row = STRINGS.get(key)
    if not row:
        return key
    s = row.get(code) or row.get(DEFAULT_LANG) or key
    if fmt:
        for k, v in fmt.items():
            s = s.replace("{" + k + "}", str(v))
    return s


def bundle(lang: str = "", prefix: str = "") -> Dict[str, str]:
    """整包文案（可只取某个前缀，如 "queue."），发给前端用。"""
    code = normalize(lang)
    out: Dict[str, str] = {}
    for k, row in STRINGS.items():
        if prefix and not k.startswith(prefix):
            continue
        out[k] = row.get(code) or row.get(DEFAULT_LANG) or k
    return out


def languages() -> List[Dict[str, str]]:
    return list(LANGS)


def describe(cfg: Dict[str, Any]) -> Dict[str, Any]:
    cur = normalize(str((cfg or {}).get("ui_lang") or DEFAULT_LANG))
    return {
        "current": cur,
        "languages": languages(),
        "keys": len(STRINGS),
        "note": "缺键会原样显示键名，方便发现没翻译的地方",
    }
