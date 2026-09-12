# 第四轮并行开发 · 文件归属与接口约定（冻结）

> 8 个 agent 同时改。**只改自己名下的文件**。基线：**427 passed**，不许跑红。

## 0. 硬规矩（前几轮踩过的坑，别重复）

1. **不要起 uvicorn、不要用 Playwright、不要跑全量测试** —— 只跑自己的测试文件（TestClient，秒级）。
   服务和浏览器验证由主 agent 统一做一次。
2. **不要改 `app/static/css/style.css`** —— 样式写进你自己的补丁文件（见表），我最后合并。
   只用已有 CSS 变量，不写死颜色（暗色模式要跟着走）。
3. **不引入任何新依赖**（纯标准库）。
4. 行为改动要**向后兼容**：老数据、老页面、老接口都不能坏。
5. 中文注释，风格与现有代码一致；改完跑自己的测试，必须全绿。

## 1. 文件归属（硬性）

| Agent | 只能改/新建 | 任务 |
| --- | --- | --- |
| **A 日志落盘** | `app/logging_setup.py`(新)、`run.py`、`tests/test_logging.py`(新) | 日志写文件 + 按天滚动 |
| **B 导出带图** | `app/routers/pages.py`、`app/services/importer.py`、`tests/test_export_media.py`(新) | 导出 zip 含图片、导入还原 |
| **C 索引自动跟进** | `app/services/ai_embed.py`、`tests/test_ai_embed_auto.py`(新) | 笔记改了自动增量重建索引 |
| **D 备份脱敏** | `app/services/db_backup.py`、`app/routers/backup.py`、`app/templates/backup.html`、`.scratch/css-patch-backup3.css`(新)、`tests/test_backup_sanitize.py`(新) | 备份风险提示 + 不含密钥的备份 |
| **E 回收站策略** | `app/templating.py`、`app/templates/trash.html`、`.scratch/css-patch-trash.css`(新)、`tests/test_trash_policy.py`(新) | 每篇还剩几天被清掉 |
| **F 跨页勾选** | `app/templates/notes/list.html`、`app/static/js/batch.js`、`.scratch/css-patch-batch3.css`(新)、`tests/test_batch_persist.py`(新) | 翻页/换筛选后仍记得勾了谁 |
| **G 图片去重** | `app/services/media.py`、`app/routers/media.py`、`app/routers/api.py`、`app/templates/images.html`、`.scratch/css-patch-media3.css`(新)、`tests/test_media_dedupe.py`(新) | 上传时按内容去重 + 清理重复 |
| **H 手机工具栏** | `app/templates/notes/editor.html`、`.scratch/css-patch-editor-mobile.css`(新)、`tests/test_editor_mobile.py`(新) | 窄屏收成「更多」 |

**谁都不许碰**：`app/main.py`（我接线）、`app/routers/notes.py`、`app/templates/_macros.html`、
`app/db.py`、`app/repo.py`、`app/services/ai.py`、`app/search.py`、`app/security.py`、
`tests/conftest.py`、`tests/test_smoke.py`、`style.css`、别人的文件。

## 2. 各任务要点

### A · 日志落盘
- `app/logging_setup.py`：`setup_logging(*, level=None, log_dir=None) -> Path`
  1. 目录 `data/logs/`，文件 `inknote.log`
  2. 用 `logging.handlers.TimedRotatingFileHandler(when="midnight", backupCount=N, encoding="utf-8")`
  3. 保留天数从 `INKNOTE_LOG_DAYS` 读（默认 14），级别从 `INKNOTE_LOG_LEVEL` 读（默认 `INFO`）
  4. **同时保留控制台输出**（原来的启动横幅和 uvicorn 日志不能消失）
  5. 根 logger `"inknote"`，格式：`时间 级别 模块名 消息`
  6. **重复调用不能重复加 handler**（幂等），**写不进去也不能让服务崩**（目录建不了就退回只打控制台并 warning）
