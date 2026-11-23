/* ============================================================
 * background.js —— 服务工作线程（MV3）
 *
 * 干四件事：
 *   1. 右键菜单「用汲流下载此链接 / 此媒体 / 解析本页视频」
 *   2. 统一往本地 API 发请求（popup 也走这里）
 *   3. 【接管】监听浏览器自身的下载事件，交给汲流判断要不要接手
 *   4. 记住 API 地址、接管模式与站点豁免名单
 *
 * MV3 注意：所有 chrome.* 事件监听必须在顶层同步注册。
 * 放到 async 函数里注册会在 Service Worker 被唤醒时漏掉事件。
 * ============================================================ */
'use strict';

const DEFAULTS = {
  apiBase: 'http://127.0.0.1:8788',
  preferMotrix: false,
  // 接管模式：
  //   off  完全不碰浏览器的下载
  //   smart 交给汲流判断（小文件放行，安装包/压缩包/视频/大文件接过来）
  //   all  只要汲流能处理就接过来
  interceptMode: 'smart',
  skipHosts: [],          // 豁免域名，命中的一律不接管
  notifyOnTake: true,
  // 汲流没运行时是否自动用 jiliu:// 把它拉起来。默认开 ——
  // 关掉之后点下载链接只会得到一个"连不上本机服务"的错误。
  autoLaunch: true,
  // 视频悬浮按钮（贴在播放器外侧，可拖动）
  hotspotButton: true,
  // 页面右下角的进度悬浮窗（总速度 / 当前文件 / 总进度 自动轮转）
  floatWidget: true
};

/** 接管时排除的域名（更新源、放行名单等），用户可在 popup 里追加 */
const BUILTIN_SKIP = [
  'update.googleapis.com', 'clients2.google.com', 'dl.google.com',
  'windowsupdate.com', 'microsoft.com', 'login.live.com'
];

async function getCfg() {
  const got = await chrome.storage.local.get(DEFAULTS);
  return Object.assign({}, DEFAULTS, got);
}

function apiUrl(base, path) {
  return base.replace(/\/+$/, '') + path;
}

/* ---- 端口发现 ----
 * desktop.run() 起服务时调 pick_port(8788)：8788 被占就顺延到 8789、8790…
 * 而扩展这边只有 apiBase 一个地址，于是应用明明已经起来了、扩展还在原地
 * 数到超时，最后告诉用户"请手动启动" —— 上次汲流没退干净（残留进程还
 * 咬着 8788）时最容易撞上。
 *
 * 所以探不通就按顺延范围扫一遍 /api/health，从 app 字段确认是汲流，就把
 * 地址记下来，往后都走它。用户什么都不用做。
 */
const PROBE_SPAN = 9;
let _appBase = '';

async function probeApp(base) {
  try {
    const r = await fetch(apiUrl(base, '/api/health'), { cache: 'no-store' });
    if (!r.ok) return null;
    const j = await r.json().catch(() => null);
    // 认 app 字段而不是"能连上就算"：8788 上完全可能是别的本地服务
    return (j && j.ok && /汲流|jiliu/i.test(String(j.app || ''))) ? j : null;
  } catch (e) {
    return null;
  }
}

async function findApp() {
  const cfg = await getCfg();
  if (await probeApp(cfg.apiBase)) { _appBase = cfg.apiBase; return _appBase; }
  const m = /:(\d+)\/?$/.exec(cfg.apiBase || '');
  const start = m ? Number(m[1]) : 8788;
  const head = (cfg.apiBase || 'http://127.0.0.1:8788').replace(/:\d+\/?$/, '');
  for (let i = 1; i <= PROBE_SPAN; i++) {
    const base = head + ':' + (start + i);
    if (await probeApp(base)) {
      _appBase = base;
      // 落盘：Service Worker 会被回收，下次冷启动直接命中，不用重扫
      try { await chrome.storage.local.set({ apiBase: base }); } catch (e) {}
      return base;
    }
  }
  return '';
}

/** 当前地址。探到过就用缓存；没探到过现扫；都失败回落配置值让上层报错 */
async function curBase() {
  if (_appBase) return _appBase;
  const found = await findApp();
  return found || (await getCfg()).apiBase;
}

