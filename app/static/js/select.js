/*!
 * 墨痕 InkNote · select.js
 * 把所有原生 <select class="select"> 的「展开弹层」换成站内自绘面板
 * （原生弹层是浏览器白底系统列表，选项一多就跟站点配色不搭）。
 *
 * 只换视觉、不动布局：原生 select 仍留在文档流里当尺寸基准 ——
 * 它的宽度由自己的选项文字和既有样式决定（长分类名会把下拉撑到 348px 这种），
 * 所以只要它还在，自绘后每个断点的几何就完全不变。它只是变成
 * tabindex=-1 + aria-hidden 且不可点，自绘按钮绝对定位严丝合缝地盖在上面。
 * 选中后给原生 select 派发 change：「筛选栏改了就自动提交」「批量栏按操作显隐输入框」
 * 这些既有逻辑一行都不用改；required 校验也照旧由原生 select 承担。
 *
 * 面板是原生选项的**完整镜像**（含 value="" 的重置项，见下方注释）——自绘不该
 * 让用户丢失任何原生能做的操作。
 *
 * 渐进增强：整体包在 try/catch 里，出错就保持原生下拉。想留住原生弹层的加 data-native-select。
 */

(function () {
  'use strict';

  var SEQ = 0;

  // 从周边标记里取一个人类可读的名字，给按钮当 aria-label
  function labelOf(select) {
    var field = select.closest ? select.closest('label') : null;
    if (field) {
      var node = field.querySelector('.field__label') || field.querySelector('.sr-only');
      if (node && node.textContent.trim()) return node.textContent.trim();
    }
    var aria = select.getAttribute('aria-label');
    if (aria) return aria;
    var first = select.options[0];
    return (first && (first.textContent || '').trim()) || '选项';
  }

  function enhance(select) {
    if (!select || select.multiple || select.disabled) return;
    if (select.hasAttribute('data-native-select')) return;
    if (select.closest && select.closest('.select-combo')) return; // 已经改过了
    var options = Array.prototype.slice.call(select.options || []);
    if (options.length < 2) return; // 没什么可选的，别折腾

    SEQ += 1;
    var panelId = 'select-combo-panel-' + SEQ;

    var wrapper = document.createElement('span');
    wrapper.className = 'select-combo';

    var trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'select select-combo__trigger';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', panelId);
    trigger.setAttribute('aria-label', labelOf(select));
    var labelNode = document.createElement('span');
    labelNode.className = 'select-combo__label';
    trigger.appendChild(labelNode);
    var chevron = document.createElement('span');
    chevron.className = 'select-combo__chevron';
    chevron.setAttribute('aria-hidden', 'true');
    chevron.textContent = '▾';
    trigger.appendChild(chevron);

    var panel = document.createElement('div');
    panel.className = 'combo__panel select-combo__panel';
    panel.id = panelId;
    panel.setAttribute('role', 'listbox');
    panel.hidden = true;

    var items = [];
    var placeholderText = '请选择…';
    options.forEach(function (option, index) {
      var text = (option.textContent || '').trim();
      if (index === 0) placeholderText = text || placeholderText;
      // 空值项也要进列表：它们多半是**重置项**（「全部标签」「不改」「按相关度」），
      // 早先这里 `if (!option.value) return` 把它们丢了 —— 于是选完具体值就再也
      // 回不到「全部」，筛选栏形同单向阀（列表/搜索/标签/图谱/备份页全中）。
      // 列表里那一项照常可点，选中后 select.value 回到 ""，既有提交/联动逻辑不变。
      var item = document.createElement('div');
      item.className = 'combo__item';
      item.id = panelId + '-item-' + index;
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', 'false');
      item.setAttribute('data-value', option.value);
      item.textContent = text;
      item.addEventListener('click', function () { choose(option.value); });
      panel.appendChild(item);
      items.push(item);
    });
    if (!items.length) return;

    // 包起来：原生 select 留在最前面当尺寸基准，按钮盖在它上面（面板再叠一层）
    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(select);
    wrapper.appendChild(trigger);
    wrapper.appendChild(panel);
    select.classList.add('select-combo__native');
    select.setAttribute('tabindex', '-1');
    select.setAttribute('aria-hidden', 'true');

    var active = -1;

    function syncLabel() {
      var current = select.options[select.selectedIndex];
      var hasValue = !!(current && current.value);
      labelNode.textContent = hasValue ? (current.textContent || '').trim() : placeholderText;
      labelNode.classList.toggle('is-placeholder', !hasValue);
      items.forEach(function (item) {
        var on = item.getAttribute('data-value') === select.value;
        item.classList.toggle('is-selected', on);
        item.setAttribute('aria-selected', on ? 'true' : 'false');
      });
    }

    function indexOfValue(value) {
      for (var i = 0; i < items.length; i++) {
        if (items[i].getAttribute('data-value') === value) return i;
      }
      return -1;
    }

    function setActive(index) {
      active = (index + items.length) % items.length;
      items.forEach(function (item, i) { item.classList.toggle('is-active', i === active); });
      trigger.setAttribute('aria-activedescendant', items[active].id);
      if (items[active].scrollIntoView) items[active].scrollIntoView({ block: 'nearest' });
    }

    function isOpen() { return !panel.hidden; }

    function open() {
      if (isOpen()) return;
      panel.hidden = false;
      trigger.setAttribute('aria-expanded', 'true');
      var current = indexOfValue(select.value);
      setActive(current >= 0 ? current : 0);
    }

    function close() {
      if (!isOpen()) return;
      panel.hidden = true;
      trigger.setAttribute('aria-expanded', 'false');
      trigger.removeAttribute('aria-activedescendant');
      active = -1;
      items.forEach(function (item) { item.classList.remove('is-active'); });
    }

    function choose(value) {
      select.value = value;
      // 让「自动提交」「按操作显隐输入框」这些既有逻辑原样生效
      select.dispatchEvent(new Event('change', { bubbles: true }));
      syncLabel();
      close();
      trigger.focus();
    }

    trigger.addEventListener('click', function (event) {
      event.preventDefault(); // 别让 label 把点击转给原生 select
      if (isOpen()) close(); else open();
    });

    trigger.addEventListener('keydown', function (event) {
      var key = event.key;
      if (key === 'ArrowDown' || key === 'ArrowUp') {
        event.preventDefault();
        if (!isOpen()) { open(); return; }
        setActive(active + (key === 'ArrowDown' ? 1 : -1));
        return;
      }
      if (key === 'Home' || key === 'End') {
        if (!isOpen()) return;
        event.preventDefault();
        setActive(key === 'Home' ? 0 : items.length - 1);
        return;
      }
      if (key === 'Enter' || key === ' ') {
        if (!isOpen()) return; // 关着时交给原生 click 去打开
        event.preventDefault();
        if (active >= 0) choose(items[active].getAttribute('data-value'));
        return;
      }
      if (key === 'Escape') {
        if (!isOpen()) return;
        event.preventDefault();
        close();
        return;
      }
      if (key === 'Tab') close();
    });

    document.addEventListener('click', function (event) {
      if (isOpen() && !wrapper.contains(event.target)) close();
    });

    syncLabel();
  }

  function init() {
    var nodes = document.querySelectorAll('select.select');
    Array.prototype.forEach.call(nodes, function (select) {
      try {
        enhance(select);
      } catch (error) {
        /* 自绘失败就退回原生下拉，不影响表单本身 */
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