- `run.py`：启动时调用它（在 uvicorn 起之前）
- 测试：幂等、文件真的写进去了、按天滚动配置生效、目录不可写时不抛异常（用 tmp_path + monkeypatch）

### B · 导出 zip 带图片
- 现在 `/export/zip` 只打 `.md` + `notes.json`（实测 7 个文件），**图片没跟过去**，换机器后 `/media/...` 全是死链。
- 导出：zip 里加 `media/` 目录（保持 `uploads/` 下的相对路径），`notes.json` 里加一个 `"media"` 字段列出这些文件（老解析器忽略未知字段即可）
- 导入：`importer.py` 识别 `media/` 里的文件 → 写到 `data/uploads/` 对应相对路径；
  **已存在且内容相同就跳过**，不同则保留两边（重命名加 `-1`）并在结果里报「图片新增 N 张、跳过 M 张」
- **安全**：zip 里的路径要防目录穿越（`../`、绝对路径、符号链接一律拒绝），只写进 `uploads/` 下
- 页面上（`/backup` 的说明里？不行，那个文件不归你）→ 在 `pages.py` 的导出响应头里带个注释说明即可；
  文档由主 agent 更新
- 测试：**往返测试最重要** —— 导出的 zip 里真有图片 → 导进新库 → `uploads/` 里文件内容一致；
  同一份导入两次不重复；带 `../` 的恶意 zip 被拒

### C · 向量索引自动跟进
- 现状：只有手动点「重建索引」才会更新（`rebuild(force=False)` 已经是增量的：只重算 `updated_at` 变过的笔记）。
- 新增：`maybe_auto_index(conn, *, interval_minutes=30, dry_run=False) -> dict | None`
  - 距上次自动索引不足 `interval_minutes` 返回 None（幂等，用 meta 记时间戳）
  - 否则调增量重建，返回 `{"indexed": n, "total": m, "seconds": x}`
  - **没配 embed_model 时直接返回 None**（不报错）
  - **单篇失败不能中断整轮**（收集错误继续）
- 主 agent 会在启动时 + 每 30 分钟的后台线程里调它（你在总结里写清签名）
- 测试：没配模型返回 None；改一篇笔记后 `maybe_auto_index` 能只重建那一篇；幂等（立刻再调返回 None）；
  embedding 服务挂了不抛异常

### D · 备份脱敏 + 风险提示
- 问题：备份 `.db` 里 `meta` 表有 `ai.api_key` **明文**（实测确认），下载出去等于把 Key 一起给了别人。
- 做两件事：
  1. **风险提示**：备份页顶部一行明显提示「备份文件里含 AI 密钥等敏感信息，别随手分享；要分享请用『脱敏备份』」
  2. **脱敏备份**：`create_snapshot(conn, *, reason="manual", keep=7, sanitized=False)`
     脱敏版把 `meta` 里的 `ai.api_key` 与 `account.password_hash` 置空（其余原样），文件名带 `-sanitized`
     页面上「立即备份」旁边加一个「脱敏备份（可安全分享）」按钮
- **不要自己发明加密算法**（标准库里没有 AES，手搓必然不安全）：在总结里明确写「没有做加密，原因是 X」
- 测试：普通备份里 `ai.api_key` 还在；脱敏备份里它是空串且**其它键都在**；脱敏备份能回滚（回滚后 AI 未配置但笔记完好）；文件名后缀正确

### E · 回收站保留策略可视化
- 现状：回收站的笔记只在**下次启动**时按 `settings.trash_days` 清掉，页面上完全看不出来。
- 在 `app/templating.py` 注册 Jinja **过滤器** `purge_in(deleted_at) -> str`（或返回天数，你定，写清楚）：
  - 返回如「还有 12 天」「今天会清掉」「已过期，下次启动清理」「不自动清理」（`trash_days=0` 时）
  - 解析失败/空值返回空串，**绝不抛异常**
  - 它是纯函数（读 `settings.trash_days`），**不查库** —— 这样就不用改路由
