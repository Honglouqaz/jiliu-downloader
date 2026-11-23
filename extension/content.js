/* ============================================================
 * content.js —— 隔离世界：链接收集 + 视频悬浮按钮 + 进度悬浮窗
 *
 * 三件事：
 *   1. 收集候选链接（inject.js 主世界消息 + DOM 扫描），等 popup 来问。
 *   2. IDM 风格的视频悬浮按钮。**贴在视频框外面**（上/右/下/左依次尝试），
 *      可拖动，位置记在 chrome.storage 里。以前放在视频右上角内侧，
 *      正好压住播放器的「设置/全屏/弹幕」一排按钮 —— 主人点名要挪出去。
 *   3. 进度悬浮窗：总速度 / 当前文件 / 总进度 三个视图每 4 秒自动轮转，
 *      常驻在页面右下角，可拖动、可收起。
 *
 * 为什么两样东西都塞进一个 shadow DOM：
 *   - 页面 CSS 五花八门（`div{...}`、`* { box-sizing }`、播放器自带的
 *     reset），不隔离就得一路 !important 打地鼠；
 *   - 一个 `<div id="jiliu-layer">` 全屏铺开、pointer-events:none，
 *     既不会挡住网页点击，也不会让页面布局抖一下。
 *
 * 只在顶层页面挂进度窗（iframe 里也挂的话，一个页面能长出七八个），
 * 悬浮按钮则每个 frame 都要有 —— 有的站播放器整个在 iframe 里。
 * ============================================================ */