async function jiliuFetch(path, body) {
  const base = await curBase();
  let res;
  try {
    res = await fetch(apiUrl(base, path), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  } catch (e) {
    _appBase = '';        // 记下的地址失效了（应用退出或换了端口），下次重探
    throw e;
  }
  const data = await res.json().catch(() => ({ ok: false, error: '返回不是 JSON' }));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || ('HTTP ' + res.status));
  }
  return data;
}

async function ping() {
  try {
    const base = await curBase();
    const res = await fetch(apiUrl(base, '/api/health'), { cache: 'no-store' });
    const d = await res.json();
    return { ok: true, version: d.version, base };
  } catch (e) {
    _appBase = '';
    return { ok: false, error: String(e.message || e) };
  }
}

/* ============================================================
 * 进度快照（页面里的悬浮窗用）
 * ============================================================
 * 悬浮窗是**每个标签页**一个，各自 1.5 秒一问。开五个页面就是 3.3 次/秒，
 * 全部原样打到本地服务上没必要 —— 900ms 内的重复请求共用同一份结果。
 *
 * 优先打 /api/progress（1.9.6 新增，只拼速度和百分比）。老版本服务端没有
 * 这个接口，就退回 /api/state 自己蒸馏一遍 —— 扩展不该因为主程序没升级
 * 就整个失效。
 */
const _progCache = { at: 0, val: null, flight: null };

function humanBytes(n) {
  n = Number(n) || 0;
  if (n <= 0) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? Math.round(n) : n.toFixed(n < 10 ? 1 : 0)) + ' ' + u[i];
}

/** /api/state → /api/progress 的形状（给没升级的服务端兜底） */
function distillState(j) {
  const ts = Array.isArray(j.tasks) ? j.tasks : [];
  const counts = {};
  let active = 0, queued = 0, paused = 0, speed = 0, done = 0, size = 0;
  let current = null;
  const rows = [];

  for (const t of ts) {
    const st = String(t.status || '');
    counts[st] = (counts[st] || 0) + 1;
    if (st !== 'running' && st !== 'queued' && st !== 'paused' && st !== 'interrupted') continue;

    const d = Number(t.done) || 0;
    const s = Number(t.size) || 0;
    if (s > 0) { done += Math.min(d, s); size += s; }

    if (st === 'running') {
      active++;
      speed += Number(t.speed) || 0;
      if (!current) {
        current = {
          id: t.id, filename: t.filename || '', title: t.title || '',
          percent: t.percent || 0, speed: t.speed || 0,
          speed_text: t.speed_text || '-', eta_text: t.eta_text || '-',
          done_text: t.done_text || '-', size_text: t.size_text || '-',
          status: st
        };
      }
    } else if (st === 'queued') queued++;
    else if (st === 'paused') paused++;

    if (rows.length < 8) {
      rows.push({
        id: t.id, filename: t.filename || '', title: t.title || '',
        status: st, percent: t.percent || 0, speed_text: t.speed_text || '-'
      });
    }
  }

  const totalSpeed = Number(j.stats && j.stats.total_speed) || speed;
  const pct = size > 0 ? Math.round(done * 1000 / size) / 10 : 0;
  return {
    ok: true, version: j.version, ts: Math.floor(Date.now() / 1000),
    counts, total: ts.length, active, queued, paused,
    speed: Math.round(totalSpeed),
    speed_text: totalSpeed ? humanBytes(totalSpeed) + '/s' : '-',
    done_bytes: Math.round(done), size_bytes: Math.round(size),
    done_text: done ? humanBytes(done) : '-',
    size_text: size ? humanBytes(size) : '-',
    percent: pct, current, tasks: rows
  };
}

