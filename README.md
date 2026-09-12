# 墨痕 InkNote · 个人笔记 + 博客

一个自己用的笔记站：**Markdown 写笔记，勾一下公开就变成博客**。
后端 FastAPI + SQLite，前端服务端渲染（Jinja2 + 少量原生 JS），没有构建步骤、没有前端框架、不引用任何 CDN。

> 解决的问题：记了就忘、格式混乱 —— 所以核心是「写起来顺、找得到、能对外展示」。

> **只想用、不关心代码？** 看 👉 [使用说明.md](使用说明.md)：怎么启动、每个按钮干嘛、Markdown 速查、常见问题。
> 本文档偏开发：架构、配置、部署、测试与设计取舍。

---

## 快速开始

```bash
# 1. 安装依赖（建议用虚拟环境）
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt

# 2. 配置
cp .env.example .env              # 至少改掉 INKNOTE_PASSWORD

# 3. 启动
python run.py                     # http://127.0.0.1:8000
```

打开 <http://127.0.0.1:8000/>：

- `/notes` —— 笔记后台（需要密码登录）
- `/blog` —— 公开博客（无需登录）
- `/login` —— 登录页，默认密码是 `inknote`（**请务必在 `.env` 里改掉**）

想更安全一点，用哈希存密码：

```bash
python run.py --hash-password '你的密码'
# 把输出填进 .env 的 INKNOTE_PASSWORD_HASH，并删掉 INKNOTE_PASSWORD
```

常用启动参数：

```bash
python run.py --port 9000 --open    # 换端口并自动打开浏览器
python run.py --reload              # 开发模式（改代码自动重启）
```

---

## 功能一览

### 写笔记

| 能力 | 说明 | 源码位置 |
| --- | --- | --- |
| Markdown 编辑器 | 左写右预览，三档视图（编辑 / 分栏 / 预览），小屏幕自动切单栏 | `templates/notes/editor.html`、`static/js/editor.js` |
| 实时预览 | 输入 300ms 防抖后调 `POST /api/preview`，服务端渲染再回填，保证预览与最终效果一致 | `static/js/editor.js`、`routers/api.py` |
| 代码高亮 | 服务端 Pygments（`friendly` / `one-dark` 两套配色），不依赖 CDN | `markdown_render.py`、`scripts/build_highlight_css.py` |
| 图片上传 | 工具条选图 / 拖拽 / 直接粘贴，自动上传并插入 `![名字](url)`，多张串行 | `static/js/editor.js`、`routers/api.py::upload` |
| 自动保存 | 已有笔记停笔 2.5s 自动 `PATCH /api/notes/{id}`；新建笔记没有 id，不会自动保存，只在离开时提醒。状态文案为「未保存的改动 / 保存中… / 已保存 / 保存失败，将重试」 | `static/js/editor.js` |
| 快捷键 | 编辑器正文区：`Ctrl+S` 保存、`Ctrl+B/I` 加粗/斜体、`Ctrl+K` 插入链接、`Ctrl+Enter` 保存并查看、`Tab` 缩进；`Ctrl/Cmd+K` 在非编辑页面或焦点不在输入控件时呼出命令面板 | `static/js/editor.js`、`static/js/app.js` |
| 笔记模板 | 内置 6 个（空白笔记、读书笔记、周报、会议记录、技术笔记 / 踩坑记录、每日复盘），可自己增删改 | `repo.DEFAULT_TEMPLATES`、`templates/templates.html` |
| 自动摘要 | 保存时自动截取正文开头做摘要；摘要与 `make_excerpt(content)` 不一致时视为手写并保留 | `repo.update_note`、`repo._looks_auto_summary`、`markdown_render.make_excerpt` |

### 看笔记

- **列表页**：按更新时间倒序，置顶优先；卡片显示标题、摘要、时间、字数、预计阅读时长、标签、状态徽章。
- **详情页**：完整渲染 Markdown，右侧自动生成 **TOC 目录**（渲染 1–4 级标题；滚动高亮只观察正文 h2/h3/h4），长文尤其有用。
- **统计面板**：笔记总数、累计字数、连续写作天数、今日更新、标签数。可单独访问 `/stats`。
- **筛选**：关键词、状态（草稿/已保存）、分类、标签、星标/置顶/已公开，可叠加；排序支持更新时间 / 创建时间 / 字数 / 标题。

### 组织与检索

- **标签**：可填多个（逗号或空格分隔），正文里写 `#标签` 也会自动收集。
  编辑器里还有「常用标签」胶囊（点一下加/去掉，服务端按使用次数给）与输入自动补全。
- **标签页**（`/tags`）：标签云 + 按标签分组；点标签筛笔记，支持按名称/使用次数排序与关键字过滤。
  页面顶部「管理标签」可**重命名**（改成已存在的名字就是**合并**，关系取并集不会重复）、
  **删除**（从所有笔记上摘掉）、**清理未使用的标签**；每个标签胶囊旁的 ✎ 也能单独改名/删除。
  这些入口都是 `<details>` 实现，**没有 JS 也能用**。
