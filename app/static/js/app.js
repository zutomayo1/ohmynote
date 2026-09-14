/*!
 * 墨痕 InkNote · app.js
 * 所有页面共享的交互脚本（普通脚本，非 module）。
 * 契约：docs/frontend-contract.md（只使用其中出现的 id / 类名 / data 属性）。
 */
(function () {
  'use strict';

  // ===== 0. 通用小工具 =====
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn, { once: true });
    else fn();
  }

  // 单个模块初始化失败不应拖垮其它模块
  function safe(fn) {
    try { fn(); } catch (err) { if (window.console) window.console.error('[InkNote]', err); }
  }

  // 事件目标是否处于输入控件 / 可编辑区域
  function isEditableTarget(el) {
    if (!el || !el.tagName) return false;
    var tag = el.tagName.toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable === true;
  }
  function isEditorPage() { return !!(document.body && document.body.getAttribute('data-page') === 'editor'); }
  function trim(text) { return String(text === null || text === undefined ? '' : text).replace(/^\s+|\s+$/g, ''); }
  function escapeRegExp(str) { return String(str).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  var HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    return String(text).replace(/[&<>"']/g, function (ch) { return HTML_ESCAPES[ch]; });
  }

  // ===== 1. 主题（深色模式）=====
  // localStorage['inknote-theme'] 缺省时跟随系统；用户点击后写入 light/dark，此后不再跟随系统。
  var THEME_KEY = 'inknote-theme';
  var darkMedia = null;

  function getDarkMedia() {
    if (!darkMedia && window.matchMedia) darkMedia = window.matchMedia('(prefers-color-scheme: dark)');
    return darkMedia;
  }
  function readStoredTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; } // 隐私模式等场景忽略
  }
  function writeStoredTheme(mode) {
    try {
      if (mode === 'light' || mode === 'dark') localStorage.setItem(THEME_KEY, mode);
      else localStorage.removeItem(THEME_KEY); // 'auto' 等价于"没有显式选择"
    } catch (e) { /* 忽略写入失败 */ }
  }
  // 用户是否显式选择过（light / dark）；'auto' / 空值都视为未选择
  function hasExplicitTheme() { var s = readStoredTheme(); return s === 'light' || s === 'dark'; }
  function systemTheme() { var m = getDarkMedia(); return m && m.matches ? 'dark' : 'light'; }

  // 当前实际生效主题：显式选择 > 系统（未显式选择时无条件跟随 prefers-color-scheme）
  // matchMedia 不可用时退回 html[data-theme]（首屏内联脚本预设），保持一致。
  function theme() {
    var stored = readStoredTheme();
    if (stored === 'light' || stored === 'dark') return stored;
    var media = getDarkMedia();
    if (media) return media.matches ? 'dark' : 'light';
    var attr = document.documentElement.getAttribute('data-theme');
    if (attr === 'light' || attr === 'dark') return attr;
    return 'light';
  }
  function updateThemeToggle(mode) {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    var label = mode === 'dark' ? '切换到浅色模式' : '切换到深色模式';
    btn.setAttribute('title', label);
    btn.setAttribute('aria-label', label);
  }
  // persist !== false 时写入 localStorage（'auto' 表示清除显式选择）
  function applyTheme(mode, persist) {
    var next;
    if (mode === 'auto') { if (persist !== false) writeStoredTheme('auto'); next = systemTheme(); }
    else { next = mode === 'dark' ? 'dark' : 'light'; if (persist !== false) writeStoredTheme(next); }
    document.documentElement.setAttribute('data-theme', next);
    updateThemeToggle(next);
    return next;
  }
  // 只有用户没有显式选择过时才跟随系统
  function onSystemThemeChange() { if (!hasExplicitTheme()) applyTheme(systemTheme(), false); }
  function onThemeToggleClick() { applyTheme(theme() === 'dark' ? 'light' : 'dark'); } // 显式选择 → 持久化

  function initTheme() {
    applyTheme(theme(), false); // 与 base.html 首屏内联脚本保持一致
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.addEventListener('click', onThemeToggleClick);
    var media = getDarkMedia();
    if (media && typeof media.addEventListener === 'function') media.addEventListener('change', onSystemThemeChange);
    else if (media && typeof media.addListener === 'function') media.addListener(onSystemThemeChange);
  }

  // ===== 2. toast =====
  // #toast-wrap 内插入 .toast，3s 后加 .is-out，过渡结束或 4s 后移除；最多同时 3 条。
  var TOAST_LIMIT = 3;
  function toast(message, kind) {
    var wrap = document.getElementById('toast-wrap');
    if (!wrap) return;
    var type = kind === 'error' ? 'error' : 'ok';
    while (wrap.children.length >= TOAST_LIMIT) wrap.removeChild(wrap.firstChild);
    var el = document.createElement('div');
    el.className = 'toast toast--' + type;
    el.textContent = message === null || message === undefined ? '' : String(message);
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    wrap.appendChild(el);
    var removed = false;
    function remove() {
      if (removed) return;
      removed = true;
      el.removeEventListener('transitionend', remove);
      if (el.parentNode) el.parentNode.removeChild(el);
    }
    setTimeout(function () { el.classList.add('is-out'); el.addEventListener('transitionend', remove); }, 3000);
    setTimeout(remove, 4000); // 兜底：没有过渡动画时也能移除
  }

  // ===== 3. CSRF / fetchJSON =====
  function csrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }
  function hasHeaderName(headers, name) {
    var lower = name.toLowerCase();
    for (var key in headers) {
      if (Object.prototype.hasOwnProperty.call(headers, key) && key.toLowerCase() === lower) return true;
    }
    return false;
  }
  function isBrowserManagedBody(body) {
    if (body === null || body === undefined || typeof body !== 'object') return false;
    if (typeof FormData !== 'undefined' && body instanceof FormData) return true;
    if (typeof Blob !== 'undefined' && body instanceof Blob) return true;
    return typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams;
  }
  // 自动 JSON 序列化 + Content-Type / X-CSRF-Token / X-Requested-With；非 2xx 抛出带 status / message 的 Error
  function fetchJSON(url, options) {
    var opts = {}, src = options || {}, key, hk;
    for (key in src) if (Object.prototype.hasOwnProperty.call(src, key)) opts[key] = src[key];
    var headers = {};
    if (opts.headers) {
      if (typeof Headers !== 'undefined' && opts.headers instanceof Headers) {
        opts.headers.forEach(function (value, name) { headers[name] = value; });
      } else {
        for (hk in opts.headers) if (Object.prototype.hasOwnProperty.call(opts.headers, hk)) headers[hk] = opts.headers[hk];
      }
    }
    var managed = isBrowserManagedBody(opts.body); // multipart / 表单体交给浏览器设置 Content-Type
    if (!managed && opts.body !== null && opts.body !== undefined && typeof opts.body === 'object') {
      opts.body = JSON.stringify(opts.body);
    }
    if (!managed && !hasHeaderName(headers, 'Content-Type')) headers['Content-Type'] = 'application/json';
    var token = csrf();
    if (token && !hasHeaderName(headers, 'X-CSRF-Token')) headers['X-CSRF-Token'] = token;
    if (!hasHeaderName(headers, 'X-Requested-With')) headers['X-Requested-With'] = 'fetch';
    opts.headers = headers;
    if (!opts.credentials) opts.credentials = 'same-origin';
    return window.fetch(url, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
        if (!res.ok) {
          var hasError = data && typeof data === 'object' && typeof data.error === 'string' && data.error;
          var message = hasError ? data.error : '请求失败（HTTP ' + res.status + '）';
          var err = new Error(message);
          err.status = res.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  // ===== 4. 命令面板（Ctrl/Cmd+K、/ 或 #search-open）=====
  var PALETTE = {
    root: null, input: null, results: null, openBtn: null,
    isOpen: false, activeIndex: -1, searchTimer: 0, searchSeq: 0, docKey: null
  };

  function initPalette() {
    var root = document.getElementById('palette');
    var input = document.getElementById('palette-input');
    var results = document.getElementById('palette-results');
    if (!root || !input || !results) return;
    PALETTE.root = root;
    PALETTE.input = input;
    PALETTE.results = results;
    PALETTE.openBtn = document.getElementById('search-open');
    if (PALETTE.openBtn) PALETTE.openBtn.addEventListener('click', function () { openPalette(); });
    input.addEventListener('input', onPaletteInput);
    $$('[data-palette-close]', root).forEach(function (el) {
      el.addEventListener('click', function () { closePalette(); });
    });
    results.addEventListener('click', function (e) { // 点结果项：先关面板再正常跳转
      if (e.target && e.target.closest && e.target.closest('.palette__item')) closePalette(false);
    });
    document.addEventListener('keydown', onGlobalKeydown, true); // 全局快捷键常驻
  }

  function onGlobalKeydown(e) {
    if (e.defaultPrevented) return;
    var mod = e.ctrlKey || e.metaKey, key = e.key;
    if (mod && !e.altKey && (key === 'k' || key === 'K' || e.code === 'KeyK')) {
      if (isEditorPage() && isEditableTarget(e.target)) return; // 编辑器里 Ctrl+K 是"插入链接"，交给 editor.js
      e.preventDefault();
      openPalette();
      return;
    }
    if (key === '/' && !mod && !e.altKey) { // 非输入状态下按 / 打开面板
      if (isEditableTarget(e.target)) return;
      e.preventDefault();
      openPalette();
    }
  }

  function openPalette() {
    if (!PALETTE.root) return;
    if (!PALETTE.isOpen) {
      PALETTE.isOpen = true;
      PALETTE.root.hidden = false;
      PALETTE.root.classList.add('is-open');
      if (!PALETTE.docKey) PALETTE.docKey = function (ev) { onPaletteKeydown(ev); };
      document.addEventListener('keydown', PALETTE.docKey, true);
    }
    var input = PALETTE.input;
    if (!input) return;
    input.focus();
    try { input.select(); } catch (e) { /* 忽略 */ }
    var q = trim(input.value);
    if (q && PALETTE.results && !PALETTE.results.childNodes.length) runPaletteSearch(q);
  }

  function closePalette(restoreFocus) {
    if (!PALETTE.root || !PALETTE.isOpen) return;
    PALETTE.isOpen = false;
    PALETTE.root.classList.remove('is-open');
    PALETTE.root.hidden = true;
    if (PALETTE.docKey) document.removeEventListener('keydown', PALETTE.docKey, true); // 不泄漏监听器
    if (restoreFocus !== false && PALETTE.openBtn && PALETTE.openBtn.focus) PALETTE.openBtn.focus();
  }

  function onPaletteInput() {
    if (!PALETTE.input) return;
    if (PALETTE.searchTimer) { clearTimeout(PALETTE.searchTimer); PALETTE.searchTimer = 0; }
    var q = trim(PALETTE.input.value);
    if (!q) {
      PALETTE.searchSeq += 1; // 让飞行中的请求作废
      if (PALETTE.results) PALETTE.results.textContent = '';
      PALETTE.activeIndex = -1;
      return;
    }
    PALETTE.searchTimer = setTimeout(function () { PALETTE.searchTimer = 0; runPaletteSearch(q); }, 200);
  }

  function runPaletteSearch(q) {
    var input = PALETTE.input, results = PALETTE.results;
    if (!input || !results) return;
    var base = input.getAttribute('data-search-url') || '/api/search';
    var url = base + (base.indexOf('?') === -1 ? '?' : '&') + 'q=' + encodeURIComponent(q) + '&limit=20';
    var seq = (PALETTE.searchSeq += 1);
    PALETTE.activeIndex = -1;
    results.textContent = '搜索中…';
    fetchJSON(url).then(function (data) {
      if (seq !== PALETTE.searchSeq) return; // 旧请求丢弃
      renderPaletteResults(data && data.items ? data.items : [], q);
    }).catch(function (err) {
      if (seq !== PALETTE.searchSeq) return;
      results.textContent = err && err.message ? err.message : '搜索失败';
      PALETTE.activeIndex = -1;
    });
  }

  // 先 escapeHtml，再只用 <mark> 标签包裹命中词
  function highlightEscaped(escapedText, rawQuery) {
    var q = trim(rawQuery), eq, re;
    if (!q) return escapedText;
    eq = escapeHtml(q);
    if (!eq) return escapedText;
    try { re = new RegExp(escapeRegExp(eq), 'gi'); } catch (e) { return escapedText; }
    return String(escapedText).replace(re, function (m) { return '<mark>' + m + '</mark>'; });
  }
  function paletteItemUrl(item) {
    if (item && typeof item.url === 'string' && item.url.charAt(0) === '/') return item.url;
    if (item && item.id !== null && item.id !== undefined) return '/notes/' + encodeURIComponent(String(item.id));
    return '#';
  }
  function renderPaletteResults(items, query) {
    var results = PALETTE.results;
    if (!results) return;
    while (results.firstChild) results.removeChild(results.firstChild);
    if (!items.length) {
      results.textContent = '没有找到匹配的笔记';
      PALETTE.activeIndex = -1;
      return;
    }
    items.forEach(function (item) {
      var a = document.createElement('a');
      a.className = 'palette__item';
      a.setAttribute('href', paletteItemUrl(item));
      var title = document.createElement('span');
      title.className = 'palette__item-title';
      title.innerHTML = highlightEscaped(escapeHtml(item && item.title ? item.title : '无标题'), query);
      var meta = document.createElement('span');
      meta.className = 'palette__item-meta';
      var snippet = item && item.snippet ? String(item.snippet) : '';
      if (snippet) {
        meta.innerHTML = highlightEscaped(escapeHtml(snippet), query);
        if (item.updated_at) meta.appendChild(document.createTextNode(' · 更新于 ' + item.updated_at));
      } else if (item && item.updated_at) {
        meta.textContent = '更新于 ' + item.updated_at;
      }
      a.appendChild(title);
      a.appendChild(meta);
      results.appendChild(a);
    });
    setPaletteActive(0);
  }
  function paletteItems() { return PALETTE.results ? $$('.palette__item', PALETTE.results) : []; }
  function setPaletteActive(index) {
    var list = paletteItems();
    if (!list.length) { PALETTE.activeIndex = -1; return; }
    var i = index < 0 ? list.length - 1 : index;
    if (i >= list.length) i = 0;
    PALETTE.activeIndex = i;
    list.forEach(function (el, idx) { el.classList.toggle('is-active', idx === i); });
    var active = list[i];
    if (active && active.scrollIntoView) {
      try { active.scrollIntoView({ block: 'nearest' }); } catch (e) { active.scrollIntoView(); }
    }
  }
  function submitPaletteForm() {
    var form = PALETTE.root ? PALETTE.root.querySelector('form') : null;
    if (!form) return;
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.submit();
  }
  function onPaletteKeydown(e) {
    if (!PALETTE.isOpen) return;
    var key = e.key;
    if (key === 'Escape') { e.preventDefault(); closePalette(); return; }
    if (key === 'ArrowDown') { e.preventDefault(); setPaletteActive(PALETTE.activeIndex + 1); return; }
    if (key === 'ArrowUp') { e.preventDefault(); setPaletteActive(PALETTE.activeIndex - 1); return; }
    if (key !== 'Enter') return;
    e.preventDefault(); // 阻止表单原生提交
    if (e.ctrlKey || e.metaKey) { submitPaletteForm(); return; } // Ctrl/Cmd+Enter：去 /search 看全部
    var list = paletteItems();
    var active = PALETTE.activeIndex >= 0 && PALETTE.activeIndex < list.length ? list[PALETTE.activeIndex] : null;
    if (!active && list.length) active = list[0];
    if (active) {
      var href = active.getAttribute('href');
      if (href && href !== '#') window.location.assign(href);
    } else if (trim(PALETTE.input ? PALETTE.input.value : '')) {
      submitPaletteForm();
    }
  }

  // ===== 5. 危险操作确认（form[data-confirm]）=====
  function initConfirm() {
    document.addEventListener('submit', function (e) {
      var form = e.target;
      if (!form || !form.matches || !form.matches('form[data-confirm]')) return;
      if (!window.confirm(form.getAttribute('data-confirm') || '')) {
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);
  }

  // ===== 6. 代码块复制 =====
  // .prose 内的 pre（以及不含 pre 的 .codehilite 容器）右上角插入复制按钮，不重复插入。
  var copyMarked = typeof WeakSet === 'function' ? new WeakSet() : null;
  function alreadyHasCopy(el) { return copyMarked ? copyMarked.has(el) : !!el.querySelector(':scope > .code-copy'); }
  function markCopy(el) { if (copyMarked) copyMarked.add(el); }

  function addCopyButton(el) {
    if (!el || alreadyHasCopy(el)) return;
    markCopy(el);
    var pos = '';
    try { pos = window.getComputedStyle(el).position; } catch (e) { pos = ''; }
    if (!pos || pos === 'static') el.style.position = 'relative'; // 保证按钮能定位到右上角
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'code-copy'; btn.textContent = '复制';
    el.appendChild(btn);
  }

  function setupCodeCopy(root) {
    root = root || document;
    $$('.prose pre', root).forEach(addCopyButton);
    $$('.codehilite', root).forEach(function (box) {
      if (!box.querySelector('pre')) addCopyButton(box); // 内部 pre 已处理，避免重复
    });
  }

  // 克隆后剔除按钮，避免把"复制"字样带进剪贴板
  function codeTextOf(source) {
    var clone = source.cloneNode(true);
    $$('.code-copy', clone).forEach(function (btn) { if (btn.parentNode) btn.parentNode.removeChild(btn); });
    return clone.textContent || '';
  }

  // 剪贴板 API 不可用时的退化方案：选中 + document.execCommand('copy')
  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-1000px;left:-1000px;opacity:0;';
    document.body.appendChild(ta);
    var ok = false;
    try {
      ta.select();
      ta.setSelectionRange(0, ta.value.length);
      ok = document.execCommand('copy');
    } catch (e) { ok = false; }
    if (ta.parentNode) ta.parentNode.removeChild(ta);
    return ok;
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return fallbackCopy(text); });
      } catch (e) { return Promise.resolve(fallbackCopy(text)); }
    }
    return Promise.resolve(fallbackCopy(text));
  }

  function handleCopyClick(btn) {
    var source = btn.parentNode;
    if (!source) return;
    copyText(codeTextOf(source)).then(function (ok) {
      if (!ok) { toast('复制失败，请手动选择代码', 'error'); return; }
      btn.textContent = '已复制';
      setTimeout(function () { if (document.contains(btn)) btn.textContent = '复制'; }, 1500);
      toast('已复制到剪贴板', 'ok');
    });
  }

  function initCodeCopy() {
    setupCodeCopy(document);
    document.addEventListener('click', function (e) {
      var target = e.target;
      if (!target || !target.closest) return;
      var btn = target.closest('.code-copy');
      if (btn) { e.preventDefault(); handleCopyClick(btn); }
    });
    var preview = document.getElementById('preview'); // 编辑器预览整体重渲染时动态补挂
    if (preview && window.MutationObserver) {
      var observer = new MutationObserver(function () {
        safe(function () { setupCodeCopy(preview); enhanceContent(preview); });
      });
      observer.observe(preview, { childList: true, subtree: true });
    }
  }

  // ===== 7. 目录高亮（#toc[data-toc]）=====
  function initToc() {
    var toc = document.getElementById('toc');
    if (!toc || !toc.hasAttribute('data-toc')) return;
    var body = document.getElementById('post-body') || document.querySelector('.post-body');
    if (!body) return;
    var headings = $$('h2, h3, h4', body).filter(function (h) { return !!h.id; });
    if (!headings.length) return;
    var links = $$('.toc__link', toc);
    var TOC_OFFSET = 110;
    var activeId = null;

    function setActive(id) {
      if (id === activeId) return;
      activeId = id;
      links.forEach(function (a) {
        var href = a.getAttribute('href') || '';
        var targetId = href.charAt(0) === '#' ? decodeURIComponent(href.slice(1)) : '';
        a.classList.toggle('is-active', !!id && targetId === id); // 只保留一个 active
      });
    }

    // 取"阈值上方最靠下"的标题；没有则取最靠近顶部的那个
    function pickActive() {
      var above = null, aboveTop = -Infinity, below = null, belowTop = Infinity;
      for (var i = 0; i < headings.length; i++) {
        var top = headings[i].getBoundingClientRect().top;
        if (top <= TOC_OFFSET) { if (top > aboveTop) { aboveTop = top; above = headings[i]; } }
        else if (top < belowTop) { belowTop = top; below = headings[i]; }
      }
      var current = above || below || headings[0];
      setActive(current ? current.id : null);
    }

    var raf = window.requestAnimationFrame || function (cb) { return setTimeout(cb, 16); };
    var rafId = 0;
    function scheduleUpdate() {
      if (rafId) return;
      rafId = raf(function () { rafId = 0; pickActive(); });
    }

    if ('IntersectionObserver' in window) {
      var observer = new IntersectionObserver(function () { scheduleUpdate(); },
        { root: null, rootMargin: '-10% 0px -70% 0px', threshold: [0, 1] });
      headings.forEach(function (h) { observer.observe(h); });
    } else { // 兜底
      window.addEventListener('scroll', scheduleUpdate, { passive: true });
      window.addEventListener('resize', scheduleUpdate);
    }

    links.forEach(function (a) {
      a.addEventListener('click', function (e) {
        var href = a.getAttribute('href') || '';
        if (href.charAt(0) !== '#') return;
        var id = decodeURIComponent(href.slice(1));
        var target = document.getElementById(id);
        if (!target) return;
        e.preventDefault(); // 自己控制平滑滚动与 URL hash
        var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        try { target.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' }); }
        catch (err) { target.scrollIntoView(); }
        if (window.history && window.history.pushState) window.history.pushState(null, '', href);
        else window.location.hash = href;
        setActive(id);
      });
    });

    var initial = window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : '';
    pickActive();
    if (initial && document.getElementById(initial)) setActive(initial);
    window.addEventListener('load', scheduleUpdate, { once: true });
  }

  // ===== 8. 离线提示 =====
  function initOffline() {
    var banner = document.getElementById('offline-banner');
    if (!banner) return;
    function sync() { banner.hidden = !!navigator.onLine; } // 在线时隐藏
    window.addEventListener('online', sync);
    window.addEventListener('offline', sync);
    sync();
  }

  // ===== 9. 编辑器标题字号自适应 =====
  function initTitleAutoSize() {
    var input = document.querySelector('.editor__title-input');
    if (!input) return;
    function sync() {
      var len = (input.value || '').length;
      input.classList.toggle('is-long', len > 24);
      input.classList.toggle('is-xlong', len > 40);
    }
    input.addEventListener('input', sync);
    sync(); // 初始化时执行一次
  }

  // ===== 10. 正文增强：外链 rel / 图片懒加载 =====
  function enhanceContent(root) {
    root = root || document;
    $$('.prose a[href^="http"]', root).forEach(function (a) {
      var rel = (a.getAttribute('rel') || '').toLowerCase();
      var add = [];
      if (rel.indexOf('noopener') === -1) add.push('noopener');
      if (rel.indexOf('noreferrer') === -1) add.push('noreferrer');
      if (add.length) a.setAttribute('rel', (a.getAttribute('rel') ? a.getAttribute('rel') + ' ' : '') + add.join(' '));
    });
    $$('.prose img', root).forEach(function (img) {
      if (!img.hasAttribute('loading')) img.setAttribute('loading', 'lazy');
    });
  }

  // ===== 11. 筛选表单自动提交（select / checkbox，q 输入框不自动提交）=====
  function initToolbarAutoSubmit() {
    $$('.toolbar select, .toolbar input[type="checkbox"]').forEach(function (el) {
      el.addEventListener('change', function () {
        var form = el.form || (el.closest ? el.closest('form') : null);
        if (form) form.submit();
      });
    });
  }

  // ===== 12. 对外接口 + 初始化 =====
  // editor.js 依赖这些方法，必须在 app.js 执行时立即挂载（defer 顺序保证）
  // ===== 9. 键盘导航（列表/搜索结果卡片）与快捷键帮助（? 呼出）=====
  function isTypingTarget(el) {
    if (!el || el.nodeType !== 1) return false;
    var tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' ||
      el.isContentEditable || el.closest('dialog[open]');
  }

  function cardNodes() {
    return document.querySelectorAll('.note-grid .note-card');
  }

  function titleLink(card) {
    return card ? card.querySelector('.card__title a') : null;
  }

  function initKbdNav() {
    var current = -1;

    // 帮助浮层的关闭按钮与点击遮罩关闭
    document.addEventListener('click', function (event) {
      var help = document.getElementById('kbd-help');
      if (!help) return;
      if (event.target.closest('[data-kbd-help-close]')) {
        help.close();
      } else if (event.target === help && help.open) {
        help.close(); // 点到 backdrop（dialog 本体）也算关闭
      }
    });

    function clearActive() {
      document.querySelectorAll('.note-card.kbd-active').forEach(function (card) {
        card.classList.remove('kbd-active');
        card.removeAttribute('aria-current');
      });
      current = -1;
    }

    function setActive(index, focusTitle) {
      var cards = cardNodes();
      if (!cards.length) return;
      index = Math.max(0, Math.min(index, cards.length - 1));
      clearActive();
      current = index;
      var card = cards[index];
      card.classList.add('kbd-active');
      card.setAttribute('aria-current', 'true');
      card.scrollIntoView({ block: 'nearest' });
      if (focusTitle) {
        var link = titleLink(card);
        if (link) link.focus({ preventScroll: true });
      }
    }

    document.addEventListener('keydown', function (event) {
      if (event.defaultPrevented) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (isTypingTarget(event.target)) return;

      var key = event.key;
      var cards = cardNodes();
      var help = document.getElementById('kbd-help');

      // ? 呼出快捷键帮助（任意页面；不带 shift 的 / 已被命令面板占用）
      if (key === '?') {
        if (help && typeof help.showModal === 'function' && !help.open) {
          event.preventDefault();
          help.showModal();
          return;
        }
      }

      if (!cards.length) return;

      if (key === 'j' || key === 'ArrowDown') {
        event.preventDefault();
        setActive(current < 0 ? 0 : current + 1, true);
      } else if (key === 'k' || key === 'ArrowUp') {
        event.preventDefault();
        setActive(current < 0 ? 0 : current - 1, true);
      } else if (key === 'x' && current >= 0) {
        // 批量选择框（列表页有）：空格切换更自然，但 x 避免与滚动冲突
        event.preventDefault();
        var box = cards[current].querySelector('.note-select');
        if (box) box.checked = !box.checked;
      } else if (key === 'Escape') {
        if (help && help.open) { help.close(); return; }
        clearActive();
      }
    });
  }

  var InkNote = window.InkNote = window.InkNote || {};
  InkNote.toast = toast; InkNote.csrf = csrf; InkNote.fetchJSON = fetchJSON;
  InkNote.escapeHtml = escapeHtml; InkNote.theme = theme; InkNote.applyTheme = applyTheme;
  // 供 editor.js 在预览重渲染后复用
  InkNote.enhanceContent = enhanceContent; InkNote.setupCodeCopy = setupCodeCopy;

  // ===== 统计页：热力图初始定位到最新（右端） =====
  // 53 周的格子比卡片宽，overflow 裁掉右半边；有记录的日子几乎总在最新那几周，
  // 不自动滚过去的话用户看到的就是一整片空白灰格（2026-09-14 用户实报「看不见热力图」）
  function initHeatmapScroll() {
    var heat = document.querySelector('.heatmap');
    if (!heat) { return; }
    heat.scrollLeft = heat.scrollWidth;
  }

  // ===== 今日待办页：勾选回写原笔记 =====
  function initTodoPage() {
    var root = document.getElementById('todo-groups');
    if (!root) { return; }
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');

    root.addEventListener('change', function (event) {
      var box = event.target.closest('.todo-item__box');
      if (!box) { return; }
      var item = box.closest('.todo-item');
      var noteId = box.getAttribute('data-task-note');
      box.disabled = true;

      fetch('/notes/' + noteId + '/task-toggle', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-CSRF-Token': csrfMeta ? csrfMeta.getAttribute('content') : ''
        },
        credentials: 'same-origin',
        body: 'index=' + encodeURIComponent(box.getAttribute('data-task-index'))
      }).then(function (res) {
        return res.json().catch(function () { throw new Error('bad-json'); });
      }).then(function (data) {
        if (!data || !data.ok) { throw new Error((data && data.error) || '更新失败'); }
        box.checked = !!data.checked;
        if (item) { item.classList.toggle('is-done', !!data.checked); }
        // 更新该组的未完成计数
        var group = box.closest('[data-todo-group]');
        if (group) {
          var counter = group.querySelector('[data-open-count]');
          if (counter) {
            var n = parseInt(counter.textContent, 10) || 0;
            counter.textContent = String(Math.max(0, n + (data.checked ? -1 : 1)));
          }
        }
        toast(data.checked ? '已完成，原笔记已同步' : '已恢复为未完成', 'ok');
      }).catch(function () {
        box.checked = !box.checked;  // 回滚
        toast('没能回写原笔记，请稍后再试', 'warn');
      }).then(function () {
        box.disabled = false;
      });
    });
  }

  // ===== 笔记快速复制：复制全文 Markdown / 纯文本 =====
  function initNoteCopy() {
    var dataEl = document.getElementById('note-copy-data');
    if (!dataEl) { return; }
    var data;
    try { data = JSON.parse(dataEl.textContent || '{}'); } catch (err) { return; }

    document.addEventListener('click', function (event) {
      var btn = event.target.closest('[data-copy-note]');
      if (!btn || !document.getElementById('note-copy-data')) { return; }
      var kind = btn.getAttribute('data-copy-note');
      var text = kind === 'plain' ? data.plain : data.md;
      if (!text) { toast('这篇笔记没有可复制的内容', 'warn'); return; }
      navigator.clipboard.writeText(text).then(function () {
        toast(kind === 'plain' ? '已复制纯文本' : '已复制 Markdown', 'ok');
      }, function () {
        toast('复制失败，浏览器不允许访问剪贴板', 'error');
      });
    });
  }

  // ===== 配色预设：六套配色，与亮暗主题正交，自定义下拉切换 =====
  function initPalettePicker() {
    var root = document.documentElement;
    var dd = document.getElementById('palette-dd');
    if (!dd) { return; }
    var KEY = 'inknote-palette';
    var toggle = dd.querySelector('.palette-dd__toggle');
    var menu = dd.querySelector('.palette-dd__menu');
    var label = dd.querySelector('.palette-dd__label');
    var NAMES = { '': '墨迹', bamboo: '竹青', ocean: '墨蓝', plum: '霞紫', amber: '琥珀', slate: '石墨' };

    function openMenu(on) {
      menu.hidden = !on;
      toggle.setAttribute('aria-expanded', on ? 'true' : 'false');
      dd.classList.toggle('is-open', on);
    }

    function apply(palette) {
      if (palette) { root.setAttribute('data-palette', palette); }
      else { root.removeAttribute('data-palette'); }
      if (label) { label.textContent = NAMES[palette] || '墨迹'; }
      dd.querySelectorAll('.palette-dd__opt').forEach(function (opt) {
        var active = (opt.getAttribute('data-palette-opt') || '') === palette;
        opt.classList.toggle('is-active', active);
        opt.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      try { window.localStorage.setItem(KEY, palette); } catch (err) { /* 忽略 */ }
    }

    toggle.addEventListener('click', function () {
      openMenu(menu.hidden);
    });

    menu.addEventListener('click', function (event) {
      var opt = event.target.closest('[data-palette-opt]');
      if (!opt) { return; }
      apply(opt.getAttribute('data-palette-opt') || '');
      openMenu(false);
    });

    // 点外面 / Esc 收起
    document.addEventListener('click', function (event) {
      if (!dd.contains(event.target)) { openMenu(false); }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !menu.hidden) { openMenu(false); }
    });

    // 初始高亮与文案（防闪烁脚本只设了 html 属性，label 在这里恢复）
    var current = root.getAttribute('data-palette') || '';
    if (label) { label.textContent = NAMES[current] || '墨迹'; }
    dd.querySelectorAll('.palette-dd__opt').forEach(function (opt) {
      var active = (opt.getAttribute('data-palette-opt') || '') === current;
      opt.classList.toggle('is-active', active);
      opt.setAttribute('aria-selected', active ? 'true' : 'false');
    });
  }

  // ===== 任务清单点击回写：阅读视图里点任务复选框，勾选态写回笔记正文 =====
  function initTaskToggle() {
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');

    // disabled 的 input 会吞掉鼠标事件，所以渲染时已放开 disabled；
    // 这里在 document 级接管：详情页（有 data-task-note）回写，其它页面只拦不存
    document.addEventListener('click', function (event) {
      var label = event.target.closest('.task-list-control');
      if (!label) { return; }
      var prose = label.closest('.prose');
      var noteId = prose && prose.getAttribute('data-task-note');
      var box = label.querySelector('input[data-task-index]');
      if (!box) { return; }
      event.preventDefault();
      if (!noteId || box.classList.contains('task-busy')) { return; }

      box.classList.add('task-busy');
      fetch('/notes/' + noteId + '/task-toggle', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-CSRF-Token': csrfMeta ? csrfMeta.getAttribute('content') : ''
        },
        credentials: 'same-origin',
        body: 'index=' + encodeURIComponent(box.getAttribute('data-task-index'))
      }).then(function (res) {
        return res.json().catch(function () { throw new Error('bad-json'); });
      }).then(function (data) {
        if (data && data.ok) {
          box.checked = !!data.checked;
        } else {
          toast((data && data.error) || '没能更新这个待办', 'warn');
        }
      }).catch(function () {
        toast('网络问题，待办没有保存', 'warn');
      }).then(function () {
        box.classList.remove('task-busy');
      });
    });
  }

  // ===== 「更多」动作菜单（details.menu-group）：点外面 / Esc 关闭 =====
  // details 只负责开合，不会自己收起来；同时开着两个菜单也会显得乱。
  function initMenuGroup() {
    var groups = Array.prototype.slice.call(document.querySelectorAll('details.menu-group'));
    if (!groups.length) { return; }

    function closeAll(except) {
      groups.forEach(function (group) {
        if (group !== except) { group.removeAttribute('open'); }
      });
    }

    groups.forEach(function (group) {
      group.addEventListener('toggle', function () {
        if (group.open) { closeAll(group); }
      });
      // 点了菜单里的动作就收起来（链接会跳转、表单会提交，留着开着很怪）
      group.addEventListener('click', function (event) {
        if (event.target.closest && event.target.closest('.menu-group__item')) {
          group.removeAttribute('open');
        }
      });
    });

    document.addEventListener('click', function (event) {
      var node = event.target;
      if (node && node.closest && node.closest('details.menu-group')) { return; }
      closeAll(null);
    });

    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') { return; }
      var open = document.querySelector('details.menu-group[open]');
      if (!open) { return; }
      open.removeAttribute('open');
      var trigger = open.querySelector('summary');
      if (trigger) { trigger.focus(); }
    });
  }

  ready(function () {
    safe(initTheme); safe(initPalette); safe(initConfirm); safe(initCodeCopy); safe(initNoteCopy); safe(initHeatmapScroll); safe(initTodoPage); safe(initPalettePicker); safe(initTaskToggle);
    safe(initToc); safe(initOffline); safe(initTitleAutoSize); safe(initKbdNav); safe(initMenuGroup);
    safe(function () { enhanceContent(document); });
    safe(initToolbarAutoSubmit);
  });
})();
