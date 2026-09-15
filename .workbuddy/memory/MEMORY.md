# 墨痕 InkNote · 项目约定（跨会话，精简版；详录见 memory/YYYY-MM-DD.md）

## 跑测试 / 验收
- bash PATH 坏：先 `export PATH="/usr/bin:/bin:$PATH"`；PowerShell 拿不到 stdout，优先 bash。
- 全量：`./.venv/Scripts/python.exe -m pytest tests -q -n 8 --dist loadfile --junitxml=.scratch/junit.xml`（约 20s；loadfile 不能省；沙箱吞退出码，权威解析 junitxml；临时目录别指项目内）。
- 三件套：全量测试 + `scripts/audit_css.py` + `scripts/check.py --quick`。

## 版本管理（.git 丢过两次：09-14 / 09-15）
- 提交后保持工作区干净（`.scratch/`、`data/`、`.venv/` 已 gitignore）；无远端。
- 每轮提交后 `git bundle create C:/repo/inknote-<日期>.bundle --all`；批量操作前 `git log -1` 确认。
- 恢复：`git init -b main` → `git fetch <bundle> "+refs/heads/main:refs/remotes/recover/main"` → `git reset --mixed refs/remotes/recover/main`；工作区不丢。已配每日自动备份。
- 多 agent 并行：按文件所有权分波（样式走 `.scratch/css-patch-*.css`，agent 不碰 style.css），主 agent 合并 + 接线；e2e 端口一人一个；合并脚本别用长 assert 链（中断会静默跳过后半段）。

## 全局约定
- 真值解析唯一入口 `utils.as_bool()`，禁止 `bool(表单串)`（`bool("0")` 恒真）。
- 后台线程总开关 `INKNOTE_WORKERS`（conftest 设 0）。
- 表单整数 ID 先过 `deps.MAX_SQLITE_INT`，否则 OverflowError → 500。

## 测试写法（共享 session 库）
- 会话级 fixture 不能依赖函数级；`auth_client`/`csrf` 是函数作用域。
- 禁止断言全局聚合：专属前缀命名 + 前后增量，或按主键定位（别用 `groups[0]`）。
- 模板分母兜底 `or 1`（ZeroDivisionError 曾连红十几个用例）。

## AI 用量记账
- 记账唯一入口 `ai.chat()` 的 `_record_usage()`；不走 chat() 的路径必须自己记（流式 usage 在 `if not choices: continue` 之前取）。
- 失败也记账；task 名区分调用方，新调用点同步 `ai_admin.TASK_LABELS`。
- ai_usage 表自管列（`ensure()` 按 PRAGMA 补 ALTER）；`summary()` 只加键不改键。

## 前端守卫与惯例
- 双守卫：audit_css.py（模板）+ test_js_class_guard.py（JS 类名，白名单只许变短）。
- CSS 按轮次追加：加规则前先 grep 同选择器（优先级坑）；删补丁节用双锚点，绝不能 `src[:start]`（曾连带删掉后 63 行）；入场动画用 `backwards` 不用 `both`。
- `.is-empty` 带 pointer-events:none，加到可交互元素会静默失效。
- 图标按钮必须 title + aria-label；新页面必须有导航入口；动作区控件同高（36/30/44）；低频动作收 `.menu-group`。
- 接口冻结在 `docs/roundN-*.md`；类名 / id 以 `docs/frontend-contract.md` 为准。

## 自绘下拉（select.js）
- 原生 select 留文档流当尺寸基准（tabindex=-1 / aria-hidden / pointer-events:none），自绘按钮 absolute inset:0 盖上；选中后 dispatchEvent(change) 保持自动提交；无 JS 时 SSR 不变。
- 坑：原生 select 必须 `display:block; width:100%`，否则退回 shrink-to-fit。

## 悬浮提示（chart-tip.js）
- 命中 `[data-tip-title], [title], [data-tip-stash-title]`；title 一进元素立刻摘到 stash（防原生气泡抢跑），离开还原。摘 title 的涟漪三处必须同步：readInfo 兜底读 stash、closestTipped 含 stash、cancelShow 还原 pending。
- 160ms 延迟（扫过不弹、停住才弹）；面板 `width:max-content` 别加 min-width，上限 300px。

## UI/UX 惯例
- 历史/审计区块默认折叠（details + 条数徽标）；summary 里的按钮 preventDefault + stopPropagation。
- 「无 JS 才显示」：head 内联脚本挂 js 类（**CSP 要 nonce**，缺了静默失效）+ `.js .no-js-only{display:none}`。
- Enter 发送 / Shift+Enter 换行（IME 组词不误触）；合并 keydown 时 Enter 分支会吃掉 Ctrl+Enter。
- seg 滑块：绝对定位 `.seg__thumb` + transform/width 过渡。

## 笔记助手（agent.py）
- 每轮一个 JSON；观察结果按工具给 `observe_limit`（统一截断会废掉 read 工具）；防空转拦「同工具同参数」；测试用 ScriptedChat monkeypatch ai.chat。

## 图形 / 布局类改动 + e2e（近期教训）
- 零依赖 ⇒ 借算法不引库。Louvain 类局部移动算法：候选必须含「原状态」且严格更优才移动，否则对称图震荡不收敛（会挂死）；指针拖拽别用 setPointerCapture（click 会跑到 `<svg>`，节点收不到）；力布局拖完要短时「钉住」节点，否则被弹簧拽回。
- e2e：临时库带着真实数据 ⇒ 只统计本测试造的记录；读数前先「等模拟停稳」；观感用 getComputedStyle 量化，别靠截图肉眼判断。

## 界面问题排查
- 真实数据复现：复制 `data/inknote.db` 到临时目录 + `INKNOTE_DATA_DIR`；用户改过密码用 `make_session()` 造 cookie。
- 先量再改：几何基线 + 亮暗双主题截图；「重叠」要视口内同时可见才算。
- 浮层问题：elementFromPoint → 沿父链收集 position/z/transform → 找创建层叠上下文的祖先。
- 悬停行为：插桩 `window.__ctl` + 探针记录事件与 closest 结果。
- **同一条消息里对同一文件发多个 Edit 会丢更新** → 同文件多处改动串行，改完 Read 复核。
