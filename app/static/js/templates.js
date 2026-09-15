/*!
 * 墨痕 InkNote · templates.js
 * 模板页的小交互：AI 生成模板（回显进编辑器，不落库）+ 卡片「复制内容」。
 * 没有 JS 时页面完全可用：AI 生成是额外的便捷入口，复制有 title 兜底提示。
 */
(function () {
  'use strict';

  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn, { once: true });
    else fn();
  }

  function csrfToken() {
    try {
      if (window.InkNote && typeof window.InkNote.csrf === 'function') {
        var value = window.InkNote.csrf();
        if (value) return value;
      }
    } catch (error) { /* 忽略，走 meta 兜底 */ }
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? (meta.getAttribute('content') || '') : '';
  }

  function toast(message, kind) {
    try {
      if (window.InkNote && typeof window.InkNote.toast === 'function') {
        window.InkNote.toast(message, kind);
        return;
      }
    } catch (error) { /* 忽略 */ }
    if (window.console) window.console.log('[模板]', message);
  }

  /* ---------- AI 生成模板 ---------- */
  function initAiGenerate() {
    var btn = document.getElementById('tpl-ai-btn');
    var topicInput = document.getElementById('tpl-ai-topic');
    if (!btn || !topicInput) { return; }
    var form = btn.closest('form');
    var nameInput = form ? form.querySelector('input[name="name"]') : null;
    var descInput = form ? form.querySelector('input[name="description"]') : null;
    var contentInput = form ? form.querySelector('textarea[name="content"]') : null;

    function setBusy(busy) {
      btn.disabled = busy;
      btn.classList.toggle('is-busy', busy);
      topicInput.disabled = busy;
    }

    function generate() {
      var topic = (topicInput.value || '').trim();
      if (!topic) { toast('先写一句想要什么模板', 'error'); topicInput.focus(); return; }
      if (!nameInput || !contentInput) { return; }
      setBusy(true);
      var body = new FormData();
      body.append('topic', topic);
      fetch('/templates/ai-generate', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrfToken() },
        body: body,
      }).then(function (res) {
        return res.json().then(function (data) { return { ok: res.ok, data: data }; });
      }).then(function (result) {
        if (!result.ok || result.data.error) {
          toast(result.data.error || 'AI 生成失败，请重试', 'error');
          return;
        }
        // 名称只在空着时填（用户可能已起好名），说明与内容直接覆盖
        if (!nameInput.value.trim() && result.data.name) { nameInput.value = result.data.name; }
        if (descInput && result.data.description) { descInput.value = result.data.description; }
        // 名称只在空着时填（用户可能已起好名），说明与内容直接覆盖
        if (!nameInput.value.trim() && result.data.name) { nameInput.value = result.data.name; }
        if (descInput && result.data.description) { descInput.value = result.data.description; }
        contentInput.value = result.data.content || '';
        toast('已生成，检查一下再保存', 'ok');
      }).catch(function () {
        toast('网络错误，生成失败', 'error');
      }).finally(function () { setBusy(false); });
    }

    btn.addEventListener('click', generate);
    topicInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        generate();
      }
    });
  }

  /* ---------- 卡片「复制内容」 ---------- */
  function initCopy() {
    document.addEventListener('click', function (event) {
      var btn = event.target.closest ? event.target.closest('.tpl-copy') : null;
      if (!btn) { return; }
      var content = btn.getAttribute('data-content') || '';
      var done = function () {
        var original = btn.textContent;
        btn.textContent = '已复制';
        setTimeout(function () { btn.textContent = original; }, 1400);
        toast('模板内容已复制', 'ok');
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(content).then(done, function () { toast('复制失败', 'error'); });
      } else {
        toast('当前浏览器不支持一键复制', 'error');
      }
    });
  }

  // 「全部模板」区块折叠：记住展开/收起状态（默认展开）
  function initListFold() {
    var fold = document.getElementById('template-list-fold');
    if (!fold) { return; }
    try {
      if (window.localStorage.getItem('inknote.tpl-list-open') === '0') { fold.open = false; }
    } catch (error) { /* 拿不到 localStorage 就用默认展开 */ }
    fold.addEventListener('toggle', function () {
      try {
        window.localStorage.setItem('inknote.tpl-list-open', fold.open ? '1' : '0');
      } catch (error) { /* 忽略 */ }
    });
  }

  /* ---------- 模板拖拽排序 ---------- */
  function initDragSort() {
    var grid = document.querySelector('.template-grid');
    if (!grid) { return; }
    var cards = Array.prototype.slice.call(grid.querySelectorAll('.template-card'));
    if (cards.length < 2) { return; }   // 一张及以下没有排序意义

    var dragging = null;
    var handleArmed = false;            // 只有按住手柄才允许拖（避免误拖卡片里的按钮）
    var dragStartOrder = [];            // 拖拽前的顺序，用于失败回滚

    function idsInDom() {
      return Array.prototype.slice.call(grid.querySelectorAll('.template-card'))
        .map(function (el) { return parseInt(el.getAttribute('data-template-id'), 10); });
    }

    // FLIP：先记录旧位置，执行 DOM 变更，再对其它卡片用 translateY 平滑归位。
    function flip(mutate) {
      var before = {};
      Array.prototype.slice.call(grid.querySelectorAll('.template-card')).forEach(function (el) {
        before[el.getAttribute('data-template-id')] = el.getBoundingClientRect().top;
      });
      mutate();
      Array.prototype.slice.call(grid.querySelectorAll('.template-card')).forEach(function (el) {
        if (el === dragging) { return; }      // 正在拖的那张交给浏览器拖影，不参与归位
        var tid = el.getAttribute('data-template-id');
        var dy = before[tid] - el.getBoundingClientRect().top;
        if (dy) {
          el.style.transition = 'none';
          el.style.transform = 'translateY(' + dy + 'px)';
          requestAnimationFrame(function () {
            el.style.transition = 'transform 0.18s ease';
            el.style.transform = '';
          });
        }
      });
    }

    // 把 DOM 按给定 id 顺序重排（带 FLIP 动画）。
    function applyOrder(targetIds) {
      var map = {};
      grid.querySelectorAll('.template-card').forEach(function (el) {
        map[el.getAttribute('data-template-id')] = el;
      });
      flip(function () {
        targetIds.forEach(function (tid) {
          var el = map[String(tid)];
          if (el) { grid.appendChild(el); }
        });
      });
    }

    // 返回指针上方、应插到其前面的卡片。
    function dragAfter(y) {
      var els = Array.prototype.slice.call(grid.querySelectorAll('.template-card:not(.is-dragging)'));
      var best = { offset: -Infinity, el: null };
      els.forEach(function (el) {
        var box = el.getBoundingClientRect();
        var offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > best.offset) { best = { offset: offset, el: el }; }
      });
      return best.el;
    }

    // 松手后提交新顺序；失败则把 DOM 还原成 rollback（不让 UI 与服务端不一致）。
    function commit(order, rollback) {
      fetch('/templates/reorder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
        body: JSON.stringify({ ids: order }),
      }).then(function (res) {
        return res.json().then(function (data) { return { ok: res.ok, data: data }; });
      }).then(function (result) {
        if (!result.ok || result.data.error) {
          toast(result.data.error || '排序保存失败，已还原顺序', 'error');
          if (rollback) { applyOrder(rollback); }
        }
      }).catch(function () {
        toast('网络错误，排序未保存，已还原顺序', 'error');
        if (rollback) { applyOrder(rollback); }
      });
    }

    cards.forEach(function (card) {
      var handle = card.querySelector('.tpl-drag');
      if (!handle) { return; }

      // 鼠标 / 触控按下手柄才「上膛」；松手或拖完解除。
      handle.addEventListener('mousedown', function () { handleArmed = true; });
      handle.addEventListener('mouseup', function () { handleArmed = false; });
      handle.addEventListener('touchstart', function () { handleArmed = true; }, { passive: true });
      handle.addEventListener('touchend', function () { handleArmed = false; });

      card.addEventListener('dragstart', function (e) {
        if (!handleArmed) { e.preventDefault(); return; }   // 不是从手柄发起 → 取消拖拽
        dragging = card;
        card.classList.add('is-dragging');
        dragStartOrder = idsInDom();
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = 'move';
          try { e.dataTransfer.setData('text/plain', card.getAttribute('data-template-id') || ''); } catch (err) { /* 部分浏览器禁写 */ }
        }
      });

      card.addEventListener('dragend', function () {
        card.classList.remove('is-dragging');
        handleArmed = false;
        if (!dragging) { return; }
        var order = idsInDom();
        var rollback = dragStartOrder.slice();
        dragging = null;
        commit(order, rollback);
      });

      // 键盘可达：手柄 Tab 聚焦后 Alt+↑ / Alt+↓ 移动一张，同样提交。
      handle.addEventListener('keydown', function (e) {
        if (!e.altKey) { return; }
        if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') { return; }
        e.preventDefault();
        var current = idsInDom();
        var tid = parseInt(card.getAttribute('data-template-id'), 10);
        var idx = current.indexOf(tid);
        var swap = idx + (e.key === 'ArrowUp' ? -1 : 1);
        if (swap < 0 || swap >= current.length) { return; }
        var next = current.slice();
        next[idx] = current[swap];
        next[swap] = current[idx];
        applyOrder(next);
        commit(next, current);
      });
    });

    // 拖动中其余卡片让位：随指针实时把被拖卡片插到合适位置。
    grid.addEventListener('dragover', function (e) {
      if (!dragging) { return; }
      e.preventDefault();
      if (e.dataTransfer) { e.dataTransfer.dropEffect = 'move'; }
      var after = dragAfter(e.clientY);
      if (after == null) {
        if (grid.lastElementChild !== dragging) { flip(function () { grid.appendChild(dragging); }); }
      } else if (after !== dragging && after.previousElementSibling !== dragging) {
        flip(function () { grid.insertBefore(dragging, after); });
      }
    });

    grid.addEventListener('drop', function (e) {
      if (dragging) { e.preventDefault(); }   // 阻止浏览器把 dataTransfer 当导航
    });
  }

  ready(function () {
    initAiGenerate();
    initCopy();
    initListFold();
    initDragSort();
  });
})();
