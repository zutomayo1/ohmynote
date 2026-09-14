# 墨痕 InkNote · UI 打磨六项实施计划（2026-09-13）

> 本计划供后续会话/其他模型执行。当前代码基线：HEAD ≈ `6a5aeb9`，全量 **743 passed**。
> 六项均已冻结范围：**不加新功能面，只做体验与视觉**。

---

## 0. 项目约定（动手前必读，不遵守会翻车）

- **测试命令**（并行，约 24 秒；`--dist loadfile` 不能省，否则同文件的共享状态被拆散会假红）：
  ```bash
  export PATH="/usr/bin:/bin:$PATH"   # bash 工具 PATH 是坏的，每条命令必加
  cd C:/repo/inknote
  ./.venv/Scripts/python.exe -m pytest tests -q -n 8 --dist loadfile
  ```
  临时目录**不要**指到项目内（`.scratch/` 下的删除会被沙箱拦下），直接用系统临时目录即可。
- **CSS 只追加不改写**：所有新样式追加到 `app/static/css/style.css` **末尾**，开一个补丁节：
  `/* ===== 补丁：<名字>（2026-09-13） ===== */`。颜色**只能用既有令牌**（`--brand`、`--surface` 等，见 style.css 头部 `:root` 与 `[data-theme="dark"]`），禁止写死色值；动效必须包在 `@media (prefers-reduced-motion: no-preference)` 内（文件末尾已有 reduce 规则，顺序会覆盖你）。
- **验收三件套**（每次提交前）：全量测试 + `./.venv/Scripts/python.exe scripts/audit_css.py`（必须报「所有模板 class 都有对应样式」）+ `./.venv/Scripts/python.exe scripts/check.py --quick`。
- **CSP 严格**：`script-src 'self' + nonce`，无 inline JS（除非模板 nonce）、无 CDN。字体/库全自托管。
- **主题机制**：`<html data-theme="light|dark">`，`app.js` 的 `initTheme()` 读写 localStorage；配色预设沿用同模式（新加 `data-palette`）。
- **截图验证法**（视觉改动必做）：参考 `.scratch/beauty_shot.py`——临时实例 + playwright 登录、逐页截图、亮暗双主题对比。
- **提交规范**：`feat:/style:/perf: ` + 中文要点；`.scratch/` 一次性脚本不进库；完成后同步 `README.md` 与 `使用说明.md` 相关段落，并追加 `.workbuddy/memory/2026-09-13.md` 日志。
- **数据格式**：`notes.created_at` 是 `ISO_FMT` 文本（`YYYY-MM-DD HH:MM:SS`，见 `app/utils.py:now_iso`），日期切片用 `substr(created_at, 1, 10)`。
- **现成参考**：批量查询窗口函数写法见 `repo.notes_grouped_by_tags / notes_grouped_by_months`；下载响应写法参考 notes.py 里任意 `Response(..., headers={"Content-Disposition": ...})` 或备份导出路由。

---

## ① 写作热力图（统计页，Task #12）

**现状**：路由 `app/routers/pages.py:456 stats_page`，模板 `app/templates/stats.html`（顶部 stats-strip，下方 stats-grid 卡片）。数据按 `notes.created_at` 日分组，排除回收站。

**设计**：GitHub 风格年度贡献图：53 周列 × 7 日行，以今天为最后一格往前推；篇数分 5 档色阶（0 / 1 / 2–3 / 4–6 / 7+）；每个格子 `title="YYYY-MM-DD · N 篇"`（纯 HTML tooltip，免 JS）；右上图例「少→多」。

**改动**：
1. `app/repo.py` 新增：
   ```python
   def daily_note_counts(conn, days: int = 371) -> dict[str, int]:
       """最近 N 天每天创建的篇数：{'YYYY-MM-DD': n}，排除回收站。"""
       # SELECT substr(created_at,1,10) AS d, COUNT(*) FROM notes
       # WHERE deleted_at IS NULL AND d >= ? GROUP BY d
   ```
2. `app/routers/pages.py` `stats_page`：新增 `heatmap` 上下文。在路由（或一个小 helper，建议放 `app/services/stats_heatmap.py` 新文件保持路由瘦）计算周结构：
   `weeks: list[list[dict]]`，每格 `{"date": "YYYY-MM-DD", "count": n, "level": 0..4, "future": False}`，首周补空白占位（`future: True` 的格不渲染颜色）。level 阈值：0=0, 1=1, 2=2~3, 3=4~6, 4=7+。
