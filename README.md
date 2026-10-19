# 汲流 Jiliu

自建多线程下载器。四件事做到底：**认清是什么** → **用对的方式下** →
**一个界面管全部** → **浏览器里替你接管**。

```
汲流  v1.1.0
多线程分片 · 类型识别 · 统一下载协议 · 合集下载 · 下载接管 · 原生桌面窗口
多引擎（自研/aria2/curl/wget/yt-dlp/ffmpeg） · 自动分类归档 · 系统代理跟随
国内版 / 全球版双形态
```

双击 `jiliu.exe` 出来的是**一个原生窗口**（pywebview + Edge WebView2），
不弹终端、不开浏览器地址栏。想要老样子（浏览器面板）用 `jiliu.exe serve`。

---

## 它解决什么问题

浏览器自带的下载器只会做一件事：**从第一个字节顺序拉到最后一个字节**。
于是你会遇到这些：

| 场景 | 浏览器的做法 | 汲流的做法 |
|---|---|---|
| 2GB 的 ISO | 单线程，断了从头来 | 8 线程分片，断点续传，下完自动校验 SHA256 |
| 40KB 的图标 | 也走完整下载流程 | 认出是图片，单线程直下，不折腾 |
| `.m3u8` 直播流 | 拉下来一堆碎片，毫无用处 | 认出是流清单，转交 yt-dlp 合流成 mp4 |
| B站 / YouTube 视频 | 下不到，或下到无声视频 | 交给 yt-dlp，音视频分离下载再合并 |
| 没后缀的 CDN 链接 | 存成 `download.bin` | 读文件头魔数认出真实类型，补上正确后缀 |
| `.zip` 伪装成图片 | 存下来说打不开 | 魔数投票推翻后缀，判定为压缩包 |
| 应用安装包 | 下坏了才发现，装到一半报错 | 认出是安装包，强制下完校验哈希 |
| B站合集 / 油管播放列表 | 只能一集一集点 | 展开成条目列表，勾选要哪些，批量下 |
| `magnet:` / `.torrent` | 完全不认 | 交给 aria2 的 BT 引擎（DHT/PEX/LPD 找源） |
| `thunder://` 迅雷链接 | 点开就装迅雷 | 解开壳还原成真实地址，照样多线程 |
| 各个下载器各管一摊 | 装四五个软件 | 直链/视频/BT/网盘识别，一个软件全接 |

核心是一件事：**不同类型的东西，不该用同一种方式搬**。

---

## 三种装法

### 一、安装版（推荐）

```
jiliu-setup-1.0.0.exe
```

双击 → 装到 `%LOCALAPPDATA%\Programs\Jiliu\` → 建开始菜单与桌面快捷方式 →
写入卸载项（控制面板里能正常卸载）。

命令行参数：

```bat
jiliu-setup.exe                       :: 常规安装
jiliu-setup.exe --target "D:\Tools\Jiliu"   :: 指定目录
jiliu-setup.exe --autostart           :: 开机自启
jiliu-setup.exe --no-shortcuts        :: 不建快捷方式
jiliu-setup.exe --quiet               :: 静默安装
jiliu-setup.exe --dry-run             :: 只打印将要做什么，不真装
jiliu-setup.exe --uninstall           :: 卸载
```

### 二、便携版

```
jiliu-portable-1.0.0.zip
```

解压到任意目录（U 盘也行）→ 双击 `jiliu.exe`，直接出一个桌面窗口。
（`启动.cmd` 是同一件事的另一层壳，给习惯找启动文件的人留着，效果一样。）

包里有个名为 `portable` 的空文件，**它的存在就是便携模式的开关**：
配置、任务记录、下载目录全部落在程序目录下的 `data/` 与 `downloads/`，
不往 `%USERPROFILE%` 写任何东西。整个文件夹拷走就是完整搬家。

想改用用户目录（`~/.jiliu`）：删掉 `portable` 文件即可。

### 三、源码运行

需要 Python 3.9+。

```bash
python jiliu.py                # 双击即开（等价于 gui）：桌面窗口
python jiliu.py gui            # 同上
python jiliu.py serve          # 只要 Web 面板 + API（不弹窗口）
python jiliu.py add <url>      # 直接下一个
python -m tests.run_all        # 跑测试（516 项）
```

---

## 双击即开：原生桌面窗口

```
jiliu.exe            → 桌面窗口（推荐）
jiliu.exe gui        → 同上，显式写法
jiliu.exe serve      → 只起服务，自己用浏览器开
```

以前是「起个后台服务，再打开浏览器」。现在双击出来的是一个**真正的桌面窗口**：
有标题栏、有任务栏图标、能最小化最大化、关掉就退出，没有终端窗口闪一下。

技术上用 **pywebview**（Windows 上走 Edge WebView2 + WinForms 宿主）。
好处是界面不用为了「像桌面软件」用 Qt 重写一遍 —— 还是那套 HTML，
换成原生壳装着。

几个刻意处理过的点：

| 问题 | 处理 |
|---|---|
| 端口被占（已经开着一个汲流） | 自动往后顺延，不报错退出 |
| 关窗口后进程不退（留僵尸） | 关窗时收干净服务线程与下载任务 |
| GUI 起不来却什么都看不见 | GUI 起不来时**恢复控制台**并把错误打出来，不静默退出 |
| 打包漏了 WebView2 的 DLL | 打包脚本 `--collect-all webview/pythonnet/clr_loader` |

窗口设置的三种模式（设置 → 界面）：`自动` / `原生窗口` / `浏览器`。

命令行也留了 `--window browser` 之类的开关，装不了 pywebview 的机器上会自动退回浏览器。

---

## 面板（IDM 风格）

`jiliu.exe` 双击即起面板，浏览器自动打开 `http://127.0.0.1:8788`。
布局照 IDM 的路子来 —— 左边分类，中间任务，底部状态：

