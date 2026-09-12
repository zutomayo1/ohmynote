# 墨痕 InkNote · 前端契约（冻结版）

> 本文件是 HTML / CSS / JS 三方共同遵守的接口约定。
> 模板由 Python 端生成，**必须**只使用下面列出的类名与 id；
> CSS 与 JS 也**必须**只针对下面列出的类名/id 编写。任何一方需要新增，都先改本文件。

---

## 0. 技术约束

- 无构建步骤、无 CDN、无框架。`app/static/js/*.js` 是普通脚本（`<script src>` 加载，非 module）。
- 每个 JS 文件用 IIFE 包裹 + `'use strict'`，挂在 `window.InkNote` 下共享工具。
- 浏览器目标：Chrome/Edge 111+、Firefox 113+、Safari 16.2+。
- 中文界面。注释用中文。
- 所有请求都是同源的；CSRF token 从 `<meta name="csrf-token" content="...">` 读取。

## 1. 设计令牌（CSS 变量，定义在 `:root` / `[data-theme="dark"]`）

风格：**温暖编辑风 / 纸上墨迹**。米白奶油纸面、衬线标题、砖红强调色、宽松行高。

| 变量 | 亮色 | 暗色（深棕） | 用途 |
| --- | --- | --- | --- |
| `--bg` | `#FAF6EF` | `#1F1B17` | 页面底色（绝不用纯白） |
| `--bg-soft` | `#F3ECE0` | `#262019` | 次级底色 / 表头 |
| `--surface` | `#FFFDF8` | `#2A241E` | 卡片纸面 |
| `--surface-2` | `#F7F1E7` | `#332B24` | 卡片内嵌块 |
| `--ink` | `#2E2A26` | `#EDE4D6` | 正文（深灰，非纯黑） |
| `--ink-2` | `#5C5348` | `#C6B9A6` | 次级文字 |
| `--ink-3` | `#8B8073` | `#9A8D7C` | 弱化文字 / 时间戳 |
| `--line` | `#E6DCCB` | `#3D342C` | 分隔线、边框 |
| `--line-strong` | `#D6C7AE` | `#4A4038` | 强调边框 |
| `--brand` | `#B5533C` | `#D98872` | 砖红强调色：链接、按钮、标签 |
| `--brand-dark` | `#8F3F2C` | `#E8A48F` | 悬停态 |
| `--brand-soft` | `#F2E2DA` | `#3A2A23` | 强调色淡背景 |
| `--accent` | `#4A6C5A` | `#83A694` | 墨绿：次要信息、成功态 |
| `--accent-soft` | `#E4EDE7` | `#26332C` | 墨绿淡背景 |
| `--warn-soft` | `#F6E7C8` | `#3B3222` | 草稿/提醒淡背景 |
| `--danger` | `#A83B2E` | `#E08A7B` | 删除等危险操作 |
| `--shadow-sm` | `0 1px 2px rgba(90,70,45,.05), 0 1px 3px rgba(90,70,45,.06)` | 用暗色重定义 | 卡片默认 |
| `--shadow-md` | `0 2px 6px rgba(90,70,45,.07), 0 10px 24px -12px rgba(90,70,45,.18)` | 同上 | 卡片悬停/浮层 |
| `--radius` | `12px` | 同 | 卡片圆角 |
| `--radius-sm` | `8px` | 同 | 输入框、小按钮 |
| `--radius-pill` | `999px` | 同 | 胶囊标签 |
| `--font-serif` | `Georgia, "Source Serif Pro", "Noto Serif SC", "Source Han Serif SC", "Songti SC", SimSun, serif` | 同 | 标题 / 正文 / 大数字 |
| `--font-sans` | `-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif` | 同 | 导航、按钮、表单 |
| `--font-mono` | `ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace` | 同 | 代码、计数 |
| `--measure` | `68ch` | 同 | 正文最大宽度 |
| `--wrap` | `1120px` | 同 | 页面容器宽度 |
| `--lh` | `1.75` | 同 | 正文行高 |

