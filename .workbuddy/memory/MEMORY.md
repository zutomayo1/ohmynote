# 墨痕 InkNote · 项目约定（跨会话，精简版；详录见 memory/YYYY-MM-DD.md）

## 跑测试 / 验收
- bash 工具 PATH 是坏的，每条命令先 `export PATH="/usr/bin:/bin:$PATH"`；PowerShell 拿不到 stdout，优先 bash。
- 全量：`cd C:/repo/inknote && ./.venv/Scripts/python.exe -m pytest tests -q -n 8 --dist loadfile --junitxml=.scratch/junit.xml`（约 20s）。
  - `--dist loadfile` 不能省（默认 load 会拆散文件内共享 session 库 → 假红）；沙箱会吞退出码和摘要，权威结果解析 junitxml。
  - 临时目录指 `tempfile.gettempdir()`，别指项目内（沙箱安全删除钩子会拦 `.scratch/`）。
- 验收三件套：全量测试 + `scripts/audit_css.py`（模板 class 都要有样式）+ `scripts/check.py --quick`。

## 版本管理（.git 丢过一次）
- 提交后保持工作区干净；`.scratch/`、`data/`、`.venv/` 已 gitignore。仓库无远端。
- 每轮提交后刷新备份：`git bundle create C:/repo/inknote-<日期>.bundle --all`；跑 `git stash` 等批量操作前先 `git log -1` 确认 .git 完好。

## 全局约定（踩过坑的）
- 真值解析唯一入口 `app/utils.py` 的 `as_bool()`，禁止 `bool(表单字符串)`（`bool("0")` 恒真）。
- 后台线程总开关 `INKNOTE_WORKERS`，conftest 设 0；测线程自设 1（见 test_workers.py）。
- 表单整数 ID 先过 `app/deps.py` 的 `MAX_SQLITE_INT`，否则 OverflowError → 500。

## 测试写法（共享 session 库）
- 会话级 fixture 不能依赖函数级（ScopeMismatch）；`auth_client`/`csrf` 是函数作用域。
- session 级 client 共用一个 SQLite：新用例**禁止断言全局聚合**。二选一：专属前缀命名 + 前后增量；或按主键定位（别用 `groups[0]`）。
- 模板分母必须兜底 `or 1`（ZeroDivisionError 曾让设置页 500 连红十几个用例）。

## AI 用量记账（ai_usage.py + ai.py）
- 记账唯一入口 `ai.chat()` 的 `_record_usage()`；不走 chat() 的路径（如流式 /ask/stream）必须自己记。流式 usage 分片 choices 为空，要在 `if not choices: continue` 之前取。
- 失败也记账（ok=0 + 原因）；task 名区分调用方，新调用点同步 `ai_admin.py` 的 TASK_LABELS。
- ai_usage 表自管列（`ai_usage.COLUMNS` + ensure() 用 PRAGMA 判断后 ALTER）；`summary()` 只加键不改键。

## 前端守卫与惯例
- 样式双守卫：audit_css.py（SSR 模板）+ test_js_class_guard.py（JS 造的类名；KNOWN_UNSTYLED 白名单只许变短）。
- CSS 按轮次追加：加规则前先 grep 同选择器（优先级坑曾让下划线偏 14px）；删补丁节用双锚点，绝不能 `src[:start]`（曾连带删掉后 63 行）；伪元素重复定义有守卫（test_css_rules.py）。
- 入场动画用 `backwards` 不用 `both`（残留 transform 创建层叠上下文，困住内部浮层 z-index）。
- `.is-empty` 带 pointer-events:none，加到可交互元素会静默失效；排查「悬停没反应」用 elementFromPoint + getComputedStyle。
- 图标按钮必须 title + aria-label；引用图标前确认 `_macros.html` 里定义过。
- 新页面必须有导航入口（/stats 曾整站无链接）。
- 动作区控件必须同高（36/30/44 三档）；低频动作收 `.menu-group`。
- 接口冻结在 `docs/roundN-*.md`；类名与 id 以 `docs/frontend-contract.md` 为准。

## 自绘下拉（select.js，全站 10 个已改）
- 核心是只换视觉不动布局：原生 select 留在文档流当尺寸基准（决定宽度），加 tabindex=-1/aria-hidden/pointer-events:none；自绘按钮 absolute inset:0 盖上。
- 选中后 dispatchEvent(change) 让 app.js 自动提交和 batch.js 显隐继续生效；渐进增强 try/catch，无 JS 时 SSR 不变（有用例守着）。
- 坑：select 换块级盒子后退回 shrink-to-fit，必须 `.select-combo .select-combo__native { display:block; width:100% }`（两段类压过 toolbar 规则）。

## 悬浮提示（chart-tip.js，全站自绘浮层）
- 命中选择器 `[data-tip-title], [title], [data-tip-stash-title]`：原生 title 自动桥接，模板不用改；title 一进元素立刻摘到 stash（防原生气泡抢跑），离开还原（无 JS 兜底仍在，有用例守着）。
- 摘 title 的涟漪（三处必须同步）：readInfo 兜底读 stash 属性（否则面板不出现）；closestTipped 含 stash 选择器（否则 pointerout 解析到外层祖先，取消/还原全落空）；cancelShow 还原 pending 元素。
- 触发 160ms 延迟：扫过不弹、停住才弹、移开即消失；同元素内部移动不重置不重渲染；键盘导航（data-tip-nav）即时。
- 面板宽度 width:max-content 紧贴文字，别加 min-width；上限 300px。label nowrap（竖排折行根因）。
- 提示要给页面没有的信息（failed/avg_latency）；分母为 0 时 AVG 用 NULLIF 兜底。

## UI/UX 惯例
- 历史/审计类区块默认折叠（details + 条数徽标）；summary 里的按钮要 preventDefault + stopPropagation。
- 「无 JS 才显示」模式：head 内联脚本给 html 挂 js 类（**CSP 要 nonce**，缺了静默失效——「JS 没生效」先查 CSP）+ `.js .no-js-only{display:none}`。
- 快捷键统一：Enter 发送 / Shift+Enter 换行（IME 组词不误触）；合并 keydown 时注意 Enter 分支会吃掉 Ctrl+Enter。
- seg 切换滑块：绝对定位 .seg__thumb + transform/width 过渡，is-on 不自己画底色。

## 笔记助手（agent.py）
- 每轮一个 JSON（action/params 或 final）；观察结果按工具给 `observe_limit`，统一截断会废掉 read 类工具（模型显得笨常是喂得不完整）。
- 会话记忆靠服务端 `_last_run_recap`；防空转拦「同工具同参数」重复（影响步数类断言）。
- 测试用 ScriptedChat monkeypatch ai.chat + db_conn，不需要真模型。

## 界面问题排查方法论
- 用用户真实数据复现：复制 data/inknote.db 到临时目录 + INKNOTE_DATA_DIR 指过去；用户改过密码，用 `make_session()` 造 cookie。
- **先量再改**：几何基线脚本（.scratch/selects_geo.py 等）+ 亮暗双主题截图；造数据要故意放超长文本逼出边界分支；「重叠」必须视口内同时可见才算数（关闭的 details 子元素仍有布局盒要排除）。
- 浮层问题三板斧：elementFromPoint → 沿父链收集 position/z/transform/filter → 找创建层叠上下文的祖先，修复优先消除上下文。
- 复杂悬停行为排查：给相关函数插桩 window.__ctl + 探针记录事件与 closest 结果，一轮定位。
- 教训：同一条消息里对同一文件发多个 Edit 会丢更新（报成功但内容不在）——同文件多处修改要串行，改完 Read 复核。