async function progressSnapshot() {
  const now = Date.now();
  if (_progCache.at && now - _progCache.at < 900) return _progCache.val;
  if (_progCache.flight) return _progCache.flight;

  _progCache.flight = (async () => {
    let out = null;
    try {
      const base = await curBase();
      let r = await fetch(apiUrl(base, '/api/progress'), { cache: 'no-store' });
      if (r.ok) {
        const j = await r.json().catch(() => null);
        if (j && j.ok) out = j;
      }
      if (!out) {
        // 1.9.5 及以前没有 /api/progress
        r = await fetch(apiUrl(base, '/api/state'), { cache: 'no-store' });
        if (r.ok) {
          const j = await r.json().catch(() => null);
          if (j && j.ok) out = distillState(j);
        }
      }
    } catch (e) {
      _appBase = '';           // 地址失效（应用退出 / 换端口），下次重探
      out = null;
    }
    _progCache.at = Date.now();
    _progCache.val = out;
    _progCache.flight = null;
    return out;
  })();

  return _progCache.flight;
}

function notify(title, message) {
  try {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon128.png',
      title: title,
      message: String(message).slice(0, 220)
    });
  } catch (e) { /* ignore */ }
}

/* ---- 登录态透传（IDM 的核心手法之一）----
 *
 * 为什么必须做：CDN 防盗链查三样 —— Referer / UA / Cookie。扩展能拿到
 * 用户当前会话的这三样，而外部下载器拿不到（除非手动导出 cookies.txt）。
 * 这就是「浏览器扩展能下登录可见的课/会员视频，而裸 yt-dlp 403」的全部原因。
 *
 * 权限：读 cookie 需要 cookies 权限 + 对应域的 host 权限（manifest 里是
 * <all_urls>）。读不到就静默降级 —— 少带个 Cookie 顶多 403，
 * 不能让整个下载请求因为权限问题失败。
 */
async function authHeaders(url, referer) {
  const h = {};
  try { if (navigator.userAgent) h['User-Agent'] = navigator.userAgent; } catch (e) {}
  if (referer) {
    h['Referer'] = referer;
    try {
      const p = new URL(referer);
      h['Origin'] = p.origin;
    } catch (e) {}
  }
  try {
    if (chrome.cookies && url) {
      const list = await chrome.cookies.getAll({ url });
      if (list && list.length) {
        h['Cookie'] = list.map((c) => `${c.name}=${c.value}`).join('; ');
      }
    }
  } catch (e) { /* 没权限就算了 */ }
  return h;
}

/* ---- 应用没开着也能下：协议唤醒 + 起来后补投 ----
 *
 * 为什么不能只靠 fetch：扩展调的是 127.0.0.1 的 HTTP API，汲流没运行
 * 时那个端口根本没人在听，fetch 直接 TypeError。用户点下载链接，界面上
 * 什么都不会发生 —— 这是最伤人的一种失败。
 *
 * 做法（顺序很重要）：
 *   1. 先发一个 `jiliu://wake`（**不带 url**，只负责把应用拉起来）
 *   2. 轮询 /api/health，等它起来
 *   3. 起来之后用**完整 payload** 重新发一次，Cookie/Referer 一个不少
 *
 * 为什么不把整个链接和 Cookie 塞进协议串一次搞定：
 * 协议串会被浏览器记进历史、也会落进 Windows 的 URL 处理日志。
 * Cookie 写进去等于把登录态摊在磁盘上。所以协议串只承担"叫醒"，
 * 真正的请求仍然走本地 HTTP —— 那条路上敏感信息一个字节都不出内存。
 */
function isOffline(e) {
  const m = String((e && e.message) || e || '');
  return e instanceof TypeError || /Failed to fetch|NetworkError|fetch failed|ECONNREFUSED/i.test(m);
}

async function openViaProtocol(spec) {
  const url = chrome.runtime.getURL('launch.html') + '#' + encodeURIComponent(spec);
  await chrome.tabs.create({ url, active: true });
}

/* 所有"发给本地服务"的动作都套这一层。
 *
 * 之前只有建任务（ADD）走了唤醒，视频悬浮按钮（HOTSPOT_PICK）没有 ——
 * 那条路上 fetch 一失败就直接弹"先启动汲流再试"，等于告诉用户
 * "你得自己去打开"，而这恰恰是这版要消灭的动作。两条路请求的是同一个
 * 服务，失败原因也是同一个，就不该有两套待遇。
 *
 * fn 会被调用两次：第一次撞上"没在跑"，唤醒成功后原样重调一次。
 * fn 必须幂等 —— 建任务重发一次会不会建两个？不会：第一次请求根本没
 * 到达服务端（连接被拒），不存在"发了一半"。
 */
