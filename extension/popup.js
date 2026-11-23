'use strict';

const $ = (s) => document.querySelector(s);

// 扩展版本摆出来：改完代码要回 chrome://extensions 重新加载才生效，
// 用户看这里就知道自己跑的是不是新的（不用猜"到底有没有更新"）
try {
  $('#extVer').textContent = 'v' + chrome.runtime.getManifest().version;
} catch (e) { /* 取不到就算了，不影响功能 */ }

function toast(msg, cls) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = cls || '';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function send(msg) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(msg, (r) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
      } else {
        resolve(r || { ok: false, error: '无响应' });
      }
    });
  });
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

/* ---------------- 状态 ---------------- */

async function refreshStatus() {
  const r = await send({ type: 'PING' });
  const dot = $('#dot');
  if (r && r.ok) {
    dot.className = 'dot on';
    $('#status').textContent = '服务在线 · v' + (r.version || '?');
    $('#btnWake').style.display = 'none';
  } else {
    dot.className = 'dot off';
    $('#status').textContent = '服务未启动';
    // 只有连不上时才露出「启动」—— 在线时摆一个没用的按钮只会碍事
    $('#btnWake').style.display = '';
  }
  return !!(r && r.ok);
}

/* 一键启动：走 jiliu:// 协议，不需要用户自己去开始菜单找图标。
 * 冷启动 2~4 秒，所以轮询等待，最长 25 秒。 */
$('#btnWake').addEventListener('click', async () => {
  const b = $('#btnWake');
  b.disabled = true;
  const old = b.textContent;
  b.textContent = '启动中…';
  try {
    const r = await send({ type: 'WAKE' });
    if (!r || !r.ok) { toast((r && r.error) || '唤起失败', 'err'); return; }
    for (let i = 0; i < 50; i++) {
      await new Promise((x) => setTimeout(x, 500));
      if (await refreshStatus()) { toast('汲流已启动', 'ok'); await loadList(); return; }
    }
    toast('没能启动，请手动打开一次', 'err');
  } finally {
    b.disabled = false;
    b.textContent = old;
  }
});

/* ---------------- 媒体列表 ---------------- */

async function loadList() {
  const tab = await activeTab();
  if (!tab || !tab.id) { $('#list').innerHTML = '<div class="empty">没有活动标签页</div>'; return; }

  // 两个来源合并：
  //   1) content.js 收集的页面 <video>/<audio>（要点过播放才出现）
  //   2) background 网络层嗅探（IDM 同款：响应头认媒体，<video> 看不见的
  //      m3u8 / 真实媒体地址也能抓到）—— 按 URL 去重
  const sniff = await send({ type: 'SNIFF_LIST', tabId: tab.id });
  const netItems = (sniff && sniff.ok) ? (sniff.items || []) : [];

  chrome.tabs.sendMessage(tab.id, { type: 'JILIU_LIST' }, (resp) => {
    if (chrome.runtime.lastError || !resp || !resp.ok) {
      renderItems(netItems);
      return;
    }
    const seen = new Map();
    for (const it of (resp.items || [])) seen.set(it.url, it);
    for (const it of netItems) if (!seen.has(it.url)) seen.set(it.url, it);
    renderItems([...seen.values()]);
  });
}

function renderItems(items) {
  if (!items.length) {
    $('#list').innerHTML = '<div class="empty">还没发现媒体<br>点播放让页面先加载一下，再按「重扫」<br>网络嗅探是自动的，视频站的 m3u8 会直接出现在这里</div>';
    return;
  }
  $('#list').innerHTML = items.slice(0, 60).map((it, i) => {
    const mb = it.size ? ' · ' + Math.max(1, Math.round(it.size / 1048576)) + ' MB' : '';
    return `<div class="it">
      <span class="k ${esc(it.kind)}">${esc(it.kind)}</span>
      <span class="u" title="${esc(it.url)}">${esc(decodeURIComponent(it.url.split('/').pop() || it.url).slice(0, 60))}${esc(mb)}</span>
      <button data-i="${i}">下载</button>
    </div>`;
  }).join('');
  $('#list')._items = items;
}

