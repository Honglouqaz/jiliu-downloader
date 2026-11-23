/* 协议唤起的中转页。
 *
 * 为什么需要这一页，而不是在 background 里直接跳：
 * MV3 的 Service Worker 没有 location，也没有 DOM，没法发起一次
 * "页面导航"。而自定义协议只有在**页面导航**里才会被浏览器交给
 * 操作系统的协议处理器 —— 从扩展后台 fetch 一个 jiliu:// 只会报
 * ERR_UNKNOWN_URL_SCHEME，什么都不会发生。
 *
 * Chrome 还有一条限制：没有用户手势的协议导航会被静默拦掉。
 * 所以这里先自动试一次，拦了就靠下面的按钮（点击是明确手势）。
 *
 * 什么时候关这一页，只有服务自己说了算：轮询本机 /api/health，
 * 探到才关。之前是导航后 2.6 秒无条件关 —— 导航一旦被拦，按钮
 * 刚露出来 1 秒页面就自己没了，用户连点都来不及点，然后扩展那头
 * 傻等到超时，最后甩一句"请手动启动"。
 */
'use strict';

const DEF_BASE = 'http://127.0.0.1:8788';
const spec = decodeURIComponent((location.hash || '').slice(1) || '');
const $ = (id) => document.getElementById(id);

function go() {
  try {
    location.href = spec;
  } catch (e) {
    /* 被浏览器拦掉时不会抛错，只会什么都不发生 —— 交给下面的兜底 */
  }
}

async function cfgBase() {
  try {
    const got = await chrome.storage.local.get({ apiBase: DEF_BASE });
    return String(got.apiBase || DEF_BASE);
  } catch (e) {
    return DEF_BASE;
  }
}

async function ready(base) {
  try {
    const r = await fetch(base.replace(/\/+$/, '') + '/api/health', { cache: 'no-store' });
    if (!r.ok) return false;
    const j = await r.json();
    // 认 app：8788 上可能是别的本地服务，端口被占时汲流会顺延
    return !!(j && j.ok && /汲流|jiliu/i.test(String(j.app || '')));
  } catch (e) {
    return false;
  }
}

/* 端口可能因为 8788 被占而顺延，所以同 background 一样扫一段范围 */
async function anyReady() {
  const base = await cfgBase();
  if (await ready(base)) return true;
  const m = /:(\d+)\/?$/.exec(base);
  const start = m ? Number(m[1]) : 8788;
  const head = base.replace(/:\d+\/?$/, '');
  for (let i = 1; i <= 9; i++) {
    if (await ready(head + ':' + (start + i))) return true;
  }
  return false;
}

function closeSelf() {
  try { window.close(); } catch (e) { /* 不是脚本开的标签页关不掉，留着也不碍事 */ }
}

if (!spec) {
  $('msg').textContent = '没有收到要唤起的地址。';
  $('hint').textContent = '';
  const sp = document.querySelector('.spin');
  if (sp) sp.style.display = 'none';
} else {
  $('spec').textContent = spec;
  $('again').addEventListener('click', go);

  // 自动试一次
  setTimeout(go, 250);

  const t0 = Date.now();
  let showed = false;
  (async function tick() {
    if (await anyReady()) { closeSelf(); return; }

    const age = Date.now() - t0;
    if (!showed && age > 1600) {
      showed = true;
      const sp = document.querySelector('.spin');
      if (sp) sp.style.display = 'none';
      $('again').style.display = 'inline-block';
      $('msg').textContent = '浏览器没有自动唤起汲流，点下面的按钮再试一次。';
      $('hint').textContent = '也可以手动打开汲流；它起来后本页会自动关闭';
    }
    if (age > 90000) {          // 别无限轮询下去，白耗电
      $('hint').textContent = '等了 90 秒还没连上汲流。先手动打开一次，之后就能自动唤起了。';
      return;
    }
    setTimeout(tick, 400);
  })();
}