排版硬要求：
- 正文 `line-height: 1.75`；`.prose p` 段间距 ≥ `1.1em`（比常规网页更松）。
- `.prose` 内容宽度限制在 `--measure`（65–75 字符），**不铺满全屏**。
- 卡片圆角 8–12px + 柔和阴影，禁止直角硬边。
- 标签一律胶囊形（`--radius-pill`），**不用方框**。
- 暗色是「深棕背景 + 米白文字」，不是纯黑纯白。

Pygments 代码高亮的 **token 颜色**由 `app/static/css/highlight.css`（自动生成）负责，
`style.css` 只负责 `.codehilite` 的容器样式（背景、内边距、圆角、横向滚动、行号），
**不要**在 `style.css` 里写 `.codehilite .k` 这类 token 选择器（写了也不要紧，但别依赖）。

---

## 2. 全局骨架（`templates/base.html`）

```html
<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="csrf-token" content="...">   <!-- 仅登录后有值 -->
  <title>…</title>
  <link rel="stylesheet" href="/static/css/style.css">
  <link rel="stylesheet" href="/static/css/highlight.css">
  <link rel="alternate" type="application/atom+xml" href="/feed.xml" title="…">
</head>
<body data-page="dashboard|blog|post|editor|detail|login|trash|tags|versions|search|ask|templates|archive|settings|images|backup|stats|manual|error">
  <div class="offline-banner" id="offline-banner" hidden>…</div>
  <header class="site-header">…</header>
  <main class="site-main container">…</main>
  <footer class="site-footer">…</footer>
  <div class="toast-wrap" id="toast-wrap" aria-live="polite"></div>
  <script src="/static/js/app.js" defer></script>
</body>
</html>
```

`body[data-page]` 用于给某些页面加专属样式与初始化 JS。

### 顶栏结构

```html
<header class="site-header">
  <div class="site-header__inner container">
    <a class="brand" href="/">
      <span class="brand__mark">墨</span>
      <span class="brand__text">墨痕<small class="brand__sub">笔记与写作</small></span>
    </a>
    <nav class="site-nav">
      <a class="nav-link is-active" href="/notes">笔记</a>
      <a class="nav-link" href="/tags">标签</a>
      <a class="nav-link" href="/blog">博客</a>
      <div class="nav-more">
        <button class="nav-link nav-more__toggle" type="button" aria-haspopup="true" aria-label="更多导航">更多<span class="nav-more__caret" aria-hidden="true">▾</span></button>
        <div class="nav-more__menu">
          <a class="nav-link" href="/templates">模板</a>
          <a class="nav-link" href="/ask">问笔记</a>
          <a class="nav-link" href="/trash">回收站<span class="nav-badge">3</span></a>
        </div>
      </div>
    </nav>
    <div class="header-actions">
      <button class="icon-btn" id="search-open" type="button" title="搜索 (Ctrl+K)" aria-label="搜索">…svg…</button>
      <button class="icon-btn theme-toggle" id="theme-toggle" type="button" aria-label="切换深色模式">…svg…</button>
      <form class="logout-form" method="post" action="/logout">…<button class="btn btn--ghost btn--sm">退出</button></form>
    </div>
  </div>
</header>
```

未登录时 `.site-nav` 换成「博客 / 归档 / 关于」（无「更多」下拉），`.header-actions` 只留主题切换与登录按钮。

### 提示条 / 消息

- 服务端 flash：`<div class="flash flash--ok">…</div>`（在 `.site-main` 顶部，含 `#flash`）。
- JS toast：`#toast-wrap` 里插入 `<div class="toast toast--ok">…</div>`，3 秒后加 `.is-out` 再移除。

### 命令面板（每个登录页都渲染，默认隐藏）

```html
<div class="palette" id="palette" hidden>
  <div class="palette__backdrop" data-palette-close></div>
  <div class="palette__box" role="dialog" aria-modal="true" aria-label="快速搜索">
    <form class="palette__form" method="get" action="/search">
      <input class="palette__input" id="palette-input" type="search" name="q" autocomplete="off"
             placeholder="搜索笔记…" data-search-url="/api/search">
    </form>
    <div class="palette__results" id="palette-results"></div>
    <div class="palette__hint">
      <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span><span><kbd>Enter</kbd> 打开</span>
      <span><kbd>Ctrl</kbd>+<kbd>Enter</kbd> 看全部结果</span><span><kbd>Esc</kbd> 关闭</span>
    </div>
  </div>
</div>
```
结果项由 JS 渲染：`<a class="palette__item" href="/notes/12"><span class="palette__item-title">标题</span><span class="palette__item-meta">更新于 …</span></a>`，
命中项加 `.is-active`（键盘高亮）。