- **分类**：`category` 字段（如「技术 / 读书 / 生活」），列表页可筛选。
- **全文搜索**：SQLite FTS5（`trigram` 分词，对中文友好）+ 统一 Python 打分：标题每个命中词 8 分、标签 4 分、正文每次命中 1 分（同一词最多再叠加 5 次 ×0.4），置顶 +1.5、星标 +0.5。
  查询词少于 3 个字符时（比如「笔记」）自动退回 `LIKE` 路径，保证中文双字词一定能搜到；若运行环境没有 `trigram` tokenizer，则所有查询都会走 `LIKE`。
- **按意思搜**：搜索页可开「按意思搜」（`/search?mode=semantic`，默认关闭）。配了 embedding 模型且建过索引时走向量召回，
  否则自动回退关键词搜索，并在页面上标明**这次用的是哪种引擎**以及为什么回退。
- **搜索结果可以接着筛**：搜索页的结果卡片带完整操作按钮（星标 / 置顶 / 公开 / 编辑 / 历史 / 删除），
  并且能像列表页那样按 **星标 / 置顶 / 已公开 / 草稿**、**标签**、**分类**、**状态** 再缩小范围，或按最近更新 / 创建时间 / 字数 / 标题排序。
  筛选与排序在 Python 侧做，**两种检索引擎（关键词、语义）都适用**；默认保持相关度顺序，不筛就是最相关的排最前。
- **快捷搜索**：`Ctrl/Cmd+K` 在非编辑页面或焦点不在输入控件时呼出命令面板（↑↓ 选择、Enter 跳转）；编辑器正文区的 `Ctrl/Cmd+K` 改为插入 Markdown 链接，其它输入框内不触发面板。
- **双链**：正文写 `[[另一篇标题]]` 或 `[[标题|显示名]]`，详情页底部显示「反向链接」和「指向」；
  目标还不存在时渲染成虚线并给一个「创建 →」入口。
- **相关笔记**：详情页「你可能还想看」。配了向量模型时优先用**语义相似度**召回并标注「语义推荐」，
  否则回退到「共同标签 ×2 + 共同引用 ×1」的关键词算法（标注推荐理由），**行为不会变差**。

### 发布为博客

- 每篇笔记一个「公开到博客」开关，公开后出现在 `/blog`，**公开页面无需登录**。
- 博客首页：标签筛选、关键词搜索、按月归档；支持 `?month=2026-09` 直接筛月份。
- 文章页：上一篇 / 下一篇导航、相关文章、引用本文的笔记、TOC。
- **SEO**：可自定义 `slug`（URL 路径）与 `meta` 描述，输出 canonical、Open Graph、`sitemap.xml`、`robots.txt`。
- **订阅源**：`/feed.xml`（Atom 1.0）与 `/rss.xml`（RSS 2.0），各输出最近 30 篇公开文章的完整正文。
- 公开页面的双链只指向**已公开**的笔记，草稿的存在不会泄露。

### 数据安全

- **回收站**：删除是软删除，进回收站可恢复；默认 30 天后启动时自动彻底清理。
- **历史版本**：有改动时在保存前自动存快照（手动保存触发的手动快照会累积；自动保存的快照 10 分钟内合并成一条，避免刷屏），
  可查看与当前内容的逐行差异并一键回滚；回滚前也会先把当前内容存一份。默认保留最近 20 个版本。
- **导出**：`/export/zip` 把未删除笔记（含草稿）打包成 Markdown——
  `index.md` 目录 + `notes/YYYYMMDD-标题.md`（日期取创建时间，重名追加 `-2`、`-3`；带 YAML front matter）+ `notes.json` 结构化元数据 + `README.txt`。
  走的是 `repo.all_notes()`（**不分页、不截断**），笔记再多也会全部导出。
  导出 zip 里还带 `media/` 目录（图片按 `uploads/` 下的相对路径打包），导入时一并还原。
  单篇也可以直接 `/notes/{id}/export.md` 下载。
- **置顶 / 星标**：重要的笔记可以置顶（列表最前）或标星，并作为快捷筛选。

### 备份与恢复

- **导出**：`/export/zip`（打包 Markdown）、`/export/json`（结构化，含标签/状态）
- **导入**：`/backup` 页面可拖入导出的 zip / json / 单篇 `.md`，按 **slug → 标题** 匹配后**合并**回库
  （已有则更新、没有则新建，**绝不删除**用户现有笔记；同一份备份重复导入不会翻倍）
- 解析在你自己的机器上完成，结果按「新建 / 更新 / 跳过 / 失败原因」汇报

### 站点设置

`/settings` 的「站点信息」区块可改：站名、副标题、描述、作者、站点地址、每页条数、回收站保留天数。
机制与 AI 配置一致：**设置页（meta 表）→ `.env` → 默认值**，保存后立即生效（`site_settings.bootstrap()`
会把值写回 `settings` 对象，所以模板无需改动）。非法值（条数越界、站名超长等）会被拒绝并回显，不会写坏配置。

### 图片管理