```
┌─ 新建  展开合集  B站登录  环境体检  设置  协议支持  清理 ────┐
├─ 地址栏 [____________________] 类型▾ 线程▾ [探测] [开始] ──┤
├─ 暂停 继续 取消 │ 打开 定位 删除 │ 全选 取消选择 ──────────┤
├──────────────┬────────────────────────────────────────────┤
│ ▼ 分类        │  文件名        大小    进度      速度   剩余  │
│   全部        │  ubuntu.iso   4.7GB   ▓▓▓▓░░   12.4MB/s  2m │
│   视频        │  BV1xx...mp4  486MB   ▓▓▓▓▓▓   —        完成│
│   音频        │                                            │
│   合集        │                                            │
│   应用        │                                            │
│   压缩包      │                                            │
│   文档        │                                            │
│   光盘镜像    │                                            │
│   图片/字体   │                                            │
│   数据集      │                                            │
│   BT / 磁力   │                                            │
│   网盘链接    │                                            │
│   其他        │                                            │
│ ▼ 状态        │                                            │
│   下载中/等待  │                                            │
│   已完成/失败  │                                            │
│ ▼ 已下载记录   │                                            │
└──────────────┴────────────────────────────────────────────┘
  就绪 · 3 个任务 · 直连 · yt-dlp 2026.08.19 · aria2 已就绪
```

**左侧分类树**按类型 / 状态 / 已下载记录三段分组，点一下就是筛选。

**底部状态栏**把关键环境信息一直摊在那儿：代理走直连还是走代理、
yt-dlp 什么版本、aria2 有没有就绪、有几个任务在跑。少一次「为什么下不动」
的来回猜测 —— 状态栏里没看到 aria2，「BT 下不动」就有了答案。

**设置抽屉**分七页：常规、视频、合集、代理、Cookie、命名、BT。
命名页可以实时试算模板（`%title% [%id%]` → `%(title).150B [%(id)s]`），
改完立刻看到翻译结果，不用猜。

**弹层**：合集勾选（选要下哪几集）、B站扫码登录、环境体检、协议支持说明。

---

## 分类归档：下载目录下自动分文件夹

选中一个下载目录之后，汲流会在它下面按类型建子文件夹自动归位 ——
不再让几百个文件全堆在 `Downloads` 里。

```
<下载目录>/
├── 视频/          mp4 mkv flv rmvb ts …
├── 音频/          mp3 flac wav m4a …
├── 软件/          exe msi dmg apk …
├── 压缩包/        zip rar 7z tar.gz …
├── 文档/          pdf docx xlsx epub …
├── 光盘镜像/      iso img …
├── 图片/          jpg png webp gif …
├── 字体/          ttf otf woff2 …
├── 数据集/        safetensors ckpt …
├── BT/            磁力 / 种子
├── 网盘/          网盘直链
└── （认不出类型的直接放这里）
```

三个刻意的决定：

- **「其他文件」不建子目录**。认不出来的东西再塞进一个「其他」文件夹，
  只是把杂物换个地方堆，下次还是找不到 —— 不如让它在根目录显眼。
- **你显式指定了目录就不归档**。手动选目录 = 明确意图，再自作主张套一层是添乱。
  想让显式目录也归档，设置里开「显式目录也归档」。
- **只接受单个路径片段**。配置里写 `../../Windows` 必须被挡掉 ——
  这类项是本地接口能写进来的，路径穿越是实打实的风险。

子目录名可在 设置 → 归档 里改，也可以把某一类设成空（那一类就不归档）。
B站合集这类「本质还是视频」的分类会归到 `视频/`，不再单开一层。

```bash
python jiliu.py layout                                  # 看当前布局与示例树
python jiliu.py layout --url "https://x/a.zip"           # 预览某个链接会落到哪
python jiliu.py layout --category video                  # 看某个分类的落位
```

---

## 引擎：能用哪些、怎么选

汲流不只有一个下载引擎。引擎分三类，**职责不同，不能互相替代**：

| 引擎 | 类别 | 定位 |
|---|---|---|
| **自研多线程** | HTTP | 零依赖。按资源类型定制线程数与分片，支持续传/限速/暂停/哈希校验 |
| **aria2** | HTTP + P2P | **磁力/种子/ed2k 的唯一选择**；也能下普通 HTTP |
| **curl** | HTTP | 兼容性最好。自研引擎下不动的服务端，换它常常就过了 |
| **wget** | HTTP | 对「服务端中途断流」容忍度最高，会自己重连续传 |
| **yt-dlp** | 视频 | 只有它认识几千个站点的页面结构。不是可选功能 |
| **ffmpeg** | 合流 | 音视频分离下载后必须靠它合并；转码、嵌字幕也靠它 |
| **N_m3u8DL-RE** | HLS | 分片加密、多音轨的流比 ffmpeg 更稳。可选加强项 |

择优规则：

```
P2P（磁力/种子/ed2k）  → aria2（别无选择）
视频页 / m3u8 / mpd     → yt-dlp
普通 HTTP              → 按「引擎优先级」挑第一个可用的
```

**失败会自动降级。** 自研引擎偶尔会撞上挑食的服务端，这时候静默换 curl 重来一遍，
比让你对着一个错误码发呆强。降级链可在设置里关掉。

```bash
python jiliu.py engines            # 列出所有引擎与可用状态
python jiliu.py config --set engine_order=native,aria2,curl,wget
```

---

## 两个版本：国内版 / 全球版

打包出**两个独立版本**，不是同一份程序藏几个按钮：

| | 国内版 `cn` | 全球版 `global` |
|---|---|---|
| 国内站点 | ✅ | ✅ |
| 海外站点（YouTube/TikTok/X…） | ❌ **真拦掉** | ✅ |
| 代理设置 | 界面上不出现 | ✅ 完整 |
| BT / 网盘 | ✅ | ✅ |