3. `app/templates/stats.html`：在 stats-strip 后、stats-grid 前插入 `<section class="card heatmap-card">`：标题「写作热力」，嵌套循环输出 `<span class="hm-cell hm-l{level}">`。容器 `<div class="heatmap" role="img" aria-label="近一年写作热力图">`。末尾加图例（5 个样例格 + 少/多字样）。
4. CSS 补丁节：`.hm-cell` 9px×9px、2px 圆角、3px gap；`.hm-l0` 用 `var(--surface-2)`，l1~l4 用 `color-mix(in srgb, var(--brand) 25%/50%/75%/100%, var(--surface))`（暗色自动适配）；容器 `overflow-x: auto; direction: rtl`（让最新列贴右，滚到最右即今天）；窄屏自动横滚。

**验收**：`tests/` 新文件 `test_stats_heatmap.py`——
- `daily_note_counts` 计数正确、回收站排除；
- 周结构：最后一格是今天、首周对齐星期（Python `date.weekday()`，注意 GitHub 是周日开头，本项目建议周一开头，测试锁定选择）；
- 页面渲染含 `hm-cell`、图例、无数据时不报错（空库也渲染空图）。
- 截图验证亮/暗（复用 beauty_shot.py 模式）。

---

## ③ 专注写作模式（编辑器，Task #21）

**现状**：编辑器 `app/templates/notes/editor.html` + `app/static/js/editor.js`；导航在 `base.html`（`.site-header`）；编辑器布局类名以 `editor-` 前缀为主。

**设计**：编辑器页右上角新增「专注」按钮（图标 + 文字），点击后 `document.body.classList.toggle('editor-focus')`：隐藏 site-header/footer/发布设置侧栏/AI 按钮行，正文容器放大居中（max-width 放宽到 ~780px、字号 +1px），左上角留一个极简「退出专注」按钮；Esc 退出；状态存 localStorage（`inknote.editor.focus`，刷新保持）。

**改动**：
1. `editor.html`：工具行加 `<button type="button" id="editor-focus-toggle">`（图标可用 `icon('eye', 15)` 或新增）。
2. `editor.js`：IIFE 挂 click/Esc 监听 + localStorage 读写；切 class 时同步按钮文字。
3. CSS 补丁节：`body.editor-focus` 下 `.site-header, .site-footer, .publish-panel, .ai-row { display:none }`（类名以实际模板为准，动手前先 grep）；`.editor-body` 或正文容器 `max-width: 780px; margin: 0 auto; padding-top: 40px;`；`.focus-exit` 固定左上半透明按钮；进出场 transition（包 no-preference）。

**验收**：`node --check`；playwright 实测：点击后 header 不可见、Esc 恢复、刷新保持；全量测试（无新用例，防回归即可）。

---

## ⑤ 单文件精美导出（笔记详情页，Task #24）

**现状**：详情页 `app/templates/notes/detail.html`；渲染结果 `rendered.html` 已在路由上下文；图片在 `/media/...`（导出文件里无法内嵌，超链接保留原路径即可，文档里说明）。

**设计**：详情页「…」或操作区加「导出网页」链接 → `GET /notes/{id}/export.html`：服务端把已渲染正文装进一份自包含 HTML：内嵌一套**精简导出专用 CSS**（~120 行，写死配色但用 `:root` + `@media (prefers-color-scheme: dark)` 两套，风格与站点一致：衬线、米白/深棕、品牌色点缀）、meta 标题、生成时间页脚「导出自 墨痕 InkNote · YYYY-MM-DD」。`Response(content, media_type="text/html", headers={"Content-Disposition": f'attachment; filename="{slug or id}.html"'})`，中文文件名用 RFC 5987 `filename*=UTF-8''<urlencoded>`。

**改动**：
1. 新文件 `app/services/note_export.py`：`build_export_html(note, rendered_html) -> str`（模板字符串 + `html.escape` 标题；正文已是渲染后安全 HTML，直接拼接）。
2. `app/routers/notes.py`：`GET /notes/{note_id}/export.html`（登录路由，与详情同权限）。
3. `detail.html` 操作区加链接（`<a href="/notes/{{ note.id }}/export.html" download>`）。

**验收**：`tests/test_note_export.py`——
- 响应 200、`Content-Disposition` 含 filename*、body 含标题与正文片段、含 dark media query；
- 不存在笔记 404；未登录 401/403。
- 手工打开导出文件截图验证排版（可复用临时实例模式）。

---

## 主题预设（配色方案，Task #22）

**现状**：配色全部在 `style.css` 的 `:root` / `[data-theme="dark"]` 令牌；`initTheme()` 管亮暗。