---

## 3. 页面清单与 DOM

### 3.1 `dashboard`（`/notes` 笔记列表，需登录）

```html
<div class="page-head">
  <div><h1 class="page-title">我的笔记</h1><p class="page-sub">共 N 篇 · 已公开 M 篇</p></div>
  <div class="page-actions">
    <a class="btn btn--ghost" href="/export/zip">导出</a>
    <a class="btn btn--primary" href="/notes/new">新建笔记</a>
  </div>
</div>
<section class="stats-strip">
  <div class="stat"><div class="stat__value">128</div><div class="stat__label">笔记</div></div>
  …（字数 / 已公开 / 连续写作天数）…
</section>
<form class="toolbar" method="get" action="/notes"> … 搜索框 + select + 标签 chips … </form>
<div class="note-grid">
  <article class="note-card is-pinned"> … </article>
</div>
<nav class="pagination"> … </nav>
```

筛选控件（GET 表单，提交即筛选）：
- `<input class="input" type="search" name="q">`
- `<select class="select" name="status">` 选项：`""`（全部）/ `draft` / `saved`
- `<select class="select" name="sort">` 选项：`updated` / `created` / `words` / `title`（另有 `fav`、`tag`、`category` 三个筛选参数）
- `<select class="select" name="category">`
- 标签：`<a class="tag-pill is-active" href="…">标签名</a>`

批量操作：网格外层是 `<form id="batch-form" action="/notes/batch" method="post">`，每张卡左上角一个
`<input type="checkbox" name="note_ids" value="12">`（`note_card(..., selectable=True)` 时才有），
网格上方一条 `.batch-bar`（全选 / 已选 N 项 / `select[name=action]` / `input[name=tag]` / 执行）。
没有 JS 也能用；`app/static/js/batch.js` 只做增强，并用 `localStorage`（key `inknote.batch.v1:<path>`）跨页记住勾选。

笔记卡片：

```html
<article class="note-card is-pinned">
  <header class="note-card__head">
    <h2 class="card__title"><a href="/notes/12">标题</a></h2>
    <div class="note-card__badges">
      <span class="badge badge--draft">草稿</span>
      <span class="badge badge--public">已公开</span>
      <span class="badge badge--pinned">置顶</span>
      <span class="badge badge--star">星标</span>
    </div>
  </header>
  <p class="card__summary">自动摘要…</p>
  <div class="card__meta meta">
    <span class="meta__item">3 天前</span><span class="meta__divider">·</span>
    <span class="meta__item">1,204 字</span><span class="meta__divider">·</span>
    <span class="meta__item">约 5 分钟</span>
  </div>
  <div class="note-card__foot">
    <div class="pill-list">
      <a class="note-tag" href="/notes?tag=x" title="标签：x">x</a>
      <span class="note-tag note-tag--more" tabindex="0" title="全部标签：…" aria-label="还有 2 个标签">+2</span>
    </div>
    <div class="note-card__actions"> … 小按钮 / 表单 … </div>
  </div>
</article>
```

分页：

```html
<nav class="pagination" aria-label="分页">
  <a class="pagination__item" href="?page=1">上一页</a>
  <a class="pagination__item is-active" href="?page=2">2</a>
  <span class="pagination__gap">…</span>
  <a class="pagination__item" href="?page=3">下一页</a>
</nav>
```

空状态：

```html
<div class="empty-state">
  <div class="empty-state__icon">…svg…</div>
  <h2 class="empty-state__title">还没有笔记</h2>
  <p class="empty-state__text">…</p>
</div>
```

### 3.2 `editor`（新建 / 编辑共用）

