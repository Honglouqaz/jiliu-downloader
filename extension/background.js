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
  notifyOnTake: true
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

async function jiliuFetch(path, body) {
  const cfg = await getCfg();
  const res = await fetch(apiUrl(cfg.apiBase, path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  const data = await res.json().catch(() => ({ ok: false, error: '返回不是 JSON' }));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || ('HTTP ' + res.status));
  }
  return data;
}

async function ping() {
  const cfg = await getCfg();
  try {
    const res = await fetch(apiUrl(cfg.apiBase, '/api/health'));
    const d = await res.json();
    return { ok: true, version: d.version };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
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

async function sendToJiliu(payload) {
  const cfg = await getCfg();
  if (cfg.preferMotrix) {
    return jiliuFetch('/api/handoff/motrix', { url: payload.url });
  }
  return jiliuFetch('/api/tasks', payload);
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
      const r = await sendToJiliu({ url: tab.url, kind: 'video', title: tab.title });
      notify('汲流已接手', '解析中：' + (r.task.title || r.task.url));
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
    const r = await sendToJiliu({
      url: target, referer: tab ? tab.url : '', from: 'context-menu'
    });
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
    const r = await fetch(apiUrl(cfg.apiBase, '/api/capture/rules'));
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
      // 两步都尽力而为：汲流没启动时至少给出能看懂的通知。
      (async () => {
        const p = msg.payload || {};
        try {
          await jiliuFetch('/api/ui/hint', {
            action: 'new-task', url: p.url, title: p.title || '', from: 'plugin'
          });
        } catch (e) {
          notify('汲流未启动', `无法唤起主程序（${e.message}）。先启动汲流再试。`);
          reply({ ok: false, error: e.message });
          return;
        }
        let focus = { ok: false };
        try {
          const r = await fetch(apiUrl((await getCfg()).apiBase, '/api/focus'),
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
          focus = await r.json();
        } catch (e) { /* hint 已写入，唤不起也就算了 */ }
        notify('已唤起汲流主程序',
          `请在弹出的确认框里确认下载：\n${(p.title || p.url || '').slice(0, 100)}`);
        reply({ ok: true, focused: focus.ok });
      })();
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

    case 'SET_CFG':
      chrome.storage.local.set(msg.cfg || {})
        .then(() => getCfg().then((c) => reply({ ok: true, cfg: c })));
      return true;

    default:
      return;
  }
});
