# 解析插件示例

把 `example_site.py` 复制到**配置目录**下的 `plugins/` 里（路径见设置页「插件」）；
windows 一般是 `%USERPROFILE%\.jiliu\plugins\`，然后在设置页点「重新加载」。

## 为什么放用户目录

站点的破法三天一变。解法焊在 exe 里就得重新发版；放在用户目录里，
改一行存盘、点重新加载就生效。

## 接口

| 名字 | 必需 | 说明 |
|---|---|---|
| `NAME` | 否 | 显示名，缺省用文件名 |
| `VERSION` | 否 | 版本号，显示用 |
| `DESCRIPTION` | 否 | 一句话说明 |
| `DOMAINS` | 否 | 只对这些域名生效；留空 = 全都问（慢） |
| `match(url) -> bool` | 否 | 快速筛，返回 False 就不再调 resolve |
| `resolve(url, cfg) -> dict/None` | 是 | 认领链接并给出解析结果 |
| `on_event(event, task)` | 否 | 收 `created/started/done/error/cancelled` |

`resolve` 返回的字典可以带：`url`（直链，必填）、`urls`（镜像）、`filename`、
`headers`、`meta`、`kind`（`http`/`video`）、`note`、`tasks`（额外产物列表）。

## 三条纪律

1. **有超时**：`resolve` 超过 8 秒会被打断并记为超时，主程序继续。别在里面
   做长时间轮询。
2. **异常被吞**：抛异常只记在该插件名下（设置页能看到最后一次错误），
   不会影响下载任务，也不影响别的插件。
3. **别做坏事**：插件和主程序同权限运行，能读写文件、联网。只装你信得过的。
