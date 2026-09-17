/*!
 * 墨痕 InkNote · onboarding.js
 * 首次使用分步引导：轻量、零依赖、可跳过、只出现一次。
 *
 * 设计契约：
 *  - 只在「已登录用户首次访问」时触发：localStorage 没有 `inknote.onboarded` 且
 *    页面上存在登录态标记（#search-open 或写笔记主按钮）时才启动。
 *  - 一旦启动立即把 `inknote.onboarded` 写死，之后任何页面（含导航后）都不再自动出现。
 *  - 每一步按 selector 查找目标，找不到就跳过该步；没有任何可用目标则不打扰用户。
 *  - 高亮用「目标元素自身 box-shadow 挖空 + 不切遮罩层」的做法；气泡自适应贴合并夹在视口内，
 *    窄屏改为底部固定条。
 *  - 不依赖任何页面特定结构，纯容错写法，绝不抛出导致页面崩溃的异常。
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'inknote.onboarded';
  var SPOT_CLASS = 'onboard-spot';

  // 小工具（与 app.js 风格一致，但不共享作用域，独立实现）
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function h(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text != null) { e.textContent = text; }
    return e;
  }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  // 预渲染守卫：页面被浏览器预渲染时先什么都不做（写标记/弹引导都等到真正激活）。
  // 否则「鼠标从链接上划过」就把一次性的新手引导消耗掉了。
  function whenActive(fn) {
    if (!document.prerendering) { fn(); return; }
    document.addEventListener('prerenderingchange', function () { fn(); }, { once: true });
  }

  function readLS(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function writeLS(key, val) {
    try { window.localStorage.setItem(key, val); } catch (e) { /* 隐私模式静默失败 */ }
  }

  // 引导步骤：sel 可为数组（按顺序取第一个命中的）；找不到目标的步骤会被自动跳过。
  var STEPS = [
    {
      sel: ['.btn--primary[href="/notes/new"]'],
      title: '写第一篇笔记',
      text: '点击「写笔记」开始记录。墨痕支持 Markdown、标签与全文搜索，随手就能记下灵感。'
    },
    {
      sel: ['#search-open', '.toolbar input[type="search"]'],
      title: '快速搜索',
      text: '点这里，或直接按 Ctrl + K 打开命令面板，输入关键词就能跳到任意一篇笔记。'
    },
    {
      sel: ['.filter-bar'],
      title: '按标签筛选',
      text: '用标签把笔记归拢起来，这里可以快速过滤出某一类笔记，找东西不再翻到底。'
    },
    {
      sel: ['#theme-toggle'],
      title: '切换深色模式',
      text: '夜里写作可以一键切到深色模式，护眼又安静。偏好会被记住，下次自动沿用。'
    }
  ];

  var started = false;        // 本页面是否已启动（防止重复）
  var active = false;         // 引导是否正在展示
  var pop = null;             // 气泡 DOM
  var target = null;          // 当前高亮目标
  var current = 0;            // 当前步骤索引（在已解析步骤数组里）
  var resolved = [];          // 解析后真正存在的步骤
  var restoreFocusEl = null;  // 启动前聚焦的元素，结束还回去
  var onResize = null, onScroll = null, onKey = null;

  function resolveSteps() {
    var out = [];
    for (var i = 0; i < STEPS.length; i++) {
      var sels = STEPS[i].sel;
      var el = null;
      for (var j = 0; j < sels.length; j++) {
        var found = $(sels[j]);
        if (found) { el = found; break; }
      }
      if (el) { out.push({ el: el, title: STEPS[i].title, text: STEPS[i].text }); }
    }
    return out;
  }

  function buildPop() {
    var box = h('div', 'onboard-pop');
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'false'); // 不是真正模态，避免制造焦点陷阱
    box.setAttribute('aria-live', 'polite');
    box.tabIndex = -1;

    var head = h('div', 'onboard-pop__head');
    var title = h('h2', 'onboard-pop__title', '');
    title.id = 'onboard-pop-title';
    var close = h('button', 'onboard-pop__close', '✕');
    close.type = 'button';
    close.setAttribute('aria-label', '跳过引导');
    head.appendChild(title);
    head.appendChild(close);

    var body = h('p', 'onboard-pop__body', '');
    body.id = 'onboard-pop-body';

    var dots = h('div', 'onboard-pop__dots');
    dots.setAttribute('aria-hidden', 'true');

    var actions = h('div', 'onboard-pop__actions');
    var skip = h('button', 'onboard-pop__btn onboard-pop__btn--skip', '跳过');
    skip.type = 'button';
    var count = h('span', 'onboard-pop__count', '');
    var prev = h('button', 'onboard-pop__btn onboard-pop__btn--prev', '上一步');
    prev.type = 'button';
    var next = h('button', 'onboard-pop__btn onboard-pop__btn--next', '下一步');
    next.type = 'button';

    actions.appendChild(skip);
    actions.appendChild(count);
    actions.appendChild(prev);
    actions.appendChild(next);

    box.appendChild(head);
    box.appendChild(body);
    box.appendChild(dots);
    box.appendChild(actions);

    box.setAttribute('aria-labelledby', 'onboard-pop-title');
    box.setAttribute('aria-describedby', 'onboard-pop-body');

    close.addEventListener('click', function () { finish(); });
    skip.addEventListener('click', function () { finish(); });
    prev.addEventListener('click', function () { go(current - 1); });
    next.addEventListener('click', function () {
      if (current >= resolved.length - 1) { finish(); } else { go(current + 1); }
    });

    return {
      box: box, title: title, body: body, dots: dots,
      skip: skip, count: count, prev: prev, next: next
    };
  }

  var ui = null;

  function renderDots() {
    while (ui.dots.firstChild) { ui.dots.removeChild(ui.dots.firstChild); }
    for (var i = 0; i < resolved.length; i++) {
      var d = h('span', i === current ? 'onboard-dot onboard-dot--active' : 'onboard-dot');
      ui.dots.appendChild(d);
    }
  }

  function layout() {
    if (!active || !target || !pop) { return; }
    var rect = target.getBoundingClientRect();
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var margin = 12;
    var gap = 12;
    var pw = pop.offsetWidth;
    var ph = pop.offsetHeight;

    pop.classList.toggle('onboard-pop--fixed', vw < 600);

    if (vw < 600) {
      // 窄屏：底部固定条
      pop.style.left = margin + 'px';
      pop.style.top = 'auto';
      pop.style.bottom = margin + 'px';
      pop.style.width = (vw - 2 * margin) + 'px';
      return;
    }

    var spaceBelow = vh - rect.bottom;
    var spaceAbove = rect.top;
    var place;
    if (spaceBelow >= ph + gap) { place = 'bottom'; }
    else if (spaceAbove >= ph + gap) { place = 'top'; }
    else if (rect.right < vw * 0.5) { place = 'right'; }
    else if (rect.left > pw + gap) { place = 'left'; }
    else { place = 'bottom'; }

    var left, top;
    if (place === 'bottom' || place === 'top') {
      if (place === 'bottom') { top = rect.bottom + gap; }
      else { top = rect.top - ph - gap; }
      left = clamp(rect.left + rect.width / 2 - pw / 2, margin, Math.max(margin, vw - pw - margin));
    } else {
      top = clamp(rect.top + rect.height / 2 - ph / 2, margin, Math.max(margin, vh - ph - margin));
      if (place === 'right') { left = rect.right + gap; }
      else { left = rect.left - pw - gap; }
    }
    pop.style.width = '';
    pop.style.left = clamp(left, margin, Math.max(margin, vw - pw - margin)) + 'px';
    pop.style.top = clamp(top, margin, Math.max(margin, vh - ph - margin)) + 'px';
    pop.style.bottom = 'auto';
  }

  function showStep(idx) {
    current = clamp(idx, 0, resolved.length - 1);
    var step = resolved[current];

    if (target && target !== step.el) { target.classList.remove(SPOT_CLASS); }
    target = step.el;
    target.classList.add(SPOT_CLASS);

    ui.title.textContent = step.title;
    ui.body.textContent = step.text;
    ui.count.textContent = (current + 1) + ' / ' + resolved.length;
    ui.prev.style.visibility = current === 0 ? 'hidden' : 'visible';
    ui.next.textContent = current >= resolved.length - 1 ? '完成' : '下一步';
    renderDots();

    // 等一帧让元素尺寸稳定再定位
    pop.style.visibility = 'hidden';
    requestAnimationFrame(function () {
      layout();
      pop.style.visibility = 'visible';
      ui.next.focus(); // 自动聚焦「下一步」
    });
  }

  function go(idx) {
    if (idx < 0 || idx > resolved.length - 1) { return; }
    showStep(idx);
  }

  function teardownDom() {
    if (target) { target.classList.remove(SPOT_CLASS); target = null; }
    if (pop && pop.parentNode) { pop.parentNode.removeChild(pop); }
    pop = null;
    if (onResize) { window.removeEventListener('resize', onResize); onResize = null; }
    if (onScroll) { window.removeEventListener('scroll', onScroll, true); onScroll = null; }
    if (onKey) { document.removeEventListener('keydown', onKey, true); onKey = null; }
  }

  function finish() {
    if (!active) { return; }
    active = false;
    writeLS(STORAGE_KEY, '1'); // 写死标记：永不再自动出现
    teardownDom();
    // 把焦点还给页面，避免留下焦点陷阱
    try {
      if (restoreFocusEl && typeof restoreFocusEl.focus === 'function' && document.contains(restoreFocusEl)) {
        restoreFocusEl.focus();
      } else {
        (document.body || document.documentElement).focus();
      }
    } catch (e) { /* 忽略 */ }
  }

  function start() {
    if (started) { return; }
    started = true;

    // 触发条件：未 onboard 且处于登录态
    if (readLS(STORAGE_KEY)) { return; }
    var authed = $('#search-open') || $('.btn--primary[href="/notes/new"]');
    if (!authed) { return; }

    resolved = resolveSteps();
    if (!resolved.length) { return; } // 没有任何可用目标，不打扰

    restoreFocusEl = document.activeElement;

    // 启动即写标记：即便用户在导航中离开，本会话及以后都不再自动出现
    writeLS(STORAGE_KEY, '1');

    ui = buildPop();
    pop = ui.box;
    pop.style.visibility = 'hidden';
    (document.body || document.documentElement).appendChild(pop);

    active = true;

    var layoutRaf = 0;
    function scheduleLayout() {
      if (layoutRaf) { return; }
      layoutRaf = window.requestAnimationFrame(function () { layoutRaf = 0; layout(); });
    }
    onResize = function () { scheduleLayout(); };
    onScroll = function () { scheduleLayout(); };
    onKey = function (ev) {
      if (!active) { return; }
      var key = ev.key;
      if (key === 'Escape') { ev.preventDefault(); finish(); }
      else if (key === 'ArrowRight') { ev.preventDefault(); go(current + 1); }
      else if (key === 'ArrowLeft') { ev.preventDefault(); go(current - 1); }
    };
    window.addEventListener('resize', onResize);
    window.addEventListener('scroll', onScroll, { passive: true, capture: true });
    document.addEventListener('keydown', onKey, true);

    showStep(0);
  }

  // 暴露「重看引导」入口（设置页 / 命令面板接线时调用）
  function resetAndStart() {
    writeLS(STORAGE_KEY, ''); // 清标记
    started = false;
    start();
  }
  try {
    var InkNote = window.InkNote = window.InkNote || {};
    InkNote.onboarding = { start: start, reset: resetAndStart };
  } catch (e) { /* 忽略 */ }

  // 预渲染中的页面不算「用户来过」：等激活后再决定要不要弹引导
  function boot() { whenActive(start); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
