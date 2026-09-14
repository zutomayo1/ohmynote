# 墨痕 InkNote · 项目约定（跨会话）

## 跑测试
- **全量（默认用这条）**：约 20 秒，当前 **770 passed**。
  ```bash
  export PATH="/usr/bin:/bin:$PATH"   # bash 工具 PATH 是坏的，每条命令必加
  cd C:/repo/inknote
  ./.venv/Scripts/python.exe -m pytest tests -q -n 8 --dist loadfile
  ```
- **`--dist loadfile` 不能省**：`-n` 默认的 `--dist load` 会把同一个文件的用例拆到不同
  worker，文件内共享的 session 库 / 登录态被拆散 → 出现大片「单独跑绿、并行跑红」的假红。
- 串行等价跑法 `-m pytest tests -q`（约 100 秒），排错时才用。
- **临时目录不要指到项目内**（旧约定已作废）。沙箱的「安全删除」钩子只对 `tempfile.gettempdir()`
  下的路径放行，指到 `.scratch/` 反而会被拦（`--basetemp=.scratch/...` 会触发
  `SAFE_DELETE_BULK_CONFIRM_REQUIRED` → `SystemExit` → 87 个 ERROR）。
- 沙箱里 pytest 收尾清理仍可能被拦掉，**退出码和 "N passed" 摘要会丢**；要权威结果就加
  `--junitxml=.scratch/junit.xml`，再解析 `testsuite` 的 `tests/failures/errors`。
- 验收三件套：上面这条 + `python scripts/audit_css.py`（必须报「所有模板 class 都有对应样式」）
  + `python scripts/check.py --quick`。`scripts/check.py` 全量自检约 22 秒（测试步骤已并行）。

## 版本管理
- 2026-09-12 才建 git（`git init -b main`，初始提交 `0de2515`）。此前所有改动都没有历史。
- 提交后请保持工作区干净；`.scratch/`、`data/`、`.venv/` 已在 `.gitignore` 里。

## 全局约定（踩过坑的）
- **真值解析只有一个入口**：`app/utils.py` 的 `as_bool(value, *, default=False)`。
  全项目不许再出现 `bool(表单字符串)` 或自己的 `{"1","true",...}` 字面量 —— `bool("0")` 恒真，
  真的把「取消公开」当成公开过一次。各模块可以留 `_form_flag` / `_truthy` 这类短名字，但必须委托过去。
- **后台线程有总开关** `INKNOTE_WORKERS`（默认开）。`tests/conftest.py` 里设成 `0`：
  测试会反复触发 lifespan，线程跑起来会让「索引状态 / 自动备份」类断言变得不确定。
  要测线程本身的用例自己 `monkeypatch.setenv("INKNOTE_WORKERS", "1")`，见 `tests/test_workers.py`。
- 表单里的整数 ID 一律先过范围校验（`app/deps.py` 的 `MAX_SQLITE_INT`）。
  直接 `int()` 一个 20 位数字绑到 SQL 上会抛 `OverflowError` → 500。

## shell 环境
- `bash` 工具的 PATH 是坏的（`ls`/`head`/`grep` 找不到）。先 `export PATH="/usr/bin:/bin:$PATH"` 再跑。
- PowerShell 工具在本会话拿不到 stdout，优先用 bash + 上面的 PATH 修复。

## fixture 作用域（踩过的坑）
- **会话级 fixture 不能依赖函数级 fixture**，否则 pytest 抛 `ScopeMismatch`（整个文件报 ERROR）。
- `tests/conftest.py` 的 `auth_client` / `csrf` 是**函数作用域**（每个用例重新登录，换密码 /
  新建 TestClient 都不会掉登录态，这是为了修「单独跑全绿、全量跑全红」）。
- 需要长时间存活的登录态时，用 `from conftest import login` 然后 `login(client)` + `csrf_of(client)`，
  像 `test_smoke.py` 的 `note_id` / `published_slug` 那样。
- 新建 TestClient 已登录的写法：`client.__class__(client.app)`，见 `tests/test_tag_input.py`。

## 用例与共享 session 库（写新用例必读）
- session 级 `client` 只建一次库，**整个 session 的用例共用一个 SQLite**。所以新用例
  **不能断言全局聚合结果**（「一共 3 条」「标题列表恰好是 [...]」）——别的文件早在库里
  留了数据，结果就是「单跑全绿、全量跑红」。
- 正确写法二选一：
  - **专属命名 + 前后增量**：名字带本用例独有前缀，断言 `after - before`
    （见 `tests/test_usage_models.py` 的 `models-alpha` 与 `before = summary(conn)`）。
  - **按主键定位**：`next(g for g in groups if g["note_id"] == mine)`，别用 `groups[0]`
    （排序会被别人的数据顶掉）；断言「我的在里面 / 排除项不在里面」
    （见 `tests/test_todos_batch_backfill.py::test_collect_tasks_groups_and_excludes`）。