async function withWake(fn, opts) {
  const o = opts || {};
  const cfg = await getCfg();
  const allow = o.autoLaunch !== undefined ? o.autoLaunch : cfg.autoLaunch !== false;
  try {
    return await fn(cfg);
  } catch (e) {
    if (!isOffline(e) || !allow) throw e;
    notify('正在启动汲流', o.note || '冷启动需要几秒，起来后自动继续');
    try {
      await openViaProtocol('jiliu://wake');
    } catch (err) {
      // 走到这里说明连"打开中转页"都失败了（扩展被禁用、tab 权限没了），
      // 和"应用没起来"是两件事，得分开说
      throw new Error('打不开唤醒中转页：' + String((err && err.message) || err));
    }
    const alive = await waitForPanel(cfg, o.timeoutMs || 25000);
    if (!alive) {
      throw new Error('汲流没能自动启动。手动打开一次后，之后就能自动唤起了');
    }
    return await fn(cfg);
  }
}

async function waitForPanel(cfg, timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < (timeoutMs || 25000)) {
    _appBase = '';                    // 每轮重探：端口可能因顺延而不同
    if (await findApp()) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

async function sendToJiliu(payload) {
  const cfg = await getCfg();
  if (cfg.preferMotrix) {
    return jiliuFetch('/api/handoff/motrix', { url: payload.url });
  }
  return withWake((c) => jiliuFetch('/api/tasks', payload),
    { note: '冷启动需要几秒，起来后自动开始下载' });
}

/* 带登录态的建任务：媒体在 CDN 上、页面在另一个域上时，
 * referer 要的是**页面**地址而不是媒体地址。 */
async function sendWithAuth(url, pageUrl, extra) {
  const headers = await authHeaders(url, pageUrl);
  return sendToJiliu(Object.assign({}, extra || {}, { url, headers }));
}

/* 播放页解析：先问后端「这页里的流地址是什么」，再挑一条建任务。
 *
 * 为什么不直接 /api/tasks 交给 yt-dlp：免费影视站/短剧站大多不在
 * yt-dlp 的站点名单里，直接交过去就是 Unsupported URL。
 * pageSniff 认的是页面结构（播放器变量 / iframe / 内联脚本），
 * 覆盖面和 IDM 的嗅探同量级，而且不用等播放器把广告播完。 */
async function resolveOnly(pageUrl, pageTitle) {
  const r = await withWake(async () => {
    const headers = await authHeaders(pageUrl, pageUrl);
    return jiliuFetch('/api/pagesniff', { url: pageUrl, headers });
  }, { note: '冷启动需要几秒，起来后自动解析本页' });
  return Object.assign({ ok: true, title: pageTitle }, r);
}

async function resolvePage(pageUrl, pageTitle) {
  const r = await withWake(async () => {
    const headers = await authHeaders(pageUrl, pageUrl);
    return jiliuFetch('/api/pagesniff', { url: pageUrl, headers });
  }, { note: '冷启动需要几秒，起来后自动解析本页' });
  const streams = r.streams || [];
  if (!streams.length) {
    const e = new Error(r.error || '页面里没找到可下载的流地址');
    e.hint = r.hint || '';
    throw e;
  }
  // 后端已按「HLS 优先 + 清晰度降序」排好，第一条就是最优选
  const best = streams[0];
  const task = await sendToJiliu({
    url: best.url,
    kind: 'video',
    title: pageTitle || (r.page && r.page.title) || '',
    headers: best.headers || {},
    from: 'extension-page'
  });
  return { task, best, count: streams.length, streams };
}

/* ============================================================
 * 下载接管
 * ============================================================ */

/** 这个下载要不要交给汲流？返回 {take, reason, plan} */
async function askIntercept(info) {
  try {
    return await jiliuFetch('/api/intercept', {
      url: info.url || '',
      mime: info.mime || '',
      filename: info.filename || '',
      size: info.size || undefined,
      category_hint: info.hint || ''
    });
  } catch (e) {
    // 汲流没启动或判不了 —— 默认放行，绝不因为插件把下载搞坏
    return { take: false, reason: '汲流不可达：' + e.message };
  }
}

function hostOf(u) {
  try { return new URL(u).hostname.toLowerCase(); } catch (e) { return ''; }
}

function shouldSkipHost(host, cfg) {
  if (!host) return false;
  const all = BUILTIN_SKIP.concat(cfg.skipHosts || []);
  return all.some((h) => host === h || host.endsWith('.' + h));
}

// onCreated 时 totalBytes 常常是 0，所以缓存下来再补体积
const pending = new Map();

/** 等一下体积信息。onCreated 时通常还没有，挂起后靠 search 补问几次。 */
async function sizeOf(downloadId, tries = 5, gapMs = 240) {
  for (let i = 0; i < tries; i++) {
    try {
      const hits = await chrome.downloads.search({ id: downloadId });
      const d = hits && hits[0];
      if (d) {
        const n = d.totalBytes || d.fileSize || 0;
        if (n > 0) return n;
      }
    } catch (e) { /* ignore */ }
    await new Promise((r) => setTimeout(r, gapMs));
  }
  return 0;
}

chrome.downloads.onCreated.addListener(async (item) => {
  let cfg;
  try { cfg = await getCfg(); } catch (e) { return; }
  if (cfg.interceptMode === 'off') return;

  const url = item.finalUrl || item.url || '';
  if (!url || /^(blob|data|filesystem):/i.test(url)) return;
  if (item.byExtensionId === chrome.runtime.id) return;

  const host = hostOf(item.url || url);
  if (shouldSkipHost(host, cfg)) return;

  // 关键：先挂起再判断。
  // onCreated 时下载已经在跑了，等一个到本地的 API 往返（还要补体积）
  // 可能已经下掉好几 MB，白下了。先暂停，判完要么继续、要么取消。
  try { chrome.downloads.pause(item.id); } catch (e) { /* 可能来不及暂停，不影响判断 */ }

  const size = item.totalBytes > 0 ? item.totalBytes
             : (item.fileSize > 0 ? item.fileSize : await sizeOf(item.id));

  const verdict = await askIntercept({
    url: url,
    mime: item.mime || '',
    filename: item.filename || '',
    size: size || undefined
  });

  if (!verdict.take) {
    // 放行：继续让浏览器下
    try { chrome.downloads.resume(item.id); } catch (e) { /* 已在下就无所谓 */ }
    return;
  }

  try {
    // 取消并抹掉浏览器那条，再交给汲流，避免同一个文件下两份
    chrome.downloads.cancel(item.id, () => {
      try { chrome.downloads.erase({ id: item.id }); } catch (e) { /* ignore */ }
    });

    const r = await sendToJiliu({
      url: url,
      referer: item.referrer || '',
      from: 'download-intercept',
      auto_classify: true
    });
    const t = r.task || {};
    if (cfg.notifyOnTake) {
      const plan = (t.meta && t.meta.resource) || {};
      notify('汲流已接管下载',
        `${plan.label || '文件'} · ${verdict.reason}\n${(t.filename || t.url || '').slice(0, 100)}`);
    }
  } catch (e) {
    // 接管失败要把浏览器的下载救回来，否则用户就白丢一个文件
    try { chrome.downloads.resume(item.id); } catch (e2) { /* ignore */ }
    notify('汲流接管失败，已交还浏览器', String(e.message || e));
  }
});

/* ============================================================
 * 右键菜单
 * ============================================================ */

function buildMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: 'jiliu-link', title: '用汲流下载此链接', contexts: ['link']
    });
    chrome.contextMenus.create({
      id: 'jiliu-media', title: '用汲流下载此媒体',
      contexts: ['video', 'audio', 'image']
    });
    chrome.contextMenus.create({
      id: 'jiliu-video-page', title: '用汲流解析本页视频', contexts: ['page']
    });
    chrome.contextMenus.create({
      id: 'jiliu-classify', title: '让汲流识别这是什么文件', contexts: ['link']
    });
    chrome.contextMenus.create({
      id: 'jiliu-motrix-link', title: '交给 Motrix（aria2）', contexts: ['link']
    });
  });
}

