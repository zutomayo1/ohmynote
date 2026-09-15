# AI 相关模块 · 并行开发接口约定（冻结版）

> 三个 agent 同时改这一块，**只能改自己名下的文件**，跨模块只通过下面写死的接口调用。
> 谁改了别人的文件，合并时就会打架。

## 0. 现状（已由主 agent 完成，不要再动）

- 路由已拆分：`app/routers/ai_admin.py`（设置 + /api/ai/*）、`app/routers/ask.py`（问笔记）、
  `app/routers/ai_embed.py`（向量检索，**已注册进 main.py，目前是桩**）。
- `app/services/ai.py` 已经是完整的配置层，对外接口见第 2 节。
- `app/services/ai_usage.py` 已经能用：每次调用自动记一笔，`summary(conn)` 出统计数据。
- 测试基线：**186 个 pytest 用例全绿**（`python -m pytest tests -q`）。任何一步都不能把它跑红。

## 1. 文件归属（硬性）

| Agent | 只能改这些文件 | 说明 |
| --- | --- | --- |
| **A · 设置页 2.0** | `app/routers/ai_admin.py`、`app/templates/settings.html`、`app/static/js/settings.js`（新建）、`tests/test_ai_settings.py`（新建）、`app/static/css/style.css`（只允许在文件末尾追加一个「AI 设置页」分节） | 设置页的一切 |
| **B · 向量检索** | `app/services/ai_embed.py`（新建）、`app/routers/ai_embed.py`、`tests/test_ai_embed.py`（新建） | 不许改 ai.py / main.py / db.py |
| **C · 问笔记升级** | `app/routers/ask.py`、`app/templates/ask.html`、`app/static/js/ask.js`（新建）、`app/services/ai_chat.py`（新建）、`tests/test_ai_ask.py`（新建） | 多轮 + 流式 + 存成笔记 |

**谁都不许碰**：`app/services/ai.py`、`app/services/ai_usage.py`、`app/main.py`、`app/db.py`、`app/repo.py`、
`tests/test_smoke.py`、`tests/conftest.py`、`README.md`、`使用说明.md`。
需要它们改动，就在总结里写出来，由主 agent 统一改。

## 2. `app/services/ai.py` 对外接口（可以放心调用）

```python
ai.is_enabled() -> bool                  # 配好 base_url + model 才为 True
ai.models_for(task) -> str               # task: "summary" / "tags" / "answer" / "embed"
ai.current() -> dict                     # 当前生效的全部字段
ai.describe() -> dict                    # 给页面用：值 + 来源(db/env/空) + 掩码 + providers + tasks
ai.credentials() -> dict                 # {"base_url","api_key","timeout","embed_model"} 给别的模块用
ai.PROVIDERS -> list[dict]               # 预设：[{key,name,base_url,model,embed_model,key_url,note}]
ai.FIELDS / ai.BOOL_FIELDS / ai.DEFAULTS

ai.list_models(config=...) -> list[str]  # GET {base}/models；服务商不支持时抛 ai.AIUnsupported
ai.probe(config) -> dict                 # {"ok":bool,"steps":[{"name","status","detail"}],"reply":str}
ai.humanize_error(status, detail) -> str # 401/404/429… 翻成人话
ai.redact_text(text) -> (text, hits)     # 脱敏
ai.chat(messages, *, temperature, max_tokens, config=None, task="answer", conn=None) -> str
ai.summarize(title, content, *, conn=None) -> str
ai.suggest_tags(title, content, existing=None, *, conn=None) -> list[str]
ai.answer(question, contexts, *, history=None, conn=None, config=None) -> str
ai.build_qa_messages(question, contexts, history=None) -> list[dict]   # 流式/多轮共用
class ai.AIError(RuntimeError)           # 所有失败都抛它，str(exc) 可直接展示
class ai.AIUnsupported(AIError)          # 服务商没这个接口（不是配置错误）
```

`ai_usage`：

```python
ai_usage.ensure(conn)
ai_usage.record(conn, model=..., task=..., usage={...})   # chat() 里已自动调用
ai_usage.summary(conn, days=30) -> {"month_calls","month_tokens","month_prompt",
                                    "month_completion","by_task":[{"task","calls","tokens"}],
                                    "by_day":[{"day","calls","tokens"}],"last_call":{...}|None}
ai_usage.reset(conn) -> int
```

## 3. 已冻结的 HTTP 接口（跨 agent 调用）

### 3.1 A 的页面要用的（已实现，A 可以直接 fetch）

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| POST | `/api/ai/probe` | `{ok, steps:[{name,status:ok/fail/skip,detail}], reply}`，body 传 `{base_url, model, timeout, api_key}` |
| GET | `/api/ai/models?base_url=&use_saved=1` | `{ok, models:[str]}`；失败 `{ok:false,error}` |

### 3.2 B 必须提供的（A 的设置页会去拉，实现前会 404/501，A 要能容错显示）

| 方法 | 路径 | 返回 |
| --- | --- | --- |
| GET | `/api/ai/embed/status` | `{configured:bool, model:str, total:int, indexed:int, running:bool, progress:{done,total}|null, error:str, last_built_at:str}` |
| POST | `/api/ai/embed/rebuild` | `{ok:true, started:bool, total:int}`；已在跑则 `{ok:false,error:"正在重建中"}`(409) |
| POST | `/api/ai/embed/clear` | `{ok:true, removed:int}` |

### 3.3 B 必须提供的 Python 接口（C 的 /ask 会调用）

```python
# app/services/ai_embed.py
ai_embed.ensure(conn) -> None
ai_embed.available(conn=None) -> bool          # 配了 embed_model 且库里有向量
ai_embed.retrieve(conn, question: str, *, limit: int = 6) -> list[dict] | None
    # 命中返回笔记 dict 列表（必须含 title/content/url/snippet/score，和 repo.retrieve_notes 同构）
    # 不可用（没配 embed_model / 没索引 / 调用失败）一律返回 None，让调用方回退关键词检索
ai_embed.status(conn) -> dict                  # 和第 3.2 的 status 同结构
ai_embed.rebuild(conn, progress=None) -> dict  # 同步重建，progress(done,total) 可选回调
```

C 的调用方式（**必须 try/except 兜底**，B 的模块出任何问题都不能让 /ask 挂）：

```python
sources = None
try:
    from ..services import ai_embed
    sources = ai_embed.retrieve(conn, question, limit=6)
except Exception:
    sources = None
if sources is None:
    sources = repo.retrieve_notes(conn, question, limit=6)
```

## 4. 各自的任务清单

### A · 设置页 2.0
1. **一键预设**：用 `ai.PROVIDERS` 渲染按钮，点击后把 `base_url` / `model` / `embed_model` 填进表单（纯前端，不提交）。
2. **模型名下拉**：`base_url` 有值时按钮「获取可用模型」→ `GET /api/ai/models` → 把返回的模型填进 `<datalist>`，同时显示数量；失败显示 `error`。
3. **分任务模型**：增加「摘要 / 打标签 / 问答 各自用哪个模型」三个输入框（对应 `model_summary` / `model_tags` / `model_answer`，留空=用上面那个通用模型）。页面要显示每个任务**实际生效的模型**（`ai.describe()['tasks']`）。
4. **四个开关/字段**：`local_only`（只允许本机模型）、`redact`（发送前脱敏）、`embed_model`（向量模型，见 B）。
5. **测试连接升级**：用 `POST /api/ai/probe`，把 `steps` 逐步渲染成 ✅/⚠️/❌ + 说明（不要只弹一句话）。没有 JS 时表单 `POST /settings/ai/test` 也要能用（已有）。
6. **用量统计**：显示 `ai_usage.summary(conn)` 的「本月 X 次 · Y token（输入/输出）」、「按任务拆分」、「最近 30 天按天的小柱状图（纯 CSS 高度）」。加「重置统计」按钮（POST 到 `/settings/ai/usage/reset`，自己加这个路由）。
7. **向量索引区**：调 B 的 `GET /api/ai/embed/status` 显示「已索引 N / 共 M 篇」、进度条、「重建索引」按钮（POST rebuild，然后每 1.5 秒轮询 status 直到 running=false）。**B 的接口返回 404/501 时要优雅降级**，显示「向量检索暂不可用」而不是报错。
8. 所有新交互都要**优雅降级**：JS 挂了页面也能用（表单照样提交）。

### B · 向量检索
1. `ai_embed.py`：自建表 `note_embeddings(note_id, chunk_index, chunk, vector BLOB, model, updated_at)`（`CREATE TABLE IF NOT EXISTS`，**惰性建表**，不要改 db.py）。
2. 分块：按标题/空行切，单块 600–800 字、相邻重叠 ~100 字；每篇最多留 20 块（超长笔记截断并记日志）。
3. 调用 `POST {base}/embeddings`（请求体和 OpenAI 一致：`{"model": ..., "input": [...]}`），**批量**发送（建议每批 16 条），解析 `data[i].embedding`。
4. 向量用 `array('f')` 打包存 BLOB；余弦相似度纯 Python 算（不用 numpy）。
5. `retrieve()`：把问题向量化 → 取全部向量算相似度 → 取 top_k 块 → 聚合成笔记（同一篇取最高分块）→ 返回和 `repo.retrieve_notes` 同构的列表。
6. `rebuild()` 后台线程 + 进度；`status()` 返回进度。**注意 SQLite 连接不能跨线程共用**，后台线程里自己 `db.db()` 开新连接。
7. 测试：自己起一个假的 `/embeddings` 服务（确定性向量，比如按词哈希成 8 维），验证「能建索引、能按语义排序、未配置时 retrieve 返回 None、失败时返回 None 而不是抛异常」。

### C · 问笔记升级
1. **多轮追问**：新表 `ai_conversations` / `ai_messages`（惰性建表，不要改 db.py），`/ask?c=<id>` 显示整个会话；每次提问把「问题 + 引用到的笔记 + 回答」存下来，下一次把历史（最近 6 轮）传给 `ai.answer(..., history=...)`。要有「新对话」「删除这次对话」。
2. **流式输出**：新增 `POST /ask/stream`，用 `StreamingResponse` 发 SSE。
   - 自己实现流式请求（不要改 `ai.py`）：拿 `ai.credentials()` 拼地址 + `ai.endpoint(base, "chat/completions")`，body 里 `stream: true`，逐行读 `data: {...}` 把 `choices[0].delta.content` 推给前端。
   - 事件格式自定（建议 `data: {"delta": "..."}` / `data: {"done": true, "sources": [...]}` / `data: {"error": "..."}`），前端用 `fetch` + `ReadableStream` 读（**不要用 EventSource，它不能 POST**）。
   - 流式失败要自动回退到普通 `POST /ask`。
3. **把回答存成笔记**：`POST /ask/save`，把某轮问答存成一篇笔记（标题=问题，正文=回答 + 引用来源列表），存完跳过去。
4. 检索：按第 3.3 的写法调用 `ai_embed.retrieve`，失败回退 `repo.retrieve_notes`；页面上要标明这次回答是「语义检索」还是「关键词检索」。
5. 测试：假 AI 服务返回**分块**的 SSE（至少 3 个 delta），验证 `/ask/stream` 能拼出完整回答；验证多轮历史进了 messages；验证存成笔记后笔记内容正确。

## 5. 通用要求

- **中文注释**、与现有代码风格一致；不要引入任何新的第三方依赖（只用标准库）。
- 改完**必须自己跑** `C:\repo\inknote\.venv\Scripts\python.exe -m pytest tests -q`，**必须 186+ 全绿**（你自己新增的用例算增量）。
- 每条新功能都要有测试，测试自己起假服务（参考 `tests/test_smoke.py` 里的 `fake_ai_server`，它在 `tests/test_smoke.py` 里，别改那个文件）。
- 写完在总结里说明：改了哪些文件、新增哪些接口、测试结果、**哪些地方需要主 agent 帮你接线**（比如新路由要注册、新模板要挂链接）。

## agent v2：批量工具 + 危险操作确认（2026-09-15）

### 新工具（agent 循环内）
- `bulk_add_tags` `{note_ids: int[], tags: string[]}` → `{updated, unchanged, missing, notes}`（一次 ≤50 篇，observe_limit 2600）
- `bulk_remove_tags` 同上，删指定标签（其余保留）
- `append_note` `{note_id, content}` → 在末尾追加正文（走版本历史）；「加一段」用它，别整篇重写
- `list_trash` `{limit?}` → 回收站笔记 + `days_left` 剩余可恢复天数

### 危险操作确认（机制层，不依赖提示词）
- `trash_note` 调用**不立即执行**：生成确认卡片（meta `agent.pending`，TTL 10 分钟），
  step 事件附带 `confirm: {id, note_id, title}`；提示词要求模型 final 提醒用户确认
- `POST /api/agent/confirm` `{confirm_id}` → 用户点「确认执行」后真正落地（绕过模型），
  结果记入执行历史；取走即删，同 id 不能执行两次
- `POST /api/agent/confirm/cancel` `{confirm_id}` → 作废卡片
- `_CONFIRM_TOOLS` 目前 = {trash_note}；后续加入其它危险操作只需扩这个集合

## agent v2：批量工具 + 危险操作确认（2026-09-15）

### 新工具（agent 循环内）
- `bulk_add_tags` `{note_ids: int[], tags: string[]}` → `{updated, unchanged, missing, notes}`（一次 ≤50 篇，observe_limit 2600）
- `bulk_remove_tags` 同上，删指定标签（其余保留）
- `append_note` `{note_id, content}` → 在末尾追加正文（走版本历史）；「加一段」用它，别整篇重写
- `list_trash` `{limit?}` → 回收站笔记 + `days_left` 剩余可恢复天数

### 危险操作确认（机制层，不依赖提示词）
- `trash_note` 调用**不立即执行**：生成确认卡片（meta `agent.pending`，TTL 10 分钟），
  step 事件附带 `confirm: {id, note_id, title}`；提示词要求模型 final 提醒用户确认
- `POST /api/agent/confirm` `{confirm_id}` → 用户点「确认执行」后真正落地（绕过模型），
  结果记入执行历史；取走即删，同 id 不能执行两次
- `POST /api/agent/confirm/cancel` `{confirm_id}` → 作废卡片
- `_CONFIRM_TOOLS` 目前 = {trash_note}；后续加入其它危险操作只需扩这个集合
