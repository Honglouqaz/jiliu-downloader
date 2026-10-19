'use strict';

const $ = (s) => document.querySelector(s);

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
  } else {
    dot.className = 'dot off';
    $('#status').textContent = '服务未启动';
  }
  return !!(r && r.ok);
}

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
  }
  loadList();
})();