chrome.runtime.onInstalled.addListener(buildMenus);
chrome.runtime.onStartup.addListener(buildMenus);

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    if (info.menuItemId === 'jiliu-motrix-link') {
      const r = await jiliuFetch('/api/handoff/motrix', { url: info.linkUrl });
      notify('已交给 Motrix', `gid = ${r.gid}`);
      return;
    }
    if (info.menuItemId === 'jiliu-video-page') {
      // 先解析页面结构，拿到真正的流地址再建任务。
      // 直接把这页丢给 yt-dlp 在免费影视站上基本必然 Unsupported URL。
      const r = await resolvePage(tab.url, tab.title);
      const q = r.best.quality || r.best.label || '';
      notify('汲流已接手',
        `解析到 ${r.count} 条流，已下最优的一条${q ? '（' + q + '）' : ''}\n` +
        (r.task.url || '').slice(0, 110));
      return;
    }
    if (info.menuItemId === 'jiliu-classify') {
      const r = await jiliuFetch('/api/classify', { url: info.linkUrl });
      const p = r.plan || {};
      notify('汲流识别结果',
        `${p.label || '未知'}（${p.filetype || p.ext || '-'}）\n` +
        `引擎 ${p.engine} · ${p.concurrency} 线程 · 分片 ${Math.round((p.chunk_size || 0) / 1024)} KB` +
        (p.platform ? `\n站点 ${p.platform}` : ''));
      return;
    }
    const target = info.linkUrl || info.srcUrl;
    if (!target) {
      notify('汲流', '这个位置没取到链接');
      return;
    }
    const r = await sendWithAuth(target, tab ? tab.url : '',
      { referer: tab ? tab.url : '', from: 'context-menu' });
    notify('汲流已接手', (r.task.filename || r.task.url).slice(0, 120));
  } catch (e) {
    notify('汲流：发送失败', e.message + '  （汲流服务启动了吗？）');
  }
});