国内版面对的是「只用国内站」的场景 —— 单位电脑、内网、给家人装的机器。
这类环境下把 YouTube、代理这些东西摆在界面上，除了制造困惑没有别的作用
（点了必然失败，还要解释为什么失败）。所以国内版是**真的一条都不发**：
界面上不出现，逻辑上也拒掉，报错信息直接说清「本版不含此能力，需要请换全球版」。

反过来，**判「是不是海外站点」这件事刻意保守**：只有明确认识的海外平台才判为海外，
白名单之外的域名一律放行。理由是搞错方向的代价不对称 ——
把国内小众站误判成海外，会让你正常能下的东西下不了。

```bash
python jiliu.py edition            # 看当前版本与两种形态的区别
python jiliu.py edition cn         # 切到国内版
python jiliu.py edition global     # 切到全球版
```

---

## 代理：默认跟随系统代理

下载外网的东西需要代理，但**默认不需要你填任何东西** —— 汲流直接读 Windows
的 Internet 设置（注册表里的系统代理），解析 `host:port` / `http=` / `socks=`
各种写法，连「哪些地址不走代理」的例外列表也一起读。

设置 → 代理里有三种模式：

| 模式 | 说明 |
|---|---|
| **跟随系统**（默认） | 读系统代理；系统没配就直连。界面会显示当前读到的是什么 |
| **手动** | 自己填。内置 8 个常见预设，点一下就填好 |
| **直连** | 一律不走代理 |

还有「连不上时自动直连」的兜底开关 —— 代理挂了不至于整个不能用。

代理按链接分流：国内站直连，海外站走代理。同一个任务队列里两种链接混着也没问题。

```bash
python jiliu.py proxy              # 看当前用的是哪个代理、通不通
python jiliu.py proxy --test       # 逐项测速
```

⚠️ 一个踩过的坑：本机如果同时存在**环境变量**代理（`HTTP_PROXY`）和**系统**代理，
两边可能只有一个是活的。汲流优先用系统代理并验活，不会闷头走一个死掉的环境变量。


## 国内站点实测结论

「国内一般的视频网站链接都要做测试」—— 这份表是真跑出来的，不是照着
yt-dlp 的站点列表抄的。测试脚本 `build/_test_cn_sites.py` 可以随时重跑。

判定分三层，混在一起就会得出错结论：

1. **域名有没有解析器**（离线）—— 站点能力
2. **yt-dlp 认不认这条链接**（离线，官方 `suitable()` API）—— 链接形态
3. **实际能不能解出媒体**（联网，`--simulate`）—— 真相

> 前两版脚本就是把这三层混成了一层：拿站点**首页**去喂 yt-dlp，
> 首页必然报 `Unsupported URL`，于是把所有站都判成「不支持」——
> 而首页从来就不是播放页。

### 国内 16 站（实测于 2026-09-23）

| 站点 | 结果 | 说明 |
|---|---|---|
| 哔哩哔哩 | ✅ 可用 | 免登录拿 1080P，15 档格式。有官方扫码登录 |
| 优酷 | ✅ 可用 | 免登录 1080P |
| 腾讯视频 | ✅ 可解析 | 不带登录态通常只给试看片段 |
| 芒果TV | ✅ 可用 | 要用当前的 `/b/<栏目id>/<集id>.html`，老链接会 404 |
| 微博 | ✅ 可用 | 免登录 |
| AcFun | ✅ 可用 | 免登录拿到多档画质 |
| 央视网 | ✅ 可用 | 免登录。只认 `/<年>/<月>/<日>/VIDE….shtml` 和 `/video/` |
| 搜狐视频 | ⚠️ 限流 | 解析器认链接，但接口会 **429** —— 换 IP 或稍后重试 |
| 西瓜视频 | ⚠️ 需 Cookie | `Ixigua: Cookies (not necessarily logged in) are needed` |
| 抖音 | ⚠️ 需新鲜 Cookie | `Fresh cookies … are needed` —— 旧的 Cookie 也不行 |
| 小红书 | ⚠️ 需登录 | 必须给**视频**帖链接 + 登录态；图文帖会报 `No video formats` |
| 爱奇艺 | ❌ 解析器失效 | 换多个有效播放页都报 `Can't find any video` —— 爱奇艺改版后上游没跟上 |
| 快手 | ❌ 无解析器 | yt-dlp 没有快手的解析器 |
| 咪咕视频 | ❌ 无解析器 | 同上 |
| PP视频 | ❌ 无解析器 | 同上 |
| 电影网 | ❌ 无解析器 + 403 | 对非浏览器 UA 直接 403，连首页都抓不到 |

**结论：16 个国内站里 7 个开箱可用（B站/优酷/腾讯/芒果/微博/AcFun/央视），
3 个只差一份 Cookie（西瓜/抖音/小红书），1 个被限流（搜狐），
1 个上游解析器坏了（爱奇艺），4 个 yt-dlp 根本不支持（快手/咪咕/PP视频/电影网）。**

这 4 个要支持只能自研接口 —— 不是配置问题，是缺能力，所以界面上会**如实说**
「本站暂不支持」，而不是让你等一个必然的失败。

这份实测结论回写进了 `jiliu/sites.py` 每个站点的 `note` 字段：
哪家免登录、哪家要 Cookie、哪家链接会过期，都记在代码旁边。
**改代码的人一眼能看到上次测出来是什么样**，不用重新踩一遍。

```bash
# 全量重跑（联网，约 1.5 分钟）
python build/_test_cn_sites.py

# 只看离线判定（不联网，秒出）
python build/_test_cn_sites.py --offline

# 单条链接归因，打印完整 stderr
python build/_probe_url.py "https://www.mgtv.com/b/896231/24547204.html"

# 自动从站点首页/搜索接口捞一条真实播放页链接
python build/_discover_links.py --json
```

---

## 浏览器插件