```html
<form class="editor" id="editor-form" method="post" action="/notes"
      data-note-id="" data-is-new="1"
      data-preview-url="/api/preview"
      data-upload-url="/api/upload"
      data-autosave-url=""
      data-ai-summary-url="/api/ai/summarize"
      data-ai-tags-url="/api/ai/tags"
      data-unsaved-text="有未保存的改动">
  <div class="editor__bar">
    <input class="editor__title-input" id="note-title" name="title" placeholder="无标题笔记" autocomplete="off">
    <div class="editor__tabs" id="editor-tabs" role="tablist">
      <button class="editor__tab is-active" type="button" data-mode="write">编辑</button>
      <button class="editor__tab" type="button" data-mode="split">分栏</button>
      <button class="editor__tab" type="button" data-mode="preview">预览</button>
    </div>
    <span class="save-state" id="autosave-state" data-state="idle">已保存</span>
  </div>

  <div class="editor__toolbar" id="md-toolbar">
    <button class="md-tool" type="button" data-md="bold" title="加粗 Ctrl+B"><b>B</b></button>
    … data-md 取值见下表 …
    <label class="md-tool md-tool--upload" title="插入图片">
      <input class="sr-only" id="upload-input" type="file" accept="image/*" multiple>
      🖼
    </label>
  </div>

  <div class="editor__split" id="editor-split" data-layout="split">
    <div class="editor__pane editor__pane--write">
      <textarea class="editor__textarea" id="note-content" name="content" spellcheck="false"></textarea>
    </div>
    <div class="editor__pane editor__pane--preview">
      <div class="editor__preview prose" id="preview"></div>
    </div>
  </div>

  <div class="editor__status meta">
    <span id="word-count">0 字</span> · <span id="reading-time">约 1 分钟</span> ·
    <span id="cursor-info">第 1 行</span>
    <span class="editor__upload-hint">可直接拖拽 / 粘贴图片</span>
  </div>

  <details class="editor__settings" id="editor-settings">
    <summary>发布设置</summary>
    <div class="field-grid">
      <label class="field"><span class="field__label">标签</span>
        <input class="input" id="tags-input" name="tags" placeholder="用逗号或空格分隔">
      </label>
      <label class="field"><span class="field__label">分类</span>
        <input class="input" id="category-input" name="category" list="category-options">
      </label>
      <label class="field field--wide"><span class="field__label">摘要（留空自动生成）</span>
        <textarea class="textarea" id="summary-input" name="summary" rows="2"></textarea>
      </label>
      <label class="field"><span class="field__label">slug（URL 路径）</span>
        <input class="input" id="slug-input" name="slug" placeholder="python-note">
      </label>
      <label class="field"><span class="field__label">meta 描述</span>
        <input class="input" id="meta-description-input" name="meta_description">
      </label>
      <label class="checkbox"><input type="checkbox" name="is_public" value="1"> 公开到博客</label>
      <label class="checkbox"><input type="checkbox" name="is_pinned" value="1"> 置顶</label>
      <label class="checkbox"><input type="checkbox" name="is_starred" value="1"> 星标</label>
    </div>
    <div class="ai-row">
      <button class="btn btn--ghost btn--sm" type="button" id="ai-summary">AI 生成摘要</button>
      <button class="btn btn--ghost btn--sm" type="button" id="ai-tags">AI 推荐标签</button>
      <span class="ai-hint" id="ai-hint"></span>
    </div>
  </details>

  <div class="editor__actions">
    <button class="btn btn--ghost" type="submit" name="action" value="draft">存为草稿</button>
    <button class="btn" type="submit" name="action" value="save">保存</button>
    <button class="btn btn--primary" type="submit" name="action" value="view">保存并查看</button>
    <a class="btn btn--ghost" href="/notes">取消</a>
  </div>
</form>
```

`data-md` 工具按钮取值（点击后对选中的文本做包裹/插入）：
`bold` `italic` `strike` `h1` `h2` `h3` `quote` `ul` `ol` `task` `code` `codeblock` `link` `image` `wiki` `table` `hr`。

编辑器布局：`.editor__split[data-layout]` 由 JS 切换为 `write` / `split` / `preview`（属性值与 tab 的 `data-mode` 一致）。
`<body data-page="editor">` 时 `app.js` 不再接管快捷键（编辑器有自己的），但主题切换、命令面板仍工作。

`#autosave-state[data-state]` 取值：`idle` / `dirty` / `saving` / `saved` / `error` / `offline`，文案由 JS 写入元素文本。

