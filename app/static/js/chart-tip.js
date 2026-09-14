/**
 * 站内悬浮提示（全站组件）：把浏览器原生的 title 小黑条换成自绘浮层。
 *
 * 为什么不用 title：它由浏览器绘制（系统字体 / 位置固定 / 延迟约 1 秒 / 无法做排版），
 * 和站点配色对不上，也没法显示「输入输出占比」这类结构化信息。而且 title 在移动端
 * 基本不触发（长按才有反应），键盘用户更是完全看不到。
 *
 * 数据从元素上的 data-tip-* 属性读，所以模板仍然可以只渲染一份数据：
 *   data-tip-title      标题（必填）
 *   data-tip-calls/tokens/prompt/completion/failed/latency
 *                       用量图表的结构化行（缺口按需显示，0 值不显示）
 *   data-tip-lines      JSON：[["标签","值"], …] —— 通用键值行（历史任务、热力图等）
 *   data-tip-note       备注块（多行文本，pre-wrap：步骤链、错误原因这类长内容）
 *
 * 自动桥接：没有 data-tip-* 但带原生 title 的元素（图标提示、删除按钮这类）
 * 也会走自绘浮层 —— show() 时把 title 摘下存进 data-tip-stash-title（防止
 * 原生气泡和浮层叠出双份），hide() 时还原。所以 SSR 标记里的 title 仍然
 * 是无 JS 环境的兜底，一个都不用从模板里删。
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
  var noteEl = null;
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
    noteEl = document.createElement('div');
    noteEl.className = 'chart-tip__note';
    tip.appendChild(noteEl);
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
    var title = data.tipTitle;
    var isUsage = false;
    if (!title) {
      // 自动桥接：纯原生 title 的元素（图标提示、删除按钮…）也走自绘浮层。
      // title 可能已被摘到 stash（进入元素瞬间就摘，防原生气泡抢先）——兜底读暂存
      title = el.getAttribute('title');
      if (!title || !title.trim()) { title = data.tipStashTitle; }
      if (!title || !title.trim()) { return null; }
      return { title: title.trim(), simple: true };
    }
    if (data.tipCalls !== undefined || data.tipTokens !== undefined) {
      isUsage = true;
    }
    var lines = null;
    if (data.tipLines) {
      try {
        var parsed = JSON.parse(data.tipLines);
        if (parsed instanceof Array && parsed.length) { lines = parsed; }
      } catch (err) { /* 坏数据就当没有 */ }
    }
    return {
      title: title,
      calls: toNumber(data.tipCalls),
      tokens: toNumber(data.tipTokens),
      prompt: toNumber(data.tipPrompt),
      completion: toNumber(data.tipCompletion),
      failed: toNumber(data.tipFailed),
      latency: toNumber(data.tipLatency),
      lines: lines,
      note: data.tipNote || '',
      usage: isUsage
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

    // 通用键值行（历史任务 / 热力图这类非用量场景）
    (info.lines || []).forEach(function (pair) {
      if (pair && pair.length >= 2) { addRow(String(pair[0]), String(pair[1]), {}); }
    });

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

    // 备注块（步骤链 / 错误原因这类长内容）
    noteEl.textContent = info.note || '';
    noteEl.hidden = !info.note;

    var usageEmpty = info.usage && !info.calls && !info.tokens;
    if (usageEmpty && !(info.lines || []).length && !info.note) {
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

  // 原生 title 的摘下与还原：面板显示期间不让原生气泡和浮层叠出双份；
  // 离开后还原属性，无 JS 环境的兜底不受影响
  function stashTitle(el) {
    if (el && el.hasAttribute('title')) {
      el.setAttribute('data-tip-stash-title', el.getAttribute('title'));
      el.removeAttribute('title');
    }
  }

  function restoreTitle(el) {
    if (el && el.hasAttribute('data-tip-stash-title')) {
      el.setAttribute('title', el.getAttribute('data-tip-stash-title'));
      el.removeAttribute('data-tip-stash-title');
    }
  }

  function show(el, keep) {
    var info = readInfo(el);
    if (!info) { return; }
    if (!tip) { build(); }   // 动态渲染的元素（如助手的历史任务）出现时才建浮层
    if (anchor && anchor !== el) { restoreTitle(anchor); }
    render(info);
    stashTitle(el);
    anchor = el;
    pendingEl = null;
    pinned = !!keep;
    tip.classList.add('is-visible');
    tip.setAttribute('aria-hidden', 'false');
    place(el);
  }

  // 悬停延迟：鼠标横扫页面时不闪面板；停住约 160ms 才出现，移开立即消失。
  // 键盘导航（pinned）与已显示的元素不走延迟。
  // 同一元素内部继续移动不重置计时——大卡片上扫过也能正常出现
  var SHOW_DELAY = 160;
  var showTimer = null;
  var pendingEl = null;

  function scheduleShow(el) {
    if (showTimer && pendingEl === el) { return; }
    cancelShow();
    pendingEl = el;
    showTimer = setTimeout(function () { show(el); }, SHOW_DELAY);
  }

  function cancelShow() {
    clearTimeout(showTimer);
    showTimer = null;
    // 还在延迟等待中的元素：title 是一进来就摘掉的，撤销时还原
    if (pendingEl && pendingEl !== anchor) { restoreTitle(pendingEl); }
    pendingEl = null;
  }

  function hide() {
    cancelShow();
    restoreTitle(anchor);
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
    // data-tip-stash-title 也要算：title 在进入瞬间就被摘到 stash 里了，
    // 不补上这条，指针事件的 target（stashed 的按钮）会跳过自身解析到外层元素
    var el = node && node.closest
      ? node.closest('[data-tip-title], [title], [data-tip-stash-title]')
      : null;
    return el;
  }

  function onPointerOver(event) {
    var el = closestTipped(event.target);
    // 键盘选中后鼠标又移过来：允许切换到鼠标指的这根
    if (pinned && el === anchor) { return; }
    if (el) {
      // 已显示的就是它：不重渲染（避免面板在元素内部移动时闪烁）
      if (el === anchor && tip && tip.classList.contains('is-visible')) { return; }
      if (el !== anchor && anchor) { hide(); }   // 换了目标：旧面板立刻收起
      // **一进来就摘 title**：不能等面板显示才摘——延迟窗口里 title 还在的话，
      // 原生气泡（尤其刚展示过一次、再触发极快的）会抢在自绘面板前面冒出来
      stashTitle(el);
      scheduleShow(el);
      return;
    }
    if (tip && !tip.contains(event.target)) { hide(); }
  }

  function onPointerOut(event) {
    var el = closestTipped(event.target);
    if (!el || pinned) { return; }
    // 移到同一根柱子内部不算移开
    if (event.relatedTarget && el.contains(event.relatedTarget)) { return; }
    if (closestTipped(event.relatedTarget) === el) { return; }
    // 延迟等待期间就离开：撤销计时（cancelShow 会把摘掉的 title 还原）
    if (el === pendingEl) { cancelShow(); }
    if (el === anchor) { hide(); }
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
    // 全站组件：监听常驻，浮层在第一次 show() 时才构建 ——
    // 像「历史任务」这种 JS 动态渲染的条目，DOMContentLoaded 时还不存在
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
