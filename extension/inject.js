/* ============================================================
 * inject.js —— 在「页面主世界」里跑，用来钩住页面自己的网络请求
 *
 * 为什么必须单独一个文件 + "world": "MAIN"：
 * 内容脚本默认跑在隔离世界（isolated world），它和页面共享 DOM，
 * 但 JS 上下文是分开的。在隔离世界里改写 window.fetch，
 * 只能骗到你自己，钩不到页面发起的请求。
 * 所以嗅探必须在主世界做，抓到之后再 postMessage 回隔离世界。
 * ============================================================ */
(function () {
  'use strict';

  var MEDIA_RE = /\.(mp4|m4v|mkv|webm|flv|mov|avi|ts|m4s|mp3|m4a|aac|flac|wav|ogg|opus|m3u8|mpd)(\?|#|$)/i;
  var seen = new Set();

  function post(url, initiator) {
    if (!url || typeof url !== 'string') return;
    if (url.indexOf('blob:') === 0 || url.indexOf('data:') === 0) return;
    if (!MEDIA_RE.test(url)) return;
    if (seen.has(url)) return;
    seen.add(url);
    if (seen.size > 500) seen.clear();
    try {
      window.postMessage({
        __jiliu: true,
        url: url,
        initiator: initiator || '',
        title: document.title || '',
        page: location.href
      }, '*');
    } catch (e) { /* 跨域 iframe 等，忽略 */ }
  }

  function absolutize(u) {
    try { return new URL(u, location.href).href; } catch (e) { return u; }
  }

  /* ---- 1. 钩 fetch ---- */
  try {
    var _fetch = window.fetch;
    if (_fetch) {
      window.fetch = function (input, init) {
        try {
          var u = (typeof input === 'string') ? input : (input && input.url);
          if (u) post(absolutize(u), 'fetch');
        } catch (e) { /* ignore */ }
        return _fetch.apply(this, arguments);
      };
    }
  } catch (e) { /* ignore */ }

  /* ---- 2. 钩 XMLHttpRequest ---- */
  try {
    var _open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
      try { if (url) post(absolutize(url), 'xhr'); } catch (e) { /* ignore */ }
      return _open.apply(this, arguments);
    };
  } catch (e) { /* ignore */ }

  /* ---- 3. 钩媒体元素的 src 赋值（等价于 IDM 的"抓播放地址"） ---- */
  try {
    ['HTMLMediaElement', 'HTMLSourceElement'].forEach(function (cls) {
      var proto = window[cls] && window[cls].prototype;
      if (!proto) return;
      var desc = Object.getOwnPropertyDescriptor(proto, 'src');
      if (!desc || !desc.set) return;
      Object.defineProperty(proto, 'src', {
        configurable: true,
        enumerable: desc.enumerable,
        get: desc.get,
        set: function (v) {
          try { if (v) post(absolutize(v), 'media.src'); } catch (e) { /* ignore */ }
          return desc.set.call(this, v);
        }
      });
    });
  } catch (e) { /* ignore */ }

  /* ---- 4. 钩 MediaSource / SourceBuffer 的 m3u8 之类的线索不在此处理，
           交给 DOM 扫描和上面的 fetch 钩子。 ---- */

  /* ---- 5. 向隔离世界报到 ---- */
  try { window.postMessage({ __jiliu: true, ready: true }, '*'); } catch (e) { /* ignore */ }
})();
