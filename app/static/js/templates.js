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

  ready(function () {
    initAiGenerate();
    initCopy();
    initListFold();
  });
})();
