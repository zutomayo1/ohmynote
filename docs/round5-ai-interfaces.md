# 第五轮并行开发 · AI 功能加强（接口冻结）

> 5 个 agent 同时改。**只改自己名下的文件**。基线 **499 passed**，不许跑红。

## 0. 硬规矩
1. **不要起 uvicorn、不要用 Playwright、不要跑全量测试** —— 只跑自己的测试文件（TestClient）。
2. **不要改 `app/static/css/style.css`** —— 样式写进自己的 `.scratch/css-patch-<名字>.css`，主 agent 合并。
   只用已有 CSS 变量（`--brand` `--brand-50/100/200/600/700` `--sp-1..8` `--ink-3` `--surface-2` `--line` `--radius*` `--sh-1/2/3`）。
3. 不引入新依赖（纯标准库 + 现有栈）。
4. AI 不可用时**一律优雅降级**：功能入口照旧显示，点了给人话提示，不能 500。
5. 所有 AI 调用**必须走 `app/services/ai.py` 的 `ai.chat()`**（它统一处理：配置、超时、错误翻译、脱敏、用量统计、local_only）。
   **不要自己写 urllib 请求**（除了 C 的向量检索 —— 那是走 `ai_embed` 的 embeddings）。

## 1. 文件归属

| Agent | 只能改/新建 | 任务 |
| --- | --- | --- |
| **A 内联 AI** | `app/services/ai_inline.py`(新)、`app/routers/ai_inline.py`(新)、`app/static/js/editor.js`、`app/templates/notes/editor.html`、`.scratch/css-patch-ai-inline.css`(新)、`tests/test_ai_inline.py`(新) | 续写 + 选中文本润色/精简/扩写/翻译 |
| **B 语义搜索** | `app/services/ai_search.py`(新)、`app/routers/pages.py`、`app/templates/search.html`、`.scratch/css-patch-ai-search.css`(新)、`tests/test_ai_search.py`(新) | 搜索页支持「按意思搜」 |
| **C 语义相关** | `app/services/ai_related.py`(新)、`app/services/ai_embed.py`、`app/routers/notes.py`、`tests/test_ai_related.py`(新) | 详情页「相关笔记」改用向量相似度 |
| **D 问这篇** | `app/routers/ask.py`、`app/templates/ask.html`、`app/templates/notes/detail.html`、`.scratch/css-patch-ask-scope.css`(新)、`tests/test_ask_scope.py`(新) | 「问这篇笔记」把检索限定到一篇 |
| **E 可控性** | `app/services/ai.py`、`app/routers/ai_admin.py`、`app/templates/settings.html`、`.scratch/css-patch-ai-controls.css`(新)、`tests/test_ai_controls.py`(新) | 提示词可改 + 模型回退链 |

**谁都不许碰**：`app/main.py`（我接线）、`app/db.py`、`app/repo.py`、`app/security.py`、`app/templating.py`、
`tests/conftest.py`、`tests/test_smoke.py`、`style.css`、别人的文件。

## 2. 各自要做的事

### A · 编辑器内联 AI（最重要）
```python
# app/services/ai_inline.py
continue_text(title, content, *, instruction="", conn=None) -> str
rewrite(text, mode, *, conn=None) -> str
# mode ∈ {"polish" 润色, "concise" 精简, "expand" 扩写, "translate" 翻译成英文, "formal" 更正式}
```
- 路由（新文件 `app/routers/ai_inline.py`，`prefix="/api"`，依赖 `require_login_api` + `csrf_protect`）：
  - `POST /api/ai/continue` → `{ok, text}`，body `{title, content, instruction?}`
  - `POST /api/ai/rewrite` → `{ok, text}`，body `{text, mode}`
  - **我（主 agent）会把 `ai_inline.router` 注册进 main.py，你只管写**
- 编辑器 UI：
  - 工具栏最右加一个「**✦ AI**」按钮组（或一个按钮 + 下拉）：`接着写` / 选中文本时显示 `润色 · 精简 · 扩写 · 翻译 · 更正式`
  - 选中文本走**浮动小菜单**（选区上方，深色小药丸按钮）；没选中就只显示「接着写」
  - 结果**先预览再插入**（不要直接改正文）：弹出一个小面板显示结果 + [插入] [替换选区] [重试] [关闭]
  - 等待时按钮转圈/禁用；失败用现有 toast（`window.InkNote.toast`）报人话
  - **纯增强**：JS 挂了不影响写笔记；`Ctrl+J` 快捷键留给「接着写」