**设计**：与亮暗**正交**的配色维度：`html[data-palette="ink"|"bamboo"|"ocean"]`（默认无属性 = 现配色）。三套：
- `bamboo`（竹青）：品牌色转青绿系（亮 `#3D7A5F` / 暗 `#7FB69B`），底色偏冷白绿
- `ocean`（墨蓝）：品牌色转蓝（亮 `#3B5BA5` / 暗 `#8FA6D9`），底色偏冷灰蓝
- 默认（墨痕原色）无需属性
切换器放导航主题按钮旁（「更多」菜单里一个「配色」子项）或设置页「外观」；选择写 localStorage `inknote.palette`，`app.js` 启动时应用（防闪烁：与主题同款内联脚本位置——查 base.html head 里现有防闪烁脚本，照抄模式）。

**改动**：
1. `style.css` 补丁节：`[data-palette="bamboo"]` 与 `[data-palette="bamboo"][data-theme="dark"]` 等 4 组令牌覆盖（每组 ~12 个变量：bg/surface/ink/line/brand/brand-soft 系）。
2. `app/static/js/app.js`：`initPalette2()`（命名勿撞现有 initPalette）读 localStorage 设置 `data-palette`；切换器 click 写入。
3. `base.html`：切换器 UI（radio 组或下拉），head 防闪烁脚本同 initTheme 模式。

**验收**：四套组合（2 主题 × 2 新配色 + 默认）截图对比；`audit_css` 过；无闪烁（截图时 localStorage 预置验证）。

---

## 空状态插画（Task #23）

**现状**：`_macros.html` 的 `empty_state(title, hint, icon, href, label)` 宏，调用处传图标名；`.empty-state` 样式在 style.css。

**设计**：一套 4~5 张品牌一致的线条风 SVG（纸页+墨点 / 羽毛笔 / 书堆 / 放大镜+空 / 回收篮），描边 `currentColor`、1.5px 线宽、48×48 viewBox，与现有线性图标同气质。宏升级为支持 `art` 参数（无 art 回落原 icon 行为，零回归）。

**改动**：
1. `_macros.html`：新 `empty_art(name)` 宏输出内联 SVG；`empty_state` 增加可选 `art` 参数优先于 icon。
2. 主要调用点换 art：笔记列表空（纸页）、搜索无结果（放大镜）、回收站空（回收篮）、标签空、博客空。
3. CSS 补丁节：`.empty-state__art { color: var(--ink-3); margin-bottom: 10px; }` 加极轻 float 动效（no-preference 包裹）。

**验收**：调用点页面渲染含新 SVG class；未改动的调用点回落 icon；`audit_css` 过。

---

## 加载骨架屏（异步场景，Task #20）

**现状**：SSR 无传统骨架需求；真实异步场景：agent 执行中（`agent.js` 已有 `.agent-steps__pending` 文字提示）、编辑器 AI 按钮（摘要/标题/分类，等待时只有按钮禁用）、ask 流式回答前。

**设计**：骨架样式组件 + 用到三处真实异步点：
1. `.skeleton`（灰底脉冲动画：`background: linear-gradient(90deg, var(--surface-2) 25%, color-mix(...40%) 50%, var(--surface-2) 75%)`，`background-size: 200% 100%`，`@keyframes skeleton-sweep`，包 no-preference）+ `.skeleton--text/--card` 变体。
2. agent 执行中：`agent.js` 的 pending `<li>` 换成 3 行 `.skeleton--text`。
3. 编辑器 AI 生成摘要/标题时：目标输入框位置叠 `.skeleton--text` 遮罩（editor.js 的 ai row 逻辑里加/移 class）。
4. ask 首 token 到达前：回答容器显示骨架行。

**改动**：CSS 补丁节 + `agent.js` + `editor.js` + ask 对应 JS 各 ~5 行。

**验收**：三处异步场景 playwright 验证骨架出现与消失；减弱动效下无动画但占位在。

---

## 执行顺序建议

1. ① 热力图（服务端为主，独立性最强）
2. ⑤ 单文件导出（纯服务端新文件，互不干扰）
3. ③ 专注模式（编辑器局部）
4. 主题预设（令牌覆盖，视觉验证量大）
5. 空状态插画（模板宏 + 少量替换）
6. 骨架屏（CSS + 三处 JS 小改，最后做）

每项独立提交（`style:`/`feat:` 前缀），提交前跑验收三件套；视觉项（①主题预设、③④⑤⑥）按 beauty_shot.py 模式截图验证亮/暗。

## 总验收

- 全量测试全绿（现 743，新增用例后应 ≥760）
- `audit_css.py` OK + `check.py --quick` 全过
- 手机视口（390×844）复扫受影响的页面：/stats、编辑器、详情页、列表页（含空状态）
- README / 使用说明.md 同步；`.workbuddy/memory/2026-09-13.md` 追加日志