- 模板里凡是要 `a / b` 的分母必须兜底 `or 1`：`settings.html` 的按模型占比条就因为
  `max_model_tokens` 为 0 抛过 `ZeroDivisionError`，设置页 500 → 十几个用例一起红。

## AI 用量记账（app/services/ai_usage.py + ai.py）
- **记账唯一入口是 `ai.chat()` 里的 `_record_usage()`**。任何**不走 chat() 的调用路径**
  都必须自己记账 —— 流式问笔记（`/ask/stream` 自己收流）就漏了很久，
  结果「问笔记的主路径一条都没记」。新增这类路径时先问一句「它的用量谁记？」
  - 流式要 `stream_options: {"include_usage": true}`，且 usage 分片的 `choices` 是空数组，
    必须**在 `if not choices: continue` 之前**取 usage。
- **失败也要记账**（超时/HTTP 错/非 JSON/无 choices 都记 `ok=0` + 原因）：
  只记成功的话，「失败率」「最慢的一次」这些指标永远算不出来。副作用是调用次数会变大。
- **归因要区分调用方**：`task` 名不能混（agent 曾和问笔记共用 `answer`，看不出助手花了多少）。
  新增调用点时同时更新 `ai_routers/ai_admin.py` 的 `TASK_LABELS`。
- `ai_usage` 表**自己管表、不走 `db.MIGRATIONS`**：加列要写进 `ai_usage.COLUMNS`，
  由 `ensure()` 用 `PRAGMA table_info` 判断后 ALTER TABLE 补上（`CREATE TABLE IF NOT EXISTS` 不补列）。
- `summary()` 返回结构被别处引用（`month_calls`/`all_tokens`/`by_model`/`by_day` 等），
  扩展时**只加键、不改已有键**。

## 样式守卫（两层，都要过）
- `scripts/audit_css.py`：模板里用到的 class 是否都在 `style.css` 里有规则。**只扫 SSR HTML。**
- `tests/test_js_class_guard.py`：扫描 `app/static/js/*.js` 里
  `className = '...'` / `classList.add|toggle|remove('...')` 出来的类名，逐个断言有样式。
  JS 造类名写错是静默失败 —— 模板审计扫不到，元素只是变成裸样式（`.tag-chip__*` 那次漏过一回）。
  文件里有个 `KNOWN_UNSTYLED` 白名单（现存两个遗留死类），**只许变短**，另有用例盯着它。

## 自绘下拉（原生 select 的统一改造法，已全站铺开）
- 站内两套下拉：原生 `<select class="select">`（列表/搜索/标签/批量栏共 10 个，**已全部改造**）
  + 自绘 `.combo`（设置页「选 AI 模型」）。实现全在 `app/static/js/select.js`，
  base.html 在 app.js 之后 defer 引入；自动扫 `select.select`，要跳过就加 `data-native-select`。
- **核心：只换视觉、不动布局。** 原生 select **仍留在文档流里当尺寸基准**
  （它决定宽度：长分类名会把下拉撑到 348px、/tags 排序下拉是 220px），只加
  `tabindex=-1` / `aria-hidden` / `pointer-events:none`；自绘按钮 `position:absolute; inset:0`
  严丝合缝盖在上面，面板用 `.combo__panel` / `.combo__item`。
  早期版本是把原生 select `hidden` + 让按钮撑宽度，**会丢「宽度随最长选项增长」**，别回退到那种做法。
- 选中后给原生 select `dispatchEvent(new Event('change', {bubbles:true}))` ——
  `app.js` 的「筛选栏改了就自动提交」（`.toolbar select` 的 change → `form.submit()`）
  和 `batch.js` 的「按操作显隐输入框」都靠它继续生效；`required` 的原生校验也照旧。
- 必须渐进增强：整体 `try/catch`，失败保持原生下拉；无 JS 时 SSR 一个字不变
  （`tests/test_js_class_guard.py::test_native_selects_still_in_html_for_no_js` 守着）。
- **坑**：原生 select 原来是 flex/grid 项目，靠 `align-items/stretch` 被撑满；换成块级盒子后
  会退回 `shrink-to-fit`。必须写
  `.select-combo .select-combo__native { display: block; width: 100% }`
  （选择器要两段类，才能压过 `.toolbar .select { width: auto }`）。

## 悬浮提示 / 图表（用量页）
- 图表**不用原生 `title`**：它是浏览器画的（系统字体、固定位置、延迟约 1 秒、
  移动端几乎不触发、键盘用户看不到）。统一用 `app/static/js/chart-tip.js` 的自绘浮层
  （`.chart-tip`），数据从元素的 `data-tip-*` 属性读，所以模板只渲染一份数据。
