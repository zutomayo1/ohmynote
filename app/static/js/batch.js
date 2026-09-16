/*!
 * 墨痕 InkNote · batch.js
 * /notes 列表页「批量操作」的渐进增强（无 JS 时表单仍可原生提交）。
 * 做六件事：实时计数、全选/取消全选、选中「全部筛选结果」、选中后高亮操作条、
 * 按操作显示标签框、用 localStorage 跨页/跨筛选记住勾选（点「清空选择」或提交成功后清空）。
 * 「批量操作」下拉的自绘面板由 select.js 统一负责（它把值写回原生 select 并派发 change）。筛选参数由服务端渲染成隐藏域，勾选后原生提交即可，不依赖本脚本；关掉 JS 就退化回「只勾当前页」的原生表单行为。
 * 风格对齐 app.js：普通脚本、IIFE、'use strict'。
 */
(function () {
  'use strict';

  // DOM 就绪后初始化一次（defer 脚本通常已就绪，这里兜底）
  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn, { once: true });
    else fn();
  }

  // ---- localStorage：记住选中的 id，翻页 / 换筛选都不丢 ----
  // key 带「站点 + 路径」区分不同页面，且不含 query，所以换筛选 URL 仍命中同一个 key。
  var STORAGE_PREFIX = 'inknote.batch.v1:';

  // 和 app/services/ai.py 的 BACKFILL_LIMIT 保持一致（只用于确认文案）
  var BACKFILL_HINT = 5;

  function storageKey() {
    try {
      var loc = window.location || {};
      return STORAGE_PREFIX + (loc.origin || '') + (loc.pathname || '/notes');
    } catch (error) {
      return STORAGE_PREFIX + '/notes';
    }
  }

  // 提交成功后写一个标记；下次进页面读到就清空，避免下次打开还勾着一堆
  function submittedKey() {
    return storageKey() + ':submitted';
  }

  // 读取容错：解析失败 / 隐私模式 / 数据不是数组，一律当空，绝不抛异常
  function readSelected() {
    try {
      var raw = window.localStorage.getItem(storageKey());
      if (!raw) return [];
      var data = JSON.parse(raw);
      if (!Array.isArray(data)) return [];
      return data.filter(function (id) { return typeof id === 'string' && /^\d+$/.test(id); });
    } catch (error) {
      return [];
    }
  }

  function writeSelected(ids) {
    try {
      if (!ids || !ids.length) window.localStorage.removeItem(storageKey());
      else window.localStorage.setItem(storageKey(), JSON.stringify(ids));
    } catch (error) { /* 写不进去就退化成不记忆，别影响页面 */ }
  }

  function consumeSubmittedFlag() {
    try {
      if (window.localStorage.getItem(submittedKey())) {
        window.localStorage.removeItem(submittedKey());
        return true;
      }
    } catch (error) { /* 忽略 */ }
    return false;
  }

  function markSubmitted() {
    try { window.localStorage.setItem(submittedKey(), String(Date.now())); } catch (error) { /* 忽略 */ }
  }

  function initBatch() {
    var form = document.getElementById('batch-form');
    if (!form) return; // 其它页面没有批量表单，直接跳过

    var selectAllBtn = form.querySelector('[data-batch-all]');
    var countLabel = form.querySelector('[data-batch-count]');
    var clearBtn = form.querySelector('[data-batch-clear]');
    var actionSelect = form.querySelector('[data-batch-action]');
    var tagField = form.querySelector('[data-batch-tag]');
    var categoryField = form.querySelector('[data-batch-category]');
    // 「选中当前筛选出的全部 N 篇」：复选框 name="all"，N 由服务端渲染在 data 属性上。
    var selectAllFiltered = form.querySelector('[data-batch-select-all]');
    var confirmAllLabel = form.querySelector('[data-batch-confirm-all]');
    var confirmAllBox = confirmAllLabel ? confirmAllLabel.querySelector('input[name="confirm_all"]') : null;
    var filteredTotal = parseInt(form.getAttribute('data-batch-total') || '0', 10) || 0;

    // 提交成功后页面会重新加载：这次进页面就把上次的勾选全清掉。
    var submitted = consumeSubmittedFlag();
    var selected = submitted ? [] : readSelected();
    if (submitted) writeSelected([]);

    // 进入页面：把存储里、当前页存在的那部分复选框勾上；其它页的 id 留在存储里。
    (function restore() {
      if (!selected.length) return;
      var wanted = {};
      selected.forEach(function (id) { wanted[id] = true; });
      boxes().forEach(function (box) {
        if (wanted[box.value]) box.checked = true;
      });
    })();

    // 当前页勾选变化后，用 DOM 覆盖存储里的「当前页部分」，其它页的 id 原样保留。
    function syncFromDom() {
      var onPage = {};
      boxes().forEach(function (box) { onPage[box.value] = true; });
      var next = selected.filter(function (id) { return !onPage[id]; });
      boxes().forEach(function (box) {
        if (box.checked && next.indexOf(box.value) === -1) next.push(box.value);
      });
      selected = next;
      writeSelected(selected);
    }

    // 「（含其它页 M 项）」：只有选中总数超过当前页可见数时才补这一句。
    function extraSuffix(all) {
      if (selected.length <= all.length) return '';
      var pageIds = {};
      all.forEach(function (box) { pageIds[box.value] = true; });
      var other = selected.filter(function (id) { return !pageIds[id]; }).length;
      return other > 0 ? '（含其它页 ' + other + ' 项）' : '';
    }

    // 复选框写在卡片宏里、不在 form 内部，靠 form="batch-form" 关联，
    // 所以按属性选择，而不是 form.querySelectorAll。
    function boxes() {
      return Array.prototype.slice.call(
        document.querySelectorAll('input.note-select[name="note_ids"][form="batch-form"]')
      );
    }

    function checkedBoxes() {
      return boxes().filter(function (box) { return box.checked; });
    }

    // 「全部筛选结果」是否已勾选（它不在 boxes() 里，name 是 all）
    function allFilteredOn() {
      return !!(selectAllFiltered && selectAllFiltered.checked);
    }

    // 是否需要标签框：只有加 / 删标签两个操作需要
    function needsTag() {
      var value = actionSelect ? actionSelect.value : '';
      return value === 'add_tag' || value === 'remove_tag';
    }

    function needsCategory() {
      var value = actionSelect ? actionSelect.value : '';
      return value === 'set_category';
    }

    // 刷新：计数、全选按钮文案、操作条高亮、卡片高亮、标签框显隐
    function refresh() {
      var all = boxes();
      var chosen = checkedBoxes();
      var allFiltered = allFilteredOn();

      if (countLabel) {
        countLabel.textContent = allFiltered
          ? '已选全部 ' + filteredTotal + ' 篇'
          : '已选 ' + selected.length + ' 项' + extraSuffix(all);
      }

      // 有选中才让操作条显眼（未选中时保持低调；只存在其它页的 id 也算有选）
      form.classList.toggle('is-active', allFiltered || selected.length > 0);

      // 「清空选择」只在真有选择（含其它页 / 全部筛选结果）时露出来
      if (clearBtn) clearBtn.classList.toggle('is-hidden', !allFiltered && selected.length === 0);

      if (selectAllBtn) {
        selectAllBtn.disabled = allFiltered;
        var allChecked = all.length > 0 && chosen.length === all.length;
        if (!allFiltered) selectAllBtn.textContent = allChecked ? '取消全选' : '全选';
        if (!allFiltered) selectAllBtn.setAttribute('aria-pressed', allChecked ? 'true' : 'false');
      }

      // 给选中的卡片加一层淡色高亮（纯视觉，不影响提交）
      all.forEach(function (box) {
        var card = box.closest ? box.closest('article.note-card') : null;
        box.disabled = allFiltered;
        if (card) card.classList.toggle('is-selected', allFiltered || box.checked);
      });

      // 没选到加/删标签时把标签框收起（用 style.css 已有的 .is-hidden）
      // 没有筛选条件时，光勾「全部筛选结果」还不够，要再勾确认框；有 JS 时默认
      // 收起确认框，勾上主复选框再露出来（无 JS 则一直可见，原生可用）。
      if (confirmAllLabel) {
        confirmAllLabel.classList.toggle('is-hidden', !allFiltered);
        if (!allFiltered && confirmAllBox) confirmAllBox.checked = false;
      }

      // 「补摘要」是唯一会调模型的动作（慢、要花 token），执行前先说清楚要发生什么
      if (form) {
        var picked = actionSelect ? actionSelect.value : '';
        if (picked === 'backfill_summary') {
          form.setAttribute('data-confirm',
            '给选中的笔记生成摘要？只处理还没有摘要的，每篇一次模型调用，单次最多 '
            + BACKFILL_HINT + ' 篇，可能要等一会儿。');
        } else {
          form.removeAttribute('data-confirm');
        }
      }

      if (tagField) tagField.classList.toggle('is-hidden', !needsTag());
      if (categoryField) categoryField.classList.toggle('is-hidden', !needsCategory());
    }

    // 全选 / 取消全选：只要还有没选中的就全选，否则全部取消
    if (selectAllBtn) {
      selectAllBtn.addEventListener('click', function () {
        var all = boxes();
        var target = all.some(function (box) { return !box.checked; });
        all.forEach(function (box) { box.checked = target; });
        syncFromDom(); // 全选 / 取消也要落盘
        refresh();
      });
    }

    // 「清空选择」按钮：只清本地记忆和当前页勾选，不动表单结构（无 JS 时它保持隐藏）
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        selected = [];
        writeSelected([]);
        boxes().forEach(function (box) { box.checked = false; });
        if (selectAllFiltered) selectAllFiltered.checked = false;
        if (confirmAllBox) confirmAllBox.checked = false;
        refresh();
      });
    }

    // 提交前：把「当前页之外」的已选 id 注入 hidden input（当前页复选框会原生提交）。
    // 勾了「全部筛选结果（all=1）」就绝不带 id —— 那是另一条路径，服务端按筛选条件取全部。
    form.addEventListener('submit', function () {
      Array.prototype.slice.call(form.querySelectorAll('input[data-batch-extra-id]')).forEach(function (node) {
        if (node.parentNode) node.parentNode.removeChild(node);
      });
      if (!allFilteredOn()) {
        var pageIds = {};
        boxes().forEach(function (box) { pageIds[box.value] = true; });
        selected.forEach(function (id) {
          if (pageIds[id]) return; // 当前页的由复选框自己提交，不重复注入
          var input = document.createElement('input');
          input.type = 'hidden';
          input.name = 'note_ids';
          input.value = id;
          input.setAttribute('data-batch-extra-id', '1');
          form.appendChild(input);
        });
      }
      // 标记「刚提交过」：页面重新加载后 batch.js 会清空这次的选择
      markSubmitted();
    });

    // 复选框与操作下拉的 change 事件统一处理（委托到 document）
    document.addEventListener('change', function (event) {
      var node = event.target;
      if (!node || !node.classList) return;
      if (node.classList.contains('note-select')) {
        syncFromDom(); // 勾选 / 取消：立刻更新 localStorage（翻页保留）
        refresh();
        return;
      }
      if (node === actionSelect || node === selectAllFiltered || node === confirmAllBox) refresh();
    });

    // 初始：标签框默认收起；无 JS 时它会一直显示，保证原生也能填标签
    refresh();
  }


  /* ============ 操作流：把多个动作排队，一次提交按序执行 ============ */
  var FLOW_NAMES = {
    add_tag: '添加标签', remove_tag: '移除标签', publish: '设为公开', unpublish: '取消公开',
    pin: '置顶', unpin: '取消置顶', star: '加星标', unstar: '取消星标',
    trash: '移入回收站', archive: '归档', unarchive: '取消归档',
    set_category: '设为分类', backfill_summary: '补摘要',
  };

  function setupFlow() {
    var form = document.getElementById('batch-form');
    if (!form) { return; }
    var actionSelect = form.querySelector('[data-batch-action]');
    var tagField = form.querySelector('[data-batch-tag]');
    var addBtn = form.querySelector('[data-batch-flow-add]');
    var runBtn = form.querySelector('[data-batch-flow-run]');
    var wrap = form.querySelector('[data-batch-flow]');
    var flowInput = form.querySelector('[data-batch-flow-input]');
    if (!addBtn || !runBtn || !wrap || !flowInput) { return; }

    var queue = [];

    function describe(step) {
      var name = FLOW_NAMES[step.action] || step.action;
      if (step.action === 'add_tag' || step.action === 'remove_tag') { return name + '「' + step.tag + '」'; }
      if (step.action === 'set_category') { return name + '「' + step.category + '」'; }
      return name;
    }

    function render() {
      wrap.innerHTML = '';
      if (!queue.length) {
        wrap.classList.add('is-hidden');
        runBtn.classList.add('is-hidden');
        return;
      }
      wrap.classList.remove('is-hidden');
      runBtn.classList.remove('is-hidden');
      var label = document.createElement('span');
      label.className = 'batch-flow__label';
      label.textContent = '操作流（按序执行）：';
      wrap.appendChild(label);
      queue.forEach(function (step, index) {
        var chip = document.createElement('span');
        chip.className = 'batch-flow__chip';
        var stepNo = document.createElement('span');
        stepNo.className = 'batch-flow__chip-step';
        stepNo.textContent = String(index + 1);
        chip.appendChild(stepNo);
        chip.appendChild(document.createTextNode(describe(step)));
        var remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'batch-flow__chip-remove';
        remove.textContent = '×';
        remove.setAttribute('aria-label', '移除第 ' + (index + 1) + ' 步');
        remove.addEventListener('click', function () {
          queue.splice(index, 1);
          render();
        });
        chip.appendChild(remove);
        wrap.appendChild(chip);
      });
    }

    addBtn.classList.remove('is-hidden');
    addBtn.addEventListener('click', function () {
      var action = actionSelect.value;
      if (!action || queue.length >= 5) { return; }
      var step = { action: action };
      if (action === 'add_tag' || action === 'remove_tag') {
        step.tag = tagField ? (tagField.querySelector('input') || {}).value || '' : '';
        if (!step.tag.trim()) { return; }
      }
      if (action === 'set_category') {
        var catField = form.querySelector('[data-batch-category-input]');
        step.category = catField ? catField.value || '' : '';
        if (!step.category.trim()) { return; }
      }
      queue.push(step);
      render();
    });

    runBtn.addEventListener('click', function () {
      if (!queue.length) { return; }
      flowInput.value = JSON.stringify(queue);
      if (actionSelect) { actionSelect.disabled = true; }   // flow 优先，避免空 action 干扰
      form.submit();
    });
  }

  ready(initBatch);
  ready(setupFlow);
})();