`extension/` 目录。支持 Chrome / Edge / 其他 Chromium 内核浏览器（MV3，要求 Chrome 111+）。

**安装：** 打开 `chrome://extensions` → 右上角开启「开发者模式」→
点「加载已解压的扩展程序」→ 选中 `extension` 文件夹。

装好后点插件图标，确认 **API 地址**填的是 `http://127.0.0.1:8788`（面板默认端口）。

### 下载接管

这是本次的重点能力。插件会监听浏览器的每一次下载，**先暂停、再判断、然后决定放行还是接管**。

顺序很关键 —— `onCreated` 触发时下载已经跑起来了，所以必须先 `pause()` 再决策，
否则那几百 KB 已经白白流过网卡了。

接管模式三选一：

| 模式 | 行为 | 适合 |
|---|---|---|
| `off` | 完全不管，浏览器自己下 | 调试插件 / 不想被打扰 |
| `smart`（默认） | 按下面的规则挑着接管 | 日常用 |
| `all` | 全部交给汲流 | 我就想要所有下载都走汲流 |

`smart` 的判断规则（`resource.intercept_decision()`）：

- 跳过非 HTTP 协议：`blob:` / `data:` / `about:` / `javascript:` / `chrome:`
- 跳过内置黑名单域名：浏览器自更新、扩展商店、DRM 服务等
- 跳过用户自己配的 `skipHosts`
- **小于 1 MiB 的图片/字体** → 放行（接管反而更慢）
- **大于 8 MiB 且类型不明** → 接管（大文件正是多线程的用武之地）
- **安装包 / 压缩包 / 镜像 / 数据集** → 接管（下坏了代价大，汲流会校验哈希）
- `.m3u8` / `.mpd` / `.f4m` 流清单 → 接管（浏览器根本处理不了）
- 视频站页面 → 接管（走 yt-dlp）

**接管失败一定救回来**：任何一步出错都会调 `chrome.downloads.resume()`，
绝不让一个下载卡在暂停状态无声无息。

### 页面嗅探

除了接管浏览器下载，插件还会钩页面里的 `<video>` / `<audio>` 标签、
`fetch` / `XMLHttpRequest` 请求，把真实媒体地址捞出来。
这部分的钩子跑在 `"world": "MAIN"`（页面主世界）——
放隔离世界里是钩不到页面自己发的请求的。

点插件图标 → 列表里点「交给汲流」即可。

---

## 类型识别

`jiliu/resource.py`，10 个分类、237 个后缀、64 条魔数签名。

### 四路嗅探

| # | 线索 | 权重 | 说明 |
|---|---|---|---|
| ① | `Content-Disposition` 文件名 | 0.35 | 服务端自己声明的，最可信 |
| ② | `Content-Type` MIME | 0.6 | 服务端声明，但常有 `application/octet-stream` 糊弄 |
| ③ | URL 路径后缀 | 0.5 | 最省事，但 CDN 常带假后缀或干脆没有 |
| ④ | 文件头魔数 | 1.0 | 拿到实打实的前 1 KB，骗不了人 |

四条线索加权投票，权重最高的说了算。**魔数权重最高**，因为它是唯一无法伪造的。

### 魔数的二级分类

同一个文件头下面还有细分，这层最容易被写漏：

- **ZIP 家族**（`PK\x03\x04`）：光看头部分不出 `.docx` / `.xlsx` / `.pptx` / `.apk` /
  `.jar` / `.msix` / `.epub`。要**解开内部条目名**才能判断 —— 而且必须扫**全部**
  条目，不能只看第一个：MSIX 的 `[Content_Types].xml` 通常排在第二个。
- **RIFF 家族**（`RIFF....`）：要看偏移 8–12 字节。`AVI ` 是视频，`WAVE` 是音频，
  `WEBP` 是图片。
- **MP4 家族**（`ftyp`）：要看 brand。`isom` / `mp42` 是视频，`M4A ` 是音频，
  `avif` / `heic` 是图片。

### 各分类的策略

| 分类 | 引擎 | 线程 | 分片 | 校验哈希 | 可解压 |
|---|---|---|---|---|---|
| 视频 | auto | 8 | 1 MiB | – | – |
| 音频 | auto | 8 | 1 MiB | – | – |
| 合集 / 播放列表 | ytdlp | 1 | – | – | – |
| 应用 | 多线程 | 8 | 1 MiB | **是** | – |
| 压缩包 | 多线程 | 8 | 1 MiB | **是** | 是 |
| 文档 | 多线程 | 6 | 512 KiB | – | – |
| 光盘镜像 | 多线程 | 8 | 4 MiB | **是** | 是 |
| 图片 | 多线程 | 4 | 256 KiB | – | – |
| 字体 | 多线程 | 4 | 256 KiB | – | – |
| 数据集/模型 | 多线程 | 8 | 4 MiB | **是** | 是 |
| BT / 磁力 | aria2 | – | – | – | – |
| 网盘链接 | native | 1 | 1 MiB | – | – |
| 其他文件 | 多线程 | 8 | 1 MiB | – | – |

**合集 / 播放列表**没有线程与分片可谈 —— 它不是一个文件，而是一串条目，
整串交给 yt-dlp 顺序下（并发是「同时下几集」，在设置里改）。

**BT / 磁力**同样不列线程：BT 的并发由 aria2 按 peer 数自己调度，
人为指定线程数没有意义，反而会打架。

几个刻意的设计：

- **应用 / 压缩包 / 镜像 / 数据集强制校验哈希** —— 这几类下坏了代价最大。
  装到一半报错的安装包、解压到 99% 失败的压缩包，比压根没下更烦人。
- **小文件降级单线程** —— 每个分类都有 `min_multithread` 门槛。
  40KB 的文件开 8 个连接，握手开销比传输本身还大。