$('#list').addEventListener('click', async (ev) => {
  const b = ev.target.closest('button[data-i]');
  if (!b) return;
  const items = $('#list')._items || [];
  const it = items[Number(b.dataset.i)];
  if (!it) return;
  b.disabled = true;
  const tab = await activeTab();
  const r = await send({
    type: 'ADD',
    payload: {
      url: it.url,
      referer: tab ? tab.url : '',
      kind: it.kind === 'stream' ? 'video' : 'auto',
      filename: '',
      title: it.title || '',
      from: 'extension'
    }
  });
  b.disabled = false;
  if (r.ok) toast('已加入下载：' + (r.task.filename || r.task.url).slice(0, 60), 'ok');
  else toast('失败：' + r.error, 'err');
});

/* ---------------- 解析播放页 ----------------
 *
 * 和上面的「本页发现的媒体」互补：
 *   嗅探 = 播放器**已经请求过**的地址（要等广告播完、要开播放）
 *   解析 = 直接从 HTML 里抠（不用点播放，免费影视站/短剧站大多能命中）
 * 两条路都堵上，才谈得上"IDM 级"的覆盖率。
 */
$('#btnParse').addEventListener('click', async () => {
  const tab = await activeTab();
  if (!tab || !tab.url) return toast('没有活动标签页', 'err');
  const btn = $('#btnParse');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '解析中…';
  $('#list').innerHTML = '<div class="empty">正在打开页面找播放地址…</div>';
  try {
    const r = await send({ type: 'PAGE_RESOLVE', payload: { url: tab.url, title: tab.title } });
    if (!r.ok) {
      $('#list').innerHTML = '<div class="empty">' + esc(r.error || '解析失败') +
        (r.hint ? '<br><br>' + esc(r.hint) : '') + '</div>';
      return toast('解析失败：' + (r.error || ''), 'err');
    }
    const items = (r.streams || []).map((s) => ({
      url: s.url,
      // hls/dash 走视频解析（yt-dlp 直链模式），整段文件走自动
      kind: s.kind === 'hls' || s.kind === 'dash' ? 'stream' : 'video',
      size: 0,
      title: [s.quality || s.label, s.how].filter(Boolean).join(' · ')
    }));
    renderItems(items);
    toast('找到 ' + items.length + ' 条可下载地址', 'ok');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

/* ---------------- 手动添加 ---------------- */

$('#btnAdd').addEventListener('click', async () => {
  const url = $('#url').value.trim();
  if (!url) return toast('先填链接', 'err');
  const kind = $('#kind').value;
  const tab = await activeTab();
  const payload = { url, referer: tab ? tab.url : '', from: 'extension-popup' };
  if (kind !== 'auto') payload.kind = kind;
  const r = await send({ type: 'ADD', payload });
  if (r.ok) { toast('已加入下载', 'ok'); $('#url').value = ''; }
  else toast('失败：' + r.error, 'err');
});

/** 只识别不下载：把类型与将要采用的策略摊开给用户看 */
$('#btnClassify').addEventListener('click', async () => {
  const url = $('#url').value.trim();
  if (!url) return toast('先填链接', 'err');
  $('#planBox').textContent = '识别中…';
  const r = await send({ type: 'CLASSIFY', payload: { url } });
  if (!r.ok) { $('#planBox').textContent = ''; return toast('识别失败：' + r.error, 'err'); }
  const p = r.plan || {};
  const kb = (n) => (n ? Math.round(n / 1024) + ' KB' : '自适应');
  $('#planBox').innerHTML =
    `类型 <b style="color:var(--text)">${esc(p.label || '未知')}</b>` +
    (p.filetype ? `（${esc(p.filetype)}）` : '') +
    (p.platform ? ` · 站点 ${esc(p.platform)}` : '') +
    `<br>引擎 ${esc(p.engine || '-')} · ${p.concurrency || '-'} 线程 · 分片 ${kb(p.chunk_size)}` +
    (p.verify_hash ? ' · 下完校验 SHA256' : '') +
    (p.needs_extractor ? '<br><span style="color:var(--warn)">需要 yt-dlp 解析真实媒体地址</span>' : '') +
    ((p.warnings || []).length
      ? '<br><span style="color:var(--warn)">⚠ ' + p.warnings.map(esc).join('<br>⚠ ') + '</span>'
      : '');
});

$('#btnMotrix').addEventListener('click', async () => {
  const url = $('#url').value.trim();
  if (!url) return toast('先填链接', 'err');
  const r = await send({ type: 'HANDOFF_MOTRIX', payload: { url } });
  if (r.ok) toast('已交给 Motrix，gid=' + r.gid, 'ok');
  else toast('失败：' + r.error, 'err');
});

$('#btnRescan').addEventListener('click', async () => {
  const tab = await activeTab();
  if (tab && tab.id) {
    chrome.tabs.sendMessage(tab.id, { type: 'JILIU_CLEAR' }, () => { void chrome.runtime.lastError; });
    setTimeout(loadList, 400);
    toast('已清空并重扫，等 1~2 秒');
  }
});

/* ---------------- 设置 ---------------- */

const MODE_TEXT = { off: '已关闭', smart: '智能接管中', all: '全部接管中' };

$('#interceptMode').addEventListener('change', async () => {
  // 改模式立即生效，不用点保存 —— 这是个高频开关
  await send({ type: 'SET_CFG', cfg: { interceptMode: $('#interceptMode').value } });
  $('#icTag').textContent = MODE_TEXT[$('#interceptMode').value] || '';
  toast('接管模式：' + (MODE_TEXT[$('#interceptMode').value] || ''), 'ok');
});

$('#btnSaveCfg').addEventListener('click', async () => {
  const cfg = {
    apiBase: $('#apiBase').value.trim() || 'http://127.0.0.1:8788',
    preferMotrix: $('#preferMotrix').checked,
    notifyOnTake: $('#notifyOnTake').checked,
    skipHosts: $('#skipHosts').value.split('\n').map((s) => s.trim()).filter(Boolean)
  };
  const r = await send({ type: 'SET_CFG', cfg });
  if (r.ok) { toast('已保存', 'ok'); refreshStatus(); }
  else toast('保存失败', 'err');
});

/* ---------------- 页面组件 ---------------- */

/** 两个开关立即生效：content script 监听 storage 变化，不用刷新页面。 */
async function setFeat(key, el) {
  await send({ type: 'SET_CFG', cfg: { [key]: el.checked } });
  $('#featTag').textContent = featSummary();
  toast(el.checked ? '已开启' : '已关闭', 'ok');
}

function featSummary() {
  const h = $('#hotspotButton').checked;
  const w = $('#floatWidget').checked;
  if (h && w) return '两个都开';
  if (!h && !w) return '都关着';
  return h ? '仅悬浮按钮' : '仅进度窗';
}

$('#hotspotButton').addEventListener('change', () => setFeat('hotspotButton', $('#hotspotButton')));
$('#floatWidget').addEventListener('change', () => setFeat('floatWidget', $('#floatWidget')));

$('#btnResetPos').addEventListener('click', async () => {
  await new Promise((r) => chrome.storage.local.remove(['hotspot_pos', 'float_pos'], r));
  toast('位置已还原', 'ok');
});

/* ---------------- 启动 ---------------- */

(async function init() {
  await refreshStatus();
  const c = await send({ type: 'GET_CFG' });
  if (c.ok) {
    $('#apiBase').value = c.cfg.apiBase;
    $('#preferMotrix').checked = !!c.cfg.preferMotrix;
    $('#notifyOnTake').checked = c.cfg.notifyOnTake !== false;
    $('#interceptMode').value = c.cfg.interceptMode || 'smart';
    $('#icTag').textContent = MODE_TEXT[$('#interceptMode').value] || '';
    $('#skipHosts').value = (c.cfg.skipHosts || []).join('\n');
    $('#hotspotButton').checked = c.cfg.hotspotButton !== false;
    $('#floatWidget').checked = c.cfg.floatWidget !== false;
    $('#featTag').textContent = featSummary();
  }
  loadList();
})();
