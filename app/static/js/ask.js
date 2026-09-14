/*!
 * 墨痕 InkNote · ask.js
 * 「问笔记」页面的多轮追问 + 流式回答 + 存成笔记。
 * 普通脚本（IIFE），不依赖任何框架；没有 JS 时表单 POST /ask 照样能用。
 *
 * 与后端的约定：
 *   POST /ask/stream  SSE：data: {"sources":[...],"engine":"keyword"}
 *                          data: {"delta":"..."}            （可多次）
 *                          data: {"done":true,"conversation_id":N,"message_id":M}
 *                          data: {"error":"人话"}
 *   POST /ask/save    表单（_csrf / conversation_id / message_id）
 */
(function () {
  'use strict';

  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn, { once: true });
    else fn();
  }

  // CSRF：优先用 app.js 暴露的工具，取不到再读 <meta>，最后给空串（服务端会 403）
  function csrfToken() {
    try {
      if (window.InkNote && typeof window.InkNote.csrf === 'function') {
        var value = window.InkNote.csrf();
        if (value) return value;
      }
    } catch (error) { /* 忽略，走下面的兜底 */ }
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
    if (kind === 'error' && window.console) window.console.warn('[问笔记]', message);
  }

  ready(function () {
    var form = document.getElementById('ask-form');
    var input = document.getElementById('ask-input');
    var statusEl = document.getElementById('ask-status');

    // 行内状态提示（空填警告等）：替代原生校验气泡
    function setAskStatus(text, kind) {
      if (!statusEl) { return; }
      statusEl.textContent = text || '';
      statusEl.className = 'ai-status' + (kind ? ' ai-status--' + kind : '');
      if (text) {
        clearTimeout(statusEl._timer);
        statusEl._timer = setTimeout(function () {
          statusEl.textContent = '';
          statusEl.className = 'ai-status';
        }, 3500);
      }
    }
    var cidInput = document.getElementById('ask-conversation-id');
    var thread = document.getElementById('ask-thread');
    var sendBtn = document.getElementById('ask-send');
    if (!form || !input || !thread) return;

    var streamUrl = form.getAttribute('data-stream-url') || '/ask/stream';
    var saveUrl = form.getAttribute('data-save-url') || '/ask/save';
    var aiReady = form.getAttribute('data-ai-ready') === '1';

    function currentCid() {
      return cidInput ? (cidInput.value || '') : '';
    }

    function setBusy(busy) {
      if (sendBtn) sendBtn.disabled = !!busy;
      form.classList.toggle('is-busy', !!busy);
    }

    function scrollToBottom() {
      thread.scrollTop = thread.scrollHeight;
    }

    function removeEmptyState() {
      var empty = thread.querySelector('.empty-state');
      if (empty && empty.parentNode) empty.parentNode.removeChild(empty);
    }

    function autosize() {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight || 54, 200) + 'px';
    }

    function addUserBubble(text) {
      var bubble = document.createElement('div');
      bubble.className = 'ask-bubble ask-bubble--user';
      var role = document.createElement('div');
      role.className = 'ask-bubble__role';
      role.textContent = '你';
      var body = document.createElement('div');
      body.className = 'ask-bubble__body';
      body.textContent = text;
      bubble.appendChild(role);
      bubble.appendChild(body);
      thread.appendChild(bubble);
      scrollToBottom();
    }

    function addAiBubble() {
      var bubble = document.createElement('div');
      bubble.className = 'ask-bubble ask-bubble--ai is-typing';
      var role = document.createElement('div');
      role.className = 'ask-bubble__role';
      role.textContent = 'AI 回答';
      var body = document.createElement('div');
      body.className = 'ask-bubble__body ask-answer prose ask-stream';
      var cursor = document.createElement('span');
      cursor.className = 'ask-cursor';
      cursor.hidden = true;   // 首个字符到达前先显示骨架
      body.appendChild(cursor);
      // 骨架占位：检索与首 token 之间的等待不再是一片空白
      var skeletons = [];
      ['w80', 'w60', 'w80'].forEach(function (w) {
        var bar = document.createElement('div');
        bar.className = 'skeleton skeleton--text ' + w;
        body.appendChild(bar);
        skeletons.push(bar);
      });
      bubble.appendChild(role);
      bubble.appendChild(body);
      thread.appendChild(bubble);
      scrollToBottom();
      return {
        bubble: bubble, role: role, body: body, cursor: cursor,
        clearSkeletons: function () {
          skeletons.forEach(function (bar) { bar.remove(); });
          skeletons = [];
          cursor.hidden = false;
        }
      };
    }

    function renderSources(node, sources) {
      if (!sources || !sources.length) return;
      var box = document.createElement('div');
      box.className = 'ask-sources';
      var title = document.createElement('div');
      title.className = 'ask-sources__title';
      title.textContent = '引用到的笔记（' + sources.length + '）';
      box.appendChild(title);
      var list = document.createElement('div');
      list.className = 'ask-source-list';
      sources.forEach(function (item) {
        var card = document.createElement('a');
        card.className = 'ask-source-card';
        card.setAttribute('href', item && item.url ? String(item.url) : '#');
        var name = document.createElement('span');
        name.className = 'ask-source-card__title';
        name.textContent = item && item.title ? String(item.title) : '无标题';
        card.appendChild(name);
        if (item && item.snippet) {
          var snippet = document.createElement('span');
          snippet.className = 'ask-source-card__snippet';
          snippet.textContent = String(item.snippet);
          card.appendChild(snippet);
        }
        list.appendChild(card);
      });
      box.appendChild(list);
      node.bubble.appendChild(box);
    }

    function renderEngine(node, engine) {
      if (!engine) return;
      var badge = document.createElement('span');
      badge.className = 'ask-engine ask-engine--' + (engine === 'semantic' ? 'semantic' : 'keyword');
      badge.textContent = engine === 'semantic' ? '语义检索' : '关键词检索';
      node.role.appendChild(badge);
    }

    function renderSaveButton(node, messageId, conversationId) {
      if (!messageId) return;
      var actions = document.createElement('div');
      actions.className = 'ask-actions';
      var saveForm = document.createElement('form');
      saveForm.method = 'post';
      saveForm.action = saveUrl;
      [
        ['_csrf', csrfToken()],
        ['conversation_id', String(conversationId || '')],
        ['message_id', String(messageId)]
      ].forEach(function (pair) {
        var hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = pair[0];
        hidden.value = pair[1];
        saveForm.appendChild(hidden);
      });
      var btn = document.createElement('button');
      btn.className = 'btn btn--ghost btn--sm';
      btn.type = 'submit';
      btn.textContent = '存成笔记';
      saveForm.appendChild(btn);
      actions.appendChild(saveForm);
      node.bubble.appendChild(actions);
    }

    // 解析一个 SSE 事件块（可能有多个 data: 行），边到边渲染
    function handleEvent(block, state) {
      var payloadRaw = '';
      block.split('\n').forEach(function (line) {
        if (line.indexOf('data:') === 0) {
          if (payloadRaw) payloadRaw += '\n';
          payloadRaw += line.slice(5).replace(/^\s+/, '');
        }
      });
      if (!payloadRaw) return;
      var payload;
      try { payload = JSON.parse(payloadRaw); } catch (error) { return; }
      var node = state.node;
      if (payload.sources) {
        state.sources = payload.sources;
        renderSources(node, payload.sources);
        renderEngine(node, payload.engine || '');
      }
      if (typeof payload.delta === 'string') {
        state.answer += payload.delta;
        if (node.clearSkeletons) { node.clearSkeletons(); node.clearSkeletons = null; }
        if (node.cursor) node.cursor.insertAdjacentText('beforebegin', payload.delta);
        scrollToBottom();
      }
      if (payload.error) state.error = String(payload.error);
      if (payload.done) {
        state.done = true;
        state.donePayload = payload;
      }
    }

    // 用 fetch + ReadableStream 读 SSE（EventSource 不能 POST，所以不用它）
    function streamAnswer(question, node) {
      var body = { question: question };
      var cid = currentCid();
      if (cid) body.conversation_id = cid;
      var headers = { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' };
      var token = csrfToken();
      if (token) headers['X-CSRF-Token'] = token;

      var state = { node: node, answer: '', error: '', done: false, donePayload: null };

      return fetch(streamUrl, {
        method: 'POST',
        headers: headers,
        credentials: 'same-origin',
        body: JSON.stringify(body)
      }).then(function (response) {
        if (!response.ok || !response.body) throw new Error('流式接口不可用（HTTP ' + response.status + '）');
        var reader = response.body.getReader();
        var decoder = new TextDecoder('utf-8');
        var buffer = '';
        function pump() {
          return reader.read().then(function (result) {
            if (result.done) {
              if (buffer.trim()) handleEvent(buffer, state);
              return;
            }
            buffer += decoder.decode(result.value, { stream: true });
            var parts = buffer.split('\n\n');
            buffer = parts.pop();
            for (var i = 0; i < parts.length; i += 1) handleEvent(parts[i], state);
            return pump();
          });
        }
        return pump();
      }).then(function () {
        if (state.error) throw new Error(state.error);
        if (!state.done) throw new Error('回答中断了，请重试');
        return state;
      });
    }

    // 流成功收尾：去掉光标、把会话 id 写回地址栏、显示「存成笔记」
    function finishNode(node, state) {
      node.bubble.classList.remove('is-typing');
      if (node.cursor && node.cursor.parentNode) node.cursor.parentNode.removeChild(node.cursor);
      if (!state.donePayload) return;
      var cid = state.donePayload.conversation_id;
      if (cid) {
        if (cidInput) cidInput.value = String(cid);
        try {
          var url = new URL(window.location.href);
          url.searchParams.set('c', String(cid));
          window.history.replaceState(null, '', url.toString());
        } catch (error) { /* 老浏览器不支持就算了 */ }
      }
      renderSaveButton(node, state.donePayload.message_id, cid || currentCid());
    }

    // 流式失败：回退到普通表单提交（服务端会重新检索并作答、303 回会话页）
    function fallbackSubmit(question) {
      input.value = question;
      form.submit(); // 直接 submit() 不会再触发 submit 事件，避免死循环
    }

    form.addEventListener('submit', function (event) {
      if (!aiReady) return; // 没配 AI：交给普通表单，服务端渲染检索结果
      event.preventDefault();
      var question = (input.value || '').trim();
      if (!question) {
        // 空填：行内琥珀提示（原来靠 required 的浏览器气泡，又丑又慢还不出现在移动端）
        setAskStatus('先输入你的问题', 'warn');
        input.focus();
        return;
      }
      setAskStatus('');
      input.value = '';
      autosize();
      setBusy(true);
      removeEmptyState();
      addUserBubble(question);
      var node = addAiBubble();
      streamAnswer(question, node)
        .then(function (state) {
          finishNode(node, state);
          setBusy(false);
          input.focus();
        })
        .catch(function (error) {
          setBusy(false);
          toast(error && error.message ? error.message : '流式回答失败', 'error');
          fallbackSubmit(question);
        });
    });

    // Enter 发送、Shift+Enter 换行（中文输入法组词时不误触）
    input.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
      event.preventDefault();
      if (typeof form.requestSubmit === 'function') {
        form.requestSubmit();
      } else {
        var evt;
        try {
          evt = new Event('submit', { cancelable: true, bubbles: true });
        } catch (error) {
          evt = document.createEvent('Event');
          evt.initEvent('submit', true, true);
        }
        form.dispatchEvent(evt);
      }
    });

    input.addEventListener('input', autosize);
    autosize();
  });
})();