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
    var cancelBtn = document.getElementById('agent-cancel');
    var statusEl = document.getElementById('agent-status');
    var resultEl = document.getElementById('agent-result');
    var stepsEl = document.getElementById('agent-steps');
    var answerEl = document.getElementById('agent-answer');
    if (!form || !taskInput || !runBtn) { return; }
    var currentRunId = '';   // 本次任务的 run_id（响应头 X-Run-Id），取消用

    function hideCancel() {
      currentRunId = '';
      if (cancelBtn) { cancelBtn.hidden = true; }
    }

    if (cancelBtn) {
      cancelBtn.addEventListener('click', function () {
        if (!currentRunId) { return; }
        cancelBtn.disabled = true;
        setStatus('正在取消（当前步骤完成后停止）…', 'warn');
        var csrfMeta = document.querySelector('meta[name="csrf-token"]');
        fetch('/api/agent/cancel', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': csrfMeta ? csrfMeta.getAttribute('content') : ''
          },
          credentials: 'same-origin',
          body: JSON.stringify({ run_id: currentRunId })
        }).catch(function () { /* 就算请求失败，final 事件也会把状态收尾 */ });
      });
    }

    // ===== 编辑页「让助手处理这篇」入口：/agent?note=ID&title=标题 预填任务上下文 =====
    try {
      var params = new URLSearchParams(window.location.search);
      var preNote = params.get('note');
      if (preNote && /^\d+$/.test(preNote) && !taskInput.value) {
        var preTitle = params.get('title') || ('#' + preNote);
        taskInput.value = '我想调整《' + preTitle + '》（笔记 id：' + preNote + '）。我的要求：';
        taskInput.focus();
      }
    } catch (err) { /* 没有 URLSearchParams 的老浏览器直接跳过 */ }

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
      var count = document.getElementById('agent-history-count');
      if (count) { count.textContent = history.length ? String(history.length) : ''; }
      historyList.textContent = '';
      history.forEach(function (turn) {
        var li = document.createElement('li');
        li.className = 'agent-history__item';
        // 气泡式区分：任务靠右（品牌底色），回答靠左（描边底色），并标注角色
        var q = document.createElement('div');
        q.className = 'agent-history__task';
        var qRole = document.createElement('span');
        qRole.className = 'agent-history__role';
        qRole.textContent = '我';
        q.appendChild(qRole);
        q.appendChild(document.createTextNode(turn.task));
        var a = document.createElement('div');
        a.className = 'agent-history__answer';
        var aRole = document.createElement('span');
        aRole.className = 'agent-history__role';
        aRole.textContent = '助手';
        a.appendChild(aRole);
        a.appendChild(document.createTextNode(turn.answer));
        li.appendChild(q);
        li.appendChild(a);
        historyList.appendChild(li);
      });
    }

    function pushHistory(taskText, answerText) {
      var wasEmpty = !history.length;
      history.push({ task: taskText, answer: answerText });
      if (history.length > 5) { history = history.slice(-5); }
      try { window.localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); }
      catch (err) { /* 无痕模式就算了 */ }
      renderHistory();
      // 第一轮进来时自动展开一次，让用户知道这东西在记；之后由用户自己收放
      if (wasEmpty) { historySection.open = true; }
    }

    if (historyClear) {
      historyClear.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();  // 在 summary 里，别把折叠面板一起点了
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
        // 详情走全站自绘悬浮面板（chart-tip.js）；列表本身是 JS 渲染的，
        // 没有「无 JS 兜底」一说，所以不留原生 title（会和浮层叠出双份）
        var stepBits = (run.steps || []).map(function (s) { return s.summary || s.tool; })
          .join('\n');
        li.setAttribute('data-tip-title',
          (run.read_only ? '[只读] ' : '') + run.task);
        li.setAttribute('data-tip-lines', JSON.stringify([
          ['时间', String(run.at || '').slice(0, 16).replace('T', ' ')],
          ['结果', (run.cancelled ? '已取消 · ' : '') + (run.ok ? '成功' : '失败：' + (run.error || '未知'))],
          ['用时', run.duration_ms
            ? Math.max(1, Math.round(run.duration_ms / 1000)) + ' 秒 / ' + (run.steps || []).length + ' 步'
            : '—']
        ]));
        if (stepBits) { li.setAttribute('data-tip-note', '步骤：\n' + stepBits); }
        var at = document.createElement('time');
        at.className = 'agent-runs__at';
        at.textContent = String(run.at || '').slice(5, 16).replace('T', ' ');
        var task = document.createElement('span');
        task.className = 'agent-runs__head';
        var modeLabel = run.dry_run ? '[计划] ' : (run.read_only ? '[只读] ' : '');
        task.textContent = modeLabel + run.task;
        var mark = document.createElement('span');
        mark.className = 'agent-runs__mark';
        mark.textContent = run.ok ? '✓' : '✗';
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'agent-runs__del';
        del.setAttribute('aria-label', '删除这条记录');
        del.title = '删除这条记录';
        del.textContent = '×';
        del.addEventListener('click', function (e) {
          e.stopPropagation();
          if (!run.id) { return; }   // 早期记录会在列表加载时被补上 id，正常不会走到这
          var csrfMeta = document.querySelector('meta[name="csrf-token"]');
          fetch('/api/agent/runs/delete', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRF-Token': csrfMeta ? csrfMeta.getAttribute('content') : ''
            },
            credentials: 'same-origin',
            body: JSON.stringify({ id: run.id })
          }).then(function (res) { return res.json(); }).then(function (data) {
            if (data && data.ok) {
              li.remove();
              var count = document.getElementById('agent-runs-count');
              if (count) { count.textContent = data.remaining ? String(data.remaining) : ''; }
              if (!runsList.children.length) { runsSection.hidden = true; }
            }
          }).catch(function () { /* 静默 */ });
        });
        li.appendChild(at);
        li.appendChild(task);
        li.appendChild(mark);
        li.appendChild(del);
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

    // ===== 执行模式切换（读写 / 只读 / 计划） =====
    var MODE_KEY = HISTORY_KEY + '.mode';

    // 非读写模式的常驻提示文案：横幅 + 输入框变色（CSS .composer.is-dry / .is-ro），
    // 免得用户扫一眼没读到回答里那句话，就以为「删除」真的执行了
    var MODE_NOTES = {
      ro: '只读模式：只看不改 —— 任何写操作都会被拦下，不会动你的笔记。',
      dry: '计划模式：只出计划、不动手 —— 不会真正修改任何笔记；要执行请点结果下方的「按计划执行」。'
    };
    var modeBox = document.getElementById('agent-mode');
    var currentMode = 'rw';
    if (modeBox) {
      try {
        var savedMode = window.localStorage.getItem(MODE_KEY);
        if (savedMode === 'ro' || savedMode === 'dry') { currentMode = savedMode; }
      } catch (err) { /* 无痕模式就算了 */ }

      var segBtns = modeBox.querySelectorAll('.seg__btn');
      var segThumb = modeBox.querySelector('.seg__thumb');

      // 滑块跟手：宽度与位移都取当前选中按钮的实测值（切换时有过渡动画）
      function moveThumb(btn) {
        if (!segThumb || !btn) { return; }
        segThumb.style.width = btn.offsetWidth + 'px';
        segThumb.style.transform = 'translateX(' + btn.offsetLeft + 'px)';
      }

      function applyMode(mode) {
        currentMode = mode;
        segBtns.forEach(function (btn) {
          var on = btn.getAttribute('data-mode') === mode;
          btn.classList.toggle('is-on', on);
          btn.setAttribute('aria-pressed', on ? 'true' : 'false');
          if (on) { moveThumb(btn); }
        });
        // 强提示：非读写模式下横幅常驻 + 输入框变色，别让用户以为操作真的执行了
        var banner = document.getElementById('agent-mode-banner');
        if (banner) {
          var note = MODE_NOTES[mode];
          banner.hidden = !note;
          banner.className = 'agent-mode-banner'
            + (note && mode === 'dry' ? ' agent-mode-banner--dry' : '')
            + (note && mode === 'ro' ? ' agent-mode-banner--ro' : '');
          banner.textContent = note || '';
        }
        form.classList.toggle('is-dry', mode === 'dry');
        form.classList.toggle('is-ro', mode === 'ro');
        try { window.localStorage.setItem(MODE_KEY, mode); } catch (err) { /* 静默 */ }
      }
      segBtns.forEach(function (btn) {
        btn.addEventListener('click', function () { applyMode(btn.getAttribute('data-mode')); });
      });
      applyMode(currentMode);   // 恢复上次的选择
      var thumbRaf = 0;
      window.addEventListener('resize', function () {
        if (thumbRaf) { return; }
        thumbRaf = window.requestAnimationFrame(function () {
          thumbRaf = 0;
          moveThumb(modeBox.querySelector('.seg__btn.is-on'));
        });
      }, { passive: true });
    }

    // 示例任务：点一下填进输入框
    document.querySelectorAll('.agent-example').forEach(function (btn) {
      btn.addEventListener('click', function () {
        taskInput.value = btn.getAttribute('data-task') || '';
        taskInput.focus();
      });
    });

    // 发送：Enter（与问笔记统一）；Shift+Enter 换行；Ctrl/⌘+Enter 也保留；
    // 中文输入法组词时不误触
    taskInput.addEventListener('keydown', function (event) {
      var isSend = (event.key === 'Enter' && !event.shiftKey && !event.isComposing)
        || ((event.ctrlKey || event.metaKey) && event.key === 'Enter');
      if (!isSend) { return; }
      event.preventDefault();
      if (!runBtn.disabled) {
        form.requestSubmit ? form.requestSubmit() : form.submit();
      }
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

    function renderConfirmCard(confirm) {
      if (!stepsEl || !confirm || !confirm.id) { return; }
      var li = document.createElement('li');
      li.className = 'agent-confirm';
      var text = document.createElement('p');
      text.className = 'agent-confirm__text';
      var label = confirm.label || '执行该操作';
      var consequence = confirm.consequence || '此操作影响面较大，请确认。';
      text.textContent = '确认' + label + '《' + (confirm.title || ('#' + confirm.note_id)) + '》？' + consequence;
      var actions = document.createElement('div');
      actions.className = 'agent-confirm__actions';
      var okBtn = document.createElement('button');
      okBtn.className = 'btn btn--danger btn--sm agent-confirm__ok';
      okBtn.type = 'button';
      okBtn.textContent = '确认执行';
      var noBtn = document.createElement('button');
      noBtn.className = 'btn btn--ghost btn--sm agent-confirm__no';
      noBtn.type = 'button';
      noBtn.textContent = '取消';
      actions.appendChild(okBtn);
      actions.appendChild(noBtn);
      li.appendChild(text);
      li.appendChild(actions);
      stepsEl.appendChild(li);

      var csrfMeta = document.querySelector('meta[name="csrf-token"]');
      var csrf = csrfMeta ? csrfMeta.getAttribute('content') : '';

      function settle(message, ok) {
        text.textContent = message;
        text.className = 'agent-confirm__text' + (ok ? '' : ' agent-confirm__text--warn');
        if (actions.parentNode) { actions.parentNode.removeChild(actions); }
      }

      okBtn.addEventListener('click', function () {
        okBtn.disabled = true;
        noBtn.disabled = true;
        fetch('/api/agent/confirm', {
          method: 'POST',
          headers: { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ confirm_id: confirm.id }),
        }).then(function (res) { return res.json(); }).then(function (data) {
          if (data && data.ok) {
            var note = (data.result && data.result.note) ? data.result.note : '已执行。';
            settle(note, true);
          } else { settle((data && data.error) || '执行失败', false); }
        }).catch(function () { settle('网络错误，请重试', false); });
      });

      noBtn.addEventListener('click', function () {
        okBtn.disabled = true;
        noBtn.disabled = true;
        fetch('/api/agent/confirm/cancel', {
          method: 'POST',
          headers: { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify({ confirm_id: confirm.id }),
        }).then(function () { settle('已取消，没有做任何改动。', true); })
          .catch(function () { settle('已取消，没有做任何改动。', true); });
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
      runTask(task, null);
    });

    // ===== 两段式任务流：计划产出的计划审阅后可以一键真正执行 =====
    var lastPlan = '';
    var lastPlanTask = '';
    var planBar = null;

    function clearPlanBar() {
      if (planBar && planBar.parentNode) { planBar.parentNode.removeChild(planBar); }
      planBar = null;
      lastPlan = '';
    }

    function showPlanBar(planText, planTask) {
      if (!answerEl || !answerEl.parentNode) { return; }
      clearPlanBar();
      planBar = document.createElement('div');
      planBar.className = 'agent-planbar';
      var hint = document.createElement('span');
      hint.className = 'agent-planbar__hint';
      hint.textContent = '计划看过了没问题？';
      var goBtn = document.createElement('button');
      goBtn.type = 'button';
      goBtn.className = 'btn btn--primary btn--sm';
      goBtn.textContent = '按计划执行';
      goBtn.addEventListener('click', function () {
        goBtn.disabled = true;
        runTask(planTask, planText);
      });
      planBar.appendChild(hint);
      planBar.appendChild(goBtn);
      answerEl.parentNode.insertBefore(planBar, answerEl.nextSibling);
    }

    function runTask(task, plan) {
      var csrfMeta = document.querySelector('meta[name="csrf-token"]');
      var csrf = csrfMeta ? csrfMeta.getAttribute('content') : '';
      runBtn.disabled = true;
      if (plan) {
        clearPlanBar();
        taskInput.value = task;   // 输入框跟上正在执行的任务
        setStatus('正在按确认的计划执行…', '');
      } else {
        setStatus('正在执行（每完成一步都会即时显示）…', '');
      }

      // 清掉上一次的结果
      pendingEl = null;
      stepCount = 0;
      hideCancel();
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
        if (item.confirm) { renderConfirmCard(item.confirm); }   // 危险操作确认卡片
      }

      function onFinal(ev) {
        removePending();
        hideCancel();
        var secs = ev.duration_ms ? '（用时 ' + Math.max(1, Math.round(ev.duration_ms / 1000)) + ' 秒）' : '';
        if (ev.cancelled) { setStatus('已取消' + secs, 'warn'); }
        else { setStatus((ev.ok ? '完成' : (ev.error || '没有完成')) + secs, ev.ok ? 'ok' : 'warn'); }
        if (answerEl) {
          answerEl.textContent = ev.answer || ev.error || '';
          answerEl.className = 'agent-answer' + (ev.ok ? '' : ' agent-answer--error');
        }
        if (ev.ok && ev.answer) { pushHistory(task, ev.answer); }
        if (!plan && currentMode === 'dry' && ev.ok && ev.answer) {
          lastPlan = ev.answer;
          lastPlanTask = task;
          showPlanBar(ev.answer, task);
        } else {
          clearPlanBar();
        }
        loadRuns();
      }

      fetch(form.getAttribute('data-run-url') || '/api/agent/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrf
        },
        credentials: 'same-origin',
        body: JSON.stringify({
          task: task,
          mode: plan ? 'rw' : currentMode,
          plan: plan || undefined,
          history: history.reduce(function (acc, turn) {
            acc.push({ role: 'user', content: turn.task });
            acc.push({ role: 'assistant', content: turn.answer });
            return acc;
          }, [])
        })
      }).then(function (res) {
        if (!res.ok) {
          runBtn.disabled = false;
          hideCancel();
          removePending();
          setStatus('请求失败（HTTP ' + res.status + '），请重试', 'warn');
          return;
        }
        // run_id 从响应头拿到：任务运行期间可以点「停止」取消
        currentRunId = res.headers.get('X-Run-Id') || '';
        if (cancelBtn && currentRunId) {
          cancelBtn.hidden = false;
          cancelBtn.disabled = false;
        }
        if (!res.body || !res.body.getReader) {
          runBtn.disabled = false;
          hideCancel();
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
            onStep(ev);   // 整个事件传下去：confirm 卡片、涉及笔记都在里面
          } else if (ev.type === 'final') {
            onFinal(ev);
          } else if (ev.type === 'error') {
            removePending();
            hideCancel();
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
              hideCancel();
              return;
            }
            buffer += decoder.decode(chunk.value, { stream: true });
            processBuffer();
            return pump();
          }).catch(function (err) {
            runBtn.disabled = false;
            hideCancel();
            removePending();
            setStatus('读取失败：' + (err && err.message ? err.message : '网络错误'), 'warn');
          });
        }
        pump();
      }).catch(function (err) {
        runBtn.disabled = false;
        hideCancel();
        removePending();
        setStatus('请求失败：' + (err && err.message ? err.message : '网络错误'), 'warn');
      });
    }
  });
})();
