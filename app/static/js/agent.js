/* 笔记助手：textarea 输入任务 → POST /api/agent/run → 展示步骤与结果。
 * 只做展示层，所有决策在服务端的 agent 循环里。 */
(function () {
  'use strict';

  function ready(fn) {
    if (document.readyState !== 'loading') { fn(); }
    else { document.addEventListener('DOMContentLoaded', fn); }
  }

  ready(function () {
    var form = document.getElementById('agent-form');
    var taskInput = document.getElementById('agent-task');
    var runBtn = document.getElementById('agent-run');
    var statusEl = document.getElementById('agent-status');
    var resultEl = document.getElementById('agent-result');
    var stepsEl = document.getElementById('agent-steps');
    var answerEl = document.getElementById('agent-answer');
    if (!form || !taskInput || !runBtn) { return; }

    // ===== 会话记忆：本轮会话的任务/回答存 localStorage，追问时随请求带给模型 =====
    var HISTORY_KEY = 'inknote.agent.history';
    var history = [];
    try {
      var savedHistory = JSON.parse(window.localStorage.getItem(HISTORY_KEY) || '[]');
      if (Array.isArray(savedHistory)) { history = savedHistory; }
    } catch (err) { history = []; }

    var historySection = document.getElementById('agent-history');
    var historyList = document.getElementById('agent-history-list');
    var historyClear = document.getElementById('agent-history-clear');

    function renderHistory() {
      if (!historySection || !historyList) { return; }
      historySection.hidden = !history.length;
      historyList.textContent = '';
      history.forEach(function (turn) {
        var li = document.createElement('li');
        li.className = 'agent-history__item';
        var q = document.createElement('div');
        q.className = 'agent-history__task';
        q.textContent = '任务：' + turn.task;
        var a = document.createElement('div');
        a.className = 'agent-history__answer';
        a.textContent = turn.answer;
        li.appendChild(q);
        li.appendChild(a);
        historyList.appendChild(li);
      });
    }

    function pushHistory(taskText, answerText) {
      history.push({ task: taskText, answer: answerText });
      if (history.length > 5) { history = history.slice(-5); }
      try { window.localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); }
      catch (err) { /* 无痕模式就算了 */ }
      renderHistory();
    }

    if (historyClear) {
      historyClear.addEventListener('click', function () {
        history = [];
        window.localStorage.removeItem(HISTORY_KEY);
        renderHistory();
      });
    }
    renderHistory();

    // ===== 历史任务（落库审计）：加载、渲染、清空 =====
    var runsSection = document.getElementById('agent-runs');
    var runsList = document.getElementById('agent-runs-list');
    var runsClear = document.getElementById('agent-runs-clear');

    function renderRuns(runs) {
      if (!runsSection || !runsList) { return; }
      runsSection.hidden = !runs.length;
      var count = document.getElementById('agent-runs-count');
      if (count) { count.textContent = runs.length ? String(runs.length) : ''; }
      runsList.textContent = '';
      runs.forEach(function (run) {
        var li = document.createElement('li');
        li.className = 'agent-runs__item' + (run.ok ? '' : ' agent-runs__item--fail');
        // 完整步骤链放进 title（悬停可看），行内只留单行摘要 —— 原来整条工具链
        // 平铺在页面上，8 条记录就占了半屏
        var stepBits = (run.steps || []).map(function (s) { return s.summary || s.tool; }).join(' → ');
        li.title = (run.ok ? '成功' : '失败：' + (run.error || '未知')) +
                   (stepBits ? '\n步骤：' + stepBits : '');
        var at = document.createElement('time');
        at.className = 'agent-runs__at';
        at.textContent = String(run.at || '').slice(5, 16).replace('T', ' ');
        var task = document.createElement('span');
        task.className = 'agent-runs__head';
        task.textContent = (run.read_only ? '[只读] ' : '') + run.task;
        var mark = document.createElement('span');
        mark.className = 'agent-runs__mark';
        mark.textContent = run.ok ? '✓' : '✗';
        li.appendChild(at);
        li.appendChild(task);
        li.appendChild(mark);
        runsList.appendChild(li);
      });
    }

    function loadRuns() {
      fetch('/api/agent/runs', { credentials: 'same-origin' })
        .then(function (res) { return res.json(); })
        .then(function (data) { if (data && data.ok) { renderRuns(data.runs || []); } })
        .catch(function () { /* 静默 */ });
    }
    loadRuns();

    if (runsClear) {
      runsClear.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();  // 在 summary 里，别把折叠面板一起点了
        var csrfMeta = document.querySelector('meta[name="csrf-token"]');
        fetch('/api/agent/runs/clear', {
          method: 'POST',
          headers: { 'X-CSRF-Token': csrfMeta ? csrfMeta.getAttribute('content') : '' },
          credentials: 'same-origin'
        }).then(function () { renderRuns([]); }).catch(function () { /* 静默 */ });
      });
    }

    var pendingEl = null;   // 进行中提示 <li>，收到事件时移除
    var stepCount = 0;      // 已展示的步骤数

    function setStatus(text, kind) {
      if (!statusEl) { return; }
      statusEl.textContent = text || '';
      statusEl.className = 'ai-status' + (kind ? ' ai-status--' + kind : '');
    }

    // 示例任务：点一下填进输入框
    document.querySelectorAll('.agent-example').forEach(function (btn) {
      btn.addEventListener('click', function () {
        taskInput.value = btn.getAttribute('data-task') || '';
        taskInput.focus();
      });
    });

    function renderStep(item, index) {
      var li = document.createElement('li');
      li.className = 'agent-steps__item';
      var head = document.createElement('span');
      head.className = 'agent-steps__tool';
      head.textContent = (index + 1) + '. ' + (item.summary || item.tool || '');
      li.appendChild(head);
      if (item.tool) {
        var detail = document.createElement('code');
        detail.className = 'agent-steps__detail';
        try { detail.textContent = JSON.stringify(item.params || {}); }
        catch (err) { detail.textContent = ''; }
        li.appendChild(detail);
      }
      return li;
    }

    function renderNotes(notes) {
      var box = document.getElementById('agent-notes');
      if (!box) { return; }
      box.textContent = '';
      var list = (notes || []).filter(function (n) { return n && n.id; });
      if (!list.length) { box.hidden = true; return; }
      box.hidden = false;
      var head = document.createElement('span');
      head.className = 'agent-notes__head';
      head.textContent = '涉及的笔记：';
      box.appendChild(head);
      list.forEach(function (note) {
        var a = document.createElement('a');
        a.className = 'agent-notes__chip';
        a.href = '/notes/' + note.id;
        a.textContent = note.title || ('#' + note.id);
        box.appendChild(a);
      });
    }

    function render(result) {
      if (!resultEl || !stepsEl || !answerEl) { return; }
      resultEl.hidden = false;
      stepsEl.textContent = '';
      (result.steps || []).forEach(function (item, index) {
        stepsEl.appendChild(renderStep(item, index));
      });
      answerEl.textContent = result.answer || result.error || '';
      answerEl.className = 'agent-answer' + (result.ok ? '' : ' agent-answer--error');
      renderNotes(result.notes);
    }

    function showPending() {
      if (!stepsEl || pendingEl) { return; }
      pendingEl = document.createElement('li');
      pendingEl.className = 'agent-steps__pending';
      pendingEl.textContent = '正在思考下一步…';
      // 骨架占位：比一行小字更能传达「正在干活」
      ['w80', 'w60'].forEach(function (w) {
        var bar = document.createElement('div');
        bar.className = 'skeleton skeleton--text ' + w;
        pendingEl.appendChild(bar);
      });
      stepsEl.appendChild(pendingEl);
    }

    function removePending() {
      if (pendingEl && pendingEl.parentNode) { pendingEl.parentNode.removeChild(pendingEl); }
      pendingEl = null;
    }

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var task = String(taskInput.value || '').trim();
      if (!task) { setStatus('先写一句你想让它做的事', 'warn'); taskInput.focus(); return; }

      var csrfMeta = document.querySelector('meta[name="csrf-token"]');
      var csrf = csrfMeta ? csrfMeta.getAttribute('content') : '';
      runBtn.disabled = true;
      setStatus('正在执行（每完成一步都会即时显示）…', '');

      // 清掉上一次的结果
      pendingEl = null;
      stepCount = 0;
      if (resultEl) { resultEl.hidden = false; }
      if (stepsEl) { stepsEl.textContent = ''; }
      if (answerEl) { answerEl.textContent = ''; answerEl.className = 'agent-answer'; }

      function onStep(item) {
        removePending();
        var li = renderStep(item, stepCount);
        if (stepsEl) { stepsEl.appendChild(li); }
        stepCount += 1;
        setStatus('第 ' + stepCount + ' 步：' + (item.summary || item.tool || ''), '');
        renderNotes(item.notes);
      }

      function onFinal(ev) {
        removePending();
        setStatus(ev.ok ? '完成' : (ev.error || '没有完成'), ev.ok ? 'ok' : 'warn');
        if (answerEl) {
          answerEl.textContent = ev.answer || ev.error || '';
          answerEl.className = 'agent-answer' + (ev.ok ? '' : ' agent-answer--error');
        }
        if (ev.ok && ev.answer) { pushHistory(task, ev.answer); }
        loadRuns();
      }

      var readonlyBox = document.getElementById('agent-readonly');
      fetch(form.getAttribute('data-run-url') || '/api/agent/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrf
        },
        credentials: 'same-origin',
        body: JSON.stringify({
          task: task,
          read_only: !!(readonlyBox && readonlyBox.checked),
          history: history.reduce(function (acc, turn) {
            acc.push({ role: 'user', content: turn.task });
            acc.push({ role: 'assistant', content: turn.answer });
            return acc;
          }, [])
        })
      }).then(function (res) {
        if (!res.ok) {
          runBtn.disabled = false;
          removePending();
          setStatus('请求失败（HTTP ' + res.status + '），请重试', 'warn');
          return;
        }
        if (!res.body || !res.body.getReader) {
          runBtn.disabled = false;
          removePending();
          setStatus('你的浏览器不支持流式读取，已降级', 'warn');
          return;
        }
        showPending();
        var reader = res.body.getReader();
        var decoder = new TextDecoder('utf-8');
        var buffer = '';

        function handleLine(line) {
          if (line.indexOf('data:') !== 0) { return; }
          var payload = line.slice(5).replace(/^ /, '');
          if (!payload) { return; }
          var ev;
          try { ev = JSON.parse(payload); }
          catch (e) { return; } // 容错坏行：跳过无法解析的事件
          if (ev.type === 'step') {
            onStep({ tool: ev.tool, summary: ev.summary, params: ev.params });
          } else if (ev.type === 'final') {
            onFinal(ev);
          } else if (ev.type === 'error') {
            removePending();
            setStatus(ev.error || '出错了', 'warn');
            if (answerEl) { answerEl.textContent = ev.error || ''; answerEl.className = 'agent-answer agent-answer--error'; }
          }
        }

        function processBuffer() {
          var sep;
          while ((sep = buffer.indexOf('\n\n')) >= 0) {
            var rawEvent = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            rawEvent.split('\n').forEach(handleLine);
          }
        }

        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) {
              processBuffer(); // 处理可能残留的最后一段
              removePending();
              runBtn.disabled = false;
              return;
            }
            buffer += decoder.decode(chunk.value, { stream: true });
            processBuffer();
            return pump();
          }).catch(function (err) {
            runBtn.disabled = false;
            removePending();
            setStatus('读取失败：' + (err && err.message ? err.message : '网络错误'), 'warn');
          });
        }
        pump();
      }).catch(function (err) {
        runBtn.disabled = false;
        removePending();
        setStatus('请求失败：' + (err && err.message ? err.message : '网络错误'), 'warn');
      });
    });
  });
})();