- **`title` 必须继续留着**：没有 JS 的环境靠它兜底，这是渐进增强的底线（有用例守着）。
- 提示里要给**页面上没有的信息**（失败次数、平均耗时），只重复柱高的话没意义 ——
  新增图表维度时同步给 `ai_usage.summary()` 的对应分组补字段。
- `grouped()` 返回 `failed` / `avg_latency`；平均耗时用 `AVG(NULLIF(latency_ms, 0))`，
  失败记录的 latency 是 0，不排除会把均值拉低。
- 键盘可达：容器 `tabindex=0` + `data-tip-nav`，方向键移动、`data-tip-active` 标记当前格。

## `.is-empty` 是全局工具类（带 pointer-events: none）
- `style.css` 里 `.is-empty { pointer-events: none }` 是给禁用态用的。**把它加到可交互元素上
  会让整块区域静默失效** —— 悬停没反应、点不着，页面看不出异常。
- 需要「空状态样式 + 仍可交互」时，用两段类名压过去（`.x__col.is-empty { pointer-events: auto }`）。
- 排查「悬停没反应」：先用 `document.elementFromPoint(x, y)` 看命中的是谁，
  再 `getComputedStyle(el).pointerEvents` 看是不是被工具类关了。playwright 只会报
  「父容器 intercepts pointer events」，那是个误导性的表象。

## 笔记助手（app/services/agent.py）
- 协议：模型每轮只回一个 JSON（`{"action": ..., "params": ...}` 或 `{"action":"final",...}`），
  服务端执行工具后把观察结果喂回去。工具表在 `_make_tools()`，写操作白名单在 `_WRITE_TOOLS`。
- **观察结果按工具给上限**（`spec["observe_limit"]`）。所有结果统一按 `OBSERVE_LIMIT` 截断会
  把 read 类工具直接搞废：当初 `read_note` 的正文被截到 800 字，模型只能反复换 offset 读同一篇，
  6 步预算烧光后报「步骤太多」——**看起来像模型笨，其实是喂给它的东西不完整**。
  改 Agent 行为前，先量「它到底能看到多少字」。
- 会话记忆有两层：前端传的 `history`（只有 user/assistant 文本，note_id 早丢了）
  + 服务端注入的「上一轮任务存档」（`_last_run_recap`，来自 `agent.runs` 里的 involved notes）。
  后者才是「继续 / 刚才那篇」能接上的关键。
- 防空转：同一「工具 + 完全相同的参数」重复调用会被拦下，连撞 `MAX_REPEAT_STEPS` 次就收场。
  报错的调用不算「做过」，换参数仍可重试。新增测试时要留意它会让「步数上限」类断言提前结束。
- `run_agent` 是 `iter_agent_events` 的消费者；测试用 `tests/test_agent.py` 的 `ScriptedChat`
  monkeypatch `ai.chat` 按脚本吐回复，配合 `db_conn` fixture 即可，不需要真模型。

## 视觉改动必须先量基线（逐像素回归法）
- 改下拉/布局前先跑 `.scratch/selects_geo.py baseline`，改完跑 `... after`，再 diff：
  它记录 9 个「页面×视口」组合里每个 select 的盒子、父容器、自绘按钮覆盖位置，
  以及 toolbar / 首屏卡片 / 页面标题 / `scrollWidth`·`scrollHeight`。
- 造数据要**故意放超长标签 / 超长分类名**，否则逼不出「宽度随最长选项增长」这类分支
  （不造的话 150px 和 348px 的差异根本看不见）。
- 只看几何不够，最后还要 playwright 实测行为 + 亮暗双主题截图。

## ⚠️ 版本库与备份（.git 丢过一次）
- 2026-09-14 会话中 `.git` **整个目录意外消失**（原因未定位，源码没丢）。**仓库没有远端**。
- 恢复来源：`C:/repo/inknote.zip` 里有 9-12 的 `inknote/.git/`（207 个文件）。
  提取要用 Python 的 `zipfile` 按前缀取（`unzip "inknote/.git/*"` 不递归，只出一层）。
- 以后要打包 `.git` 备份或加远端；跑 `git stash` 这类批量改工作区的命令前，
  先确认 `.git` 完好（`git log -1` 能出）。

## 用用户真实数据复现界面问题（有效且安全）
- 复制 `data/inknote.db` 到临时目录、`INKNOTE_DATA_DIR` 指过去 —— 绝不动原库。
- **用户改过密码**，默认密码登不进去。浏览器探测：
  `from app.security import make_session; token, _ = make_session(settings.secret_key, max_age=3600)`
  然后 `context.add_cookies([{name: settings.session_cookie, value: token, url: base}])`。
  注意 `is_authed` 是模板上下文变量，光覆盖 `require_login` 依赖顶栏仍是未登录版。
