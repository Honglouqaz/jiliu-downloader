/* ============================================================
 * content.js —— 隔离世界里的收集器 + 视频悬浮按钮
 *
 * 两件事：
 *   1. 收集候选链接（inject.js 主世界消息 + DOM 扫描），等 popup 来问。
 *      —— 原有能力，保持不变。
 *   2. IDM 风格的视频悬浮按钮：鼠标悬到页面的 <video> 上时，右上角
 *      浮出「下载视频」，点击交给 background 唤起汲流主程序。
 *      —— 主人点名要的（任务20）。
 *
 * 悬浮按钮的设计取舍：
 *   - 只跟踪「可见面积最大」的那一个 video。直播站一页几十个小窗，
 *     每个都挂按钮等于满屏都是按钮；用户想下的几乎总是主播放器。
 *   - blob: 流（MSE）说明视频是分段拼的，地址本身不可下 —— 这时交
 *     页面 URL，让汲流主程序用 yt-dlp 解析整页（B站/腾讯视频都这样）。
 *   - iframe 里的 video 也有按钮：content script 本来就在每个 frame
 *     注入（manifest all_frames），点 iframe 里的视频交 iframe 自己的
 *     页面 URL，yt-dlp 同样认得大多数嵌入页。
 * ============================================================ */
(function () {
  'use strict';

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

  /* 穿透 open shadow root 收集元素。腾讯视频 txp 播放器这类把 <video>
   * 藏进自定义元素影子树里的站点，普通 querySelectorAll('video') 是
   * 摸不到的 —— 悬浮按钮「弹不出来」多半就是它。 */
  function deepCollect(root, sel, out, depth) {
    if (!root || depth > 12 || out.length > 200) return;
    var els;
    try { els = root.querySelectorAll(sel); } catch (e) { return; }
    for (var i = 0; i < els.length; i++) out.push(els[i]);
    var all;
    try { all = root.querySelectorAll('*'); } catch (e) { return; }
    for (var j = 0; j < all.length; j++) {
      var sr = all[j].shadowRoot;          // open shadow 才拿得到
      if (sr) deepCollect(sr, sel, out, depth + 1);
    }
  }

  var deepVids = [];      // 全页 video 的深度快照（scan 每 5s 刷新）

  function scan() {
    var sels = ['video', 'audio', 'source', 'a[href]', 'link[href]'];
    for (var i = 0; i < sels.length; i++) {
      var found = [];
      deepCollect(document, sels[i], found, 0);
      if (sels[i] === 'video') deepVids = found;   // 给悬浮按钮兜底用
      for (var j = 0; j < found.length; j++) {
        var el = found[j];
        var u = el.currentSrc || el.src || el.href;
        if (u) add(u, 'dom');
      }
    }
  }

  try { scan(); } catch (e) { /* ignore */ }

  try {
    var mo = new MutationObserver(function () { scan(); });
    mo.observe(document.documentElement || document, {
      subtree: true, childList: true, attributes: true,
      attributeFilter: ['src', 'href']
    });
  } catch (e) { /* ignore */ }

  setInterval(scan, 5000);

  /* ============================================================
   * 视频悬浮按钮（IDM 风格）
   * ============================================================ */

  var hot = null;          // 悬浮按钮 DOM
  var target = null;       // 当前跟踪的 video
  var hideTimer = null;

  function ensureHot() {
    if (hot && hot.isConnected) return hot;
    hot = document.createElement('div');
    hot.id = 'jiliu-hotspot';
    hot.textContent = '⬇ 下载视频';
    hot.style.cssText = [
      'position:fixed', 'z-index:2147483647', 'display:none',
      'padding:7px 14px', 'border-radius:8px', 'cursor:pointer',
      'font:600 13px/1.4 system-ui,sans-serif', 'user-select:none',
      'color:#fff', 'background:linear-gradient(135deg,#2563eb,#7c3aed)',
      'box-shadow:0 4px 14px rgba(37,99,235,.45)',
      'border:1px solid rgba(255,255,255,.25)'
    ].join(';');
    (document.body || document.documentElement).appendChild(hot);

    hot.addEventListener('mouseenter', function () { clearTimeout(hideTimer); });
    hot.addEventListener('mouseleave', scheduleHide);
    // mousedown 阻止把点击传播给页面（有的播放器会把点在视频上的事件
    // 当成播放/暂停切换）；click 只响应主键
    hot.addEventListener('mousedown', function (e) { e.stopPropagation(); });
    hot.addEventListener('click', function (e) {
      e.preventDefault(); e.stopPropagation();
      clearTimeout(hideTimer);
      hideHot();
      pickVideo();
    });
    return hot;
  }

  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideHot, 700);
  }

  function hideHot() {
    if (hot) hot.style.display = 'none';
    target = null;
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

  function findTarget(ev) {
    // 1) 从事件目标往上找 video（穿透 open shadow root 的父链）
    var el = ev && ev.target;
    while (el && el !== document) {
      if (el.tagName === 'VIDEO') return el;
      el = el.parentElement || (el.getRootNode && el.getRootNode().host) || null;
    }
    // 2) 命中点元素链：播放器常用透明覆盖层挡在 video 上面，
    //    mousemove 的 target 是覆盖层而不是 video。elementsFromPoint
    //    把那一整叠都拿出来，逐个翻（含各自的 shadow root）。
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
    // 3) 兜底：全页找可见面积最大的 video（用 scan() 的深度快照，
    //    不在 mousemove 里做全页 shadow 遍历 —— 大页面上太贵）
    var best = null, bestA = 0;
    for (var i = 0; i < deepVids.length; i++) {
      var a = visibleArea(deepVids[i]);
      if (a > bestA) { bestA = a; best = deepVids[i]; }
    }
    return best;
  }

  function placeHot(v) {
    var r = v.getBoundingClientRect();
    var h = ensureHot();
    // 放在视频右上角内侧；贴边时收进来
    var x = Math.min(r.right - h.offsetWidth - 8, window.innerWidth - h.offsetWidth - 6);
    var y = Math.max(r.top + 8, 6);
    h.style.left = Math.max(6, x) + 'px';
    h.style.top = y + 'px';
    h.style.display = 'block';
  }

  /** 点下按钮时决定交给汲流哪个地址。 */
  function pickVideo() {
    var v = target;
    var src = v ? (v.currentSrc || v.src || '') : '';
    var pageUrl = location.href;
    var url;
    if (/^https?:/i.test(src)) {
      url = src;                       // 真直链（mp4/m3u8…），直接下
    } else if (/^blob:/i.test(src) || !src) {
      url = pageUrl;                   // MSE 流 / 空地址：整页交给 yt-dlp
    } else {
      url = pageUrl;
    }
    // 清理掉 #t= 之外的无效部分没必要 —— 主程序会自己判断
    try {
      chrome.runtime.sendMessage({
        type: 'HOTSPOT_PICK',
        payload: { url: url, title: document.title || '', src: src, page: pageUrl }
      });
    } catch (e) { /* 扩展上下文失效（刚重载过扩展）就静默 */ }
    try { hot.style.display = 'none'; } catch (e) { }
  }

  // mousemove 才能跟上动态插入的 video（比事件委托更省事）。
  // 用 passive + 节流，避免在直播站拖累帧率。
  var lastMove = 0;
  document.addEventListener('mousemove', function (ev) {
    var now = Date.now();
    if (now - lastMove < 120) return;
    lastMove = now;
    try {
      var v = findTarget(ev);
      if (v) {
        target = v;
        placeHot(v);
        clearTimeout(hideTimer);
        hideTimer = setTimeout(function () { hideHot(); }, 1500);
      } else if (target && !hot.matches(':hover')) {
        hideHot();
      }
    } catch (e) { /* ignore */ }
  }, { passive: true });

  window.addEventListener('scroll', function () {
    if (target && hot && hot.style.display !== 'none') {
      try { placeHot(target); } catch (e) { hideHot(); }
    }
  }, { passive: true });

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
