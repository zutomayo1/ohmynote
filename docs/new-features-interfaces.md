# 新一轮功能 · 并行开发接口约定（冻结）

> 4 个 agent 同时改代码，**只能改自己名下的文件**。跨模块只通过本文写死的接口调用。

## 0. 现状（不要动）

- 测试基线：**265 passed**。任何一步都不能把它跑红。
- `app/main.py` 已经注册好 `backup` 和 `media` 两个桩路由，你们**不需要改 main.py**。
- 上一轮刚做完样式改造（卡片密度 / 设置页折叠 / 右栏去盒 / 暗色品牌色 / 导航），
  `style.css` 现在 2151 行，末尾有若干「补丁分节」。**不要直接改 `style.css`**（见下）。

## 1. 文件归属（硬性）

| Agent | 只能改/新建这些文件 | 任务 |
| --- | --- | --- |
| **A** | `app/services/importer.py`(新)、`app/routers/backup.py`、`app/templates/backup.html`(新)、`.scratch/css-patch-backup.css`(新)、`tests/test_import.py`(新) | 导入 / 恢复 |
| **B** | `app/services/site_settings.py`(新)、`app/routers/ai_admin.py`、`app/templates/settings.html`、`app/templates/base.html`、`app/templating.py`、`.scratch/css-patch-site.css`(新)、`tests/test_site_settings.py`(新) | 站点设置 + 回收站角标 |
| **C** | `app/services/media.py`(新)、`app/routers/media.py`、`app/templates/images.html`(新)、`.scratch/css-patch-media.css`(新)、`tests/test_media.py`(新) | 图片管理 |
| **E** | `app/routers/notes.py`、`app/templates/_macros.html`、`app/templates/notes/list.html`、`app/static/js/batch.js`(新)、`.scratch/css-patch-batch.css`(新)、`tests/test_batch.py`(新) | 批量操作 + 卡片历史入口 |

**谁都不许碰**：`app/services/ai*.py`、`app/main.py`、`app/db.py`、`app/repo.py`、`app/search.py`、
`app/markdown_render.py`、`app/security.py`、`app/deps.py`、`app/utils.py`、`app/services/exporter`（如有）、
`tests/conftest.py`、`tests/test_smoke.py`、其它 agent 的文件。
需要它们改动，就在总结里写出来，由主 agent 统一改。

## 2. CSS 补丁规则（重要）

你们**不能**改 `app/static/css/style.css`（4 个人同时改会互相覆盖）。
把自己需要的样式写进**自己的补丁文件**（路径见上表），主 agent 最后统一合并到 `style.css` 末尾。
- 只允许用已有 CSS 变量（`--brand` `--ink-3` `--surface-2` `--line` `--radius` `--accent-soft` `--danger` …），**不要写死颜色**（否则暗色模式会坏）
- 在总结里列出你新增的 class 名

## 3. 冻结的接口

### 3.1 A · 导入 / 恢复

```python
# app/services/importer.py
sniff_and_import(conn, filename: str, data: bytes, *, dry_run: bool = False) -> dict
# 按扩展名/内容分派到 import_zip / import_json / import_markdown
# 返回 {"created": int, "updated": int, "skipped": int, "notes": int,
#       "errors": [str, ...], "source": "zip|json|markdown", "dry_run": bool}

import_zip(conn, data: bytes, **kw) -> dict
import_json(conn, data: bytes, **kw) -> dict          # 兼容 {"notes": [...]} 和裸数组
import_markdown(conn, text: str, *, filename: str, **kw) -> dict
```

HTTP（已在 `app/routers/backup.py` 里注册好路径）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/backup` | 页面：导出入口 + 导入表单（`multipart/form-data`，字段名 `file`，可选 `dry_run`）+ 说明 |
| POST | `/backup/import` | 303 回 `/backup`，flash 里带摘要「新建 N 篇 · 更新 M 篇 · 跳过 K 篇」 |

**合并规则（必须保证幂等）**：
1. 有 `slug` 就按 `slug` 找现有笔记；没有 slug 就按**标题**找。
2. 找到 → **更新**（标题/正文/标签/分类/公开状态），不新建；没找到 → **新建**。
3. 绝对**不要**删除或覆盖用户已有的、备份里没有的笔记。
4. 同一份备份导入两次，第二次应该全是「更新 0 新建 0」或「更新 N」（**不能翻倍**）。
5. 导入的单篇 Markdown：标题取 front matter 的 `title`，没有就取正文第一个 `# 标题`，再没有就用文件名。
6. 出错要收集到 `errors` 里继续处理其它笔记，**不要整体失败**。

