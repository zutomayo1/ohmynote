# 第三轮并行开发 · 文件归属与接口约定（冻结）

> 8 个 agent 同时改代码。**只改自己名下的文件**，跨模块只通过本文写死的接口调用。
> 会议：基线 **360 passed**（`python -m pytest tests -q`），不许把它跑红。

## 0. 本轮的硬规矩（为了不重演上一轮的翻车）

1. **不要起 uvicorn、不要用 Playwright、不要跑全量测试**。
   上一轮 8 个 agent 各起一个预览实例 + 各跑一遍全量，机器直接卡住。
   → 你只写代码 + 跑**你自己的测试文件**（`pytest tests/test_xxx.py -q`，进程内 TestClient，秒级）。
   → 服务和浏览器的端到端验证由主 agent 统一做一次。
2. **不要改 `app/static/css/style.css`**。把所有样式写进**你自己的补丁文件**（见下表），
   主 agent 最后统一合并进 `style.css` 并做结构整理。只用已有 CSS 变量，不许写死颜色。
3. 中文注释、与现有代码风格一致、**不引入任何新依赖**。
4. 新加的 class 必须在你自己的补丁文件里有规则（主 agent 合并后会跑 `scripts/audit_css.py`）。

## 1. 文件归属（硬性，冲突了自己负责）

| Agent | 只能改/新建 | 任务 |
| --- | --- | --- |
| **A1 备份** | `app/services/db_backup.py`(新)、`app/routers/backup.py`、`app/templates/backup.html`、`.scratch/css-patch-backup2.css`(新)、`tests/test_db_backup.py`(新) | 数据库自动备份 + 列表 + 下载 + 回滚 |
| **A2 改密码** | `app/services/account.py`(新)、`app/security.py`、`app/routers/auth.py`、`app/routers/ai_admin.py`、`app/templates/settings.html`、`.scratch/css-patch-account.css`(新)、`tests/test_account.py`(新) | 页面里改密码 |
| **A3 图片库** | `app/services/media.py`、`app/routers/media.py`、`app/templates/images.html`、`.scratch/css-patch-media2.css`(新)、`tests/test_media2.py`(新) | 图片库分页 + 体验 |
| **A4 图片提示** | `app/templating.py`、`app/templates/_macros.html`、`app/templates/notes/detail.html`、`.scratch/css-patch-imghint.css`(新)、`tests/test_image_hint.py`(新) | 删笔记前提示它引用了几张图 |
| **A5 批量增强** | `app/routers/notes.py`、`app/templates/notes/list.html`、`app/static/js/batch.js`、`.scratch/css-patch-batch2.css`(新)、`tests/test_batch2.py`(新) | 「选中当前筛选的全部 N 篇」 |
| **A6 自检脚本** | `scripts/check.py`(新)、`tests/test_check_script.py`(新) | 一条命令跑完自检 |
| **A7 服务层日志** | `app/services/ai_embed.py`、`ai_chat.py`、`ai_usage.py`、`importer.py`、`site_settings.py`、`app/config.py` | 33 处静默吞异常补日志 |
| **A8 路由层日志** | `app/routers/ask.py`、`api.py`、`ai_embed.py`(路由)、`blog.py`、`meta.py`、`pages.py`、`app/search.py`、`app/utils.py` | 同上 |

**谁都不许碰**：`app/main.py`（接线由主 agent 做）、`app/db.py`、`app/repo.py`、`app/markdown_render.py`、
`app/services/ai.py`、`tests/conftest.py`、`tests/test_smoke.py`、`app/static/css/style.css`、别人的文件。

## 2. 各任务要点

### A1 · 自动备份（最重要的一件）
```python
# app/services/db_backup.py —— 必须用 sqlite3 的备份 API，不要直接复制文件！
# （WAL 模式下复制文件可能拿到不一致的快照）
create_snapshot(conn, *, reason: str = "manual", keep: int = 7) -> dict
#   用 conn.backup(dest) 生成 data/backups/inknote-<时间戳>-<原因>.db
#   返回 {"name","path","size","created_at","kept","removed":[str,...]}
list_snapshots(*, limit: int = 50) -> list[dict]     # 按时间倒序：{"name","size","mtime","reason"}
maybe_auto_backup(conn, *, interval_hours: int = 24, keep: int = 7) -> dict | None
#   距离上次自动备份不足 interval_hours 就返回 None（幂等，主 agent 会放在启动时 + 每小时调一次）
restore_snapshot(conn, name: str) -> dict
#   回滚前先用 create_snapshot(reason="before-restore") 保护现场，然后用 source.backup(conn)
#   把备份写回**当前连接**（不要替换文件！），返回 {"restored": name, "safety": "..."}
delete_snapshot(name: str) -> bool
```
- 文件名要能安全解析（**防目录穿越**：只允许 `backups/` 里、`.db` 结尾、名字不含路径分隔符）
- 页面上：**手动立即备份**按钮、备份列表（时间/大小/原因）、**下载**、**回滚**（`data-confirm` 二次确认，说明会覆盖当前数据）、删除
- 回滚后 flash 提示「已回滚到 X；回滚前的数据已另存为 Y」

### A2 · 页面里改密码
- 密码来源优先级：**数据库 meta（页面改的）→ `.env` 的 `INKNOTE_PASSWORD_HASH` → `.env` 的 `INKNOTE_PASSWORD`**
- `app/services/account.py`：`bootstrap(conn)` / `save_password(conn, new_password)` / `verify(conn, password) -> bool` /
  `describe()`（**绝不返回哈希**，只返回「已设置 / 来自 `.env`」这类信息）/ `reset(conn)`（回到 `.env`）