/* ============================================================
 * 页面媒体嗅探（IDM 同款思路）
 * ============================================================
 * 被动监听每个响应头，认出视频 / 音频 / 流媒体（m3u8 / mpd）就记到
 * 「当前标签页」的清单里。popup 打开时把它和页面里收集到的 <video>
 * 合并去重展示 —— 页面里藏着的、<video> 标签上根本看不到的
 * 真实媒体地址（比如 mse 分片背后的 m3u8）也能被列出来。
 *
 * MV3：监听必须顶层注册。处理器只做便宜的字符串判断，
 * 非媒体请求立刻返回，不给浏览上网添负担。
 */

const SNIFF_MAX = 60;          // 每个标签页最多记多少条（超了淘汰最旧）
const sniffed = new Map();     // tabId -> Map(url -> {url, kind, mime, size, t})

const STREAM_EXT = /\.(m3u8|mpd)(\?|$)/i;
const SEG_EXT = /\.(ts|m4s|mpd)(\?|$)/i;
const AUDIO_EXT = /\.(mp3|m4a|aac|flac|wav|ogg|opus)(\?|$)/i;
const MEDIA_EXT = /\.(mp4|m4v|webm|mkv|mov|avi|flv|ts|m2ts|mp3|m4a|aac|flac|wav|ogg|opus|m3u8|mpd)(\?|$)/i;