**必须有的测试**：`导出 → 导入到空库 → 内容/标签/公开状态一致` 的往返测试（用现有的导出函数生成 zip/json）。

### 3.2 B · 站点设置 + 回收站角标

```python
# app/services/site_settings.py
FIELDS = ("site_title", "site_subtitle", "site_description", "author", "base_url",
          "per_page", "trash_days")
DEFAULTS = {...}
bootstrap(conn) -> None          # 读 meta（前缀 "site."），覆盖掉 app.config.settings 上的同名属性
current() -> dict
save(conn, values: dict) -> dict # 写 meta + 立刻生效（不要等重启）
describe() -> dict               # 给页面用：值 + 来源(db/env)
reset(conn) -> None              # 清掉页面配置，回到 .env
```

- **生效方式**：`bootstrap(conn)` 把值直接写回 `app.config.settings` 对象（`settings.site_title = ...`）。
  模板里到处都在读 `settings.xxx`，这样**不用改任何模板**就能生效。
- 在 lifespan 里调用 `bootstrap`。**main.py 不许改** → 在总结里让主 agent 加（一行）。
- `per_page` 限 5~200，`trash_days` 限 0~3650，`site_title` 非空且 ≤60 字，非法值要回显错误不要崩。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/settings/site` | 保存站点信息，303 回 `/settings` 并 flash |

**回收站角标（item 4）**：
- `app/templating.py` 的 `base_context()` 里加 `nav_trash`（回收站里有多少篇）。
  注意 `base_context` 目前拿不到连接：自己开一个短连接（`with db.db() as conn:`）跑 `SELECT COUNT(*)`，
  查询失败要吞掉异常并给 0（**绝不能因为它让页面 500**）。
- `base.html` 里回收站链接后面加角标：`<span class="nav-badge">{{ nav_trash }}</span>`，为 0 时不显示。
- 顺带在页脚加两个链接：`备份与恢复` → `/backup`、`图片` → `/images`（其它 agent 的页面入口，放在「使用说明 · 设置」一排）。

**必须有的测试**：保存后 `settings.site_title` 立刻变 → 首页 `<title>` 跟着变；重启（重新 bootstrap）后仍在；
非法值被拒；`/settings` 里能看到新表单；未登录 POST 被挡；角标数字与回收站篇数一致。

### 3.3 C · 图片管理

```python
# app/services/media.py
scan(upload_dir: Path) -> list[dict]
# [{"name","rel","url","size","mtime","uploaded"}...]，按 mtime 倒序；url 形如 /media/2026/09/x.png
usage(conn) -> dict[str, list[dict]]
# {url: [{"id":..,"title":..,"url":"/notes/N"}, ...]}  ← 扫 notes.content 里出现的 /media/ 链接
library(conn, *, upload_dir=None) -> dict
# {"items":[{"name","rel","url","size","mtime","used_by":[...],"orphan":bool}...],
#  "total":int, "orphan_count":int, "total_size":int, "orphan_size":int}
delete(path_or_rel: str, *, upload_dir=None) -> bool
delete_orphans(conn, *, upload_dir=None) -> int
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/images` | 图片库：网格 + 每张的尺寸/上传时间/**被哪几篇笔记引用** + 「只看孤立图片」筛选 |
| POST | `/images/delete` | 表单字段 `rel`（相对路径）+ 可选 `force=1`；被引用的且没 force → 拒绝并 flash 警告；303 回 `/images` |
| POST | `/images/delete-orphans` | 删掉全部孤立图片，303 回 `/images`，flash 里报数量和省下多少空间 |

- 只允许删除 `uploads/` **里面**的文件：必须防目录穿越（`../`、绝对路径、符号链接），
  用 `Path.resolve()` 判断是否在 uploads 根目录内。
- 被引用的图片要能列出**哪些笔记**引用了它，并给可点的链接。
- 页面上「孤立图片」要显眼（数量 + 总大小），并给「一键清理」按钮。

**必须有的测试**：扫描出预置的图；引用统计正确（一篇笔记引用两张图）；被引用的删除被拒（force 后成功）；
删孤立图片后文件真的没了；目录穿越被拒（`../../.env`）；删除后笔记正文不受影响。

### 3.4 E · 批量操作 + 卡片历史入口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/notes/batch` | 表单：`note_ids`（重复字段）、`action`、可选 `tag`、`next`；303 回 `next`（默认 `/notes`），flash「已处理 N 篇」 |

