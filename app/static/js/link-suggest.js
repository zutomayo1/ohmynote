/* 笔记链接补全：让「建立联系」不用手打精确标题。
 *
 * 两个入口共用同一套下拉：
 *   1) 编辑器正文里输入 `[[` → 就地弹出标题候选，↑↓ 选、Enter/Tab 插入完整 `[[标题]]`；
 *   2) 笔记页「建立联系」输入框 → 选一篇笔记当链接目标。
 *
 * 候选来自 `/api/note-titles`（**只匹配标题**，不是 /api/search 的全文搜索——
 * 全文命中会推荐标题里根本没有关键词的笔记，用户会莫名其妙）。
 *
 * 下拉挂在 document.body 上并用 fixed 定位：编辑器/侧栏都有 overflow 容器，
 * 挂在原地会被裁掉（这个项目在浮层上踩过好几次）。
 */
(function () {
  'use strict';

  var URL_DEFAULT = '/api/note-titles';
  var LIMIT = 8;
  var DEBOUNCE = 130;

  var box = null;        // 共享的下拉容器
  var active = null;     // 当前正在使用下拉的会话

  function ensureBox() {
    if (box && document.body.contains(box)) { return box; }
    box = document.createElement('div');
    box.className = 'link-suggest';
    box.setAttribute('role', 'listbox');
    box.setAttribute('aria-label', '匹配的笔记');
    box.hidden = true;
    document.body.appendChild(box);
    return box;
  }

  function closeBox() {
    if (!box) { return; }
    box.hidden = true;
    box.innerHTML = '';
  }

  function placeAt(rect, minWidth) {
    var el = ensureBox();
    var width = Math.max(minWidth || 200, rect.width);
    el.style.minWidth = Math.min(width, 360) + 'px';
    // 先显示再量高度，避免贴着视口底部时算错
    el.hidden = false;
    var height = el.offsetHeight || 0;
    var below = rect.bottom + 6;
    var top = below;
    if (below + height > window.innerHeight - 8) {
      top = Math.max(8, rect.top - height - 6);   // 下方放不下就翻到上面
    }
    el.style.top = Math.round(top) + 'px';
    el.style.left = Math.round(Math.min(rect.left, window.innerWidth - width - 12)) + 'px';
  }

  function renderItems(items, emptyText, onPick) {
    var el = ensureBox();
    el.innerHTML = '';
    if (!items.length) {
      var empty = document.createElement('p');
      empty.className = 'link-suggest__empty';
      empty.textContent = emptyText || '没有匹配的笔记';
      el.appendChild(empty);
      return;
    }
    items.forEach(function (item, i) {
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'link-suggest__item';
      row.id = 'link-suggest-opt-' + i;
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', 'false');
      var name = document.createElement('span');
      name.className = 'link-suggest__name';
      name.textContent = item.title;
      var meta = document.createElement('span');
      meta.className = 'link-suggest__meta';
      meta.textContent = (item.updated_at || '').slice(0, 10);
      row.appendChild(name);
      row.appendChild(meta);
      row.addEventListener('mousedown', function (ev) {
        ev.preventDefault();          // 别让输入框先失焦，否则下拉已经被关掉
        onPick(item);
      });
      el.appendChild(row);
    });
  }

  function markActive(index) {
    if (!box) { return; }
    var rows = box.querySelectorAll('.link-suggest__item');
    for (var i = 0; i < rows.length; i++) {
      var on = i === index;
      rows[i].classList.toggle('is-active', on);
      rows[i].setAttribute('aria-selected', on ? 'true' : 'false');
    }
    if (rows[index]) { rows[index].scrollIntoView({ block: 'nearest' }); }
  }

  function fetchItems(url, query, exclude, done) {
    var qs = '?q=' + encodeURIComponent(query || '') + '&limit=' + LIMIT;
    if (exclude) { qs += '&exclude=' + encodeURIComponent(exclude); }
    fetch(url + qs, { headers: { Accept: 'application/json' }, credentials: 'same-origin' })
      .then(function (res) { return res.ok ? res.json() : { items: [] }; })
      .then(function (data) { done((data && data.items) || []); })
      .catch(function () { done([]); });   // 网络/未登录：静默降级成手打标题
  }

  /* ------------------------------------------------------------ 用法一：普通输入框 */
  function attachInput(input, options) {
    if (!input) { return null; }
    var opts = options || {};
    var url = opts.url || input.getAttribute('data-suggest-url') || URL_DEFAULT;
    var exclude = opts.exclude || input.getAttribute('data-exclude') || '';
    var session = { items: [], index: -1, input: input };
    var timer = null;

    function hide() {
      if (active === session) { active = null; }
      closeBox();
    }

    function show(items) {
      session.items = items;
      session.index = items.length ? 0 : -1;
      active = session;
      renderItems(items, opts.emptyText, pick);
      markActive(session.index);
      placeAt(input.getBoundingClientRect(), 220);
    }

    function pick(item) {
      input.value = item.title;
      if (typeof opts.onPick === 'function') { opts.onPick(item); }
      hide();
      input.focus();
    }

    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-autocomplete', 'list');

    input.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        if (document.activeElement !== input) { return; }
        fetchItems(url, input.value.trim(), exclude, show);
      }, DEBOUNCE);
    });
    input.addEventListener('focus', function () {
      if (input.value.trim()) { return; }
      fetchItems(url, '', exclude, function (items) {
        if (items.length) { show(items); }   // 空输入时给「最近更新」当候选
      });
    });
    input.addEventListener('blur', function () { setTimeout(hide, 160); });

    input.addEventListener('keydown', function (ev) {
      if (active !== session || !session.items.length) {
        if (ev.key === 'Escape') { hide(); }
        return;
      }
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        var delta = ev.key === 'ArrowDown' ? 1 : -1;
        session.index = (session.index + delta + session.items.length) % session.items.length;
        markActive(session.index);
      } else if (ev.key === 'Enter' || ev.key === 'Tab') {
        if (session.index < 0) { return; }
        // Enter 只有在确实选中一项时才吃掉，否则交给表单正常提交
        ev.preventDefault();
        pick(session.items[session.index]);
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        hide();
      }
    });

    return { hide: hide, session: session };
  }

  /* ------------------------------------------- 用法二：编辑器正文里的 [[ 补全 */
  function caretRect(ta, index) {
    // 用镜像 div 量光标位置：textarea 没有 API 能直接拿到插入符坐标
    var cs = window.getComputedStyle(ta);
    var lineHeight = parseFloat(cs.lineHeight);
    if (!lineHeight || isNaN(lineHeight)) { lineHeight = parseFloat(cs.fontSize) * 1.6 || 20; }
    var mirror = document.createElement('div');
    var props = ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'letterSpacing',
                 'textTransform', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
                 'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
                 'textIndent', 'wordSpacing'];
    props.forEach(function (k) { mirror.style[k] = cs[k]; });
    mirror.style.position = 'fixed';
    mirror.style.left = '-9999px';
    mirror.style.top = '0';
    mirror.style.whiteSpace = 'pre-wrap';
    mirror.style.wordWrap = 'break-word';
    mirror.style.overflowWrap = cs.overflowWrap;
    mirror.style.boxSizing = 'border-box';
    mirror.style.width = ta.clientWidth + 'px';
    mirror.textContent = ta.value.slice(0, index);
    var marker = document.createElement('span');
    marker.textContent = "\u200b";
    mirror.appendChild(marker);
    document.body.appendChild(mirror);
    var rect = ta.getBoundingClientRect();
    var top = rect.top + marker.offsetTop - ta.scrollTop;
    var left = rect.left + marker.offsetLeft - ta.scrollLeft;
    document.body.removeChild(mirror);
    if (!isFinite(top) || !isFinite(left)) {
      top = rect.bottom - lineHeight - ta.scrollTop;
      left = rect.left + 8;
    }
    // 下拉放到这一行的下方
    return { top: top + lineHeight, bottom: top + lineHeight, left: left, width: 220 };
  }

  function attachWiki(textarea, options) {
    if (!textarea) { return null; }
    var opts = options || {};
    var url = opts.url || URL_DEFAULT;
    var exclude = opts.exclude || '';
    var session = { items: [], index: -1, start: -1, end: -1, textarea: textarea };
    var timer = null;
    var caret = 0;

    function hide() {
      if (active === session) { active = null; }
      closeBox();
      textarea.setAttribute('aria-expanded', 'false');
    }

    // 光标前是不是一个还没写完的 [[xxx
    function wikiMatch() {
      caret = textarea.selectionStart;
      if (textarea.selectionEnd !== caret) { return null; }   // 有选区时不补全
      var before = textarea.value.slice(0, caret);
      var at = before.lastIndexOf('[[');
      if (at < 0) { return null; }
      var partial = before.slice(at + 2);
      if (partial.length > 60) { return null; }
      if (partial.indexOf(']') >= 0 || partial.indexOf("\n') >= 0 || partial.indexOf('|") >= 0) {
        return null;
      }
      return { start: at, query: partial };
    }

    function show(query, at) {
      fetchItems(url, query, exclude, function (items) {
        if (!items.length && !query) { hide(); return; }
        session.items = items;
        session.index = items.length ? 0 : -1;
        session.start = at;
        session.query = query;
        // 光标后面已经是 ]] 或 |别名 时，一并替换掉（工具栏按钮会插出 [[占位]]）
        var rest = textarea.value.slice(caret);
        session.end = rest.indexOf(']]') === 0 ? caret + 2
          : (rest.charAt(0) === '|' ? caret : caret);
        active = session;
        renderItems(items, '没找到标题匹配的笔记（也可以直接写完标题）', pick);
        markActive(session.index);
        placeAt(caretRect(textarea, at), 220);
        textarea.setAttribute('aria-expanded', 'true');
      });
    }

    function refresh() {
      var match = wikiMatch();
      if (!match) { hide(); return; }
      // 只是挪了光标（方向键 / Home / End）：位置与关键词都没变，就别重新拉取 ——
      // 重新 show() 会把选中项重置回第一条，用户按 ↓ 选完随手一按就插错了。
      if (active === session && session.start === match.start && session.query === match.query) {
        return;
      }
      show(match.query, match.start);
    }

    function scheduleRefresh() {
      clearTimeout(timer);
      timer = setTimeout(refresh, 90);
    }

    function pick(item) {
      var text = '[[' + item.title + ']]';
      var value = textarea.value;
      var end = session.end >= 0 ? session.end : caret;
      textarea.value = value.slice(0, session.start) + text + value.slice(end);
      var pos = session.start + text.length;
      textarea.setSelectionRange(pos, pos);
      textarea.dispatchEvent(new Event('input', { bubbles: true }));  // 让自动保存/预览跟上
      hide();
      textarea.focus();
    }

    textarea.setAttribute('aria-autocomplete', 'list');
    textarea.addEventListener('input', scheduleRefresh);
    textarea.addEventListener('keyup', function (ev) {
      // 纯光标移动（方向键/Home/End）也要重新判断位置
      if (ev.key && ev.key.indexOf('Arrow') === 0 || ev.key === 'Home' || ev.key === 'End') {
        scheduleRefresh();
      }
    });
    textarea.addEventListener('click', scheduleRefresh);
    textarea.addEventListener('blur', function () { setTimeout(hide, 160); });

    // 用捕获阶段：编辑器自身也监听 keydown（缩进、Ctrl+S 等），必须先把下拉的方向键吃掉
    textarea.addEventListener('keydown', function (ev) {
      if (active !== session) { return; }
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        ev.stopPropagation();
        var delta = ev.key === 'ArrowDown' ? 1 : -1;
        session.index = (session.index + delta + session.items.length) % session.items.length;
        markActive(session.index);
      } else if (ev.key === 'Enter' || ev.key === 'Tab') {
        if (session.index < 0) { return; }
        ev.preventDefault();
        ev.stopPropagation();
        pick(session.items[session.index]);
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        ev.stopPropagation();
        hide();
      }
    }, true);

    return {
      hide: hide,
      // 工具栏「双链」按钮：插上 [[ 并把候选框打开（比插一个占位符再手打强）
      open: function () {
        var sel = { start: textarea.selectionStart, end: textarea.selectionEnd };
        var value = textarea.value;
        textarea.value = value.slice(0, sel.start) + '[[' + value.slice(sel.end);
        var pos = sel.start + 2;
        textarea.setSelectionRange(pos, pos);
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        textarea.focus();
        refresh();
      }
    };
  }

  // 自动挂载：笔记页的「建立联系」输入框（编辑器的补全由 editor.js 显式接，
  // 因为工具栏按钮要拿到 handle）
  function boot() {
    var inputs = document.querySelectorAll('[data-link-add-input]');
    for (var i = 0; i < inputs.length; i++) {
      (function (input) {
        var form = input.form;
        attachInput(input, {
          url: input.getAttribute('data-suggest-url') || URL_DEFAULT,
          exclude: input.getAttribute('data-exclude') || '',
          emptyText: '没有标题匹配的笔记 —— 也可以先建好那篇笔记',
          onPick: function (item) {
            var hidden = form && form.querySelector('[data-link-target-id]');
            if (hidden) { hidden.value = item.id; }   // 选中则按 id 精确提交
          }
        });
        // 手打标题（没选候选）时清掉 id，交给服务端按标题解析
        input.addEventListener('input', function () {
          var hidden = form && form.querySelector('[data-link-target-id]');
          if (hidden) { hidden.value = ''; }
        });
      })(inputs[i]);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  window.LinkSuggest = { attachInput: attachInput, attachWiki: attachWiki };
})();