function sniffKind(url, mime) {
  if (STREAM_EXT.test(url) || /mpegurl|dash\+xml/i.test(mime)) return 'stream';
  if (AUDIO_EXT.test(url) || /^audio\//i.test(mime)) return 'audio';
  if (MEDIA_EXT.test(url) || /^video\//i.test(mime)) return 'video';
  return '';
}


/* ---- 接管规则（与后端 capture_rules.py 同步）----
 * 服务端会在 /api/capture/rules 给出「例外站点 / 噪音后缀 / 体积阈值」。
 * 嗅探时先过一遍：聊天表情、更新包、统计探针不该出现在"可下载"列表里。
 * 规则缓存 10 分钟 —— onHeadersReceived 是同步回调，不能每次都 await。
 */
let CAP_RULES = { hosts: [], noise: [], maxMb: 0, at: 0 };

async function refreshCaptureRules() {
  try {
    const cfg = await getCfg();
    const r = await fetch(apiUrl(await curBase(), '/api/capture/rules'));
    const j = await r.json();
    if (j && j.ok !== false) {
      CAP_RULES = {
        hosts: Array.isArray(j.hosts) ? j.hosts : [],
        noise: Array.isArray(j.noise_suffix) ? j.noise_suffix : [],
        maxMb: Number(j.max_mb) || 0,
        at: Date.now()
      };
    }
  } catch (_) { /* 面板没开就沿用上一份 */ }
}
try { refreshCaptureRules(); } catch (_) {}
setInterval(refreshCaptureRules, 10 * 60 * 1000);

function captureBlocked(url, size) {
  try {
    const h = new URL(url).hostname.toLowerCase();
    for (const p0 of CAP_RULES.hosts) {
      const p = String(p0 || '').toLowerCase();
      if (!p) continue;
      if (p.startsWith('*.')) {
        const base = p.slice(2);
        if (h === base || h.endsWith('.' + base)) return true;
      } else if (h === p) return true;
    }
    const path = new URL(url).pathname.toLowerCase();
    if (CAP_RULES.noise.some(x => path.endsWith(String(x).toLowerCase()))) return true;
    if (CAP_RULES.maxMb && size && size > CAP_RULES.maxMb * 1024 * 1024) return true;
  } catch (_) {}
  return false;
}

chrome.webRequest.onHeadersReceived.addListener((details) => {
  const u = details.url || '';
  if (!/^https?:/i.test(u)) return;
  if (/^https?:\/\/(127\.0\.0\.1|localhost)([:/]|$)/i.test(u)) return;

  let mime = '';
  for (const h of details.responseHeaders || []) {
    if (h.name.toLowerCase() === 'content-type') { mime = h.value || ''; break; }
  }
  mime = mime.split(';')[0].trim();
  const kind = sniffKind(u, mime);
  if (!kind) return;

  let clen = 0;
  for (const h of details.responseHeaders || []) {
    if (h.name.toLowerCase() === 'content-length') { clen = Number(h.value) || 0; break; }
  }
  // 例外站点 / 噪音后缀 / 超体积：不进「可下载」清单
  if (captureBlocked(u, clen)) return;

  // .ts / .m4s 分片一页能来几百个，把列表刷爆 —— 一律不收，
  // 真正要下的是它们的 m3u8 索引（kind=stream 那条）
  if (SEG_EXT.test(u) && kind === 'video') return;

  let m = sniffed.get(details.tabId);
  if (!m) { m = new Map(); sniffed.set(details.tabId, m); }
  if (m.has(u)) { m.get(u).t = Date.now(); return; }

  let size = 0;
  for (const h of details.responseHeaders || []) {
    if (h.name.toLowerCase() === 'content-length') { size = Number(h.value) || 0; break; }
  }
  m.set(u, { url: u, kind, mime, size, t: Date.now() });

  if (m.size > SNIFF_MAX) {
    let oldest = null, ot = Infinity;
    for (const [k, v] of m) if (v.t < ot) { ot = v.t; oldest = k; }
    if (oldest) m.delete(oldest);
  }
}, { urls: ['http://*/*', 'https://*/*'] }, ['responseHeaders']);

// 换页 / 关页就清 —— 嗅探清单只属于「当前页面」
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === 'loading') sniffed.delete(tabId);
});
chrome.tabs.onRemoved.addListener((tabId) => sniffed.delete(tabId));