- **文档并发降到 6、分片降到 512 KiB** —— 文档普遍不大，太多连接是浪费。
- **平台识别不压过后缀** —— `x.com/sprite.png` 命中 X 平台域名，
  但它有明确的 `.png` 后缀，就是个静态图片，不该硬塞给 yt-dlp 解析。

> 表里的 KiB / MiB 是 1024 进制。面板和命令行显示成 `1.00 MB` 也是按 1024 算的
> （跟 Windows 的习惯一致），不是 1000。

### 命令行查识别结果

```bash
# 联网探测 + 识别
python jiliu.py classify https://example.com/thing

# 纯离线判断（不联网）
python jiliu.py classify -n setup.exe
python jiliu.py classify -n data.bin -m application/pdf
python jiliu.py classify -n huge.iso -s 4294967296

# 看判定依据
python jiliu.py classify https://example.com/thing --reasons
```

输出示例：

```
  分类     应用   (app)
  具体类型 Windows 安装程序
  引擎     multithread
  并发     8 线程
  分片     1.0 MiB
  多线程门槛 1.0 MiB（低于则单线程）
  下完校验 SHA256
  置信度   0.95
```

---

## 统一协议支持

从各处复制来的「链接」并不都是 `http://` 开头。汲流在识别类型之前先过一层
协议判定（`jiliu/protocols.py`），按链接的真实形态分流给不同引擎。

| 链接形态 | 处理方式 | 走向 |
|---|---|---|
| `magnet:?xt=urn:btih:...` | 取 info hash + `dn` 参数还原显示名 | aria2 的 BT 引擎 |
| `*.torrent`（种子文件） | 下载种子后交给 BT 引擎 | aria2 的 BT 引擎 |
| `ed2k://\|file\|...` | eDonkey 链接 | aria2 的 BT 引擎 |
| `thunder://...` | base64 解壳，去掉 `AA`…`ZZ` 包裹 | 解出内层 URL 后照常识别 |
| `qqdl://...` | base64 解壳 | 解出内层 URL 后照常识别 |
| `flashget://...` | base64 解壳，再去 `[FLASHGET]` 标记 | 解出内层 URL 后照常识别 |
| 网盘分享页 | **只识别 + 如实说明**，不逆向私有接口 | native 单线程 |

### 迅雷 / 旋风 / 快车：解开就好

这三种「专属链接」本质上都是**普通 URL 套了一层壳**：

```
thunder://QUFodHRwOi8vY2RuLmV4YW1wbGUuY29tL3NldHVwLmV4ZVpa
            └─ base64 解出来是 → AAhttp://cdn.example.com/setup.exeZZ
                                 └─ 去掉 AA / ZZ → 真正的下载地址
```

解壳之后的地址就是普通 HTTP 直链，交给多线程引擎跑满带宽。
**壳里裹的也可能是个种子**（`thunder://` 后面跟的是 `.torrent` 地址），
所以解壳不是终点 —— 解完还要重新走一遍完整识别。

### BT：为什么自己不写

磁力 / 种子 / ed2k 一律交给 **aria2**，自研引擎不碰 P2P。原因是这几样都要
DHT 找源、PEX 交换、LPD 本地发现、peer 阻塞检测、分块优先级调度 ——
是一个独立且庞大的领域，重写一遍的收益远小于直接复用 aria2。

代价是**下 BT 需要 aria2c**。没装时汲流会直接说「BT 需要 aria2 后端」，
不会假装在下然后卡在 0%。放一个 `aria2c.exe` 到 `bin/` 即可，面板里的
「环境体检」也能看到它有没有被认出来。

### 网盘：说清楚能做什么、不能做什么

**不做**：逆向百度网盘 / 夸克 / 115 的私有接口来「提速」。那既违反服务条款，
维护成本也高得离谱 —— 接口一改就废，属于典型的负资产。

**做**：识别出是哪个网盘，然后告诉你**怎么拿到直链**。拿到直链后，
汲流的普通多线程引擎本来就能满速下。已识别的网盘：

百度网盘、阿里云盘、夸克网盘、天翼云盘、115 网盘、123 云盘、蓝奏云、
Google Drive、MEGA。
（未列出的域名按普通链接处理，不做猜测。）

### 命令行查协议

```bash
python jiliu.py protocol "magnet:?xt=urn:btih:..."
python jiliu.py protocol "thunder://QUFodHRwOi8v..."
python jiliu.py protocol "https://pan.baidu.com/s/1abc" --json
```

---

## 合集 / 播放列表

B站合集、YouTube 播放列表、频道，都按「合集」处理：

- 默认**建子目录**、文件名带**序号前缀**（`%pl_index% - `），否则下完是一堆
  名字相似的文件混在一起，按标题排序就是乱的
- 可以先探测条目、**只挑其中几集**下
- 开启「跳过已下载」后，同一集不会重复下 —— 补集时不用人工比对

```bash
# 先看合集里有什么
python jiliu.py playlist "https://www.bilibili.com/video/BV1xx411c7mD"

# 确认后整季下
python jiliu.py video "https://space.bilibili.com/xxx" --playlist

# 只下第 3、5、7 集
python jiliu.py video "https://space.bilibili.com/xxx" --playlist --items 3,5,7

# 只抽音轨
python jiliu.py video "<url>" --playlist -x
```

B站合集优先走**官方接口**（更快、标题更准），失败才回退到 yt-dlp 通用解析。

---

## 命令行

