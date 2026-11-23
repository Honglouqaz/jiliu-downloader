/* 扩展侧唤醒逻辑的真实执行验证（Node + mock chrome API）。
 *
 * 为什么不在浏览器里点：装 Playwright 浏览器要另下一份 Chromium，
 * 而这轮要验的其实是「fetch 失败之后有没有去叫醒应用」——
 * 那一段是纯 JS，用 Node 把 background.js 原样跑起来、给一个假的
 * chrome 命名空间、后面接真的 HTTP 服务，比点界面更贴近事实：
 * 网络请求是真的，端口扫描是真的，只有浏览器 API 是假的。
 *
 * 四个场景：
 *   A 应用在线（8788）        → 直接建任务，不开中转页
 *   B 应用没开                → 开中转页（= 走了 jiliu://wake）
 *   C 8788 被别的程序占着，
 *     汲流顺延到 8789         → 自动发现端口并成功
 *   D 悬浮按钮（HOTSPOT_PICK）→ 和 ADD 一样先叫醒再投
 *
 *   node scripts/ext_logic_check.mjs
 */

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SRC = path.join(ROOT, 'extension', 'background.js');

let ok = 0, fail = 0;
function check(name, cond, extra = '') {
  if (cond) { ok++; console.log(`  ok   ${name}`); }
  else { fail++; console.log(`  FAIL ${name}   ${JSON.stringify(extra)}`); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------- 假服务 ---------------- */
function serve(port, { app, onTask }) {
  const srv = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      const send = (obj) => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(obj));
      };
      if (req.url.startsWith('/api/health')) {
        send({ ok: true, app, version: 'test' });
      } else if (req.url.startsWith('/api/tasks')) {
        if (onTask) onTask(body);
        send({ ok: true, task: { id: 't1', url: 'x' }, kind: 'http' });
      } else if (req.url.startsWith('/api/ui/hint')) {
        if (onTask) onTask(body);
        send({ ok: true });
      } else if (req.url.startsWith('/api/focus')) {
        send({ ok: true });
      } else {
        send({ ok: true });
      }
    });
  });
  return new Promise((resolve, reject) => {
    srv.once('error', reject);
    srv.listen(port, '127.0.0.1', () => resolve(srv));
  });
}

/* ---------------- 万能 chrome 替身 ---------------- */
/* 关键点：同一个属性必须每次返回同一个对象。
 * 早期版本每次 get 都新建一个 magic，于是
 *   chrome.runtime.onMessage.addListener = fn
 * 写在了一个临时对象上，脚本后面再取 chrome.runtime.onMessage
 * 拿到的是另一个新的 —— 监听根本没注册上，测出来的"没反应"
 * 是替身的 bug，不是被测代码的 bug。 */
function magic(name = '') {
  const kids = new Map();
  const f = function () { return magic(name + '()'); };
  return new Proxy(f, {
    get(t, k) {
      if (typeof k === 'symbol') return undefined;
      if (k === 'then') return undefined;
      if (Object.prototype.hasOwnProperty.call(t, k)) return t[k];
      if (!kids.has(k)) kids.set(k, magic(name + '.' + String(k)));
      return kids.get(k);
    },
  });
}

function makeChrome(state) {
  const chrome = magic();
  const handlers = new Map();

  chrome.storage = {
    local: {
      get: async (def) => ({ ...(def || {}), ...state.cfg }),
      set: async (obj) => { Object.assign(state.cfg, obj); Object.assign(state.saved, obj); },
    },
  };
  chrome.runtime.getURL = (p) => 'chrome-extension://TESTEXT/' + p;
  chrome.runtime.getManifest = () => ({ version: '1.9.0' });
  chrome.tabs.create = async (arg) => { state.tabs.push(arg.url); return { id: state.tabs.length }; };
  chrome.tabs.query = async () => [{ id: 1, url: 'https://example.com/' }];
  chrome.notifications.create = (o) => { state.notifies.push(o.title + '|' + o.message); };
  chrome.cookies.getAll = async () => [];
  chrome.runtime.onMessage.addListener = (fn) => handlers.set('msg', fn);
  return { chrome, handlers };
}