### 3.3 `detail`（笔记详情 `/notes/{id}`）

```html
<div class="page-head">
  <div>
    <h1 class="page-title">标题</h1>
    <div class="post-meta meta">…时间 / 字数 / 阅读时长 / 分类…</div>
    <div class="pill-list">…tag-pill…</div>
  </div>
  <div class="page-actions">…编辑 / 公开 / 置顶 / 星标 / 历史 / 导出 / 删除（都是小表单）…</div>
</div>
<div class="post-layout">
  <article class="post-body prose" id="post-body"> …渲染后的 HTML… </article>
  <aside class="post-rail">
    <nav class="toc" id="toc" data-toc>
      <div class="toc__title">目录</div>
      <a class="toc__link" href="#anchor">标题</a>
      <a class="toc__link toc__link--l3" href="#anchor">子标题</a>
    </nav>
    <section class="rail-block"><h3 class="rail-block__title">相关笔记</h3>
      <ul class="related"><li class="related__item"><a href="/notes/9">标题</a><span class="related__why">共同标签：x</span></li></ul>
    </section>
    <section class="rail-block"><h3 class="rail-block__title">反向链接</h3>
      <ul class="backlinks"><li class="backlinks__item"><a href="/notes/3">标题</a></li></ul>
    </section>
    <section class="rail-block"><h3 class="rail-block__title">指向</h3>…待创建用 <span class="wikilink wikilink--missing">标题</span>…</section>
  </aside>
</div>
<nav class="post-nav">…上一篇 / 下一篇（笔记详情也可以有）…</nav>
```

正文里的 wikilink 渲染成 `<a class="wikilink" href="/notes/12">标题</a>`，
不存在时 `<span class="wikilink wikilink--missing" title="尚未创建">标题</span>`。

### 3.4 `blog`（公开首页 `/blog`）

```html
<section class="blog-hero">
  <h1 class="blog-hero__title">墨痕</h1>
  <p class="blog-hero__sub">一个人的笔记与写作</p>
  <div class="blog-hero__links"><a href="/feed.xml">RSS 订阅</a><a href="/blog/archive">归档</a></div>
</section>
<div class="blog-filters">…tag-pill 列表 + 搜索框…</div>
<div class="blog-list">
  <article class="post-card post-card--feature"> … 标题 / 摘要 / meta / tags … </article>
  <article class="post-card"> … </article>
</div>
<nav class="pagination">…</nav>
```

### 3.5 `post`（博客文章 `/blog/{slug}`）

结构与 3.3 相同（`post-layout` / `post-body prose` / `post-rail` + toc），
区别：顶部是 `<header class="post-header">`，含 `.post-title`、`.post-meta`、标签；
底部 `<nav class="post-nav">` 是真正的上一篇/下一篇：

```html
<nav class="post-nav">
  <a class="post-nav__item post-nav__item--prev" href="/blog/x">
    <span class="post-nav__label">上一篇</span><span class="post-nav__title">标题</span>
  </a>
  <a class="post-nav__item post-nav__item--next" href="/blog/y">…</a>
</nav>
```

### 3.6 `versions`（历史版本）

```html
<ul class="version-list">
  <li class="version-item">
    <div class="version-item__meta meta"><span>2026-09-01 12:00</span><span class="badge badge--draft">编辑前自动快照</span></div>
    <form method="post" action="/notes/12/versions/3/restore">…<button class="btn btn--sm">回滚到这一版</button></form>
    <a class="btn btn--sm btn--ghost" href="/notes/12/versions/3">查看</a>
  </li>
</ul>
```

版本对比页用：

```html
<div class="diff">
  <div class="diff__line ">上下文行</div>
  <div class="diff__line diff__line--add">+ 新增行</div>
  <div class="diff__line diff__line--del">- 删除行</div>
  <div class="diff__line diff__line--meta">@@ 元信息 @@</div>
</div>
```

### 3.7 其它页面

