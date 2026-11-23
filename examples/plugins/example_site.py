NAME = "示例站点（把这个文件复制到配置目录/plugins/ 下再改）"
VERSION = "1.0"
DESCRIPTION = "演示插件接口：把 /page/<id> 解析成一个直链"

# 留空 = 所有链接都来问一遍（慢，不推荐）
# 填了就只有这些域名会被问
DOMAINS = ["example.com"]

import re

_ID = re.compile(r"/page/(\w+)")


def match(url: str) -> bool:
    """快速筛。返回 False 就不会再调 resolve。"""
    return bool(_ID.search(url))


def resolve(url: str, cfg: dict):
    """返回 None = 不认领；返回 dict = 认领。

    dict 可用字段：
        url      直链（必填）
        urls     其他镜像（会并入 meta["mirrors"]）
        filename 建议文件名
        headers  请求头（直链常要 Referer 才给下）
        meta     任意附加信息，会原样挂到任务上
        kind     "http"（默认）或 "video"
        note     给用户看的一句话
        tasks    额外产物 [{"url":..., "filename":...}, ...]
    """
    m = _ID.search(url)
    if not m:
        return None
    pid = m.group(1)
    return {
        "url": f"https://cdn.example.com/files/{pid}.mp4",
        "filename": f"{pid}.mp4",
        "headers": {"Referer": url},
        "meta": {"page_id": pid},
        "note": "由示例插件解析",
    }


def on_event(event: str, task) -> None:
    """created / started / done / error / cancelled。

    别在这里做耗时操作 —— 它跑在下载线程里。
    """
    if event == "done":
        pass