function loadBackground(state) {
  const { chrome, handlers } = makeChrome(state);
  const code = fs.readFileSync(SRC, 'utf8');
  const ctx = vm.createContext({
    chrome, fetch, console, setTimeout, clearTimeout,
    // 定时器不用真跑：这些是 SW 里的周期任务，跑起来只会干扰断言
    setInterval: () => 0, clearInterval: () => {},
    URL, JSON, Object, String, Number, Boolean, Array, Math, Error, Promise, RegExp,
    globalThis: null,
  });
  ctx.globalThis = ctx;
  // 把要看的东西从脚本作用域里带出来
  const tail = `
;globalThis.__api = {
  findApp, curBase, sendToJiliu, withWake, probeApp, discoverReset: () => { _appBase = ""; },
  ping, apiUrl, DEFAULTS
};`;
  vm.runInContext(code + tail, ctx, { filename: 'background.js' });
  return { api: ctx.__api, handlers, chrome };
}

/* ---------------- 场景 ---------------- */
const cfgBase = { apiBase: 'http://127.0.0.1:8788', autoLaunch: true };
const fresh = () => ({ cfg: { ...cfgBase }, saved: {}, tabs: [], notifies: [] });

console.log('\n[A] 应用在线（8788）');
{
  const st = fresh();
  const tasks = [];
  const srv = await serve(8788, { app: '汲流', onTask: (b) => tasks.push(b) });
  try {
    const { api } = loadBackground(st);
    const r = await api.sendToJiliu({ url: 'https://example.com/a.zip', from: 'protocol' });
    check('建任务成功', !!(r && r.task), r);
    check('没有开中转页', st.tabs.length === 0, st.tabs);
    check('请求真的到达服务端', tasks.length === 1, tasks.length);
  } finally { srv.close(); }
}

console.log('\n[B] 应用没开（端口没人听）');
{
  const st = fresh();
  const { api } = loadBackground(st);
  // 不等它等满 25 秒：3 秒后看它有没有去开中转页
  const p = api.sendToJiliu({ url: 'https://example.com/b.zip' }).catch((e) => e);
  await sleep(1200);
  check('开了中转页（= 走了 jiliu://wake）', st.tabs.length === 1, st.tabs);
  check('中转页带的是 launch.html', /launch\.html/.test(st.tabs[0] || ''), st.tabs[0]);
  check('中转页里传的是 jiliu://wake，不是整条链接',
    decodeURIComponent(String(st.tabs[0] || '').split('#')[1] || '') === 'jiliu://wake',
    st.tabs[0]);
  check('有「正在启动」的通知', st.notifies.some((n) => n.includes('正在启动')), st.notifies);
  const got = await p;
  check('最终如实报错（没静默吞掉）', got instanceof Error, String(got));
}

console.log('\n[C] 8788 被别的程序占着，汲流顺延到 8789');
{
  const st = fresh();
  const tasks = [];
  const other = await serve(8788, { app: 'someone-else' });
  const jl = await serve(8789, { app: '汲流', onTask: (b) => tasks.push(b) });
  try {
    const { api } = loadBackground(st);
    const found = await api.findApp();
    check('扫到了 8789', found === 'http://127.0.0.1:8789', found);
    check('不把 8788 的陌生服务当汲流',
      (await api.probeApp('http://127.0.0.1:8788')) === null);

    const r = await api.sendToJiliu({ url: 'https://example.com/c.zip' });
    check('顺着新端口建任务成功', !!(r && r.task), r);
    check('请求落在 8789 上', tasks.length === 1, tasks.length);
    check('地址被记住（下次不用再扫）', st.saved.apiBase === 'http://127.0.0.1:8789', st.saved);
    check('这种情况不需要开中转页', st.tabs.length === 0, st.tabs);
  } finally { other.close(); jl.close(); }
}

console.log('\n[D] 视频悬浮按钮（HOTSPOT_PICK）—— 用户报的那条路');
{
  const st = fresh();
  const { api, handlers } = loadBackground(st);
  const listener = handlers.get('msg');
  check('消息监听注册了', typeof listener === 'function');

  const replies = [];
  const ret = listener({ type: 'HOTSPOT_PICK', payload: { url: 'https://example.com/d.mp4', title: 'd' } },
    {}, (r) => replies.push(r));
  check('异步回复（返回 true）', ret === true, ret);
  await sleep(1200);
  check('开了中转页（以前这里直接弹「请先启动」）', st.tabs.length === 1, st.tabs);
  // 关键差别：修复前这里立刻回 {ok:false}，界面弹一句"请你自己去打开"。
  // 修复后在等应用起来（25 秒窗口），所以此刻**不该**有失败回复。
  check('没有立刻把「请先启动」甩给界面（在等应用起来）',
    replies.length === 0, replies);
  check('通知说的是「正在启动」而不是「未启动」',
    st.notifies.some((n) => n.includes('正在启动')), st.notifies);
}

console.log(`\n结果: ${ok}/${ok + fail} 通过`);
process.exit(fail ? 1 : 0);