(function () {
  'use strict';

  var TOP = (window.top === window.self);
  var IS_APP_PAGE = /^(127\.0\.0\.1|localhost|\[::1\])$/.test(location.hostname);

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  /** 消息封装：扩展刚重载过时会抛 "context invalidated"，静默返回 null。 */
  function send(msg) {
    return new Promise(function (resolve) {
      try {
        chrome.runtime.sendMessage(msg, function (r) {
          if (chrome.runtime.lastError) { resolve(null); return; }
          resolve(r || null);
        });
      } catch (e) { resolve(null); }
    });
  }

  /** 文件名截断：保留扩展名，中间用省略号。悬浮窗只有 200px 宽。 */
  function shortName(s, n) {
    s = String(s || '');
    if (!s) return '';
    if (s.length <= n) return s;
    var dot = s.lastIndexOf('.');
    var ext = (dot > 0 && s.length - dot <= 6) ? s.slice(dot) : '';
    var keep = Math.max(4, n - ext.length - 1);
    return s.slice(0, keep) + '…' + ext;
  }

  /* ============================================================
   * 一、链接收集（原有能力，逻辑未动）
   * ============================================================ */

  var items = new Map();
  var MEDIA_RE = /\.(mp4|m4v|mkv|webm|flv|mov|avi|ts|m4s|mp3|m4a|aac|flac|wav|ogg|opus|m3u8|mpd)(\?|#|$)/i;
  // 用于列表标签的轻量分类（离线，不查服务）。
  // 下载时服务端还会用「响应头 + 文件头魔数」重判一遍，这里只是为了好认。
  var EXT_CAT = {
    stream: 'm3u8 mpd f4m ism',
    audio: 'mp3 m4a aac flac wav ogg opus wma aiff ape',
    image: 'jpg jpeg png gif webp bmp tiff svg avif heic ico',
    archive: 'zip rar 7z tar gz bz2 xz zst tgz cab lz4',
    app: 'exe msi msix appx apk apks xapk ipa dmg pkg deb rpm appimage jar',
    disk: 'iso img vhd vhdx vmdk',
    doc: 'pdf doc docx xls xlsx ppt pptx epub mobi txt md rtf csv',
    dataset: 'pt pth ckpt safetensors onnx gguf h5 npy npz parquet',
    font: 'ttf otf woff woff2'
  };

  function extOf(url) {
    try {
      var p = new URL(url, location.href).pathname;
      var base = p.split('/').pop() || '';
      var i = base.lastIndexOf('.');
      return i < 0 ? '' : base.slice(i + 1).toLowerCase();
    } catch (e) { return ''; }
  }

  function classify(url) {
    var e = extOf(url);
    for (var k in EXT_CAT) {
      if (EXT_CAT[k].split(' ').indexOf(e) >= 0) return k;
    }
    if (/\.(mp4|m4v|mkv|webm|flv|mov|avi|ts|m4s)(\?|#|$)/i.test(url)) return 'media';
    return 'other';
  }

  function add(url, source, title, page) {
    if (!url || typeof url !== 'string') return;
    if (url.indexOf('blob:') === 0 || url.indexOf('data:') === 0) return;
    if (!MEDIA_RE.test(url)) return;
    var existing = items.get(url);
    if (existing) { existing.hits++; return; }
    items.set(url, {
      url: url,
      kind: classify(url),
      source: source || 'dom',
      title: title || document.title || '',
      page: page || location.href,
      hits: 1,
      ts: Date.now()
    });
    if (items.size > 400) {
      var oldest = null;
      items.forEach(function (v, k) { if (!oldest || v.ts < items.get(oldest).ts) oldest = k; });
      if (oldest) items.delete(oldest);
    }
  }

  /* ---- 收主世界的消息 ---- */
  window.addEventListener('message', function (ev) {
    if (ev.source !== window) return;
    var d = ev.data;
    if (!d || d.__jiliu !== true || !d.url) return;
    add(d.url, d.initiator || 'network', d.title, d.page);
  }, false);

  /* ---- DOM 扫描 ---- */

  /* 一次遍历收全部目标元素。以前对 5 个选择器各跑一遍
   * deepCollect，每次内部还要 querySelectorAll('*') 全量遍历 ——
   * 一次 scan 等于 5 次全 DOM 走查，视频站上 CPU 肉眼可见。 */
  function deepWalk(root, out, depth) {
    if (!root || depth > 12 || out.length > 2000) return;
    var els;
    try { els = root.querySelectorAll('video,audio,source,a[href],link[href]'); }
    catch (e) { return; }
    for (var i = 0; i < els.length; i++) out.push(els[i]);
    var all;
    try { all = root.querySelectorAll('*'); } catch (e) { return; }
    for (var j = 0; j < all.length; j++) {
      var sr = all[j].shadowRoot;          // open shadow 才拿得到
      if (sr) deepWalk(sr, out, depth + 1);
    }
  }

  var deepVids = [];      // 全页 video 的深度快照（scan 每 5s 刷新）

  function scan() {
    var els = [];
    deepWalk(document, els, 0);
    var vids = [];
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.tagName === 'VIDEO') vids.push(el);
      var u = el.currentSrc || el.src || el.href;
      if (u) add(u, 'dom');
    }
    if (vids.length) deepVids = vids;      // 给悬浮按钮兜底用
  }

  try { scan(); } catch (e) { /* ignore */ }

  try {
    // 属性/子节点变化很密（播放器每秒都在改），必须 debounce，
    // 否则 site 一动就触发一次全 DOM 扫描。
    var moTimer = null;
    var mo = new MutationObserver(function () {
      clearTimeout(moTimer);
      moTimer = setTimeout(function () { try { scan(); } catch (e) {} }, 250);
    });
    mo.observe(document.documentElement || document, {
      subtree: true, childList: true, attributes: true,
      attributeFilter: ['src', 'href']
    });
  } catch (e) { /* ignore */ }

  setInterval(scan, 5000);

  /* ============================================================
   * 二、覆盖层（shadow DOM）
   * ============================================================ */

  var CSS = [
    ':host{all:initial;position:fixed;left:0;top:0;width:100%;height:100%;',
    'display:block;pointer-events:none;z-index:2147483647;',
    'font:13px/1.45 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;',
    'color:#e6e9ef;direction:ltr;text-align:left;}',

    /* ---- 视频悬浮按钮 ---- */
    '.jl-hot{position:fixed;display:none;align-items:center;gap:5px;',
    'padding:6px 11px;border:1px solid rgba(255,255,255,.28);border-radius:8px;',
    'background:linear-gradient(135deg,#2563eb,#7c3aed);color:#fff;',
    'font-size:12.5px;font-weight:600;line-height:1.3;',
    'font-family:-apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;',
    'white-space:nowrap;cursor:grab;',
    'box-shadow:0 4px 14px rgba(37,99,235,.45);pointer-events:auto;',
    'user-select:none;touch-action:none;transition:opacity .12s,transform .12s,filter .12s;}',
    '.jl-hot.on{display:flex;}',
    '.jl-hot:hover{filter:brightness(1.08);}',
    '.jl-hot.dragging{cursor:grabbing;transform:scale(1.03);}',
    '.jl-hot .ico{font-size:13px;line-height:1;}',
    /* 视频几乎铺满视口时退到内部：只留图标，尽量少挡 */
    '.jl-hot.compact{padding:5px 8px;border-radius:50%;}',
    '.jl-hot.compact .txt{display:none;}',

    /* ---- 进度悬浮窗 ---- */
    '.jl-fw{position:fixed;z-index:2;width:212px;pointer-events:auto;',
    'background:rgba(16,19,25,.93);border:1px solid rgba(255,255,255,.13);',
    'border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.42);',
    'overflow:hidden;user-select:none;backdrop-filter:blur(8px);',
    'transition:opacity .18s,transform .18s,width .18s;}',
    '.jl-fw.is-hidden{opacity:0;pointer-events:none;transform:translateY(8px);}',
    '.jl-fw.is-fold{width:150px;}',

    '.jl-fw-head{display:flex;align-items:center;gap:6px;padding:6px 8px;',
    'cursor:grab;touch-action:none;background:rgba(255,255,255,.04);}',
    '.jl-fw-head.dragging{cursor:grabbing;}',
    '.jl-fw-dot{width:6px;height:6px;border-radius:50%;background:#3fd0c9;flex:none;',
    'box-shadow:0 0 0 3px rgba(63,208,201,.16);}',
    '.jl-fw-title{font-size:11px;color:#9aa3b2;letter-spacing:.3px;flex:none;}',
    '.jl-fw-mini{display:none;font-size:11.5px;color:#3fd0c9;font-weight:600;',
    'margin-left:auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.jl-fw.is-fold .jl-fw-mini{display:block;}',
    '.jl-fw-btns{display:flex;gap:2px;margin-left:auto;flex:none;}',
    '.jl-fw.is-fold .jl-fw-btns{margin-left:0;}',
    '.jl-fw-btns button{all:unset;display:inline-block;box-sizing:border-box;',
    'width:17px;height:17px;line-height:17px;text-align:center;',
    'border-radius:4px;color:#8b93a3;font-size:11px;cursor:pointer;}',
    '.jl-fw-btns button:hover{background:rgba(255,255,255,.12);color:#e6e9ef;}',

    '.jl-fw-body{padding:2px 9px 9px;cursor:pointer;}',
    '.jl-fw.is-fold .jl-fw-body{display:none;}',
    '.jl-fw-cap{font-size:9.5px;color:#6b7484;letter-spacing:.6px;text-transform:uppercase;}',
    '.jl-fw-main{font-size:19px;font-weight:600;line-height:1.25;letter-spacing:.2px;',
    'font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.jl-fw-sub{font-size:11px;color:#9aa3b2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.jl-fw-meta{font-size:10.5px;color:#6b7484;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.jl-fw-meta:empty{display:none;}',
    '.jl-fw-bar{height:4px;border-radius:2px;background:rgba(255,255,255,.13);margin-top:7px;overflow:hidden;}',
    '.jl-fw-bar i{display:block;height:100%;width:0;border-radius:2px;',
    'background:linear-gradient(90deg,#3fd0c9,#5b8def);transition:width .35s ease;}',

    /* ---- 轻提示 ---- */
    '.jl-toast{position:fixed;left:50%;bottom:72px;transform:translate(-50%,8px);',
    'padding:7px 14px;border-radius:8px;background:rgba(16,19,25,.95);',
    'border:1px solid rgba(255,255,255,.14);color:#e6e9ef;font-size:12px;',
    'opacity:0;pointer-events:none;transition:opacity .18s,transform .18s;',
    'box-shadow:0 6px 20px rgba(0,0,0,.4);max-width:70vw;}',
    '.jl-toast.on{opacity:1;transform:translate(-50%,0);}'
  ].join('');

  var host = null, root = null, toastEl = null, toastTimer = null;

  function ensureOverlay() {
    if (host && host.isConnected) return root;
    host = document.createElement('div');
    host.id = 'jiliu-layer';
    // 页面可能给 #jiliu-layer 写样式（极少数），直接打 inline 更稳
    host.setAttribute('aria-hidden', 'false');
    root = host.attachShadow({ mode: 'open' });

    var st = document.createElement('style');
    st.textContent = CSS;
    root.appendChild(st);

    toastEl = document.createElement('div');
    toastEl.className = 'jl-toast';
    root.appendChild(toastEl);

    var parent = document.body || document.documentElement;
    parent.appendChild(host);
    return root;
  }

  function toast(msg, ms) {
    if (!toastEl) return;
    toastEl.textContent = String(msg || '');
    toastEl.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove('on'); }, ms || 2200);
  }

  /* ============================================================
   * 三、视频悬浮按钮（框外 + 可拖动）
   * ============================================================ */

  var hot = null;
  var target = null;            // 当前跟踪的 video
  var hideTimer = null;
  var drag = null;              // 拖拽中的中间状态
  var dragMoved = false;        // 刚拖完，别把 pointerup 当点击
  var hsOn = false;
  var lastMove = 0;

  var store = {
    hotspotPos: null,           // {dx, dy} 相对视频左上角
    floatPos: null,             // {left, top} 视口坐标
    collapsed: false
  };

  function loadStore(cb) {
    try {
      chrome.storage.local.get({
        hotspot_pos: null, float_pos: null, float_collapsed: false
      }, function (v) {
        store.hotspotPos = v.hotspot_pos || null;
        store.floatPos = v.float_pos || null;
        store.collapsed = v.float_collapsed === true;
        if (cb) cb();
      });
    } catch (e) { if (cb) cb(); }
  }

  function saveStore(obj) {
    try { chrome.storage.local.set(obj); } catch (e) { /* ignore */ }
  }

  function buildHot() {
    hot = document.createElement('div');
    hot.className = 'jl-hot';
    hot.setAttribute('role', 'button');
    hot.setAttribute('title', '拖动可换位置 · 点击交给汲流');
    hot.innerHTML = '<span class="ico">⬇</span><span class="txt">下载视频</span>';

    hot.addEventListener('pointerdown', onHotDown);
    hot.addEventListener('pointermove', onHotMove);
    hot.addEventListener('pointerup', onHotUp);
    hot.addEventListener('pointercancel', onHotUp);
    // 鼠标停在按钮上时绝不能自己消失 —— 否则用户正要按，它先没了
    hot.addEventListener('mouseenter', function () { clearTimeout(hideTimer); });
    hot.addEventListener('mouseleave', function () { scheduleHide(800); });
    hot.addEventListener('click', function (e) {
      e.preventDefault(); e.stopPropagation();
      if (dragMoved) { dragMoved = false; return; }
      pickVideo();
    });
    // 别让播放器把这一下当成"点视频暂停"
    hot.addEventListener('mousedown', function (e) { e.stopPropagation(); });
    hot.addEventListener('dblclick', function (e) { e.stopPropagation(); });
    root.appendChild(hot);
    return hot;
  }

  function startHotspot() {
    if (hsOn) return;
    hsOn = true;
    if (!hot) buildHot();
  }

  function stopHotspot() {
    hsOn = false;
    hideHot();
  }

  function onHotDown(e) {
    if (e.button !== 0) return;
    e.preventDefault(); e.stopPropagation();
    var r = hot.getBoundingClientRect();
    drag = {
      id: e.pointerId,
      sx: e.clientX, sy: e.clientY,
      baseL: r.left, baseT: r.top,
      moved: false, l: r.left, t: r.top
    };
    try { hot.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    hot.classList.add('dragging');
    clearTimeout(hideTimer);
  }

  function onHotMove(e) {
    if (!drag || e.pointerId !== drag.id) return;
    var dx = e.clientX - drag.sx;
    var dy = e.clientY - drag.sy;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
    if (!drag.moved) return;
    var w = hotW || hot.offsetWidth, h = hotH || hot.offsetHeight;
    drag.l = clamp(drag.baseL + dx, 4, window.innerWidth - w - 4);
    drag.t = clamp(drag.baseT + dy, 4, window.innerHeight - h - 4);
    hot.style.left = Math.round(drag.l) + 'px';
    hot.style.top = Math.round(drag.t) + 'px';
    hot.dataset.side = 'custom';
  }

  function onHotUp(e) {
    if (!drag || (e && e.pointerId !== drag.id)) return;
    var moved = drag.moved;
    var left = drag.l, top = drag.t;
    drag = null;
    hot.classList.remove('dragging');
    try { hot.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }

    if (!moved) return;
    // 随后的 click 不该触发下载
    dragMoved = true;
    setTimeout(function () { dragMoved = false; }, 350);

    if (target && target.isConnected) {
      var r = target.getBoundingClientRect();
      store.hotspotPos = { dx: Math.round(left - r.left), dy: Math.round(top - r.top) };
      saveStore({ hotspot_pos: store.hotspotPos });
      toast('按钮位置已保存（设置里可还原）');
    }
  }

  /** video 在视口里的可见面积。广告位常用 opacity/visibility 藏 video。 */
  function visibleArea(v) {
    var r = v.getBoundingClientRect();
    if (r.width < 80 || r.height < 60) return 0;      // 太小的不算
    var st = getComputedStyle(v);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return 0;
    var vw = window.innerWidth, vh = window.innerHeight;
    var w = Math.min(r.right, vw) - Math.max(r.left, 0);
    var h = Math.min(r.bottom, vh) - Math.max(r.top, 0);
    if (w <= 0 || h <= 0) return 0;                    // 不在视口内
    return w * h;
  }

  function nearRect(r, x, y, pad) {
    return x >= r.left - pad && x <= r.right + pad &&
           y >= r.top - pad && y <= r.bottom + pad;
  }

  /** 光标正下方（含覆盖层、open shadow root）有没有 video。 */
  function videoAt(ev) {
    // 1) 从事件目标往上找 video
    var el = ev && ev.target;
    while (el && el !== document) {
      if (el.tagName === 'VIDEO') return el;
      el = el.parentElement || (el.getRootNode && el.getRootNode().host) || null;
    }
    // 2) 播放器常用透明覆盖层挡在 video 上面，mousemove 的 target 是覆盖层。
    //    elementsFromPoint 把那一整叠都拿出来，逐个翻（含各自的 shadow root）。
    //    注意：我们的覆盖层是 pointer-events:none，不会出现在这里。
    if (ev && document.elementsFromPoint) {
      try {
        var stack = document.elementsFromPoint(ev.clientX, ev.clientY);
        for (var s = 0; s < stack.length && s < 8; s++) {
          var node = stack[s];
          var hop = 0;
          while (node && hop < 6) {
            if (node.tagName === 'VIDEO') return node;
            if (node.shadowRoot) {
              var sv = node.shadowRoot.querySelector('video');
              if (sv) return sv;
            }
            node = node.parentElement;
            hop++;
          }
        }
      } catch (e) { /* ignore */ }
    }
    return null;
  }

  /** 兜底：全页可见面积最大的 video（用 scan() 的深度快照）。 */
  function biggestVideo() {
    var best = null, bestA = 0;
    for (var i = 0; i < deepVids.length; i++) {
      if (!deepVids[i].isConnected) continue;
      var a = visibleArea(deepVids[i]);
      if (a > bestA) { bestA = a; best = deepVids[i]; }
    }
    return best;
  }

  /**
   * 默认落点：优先视频**上沿外侧**（IDM 就是这条），放不下再依次试
   * 右 / 下 / 左，都不行才退到内部——此时按钮转成纯图标，尽量少挡。
   */
  function defaultSpot(r, w, h) {
    var GAP = 8, EDGE = 8;
    var vw = window.innerWidth, vh = window.innerHeight;
    var cx = function (v) { return clamp(v, EDGE, Math.max(EDGE, vw - w - EDGE)); };
    var cy = function (v) { return clamp(v, EDGE, Math.max(EDGE, vh - h - EDGE)); };

    if (r.top - h - GAP >= EDGE) {
      return { x: cx(r.right - w), y: r.top - h - GAP, side: 'top' };
    }
    if (r.right + GAP + w <= vw - EDGE) {
      return { x: r.right + GAP, y: cy(r.top), side: 'right' };
    }
    if (r.bottom + GAP + h <= vh - EDGE) {
      return { x: cx(r.right - w), y: r.bottom + GAP, side: 'bottom' };
    }
    if (r.left - GAP - w >= EDGE) {
      return { x: r.left - GAP - w, y: cy(r.top), side: 'left' };
    }
    return { x: cx(r.right - w - 10), y: Math.max(r.top + 10, EDGE), side: 'inside' };
  }

  function placeHot(v) {
    if (!hot || !v || !v.isConnected) return;
    var r = v.getBoundingClientRect();
    if (visibleArea(v) <= 0) { hideHot(); return; }

    hot.style.display = 'flex';
    hot.style.visibility = 'hidden';

    var x, y, side;
    var pos = store.hotspotPos;

    if (pos && isFinite(pos.dx) && isFinite(pos.dy)) {
      // 用户拖过：位置相对视频左上角，跟着视频走
      measureHot(false);
      x = r.left + pos.dx;
      y = r.top + pos.dy;
      side = 'custom';
    } else {
      // 先按完整宽度试；放不下才退成图标再试一次
      measureHot(false);
      var p = defaultSpot(r, hotW, hotH);
      if (p.side === 'inside') {
        measureHot(true);
        p = defaultSpot(r, hotW, hotH);
      }
      x = p.x; y = p.y; side = p.side;
    }

    hot.style.left = Math.round(clamp(x, 4, window.innerWidth - hotW - 4)) + 'px';
    hot.style.top = Math.round(clamp(y, 4, window.innerHeight - hotH - 4)) + 'px';
    hot.style.visibility = 'visible';
    hot.classList.add('on');
    hot.dataset.side = side;
  }

  /* 按钮尺寸只跟"完整/紧凑"两态有关，量一次就够。
   * 每次 mousemove 都读 offsetWidth 会强制同步布局 —— 页面越复杂越卡。 */
  var hotW = 0, hotH = 0, hotCompact = null;

  function measureHot(compact) {
    if (hotW && hotCompact === compact) return;
    hot.classList.toggle('compact', compact);
    hot.style.display = 'flex';
    hot.style.visibility = 'hidden';
    hotW = hot.offsetWidth;
    hotH = hot.offsetHeight;
    hotCompact = compact;
  }

  function hideHot() {
    if (hot) { hot.classList.remove('on'); hot.style.display = 'none'; }
    target = null;
  }

  function scheduleHide(ms) {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideHot, ms || 1200);
  }

  /** 点下按钮时决定交给汲流哪个地址。 */
  function pickVideo() {
    var v = target;
    var src = v ? (v.currentSrc || v.src || '') : '';
    var pageUrl = location.href;
    var url;
    if (/^https?:/i.test(src)) {
      url = src;                       // 真直链（mp4/m3u8…），直接下
    } else {
      url = pageUrl;                   // MSE 流 / blob / 空地址：整页交给 yt-dlp
    }

    hideHot();
    toast('已交给汲流…', 4000);
    send({
      type: 'HOTSPOT_PICK',
      payload: { url: url, title: document.title || '', src: src, page: pageUrl }
    }).then(function (r) {
      if (!r) return;
      if (r.ok) toast('汲流已收到，请在确认框里确认', 2600);
      else toast('没能唤起汲流：' + (r.error || '未知原因'), 4000);
    });
  }

  // mousemove 才能跟上动态插入的 video（比事件委托更省事）。
  // passive + 节流，避免在直播站拖累帧率。
  document.addEventListener('mousemove', function (ev) {
    if (!hsOn) return;
    if (drag) return;                       // 拖拽中位置由 drag 逻辑管
    // 光标在进度窗上：别顺手把下载按钮也弹出来
    if (W.el && W.visible &&
        nearRect(W.el.getBoundingClientRect(), ev.clientX, ev.clientY, 2)) return;
    var now = Date.now();
    if (now - lastMove < 90) return;
    lastMove = now;

    try {
      var direct = videoAt(ev);
      var v = direct || (target && target.isConnected ? target : null) || biggestVideo();
      if (!v) { hideHot(); return; }

      var r = v.getBoundingClientRect();
      var onVideo = !!direct;
      var near = nearRect(r, ev.clientX, ev.clientY, 110);
      var overHot = hot && hot.classList.contains('on') &&
                    nearRect(hot.getBoundingClientRect(), ev.clientX, ev.clientY, 2);

      if (onVideo || near || overHot) {
        target = v;
        placeHot(v);
        clearTimeout(hideTimer);
        hideTimer = setTimeout(hideHot, 2200);
      } else {
        scheduleHide(900);
      }
    } catch (e) { /* ignore */ }
  }, { passive: true });

  window.addEventListener('scroll', function () {
    if (target && hot && hot.classList.contains('on')) {
      try { placeHot(target); } catch (e) { hideHot(); }
    }
  }, { passive: true });

  window.addEventListener('resize', function () {
    if (target && hot && hot.classList.contains('on')) {
      try { placeHot(target); } catch (e) { /* ignore */ }
    }
    placeWidget();
  });

  /* ============================================================
   * 四、进度悬浮窗
   * ============================================================ */

  var MODES = ['speed', 'file', 'total'];
  var W = {
    on: false, el: null, data: null,
    mode: 0, tick: 0, visible: false,
    paused: false, pausedUntil: 0,
    hadWork: false, doneTimer: null, doneShown: false,
    timer: null, drag: null, l: 0, t: 0,
    fileTurn: -1           // 「当前文件」视图轮到第几个在下的文件（进一次视图 +1）
  };

  function buildWidget() {
    var el = document.createElement('div');
    el.className = 'jl-fw is-hidden';
    el.setAttribute('title', '按住标题栏可拖动 · 点内容区切换显示');
    el.innerHTML =
      '<div class="jl-fw-head">' +
        '<span class="jl-fw-dot"></span>' +
        '<span class="jl-fw-title">汲流</span>' +
        '<span class="jl-fw-mini">-</span>' +
        '<span class="jl-fw-btns">' +
          '<button data-act="open" title="切到汲流窗口">↗</button>' +
          '<button data-act="fold" title="收起 / 展开">—</button>' +
        '</span>' +
      '</div>' +
      '<div class="jl-fw-body">' +
        '<div class="jl-fw-cap">总速度</div>' +
        '<div class="jl-fw-main">-</div>' +
        '<div class="jl-fw-sub"></div>' +
        '<div class="jl-fw-meta"></div>' +
        '<div class="jl-fw-bar"><i></i></div>' +
      '</div>';

    W.head = el.querySelector('.jl-fw-head');
    W.mini = el.querySelector('.jl-fw-mini');
    W.cap = el.querySelector('.jl-fw-cap');
    W.main = el.querySelector('.jl-fw-main');
    W.sub = el.querySelector('.jl-fw-sub');
    W.meta = el.querySelector('.jl-fw-meta');
    W.bar = el.querySelector('.jl-fw-bar i');

    W.head.addEventListener('pointerdown', onWidgetDown);
    W.head.addEventListener('pointermove', onWidgetMove);
    W.head.addEventListener('pointerup', onWidgetUp);
    W.head.addEventListener('pointercancel', onWidgetUp);

    el.addEventListener('mouseenter', function () { W.paused = true; });
    el.addEventListener('mouseleave', function () { W.paused = false; });

    el.querySelector('.jl-fw-body').addEventListener('click', function (e) {
      e.stopPropagation();
      W.mode = (W.mode + 1) % MODES.length;
      if (W.mode === 1) W.fileTurn++;
      W.tick = 0;
      W.pausedUntil = Date.now() + 8000;    // 手动切过就先别自动转
      render();
    });

    el.querySelectorAll('.jl-fw-btns button').forEach(function (b) {
      b.addEventListener('pointerdown', function (e) { e.stopPropagation(); });
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        if (b.dataset.act === 'fold') {
          var fold = !el.classList.contains('is-fold');
          el.classList.toggle('is-fold', fold);
          store.collapsed = fold;
          saveStore({ float_collapsed: fold });
          placeWidget();
        } else {
          send({ type: 'FOCUS' }).then(function (r) {
            toast(r && r.ok ? '已切到汲流窗口' : '没能切过去，请手动打开汲流', 2200);
          });
        }
      });
    });

    return el;
  }

  function onWidgetDown(e) {
    if (e.button !== 0) return;
    if (e.target.closest('button')) return;
    e.preventDefault(); e.stopPropagation();
    var r = W.el.getBoundingClientRect();
    W.drag = { id: e.pointerId, sx: e.clientX, sy: e.clientY, bl: r.left, bt: r.top, l: r.left, t: r.top };
    try { W.head.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    W.head.classList.add('dragging');
  }

  function onWidgetMove(e) {
    var d = W.drag;
    if (!d || e.pointerId !== d.id) return;
    var w = W.el.offsetWidth, h = W.el.offsetHeight;
    var dx = e.clientX - d.sx, dy = e.clientY - d.sy;
    if (Math.abs(dx) + Math.abs(dy) < 3) return;
    d.l = clamp(d.bl + dx, 4, window.innerWidth - w - 4);
    d.t = clamp(d.bt + dy, 4, window.innerHeight - h - 4);
    W.el.style.left = Math.round(d.l) + 'px';
    W.el.style.top = Math.round(d.t) + 'px';
    W.el.style.right = 'auto';
    W.el.style.bottom = 'auto';
  }

  function onWidgetUp(e) {
    var d = W.drag;
    if (!d || (e && e.pointerId !== d.id)) return;
    W.drag = null;
    W.head.classList.remove('dragging');
    try { W.head.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    if (Math.abs(d.l - d.bl) + Math.abs(d.t - d.bt) < 4) return;
    store.floatPos = { left: Math.round(d.l), top: Math.round(d.t) };
    saveStore({ float_pos: store.floatPos });
  }

  function placeWidget() {
    if (!W.el) return;
    var w = W.el.offsetWidth, h = W.el.offsetHeight;
    var x, y;
    if (store.floatPos && isFinite(store.floatPos.left) && isFinite(store.floatPos.top)) {
      x = store.floatPos.left; y = store.floatPos.top;
      // 只收回"已经看不见"的那种，别每次 resize 都把用户摆好的位置挪乱
      var farX = x > window.innerWidth - 24 || x + w < 24;
      var farY = y > window.innerHeight - 24 || y + h < 24;
      store.floatPos = {
        left: farX ? Math.max(4, window.innerWidth - w - 18) : x,
        top: farY ? Math.max(4, window.innerHeight - h - 18) : y
      };
    } else {
      store.floatPos = {
        left: Math.max(4, window.innerWidth - w - 18),
        top: Math.max(4, window.innerHeight - h - 18)
      };
    }
    W.el.style.left = Math.round(store.floatPos.left) + 'px';
    W.el.style.top = Math.round(store.floatPos.top) + 'px';
    W.el.style.right = 'auto';
    W.el.style.bottom = 'auto';
  }

  function setBar(pct) {
    if (!W.bar) return;
    W.bar.style.width = clamp(Number(pct) || 0, 0, 100).toFixed(1) + '%';
  }

  /**
   * 当前该展示哪个文件。同时下多个时轮流来 —— 每轮到这个视图就换下一个，
   * 三个文件的任务不会永远只显示第一个。
   */
  function currentFile(p) {
    var list = [];
    var ts = p.tasks || [];
    for (var i = 0; i < ts.length; i++) {
      if (ts[i].status === 'running') list.push(ts[i]);
    }
    if (!list.length) return p.current || null;
    var turn = W.fileTurn < 0 ? 0 : W.fileTurn;
    return list[turn % list.length];
  }

  function render() {
    if (!W.el || !W.data) return;
    var p = W.data;
    var mode = MODES[W.mode];
    var cur = (mode === 'file') ? currentFile(p) : null;
    if (mode === 'file' && !cur) mode = 'speed';

    if (mode === 'speed') {
      W.cap.textContent = '总速度';
      W.main.textContent = p.speed_text || '-';
      W.sub.textContent = (p.active || 0) + ' 个进行中' +
        (p.queued ? ' · ' + p.queued + ' 排队' : '') +
        (p.paused ? ' · ' + p.paused + ' 暂停' : '');
      W.meta.textContent = '';
      setBar(p.percent);
    } else if (mode === 'file') {
      var total = (p.tasks || []).filter(function (t) { return t.status === 'running'; }).length;
      var turn = W.fileTurn < 0 ? 0 : W.fileTurn;
      W.cap.textContent = total > 1
        ? '当前文件 ' + ((turn % total) + 1) + '/' + total
        : '当前文件';
      W.main.textContent = (Number(cur.percent) || 0).toFixed(1) + '%';
      W.sub.textContent = shortName(cur.filename || cur.title || '未命名', 28);
      W.meta.textContent = (cur.speed_text || '-') + ' · 剩余 ' + (cur.eta_text || '-');
      setBar(cur.percent);
    } else {
      W.cap.textContent = '总进度';
      W.main.textContent = (Number(p.percent) || 0).toFixed(1) + '%';
      W.sub.textContent = (p.done_text || '-') + ' / ' + (p.size_text || '-');
      W.meta.textContent = '共 ' +
        ((p.active || 0) + (p.queued || 0) + (p.paused || 0)) + ' 个未完成任务';
      setBar(p.percent);
    }

    W.el.dataset.mode = mode;
    W.mini.textContent = p.speed_text || '-';
  }

  function renderDone() {
    if (!W.el) return;
    W.cap.textContent = '状态';
    W.main.textContent = '✓ 全部完成';
    W.sub.textContent = '这一批已经下完';
    W.meta.textContent = '';
    setBar(100);
    W.mini.textContent = '✓ 完成';
  }

  function showWidget() {
    if (!W.el) return;
    W.el.classList.remove('is-hidden');
    W.visible = true;
  }

  function hideWidget() {
    if (!W.el) return;
    W.el.classList.add('is-hidden');
    W.visible = false;
  }

  function applyProgress(p) {
    if (!p || !p.ok) {
      W.data = null;
      hideWidget();
      return;
    }
    W.data = p;
    var work = (p.active || 0) + (p.queued || 0) + (p.paused || 0);

    if (work > 0) {
      clearTimeout(W.doneTimer);
      W.doneShown = false;
      W.hadWork = true;
      showWidget();
      render();
      return;
    }

    // 从「有活」变成「没活」：先亮 5 秒「全部完成」，再自己消失。
    // 直接消失的话，用户最后一眼看到的是 99%，会以为卡住了。
    if (W.hadWork && !W.doneShown) {
      W.hadWork = false;
      W.doneShown = true;
      showWidget();
      renderDone();
      clearTimeout(W.doneTimer);
      W.doneTimer = setTimeout(function () {
        W.doneShown = false;
        hideWidget();
      }, 5000);
      return;
    }

    if (!W.doneShown) hideWidget();
  }

  function poll() {
    if (!W.on) return;
    send({ type: 'PROGRESS' }).then(function (r) {
      if (!W.on) return;
      applyProgress(r && r.ok ? (r.progress || null) : null);
      var work = !!(r && r.ok && r.progress &&
        ((r.progress.active || 0) + (r.progress.queued || 0) + (r.progress.paused || 0)) > 0);
      // 有活时 1.5 秒一问（速度要跟得上），闲着 6 秒一探就够了
      var gap = document.hidden ? 8000 : (work ? 1500 : 6000);
      clearTimeout(W.timer);
      W.timer = setTimeout(poll, gap);
    });
  }

  function startWidget() {
    if (W.on) return;
    W.on = true;
    if (!W.el) {
      W.el = buildWidget();
      root.appendChild(W.el);
      W.el.classList.toggle('is-fold', store.collapsed === true);
      placeWidget();
      // 首次显示要等数据，先藏住，别在角落闪一个空壳
      W.el.classList.add('is-hidden');
    }

    // 自动轮转：每秒数一次，数到 4 就换一个视图（4 秒一轮）
    if (!autoTimer) {
      autoTimer = setInterval(function () {
        if (!W.on || !W.visible) return;
        if (W.el.classList.contains('is-fold')) return;
        if (W.paused || Date.now() < W.pausedUntil) return;
        if (++W.tick < 4) return;
        W.tick = 0;
        W.mode = (W.mode + 1) % MODES.length;
        if (W.mode === 1) W.fileTurn++;     // 每轮到「当前文件」就换下一个
        render();
      }, 1000);
    }
    poll();
  }

  function stopWidget() {
    W.on = false;
    clearTimeout(W.timer);
    hideWidget();
  }

  /* ============================================================
   * 五、开关与启动
   * ============================================================ */

  var flags = { hotspot: true, widget: true };
  var autoTimer = null;

  function applyFlags() {
    var wantHot = flags.hotspot;
    var wantWidget = flags.widget && TOP && !IS_APP_PAGE;

    // 先关掉不需要的，再开需要的 —— 两个都关掉时也要能真的收干净
    if (!wantHot) stopHotspot();
    if (!wantWidget) stopWidget();
    if (!wantHot && !wantWidget) return;

    ensureOverlay();
    if (wantHot) startHotspot();
    if (wantWidget) startWidget();
  }

  function boot() {
    loadStore(function () {
      applyFlags();
      // 字体加载完按钮宽度会变一点，重新量一次
      try {
        if (document.fonts && document.fonts.ready) {
          document.fonts.ready.then(function () { hotW = 0; hotCompact = null; });
        }
      } catch (e) { /* ignore */ }
    });
  }

  if (document.body) {
    boot();
  } else {
    // document_start 时 <body> 还没解析出来。挂到 <html> 上也能显示，
    // 但某些站后续会替换 body，覆盖层就跟着没了 —— 等 DOM 就绪更稳。
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  }

  /* 在 popup 里改开关，页面这边立刻生效，不用刷新 */
  try {
    chrome.storage.onChanged.addListener(function (ch, area) {
      if (area !== 'local') return;

      if (ch.hotspotButton || ch.floatWidget) {
        if (ch.hotspotButton) flags.hotspot = ch.hotspotButton.newValue !== false;
        if (ch.floatWidget) flags.widget = ch.floatWidget.newValue !== false;
        applyFlags();
      }
      if ('hotspot_pos' in ch) {
        store.hotspotPos = ch.hotspot_pos.newValue || null;
        // 只在按钮正显示时重摆；否则还原位置会把按钮凭空弹出来
        if (hsOn && hot && hot.classList.contains('on')) {
          var v = target || biggestVideo();
          if (v) placeHot(v);
        }
      }
      if ('float_pos' in ch) {
        store.floatPos = ch.float_pos.newValue || null;
        if (W.on) placeWidget();
      }
      if ('float_collapsed' in ch) {
        store.collapsed = ch.float_collapsed.newValue === true;
        if (W.el) { W.el.classList.toggle('is-fold', store.collapsed); placeWidget(); }
      }
    });
  } catch (e) { /* ignore */ }

  /* ---- 响应 popup / background ---- */
  chrome.runtime.onMessage.addListener(function (msg, sender, reply) {
    if (!msg || !msg.type) return;

    if (msg.type === 'JILIU_LIST') {
      var arr = [];
      items.forEach(function (v) { arr.push(v); });
      arr.sort(function (a, b) { return b.ts - a.ts; });
      reply({ ok: true, items: arr, page: location.href, title: document.title });
      return true;
    }

    if (msg.type === 'JILIU_CLEAR') {
      items.clear();
      reply({ ok: true, count: 0 });
      return true;
    }

    if (msg.type === 'JILIU_COUNT') {
      reply({ ok: true, count: items.size });
      return true;
    }
  });
})();