- 排查「布局乱」先量再改：`.scratch/repro_three.py` 是范例（真实数据 + 逐元素测量 + 截图）。
- **「重叠」必须在视口内同时可见才算数**：两个都滚出屏幕的元素的 rect 差是幻影
  （这次量出 851px「重叠」其实不存在）；关闭的 `<details>` 子元素也有布局盒，要排除。
- sticky 吸顶元素的活动范围是**整个父容器列**：同列的后继块会从它底下穿过、被它盖住。
  CSS 无解，要么把后继块挪出该列（本次：相关内容挪到正文下方 `.post-after`），要么 JS 钳制。

## 按钮与动作区（规范见 docs/frontend-contract.md 第 4 节）
- 层级 4 种（primary 每屏一个 / 默认 / ghost / danger 必带确认）；尺寸 3 档（36 / 30 / 44）。
  **同一个动作区里所有控件必须同高**，混 36 和 30 就是「看着没对齐」。
- 低频动作收进 `.menu-group`（details 实现，无 JS 可用）；菜单项留在 DOM 里
  （文案断言与无障碍不受影响）；`app.js` 的 `initMenuGroup()` 管点外/Esc/点项收起。
- 图标按钮必须 `title` + `aria-label`，状态开关加 `aria-pressed` 与 `is-on`。
- 引用图标前先确认 `_macros.html` 里定义过：`icon('copy')` 曾经根本不存在，
  渲染成空 svg（按钮上凭空一段空白）。守卫在 `tests/test_icon_macro.py`。
- 量按钮用 `.scratch/button_audit.py` / `.scratch/button_verify.py`
  （17 页逐动作区数按钮与尺寸，能报「同区尺寸不一」）。注意探针要排除
  关闭的 `<details>` 内容（Chrome 里它们仍有布局盒），但 summary 要放行。

## CSS：同一条规则别写两遍（有守卫）
- `style.css` 是按轮次追加的，很容易出现「同一件事被两段规则同时管」：
  新规则想把下划线居中（`left:50%` + `translateX(-50%)`），旧规则的
  `left: 12px` 却因**优先级更高**（`.a.is-b::after` 0,2,1 vs `.a::after` 0,1,1）压住它，
  于是 transform 反过来把元素拖偏 —— 导航下划线就这么偏了 14px。
- **加新规则前先 grep 同一个选择器**；确实要分两处写的，写清理由并进白名单。
- 守卫在 `tests/test_css_rules.py`：伪元素选择器不许重复定义（白名单 + 不许烂着）、
  导航下划线居中契约、hover 规则必须排除选中项。
- 调试这类「看着偏/看着不对」：**先量数字再改** ——
  `::after` 拿不到 rect，就从 computed style 的 left/width/transform 反推落点再和中心比。

## 删 CSS 补丁节：必须用双锚点（踩过）
- `style.css` 是**按轮次追加**的，补丁节的先后顺序 = 加入顺序，**不保证谁在末尾**。
- 删某一节**绝不能**写 `src[:start]`（那会截掉后面所有内容）。用
  「本节起点 → 下一个 `/* ===== 补丁` 标记」两个 index 拼，或
  `git show HEAD:app/static/css/style.css` 取回原文件再精确切除。
- 曾经这么删 `.settings-bulk`，连带把后面的 `.select-combo`（63 行）全删了，
  列表页每个下拉渲染成两份、宽度翻倍。**改完 CSS 立刻跑全量**：
  `tests/test_js_class_guard.py` 就是为抓这类「JS 造了类名但样式没了」而存在的。
- 几何回归脚本要**连截图一起看**，别只比 JSON 数字。

## CSS 补丁流程（多 agent 并行轮次的惯例）1. 并行 agent 只把自己的样式写进 `.scratch/css-patch-<名字>.css`，**不碰 `style.css`**，只用已有令牌。
2. 主 agent 合并进 `app/static/css/style.css` 末尾，加一节
   `/* ===== 补丁：<名字>（…，已合并） ===== */`。
3. **合并后要对着补丁文件核一遍类名**：补丁在合并前又被改过类名的话，`style.css` 里留的是过期版本
   （本轮 `.tag-chip__*` → `.tag-manage__*` 就是这么漏的：模板用新名，样式表里是旧名 + 死代码）。
   核对法：`grep -o '\.[a-zA-Z][a-zA-Z0-9_-]*' 补丁 | sort -u` 与对应分节做 `comm` 差集。
4. 顺手把「运行时读 `.scratch/` 再内联」这类 hack 拆掉（`pages.py` 读 `css-patch-tags.css` 那种），
   否则 `.scratch/` 一被清理，样式就静默丢失。

## 接口/文档约定
- 每轮并行开发的接口冻结写在 `docs/roundN-*.md`；前端类名与 id 以 `docs/frontend-contract.md` 为准，
  新增类名要先改这份契约。
- 验收标准：**不许跑红**，全量测试必须全绿。