`action` 取值（全部必须实现）：
`add_tag` / `remove_tag`（用 `tag` 字段）/ `publish` / `unpublish` / `pin` / `unpin` / `star` / `unstar` / `trash`

- `note_ids` 要做健壮解析：空、非数字、超大整数、重复 id、不存在的 id 都不能 500。
  复用 `app/deps.py` 里已有的 `NoteId` 注解或 `MAX_SQLITE_INT` 常量来防护。
- 全部动作复用 `app/repo.py` 里已有的函数（`set_public`/`set_pinned`/`set_starred`/`trash_note`/`add_tag`…），
  **不要新写 SQL**；没有对应函数就用最小改动实现并说明。
- 收回站里没有的笔记被操作时要**跳过而不是报错**，最后 flash 里说清「处理 N 篇、跳过 M 篇」。
- 前端：`notes/list.html` 的网格外面包一个 `<form id="batch-form" action="/notes/batch" method="post">`，
  每张卡左上角一个复选框（`_macros.html` 里的 `note_card` 宏加，`name="note_ids"`），
  网格上方一条 `.batch-bar`：`[全选] [已选 N 项] [选择操作 ▾ select name=action] [可选：标签输入框 name=tag] [执行]`。
- **没有 JS 也要能用**（`<select>` + 提交按钮就行）；JS（`app/static/js/batch.js`）只做增强：
  实时更新「已选 N 项」、全选/反选、选中后把操作条高亮、`action` 选 add/remove_tag 时才显示标签输入框。
- 别破坏现有的筛选/搜索/分页：批量表单的 action 固定 `/notes/batch`，当前筛选条件通过 `next` 带回来。

**历史入口（item 6）**：`note_card` 的操作按钮里加一个「历史」图标链接 → `/notes/{id}/versions`
（`title="查看版本历史（N 个版本）"`）；如果列表路由能顺手批量查出版本数就在 title 里显示，查不到就不显示。
**注意**：`note_card` 现在的签名是 `note_card(note, request, tokens=None, tag_base='/notes', actions=True)`，
新增参数必须带默认值（调用方在 4 个模板里）。

**必须有的测试**：批量打标签 / 公开 / 置顶 / 星标 / 移入回收站各一条；
`note_ids` 为空的请求不报错；混入非数字和超大数字不 500（422/303 都行但要有断言）；
不存在的 id 被跳过；未登录被挡；卡片里有 `name="note_ids"` 和历史链接。

## 4. 怎么验证（都要做）

```powershell
cd C:\repo\inknote
# 起带示例数据的预览实例（端口自己挑，别和别人撞）
Start-Process -FilePath '.venv\Scripts\python.exe' -ArgumentList '.scratch\preview.py','8111' -WindowStyle Hidden
Start-Sleep -Seconds 10
# 量样式 / 截图（真 Chrome）
.venv\Scripts\python.exe .scratch\shot.py 8111 /backup --sel ".backup-drop" --shot .scratch/shots/backup.png
```
- 预览实例：密码 `previewpw`，预置 6 篇笔记 + 3 张图片（1 张孤立）
- 自己的用例：`.venv\Scripts\python.exe -m pytest tests/test_xxx.py -q` 必须全绿
- **收尾前跑一次全量**：`.venv\Scripts\python.exe -m pytest tests -q`，必须 **265 + 你新增的** 全绿
- `bash` 工具在本会话是坏的，请用 pwsh / 直接读写文件

## 5. 总结里要写什么

≤20 行：改了哪些文件、新增哪些接口/class、**实测数字**（跑通的流程 + 测试数）、
需要在 `main.py` 或别处接线的地方、以及不确定的点。不要粘贴大段代码。