- 哈希继续用 `app/security.py` 里已有的 pbkdf2 实现（自己看清楚函数名再用），比较必须用 `hmac.compare_digest`
- 设置页加一个「账号」区块：旧密码 + 新密码 + 确认新密码；**旧密码错了要拒绝并计数节流**（复用登录节流那套思路）
- 新密码最少 8 位；两次输入不一致要拒绝；改成功后 flash 提示「已改，下次登录用新密码」
- **改完密码不要动现有会话 cookie**（会话是 HMAC 签名的，跟密码无关），但要在页面上说明「已登录的设备不会被踢下线」
- 主 agent 会在 lifespan 里调 `bootstrap`，你在总结里提醒一下

### A3 · 图片库分页 + 体验
- `/images` 加分页（复用现有的 `pagination` 宏，每页 24 张），筛选（全部 / 只看孤立）和分页要能共存
- 顶部统计改成「共 N 张 · 占用 X · 孤立 M 张（Y）」并保留「清理孤立图片」
- 每张图加「复制 Markdown 链接」按钮（`![](url)`，用 `navigator.clipboard`，失败回退 `prompt`）
- 被引用的图片要能展开看**引用它的笔记标题**（默认显示前 3 条 + 「还有 N 篇」）

### A4 · 删笔记前提示引用了几张图
- 在 `app/templating.py` 注册一个 Jinja 过滤器 `image_count(content) -> int`：数正文里 `/media/...` 的引用数（去重）
- 卡片上的删除确认（`_macros.html` 的 `data-confirm`）和详情页的删除按钮，文案改成
  「把《标题》移入回收站？正文引用了 N 张图片，删完可以去「图片」页清理。」（N=0 时不提图片）
- 详情页可以再显示一行小字：`本文引用 N 张图片`（N=0 不显示），并给一个跳 `/images` 的链接
- **不要改 `repo.py` / `notes.py`**：过滤器只吃 `note.content` 字符串，不查库

### A5 · 批量「选中当前筛选的全部 N 篇」
- 现在只能勾当前页；加一个「选中筛选出的全部 N 篇」（N = 当前筛选条件下的总篇数）
- 服务端：`POST /notes/batch` 支持 `all=1` + `filters`（把当前筛选参数带过来），意思是「按这些条件取全部 id」
  → 但要**设上限**（比如一次最多 500 篇）并在 flash 里说明实际处理了多少
- `_parse_note_ids` 的健壮性不能退步（空/非数字/超 64 位/重复/不存在都要跳过而不是 500）
- 前端：`batch.js` 里加一个复选框「选中全部筛选结果」，勾上后把当前页的复选框置灰并显示「已选全部 N 篇」
- **没有 JS 也要能用**

### A6 · 一键自检脚本 `scripts/check.py`
```powershell
.venv\Scripts\python.exe scripts\check.py          # 全部检查
.venv\Scripts\python.exe scripts\check.py --quick  # 跳过慢的
```
按顺序做这些事，**每步打印 ✓/✗ 和耗时**，最后给总表并以退出码反映成败：
1. Python 语法检查（`compileall` 或逐个 `ast.parse`）
2. `pytest tests -q`
3. `scripts/audit_css.py`
4. 临时数据目录起一次应用（用 TestClient，不要占端口）→ 关键页面冒烟：`/health`、`/login`、`/blog`、`/notes`(未登录应 303)
5. 检查 `data/` 里的数据库能打开、`PRAGMA integrity_check` 通过（没有 data 就跳过）
6. 打印当前版本/依赖版本摘要
注意：Windows 控制台是 GBK，**别打印 emoji 之外的宽字符**（`✓`/`✗` 可以，`⚠️`/`✅` 会崩），
开头就 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`。

### A7 / A8 · 静默吞异常补日志
- 目标：把 `except ...: pass` / `return None` 这类**没有痕迹**的兜底，改成**照旧兜底但留一条日志**
  （`logger.warning(..., exc_info=True)` 或至少 `logger.debug`），这样线上出问题能查
- 用模块级 `logger = logging.getLogger("inknote.<模块名>")`，风格参考 `app/routers/ask.py` 里已有的写法
- **不要改变行为**：该返回 None 还返回 None，该回退还回退；只加日志
- **不要给「本来就会频繁发生」的情况刷屏**（比如搜索降级到 LIKE、FTS5 不可用）→ 这类用 `logger.debug` 或只在首次记录
- 也不要无脑加：`except Exception: pass` 后面跟着 `return None` 的**语义性兜底**（比如 `status()` 里算不出来）
  要写清楚注释为什么吞
- A7 的服务层文件里，`services/media.py` 和 `services/site_settings.py` 你有权限；A8 不要碰服务层

## 3. 验收（各自）
```powershell
cd C:\repo\inknote
.venv\Scripts\python.exe -m pytest tests/test_你的文件.py -q     # 必须全绿
```
- 不建议跑全量（主 agent 会跑）；但**你要保证自己没把别人的用例想坏** —— 如果不确定，跑一次也行
- 报错要真的看明白再改，别用 `--no-header` 之类掩盖

## 4. 总结里要写什么
≤15 行：改了哪些文件、新增的接口签名、**你实测的数字/结果**、需要主 agent 接线的地方、不确定的点。
不要粘贴大段代码。
