/*!
 * settings.js —— 墨痕 InkNote「设置」页脚本
 * 普通脚本（非 module），IIFE + 'use strict'，无依赖；
 * 只做增强：一键预设、拉取模型、分步测试连接、向量索引进度。
 * JS 整个失效时，页面里的表单仍能正常提交（测试连接走 formaction 原生提交）。
 */
(function () {
  'use strict';

  // ===== 0. 通用小工具（app.js 的 window.InkNote 缺失时各有一套兜底） =====
  var inkApi = window.InkNote || null;

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  // 目标可能在收起的 <details> 里：先打开它所在的折叠块，再聚焦 / 滚动过去。
  function revealInDetails(node) {
    if (!node || !node.closest) { return; }
    var box = node.closest('details');
    if (box && !box.open) { box.open = true; }
  }
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== null && text !== undefined) { node.textContent = String(text); }
    return node;
  }
  function val(sel) {
    var node = $(sel);
    return node ? String(node.value == null ? '' : node.value).replace(/^\s+|\s+$/g, '') : '';
  }
  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn, { once: true });
    else fn();
  }
  function safe(fn) {
    try { fn(); } catch (err) { if (window.console) { window.console.error('[InkNote settings]', err); } }
  }

  function fallbackToast(msg, kind) {
    var wrap = document.getElementById('toast-wrap');
    if (!wrap) { return; }
    var node = el('div', 'toast' + (kind ? ' toast--' + kind : ''), msg);
    wrap.appendChild(node);
    window.setTimeout(function () {
      node.classList.add('is-out');
      window.setTimeout(function () { if (node.parentNode) { node.parentNode.removeChild(node); } }, 300);
    }, 3000);
  }
  function toast(msg, kind) {
    if (inkApi && typeof inkApi.toast === 'function') {
      try { inkApi.toast(msg, kind); return; } catch (e) { /* 退回兜底 */ }
    }
    fallbackToast(msg, kind);
  }
  function getCsrf() {
    if (inkApi && typeof inkApi.csrf === 'function') {
      try { var token = inkApi.csrf(); if (token) { return token; } } catch (e) { /* 退回 meta */ }
    }
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? (meta.getAttribute('content') || '') : '';
  }

  // 优先复用 app.js 的 fetchJSON（会带 CSRF / JSON 头）；没有就本地实现一份等价的。
  function requestJSON(url, options) {
    var opts = {}, src = options || {}, key;
    for (key in src) if (Object.prototype.hasOwnProperty.call(src, key)) { opts[key] = src[key]; }
    if (inkApi && typeof inkApi.fetchJSON === 'function') {
      return Promise.resolve().then(function () { return inkApi.fetchJSON(url, opts); });
    }

    var headers = {};
    if (opts.body !== null && opts.body !== undefined) {
      opts.body = JSON.stringify(opts.body);
      headers['Content-Type'] = 'application/json';
    }
    var token = getCsrf();
    if (token) { headers['X-CSRF-Token'] = token; }
    headers['X-Requested-With'] = 'fetch';
    headers['Accept'] = 'application/json';
    opts.headers = headers;
    if (!opts.credentials) { opts.credentials = 'same-origin'; }

    return window.fetch(url, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
        if (!res.ok) {
          var message = (data && (data.error || data.detail)) || ('请求失败（HTTP ' + res.status + '）');
          var err = new Error(message);
          err.status = res.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  // 页面顶部的一块提示区：所有错误都写在这里，不用 alert。
  function showFeedback(message, kind) {
    var box = document.getElementById('ai-feedback');
    if (!box) { toast(message, kind); return; }
    box.textContent = message || '';
    box.className = 'ai-alert' + (kind ? ' ai-alert--' + kind : '');
    box.hidden = !message;
  }

  // ===== 1. 一键预设：把 data-* 里的地址/模型填进表单，并高亮当前选中 =====
  function initPresets() {
    var cards = $$('.ai-preset-card');
    if (!cards.length) { return; }
    var baseInput = $('#ai-base-url');
    var modelInput = $('#ai-model');
    var embedInput = $('#ai-embed-model');

    cards.forEach(function (card) {
      var btn = card.querySelector('.ai-preset');
      if (!btn) { return; }
      btn.addEventListener('click', function () {
        if (baseInput) { baseInput.value = btn.getAttribute('data-base-url') || ''; }
        if (modelInput) { modelInput.value = btn.getAttribute('data-model') || ''; }
        if (embedInput) { embedInput.value = btn.getAttribute('data-embed-model') || ''; }
        cards.forEach(function (other) { other.classList.toggle('is-active', other === card); });
        var nameNode = btn.querySelector('.ai-preset__name');
        var name = nameNode ? nameNode.textContent : '预设';
        showFeedback('已填入「' + name + '」的地址和模型，确认后点「保存配置」即可生效。', 'ok');
        if (modelInput) { revealInDetails(modelInput); modelInput.focus(); }
      });
    });
  }

  // ===== 2. 模型下拉：GET /api/ai/models → 自己画一个能点开、能搜索的下拉面板 =====
  // 刻意不用 <datalist>：它点一下不弹、只做前缀匹配、各浏览器还不一致，很难用。
  var MODEL_CACHE_KEY = 'inknote.ai.models';

  function readModelCache() {
    try {
      var raw = window.localStorage.getItem(MODEL_CACHE_KEY);
      var list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list.filter(function (x) { return typeof x === 'string'; }) : [];
    } catch (err) { return []; }
  }

  function writeModelCache(models) {
    try { window.localStorage.setItem(MODEL_CACHE_KEY, JSON.stringify(models || [])); }
    catch (err) { /* 无痕模式就算了 */ }
  }

  var MODEL_STATE = { models: readModelCache(), loading: false, lastError: '' };

  function comboPanelOf(combo) { return combo ? combo.querySelector('.combo__panel') : null; }

  function closeAllCombos(except) {
    $$('.combo').forEach(function (combo) {
      if (combo === except) { return; }
      var panel = comboPanelOf(combo);
      if (panel) { panel.hidden = true; }
      var input = combo.querySelector('.combo__input');
      if (input) { input.setAttribute('aria-expanded', 'false'); }
    });
  }

  function renderCombo(combo) {
    var panel = comboPanelOf(combo);
    var input = combo.querySelector('.combo__input');
    if (!panel || !input) { return; }
    var word = String(input.value || '').toLowerCase().replace(/^\s+|\s+$/g, '');
    var models = MODEL_STATE.models.filter(function (name) {
      return !word || name.toLowerCase().indexOf(word) >= 0;
    });
    panel.textContent = '';

    if (!models.length) {
      var empty = el('div', 'combo__empty');
      empty.textContent = MODEL_STATE.loading
        ? '正在拉取模型列表…'
        : (MODEL_STATE.models.length
          ? '没有匹配的模型（可以直接手输）'
          : (MODEL_STATE.lastError || '还没有模型列表，点「获取可用模型」'));
      panel.appendChild(empty);
    } else {
      models.slice(0, 300).forEach(function (name) {
        var item = el('div', 'combo__item');
        item.setAttribute('role', 'option');
        item.setAttribute('data-value', name);
        item.textContent = name;
        if (name === String(input.value || '').replace(/^\s+|\s+$/g, '')) { item.classList.add('is-selected'); }
        panel.appendChild(item);
      });
    }
    panel.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  }

  function pickComboValue(combo, value) {
    var input = combo.querySelector('.combo__input');
    if (!input) { return; }
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    closeAllCombos();
    revealInDetails(input);
    input.focus();
  }

  function initCombo(combo) {
    var input = combo.querySelector('.combo__input');
    var toggle = combo.querySelector('.combo__toggle');
    if (!input) { return; }

    function open() {
      closeAllCombos(combo);
      if (!MODEL_STATE.models.length) {
        fetchModels().then(function () { safe(function () { renderCombo(combo); }); });
      }
      renderCombo(combo);
    }

    if (toggle) {
      toggle.addEventListener('mousedown', function (event) { event.preventDefault(); });
      toggle.addEventListener('click', open);
    }
    input.addEventListener('focus', open);
    input.addEventListener('click', open);
    input.addEventListener('input', function () { renderCombo(combo); });

    input.addEventListener('keydown', function (event) {
      var panel = comboPanelOf(combo);
      if (!panel || panel.hidden) {
        if (event.key === 'ArrowDown') { open(); event.preventDefault(); }
        return;
      }
      var items = $$('.combo__item', panel);
      var index = -1;
      items.forEach(function (item, i) { if (item.classList.contains('is-active')) { index = i; } });
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (!items.length) { return; }
        var step = event.key === 'ArrowDown' ? 1 : -1;
        var next = index < 0 ? 0 : Math.min(Math.max(index + step, 0), items.length - 1);
        items.forEach(function (item) { item.classList.remove('is-active'); });
        items[next].classList.add('is-active');
        if (items[next].scrollIntoView) { items[next].scrollIntoView({ block: 'nearest' }); }
      } else if (event.key === 'Enter') {
        if (index >= 0 && items[index]) {
          event.preventDefault();
          pickComboValue(combo, items[index].getAttribute('data-value'));
        } else {
          closeAllCombos();
        }
      } else if (event.key === 'Escape') {
        closeAllCombos();
      }
    });

    combo.addEventListener('mousedown', function (event) {
      var item = event.target && event.target.closest ? event.target.closest('.combo__item') : null;
      if (item) { event.preventDefault(); pickComboValue(combo, item.getAttribute('data-value')); }
    });
  }

  function fetchModels() {
    if (MODEL_STATE.loading) { return Promise.resolve(MODEL_STATE.models); }
    var baseInput = $('#ai-base-url');
    var base = baseInput ? String(baseInput.value || '').replace(/^\s+|\s+$/g, '') : '';
    if (!base) {
      MODEL_STATE.lastError = '先填 Base URL';
      return Promise.resolve([]);
    }
    MODEL_STATE.loading = true;
    MODEL_STATE.lastError = '';
    var form = $('#ai-settings-form');
    var root = (form && form.getAttribute && form.getAttribute('data-models-url')) || '/api/ai/models';
    var url = root + '?base_url=' + encodeURIComponent(base) + '&use_saved=1';
    return requestJSON(url, { method: 'GET' }).then(function (data) {
      if (data && data.ok === false) { throw new Error(data.error || '服务商没有返回模型列表'); }
      MODEL_STATE.models = (data && data.models) || [];
      writeModelCache(MODEL_STATE.models);
      return MODEL_STATE.models;
    }).catch(function (err) {
      MODEL_STATE.lastError = (err && err.message) || '获取失败';
      return [];
    }).then(function (models) {
      MODEL_STATE.loading = false;
      $$('.combo').forEach(function (combo) {
        var panel = comboPanelOf(combo);
        if (panel && !panel.hidden) { safe(function () { renderCombo(combo); }); }
      });
      return models;
    });
  }

  function initModels() {
    var btn = $('#ai-fetch-models');
    var status = $('#ai-models-status');

    function setStatus(text, kind) {
      if (!status) { return; }
      status.textContent = text || '';
      status.className = 'ai-status' + (kind ? ' ai-status--' + kind : '');
    }

    $$('.combo[data-model-combo]').forEach(function (combo) { safe(function () { initCombo(combo); }); });

    document.addEventListener('mousedown', function (event) {
      if (!event.target || !event.target.closest || !event.target.closest('.combo')) { closeAllCombos(); }
    });

    if (MODEL_STATE.models.length) {
      setStatus('已缓存 ' + MODEL_STATE.models.length + ' 个模型名，点输入框或 ▾ 就能选', 'ok');
    }

    if (!btn) { return; }
    btn.addEventListener('click', function () {
      btn.disabled = true;
      setStatus('正在向服务商拉取模型列表…', '');
      fetchModels().then(function (models) {
        btn.disabled = false;
        if (models.length) {
          setStatus('拉到 ' + models.length + ' 个模型，点模型输入框或 ▾ 选择', 'ok');
          var first = $('.combo[data-model-combo] .combo__input');
          if (first) { revealInDetails(first); first.focus(); }
        } else {
          setStatus('没拉到模型：' + (MODEL_STATE.lastError || '服务商没有返回模型列表'), 'error');
        }
      });
    });
  }

  // ===== 3. 测试连接：POST /api/ai/probe → 逐步渲染 ✅ / ⚠️ / ❌ =====
  var PROBE_STATUS = {
    ok: { icon: '✅', label: '通过' },
    skip: { icon: '⚠️', label: '跳过' },
    fail: { icon: '❌', label: '失败' },
  };

  function renderProbe(box, data) {
    box.textContent = '';
    var steps = (data && data.steps) || [];
    if (!steps.length) {
      box.appendChild(el('div', 'ai-probe__error', (data && data.error) || '没有拿到体检结果'));
      return;
    }
    var list = el('div', 'ai-probe__steps');
    steps.forEach(function (step) {
      var status = step.status || 'fail';
      var meta = PROBE_STATUS[status] || PROBE_STATUS.fail;
      var row = el('div', 'ai-probe__step ai-probe__step--' + status);
      row.appendChild(el('span', 'ai-probe__icon', meta.icon));
      row.appendChild(el('span', 'ai-probe__name', (step.name || '步骤') + '：' + meta.label));
      row.appendChild(el('span', 'ai-probe__detail', step.detail || ''));
      list.appendChild(row);
    });
    box.appendChild(list);
    if (data && data.reply) {
      box.appendChild(el('p', 'ai-probe__reply', '模型回复：' + data.reply));
    }
    box.appendChild(
      el('p', 'ai-probe__summary', data && data.ok ? '全部通过，这份配置可用。' : '还有没通过的步骤，照着上面的说明改一改再试。')
    );
  }

  function initProbe(form) {
    var btn = $('#ai-probe');
    var box = $('#ai-probe-result');
    if (!btn || !box) { return; }
    btn.addEventListener('click', function (event) {
      // 有 JS：拦下原生提交，改走 JSON 接口逐步展示；没 JS：按钮自己会 POST /settings/ai/test
      event.preventDefault();
      var base = val('#ai-base-url');
      var model = val('#ai-model');
      if (!base || !model) { showFeedback('先把 Base URL 和模型名填上，再测试连接。', 'warn'); return; }
      btn.disabled = true;
      box.hidden = false;
      box.textContent = '';
      box.appendChild(el('p', 'ai-probe__loading', '正在逐步体检：地址 → 密钥 → 模型名 → 对话…'));
      requestJSON(form.getAttribute('data-probe-url') || '/api/ai/probe', {
        method: 'POST',
        body: {
          base_url: base,
          model: model,
          timeout: val('#ai-timeout') || '45',
          api_key: val('#ai-api-key'),
        },
      }).then(function (data) {
        renderProbe(box, data || {});
        showFeedback(data && data.ok ? '连接测试通过。' : '连接测试没有全部通过，看下面的逐步结果。', data && data.ok ? 'ok' : 'error');
      }).catch(function (err) {
        var message = (err && err.message) || '请求失败';
        renderProbe(box, { ok: false, steps: [], error: message });
        showFeedback('测试连接失败：' + message, 'error');
      }).then(function () {
        btn.disabled = false;
      });
    });
  }

  // ===== 4. 向量索引：GET status 显示进度 / POST rebuild 后轮询 =====
  var EMBED_UNAVAILABLE = '向量检索暂不可用（正由另一个模块实现）';

  function initEmbed(form) {
    var root = document.getElementById('ai-embed');
    if (!root) { return; }
    var countNode = document.getElementById('ai-embed-count');
    var hintNode = document.getElementById('ai-embed-hint');
    var progressNode = document.getElementById('ai-embed-progress');
    var barNode = document.getElementById('ai-embed-progress-bar');
    var rebuildBtn = document.getElementById('ai-embed-rebuild');
    var statusUrl = root.getAttribute('data-status-url') || form.getAttribute('data-embed-status-url') || '/api/ai/embed/status';
    var rebuildUrl = root.getAttribute('data-rebuild-url') || form.getAttribute('data-embed-rebuild-url') || '/api/ai/embed/rebuild';
    var pollTimer = 0;

    function setHint(text, kind) {
      if (!hintNode) { return; }
      hintNode.textContent = text || '';
      hintNode.className = 'ai-embed__hint micro' + (kind ? ' ai-status--' + kind : '');
    }
    function showUnavailable(detail) {
      if (countNode) { countNode.textContent = '向量检索暂不可用'; }
      setHint(detail ? ('向量检索暂不可用：' + detail) : EMBED_UNAVAILABLE, 'error');
      if (progressNode) { progressNode.hidden = true; }
    }
    function stopPoll() {
      if (pollTimer) { window.clearTimeout(pollTimer); pollTimer = 0; }
    }
    function schedulePoll() {
      if (pollTimer) { return; }
      pollTimer = window.setTimeout(function () { pollTimer = 0; loadStatus(); }, 1500);
    }
    function setBar(percent) {
      if (!progressNode || !barNode) { return; }
      progressNode.hidden = false;
      barNode.style.width = Math.max(0, Math.min(100, percent)) + '%';
    }
    function renderStatus(data) {
      if (!data || data.error) { showUnavailable(data && data.error ? data.error : ''); return; }
      var total = Number(data.total || 0);
      var indexed = Number(data.indexed || 0);
      if (!data.configured) {
        if (countNode) { countNode.textContent = '还没配置向量模型'; }
        setHint('先在上面填「向量模型」，保存后就能建索引。', 'warn');
        if (progressNode) { progressNode.hidden = true; }
        return;
      }
      var percent = total > 0 ? Math.round(indexed * 100 / total) : 0;
      if (data.running && data.progress && Number(data.progress.total)) {
        percent = Math.round((Number(data.progress.done) || 0) * 100 / (Number(data.progress.total) || 1));
      }
      if (countNode) { countNode.textContent = '已索引 ' + indexed + ' / 共 ' + total + ' 篇'; }
      setBar(percent);
      if (data.running) {
        var done = data.progress ? (data.progress.done + '/' + data.progress.total) : '处理中';
        setHint('正在重建索引…（' + done + '），稍等就好。', '');
        schedulePoll();
      } else {
        setHint(data.last_built_at ? ('索引已就绪，上次重建：' + data.last_built_at) : '索引已就绪。', 'ok');
      }
    }
    function loadStatus() {
      requestJSON(statusUrl, { method: 'GET' }).then(function (data) {
        renderStatus(data || {});
      }).catch(function () {
        stopPoll();
        showUnavailable('');
      });
    }

    if (rebuildBtn) {
      rebuildBtn.addEventListener('click', function () {
        rebuildBtn.disabled = true;
        setHint('正在启动重建…', '');
        requestJSON(rebuildUrl, { method: 'POST', body: {} }).then(function (data) {
          if (data && data.ok === false) { throw new Error(data.error || '启动失败'); }
          setHint('已开始重建，正在轮询进度…', '');
          loadStatus();
        }).catch(function (err) {
          showUnavailable((err && err.message) || '');
        }).then(function () {
          rebuildBtn.disabled = false;
        });
      });
    }

    loadStatus();
  }

  // ===== 5. 启动 =====
  function boot() {
    var form = document.getElementById('ai-settings-form');
    if (!form) { return; }
    safe(initPresets);
    safe(function () { initModels(); });
    safe(function () { initProbe(form); });
    safe(function () { initEmbed(form); });
  }

  ready(boot);
})();
