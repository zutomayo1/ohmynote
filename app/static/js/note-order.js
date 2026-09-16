/* 置顶笔记拖拽排序（笔记列表页）。
 *
 * 只重排**置顶卡片之间**的顺序：非置顶笔记按时间/字数排，把它们混进来拖没有意义，
 * 也会让「屏幕上的位置」和「服务端顺序」对不上。写回的是置顶区的 sort_order。
 *
 * 落点判定用「离指针最近的置顶卡片」（网格布局里比按行判定稳），并在左/右半区
 * 决定插到目标之前还是之后；DOM 变动后用 FLIP 让其余卡片滑到新位置。
 */
(function () {
  'use strict';

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  function csrfToken() {
    try {
      if (window.InkNote && typeof window.InkNote.csrf === 'function') {
        var value = window.InkNote.csrf();
        if (value) { return value; }
      }
    } catch (error) { /* 忽略，走 meta 兜底 */ }
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
  }

  function toast(message, kind) {
    if (window.InkNote && typeof window.InkNote.toast === 'function') {
      window.InkNote.toast(message, kind);
    }
  }

  function init() {
    var grid = document.querySelector('.note-grid');
    if (!grid) { return; }

    var cardOf = function (id) {
      return grid.querySelector('.note-card[data-note-id="' + id + '"]');
    };
    var pinned = function () {
      return Array.prototype.slice.call(grid.querySelectorAll('.note-card.is-pinned'));
    };
    if (pinned().length < 2) { return; }   // 少于两篇置顶：没有顺序可调

    var reduceMotion = !!(window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    var dragging = null;
    var armed = false;
    var startOrder = null;

    function firstUnpinned() {
      return grid.querySelector('.note-card:not(.is-pinned)');
    }

    function idOf(card) {
      return parseInt(card.getAttribute('data-note-id'), 10);
    }

    function idsInDom() {
      return pinned().map(idOf);
    }

    function rectMap() {
      var map = {};
      pinned().forEach(function (card) { map[idOf(card)] = card.getBoundingClientRect(); });
      return map;
    }

    // FLIP：先记旧位置，动完 DOM 再用 transform 从旧位置滑回来（动画结束即清空，不留痕迹）
    function flip(mutate) {
      if (reduceMotion) { mutate(); return; }
      var before = rectMap();
      mutate();
      var after = rectMap();
      pinned().forEach(function (card) {
        var key = idOf(card);
        var from = before[key];
        var to = after[key];
        if (!from || !to) { return; }
        var dx = from.left - to.left;
        var dy = from.top - to.top;
        if (!dx && !dy) { return; }
        card.style.transition = 'none';
        card.style.transform = 'translate(' + dx + 'px, ' + dy + 'px)';
        requestAnimationFrame(function () {
          card.style.transition = 'transform 0.18s ease';
          card.style.transform = '';
        });
      });
    }

    // 把置顶卡片按给定顺序放回置顶区（置顶区 = 第一个非置顶卡片之前）
    function applyOrder(order) {
      var cards = {};
      pinned().forEach(function (card) { cards[idOf(card)] = card; });
      flip(function () {
        var anchor = firstUnpinned();
        order.forEach(function (id) {
          var card = cards[id];
          if (!card) { return; }
          if (anchor) { grid.insertBefore(card, anchor); } else { grid.appendChild(card); }
        });
      });
    }

    function nearest(x, y) {
      var best = null;
      var bestDist = Infinity;
      pinned().forEach(function (card) {
        if (card === dragging) { return; }
        var rect = card.getBoundingClientRect();
        var dx = x - (rect.left + rect.width / 2);
        var dy = y - (rect.top + rect.height / 2);
        var dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < bestDist) { bestDist = dist; best = card; }
      });
      return best;
    }

    function commit(order, rollback) {
      fetch('/notes/reorder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
        body: JSON.stringify({ ids: order })
      }).then(function (resp) {
        if (!resp.ok) { throw new Error('HTTP ' + resp.status); }
        return resp.json();
      }).then(function (data) {
        if (!data || !data.ok) { throw new Error((data && data.error) || '保存失败'); }
        toast('置顶顺序已保存');
      }).catch(function () {
        if (rollback && rollback.length === order.length) { applyOrder(rollback); }
        toast('顺序没保存成功，已还原', 'error');
      });
    }

    pinned().forEach(function (card) {
      var handle = card.querySelector('.note-drag');
      if (!handle) { return; }

      // 只有按住手柄才能拖：卡片里还有勾选框、链接和按钮，误拖会很烦
      handle.addEventListener('mousedown', function () { armed = true; });
      handle.addEventListener('mouseup', function () { armed = false; });
      handle.addEventListener('touchstart', function () { armed = true; }, { passive: true });
      handle.addEventListener('touchend', function () { armed = false; });

      card.addEventListener('dragstart', function (event) {
        if (!armed) { event.preventDefault(); return; }
        dragging = card;
        startOrder = idsInDom();
        card.classList.add('is-dragging');
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = 'move';
          try {
            event.dataTransfer.setData('text/plain', card.getAttribute('data-note-id') || '');
          } catch (error) { /* 个别浏览器禁写 */ }
        }
      });

      card.addEventListener('dragend', function () {
        card.classList.remove('is-dragging');
        armed = false;
        if (!dragging) { return; }
        var order = idsInDom();
        var rollback = startOrder ? startOrder.slice() : null;
        dragging = null;
        startOrder = null;
        commit(order, rollback);
      });

      // 键盘：手柄聚焦后 Alt+↑ / Alt+↓ 换一位（触屏也能用）
      handle.addEventListener('keydown', function (event) {
        if (!event.altKey) { return; }
        if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') { return; }
        event.preventDefault();
        var list = pinned();
        var index = list.indexOf(card);
        var swap = index + (event.key === 'ArrowUp' ? -1 : 1);
        if (index < 0 || swap < 0 || swap >= list.length) { return; }
        var current = idsInDom();
        var next = current.slice();
        next[index] = current[swap];
        next[swap] = current[index];
        applyOrder(next);
        commit(next, current);
      });
    });

    grid.addEventListener('dragover', function (event) {
      if (!dragging) { return; }
      event.preventDefault();
      if (event.dataTransfer) { event.dataTransfer.dropEffect = 'move'; }
      var target = nearest(event.clientX, event.clientY);
      if (!target || target === dragging) { return; }
      var rect = target.getBoundingClientRect();
      var after = event.clientX > rect.left + rect.width / 2;
      // 「之后」且目标已在置顶区末尾时，锚点落到第一个非置顶卡片（即置顶区尾部）
      var anchor = after ? (target.nextElementSibling || firstUnpinned()) : target;
      if (anchor === dragging) { return; }
      if (anchor === dragging.nextElementSibling) { return; }   // 已在位，别抖
      flip(function () { grid.insertBefore(dragging, anchor); });
    });

    grid.addEventListener('drop', function (event) {
      if (dragging) { event.preventDefault(); }   // 别让浏览器把 dataTransfer 当导航
    });
  }

  ready(init);
})();