/* ============================================================
 * popup 的消息通道
 * ============================================================ */

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg || !msg.type) return;

  switch (msg.type) {
    case 'PING':
      ping().then(reply);
      return true;

    case 'SNIFF_LIST':
      // 网络层嗅到的媒体（与页面 <video> 收集互补）
      try {
        const m = sniffed.get(Number(msg.tabId)) || new Map();
        const items = [...m.values()].sort((a, b) => b.t - a.t);
        reply({ ok: true, items });
      } catch (e) {
        reply({ ok: false, items: [] });
      }
      return true;

    case 'ADD':
      sendToJiliu(msg.payload || {}).then(
        (r) => reply({ ok: true, task: r.task, kind: r.kind }),
        (e) => reply({ ok: false, error: e.message })
      );
      return true;

    case 'HOTSPOT_PICK':
      // 视频悬浮按钮：把链接先放进汲流的「待办提示」，再把主窗口带到
      // 前台 —— 面板轮询到提示后弹出确认框（可改名/选目录二次确认）。
      // 和 ADD 一样先过 withWake：应用没开着时先叫起来再投，而不是
      // 弹一句"请你自己去打开"。
      (async () => {
        const p = msg.payload || {};
        try {
          await withWake((cfg) => jiliuFetch('/api/ui/hint', {
            action: 'new-task', url: p.url, title: p.title || '', from: 'plugin'
          }), { note: '冷启动需要几秒，起来后会弹出下载确认框' });
        } catch (e) {
          notify('没能唤起汲流', e.message);
          reply({ ok: false, error: e.message });
          return;
        }
        let focus = { ok: false };
        try {
          const r = await fetch(apiUrl(await curBase(), '/api/focus'),
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
          focus = await r.json();
        } catch (e) { /* hint 已写入，唤不起也就算了 */ }
        notify('已唤起汲流主程序',
          `请在弹出的确认框里确认下载：\n${(p.title || p.url || '').slice(0, 100)}`);
        reply({ ok: true, focused: focus.ok });
      })();
      return true;

    case 'WAKE':
      // popup 的「启动汲流」按钮：只负责把应用叫起来，立刻返回。
      // 等它真起来（2~4 秒）是 popup 自己的事 —— 放在这里等 25 秒，
      // popup 早就被用户关掉了，reply 也就成了空发。
      openViaProtocol('jiliu://wake')
        .then(() => reply({ ok: true, launching: true }),
              (e) => reply({ ok: false, error: String(e.message || e) }));
      return true;

    case 'PAGE_RESOLVE':
      // popup 的「解析本页」按钮：只解析不建任务，把候选清单还给 popup
      resolveOnly((msg.payload || {}).url || '', (msg.payload || {}).title || '')
        .then((r) => reply(r), (e) => reply({ ok: false, error: e.message, hint: e.hint || '' }));
      return true;

    case 'CLASSIFY':
      jiliuFetch('/api/classify', {
        url: (msg.payload || {}).url || '',
        filename: (msg.payload || {}).filename || ''
      }).then(
        (r) => reply({ ok: true, plan: r.plan }),
        (e) => reply({ ok: false, error: e.message })
      );
      return true;

    case 'INTERCEPT_TEST':
      askIntercept(msg.payload || {}).then(
        (r) => reply({ ok: true, verdict: r }),
        (e) => reply({ ok: false, error: e.message })
      );
      return true;

    case 'HANDOFF_MOTRIX':
      jiliuFetch('/api/handoff/motrix', { url: (msg.payload || {}).url }).then(
        (r) => reply({ ok: true, gid: r.gid }),
        (e) => reply({ ok: false, error: e.message })
      );
      return true;

    case 'GET_CFG':
      getCfg().then((c) => reply({ ok: true, cfg: c }));
      return true;

    case 'PROGRESS':
      // 页面里的进度悬浮窗轮询这一个
      progressSnapshot().then(
        (p) => reply(p ? { ok: true, progress: p } : { ok: false, error: '汲流不可达' }),
        (e) => reply({ ok: false, error: String((e && e.message) || e) })
      );
      return true;

    case 'FOCUS':
      // 悬浮窗上的「↗」：把主窗口带到前台
      (async () => {
        try {
          const base = await curBase();
          const r = await fetch(apiUrl(base, '/api/focus'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}'
          });
          const j = await r.json().catch(() => ({ ok: false }));
          reply({ ok: !!(j && j.ok), how: j && j.how });
        } catch (e) {
          reply({ ok: false, error: String((e && e.message) || e) });
        }
      })();
      return true;

    case 'OPEN_APP':
      (async () => {
        try {
          const base = await curBase();
          await chrome.tabs.create({ url: base });
          reply({ ok: true, base });
        } catch (e) {
          reply({ ok: false, error: String((e && e.message) || e) });
        }
      })();
      return true;

    case 'SET_CFG':
      chrome.storage.local.set(msg.cfg || {})
        .then(() => getCfg().then((c) => reply({ ok: true, cfg: c })));
      return true;

    default:
      return;
  }
});