- 注意：`editor.js` 里已有上传、工具栏、预览、自动保存等逻辑，**只能追加，不能重写**

### B · 语义搜索
- 搜索页 (`/search`) 加一个开关：「**按意思搜**」（默认关闭，保持现在关键词搜索的行为）
- `app/services/ai_search.py`：
  ```python
  semantic_search(conn, query, *, limit=20, page=1) -> dict | None
  # 返回 {"items":[note...], "total":n, "engine":"semantic"}；不可用时返回 None（调用方回退关键词）
  # items 要和 repo.search_notes 的输出同构（含 score/snippet），模板不用改结构
  ```
- 路由：`/search?q=...&mode=semantic`；语义不可用时**自动回退关键词**并在页面上标注用了哪种
- 索引没建/没配向量模型 → 页面上给一句「语义搜索需要先在设置页配置向量模型并重建索引」，并可一键切回关键词
- 不要改 `repo.search_notes` 的行为

### C · 语义相关笔记
- 现状：详情页「你可能还想看」是关键词/标签重叠算的；改成**向量相似度优先**
- `app/services/ai_related.py`：
  ```python
  related_notes(conn, note, *, limit=5) -> list[dict] | None
  # 用向量余弦找相似笔记（排除自己、排除回收站）；不可用返回 None
  ```
- 在 `app/services/ai_embed.py` 里加一个**只读**辅助：`embedding_for_note(conn, note_id) -> list[float] | None`（**能用已有的就直接用，别重写索引逻辑**）
- `app/routers/notes.py` 的详情路由：优先 `ai_related.related_notes`，拿不到就回退现有逻辑（**行为不能变差**）
- 页面上要能看出「这是语义推荐」还是「关键词推荐」（一个小标注即可，别大改版式）

### D · 问这篇笔记
- `/ask?note=<id>`：把检索范围**限定在指定笔记**（标题+正文一起给模型），页面上显示「正在问：《标题》」+ 一个「切回问全部笔记」的链接
- `app/routers/ask.py`：
  - `GET /ask?note=N` -> 渲染时带 `scope_note`
  - `POST /ask`、`POST /ask/stream` 支持 `note_id` 字段：检索时**只取这一篇**（不要再跑全库语义检索），引用来源就是这一篇
  - 会话记录里存下 scope（`ai_chat` 的表结构**别动**，把 scope 记在 sources 或标题里即可，说明你的做法）
- `notes/detail.html`：详情页加一个「**问这篇**」按钮（链到 `/ask?note={{ note.id }}`），放在现有操作按钮组里
- 流式（`/ask/stream`）的 SSE 事件格式**必须保持兼容**（`sources` / `delta` / `done` / `error`），前端不用改

### E · 可控性：提示词可改 + 模型回退
1. **提示词模板**（存 meta，前缀 `ai.prompt.`）：
   - `summary` / `tags` / `answer` 三套，设置页可编辑（多行文本框），带「恢复默认」
   - `ai.py` 里现有的三处 prompt 拼接改成从 meta 读（读不到用内置默认），**模板里用 `{title}` `{content}` `{question}` 这类占位符**
   - 用户改了提示词不能把功能弄坏：占位符缺失/格式错要兜底（回退默认模板）
2. **模型回退链**：`ai.chat()` 在**主模型失败**（404/400 模型不存在、超时、5xx）时，自动用「通用模型」再试一次；
   两次都失败才抛错。要记日志（`logger.warning`）并在返回值/用量里能看出用了哪个模型（**别改 `chat()` 的签名和返回类型**，它返回 `str`）
3. 设置页新增「**提示词**」区块（`<details>`，照现有区块的 class 结构），summary 上写「摘要 / 标签 / 问答 · 已自定义 N 项」
4. `ai.FIELDS` / `PROVIDERS` / `models_for()` / `credentials()` 这些**公开接口不能改**；新增的用新函数（如 `ai.prompt_template(task)`、`ai.save_prompts(conn, values)`）

## 3. 验收
```powershell
.venv\Scripts\python.exe -m pytest tests/test_你的文件.py -q     # 必须全绿
```
AI 相关的测试**不要真的联网**：照 `tests/test_ai_core.py` 里 `fake_server` / `tests/test_ai_ask.py` 里 `FakeAI` 的写法，
自己起一个假的 OpenAI 兼容服务（`http.server`，端口 0），断言收到的 messages/prompt 内容。

## 4. 总结写什么
≤15 行：文件清单、新增接口签名、**实测**（假服务里收到的 prompt 片段、降级路径）、测试数、
需要我在 main.py 接线的地方、不确定的点。