- `login`：`.login-page > form.login-card`（`input.input[name=password]`、`button.btn.btn--primary`）。整页无导航。
- `trash`：复用 `.note-card`，操作按钮是「恢复 / 彻底删除」，顶部有 `<form class="toolbar">` 里的「清空回收站」按钮。
- `tags`：`.tag-cloud` 里若干 `<a class="tag-pill">名称<span class="tag-pill__count">12</span></a>`，下面按标签分组的 `.note-grid`。
  管理入口：顶部 `.tag-admin`（`<details>`，内含 `.tag-admin__form` 改名/合并、`.tag-admin__cleanup` 清理），
  每个标签胶囊包在 `.tag-manage` 里，配 `.tag-manage__menu > .tag-manage__toggle` + `.tag-manage__panel`（`.tag-manage__form` 改名 / 删除）。
  搜索排序用 `.tag-toolbar`。全部是 `<details>` 实现，无 JS 可用。
- `search`：结果列表复用 `.note-card`，标题里命中词用 `<mark>`；语义搜索开关与引擎标注用 `.ai-search-status__engine*`。
- `ask`：`form.ask-form` + `.ask-layout > .ask-main`；回答是 `.ask-bubble.ask-bubble--ai`（正文 `.ask-bubble__body.ask-answer.prose`），
  提问是 `.ask-bubble--user`；引用来源在 `.ask-sources > .ask-source-list > .ask-source-card`（`.ask-source-card__title` / `__snippet`）；
  限定单篇时显示 `.ask-scope` / `.ask-scope-preview`，引擎标注用 `.ask-engine--semantic|.ask-engine--keyword`。
- `templates`：`.template-grid > .template-card`（名称/描述/预览/「用这个模板」按钮）。
- `archive`：`.archive-list > .archive-year > .archive-month`（月份 + `.archive-count`）。

---

## 4. `/api/*` 契约

所有 `/api/*` 都需要登录；写操作需要 `X-CSRF-Token` 头或 `_csrf` 表单字段。

### `GET /api/search?q=关键词&limit=20`

```json
{ "query": "关键词", "count": 2,
  "engine": "fts5-trigram | like",
  "items": [
  { "id": 12, "title": "标题", "url": "/notes/12", "snippet": "纯文本片段",
    "updated_at": "3 天前", "status": "draft", "is_public": false,
    "title_html": "含<mark>关键词</mark>的转义 HTML",
    "snippet_html": "同样带 <mark> 的片段" }
] }
```

### `POST /api/preview`

请求 `{ "content": "markdown", "title": "可选" }` →

```json
{ "html": "<p>…</p>", "toc": [{"level":2,"id":"anchor","text":"标题"}],
  "word_count": 120, "reading_minutes": 1, "excerpt": "…", "empty": false }
```

### `PATCH /api/notes/{id}` — 自动保存

请求 `{ "title": "…", "content": "…" }`（字段可选，只传改动过的） →

```json
{ "ok": true, "id": 12, "saved_at": "12:03", "updated_at": "刚刚",
  "word_count": 120, "reading_minutes": 1, "excerpt": "…" }
```

### `POST /api/upload` — multipart

字段名 `file`（可重复）。成功：

```json
{ "ok": true, "url": "/media/2026/09/ab12cd.png", "markdown": "![ab12cd.png](/media/2026/09/ab12cd.png)",
  "name": "ab12cd.png", "size": 20480, "deduped": false }
```
`deduped: true` 表示这份内容和已存在的图片一模一样，**没有写新文件**，`url` 指的是已有的那份。
失败：HTTP 4xx/5xx + `{ "ok": false, "error": "中文原因" }`。

### `POST /api/ai/summarize` → `{ "ok": true, "summary": "一句话" }`
### `POST /api/ai/tags` → `{ "ok": true, "tags": ["a","b","c"] }`

未配置 AI 时返回 HTTP 503 + `{ "ok": false, "error": "尚未配置 AI 服务" }`，
JS 需要把 `error` 文案显示在 `#ai-hint` 里（`.ai-hint`，失败时加 `.is-error`）。

---

## 5. JS 行为清单

### `app.js`（所有页面）