```bash
# 面板 / 桌面窗口
python jiliu.py                    # = gui，双击即开的原生窗口
python jiliu.py gui [--port 8788] [--window auto|native|browser]
python jiliu.py serve [--port 8788] [--open-browser] [--no-browser]

# 下载
python jiliu.py add <url> [-t 16] [-o DIR] [-n NAME] [-b native|aria2|curl|wget] [-s 限速] [--sha256 HASH]
python jiliu.py video <url> [-f "bv*+ba/b"] [-o DIR] [-i] [--playlist] [--items 1,3,5] [-x/--audio]
python jiliu.py playlist <url> [--limit 200] [--full] [--bili] [--json]

# 识别与探测
python jiliu.py classify (<url> | -n FILE) [-m MIME] [-s SIZE] [-r] [--json] [--tools]
python jiliu.py probe <url> [-k http|video]
python jiliu.py protocol <url> [--json]

# 平台能力（本轮新增）
python jiliu.py engines            # 所有下载引擎与可用状态
python jiliu.py sites [--json]     # 支持的视频站点（国内/海外）+ 实测现状
python jiliu.py layout [--url U] [--category C] [--json]   # 分类归档布局
python jiliu.py edition [cn|global]                        # 版本形态
python jiliu.py qr "文本" [-o out.png] [--matrix]           # 二维码（调试/自检）

# 登录 / 凭据 / 记录
python jiliu.py bili [status|login|logout]
python jiliu.py cookies [list|browsers|import|export|rm] [file] [--label X] [--browser Y] [--profile Z]
python jiliu.py history [list|rm|clear|export|import] [key] [-q 关键词] [--limit 100] [--file PATH]

# 其它
python jiliu.py naming [--pattern "%title% [%id%]"]
python jiliu.py proxy [--test URL] [--timeout 5] [--url 目标]
python jiliu.py tools
python jiliu.py config [--set k=v ...]
python jiliu.py list
```

打包后把 `python jiliu.py` 换成 `jiliu.exe`，**双击不带参数 = 开桌面窗口**
（不是开浏览器）。想要老行为用 `jiliu.exe serve`。

### B站登录与 Cookie

B站的高清（1080P+）与部分合集需要登录态。两种拿凭据的方式：

```bash
# 方式一：扫码登录（推荐，不用折腾浏览器）
python jiliu.py bili login      # 面板里也有「B站登录」按钮，出二维码

# 方式二：从浏览器导入 cookies.txt
python jiliu.py cookies import cookies.txt --label bilibili.com
python jiliu.py cookies browsers    # 看本机有哪些浏览器能被读
```

⚠️ **Chrome / Edge 127+ 读不了** —— 它们对 cookie 库做了 App-Bound 加密，
纯 Python 解不开（`yt-dlp --cookies-from-browser` 同样不行）。
Firefox 不受影响；否则用浏览器扩展导出 `cookies.txt` 再 `import`。

YouTube 的「Sign in to confirm you're not a bot」同理：需要本机代理 +
登录态 Cookie，两者缺一不可。

---

## 外部工具

汲流本身不捆绑 ffmpeg。有则用，没有则降级，不会假装能用。

| 工具 | 用途 | 必需性 |
|---|---|---|
| **yt-dlp** | 视频站解析 | 打包版已内置；便携版可放 `bin/yt-dlp.exe` 覆盖成更新的 |
| **ffmpeg** | 音视频合并、抽音轨 | 下「音视频分离」的站（B站、YouTube 高清）必需 |
| **ffprobe** | 媒体信息探测 | 可选 |
| **aria2c** | 备用下载引擎 | 可选 |

放进 `bin/` 目录（便携版）或 exe 同级 `bin/`（安装版），汲流会自动认出来。

看当前状态：

```bash
python jiliu.py tools
```

关于 yt-dlp 版本：汲流**不看形式看版本号**。
`scoop` 装的 exe、`pip` 装的 module、exe 内置的那份，三者都会拿出来比版本，
选最新的用。两年差的老版本会大面积解析失败 —— 站点改版太频繁了。

---

## 配置

配置文件位置：

- 源码 / 安装版 → `~/.jiliu/config.json`
- 便携版 → 程序目录 `data/config.json`

常用的几项：

```bash
python jiliu.py config --set threads=16
python jiliu.py config --set backend=aria2
python jiliu.py config --set download_dir="D:/Downloads"
python jiliu.py config --set speed_limit=0
python jiliu.py config --set engine_order=native,aria2,curl,wget
python jiliu.py config --set edition=cn
python jiliu.py config --set proxy_mode=system
```

| 键 | 默认 | 说明 |
|---|---|---|
| `download_dir` | `~/Downloads` | 下载目录（便携模式为程序目录下） |
| `threads` | 8 | 分片线程数 |
| `port` | 8788 | 面板 / API 端口 |
| `max_concurrent` | 3 | 同时下载任务数上限 |
| `backend` | `auto` | `native` / `aria2` / `curl` / `wget` / `auto` |
| `speed_limit` | 0 | 全局限速（字节/秒），0 不限 |
| `min_multipart` | 1 MiB | 小于此值不走多线程 |
| `video_format` | `bv*+ba/b` | 最佳画质+最佳音轨，没有则最佳单文件 |
| `aria2_rpc_url` | 空 | 连已有的 aria2 RPC；Motrix 默认 `16800` |
| **`engine_order`** | `["aria2","native","curl","wget"]` | HTTP 引擎优先级 |
| **`auto_fallback_engine`** | `true` | 引擎失败后自动降级重试 |
| **`curl_path`** / **`wget_path`** | 空 | 手动指定外部引擎位置（PATH 探不到时） |
| **`auto_subdir`** | `true` | 按类型自动建子文件夹归档 |
| **`subdir_names`** | `{}` | 各分类的子目录名覆盖（空串 = 该类不归档） |
| **`subdir_explicit`** | `false` | 显式指定目录时是否也归档 |
| **`edition`** | `global` | `cn` 国内版 / `global` 全球版 |
| **`proxy_mode`** | `system` | `system` 跟随系统 / `manual` 手动 / `direct` 直连 |
| **`proxy_user`** / **`proxy_pass`** | 空 | 手动代理的账号密码 |
| **`proxy_fallback_direct`** | `true` | 代理连不上时自动直连 |
| **`window_mode`** | `auto` | `auto` / `native` / `browser` |

