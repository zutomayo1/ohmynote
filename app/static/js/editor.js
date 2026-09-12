/*!
 * editor.js —— 墨痕 InkNote 编辑器页脚本
 * 契约：docs/frontend-contract.md 3.2 / 4 / 5。普通脚本（非 module），IIFE + strict，无依赖。
 */
(function () {
  'use strict';
  /* ===== 0. window.InkNote 容错层（toast / csrf / fetchJSON 缺失时本地兜底） ===== */
  var inkApi = window.InkNote || null;
  function fallbackToast(msg, kind) {
    var wrap = document.getElementById('toast-wrap');
    if (!wrap) { try { console.log('[InkNote]', msg); } catch (e) { /* 忽略 */ } return; }
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? ' toast--' + kind : '');
    el.textContent = msg == null ? '' : String(msg);
    wrap.appendChild(el);
    window.setTimeout(function () {
      el.classList.add('is-out');
      window.setTimeout(function () { if (el.parentNode) { el.parentNode.removeChild(el); } }, 300);
    }, 3000);
  }
  function toast(msg, kind) {
    if (inkApi && typeof inkApi.toast === 'function') {
      try { inkApi.toast(msg, kind); return; } catch (e) { /* 退到兜底 */ }
    }
    fallbackToast(msg, kind);
  }
  function getCsrf() {
    if (inkApi && typeof inkApi.csrf === 'function') {
      try { var token = inkApi.csrf(); if (token) { return token; } } catch (e) { /* 退到 meta */ }
    }
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? (meta.getAttribute('content') || '') : '';
  }
  function mergeHeaders(extra) {
    var headers = {};
    if (extra) { Object.keys(extra).forEach(function (key) { headers[key] = extra[key]; }); }
    return headers;
  }
  function setHeader(headers, name, value) {
    var lower = name.toLowerCase();
    var exists = Object.keys(headers).some(function (key) { return key.toLowerCase() === lower; });
    if (!exists) { headers[name] = value; }
  }
  // 统一成 fetch 风格：JSON 字符串 body + Content-Type + CSRF。app.js 的 fetchJSON 只对对象 body 做 JSON.stringify，传字符串不会被二次序列化；FormData 交给浏览器补 boundary。
  function normalizeOptions(options) {
    var opts = {};
    options = options || {};
    Object.keys(options).forEach(function (key) { opts[key] = options[key]; });
    opts.headers = mergeHeaders(options.headers);
    var method = (opts.method || 'GET').toUpperCase();
    opts.method = method;
    if (opts.json !== undefined) {
      opts.body = JSON.stringify(opts.json);
      setHeader(opts.headers, 'Content-Type', 'application/json');
      delete opts.json;
    } else if (opts.form !== undefined) {
      opts.body = opts.form;
      delete opts.form;
    }
    var isForm = typeof FormData !== 'undefined' && opts.body instanceof FormData;
    if (isForm) {
      Object.keys(opts.headers).forEach(function (key) {
        if (key.toLowerCase() === 'content-type') { delete opts.headers[key]; }
      });
    }
    setHeader(opts.headers, 'Accept', 'application/json');
    if (!isForm && method !== 'GET' && method !== 'HEAD') {
      setHeader(opts.headers, 'Content-Type', 'application/json');
    }
    if (method !== 'GET' && method !== 'HEAD') {
      var csrf = getCsrf();
      if (csrf) { setHeader(opts.headers, 'X-CSRF-Token', csrf); }
    }
    return opts;
  }
  function extractError(data) {
    if (!data) { return ''; }
    return typeof data === 'string' ? data : (data.error || data.detail || data.message || data.msg || '');
  }
  function makeApiError(msg, data, status) {
    var err = new Error(msg || '请求失败');
    err.name = 'ApiError';
    err.status = status || (data && data.status) || 0;
    err.data = data || null;
    err.__api = true;
    return err;
  }
  function toApiError(err) {
    if (err && err.__api) { return err; }
    if (err && err.name === 'AbortError') { return makeApiError('请求已取消', null, 0); }
    var data = null;
    if (err) {
      if (err.data && typeof err.data === 'object') { data = err.data; }
      else if (err.response && typeof err.response === 'object') { data = err.response; }
      else if (err.body && typeof err.body === 'object') { data = err.body; }
      else if (err.detail && typeof err.detail === 'object') { data = err.detail; }
    }
    return makeApiError(extractError(data) || (err && err.message) || '请求失败', data, err && (err.status || err.statusCode));
  }
  function localFetchJSON(url, opts) {
    return fetch(url, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = {};
        if (text) { try { data = JSON.parse(text); } catch (e) { data = { raw: text }; } }
        if (!res.ok) { throw makeApiError(extractError(data) || ('请求失败（HTTP ' + res.status + '）'), data, res.status); }
        return data;
      });
    });
  }
  // 优先 app.js 的 fetchJSON，缺失则本地 fetch。
  function requestJSON(url, options) {
    var opts = normalizeOptions(options);
    var chain = (inkApi && typeof inkApi.fetchJSON === 'function')
      ? Promise.resolve().then(function () { return inkApi.fetchJSON(url, opts); })
      : localFetchJSON(url, opts);
    return chain.then(function (data) {
      if (data && data.ok === false) { throw makeApiError(extractError(data) || '请求失败', data, 0); }
      return data;
    }).catch(function (err) { throw toApiError(err); });
  }
  /* ===== 0.5 标签解析纯函数（与后端 app/utils.py 对齐；挂到 window.InkNote.tagUtils 便于 node 复测） ===== */
  var TAG_SPLIT_RE = /[,，、;；\n\r\t|]+|\s+/;
  var TAG_MAX = 12;        // 对齐 app/utils.py MAX_TAGS（后端实际是 12，不是 10）
  var TAG_MAX_LEN = 40;    // 对齐 app/utils.py MAX_TAG_LEN
  function normalizeTag(name) {
    var text = (name == null ? '' : String(name)).trim();
    text = text.replace(/^#+/, '').trim();   // 去掉 # 前缀
    text = text.replace(/\s+/g, ' ');        // 连续空白折叠成一个空格
    return text.slice(0, TAG_MAX_LEN);
  }
  function splitRawTags(raw) {
    if (raw == null) { return []; }
    var items = Object.prototype.toString.call(raw) === '[object Array]'
      ? raw : String(raw).split(TAG_SPLIT_RE);
    var out = [];
    for (var i = 0; i < items.length; i += 1) {
      var name = normalizeTag(items[i]);
      if (name) { out.push(name); }
    }
    return out;
  }
  function parseTags(raw) {
    var items = splitRawTags(raw);
    var result = [];
    var seen = {};
    for (var i = 0; i < items.length; i += 1) {
      var key = items[i].toLowerCase();     // 大小写不敏感去重
      if (seen[key]) { continue; }
      seen[key] = true;
      result.push(items[i]);
      if (result.length >= TAG_MAX) { break; }   // 最多 TAG_MAX 个
    }
    return result;
  }
  // 挂到命名空间，供 node -e / 测试复用；不依赖 DOM。
  try {
    var tagHost = (typeof window !== 'undefined') ? window : null;
    if (tagHost) {
      var tagNs = tagHost.InkNote;
      if (!tagNs || typeof tagNs !== 'object') { tagNs = tagHost.InkNote = {}; }
      if (!tagNs.tagUtils) {
        tagNs.tagUtils = {
          parseTags: parseTags, splitRawTags: splitRawTags, normalizeTag: normalizeTag,
          MAX_TAGS: TAG_MAX, MAX_TAG_LEN: TAG_MAX_LEN
        };
      }
    }
  } catch (errTag) { /* 非浏览器环境忽略 */ }

  /* ===== 1. 页面闸门与元素引用 ===== */
  function boot() {
    if (!document.body || document.body.dataset.page !== 'editor') { return; }
    init();
  }
  function init() {
    var form = document.getElementById('editor-form');
    if (!form) { return; }
    // 契约 3.2 列出的全部 id；缺失只降级对应功能，不抛错。
    var titleInput = document.getElementById('note-title');
    var tabs = document.getElementById('editor-tabs');
    var split = document.getElementById('editor-split');
    var textarea = document.getElementById('note-content');
    var preview = document.getElementById('preview');
    var toolbar = document.getElementById('md-toolbar');
    var uploadInput = document.getElementById('upload-input');
    var wordCountEl = document.getElementById('word-count');
    var readingEl = document.getElementById('reading-time');
    var cursorEl = document.getElementById('cursor-info');
    var settings = document.getElementById('editor-settings');
    var tagsInput = document.getElementById('tags-input');
    var aiSummaryBtn = document.getElementById('ai-summary');
    var aiTagsBtn = document.getElementById('ai-tags');
    var aiTitleBtn = document.getElementById('ai-title');
    var aiCategoryBtn = document.getElementById('ai-category');
    var aiHint = document.getElementById('ai-hint');
    var autosaveEl = document.getElementById('autosave-state');
    // 发布设置字段：随表单正常提交。categoryInput 会被「AI 推荐分类」写入，其余不动。
    var categoryInput = document.getElementById('category-input');
    var summaryInput = document.getElementById('summary-input');
    var slugInput = document.getElementById('slug-input');
    var metaDescriptionInput = document.getElementById('meta-description-input');
    if (!textarea) { return; }
    var noteId = (form.dataset.noteId || '').trim();
    var state = { dirty: false, saving: false, pending: false, previewTimer: null,
      saveTimer: null, previewErrorShown: false, uploadPrevState: null, userChoseLayout: false };
    /* ===== 2. 实时预览（输入 300ms 防抖 → POST data-preview-url） ===== */
    function showPlaceholder() {
      if (preview) { preview.innerHTML = '<p class="muted">开始输入，右侧会实时预览</p>'; }
    }
    function applyServerStats(wordCount, minutes) {
      if (wordCountEl && typeof wordCount === 'number') { wordCountEl.textContent = wordCount + ' 字'; }
      if (readingEl && typeof minutes === 'number') { readingEl.textContent = '约 ' + minutes + ' 分钟'; }
    }
    function fetchPreview() {
      state.previewTimer = null;
      var content = textarea.value;
      if (!content.trim()) { showPlaceholder(); state.previewErrorShown = false; return Promise.resolve(); }
      var payload = { content: content, title: titleInput ? titleInput.value : '' };
      return requestJSON(form.dataset.previewUrl || '/api/preview', { method: 'POST', json: payload })
        .then(function (data) {
          state.previewErrorShown = false;
          if (!data) { return; }
          if (data.empty || !content.trim()) { showPlaceholder(); }
          else if (typeof data.html === 'string' && preview) { preview.innerHTML = data.html; }
          applyServerStats(data.word_count, data.reading_minutes);
        })
        .catch(function (err) {
          // 失败保留旧预览，只 toast 一次，避免刷屏。
          if (!state.previewErrorShown) {
            state.previewErrorShown = true;
            toast((err && err.message) || '预览失败，请稍后再试', 'error');
          }
        });
    }
    function schedulePreview() {
      if (state.previewTimer) { window.clearTimeout(state.previewTimer); }
      state.previewTimer = window.setTimeout(fetchPreview, 300);
    }
    /* ===== 3. 本地字数 / 阅读时长 / 行号（对齐 markdown_render.text_stats） ===== */
    var CJK_RE = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]/g;
    var LATIN_RE = /[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*/g;
    function stripMarkdown(source) {
      var text = source || '';
      text = text.replace(/```[\s\S]*?```/g, ' ').replace(/~~~[\s\S]*?~~~/g, ' ');
      text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
      text = text.replace(/\[\[([^\[\]|]+)\|([^\[\]]+)\]\]/g, '$2').replace(/\[\[([^\[\]]+)\]\]/g, '$1');
      text = text.replace(/^\s{0,3}#{1,6}\s*/gm, '').replace(/^\s{0,3}>\s?/gm, '');
      text = text.replace(/^\s{0,3}([-*+]|\d+[.)])\s+/gm, '').replace(/^\s{0,3}\[[ xX]\]\s*/gm, '');
      text = text.replace(/^\s{0,3}([-*_])(\s*\1){2,}\s*$/gm, ' ').replace(/^\s{0,3}\[\^[^\]]+\]:.*$/gm, ' ');
      text = text.replace(/`+([^`]*)`+/g, '$1');
      text = text.replace(/(\*\*\*|___)([\s\S]+?)\1/g, '$2').replace(/(\*\*|__)([\s\S]+?)\1/g, '$2');
      text = text.replace(/(?<!\*)\*(?!\s)([\s\S]+?)(?<!\s)\*(?!\*)/g, '$1');
      text = text.replace(/(?<![\w_])_(?!\s)([\s\S]+?)(?<!\s)_(?![\w_])/g, '$1');
      text = text.replace(/~~([\s\S]+?)~~/g, '$1').replace(/==([\s\S]+?)==/g, '$1');
      text = text.replace(/<[^>]{1,200}>/g, ' ').replace(/^\s*\|.*\|\s*$/gm, ' ');
      return text;
    }
    function countStats(source) {
      var plain = stripMarkdown(source);
      var cjk = (plain.match(CJK_RE) || []).length;
      var latin = (plain.match(LATIN_RE) || []).length;
      var words = cjk + latin;
      var minutes = words <= 0 ? 0 : Math.max(1, Math.ceil(cjk / 400 + latin / 220));
      return { cjk: cjk, latin: latin, words: words, minutes: minutes };
    }
    function renderStats() {
      var stats = countStats(textarea.value);
      if (wordCountEl) { wordCountEl.textContent = stats.words + ' 字'; }
      if (readingEl) { readingEl.textContent = '约 ' + stats.minutes + ' 分钟'; }
    }
    function updateCursorInfo() {
      if (!cursorEl) { return; }
      var position = textarea.selectionStart || 0;
      var line = 1;
      for (var i = 0; i < position; i += 1) { if (textarea.value.charCodeAt(i) === 10) { line += 1; } }
      cursorEl.textContent = '第 ' + line + ' 行';
    }
    /* ===== 4. 三档布局（write / split / preview，≤900px 初始 write） ===== */
    function setLayout(mode, fromUser) {
      if (mode !== 'write' && mode !== 'split' && mode !== 'preview') { mode = 'split'; }
      if (split) { split.setAttribute('data-layout', mode); }
      if (tabs) {
        var buttons = tabs.querySelectorAll('button[data-mode]');
        for (var i = 0; i < buttons.length; i += 1) {
          buttons[i].classList.toggle('is-active', buttons[i].getAttribute('data-mode') === mode);
        }
      }
      if (fromUser) { state.userChoseLayout = true; }
    }
    if (tabs) {
      tabs.addEventListener('click', function (event) {
        var target = event.target;
        var btn = target && target.nodeType === 1 && target.closest ? target.closest('button[data-mode]') : null;
        if (!btn) { return; }
        event.preventDefault();
        setLayout(btn.getAttribute('data-mode'), true);
      });
    }
    var mq = window.matchMedia ? window.matchMedia('(max-width: 900px)') : null;
    var defaultLayout = split ? (split.getAttribute('data-layout') || 'split') : 'split';
    setLayout(mq && mq.matches ? 'write' : defaultLayout, false);
    if (mq) {
      var onMqChange = function () {
        if (!state.userChoseLayout) { setLayout(mq.matches ? 'write' : 'split', false); }
      };
      if (typeof mq.addEventListener === 'function') { mq.addEventListener('change', onMqChange); }
      else if (typeof mq.addListener === 'function') { mq.addListener(onMqChange); }
    }
    /* ===== 5. 自动保存（仅 data-note-id 非空；停止输入 2.5s 后 PATCH） ===== */
    var AUTOSAVE_TEXT = { idle: '已保存', dirty: '未保存的改动', saving: '保存中…',
      saved: '已保存', error: '保存失败，将重试', offline: '离线（稍后重试）' };
    function setAutosaveState(name) {
      if (!autosaveEl) { return; }
      autosaveEl.dataset.state = name;
      autosaveEl.textContent = AUTOSAVE_TEXT[name] || name;
    }
    function markDirty() {
      state.dirty = true;
      if (!noteId) { return; }  // 新建笔记没有 PATCH 目标，仅保留离开提醒
      setAutosaveState('dirty');
      scheduleSave();
    }
    function scheduleSave() {
      if (!noteId) { return; }
      if (state.saveTimer) { window.clearTimeout(state.saveTimer); }
      state.saveTimer = window.setTimeout(function () { state.saveTimer = null; saveNow(); }, 2500);
    }
    function saveNow() {
      if (!noteId) { return Promise.resolve(false); }
      if (state.saving) { state.pending = true; return Promise.resolve(false); }  // in-flight 时不并发
      state.saving = true;
      setAutosaveState('saving');
      var url = form.dataset.autosaveUrl || ('/api/notes/' + encodeURIComponent(noteId));
      var sentTitle = titleInput ? titleInput.value : '';
      var sentContent = textarea.value;
      return requestJSON(url, { method: 'PATCH', json: { title: sentTitle, content: sentContent } })
        .then(function (data) {
          state.saving = false;
          var changed = (titleInput ? titleInput.value : '') !== sentTitle || textarea.value !== sentContent;
          state.dirty = changed;
          if (changed) { state.pending = true; }
          setAutosaveState(changed ? 'dirty' : 'saved');
          if (data) { applyServerStats(data.word_count, data.reading_minutes); }
          if (state.pending) { state.pending = false; saveNow(); }  // 保存期间又有改动 -> 补一次
          return !changed;
        })
        .catch(function (err) {
          state.saving = false;
          state.pending = false;
          state.dirty = true;
          setAutosaveState('error');  // 下次输入会重新 scheduleSave
          toast((err && err.message) || '保存失败，请稍后再试', 'error');
          return false;
        });
    }
    function handleContentInput() {
      renderStats();
      updateCursorInfo();
      schedulePreview();
      markDirty();
    }
    function handleTitleInput() { schedulePreview(); markDirty(); }
    if (titleInput) { titleInput.addEventListener('input', handleTitleInput); }
    textarea.addEventListener('input', handleContentInput);
    textarea.addEventListener('click', updateCursorInfo);
    textarea.addEventListener('keyup', updateCursorInfo);
    document.addEventListener('selectionchange', function () {
      if (document.activeElement === textarea) { updateCursorInfo(); }
    });
    // 正常提交（保存 / 保存并查看 / 存为草稿）不算未保存离开。
    form.addEventListener('submit', function () {
      state.dirty = false;
      if (state.saveTimer) { window.clearTimeout(state.saveTimer); state.saveTimer = null; }
    });
    window.addEventListener('beforeunload', function (event) {
      if (!state.dirty) { return undefined; }
      var message = form.dataset.unsavedText || '有未保存的改动';
      event.preventDefault();
      event.returnValue = message;
      return message;
    });
    /* ===== 6. Markdown 工具条（17 个 data-md：包裹 / 行前缀 / 插入） ===== */
    function setValueAndSelection(value, selStart, selEnd) {
      textarea.value = value;
      textarea.focus();
      if (typeof selStart === 'number') {
        textarea.setSelectionRange(selStart, typeof selEnd === 'number' ? selEnd : selStart);
      }
      updateCursorInfo();
      textarea.dispatchEvent(new Event('input', { bubbles: true }));  // 触发一次预览 + 脏标记
    }
    function currentSelection() {
      var start = textarea.selectionStart;
      var end = textarea.selectionEnd;
      return { value: textarea.value, start: start, end: end, text: textarea.value.slice(start, end) };
    }
    function mapLines(mapper) {
      var sel = currentSelection();
      var lineStart = sel.value.lastIndexOf('\n', sel.start - 1) + 1;
      var lineEnd = sel.value.indexOf('\n', sel.end);
      if (lineEnd < 0) { lineEnd = sel.value.length; }
      var replaced = mapper(sel.value.slice(lineStart, lineEnd).split('\n')).join('\n');
      setValueAndSelection(sel.value.slice(0, lineStart) + replaced + sel.value.slice(lineEnd),
        lineStart, lineStart + replaced.length);
    }
    function toggleWrap(mark, placeholder) {
      var sel = currentSelection();
      var len = mark.length;
      var before = sel.value.slice(Math.max(0, sel.start - len), sel.start);
      var after = sel.value.slice(sel.end, sel.end + len);
      if (sel.start !== sel.end && before === mark && after === mark) {  // 选区外侧已包裹 -> 取消
        setValueAndSelection(sel.value.slice(0, sel.start - len) + sel.text + sel.value.slice(sel.end + len),
          sel.start - len, sel.start - len + sel.text.length);
        return;
      }
      if (sel.text.length >= len * 2 && sel.text.slice(0, len) === mark && sel.text.slice(-len) === mark) {
        var inner = sel.text.slice(len, sel.text.length - len);  // 选区自身已包裹 -> 取消
        setValueAndSelection(sel.value.slice(0, sel.start) + inner + sel.value.slice(sel.end),
          sel.start, sel.start + inner.length);
        return;
      }
      var text = sel.text || placeholder;
      setValueAndSelection(sel.value.slice(0, sel.start) + mark + text + mark + sel.value.slice(sel.end),
        sel.start + len, sel.start + len + text.length);
    }
    function togglePrefix(prefix) {
      mapLines(function (lines) {
        var all = lines.length > 0 && lines.every(function (line) {
          return line.indexOf(prefix) === 0 || line.trim() === '';
        });
        return lines.map(function (line) {
          if (line.trim() === '') { return all ? '' : prefix; }
          if (all) { return line.slice(prefix.length); }
          return line.indexOf(prefix) === 0 ? line : prefix + line;
        });
      });
    }
    function applyHeading(level) {
      var prefix = new Array(level + 1).join('#') + ' ';
      mapLines(function (lines) {
        var all = lines.length > 0 && lines.every(function (line) {
          var match = /^\s{0,3}(#{1,6})\s+/.exec(line);
          return !!match && match[1].length === level;
        });
        return lines.map(function (line) {
          var stripped = line.replace(/^\s{0,3}#{1,6}\s+/, '');
          return all ? stripped : prefix + stripped;
        });
      });
    }
    function applyOrderedList() {
      mapLines(function (lines) {
        var all = lines.length > 0 && lines.every(function (line) { return /^\s{0,3}\d+[.)]\s+/.test(line); });
        var index = 0;
        return lines.map(function (line) {
          if (all) { return line.replace(/^(\s{0,3})\d+[.)]\s+/, '$1'); }
          index += 1;
          if (/^\s{0,3}\d+[.)]\s+/.test(line)) {
            return line.replace(/^(\s{0,3})\d+[.)]\s+/, '$1' + index + '. ');
          }
          return index + '. ' + line;
        });
      });
    }
    function insertBlock(text, caretOffset, selectLength) {
      var sel = currentSelection();
      var caret = sel.start + (typeof caretOffset === 'number' ? caretOffset : text.length);
      setValueAndSelection(sel.value.slice(0, sel.start) + text + sel.value.slice(sel.end),
        caret, typeof selectLength === 'number' ? caret + selectLength : caret);
    }
    function insertCodeBlock() {
      var sel = currentSelection();
      var code = sel.text || '代码';
      setValueAndSelection(sel.value.slice(0, sel.start) + '```\n' + code + '\n```' + sel.value.slice(sel.end),
        sel.start + 4, sel.start + 4 + code.length);
    }
    function insertLink() {
      var sel = currentSelection();
      var label = sel.text || '链接文字';
      var next = sel.value.slice(0, sel.start) + '[' + label + '](https://)' + sel.value.slice(sel.end);
      setValueAndSelection(next, sel.start + label.length + 3, sel.start + label.length + 11);
    }
    function insertImage() {
      var sel = currentSelection();
      var label = sel.text || '图片说明';
      var next = sel.value.slice(0, sel.start) + '![' + label + '](https://)' + sel.value.slice(sel.end);
      setValueAndSelection(next, sel.start + label.length + 4, sel.start + label.length + 12);
    }
    function insertWiki() {
      var sel = currentSelection();
      var label = sel.text || '笔记标题';
      var next = sel.value.slice(0, sel.start) + '[[' + label + ']]' + sel.value.slice(sel.end);
      setValueAndSelection(next, sel.start + 2, sel.start + 2 + label.length);
    }
    function applyTool(tool) {
      switch (tool) {
        case 'bold': toggleWrap('**', '粗体'); break;
        case 'italic': toggleWrap('*', '斜体'); break;
        case 'strike': toggleWrap('~~', '删除线'); break;
        case 'code': toggleWrap('`', '代码'); break;
        case 'h1': applyHeading(1); break;
        case 'h2': applyHeading(2); break;
        case 'h3': applyHeading(3); break;
        case 'quote': togglePrefix('> '); break;
        case 'ul': togglePrefix('- '); break;
        case 'ol': applyOrderedList(); break;
        case 'task': togglePrefix('- [ ] '); break;
        case 'codeblock': insertCodeBlock(); break;
        case 'link': insertLink(); break;
        case 'image': insertImage(); break;
        case 'wiki': insertWiki(); break;
        case 'table': insertBlock('\n| 列 1 | 列 2 |\n| --- | --- |\n| 内容 | 内容 |\n'); break;
        case 'hr': insertBlock('\n---\n'); break;
        default: break;
      }
    }
    if (toolbar) {
      // 点工具条时保住 textarea 选区；上传 label 不拦，否则打不开文件框。
      toolbar.addEventListener('mousedown', function (event) {
        var target = event.target;
        var btn = target && target.nodeType === 1 && target.closest ? target.closest('[data-md]') : null;
        if (btn) { event.preventDefault(); }
      });
      toolbar.addEventListener('click', function (event) {
        var target = event.target;
        var btn = target && target.nodeType === 1 && target.closest ? target.closest('[data-md]') : null;
        if (!btn) { return; }
        event.preventDefault();
        applyTool(btn.getAttribute('data-md'));
      });
    }
    /* ===== 7. 快捷键（Ctrl/Cmd + B / I / K / S / Enter，Tab，Esc） ===== */
    function triggerAction(value) {
      var btn = form.querySelector('button[name="action"][value="' + value + '"]');
      if (btn) { btn.click(); }
    }
    function insertTwoSpaces() {
      var sel = currentSelection();
      setValueAndSelection(sel.value.slice(0, sel.start) + '  ' + sel.value.slice(sel.end),
        sel.start + 2, sel.start + 2);
    }
    document.addEventListener('keydown', function (event) {
      var mod = event.ctrlKey || event.metaKey;
      var key = event.key;
      var inText = event.target === textarea || document.activeElement === textarea;
      if (mod && (key === 's' || key === 'S')) {
        event.preventDefault();
        if (noteId) {
          if (state.saveTimer) { window.clearTimeout(state.saveTimer); state.saveTimer = null; }
          saveNow();
        } else { triggerAction('save'); }
        return;
      }
      if (mod && key === 'Enter') { event.preventDefault(); triggerAction('view'); return; }
      if (mod && inText && (key === 'b' || key === 'B')) { event.preventDefault(); applyTool('bold'); return; }
      if (mod && inText && (key === 'i' || key === 'I')) { event.preventDefault(); applyTool('italic'); return; }
      if (mod && inText && (key === 'k' || key === 'K')) { event.preventDefault(); applyTool('link'); return; }
      if (key === 'Tab' && event.target === textarea) { event.preventDefault(); insertTwoSpaces(); return; }
      if (key === 'Escape' && settings && settings.open) { settings.open = false; event.preventDefault(); }
    }, true);
    /* ===== 8. 拖拽 / 粘贴 / 选择上传（图片串行 POST multipart file） ===== */
    function isImageFile(file) {
      if (!file) { return false; }
      if (file.type && file.type.indexOf('image/') === 0) { return true; }
      return /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i.test(file.name || '');
    }
    function imageFilesOf(list) {
      var files = [];
      if (!list) { return files; }
      for (var i = 0; i < list.length; i += 1) { if (isImageFile(list[i])) { files.push(list[i]); } }
      return files;
    }
    function insertTextAtCursor(text) {
      var sel = currentSelection();
      setValueAndSelection(sel.value.slice(0, sel.start) + text + sel.value.slice(sel.end),
        sel.start + text.length, sel.start + text.length);
    }
    function showUploadFeedback(total) {
      if (!autosaveEl || state.uploadPrevState !== null) { return; }
      state.uploadPrevState = autosaveEl.dataset.state || 'idle';
      autosaveEl.dataset.state = 'saving';
      autosaveEl.textContent = total > 1 ? ('上传中 1/' + total + '…') : '图片上传中…';
    }
    function updateUploadFeedback(index, total) {
      if (!autosaveEl || state.uploadPrevState === null || total <= 1) { return; }
      autosaveEl.textContent = '上传中 ' + index + '/' + total + '…';
    }
    function hideUploadFeedback() {
      if (!autosaveEl || state.uploadPrevState === null) { return; }
      var restore = state.uploadPrevState;
      state.uploadPrevState = null;
      setAutosaveState(restore || 'idle');
    }
    function uploadOne(file) {
      var data = new FormData();
      data.append('file', file);
      return requestJSON(form.dataset.uploadUrl || '/api/upload', { method: 'POST', form: data })
        .then(function (result) {
          if (!result || !result.url) { throw makeApiError(extractError(result) || '图片上传失败', result, 0); }
          return result;
        });
    }
    // 多张图片串行上传，全部结束后按顺序插入 markdown；单张失败也继续，最后统一提示。
    function uploadFiles(fileList) {
      var files = imageFilesOf(fileList);
      if (!files.length) { return Promise.resolve(); }
      showUploadFeedback(files.length);
      var results = [];
      var firstError = null;
      var chain = Promise.resolve();
      files.forEach(function (file, index) {
        chain = chain.then(function () {
          updateUploadFeedback(index + 1, files.length);
          return uploadOne(file).then(function (result) { results.push(result); },
            function (err) { if (!firstError) { firstError = err; } });
        });
      });
      return chain.then(function () {
        hideUploadFeedback();
        if (results.length) {
          var snippet = results.map(function (result) {
            var markdown = result.markdown || ('![' + (result.name || 'image') + '](' + (result.url || '') + ')');
            return '\n' + markdown + '\n';
          }).join('');
          insertTextAtCursor(snippet);
          toast('已插入 ' + results.length + ' 张图片', 'ok');
        }
        if (firstError) { toast((firstError && firstError.message) || '图片上传失败', 'error'); }
      });
    }
    var writePane = document.querySelector('.editor__pane--write');
    if (writePane) {
      writePane.addEventListener('dragenter', function (event) {
        event.preventDefault(); writePane.classList.add('is-dragover');
      });
      writePane.addEventListener('dragover', function (event) {
        event.preventDefault();
        if (event.dataTransfer) { event.dataTransfer.dropEffect = 'copy'; }
        writePane.classList.add('is-dragover');
      });
      writePane.addEventListener('dragleave', function (event) {
        if (event.relatedTarget && writePane.contains(event.relatedTarget)) { return; }
        writePane.classList.remove('is-dragover');
      });
      writePane.addEventListener('drop', function (event) {
        event.preventDefault();
        writePane.classList.remove('is-dragover');
        if (event.dataTransfer && imageFilesOf(event.dataTransfer.files).length) { uploadFiles(event.dataTransfer.files); }
      });
    }
    textarea.addEventListener('paste', function (event) {
      var clipboard = event.clipboardData;
      if (!clipboard) { return; }
      var files = [];
      if (clipboard.items && clipboard.items.length) {
        for (var i = 0; i < clipboard.items.length; i += 1) {
          var item = clipboard.items[i];
          if (item.kind === 'file' && isImageFile(item.getAsFile())) { files.push(item.getAsFile()); }
        }
      }
      if (!files.length && clipboard.files) { files = imageFilesOf(clipboard.files); }
      if (!files.length) { return; }
      event.preventDefault();
      uploadFiles(files);
    });
    if (uploadInput) {
      uploadInput.addEventListener('change', function () {
        var reset = function () { uploadInput.value = ''; };
        if (uploadInput.files && uploadInput.files.length) { uploadFiles(uploadInput.files).then(reset, reset); }
        else { reset(); }
      });
    }
    /* ===== 9. AI 摘要 / 标签（用户点击才调用，未配置不自动请求） ===== */
    function setAiBusy(button, busy, idleLabel) {
      if (!button) { return; }
      if (busy) {
        if (!button._idleHtml) { button._idleHtml = button.innerHTML || button.textContent || idleLabel; }
        button.disabled = true;
        button.textContent = '生成中…';
      } else {
        button.disabled = false;
        button.innerHTML = button._idleHtml || idleLabel;  // 恢复时保留按钮内的图标
      }
    }
    function aiSuccess(message) {
      if (aiHint) { aiHint.classList.remove('is-error'); aiHint.textContent = message || ''; }
      toast(message || '生成完成', 'ok');
    }
    function aiFail(err) {
      var message = (err && err.message) || 'AI 服务请求失败';
      if (aiHint) { aiHint.classList.remove('is-error'); aiHint.classList.add('is-error'); aiHint.textContent = message; }
      toast(message, 'error');
    }
    function aiPayload() {
      return { title: titleInput ? titleInput.value : '', content: textarea.value };
    }
    if (aiSummaryBtn) {
      aiSummaryBtn.addEventListener('click', function () {
        setAiBusy(aiSummaryBtn, true, 'AI 生成摘要');
        requestJSON(form.dataset.aiSummaryUrl || '/api/ai/summarize', { method: 'POST', json: aiPayload() })
          .then(function (data) {
            if (data && data.summary && summaryInput) { summaryInput.value = data.summary; }
            aiSuccess('已生成摘要');
          }, aiFail)
          .then(function () { setAiBusy(aiSummaryBtn, false, 'AI 生成摘要'); });
      });
    }
    if (aiTagsBtn) {
      aiTagsBtn.addEventListener('click', function () {
        setAiBusy(aiTagsBtn, true, 'AI 推荐标签');
        requestJSON(form.dataset.aiTagsUrl || '/api/ai/tags', { method: 'POST', json: aiPayload() })
          .then(function (data) {
            var tags = data && data.tags;
            if (Object.prototype.toString.call(tags) === '[object Array]' && tags.length && tagsInput) {
              tagsInput.value = tags.join('、');
              // 通知标签增强模块重算胶囊选中态（程序写入 value 不会自动触发 input）
              try { tagsInput.dispatchEvent(new Event('input', { bubbles: true })); } catch (errAi) { /* 老旧浏览器忽略 */ }
              aiSuccess('已推荐标签');
            } else if (Object.prototype.toString.call(tags) === '[object Array]' && !tags.length) {
              // 模型没给出标签时别报「已推荐」（输入框没变，用户会以为坏了）
              aiFail(new Error('模型这次没给出标签，可以再点一次，或手动添加'));
            } else {
              aiSuccess('已推荐标签');
            }
          }, aiFail)
          .then(function () { setAiBusy(aiTagsBtn, false, 'AI 推荐标签'); });
      });
    }
    if (aiTitleBtn) {
      aiTitleBtn.addEventListener('click', function () {
        setAiBusy(aiTitleBtn, true, 'AI 起标题');
        requestJSON(form.dataset.aiTitleUrl || '/api/ai/title', { method: 'POST', json: aiPayload() })
          .then(function (data) {
            if (data && data.title && titleInput) {
              titleInput.value = data.title;
              // 程序写入 value 不会触发 input，标题缩放之类的联动要自己喊一声
              try { titleInput.dispatchEvent(new Event('input', { bubbles: true })); } catch (errTitle) { /* 老旧浏览器忽略 */ }
              aiSuccess('已生成标题');
            } else {
              aiFail(new Error('模型没给出标题，换个说法再试试'));
            }
          }, aiFail)
          .then(function () { setAiBusy(aiTitleBtn, false, 'AI 起标题'); });
      });
    }
    if (aiCategoryBtn) {
      aiCategoryBtn.addEventListener('click', function () {
        setAiBusy(aiCategoryBtn, true, 'AI 推荐分类');
        requestJSON(form.dataset.aiCategoryUrl || '/api/ai/category', { method: 'POST', json: aiPayload() })
          .then(function (data) {
            if (data && data.category && categoryInput) {
              categoryInput.value = data.category;
              try { categoryInput.dispatchEvent(new Event('input', { bubbles: true })); } catch (errCat) { /* 老旧浏览器忽略 */ }
              aiSuccess('已推荐分类（优先复用你已有的分类）');
            } else {
              aiFail(new Error('模型没给出分类，换个说法再试试'));
            }
          }, aiFail)
          .then(function () { setAiBusy(aiCategoryBtn, false, 'AI 推荐分类'); });
      });
    }

      /* ===== 9.5 标签输入增强：常用标签胶囊 + 自动补全 + 规范化提示（纯增强，JS 挂了仍可手打） ===== */
    (function initTagInput() {
      if (!tagsInput || tagsInput.name !== 'tags') { return; }
      try {
        var widget = document.getElementById('tag-input-widget') || tagsInput.parentNode || null;
        var commonBox = document.getElementById('tag-common');
        var suggestBox = document.getElementById('tag-suggest');
        var hintEl = document.getElementById('tag-input-hint');
        var countEl = document.getElementById('tag-input-count');
        var chipNodes = (commonBox && commonBox.querySelectorAll) ? commonBox.querySelectorAll('.tag-chip') : [];
        var items = [];
        var activeIndex = -1;
        var flashTimer = null;

        function lower(text) { return String(text == null ? '' : text).toLowerCase(); }
        function indexOfTag(list, name) {
          var key = lower(name);
          for (var i = 0; i < list.length; i += 1) { if (lower(list[i]) === key) { return i; } }
          return -1;
        }
        function currentToken(value) {
          var text = String(value == null ? '' : value);
          var re = /[,，、;；\n\r\t|]|\s/g;
          var last = -1;
          var m;
          while ((m = re.exec(text)) !== null) { last = m.index + m[0].length; }
          return text.slice(last);
        }
        function setHint(text, kind) {
          if (!hintEl) { return; }
          hintEl.textContent = text || '';
          hintEl.className = 'tag-input__hint' + (kind ? ' is-' + kind : '');
        }
        function flashHint(text, kind) {
          setHint(text, kind);
          if (flashTimer) { window.clearTimeout(flashTimer); }
          flashTimer = window.setTimeout(function () { flashTimer = null; syncState(); }, 2400);
        }

        // 已知标签优先取服务端 datalist（known_tags），胶囊兜底；不再依赖原生 datalist 下拉。
        var known = [];
        var knownSeen = {};
        function addKnown(raw) {
          var name = normalizeTag(raw);
          if (!name) { return; }
          var key = lower(name);
          if (knownSeen[key]) { return; }
          knownSeen[key] = true;
          known.push(name);
        }
        try {
          if (document.querySelectorAll) {
            var optionNodes = document.querySelectorAll('#tag-options option');
            for (var oi = 0; oi < optionNodes.length; oi += 1) {
              addKnown(optionNodes[oi].getAttribute('value') || optionNodes[oi].textContent || '');
            }
          }
        } catch (errDatalist) { /* datalist 不可用 */ }
        for (var ci = 0; ci < chipNodes.length; ci += 1) {
          addKnown(chipNodes[ci].getAttribute('data-tag') || '');
        }
        // HTML 里的 datalist 保留给无 JS 场景；有 JS 时改用自定义面板，避免两个下拉打架。
        try { tagsInput.removeAttribute('list'); } catch (errList) { /* 忽略 */ }

        function closeSuggest() {
          items = [];
          activeIndex = -1;
          if (suggestBox) {
            suggestBox.hidden = true;
            suggestBox.classList.remove('is-open');
            suggestBox.innerHTML = '';
          }
          tagsInput.setAttribute('aria-expanded', 'false');
          tagsInput.removeAttribute('aria-activedescendant');
        }
        function renderActive() {
          if (!suggestBox) { return; }
          var options = suggestBox.querySelectorAll('.tag-suggest__item');
          for (var i = 0; i < options.length; i += 1) {
            var active = i === activeIndex;
            options[i].classList.toggle('is-active', active);
            options[i].setAttribute('aria-selected', active ? 'true' : 'false');
          }
          if (activeIndex >= 0 && options[activeIndex]) {
            tagsInput.setAttribute('aria-activedescendant', options[activeIndex].id);
          } else {
            tagsInput.removeAttribute('aria-activedescendant');
          }
        }
        function usedKeys() {
          var list = parseTags(tagsInput.value);
          var keys = {};
          for (var i = 0; i < list.length; i += 1) { keys[lower(list[i])] = true; }
          return keys;
        }
        function openSuggest(list) {
          if (!suggestBox || !list.length) { closeSuggest(); return; }
          items = list;
          activeIndex = 0;
          suggestBox.innerHTML = '';
          var used = usedKeys();
          for (var i = 0; i < list.length; i += 1) {
            var opt = document.createElement('div');
            opt.className = 'tag-suggest__item';
            opt.id = 'tag-suggest-opt-' + i;
            opt.setAttribute('role', 'option');
            opt.setAttribute('data-tag', list[i]);
            opt.setAttribute('aria-selected', i === 0 ? 'true' : 'false');
            var nameEl = document.createElement('span');
            nameEl.className = 'tag-suggest__name';
            nameEl.textContent = list[i];
            opt.appendChild(nameEl);
            if (used[lower(list[i])]) {
              var badge = document.createElement('span');
              badge.className = 'tag-suggest__used';
              badge.textContent = '已用';
              opt.appendChild(badge);
            }
            opt.addEventListener('mousedown', function (event) { event.preventDefault(); });
            opt.addEventListener('click', function (event) {
              event.preventDefault();
              commitSuggestion(this.getAttribute('data-tag'));
            });
            suggestBox.appendChild(opt);
          }
          suggestBox.hidden = false;
          suggestBox.classList.add('is-open');
          tagsInput.setAttribute('aria-expanded', 'true');
          renderActive();
        }
        function updateSuggest() {
          if (!suggestBox) { return; }
          if (document.activeElement !== tagsInput) { closeSuggest(); return; }
          var token = normalizeTag(currentToken(tagsInput.value));
          if (!token) { closeSuggest(); return; }
          var query = lower(token);
          var prefix = [];
          var contains = [];
          for (var i = 0; i < known.length; i += 1) {
            var name = known[i];
            var key = lower(name);
            if (key === query) { continue; }        // 正在打的词本身不再提示
            if (key.indexOf(query) === 0) { prefix.push(name); }
            else if (key.indexOf(query) > 0) { contains.push(name); }
          }
          var matched = prefix.concat(contains).slice(0, 8);   // 前缀优先，最多 8 条
          if (!matched.length) { closeSuggest(); return; }
          openSuggest(matched);
        }
        function commitSuggestion(rawName) {
          var name = normalizeTag(rawName);
          if (!name) { return; }
          var value = tagsInput.value;
          var token = currentToken(value);
          var base = parseTags(value.slice(0, value.length - token.length));
          var already = indexOfTag(parseTags(value), name) >= 0;
          if (!already && base.length >= TAG_MAX) {
            flashHint('最多 ' + TAG_MAX + ' 个标签', 'warn');
            closeSuggest();
            return;
          }
          tagsInput.value = parseTags(base.concat([name])).join('，');
          syncState();
          closeSuggest();
          try { tagsInput.setSelectionRange(tagsInput.value.length, tagsInput.value.length); } catch (errSel) { /* 忽略 */ }
          try { tagsInput.focus(); } catch (errFocus) { /* 忽略 */ }
          if (already) { flashHint('「' + name + '」已经在标签里了'); }
          else { flashHint('已加入「' + name + '」'); }
        }
        function syncState() {
          var list = parseTags(tagsInput.value);
          var used = {};
          for (var i = 0; i < list.length; i += 1) { used[lower(list[i])] = true; }
          for (var c = 0; c < chipNodes.length; c += 1) {
            var chip = chipNodes[c];
            var active = !!used[lower(normalizeTag(chip.getAttribute('data-tag')))];
            chip.classList.toggle('is-active', active);
            chip.setAttribute('aria-pressed', active ? 'true' : 'false');
          }
          if (countEl) { countEl.textContent = list.length + ' / ' + TAG_MAX; }
          var raw = splitRawTags(tagsInput.value);
          var rawKeys = {};
          for (var r = 0; r < raw.length; r += 1) { rawKeys[lower(raw[r])] = true; }
          if (!flashTimer) {
            if (Object.keys(rawKeys).length > TAG_MAX) {
              setHint('最多 ' + TAG_MAX + ' 个标签，超出部分保存时会被忽略', 'warn');
            } else if (raw.length > list.length) {
              setHint('有重复标签（大小写不敏感），不会重复加入', 'warn');
            } else {
              setHint('', '');
            }
          }
        }
        function normalizeInput() {
          var before = tagsInput.value;
          var after = parseTags(before).join('，');
          if (after !== before) {
            tagsInput.value = after;
            syncState();
            flashHint('标签已自动整理（去重 / 去 # / 去空白）');
          } else {
            syncState();
          }
        }
        function toggleChip(button) {
          var name = normalizeTag(button.getAttribute('data-tag'));
          if (!name) { return; }
          var list = parseTags(tagsInput.value);
          var at = indexOfTag(list, name);
          if (at >= 0) {
            list.splice(at, 1);
            tagsInput.value = parseTags(list).join('，');
            syncState();
            flashHint('已移除「' + name + '」');
          } else {
            if (list.length >= TAG_MAX) { flashHint('最多 ' + TAG_MAX + ' 个标签', 'warn'); return; }
            list.push(name);
            tagsInput.value = parseTags(list).join('，');
            syncState();
            flashHint('已加入「' + name + '」');
          }
          try { tagsInput.focus(); } catch (errFocus2) { /* 忽略 */ }
        }

        for (var t = 0; t < chipNodes.length; t += 1) {
          chipNodes[t].addEventListener('click', function (event) {
            event.preventDefault();
            toggleChip(this);
          });
        }
        tagsInput.addEventListener('input', function () {
          if (flashTimer) { window.clearTimeout(flashTimer); flashTimer = null; }
          syncState();
          updateSuggest();
        });
        tagsInput.addEventListener('keydown', function (event) {
          var open = !!(suggestBox && !suggestBox.hidden && items.length);
          var key = event.key;
          if (open && key === 'ArrowDown') {
            event.preventDefault();
            activeIndex = (activeIndex + 1) % items.length;
            renderActive();
          } else if (open && key === 'ArrowUp') {
            event.preventDefault();
            activeIndex = (activeIndex - 1 + items.length) % items.length;
            renderActive();
          } else if (open && (key === 'Enter' || key === 'Tab')) {
            event.preventDefault();
            if (activeIndex >= 0 && items[activeIndex]) { commitSuggestion(items[activeIndex]); }
          } else if (key === 'Escape' && open) {
            event.preventDefault();
            closeSuggest();
          }
        });
        tagsInput.addEventListener('blur', function () {
          window.setTimeout(function () { closeSuggest(); normalizeInput(); }, 140);
        });
        if (suggestBox) {
          suggestBox.addEventListener('mousedown', function (event) { event.preventDefault(); });
        }
        form.addEventListener('submit', function () {
          var before = tagsInput.value;
          var after = parseTags(before).join('，');
          if (after !== before) { tagsInput.value = after; }
        });
        if (widget) { widget.setAttribute('data-max-tags', String(TAG_MAX)); }
        syncState();
      } catch (errBoost) {
        if (typeof console !== 'undefined' && console.warn) { console.warn('[InkNote] 标签增强初始化失败', errBoost); }
      }
    })();

  /* ===== 10. 站内跳转 / 取消链接的未保存拦截 ===== */
    document.addEventListener('click', function (event) {
      if (!state.dirty) { return; }
      var target = event.target;
      var link = target && target.nodeType === 1 && target.closest ? target.closest('a[href]') : null;
      if (!link || link.target === '_blank' || link.hasAttribute('download')) { return; }
      var raw = link.getAttribute('href') || '';
      if (!raw || raw.charAt(0) === '#') { return; }
      var url;
      try { url = new URL(link.href, window.location.href); } catch (e) { return; }
      if (url.origin !== window.location.origin) { return; }
      if (!window.confirm(form.dataset.unsavedText || '有未保存的改动')) {
        event.preventDefault();
        event.stopPropagation();
      }
    }, true);
    /* ===== 11. 初始化渲染 ===== */
    renderStats();
    updateCursorInfo();
    if (textarea.value.trim()) { fetchPreview(); } else { showPlaceholder(); }
      /* ===== 12. 内联 AI：接着写 + 选中文本改写（纯增强，出错不影响编辑/保存） ===== */
      (function initAiInline() {
        var aiNodes = [];
        var each = function (list, fn) { Array.prototype.forEach.call(list || [], fn); };
        try {
          var aiContinueBtn = document.getElementById('ai-continue');
          var AI_MODES = [
            { mode: 'polish', label: '润色' },
            { mode: 'concise', label: '精简' },
            { mode: 'expand', label: '扩写' },
            { mode: 'translate', label: '翻译' },
            { mode: 'formal', label: '更正式' }
          ];
          var aiState = { current: null, result: '', pillOpen: false, busy: false };

          
          function aiEnabled() {
            var flag = form.getAttribute('data-ai-enabled');
            return flag === null || flag === '' ? true : flag === '1';
          }
          function aiNotConfigured() { toast('先去设置页配置 AI 服务', 'error'); }

          /* ---- 选中文本时出现在选区上方的浮动药丸 ---- */
          var pill = document.createElement('div');
          pill.className = 'ai-inline-pill';
          pill.setAttribute('role', 'toolbar');
          pill.setAttribute('aria-label', 'AI 改写');
          AI_MODES.forEach(function (item, index) {
            if (index > 0) {
              var dot = document.createElement('span');
              dot.className = 'ai-inline-pill__dot';
              dot.setAttribute('aria-hidden', 'true');
              dot.textContent = '·';
              pill.appendChild(dot);
            }
            var modeBtn = document.createElement('button');
            modeBtn.type = 'button';
            modeBtn.className = 'ai-inline-pill__btn';
            modeBtn.setAttribute('data-ai-mode', item.mode);
            modeBtn.textContent = item.label;
            modeBtn.addEventListener('mousedown', function (event) { event.preventDefault(); });
            modeBtn.addEventListener('click', function (event) {
              event.preventDefault();
              startRewrite(item.mode, item.label);
            });
            pill.appendChild(modeBtn);
          });
          document.body.appendChild(pill);
          aiNodes.push(pill);

          function hidePill() {
            aiState.pillOpen = false;
            pill.classList.remove('is-open');
          }

          // 用镜像 div 估算光标在 textarea 里的视口坐标；滚动时直接隐藏药丸。
          function caretCoords(index) {
            var rect = textarea.getBoundingClientRect();
            var fallback = { top: rect.top + 4, left: rect.left + 12 };
            try {
              var computed = window.getComputedStyle(textarea);
              var mirror = document.createElement('div');
              var props = ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'lineHeight',
                'letterSpacing', 'wordSpacing', 'textTransform', 'textIndent',
                'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
                'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
                'boxSizing', 'width', 'textAlign', 'tabSize'];
              mirror.style.position = 'absolute';
              mirror.style.top = '0';
              mirror.style.left = '0';
              mirror.style.visibility = 'hidden';
              mirror.style.pointerEvents = 'none';
              mirror.style.whiteSpace = 'pre-wrap';
              mirror.style.overflowWrap = 'break-word';
              mirror.style.wordWrap = 'break-word';
              for (var i = 0; i < props.length; i += 1) {
                try { mirror.style[props[i]] = computed[props[i]]; } catch (e) { /* 个别属性忽略 */ }
              }
              mirror.textContent = textarea.value.slice(0, index);
              var marker = document.createElement('span');
              marker.textContent = textarea.value.slice(index) || '.';
              mirror.appendChild(marker);
              document.body.appendChild(mirror);
              var lineHeight = parseFloat(computed.lineHeight) || (parseFloat(computed.fontSize) * 1.5) || 20;
              var top = marker.offsetTop + lineHeight - textarea.scrollTop;
              var left = marker.offsetLeft - textarea.scrollLeft;
              document.body.removeChild(mirror);
              return { top: rect.top + top, left: rect.left + left };
            } catch (err) {
              return fallback;
            }
          }

          function positionPill() {
            var sel = currentSelection();
            if (sel.start === sel.end || !sel.text.trim()) { hidePill(); return; }
            var coords = caretCoords(sel.end);
            pill.classList.add('is-open');
            aiState.pillOpen = true;
            var width = pill.offsetWidth || 260;
            var height = pill.offsetHeight || 34;
            var left = Math.max(8, Math.min(coords.left - width / 2, window.innerWidth - width - 8));
            var top = coords.top - height - 8;
            if (top < 8) { top = coords.top + 22; }
            pill.style.left = Math.round(left) + 'px';
            pill.style.top = Math.round(top) + 'px';
          }

          function maybeShowPill() {
            if (document.activeElement !== textarea) { return; }
            var sel = currentSelection();
            if (sel.start !== sel.end && sel.text.trim()) { positionPill(); } else { hidePill(); }
          }

          textarea.addEventListener('mouseup', function () { window.setTimeout(maybeShowPill, 0); });
          textarea.addEventListener('keyup', maybeShowPill);
          textarea.addEventListener('scroll', hidePill);
          textarea.addEventListener('blur', hidePill);
          document.addEventListener('selectionchange', function () {
            if (document.activeElement === textarea) { maybeShowPill(); }
          });
          window.addEventListener('scroll', hidePill, true);
          window.addEventListener('resize', function () { if (aiState.pillOpen) { positionPill(); } });

          /* ---- 结果预览面板：先预览，点了按钮才动正文 ---- */
          var panel = document.createElement('div');
          panel.className = 'ai-inline-panel';
          panel.setAttribute('role', 'dialog');
          panel.setAttribute('aria-label', 'AI 结果预览');
          panel.innerHTML =
            '<div class="ai-inline-panel__head">' +
              '<span class="ai-inline-panel__title" data-ai-title>AI 结果</span>' +
              '<button type="button" class="ai-inline-panel__close" data-ai-action="close" aria-label="关闭">×</button>' +
            '</div>' +
            '<div class="ai-inline-panel__body" data-ai-body></div>' +
            '<div class="ai-inline-panel__actions">' +
              '<button type="button" class="ai-inline-panel__btn ai-inline-panel__btn--primary" data-ai-action="insert">插入到光标处</button>' +
              '<button type="button" class="ai-inline-panel__btn" data-ai-action="replace">替换选中</button>' +
              '<button type="button" class="ai-inline-panel__btn" data-ai-action="retry">重试</button>' +
              '<button type="button" class="ai-inline-panel__btn" data-ai-action="close">关闭</button>' +
            '</div>';
          document.body.appendChild(panel);
          aiNodes.push(panel);

          var panelBody = panel.querySelector('[data-ai-body]');
          var panelTitle = panel.querySelector('[data-ai-title]');
          var panelReplaceBtn = panel.querySelector('[data-ai-action="replace"]');

          function hidePanel() { panel.classList.remove('is-open'); }

          function positionPanel(anchor) {
            panel.classList.add('is-open');
            var target = anchor || textarea.getBoundingClientRect();
            var width = panel.offsetWidth || 400;
            var height = panel.offsetHeight || 240;
            var left = Math.max(12, Math.min(target.left, window.innerWidth - width - 12));
            var top = target.bottom + 10;
            if (top + height > window.innerHeight - 12) { top = Math.max(12, target.top - height - 10); }
            panel.style.left = Math.round(left) + 'px';
            panel.style.top = Math.round(top) + 'px';
          }

          function showPanel(text, ctx) {
            aiState.result = text;
            panelBody.textContent = text;
            panelTitle.textContent = ((ctx && ctx.label) || 'AI') + ' · 预览（未改动正文）';
            panelReplaceBtn.hidden = !(ctx && ctx.target && typeof ctx.target.replaceFrom === 'number');
            positionPanel(aiState.pillOpen ? pill.getBoundingClientRect() : null);
          }

          function showPanelLoading(ctx) {
            aiState.result = '';
            panelBody.innerHTML = '<span class="ai-inline-spinner"></span>正在生成…';
            panelTitle.textContent = ((ctx && ctx.label) || 'AI') + ' · 生成中';
            panelReplaceBtn.hidden = true;
            positionPanel(aiState.pillOpen ? pill.getBoundingClientRect() : null);
          }

          function setPanelBusy(busy) {
            each(panel.querySelectorAll('[data-ai-action]'), function (button) {
              if (button === panelReplaceBtn && panelReplaceBtn.hidden) { button.disabled = true; return; }
              button.disabled = !!busy;
            });
          }

          panel.addEventListener('mousedown', function (event) { event.preventDefault(); });
          panel.addEventListener('click', function (event) {
            var target = event.target;
            var button = target && target.nodeType === 1 && target.closest ? target.closest('[data-ai-action]') : null;
            if (!button) { return; }
            event.preventDefault();
            var action = button.getAttribute('data-ai-action');
            if (action === 'insert') { insertResult(); }
            else if (action === 'replace') { replaceResult(); }
            else if (action === 'retry') { if (aiState.current) { runRequest(aiState.current); } }
            else if (action === 'close') { hidePanel(); }
          });

          function setButtonBusy(button, busy) {
            if (!button) { return; }
            if (busy) {
              if (!button._aiIdleHtml) { button._aiIdleHtml = button.innerHTML || button.textContent || ''; }
              button.classList.add('is-busy');
              button.disabled = true;
              button.innerHTML = '<span class="ai-inline-spinner" aria-hidden="true"></span>生成中…';
            } else {
              button.classList.remove('is-busy');
              button.disabled = false;
              if (button._aiIdleHtml) { button.innerHTML = button._aiIdleHtml; }
            }
          }

          function setPillBusy(busy) {
            each(pill.querySelectorAll('button'), function (button) { button.disabled = !!busy; });
          }

          function insertResult() {
            var text = aiState.result;
            var ctx = aiState.current;
            if (!text || !ctx || !ctx.target) { return; }
            var value = textarea.value;
            var at = Math.max(0, Math.min(ctx.target.insertAt, value.length));
            var next = value.slice(0, at) + text + value.slice(at);
            setValueAndSelection(next, at + text.length, at + text.length);
            hidePanel();
            toast('已插入 AI 结果', 'ok');
          }

          function replaceResult() {
            var text = aiState.result;
            var ctx = aiState.current;
            if (!text || !ctx || !ctx.target || typeof ctx.target.replaceFrom !== 'number') { return; }
            var value = textarea.value;
            var from = Math.max(0, Math.min(ctx.target.replaceFrom, value.length));
            var to = Math.max(from, Math.min(ctx.target.replaceTo, value.length));
            var next = value.slice(0, from) + text + value.slice(to);
            setValueAndSelection(next, from + text.length, from + text.length);
            hidePanel();
            toast('已替换选中文本', 'ok');
          }

          function runRequest(ctx) {
            if (!aiEnabled()) { aiNotConfigured(); return; }
            if (aiState.busy) { return; }
            aiState.busy = true;
            aiState.current = ctx;
            if (ctx.button) { setButtonBusy(ctx.button, true); } else { setPillBusy(true); }
            showPanelLoading(ctx);
            setPanelBusy(true);
            requestJSON(ctx.url, { method: 'POST', json: ctx.payload })
              .then(function (data) {
                var text = data && typeof data.text === 'string' ? data.text : '';
                if (!text.trim()) { throw makeApiError('AI 没有返回内容，请重试', data, 0); }
                showPanel(text, ctx);
              })
              .catch(function (err) {
                hidePanel();
                toast((err && err.message) || 'AI 请求失败，请稍后再试', 'error');
              })
              .then(function () {
                aiState.busy = false;
                if (ctx.button) { setButtonBusy(ctx.button, false); }
                setPillBusy(false);
                setPanelBusy(false);
              });
          }

          function startContinue() {
            if (!aiEnabled()) { aiNotConfigured(); return; }
            var content = textarea.value;
            if (!content.trim()) { toast('正文还是空的，先写点内容吧', 'error'); return; }
            var sel = currentSelection();
            runRequest({
              kind: 'continue',
              label: '接着写',
              url: form.dataset.aiContinueUrl || '/api/ai/continue',
              button: aiContinueBtn,
              payload: { title: titleInput ? titleInput.value : '', content: content, instruction: '' },
              target: { insertAt: sel.start, replaceFrom: null, replaceTo: null }
            });
          }

          function startRewrite(mode, label) {
            if (!aiEnabled()) { aiNotConfigured(); hidePill(); return; }
            var sel = currentSelection();
            if (!sel.text.trim()) { toast('先选中要改写的文字', 'error'); hidePill(); return; }
            hidePill();
            runRequest({
              kind: 'rewrite',
              label: label,
              url: form.dataset.aiRewriteUrl || '/api/ai/rewrite',
              button: null,
              payload: { text: sel.text, mode: mode },
              target: { insertAt: sel.end, replaceFrom: sel.start, replaceTo: sel.end }
            });
          }

          if (aiContinueBtn) {
            aiContinueBtn.addEventListener('mousedown', function (event) { event.preventDefault(); });
            aiContinueBtn.addEventListener('click', function (event) { event.preventDefault(); startContinue(); });
          }
          document.addEventListener('keydown', function (event) {
            if ((event.ctrlKey || event.metaKey) && (event.key === 'j' || event.key === 'J')) {
              event.preventDefault();
              startContinue();
            }
          }, true);
        } catch (err) {
          each(aiNodes, function (node) { if (node.parentNode) { node.parentNode.removeChild(node); } });
          if (typeof console !== 'undefined' && console.warn) { console.warn('[InkNote] 内联 AI 初始化失败', err); }
        }
      })();
  }
  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', boot); }
  else { boot(); }
})();