| 功能 | 触发 | 实现要点 |
| --- | --- | --- |
| 深色模式切换 | `#theme-toggle` 点击 | `<html data-theme>` 切 `light`/`dark`；`localStorage['inknote-theme']` 存 `light`/`dark`/`auto`；首次加载若 localStorage 无值则跟随 `prefers-color-scheme`，并监听其变化 |
| 命令面板 | `Ctrl/Cmd+K` 或 `#search-open` | 显示 `#palette`（去掉 `hidden` 加 `.is-open`），聚焦 `#palette-input`；输入 200ms 防抖后 `fetch` `/api/search`，渲染结果，↑↓ 移动 `.is-active`，Enter 跳转，Esc 关闭；点击 backdrop 关闭 |
| toast | `window.InkNote.toast(msg, kind)` | 插入 `#toast-wrap`，3s 后 `.is-out` 并移除 |
| 危险操作确认 | 任意 `form[data-confirm]` 提交 | 用 `confirm(form.dataset.confirm)`，取消则阻止提交 |
| 代码块复制 | `.prose` 内每个 `pre` | 右上角插入 `<button class="code-copy">复制</button>`，点击写入剪贴板并 toast（需处理 `navigator.clipboard` 不可用时退化为 `select+execCommand`） |
| 目录高亮 | `#toc[data-toc]` 存在时 | `IntersectionObserver` 观察正文标题，给对应 `.toc__link` 加 `.is-active`，并处理点击平滑滚动（尊重 `prefers-reduced-motion`） |
| 离线提示 | `online`/`offline` 事件 | 切换 `#offline-banner` 的 `hidden` |
| 标题字数自适应 | `.editor__title-input` | 可选：input 时按长度缩小字号（class `is-long` / `is-xlong`） |
| 列表卡片键盘可达 | `.note-card a.card__title` | 不做额外处理（原生 a 即可） |

### `editor.js`（仅 `body[data-page="editor"]`）

| 功能 | 说明 |
| --- | --- |
| 实时预览 | 输入后 300ms 防抖 `POST /api/preview`，把 `html` 写入 `#preview`；请求期间保留旧内容；空内容显示占位提示 |
| 布局切换 | `#editor-tabs button[data-mode]` → 设置 `#editor-split[data-layout]`；小屏幕默认 `write`（用 `matchMedia('(max-width: 900px)')`） |
| 字数/阅读时长 | 本地即时估算（中文按字、英文按词），不需要等接口返回；接口返回后再校正 |
| 自动保存 | 仅当 `data-note-id` 非空：停止输入 2.5s 后 `PATCH /api/notes/{id}`；`Ctrl/Cmd+S` 立即保存并 `preventDefault`；保存中 `data-state="saving"`，成功 `saved`，失败 `error`（下次输入重试） |
| 未保存拦截 | 有改动且未保存时 `beforeunload` 提示；表单提交时不算 |
| Markdown 工具条 | `data-md` 见上表，对 `textarea` 的选区做包裹；`Tab` 缩进两个空格；`Ctrl+B`/`Ctrl+I`/`Ctrl+K`(链接) 快捷键 |
| 拖拽/粘贴上传 | 拖到 `.editor__pane--write` 或 textarea 时加 `.is-dragover`；`drop` / `paste` 里的图片走 `POST /api/upload`（可多张，串行），成功后按顺序插入 `\n![name](url)\n`，并 toast「已插入 N 张图片」 |
| 图片工具按钮 | `#upload-input` change → 上传 |
| AI 摘要 / 标签 | `#ai-summary` / `#ai-tags` 调接口，把结果写进 `#summary-input` / `#tags-input`，未配置时把错误文案写进 `#ai-hint` 并加 `.is-error` |
| 历史/离开提醒 | 编辑器内 `Ctrl+Enter` 提交「保存并查看」（`name=action value=view`） |

---

## 6. 无障碍与响应式底线

- 所有可点击元素：`<button>` 或 `<a>`，图标按钮必须有 `aria-label`/`title`。
- 焦点样式必须可见（`--brand` 描边），不要 `outline: none` 且不留替代。
- 断点：`1200px`（编辑器右栏收起）、`1024px`（正文 + 目录变单列）、`900px`（编辑器分栏变单栏 tab、导航收成两行）、`640px`（卡片与表单单列、字号略缩）。
- `@media print`：只保留正文与标题。
- `@media (prefers-reduced-motion: reduce)`：动画过渡时长归零。