环境变量：

- `JILIU_HOME` —— 强制状态目录（多实例隔离 / 测试用）
- `JILIU_PORTABLE=1` —— 强制便携模式
- `JILIU_EDITION=cn|global` —— 强制版本形态（给两个版本包塞不同默认值时用）

---

## HTTP API

面板和插件都走这套接口，也可以自己调。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 存活探测 |
| GET | `/api/state` | 任务 + 统计 + 代理/Cookie/记录/命名 快照（面板每秒轮询这个） |
| GET | `/api/tasks` | 任务列表（`?log=N` 带日志尾巴） |
| GET | `/api/tasks/<id>` | 单个任务详情 |
| POST | `/api/tasks` | **建任务**（自动识别类型并套用策略） |
| POST | `/api/tasks/<id>/pause` `resume` `cancel` `remove` | 任务控制 |
| POST | `/api/tasks/<id>/open` `reveal` | 打开产物 / 在资源管理器中定位 |
| POST | `/api/tasks/clear-finished` | 清掉已结束的任务 |
| GET | `/api/categories` | 全部分类与策略 |
| POST | `/api/classify` | 纯离线识别（给 filename / mime / size） |
| POST | `/api/probe` | 探测直链元信息 |
| POST | `/api/inspect` | 联网探测 + 识别 |
| POST | `/api/protocol` | 协议判定（磁力/迅雷/ed2k/网盘） |
| POST | `/api/intercept` | 接管决策（**浏览器插件调的就是这个**） |
| POST | `/api/video/probe` | 视频元信息与格式列表 |
| POST | `/api/video/playlist` | 展开合集，返回条目（B站优先走官方接口） |
| POST | `/api/video/inspect` | 视频站详情 |
| POST | `/api/video/diagnose` | 解析失败时的诊断（差哪个工具 / 要不要 Cookie） |
| GET | `/api/env` | 环境体检（工具、后端、目录、平台） |
| GET | `/api/engines` | 引擎元数据 + 可用性 + 实际选中的 HTTP 引擎 |
| GET | `/api/edition` | 版本形态（国内版/全球版）与能力表 |
| GET | `/api/sites` | 支持的视频站点（含国内/海外归类与实测现状） |
| GET | `/api/layout` | 分类归档布局（文件夹清单 + 示例树） |
| GET | `/api/desktop` | 原生窗口能力（可用 / 不可用原因 / 三种模式） |
| GET | `/api/proxy` | 代理现状与分流策略 |
| POST | `/api/proxy/test` | 测某个代理端点 |
| POST | `/api/proxy/detect` | 嗅探本机可用代理 |
| GET | `/api/cookies` | 凭据文件清单 |
| POST | `/api/cookies/save` | 保存 cookies.txt |
| POST | `/api/cookies/delete` | 删除凭据 |
| GET | `/api/naming` | 命名变量表 |
| POST | `/api/naming` | 命名模板试算 |
| GET | `/api/history` | 已下载记录（`-q` 搜索） |
| POST | `/api/history/remove` `/api/history/clear` | 删记录 |
| GET | `/api/bili` | B站登录状态（带 60 秒缓存） |
| GET | `/api/bili/resolve` | 解析 B站链接类型（视频/合集/番剧） |
| POST | `/api/bili/login/qr` `/api/bili/login/poll` `/api/bili/logout` | 扫码登录流程 |
| GET/POST | `/api/config` | 读 / 改配置 |
| GET | `/api/tools` | 外部工具状态（`?refresh=1` 强制重扫） |
| POST | `/api/handoff/motrix` | 把任务转交给 Motrix |

工具探测与后端探测都带 60 秒缓存 —— 一次完整探测要 spawn 四五个子进程
（实测 5 秒上下），而面板是**每秒**轮询 `/api/state` 的。
没有缓存的话，每轮刷新都要重新拉一遍版本号，界面看着像卡死，CPU 也白烧。
刚把新版的 `yt-dlp.exe` 丢进 `bin/`、或者刚启动 Motrix，
用 `/api/tools?refresh=1`（或命令行 `jiliu tools`）立刻重扫一次即可。

---

## 目录结构

```
jiliu-downloader/
├── jiliu/
│   ├── engine.py      自研多线程引擎（Range 请求、动态领块、断点续传）
│   ├── resource.py    资源识别与接管策略（13 个分类、四路嗅探）
│   ├── protocols.py   协议识别与解壳（磁力/种子/ed2k/迅雷/旋风/快车/网盘）
│   ├── naming.py      命名模板翻译 + 已下载记录（History）
│   ├── proxy.py       代理探测、验活、系统代理读取与国内外分流
│   ├── cookies.py     凭据管理（含从浏览器导出）
│   ├── bili.py        B站官方接口（扫码登录、合集解析）
│   ├── video.py       yt-dlp 视频解析与合集下载
│   ├── qr.py          二维码渲染（纯 Python，无需 Pillow）
│   ├── layout.py      下载目录的分类归档布局
│   ├── edition.py     版本形态（国内版 / 全球版）
│   ├── engines.py     引擎总览与择优（含失败降级链）
│   ├── sites.py       视频站点注册表（21 站 + 实测现状）
│   ├── desktop.py     原生桌面窗口（pywebview）
│   ├── manager.py     任务管理器
│   ├── task.py        任务模型与状态机
│   ├── backends/      下载后端（native / aria2 / curl / wget）
│   ├── server.py      本地 HTTP API + 面板
│   ├── config.py      配置与便携模式
│   └── cli.py         命令行
├── extension/         浏览器插件（MV3）
├── web/               面板页面（IDM 风格单页）
├── installer/         安装器源码
├── scripts/           打包与辅助脚本
├── build/             构建中间产物 + 站点实测脚本与报告
├── tests/             测试套件（8 套 516 项）
└── vendor/            yt-dlp / qrcode / pypng（打包时内置）
```

