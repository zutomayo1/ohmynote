/**
 * 图表悬浮提示：把浏览器原生的 title 小黑条换成站内自绘的浮层。
 *
 * 为什么不用 title：它由浏览器绘制（系统字体 / 位置固定 / 延迟约 1 秒 / 无法做排版），
 * 和站点配色对不上，也没法显示「输入输出占比」这类结构化信息。而且 title 在移动端
 * 基本不触发（长按才有反应），键盘用户更是完全看不到。
 *
 * 数据从元素上的 data-tip-* 属性读，所以模板仍然可以只渲染一份数据。
 * 原生 title 属性保留着 —— 没有 JS 的环境照旧能看到基础信息，这是渐进增强。
 *
 * 键盘：带 data-tip-nav 的容器 tabindex=0，方向键在柱子之间移动并显示提示。
 * 悬浮提示自身 pointer-events: none，不会挡住鼠标。
 */
(function () {
  'use strict';

  // data-tip-x → 提示里的行。顺序即显示顺序。
  var ROWS = [
    { key: 'calls', label: '调用', suffix: ' 次' },
    { key: 'prompt', label: '输入', suffix: ' token' },
    { key: 'completion', label: '输出', suffix: ' token' },
    { key: 'tokens', label: '合计', suffix: ' token', strong: true },
    { key: 'failed', label: '失败', suffix: ' 次', warn: true }
  ];

  var tip = null;
  var headEl = null;
  var rowsEl = null;
  var splitEl = null;
  var anchor = null;
  var pinned = false;   // 键盘选中的那根柱子（键盘导航时提示不随鼠标移开而消失）

  function toNumber(value) {
    var n = parseInt(value, 10);
    return isNaN(n) ? 0 : n;
  }

  function fmt(value) {
    return Number(value || 0).toLocaleString('zh-CN');
  }

  function seconds(ms) {
    var s = Number(ms || 0) / 1000;
    if (!s) { return ''; }
    return (s >= 10 ? Math.round(s) : Math.round(s * 10) / 10) + 's';
  }

  function build() {
    tip = document.createElement('div');
    tip.className = 'chart-tip';
    tip.setAttribute('role', 'tooltip');
    tip.setAttribute('aria-hidden', 'true');

    headEl = document.createElement('div');
    headEl.className = 'chart-tip__head';
    rowsEl = document.createElement('div');
    rowsEl.className = 'chart-tip__rows';
    splitEl = document.createElement('div');
    splitEl.className = 'chart-tip__split';

    tip.appendChild(headEl);
    tip.appendChild(rowsEl);
    tip.appendChild(splitEl);
    document.body.appendChild(tip);
  }

  function addRow(label, value, options) {
    if (!value) { return; }
    var row = document.createElement('div');
    row.className = 'chart-tip__row' + (options.warn ? ' is-warn' : '')
      + (options.strong ? ' is-strong' : '');
    var name = document.createElement('span');
    name.className = 'chart-tip__label';
    name.textContent = label;
    var val = document.createElement('span');
    val.className = 'chart-tip__value';
    val.textContent = value;
    row.appendChild(name);
    row.appendChild(val);
    rowsEl.appendChild(row);
  }

  function readInfo(el) {
    var data = el.dataset || {};
    if (!data.tipTitle) { return null; }
    return {
      title: data.tipTitle,
      calls: toNumber(data.tipCalls),
      tokens: toNumber(data.tipTokens),
      prompt: toNumber(data.tipPrompt),
      completion: toNumber(data.tipCompletion),
      failed: toNumber(data.tipFailed),
      latency: toNumber(data.tipLatency)
    };
  }

  function render(info) {
    headEl.textContent = info.title;
    rowsEl.textContent = '';
    splitEl.textContent = '';
    splitEl.style.removeProperty('display');

    ROWS.forEach(function (row) {
      // 值为 0 的行不显示：一排「失败 0 次」只是噪声
      addRow(row.label, info[row.key] ? fmt(info[row.key]) + row.suffix : '', row);
    });

    var latency = seconds(info.latency);
    if (latency) { addRow('平均耗时', latency, { warn: false }); }

    if (info.tokens) {
      // 输入 / 输出 占比条：和柱子里那两段同色，一眼对应上
      var promptPct = Math.min(100, Math.round(info.prompt * 100 / info.tokens));
      var prompt = document.createElement('span');
      prompt.className = 'chart-tip__seg chart-tip__seg--prompt';
      prompt.style.width = promptPct + '%';
      var completion = document.createElement('span');
      completion.className = 'chart-tip__seg chart-tip__seg--completion';
      completion.style.width = (100 - promptPct) + '%';
      splitEl.appendChild(prompt);
      splitEl.appendChild(completion);
    } else {
      splitEl.style.display = 'none';
    }

    if (!info.calls && !info.tokens) {
      var blank = document.createElement('div');
      blank.className = 'chart-tip__empty';
      blank.textContent = '这段时间没有调用';
      rowsEl.appendChild(blank);
    }
  }

  function place(el) {
    var rect = el.getBoundingClientRect();
    var box = tip.getBoundingClientRect();
    var gap = 10;
    var top = rect.top - box.height - gap;
    var below = false;
    if (top < 8) {
      top = rect.bottom + gap;
      below = true;
    }
    var left = rect.left + rect.width / 2 - box.width / 2;
    var maxLeft = window.innerWidth - box.width - 8;
    left = Math.max(8, Math.min(left, Math.max(8, maxLeft)));
    tip.style.transform = 'translate(' + Math.round(left) + 'px,' + Math.round(top) + 'px)';
    tip.classList.toggle('is-below', below);
    // 箭头跟着柱子中心走（浮层被视口边缘推到一边时仍然指得准）
    tip.style.setProperty('--arrow-x', Math.round(rect.left + rect.width / 2 - left) + 'px');
  }

  function show(el, keep) {
    var info = readInfo(el);
    if (!info) { return; }
    render(info);
    anchor = el;
    pinned = !!keep;
    tip.classList.add('is-visible');
    tip.setAttribute('aria-hidden', 'false');
    place(el);
  }

  function hide() {
    anchor = null;
    pinned = false;
    if (!tip) { return; }
    tip.classList.remove('is-visible');
    tip.setAttribute('aria-hidden', 'true');
    var active = document.querySelector('[data-tip-active]');
    if (active) { active.removeAttribute('data-tip-active'); }
  }

  function reposition() {
    if (!anchor || !tip.classList.contains('is-visible')) { return; }
    var rect = anchor.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > window.innerHeight) { hide(); return; }
    place(anchor);
  }

  function closestTipped(node) {
    var el = node && node.closest ? node.closest('[data-tip-title]') : null;
    return el;
  }

  function onPointerOver(event) {
    var el = closestTipped(event.target);
    // 键盘选中后鼠标又移过来：允许切换到鼠标指的这根
    if (pinned && el === anchor) { return; }
    if (el) { show(el); return; }
    if (tip && !tip.contains(event.target)) { hide(); }
  }

  function onPointerOut(event) {
    var el = closestTipped(event.target);
    if (!el || el !== anchor || pinned) { return; }
    // 移到同一根柱子内部不算移开
    if (event.relatedTarget && el.contains(event.relatedTarget)) { return; }
    if (closestTipped(event.relatedTarget)) { return; }
    hide();
  }

  function initNav(root) {
    var index = -1;

    function columns() {
      return Array.prototype.slice.call(root.querySelectorAll('[data-tip-title]'));
    }

    function activate(next) {
      var list = columns();
      if (!list.length) { return; }
      index = Math.max(0, Math.min(list.length - 1, next));
      list.forEach(function (item, i) {
        if (i === index) { item.setAttribute('data-tip-active', ''); }
        else { item.removeAttribute('data-tip-active'); }
      });
      show(list[index], true);
    }

    root.addEventListener('keydown', function (event) {
      var key = event.key;
      if (key === 'ArrowRight' || key === 'ArrowDown') {
        activate(index < 0 ? 0 : index + 1);
      } else if (key === 'ArrowLeft' || key === 'ArrowUp') {
        activate(index < 0 ? 0 : index - 1);
      } else if (key === 'Home') {
        activate(0);
      } else if (key === 'End') {
        activate(columns().length - 1);
      } else if (key === 'Escape') {
        hide();
        return;
      } else {
        return;
      }
      event.preventDefault();
    });

    root.addEventListener('blur', function () { hide(); });
    root.addEventListener('focus', function () { if (index < 0) { activate(0); } });
  }

  function init() {
    if (!document.querySelector('[data-tip-title]')) { return; }
    build();
    document.addEventListener('pointerover', onPointerOver, true);
    document.addEventListener('pointerout', onPointerOut, true);
    window.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', reposition);
    document.querySelectorAll('[data-tip-nav]').forEach(initNav);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