- `trash.html`：每条笔记上显示这个倒计时（样式写进你的补丁）；顶部再加一行说明「回收站里的笔记会在 N 天后、于下次启动时清理」
- 测试：`trash_days=0/30` 两种设置下的返回值；刚删的、删了很久的、时间格式坏的；页面渲染出倒计时文案

### F · 批量跨页保持勾选
- 现状：翻页或换筛选后，之前勾的笔记就丢了。
- 用 `localStorage` 记住选中的 id（key 里最好带上「哪个页面」，别和别处打架）：
  - 进入列表页时：把存储里的 id 对应的复选框自动勾上（只勾当前页里存在的）
  - 勾/取消时：更新存储；**翻页保留**（点分页链接、换筛选都算）
  - 操作条显示「已选 N 项（含其它页 M 项）」；提交时把**所有**选中的 id 都带上（为每个 id 生成一个 hidden input，或提交前用 JS 注入）
  - 提供「清空选择」按钮
- **没有 JS 也要能用**：退化成只勾当前页（原行为）
- 注意：不要和已有的「选中全部筛选结果」（`all=1`）打架 —— 那是另一条路径，勾了它就用 `all=1`，不带 id
- 测试（可以用断言 HTML/JS 内容的方式，纯前端逻辑没法在 pytest 里跑）：
  脚本被引入、关键函数存在、表单里能生成多个 `note_ids`、`all=1` 路径没被破坏

### G · 图片按内容去重
- 两件事：
  1. **上传时去重**：算 sha256，若 `uploads/` 里已有相同内容 → **不写新文件**，直接返回已有的 `/media/...` 地址，并在响应里标 `"deduped": true`
  2. **/images 里显示重复**：同一内容的图标记「与 xxx 内容相同（重复）」；如果重复的那些**没有被任何笔记引用**，提供「清理多余副本」
     - **被引用的绝不能自动删**（可能两篇笔记各引用一份）→ 那种只提示，不强删
- 实现：`media.duplicates(conn) -> list[dict]`（按 sha256 分组）；`media.cleanup_duplicates(conn) -> dict`（只删未被引用的副本）
- 注意：老的图片没有 hash → 懒计算（首次扫描时算并缓存到文件旁的 `.sha256` 边车文件，或存 meta；你选一种，说明理由）
- 测试：同内容传两次只有一份文件、第二次返回同一 URL；重复检测正确；被引用的不被自动删；
  `cleanup_duplicates` 只删多余的、文件真的没了；不同内容不会被误判

### H · 手机上的编辑器工具栏
- 现状：工具栏 17 个按钮，窄屏会挤成多行（实测 1440px 下已经 1~2 行，手机上更挤）。
- ≤640px：只显示最常用的几个（加粗 / 斜体 / 代码 / 链接 / 图片），其余收进一个「**更多 ▾**」
  - **纯 CSS 实现**（`<input type="checkbox" class="sr-only" id="md-more-toggle">` + `<label for="...">更多</label>` + `#md-more-toggle:checked ~ .md-tool__rest { display: flex }`）—— 不要写 JS
  - **桌面端（>640px）行为完全不变**：所有按钮照旧显示，「更多」按钮隐藏
  - 键盘可用：`<input>` 可聚焦（别用 `display:none` 藏它，用 `.sr-only`）
  - 展开后「更多」文案变成「收起」？纯 CSS 也能做到（`:checked + label::after` 换文案），可选
- 样式写进 `.scratch/css-patch-editor-mobile.css`
- 测试：模板里有那个 checkbox/label；桌面端没有多出可见元素（断言 class 存在即可）；现有 `test_smoke` 里编辑器相关用例不坏

## 3. 验收
```powershell
cd C:\repo\inknote
.venv\Scripts\python.exe -m pytest tests/test_你的文件.py -q     # 必须全绿
```
不确定会不会影响别人时，可以跑一次全量（约 40 秒），但别拿它当主要手段。

## 4. 总结里写什么
≤15 行：改了哪些文件、新增函数签名、**实测数字/结果**、需要我在 `main.py` 接线的地方、不确定的点。
不要粘贴大段代码。