---

## 已知问题

- **单文件 exe 约 18 MB**。其中大头是 `--collect-submodules yt_dlp` ——
  yt-dlp 靠 `importlib` 动态加载 extractor，不整包收集会漏掉一大半站点
  （实测冻结态枚举出 1751 个 extractor）。换来的是「目标机器不装 Python 也能用」。
  如果更在意体积，可以改成不内置、只走 `bin/yt-dlp.exe` 外部依赖。
- **虚拟/无头环境里桌面窗口可能开不起来**。pywebview 需要 Edge WebView2 运行时
  （Windows 10/11 通常自带）。开不起来时汲流会**恢复控制台并打印错误**，
  同时可以用 `--window browser` 退回浏览器面板 —— 不会静默退出。
- **下 BT 需要 aria2c**。磁力/种子/ed2k 不自研，一律交给 aria2 的 BT 引擎。
  没有 aria2c 时任务会明确报「BT 需要 aria2 后端」，不会假装在下。
- **网盘分享页不能直接下**，这是设计如此。汲流只做识别并告诉你如何取直链，
  不逆向私有接口（违反服务条款，且接口一改即废）。有直链就能满速下。
- **YouTube 需要本机代理 + 登录 Cookie**。报
  「Sign in to confirm you're not a bot」是 YouTube 的风控，
  不是解析器坏了 —— 补上代理与凭据即可。
- **国内 4 个站 yt-dlp 根本不支持**：快手、咪咕视频、PP视频、电影网。
  这是**能力缺口不是配置问题**，界面会如实说明，不会让你等一个必然的失败。
  要支持只能自研接口。（实测明细见上面「国内站点实测结论」）
- **爱奇艺的 yt-dlp 解析器当前失效**。换多个有效播放页都报
  `[iqiyi] Can't find any video` —— 爱奇艺改版后上游没跟上，等 yt-dlp 更新。
- **Chrome / Edge 127+ 的 Cookie 读不了**（App-Bound 加密，纯 Python 无解）。
  改用 Firefox，或用扩展导出 cookies.txt 后 `cookies import`。
- **DRM 保护的内容下不了**，这是设计如此，也不打算支持。
- 分片下载依赖服务端支持 `Range`。不支持的会**自动降级单线程**，不会硬失败。
- **iptv / 直播流不落盘** —— 那是流不是文件，属于另一类工具。

---

## 许可

MIT。第三方组件许可见 `LICENSE`。

---

## 1.7.0 新增：对着 IDM / Bilidown / 4K Video Downloader 补的 11 件事

做了一轮静态逆向（IDM 6.43 的字符串与注册表、Bilidown 的 app.asar 与 aria2 配置、
4K VD 的 QML 文案与设置键），把差距补进了代码。逐项对应的开关都在设置里。

| # | 能力 | 落点 | 开关 |
|---|---|---|---|
| 1 | **落盘改 `.part` 暂存 + 组装 + 原子改名**（IDM 的 TempPath 做法） | `engine.py` | `temp_dir`（留空=同目录） |
| 2 | **B 站原生 DASH**：playurl + WBI 签名，双流下载后 ffmpeg 合并 | `bili.py` / `bili_dash.py` | `bili_native` / `bili_qn` / `bili_codec` |
| 3 | **aria2 参数分三档**：视频 4 连接、通用 8、BT 16 | `backends/aria2.py` | `aria2_profile` |
| 4 | **本地嗅探代理**：没装扩展的客户端也能被接管（HTTP 精确、HTTPS 隧道） | `sniffproxy.py` | `sniff_proxy_enabled` |
| 5 | **订阅闭环**：feed 解析 → 定时 diff → 自动入队 | `subscriptions.py` | `feed_check_minutes` |
| 6 | **计划任务 / 时段限速**：只在指定星期与时段下载 | `schedule.py` | `schedule` |
| 7 | **完成度细节**：系统通知、阻止睡眠、完成后命令、托盘、m3u、序号补零 | `notify.py` / `tray.py` / `manager.py` | `notify_enabled` / `prevent_sleep` / `tray_enabled` / `after_download_command` / `m3u_enabled` / `number_pad` |
| 8 | **站点抓取**（保守版 Site Grabber）：深度、同源、页面上限、`.grab.json` 断点 | `grabber.py` | `grab_max_depth` / `grab_max_pages` / `grab_same_origin` |
| 9 | **剪贴板监听**（IDM 的 MonitorUrlClipboard） | `clipboard.py` | `clipboard_watch` |
| 10 | **接管例外站点 / 噪音后缀**，扩展与后端共用同一份规则 | `capture_rules.py` + `extension/background.js` | `capture_exceptions` / `capture_max_mb` |
| 11 | **单任务限速**：运行中可改，0 = 跟随全局 | `manager.set_task_speed` / 右键菜单 | 任务右键「限速…」 |

接口新增：`/api/schedule`、`/api/subscriptions`（含 `/check`、`/remove`）、
`/api/grab`、`/api/sniff`、`/api/clipboard`、`/api/capture/rules`、
`/api/tasks/<id>/speed`。

### 实测记录（本机）

- 取消后：目标文件**不存在**，`.part` 保留，sidecar 保留；续传复用 9.4 MB，完成后 `.part` 与 sidecar 一并消失。
- B 站：`BV1GJ411x7h7` 未登录状态取到 480P（接口上限如此），双流合并成 mp4 用时 17 s、26 MB。
- 嗅探代理：经代理下载本地 mp4，`recent()` 正确记到 URL 与 `video/mp4`。
- 托盘：创建/销毁正常（`Shell_NotifyIconW`）。
- 订阅：添加时把现有条目记为已读，立即检查返回 `new: 0`。
- 抓取：3 页 + 1 张图，路径按 `host_port` 落盘，Windows 非法字符已转义。