`/images`：列出 `data/uploads` 里的图片，显示尺寸/上传时间/**被哪几篇笔记引用**，
标出**孤立图片**并支持一键清理。所有删除都限制在 uploads 目录内（`Path.resolve()` 防目录穿越），
被引用的图片默认拒删。

### 批量操作

笔记列表页支持多选（每卡一个复选框 + 顶部操作条）：添加/移除标签、公开/取消公开、
置顶/取消置顶、加星标/取消星标、移入回收站。服务端 `POST /notes/batch` 对脏输入
（空 / 非数字 / 超 64 位整数 / 不存在的 id）一律跳过而不是 500，最后汇报「已处理 N 篇、跳过 M 篇」。
**无 JS 也能用**（原生 `<select>` + 提交），`static/js/batch.js` 只做实时计数与全选增强。

### 自动备份与回滚

`data/backups/` 里是**整库快照**（标准 SQLite 文件），启动时自动备一次、之后每小时检查、超过 24h 再备，
滚动保留 7 份自动备份（手动备份不参与滚动）。`/backup` 页可手动备份 / 下载 / **回滚** / 删除。

**实现要点**：快照用 `sqlite3.Connection.backup()`（不是 `shutil.copy` —— WAL 下复制文件可能拿到不一致的快照）；
备份源路径通过 `PRAGMA database_list` 从连接本身取出（多库场景不会备错库）；
回滚前自动存一份 `before-restore` 现场。回滚是原地写回当前连接，不需要重启。

### 页面里改密码

优先级 **库 meta(`account.password_hash`) → `.env` 的 `INKNOTE_PASSWORD_HASH` → `.env` 的 `INKNOTE_PASSWORD`**。
pbkdf2_sha256(200k) + 随机盐，`hmac.compare_digest` 比较，旧密码连错 5 次锁 5 分钟。
`describe()` 只回报来源，**绝不返回哈希**。改密码不影响已签发的会话 cookie。

### 批量操作

`/notes` 多选 + 顶部操作条：打/删标签、公开/取消、置顶/取消、星标/取消、移入回收站。
还支持「**选中全部筛选结果**」（`all=1` + 当前筛选参数，单次上限 500 篇；
无筛选条件时必须 `confirm_all=1` 二次确认）。脏数据（空/非数字/超 64 位/重复/不存在）一律跳过而不 500。

### 其它小改进

- `/images`：分页（每页 24）+ 复制 Markdown 链接 + 引用它的笔记可展开
- 删笔记前提示「正文引用了 N 张图片」（卡片确认框 + 详情页，`image_count` Jinja 过滤器）
- 回收站角标 / 卡片上的版本历史入口
- `scripts/check.py`：语法 + 样式审计 + 测试 + 应用冒烟 + 数据库完整性 + 环境摘要，一条命令跑完

### 日志与可观测性

日志写 `data/logs/inknote.log`（`TimedRotatingFileHandler` 按天切分、默认保留 14 天，
`INKNOTE_LOG_LEVEL` / `INKNOTE_LOG_DAYS` 可调），同时保留控制台输出。
`run.py` 启动时与 `app/main.py` 的 lifespan 里都会调用 `logging_setup.setup_logging()`（幂等），
所以无论用哪种方式启动都会落盘。

### 导出带图片

`/export/zip` 现在包含 `media/` 目录（只含**正文真正引用**的图片）+ `BACKUP-README.txt`；
`notes.json` 增加顶层 `media` 数组。导入时还原到 `data/uploads/`，路径做 `resolve()` 越界校验，
同内容跳过、不同内容并存（`-1` 后缀），结果里报 `media_added/media_skipped/media_renamed`。

### 脱敏备份

`create_snapshot(..., sanitized=True)` 会在快照文件里把 `ai.api_key` 与 `account.password_hash` 清空
（先写到 `.part` 临时文件、用独立连接改、再原子改名，**全程不动当前库**）。
页面 `POST /backup/create-sanitized`，文件名带 `-sanitized`，列表里有「已脱敏」徽章。
**不做加密**：标准库没有安全的对称加密，手搓不可靠 —— 真要保密请用系统级加密容器。

### 其它

- **向量索引自动跟进**：`ai_embed.maybe_auto_index(conn, interval_minutes=30)`，后台线程启动时查一次、之后每 30 分钟，
  增量化（只重算改过的），没配模型时静默返回 None
- **回收站倒计时**：`purge_countdown` Jinja 过滤器，边界与 `repo.purge_expired_trash` 对齐
- **批量跨页勾选**：`localStorage` 记住选中项（key `inknote.batch.v1:<path>`），提交时注入跨页 id
- **图片内容去重**：sha256 边车文件（`x.png.sha256`），上传命中即复用；`/images` 可清理未被引用的副本
- **手机工具栏**：≤640px 收成「更多 ▾」，纯 CSS（`:checked ~`），桌面端 DOM 变了但布局像素级一致

### AI 功能（可选）

**在网页上配**：登录后点页脚「设置」（`/settings`）：
点预设按钮一键填好服务商地址与模型 → 填 Key → 「测试连接」分四步体检（连通性/密钥、模型名、对话）
→ 「保存配置」立即生效。配置存在数据库 `meta` 表里；也支持 `.env`
（`INKNOTE_AI_BASE_URL` / `INKNOTE_AI_API_KEY` / `INKNOTE_AI_MODEL` / `INKNOTE_AI_EMBED_MODEL`），
**生效优先级：设置页保存的 → `.env` → 未配置**，页面上会标明每个字段来自哪里。

对接任意 **OpenAI 兼容** 服务：`/chat/completions`（对话）、`/embeddings`（向量）、`/models`（列模型）。

设置页还能做：

- **分任务模型**：摘要 / 打标签 / 问答各用一个模型（省钱：小模型干轻活）
- **向量检索**：填 embedding 模型 + 点「重建索引」，「问笔记」就从**字面匹配**升级成**按意思召回**
  （笔记切成块、向量存本地 SQLite BLOB、纯 Python 余弦排序）；不可用时自动回退关键词检索并标注
- **用量统计**：本月调用次数与 token（输入/输出）、按任务拆分、最近 30 天柱状图
- **两个安全开关**：`发送前脱敏`（把 `sk-*`、`password=`、手机号等打码后再发出）、
  `只允许本机模型`（挡住指向公网的地址，防止误发笔记）
- **错误翻译**：401/403/404/429/5xx 都翻成「人话 + 下一步」，并附服务端原话

「问笔记」（`/ask`）支持**多轮追问**（历史一起带给模型）、**SSE 流式输出**（边生成边显示）、
**一键把回答存成笔记**（正文含回答 + 参考来源的双链）。
详情页的「**问这篇**」按钮（`/ask?note=<id>`）把检索范围**限定在这一篇**，页面上会显示「正在问：《标题》」，
并可一键切回问全部笔记。

| 功能 | 入口 | 说明 |
| --- | --- | --- |
| 自动摘要 | 编辑器里「AI 生成摘要」 | 生成一句话摘要填进摘要框，你可以改完再保存 |
| 自动打标签 | 编辑器里「AI 推荐标签」 | 推荐 3–5 个标签，会优先复用你已经用过的标签 |
| 起标题 / 推荐分类 | 编辑器里「AI 起标题」「AI 推荐分类」 | 填进标题框与分类框（分类会优先复用你已有的分类）。模型爱加的「标题：」前缀、引号、代码围栏都会在服务端洗掉 |
| 内联 AI 写作 | 编辑器工具栏「✦ AI」 | 「接着写」（`Ctrl+J`）；选中正文时浮出「润色 / 精简 / 扩写 / 翻译 / 更正式」。
**先预览再插入**，不会直接改你的正文；纯增强，JS 挂了照样能写笔记 |
| 智能问答 | `/ask`「问笔记」 | 先在你的笔记里粗排检索（中文按 2/3/4 元字组命中打分），再把命中的笔记作为资料让 AI 作答，并列出引用来源 |
| 提示词可改 | 设置页「提示词」区块 | 摘要 / 标签 / 起标题 / 推荐分类 / 问答五套模板都能改（含占位符），占位符写错或缺失会自动回退内置默认，带「恢复默认」 |

模型调用还有一条**回退链**：主模型失败（模型不存在 / 超时 / 5xx）时自动用「通用模型」再试一次，两次都失败才报错，日志里能看出最终用了哪个模型。

没配置时点击编辑器 AI 按钮会得到 503「尚未配置 AI 服务」提示（不会发出外部请求）；`/ask` 会显示提示，并退化成纯检索结果页。AI 调用是**服务端**发起的（标准库 `urllib`，不引入额外依赖），API Key 不会出现在前端。

> 注：AI 入口不会因为未配置而隐藏，只是点击后提示未配置。

---

## 目录结构

```text
inknote/
├── run.py                       启动脚本（也是 --hash-password 工具）
├── requirements.txt             运行依赖（7 个直接依赖）
├── requirements-dev.txt         开发依赖（pytest / httpx）
├── 使用说明.md                   给人看的使用手册（怎么启动、怎么用）
├── README.md                    本文档（架构、配置、部署、测试）
├── .env.example                 配置样例（全部环境变量都带中文注释）
├── app/
│   ├── __init__.py
│   ├── main.py                  create_app()、中间件、异常处理、路由挂载
│   ├── config.py                环境变量 / .env 读取
│   ├── db.py                    SQLite 连接与建表（含迁移位）
│   ├── repo.py                  数据访问层：笔记、标签、版本、双链、模板、统计
│   ├── search.py                FTS5（trigram）+ LIKE 兜底、打分、摘要、高亮
│   ├── markdown_render.py       Markdown → HTML / TOC / 纯文本 / 字数，含安全过滤
│   ├── security.py              pbkdf2 密码、签名 Cookie 会话、登录限流
│   ├── deps.py                  FastAPI 依赖：连接、登录校验、CSRF
│   ├── templating.py            Jinja 环境、过滤器、统一 render()
│   ├── utils.py                 时间、slug、标签、分页、diff 等工具
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── ai_admin.py          设置页（AI 配置 / 模型列表 / 体检 / 用量）+ /api/ai/*
│   │   ├── ai_embed.py          向量索引状态 / 重建 / 清空
│   │   ├── ai_inline.py         编辑器内联 AI：接着写 / 润色 / 精简 / 扩写 / 翻译
│   │   ├── backup.py            备份与恢复页 + 导入
│   │   ├── media.py             图片管理页（/images）
│   │   ├── ask.py               问笔记：多轮追问 + SSE 流式 + 存成笔记
│   │   ├── auth.py              登录 / 登出
│   │   ├── notes.py             笔记 CRUD、开关、版本、回收站、单篇导出
│   │   ├── pages.py             标签页、搜索页、模板管理、问笔记、导出、统计
│   │   ├── api.py               JSON 接口：预览、自动保存、上传、快捷搜索、AI
│   │   ├── blog.py              公开博客：首页、归档、标签、文章
│   │   └── meta.py              feed / rss / sitemap / robots / about / health
│   ├── services/
│   │   ├── __init__.py
│   │   ├── content.py           详情页上下文（个人页与公开页共用）
│   │   ├── export.py            Markdown 打包导出
│   │   ├── feeds.py             Atom / RSS / sitemap（标准库 ElementTree）
│   │   ├── db_backup.py         整库快照 / 滚动保留 / 回滚
│   │   ├── account.py           登录密码（页面改的优先于 .env）
│   │   ├── importer.py          导入 zip/json/md 并合并回库
│   │   ├── media.py             图片库：扫描、引用统计、孤立清理
│   │   ├── site_settings.py     站点信息（页面配置覆盖 .env）
│   │   ├── ai.py                配置层 + OpenAI 兼容客户端 + 摘要/标签/问答（含提示词模板、模型回退链）
│   │   ├── ai_inline.py         内联 AI：接着写 / 润色 / 精简 / 扩写 / 翻译 / 更正式
│   │   ├── ai_embed.py          向量检索：分块、embedding、余弦排序、增量重建
│   │   ├── ai_related.py        语义相关笔记（拿不到向量时回退关键词算法）
│   │   ├── ai_search.py         语义搜索（不可用时返回 None，由调用方回退关键词）
│   │   ├── ai_chat.py           问笔记的会话与消息存储
│   │   └── ai_usage.py          用量统计（每次调用记一笔）
│   ├── templates/               24 个 Jinja 模板（base + 页面 + 片段宏 + 设置页 + 使用说明页）
│   └── static/
│       ├── css/style.css        设计令牌与全部样式（含深色主题、响应式、打印）
│       ├── css/highlight.css    代码高亮配色（脚本生成）
│       ├── favicon.svg          站点图标
│       └── js/app.js（598 行）、editor.js（1334 行）、settings.js（498 行）、ask.js（318 行）、batch.js（249 行）原生 JS，无依赖
├── docs/                        6 份：前端契约（frontend-contract.md）+ 各轮并行开发的接口冻结（round3/4/5、ai、new-features）
├── scripts/
│   ├── build_highlight_css.py   用 Pygments 生成 highlight.css
│   ├── audit_css.py             交叉审计：模板 class 是否都有样式
│   └── check.py                 自检脚本（--quick 跑一组快速检查）
├── tests/                       conftest.py + 37 个测试文件，共 616 个 pytest 用例
└── data/                        运行期生成：inknote.db、uploads/、backups/、logs/、.secret
```

---

## 配置项（`.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `INKNOTE_PASSWORD` | `inknote` | 登录密码（明文） |
| `INKNOTE_PASSWORD_HASH` | 空 | 优先于上面的明文密码，用 `python run.py --hash-password` 生成 |
| `INKNOTE_SECRET` | 自动生成 | 会话签名密钥；留空则在 `data/.secret` 里生成并复用，重启不掉登录 |
| `INKNOTE_SESSION_DAYS` | `30` | 会话有效期（天），写入 Cookie 的 max-age |
| `INKNOTE_HOST` | `127.0.0.1` | `run.py` 默认监听地址，命令行 `--host` 可覆盖 |
| `INKNOTE_PORT` | `8000` | `run.py` 默认监听端口，命令行 `--port` 可覆盖 |
| `INKNOTE_DEBUG` | `0` | 读取但当前源码未实际使用，预留给调试 |
| `INKNOTE_TITLE` / `INKNOTE_SUBTITLE` / `INKNOTE_DESCRIPTION` / `INKNOTE_AUTHOR` | 见样例 | 站点信息 |
| `INKNOTE_BASE_URL` | `http://127.0.0.1:8000` | 生成 RSS / sitemap 用的对外地址，部署时改成真实域名 |
| `INKNOTE_PER_PAGE` | `12` | 列表每页条数 |
| `INKNOTE_DATA_DIR` | `./data` | 数据库与上传图片的存放目录 |
| `INKNOTE_MAX_UPLOAD_BYTES` | `8388608` | 单张图片上限（8MB） |
| `INKNOTE_VERSION_KEEP` | `20` | 每篇保留的历史版本数 |
| `INKNOTE_TRASH_DAYS` | `30` | 回收站保留天数 |
| `INKNOTE_WORKERS` | `1` | 后台线程总开关：每小时检查自动备份、每 30 分钟跟进向量索引；设 `0` 关掉 |
| `INKNOTE_AI_BASE_URL` / `INKNOTE_AI_MODEL` | 空 | 两者都非空才启用 AI；`BASE_URL` 写到 `/v1` 即可（代码会自动补 `/chat/completions`）。`INKNOTE_AI_API_KEY` 可选，本地服务可留空 |
| `INKNOTE_AI_TIMEOUT` | `45` | AI 请求超时秒数 |

`.env` 是给 `python run.py` 用的；**已存在的系统环境变量优先级更高**，所以也能直接 `export INKNOTE_PASSWORD=xxx` 再启动。上面这些键（含 `INKNOTE_HOST`、`INKNOTE_PORT`、`INKNOTE_DEBUG`、`INKNOTE_SESSION_DAYS`）`.env.example` 里都有对应条目，直接照抄改成自己的值即可；配置解析器支持任意 `KEY=VALUE`。

---

## 部署

单机自用的话，`python run.py` 就够了。想跑在服务器上：

```bash
# 1. 用 uvicorn 常驻
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

# 2. systemd（/etc/systemd/system/inknote.service）
[Unit]
Description=InkNote
After=network.target

[Service]
User=you
WorkingDirectory=/srv/inknote
Environment="INKNOTE_BASE_URL=https://notes.example.com"
Environment="INKNOTE_DATA_DIR=/srv/inknote/data"
Environment="INKNOTE_PASSWORD_HASH=pbkdf2_sha256$..."
ExecStart=/srv/inknote/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

前面套一层 Nginx 反代（记得带上 `X-Forwarded-Proto`，并放开上传体积）：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 16m;
}
```

注意事项：

- `--workers 1`：SQLite + 本地文件上传，单进程最省心；SQLite 已开 WAL，多进程不是不能用，但写入会串行，个人站通常没必要加 workers，也别开 `--reload`。
- **登录限流是进程内存的**，多进程下每个进程各自计数（个人用无所谓）。
- 默认关闭了 `SessionMiddleware` 之类的额外组件，只依赖 `Starlette` 自带的 Cookie 处理。
- FastAPI 自带的 `/docs`、`/redoc`、`/openapi.json` 默认开启且**无需登录**（2026-09-12 实测均返回 200），不受后台登录依赖保护。不想暴露就把 `app/main.py` 里 `FastAPI(...)` 的 `docs_url`、`redoc_url`、`openapi_url` 设为 `None`。

---

## 数据与备份

所有数据都在 `INKNOTE_DATA_DIR`（默认 `./data`）里：

```text
data/
├── inknote.db      SQLite 数据库（笔记、标签、版本、双链、模板）
├── inknote.db-wal  开启 WAL 后的预写日志
├── inknote.db-shm
├── .secret         会话签名密钥（别泄露，否则别人能伪造登录 Cookie）
└── uploads/        上传的图片，按 YYYY/MM/ 分目录，文件名是内容 SHA256 前 16 位
```

- **备份**：停服后直接拷 `data/` 整个目录最稳（WAL 模式下只拷 `.db` 可能丢最近的事务）；
  或者在线用 `sqlite3 data/inknote.db ".backup data/backup.db"`。
- **迁移**：把 `data/` 拷到新机器，`.env` 里 `INKNOTE_DATA_DIR` 指过去即可。
- 想要人类可读的存档：笔记后台点「导出」，或访问 `/export/zip`。

---

## 设计说明（温暖编辑风）

界面按「一本摊开的米白纸书」来做，不是常见的白底卡片流：

- **配色**：底色奶油米白 `#FAF6EF`（不用纯白），卡片纸面 `#FFFDF8`，正文暖褐黑 `#2E2A26`（不用纯黑），
  强调色砖红 `#B5533C` 只出现在链接、按钮、标签和焦点描边上，次强调墨绿 `#4A6C5A` 用于成功态。
- **深色模式**：深棕 `#1F1B17` 底 + 米白 `#EDE4D6` 字，同样是「纸与墨」而不是纯黑白；右上角一键切换，
  选择存进 `localStorage`，首次访问跟随系统 `prefers-color-scheme`。首屏前有内联脚本预设主题，不会闪白。
- **字体分工**：标题、正文、大数字用衬线栈（`Georgia / Source Serif Pro / 思源宋体 / 宋体`），
  导航、按钮、表单用无衬线，代码与计数用等宽。
- **排版呼吸感**：正文行高 `1.75`，段间距 `1.1em` 以上，正文宽度限制在 `68ch` 左右，长文右侧留出目录栏。
- **细节**：卡片 12px 圆角 + 柔和阴影（悬停轻微上浮），标签一律胶囊形，元信息用「时间 · 字数 · 约 N 分钟」的杂志式行。
- 所有颜色、圆角、阴影、字体都集中在 `style.css` 的 `:root` 与 `[data-theme="dark"]` 里，改主题只动这两处。
- 断点：`1200 / 1024 / 900 / 640px`；另有 `@media print`（只留正文）与 `prefers-reduced-motion` 降级。

---

## 技术实现要点

- **Markdown 管线**（`markdown_render.py`）：一次渲染同时产出 HTML、TOC、纯文本、字数、阅读时长、摘要、双链信息。
  - 安全：**先转义原始 HTML 再交给 Markdown**（`<script>` 只会显示成文本），随后对 `href/src` 做协议白名单，
    `javascript:` / `vbscript:` / `data:text/html` 一律替换成 `#`；外部链接自动补 `target="_blank" rel="noopener noreferrer"`，
    图片补 `loading="lazy"`，表格包一层 `.table-wrap` 以便横向滚动。
  - 行内代码按 **CommonMark 规则配对**（只有长度相同的反引号段才能闭合）。这点很关键：早期版本用「一开一关」的朴素切换，
    一行里塞一个未闭合的反引号就能让后面的裸 `<script>` 逃过转义（已修复，并加了回归用例）。
  - 最后还有一道**兜底防线**：对渲染结果里真正的标签做一次清洗，`script` / `iframe` / `style` / `svg` / `form` 等一律转成文本，
    `on*=` 事件属性直接删除（只作用于真标签，不碰已经转义成文本的内容，也不影响任务清单的 `<input>`）。
  - 双链：先把 `[[标题]]` 换成 `{{wl:N}}` 占位符再交给 Markdown，避免被链接语法吃掉；代码块与行内代码里的 `[[...]]` 不解析。
  - 与 CommonMark 对齐：`# 标题` 才是标题，`#标签` 是普通文字（否则一整行 `#标签` 会被吃成 H1）。
- **搜索的取舍**：SQLite FTS5 的 `trigram` 分词器能对中文做子串匹配，但要求查询词 ≥3 个字符，
  所以短查询自动走 `LIKE`；打分在 Python 里统一做（标题每个词 8 分 / 标签 4 分 / 正文每次 1 分起，置顶 +1.5 / 星标 +0.5），摘要片段也从原文截取，
  保证两种后端的结果结构完全一致。运行环境的 SQLite 没编 FTS5 时会全程退回 `LIKE` 并在启动横幅里提示；如果只有 `unicode61` 没有 `trigram`，查询同样会走 `LIKE`（见「已知限制」）。
- **版本快照策略**：存的是「改动之前」的状态，所以历史列表里最新的一条就是上一个版本；自动保存的快照 10 分钟内合并，
  避免每 2.5 秒产生一条垃圾记录；手写摘要会被识别出来（与自动摘要不一致就视为手写）并保留。
- **写入安全**：表单类写操作走 CSRF 校验（签名会话 Cookie 里的 `csrf` 声明与 `_csrf` 字段 / `X-CSRF-Token` 头比对），
  另有 Origin/Referer 校验中间件拦跨站写请求、登录 8 次失败锁 5 分钟、会话 Cookie `HttpOnly + SameSite=Lax` 且 HMAC 签名。
- **URL / 表单里的整数有范围约束**：所有 `note_id` / `version_id` / `page` / `limit` / `template_id` 都会限住范围
  （SQLite 整数是 64 位，超过就会在绑参时抛 `OverflowError`），越界直接返回 422 或 303 回列表页而不是 500；
  `repo.list_notes` 内部也会再夹一次页码，保证任何调用方都塞不进巨大的 `OFFSET`。
- **CSP**：默认 `default-src 'self'`，脚本只允许自身与每次请求随机生成的 nonce，页面里没有内联事件处理器。
- **上传校验**：扩展名白名单（**拒绝 SVG**，避免 XSS）+ 文件头签名校验（PNG/JPEG/GIF/BMP/WebP/AVIF）+ 体积上限 + 内容 SHA256 前 16 位命名去重，静态目录单独挂载。
- **无构建**：CSS/JS 都是源码即产物，静态资源用文件 mtime 算出的 `?v=` 破缓存。

---

## 自测

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q                 # 616 个用例
python scripts/audit_css.py               # 模板里用到的 class 是否都有样式
python scripts/build_highlight_css.py     # 重新生成代码高亮配色
```

- `tests/test_smoke.py`（90 个用例）：端到端走一遍登录、建/改/删笔记、公开、博客、订阅源、搜索、上传、导出、回收站、历史版本、模板，
  外加「超大整数与畸形参数不能 500」的回归。
- 数据层与渲染：`test_repo.py`（37）、`test_markdown.py`（32）、`test_search.py`（14）——标签合并、版本剪枝、双链重连、回收站过期、
  统计；Markdown 渲染与安全（XSS、危险 URL、TOC、任务清单、未闭合反引号）；中文短词与权重打分。
- `tests/test_utils.py`（40）：slug、标签解析、**真值解析**、分页、时间、diff、密码哈希与会话签名（含篡改与过期）。
- AI 一组（`test_ai_core` 39、`test_ai_settings` 17、`test_ai_embed` 14、`test_ai_controls` 13、`test_ai_inline` 12、`test_ai_tags` 12、
  `test_ai_search` / `test_ai_ask` / `test_ask_scope` 各 9、`test_ai_related` 8、`test_ai_embed_auto` 5）：
  每个都起一个**本地假 OpenAI 兼容服务**（`http.server`，端口 0），断言实际发出去的 messages，**不联网**。
- 其余按子系统：导入导出往返（`test_import` 27、`test_export_media` 6）、批量操作（`test_batch` 27、`test_batch2` / `test_batch_persist` 各 5）、
  图片（`test_media` 20、`test_media_dedupe` 11、`test_media2` 8）、回收站策略 19、标签管理 19、备份（`test_db_backup` 25、`test_backup_sanitize` 12）、
  站点设置 12、日志 9、表单与 CSRF 9、账号 8、后台线程 5、手机端工具栏 5、标签输入 3。
- 测试全部跑在**临时数据目录**里，不会碰你的真实笔记。
- `scripts/` 下三个脚本都会把标准输出切到 UTF-8，Windows 的 GBK 控制台也能直接跑。

---

## 已知限制

- **单用户**：没有注册、多用户、权限体系，只有一道密码；不适合多人共用（密码可在设置页改）。
- **自动备份只保最近 7 份自动快照**，且都在本机 `data/backups/` —— 硬盘坏了照样全没，
  重要的库请偶尔把备份文件拷到别的地方。备份可在设置页立即做（含一份**脱敏**版本，把 AI Key 与密码哈希清空，可安全分享）；
  改密码在设置页「账号」区块（`.env` 的 `INKNOTE_PASSWORD` / `INKNOTE_PASSWORD_HASH` 仍然有效，是页面没配过时的兜底）。
- **导入是「合并」不是「替换」**：不会删除库里已有的笔记，也不能用备份把库回滚到某个时间点；
  想要那种效果得先清空 `data/` 再导入。匹配靠 slug / 标题，所以**同名不同笔记**可能被认成同一篇。
- **图片管理按「引用」判孤立**：只检查笔记正文里有没有 `/media/...` 链接。上传时会按内容 sha256 去重
  （同内容只留一份，第二次上传直接复用已有地址）；`/images` 会标出内容重复的图，未被任何笔记引用的副本可一键清理，
  **被引用的绝不自动删**。删笔记不会顺手删图（宁可留着也不误删别处的引用）。
- **双链按标题精确匹配**（忽略大小写）：同名笔记只会连到最近更新的那一篇。改标题不会自动改写正文里的 `[[旧标题]]`，页面渲染时可能显示成待创建；数据库里已解析到该笔记的反向链接仍保留，`repo._attach_dangling_links` 只会把此前悬空的链接接到新建/改名后的标题上。
- **搜索兜底用 `LIKE`**：笔记量上万后短词查询会变慢；没有分词器、没有模糊匹配与同义词。
- **FTS5 兜底细节**：`search.py` 只在 `FTS_TOKENIZER == "trigram"` 时走 FTS；如果运行环境只有 `unicode61`（中文分词不友好），实际查询会退回 `LIKE`，启动横幅与 `/api/search` 的 `engine` 字段也会如实显示为 `LIKE`。
- **历史版本是整篇快照**，不是 diff 存储；版本多了会占空间（默认只留 20 个）。
- **图片不做压缩、不清理 EXIF**：不想为此引入 Pillow；上传前请自行注意隐私信息。
- **上传会校验文件头**：除了扩展名白名单，还会检查 PNG/JPEG/GIF/BMP/WebP/AVIF 的文件签名，改名伪装的文件会被拒绝；但仍不检查图片是否损坏、也不做尺寸限制（只限体积）。
- **只支持图片附件**，不支持 PDF / 压缩包等任意文件。
- **智能问答有两条检索路径**：配了向量模型并建过索引走语义召回，否则回退关键词（2/3/4 元字组命中打分）。
  两条都不是「搜不到就不答」——AI 只会依据检索到的片段作答，并列出引用来源。
- **AI 入口不会自动隐藏**：未配置时按钮仍然可见，点击后提示「尚未配置 AI 服务」。
- **向量索引会自动跟进**：后台线程每 30 分钟跑一次增量索引（只重算 `updated_at` 变过的笔记），没配向量模型时静默跳过；
  改了 embedding 模型、或想立刻对齐，去设置页点一次「重建索引」即可。
  索引是本地算的，每篇最多 20 块、单块 ≤800 字，超长笔记只索引前面的部分。
- **回收站清理只在启动时跑一次**，常驻进程里不会定时清理。
- **时间用服务器本机时区**，没有时区配置项。
- 编辑器是「纯文本 + 预览」，不是所见即所得。

## Roadmap

- 收拢 `style.css`（多轮并行开发留下的补丁分节有重复选择器与重复断点，值得合并一遍）
- 图片上传时压缩 / 去 EXIF
- 草稿箱自动清理、回收站定时清理（现在只在启动时清一次）
- 文章目录支持多级折叠、代码块行号与复制按钮配置
- 评论（或接入 Giscus 之类的外部评论）
- 多用户 + 权限（如果哪天想给朋友用）

---

*本文档于 2026-09-12 对照源码逐条核对，并实测：`python -m pytest tests -q` → **616 个用例全部通过**（含 AI 配置/向量检索/内联写作/问笔记/标签管理/导入恢复/批量操作/后台线程等新增模块）。`scripts/audit_css.py` → 模板里用到的 class 全部有样式。*
