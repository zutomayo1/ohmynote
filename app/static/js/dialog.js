/* 墨痕 InkNote · dialog.js
 * 自绘确认对话框：替掉原生 window.confirm 那个灰白弹窗。
 *
 * 为什么用原生 <dialog> + showModal()：焦点陷阱、Esc 关闭、遮罩层、
 * aria-modal 语义都由浏览器保证 —— 自绘弹窗最容易翻车的就是这几项
 * （焦点乱跑、Esc 不关、Tab 跑出去），能交给浏览器就不自己写。
 * 这里只接管「长什么样、按钮写什么字、点了之后怎么收尾」。
 *
 * 对外只有一个入口：
 *   InkNote.dialog.confirm({title, message, okText, cancelText, danger}) -> Promise<boolean>
 * 任何一步失败（浏览器太老、<dialog> 不可用）都退回 window.confirm，
 * 行为不变、绝不把危险操作变成「没有确认」。
 */
(function () {
  'use strict';

  var InkNote = window.InkNote = window.InkNote || {};
  var node = null, titleEl = null, msgEl = null, okBtn = null, cancelBtn = null;
  var resolvePending = null, opener = null;

  function supported() {
    return typeof HTMLDialogElement !== 'undefined' &&
      typeof document.createElement('dialog').showModal === 'function';
  }

  function build() {
    if (node) { return node; }
    node = document.createElement('dialog');
    node.className = 'dialog';
    node.id = 'inknote-dialog';
    node.innerHTML =
      '<form method="dialog" class="dialog__form">' +
        '<h2 class="dialog__title" id="inknote-dialog-title"></h2>' +
        '<p class="dialog__message" data-dialog-message></p>' +
        '<div class="dialog__actions">' +
          '<button type="submit" value="cancel" class="btn btn--ghost" data-dialog-cancel>取消</button>' +
          '<button type="submit" value="ok" class="btn btn--primary" data-dialog-ok>确定</button>' +
        '</div>' +
      '</form>';
    document.body.appendChild(node);

    titleEl = node.querySelector('.dialog__title');
    msgEl = node.querySelector('[data-dialog-message]');
    okBtn = node.querySelector('[data-dialog-ok]');
    cancelBtn = node.querySelector('[data-dialog-cancel]');

    // 关掉之后的统一收尾：按钮提交 / Esc / 点遮罩都走这里
    node.addEventListener('close', function () {
      var done = resolvePending;
      resolvePending = null;
      var ok = node.returnValue === 'ok';       // Esc 与遮罩会留下空值 → 视为取消
      if (opener && typeof opener.focus === 'function') {
        try { opener.focus(); } catch (e) { /* 原元素可能已经不在文档里 */ }
      }
      opener = null;
      if (done) { done(ok); }
    });

    // 点遮罩 = 取消（遮罩就是 dialog 自己，弹层内部有 form 元素兜着）
    node.addEventListener('click', function (event) {
      if (event.target === node) { node.close('cancel'); }
    });
    return node;
  }

  function textOf(source, name, fallback) {
    if (!source || typeof source.getAttribute !== 'function') { return fallback; }
    var value = source.getAttribute(name);
    return value ? value : fallback;
  }

  function confirmBox(options) {
    var opts = options || {};
    var message = opts.message || '';

    if (!supported()) {                        // 老浏览器：退回原生，绝不静默跳过确认
      return Promise.resolve(window.confirm(message));
    }
    build();
    if (node.open) { node.close('cancel'); }   // 极端情况：上一次还开着

    var danger = !!opts.danger;
    titleEl.textContent = opts.title || '确认操作';
    msgEl.textContent = message;
    okBtn.textContent = opts.okText || '确定';
    cancelBtn.textContent = opts.cancelText || '取消';
    okBtn.className = 'btn ' + (danger ? 'btn--danger' : 'btn--primary');

    opener = document.activeElement;
    return new Promise(function (resolve) {
      resolvePending = resolve;
      node.returnValue = '';
      node.showModal();
      // 危险操作默认停在「取消」——回车手滑不该把东西删了；普通确认停在「确定」更顺手
      (danger ? cancelBtn : okBtn).focus();
    });
  }

  InkNote.dialog = {
    confirm: confirmBox,
    textOf: textOf,          // app.js 读 data-confirm-* 时复用同一套取值规则
    isOpen: function () { return !!(node && node.open); }
  };
})();
