# 墨痕 InkNote · 项目约定（跨会话）

## 跑测试
- 全量：`.venv/Scripts/python.exe -m pytest tests -q`（约 72s，当前 **616 passed**）。
- **必须把临时目录指到项目内**，否则 pytest 会去清理 `%TEMP%/pytest-of-<user>/garbage-*`，
  沙箱会拦下这条命令（表现为 "user cancelled the bulk delete request"）：
  ```bash
  TEMP="C:/repo/inknote/.scratch/tmp" TMP="C:/repo/inknote/.scratch/tmp" \
    ./.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider
  ```
- 验收三件套：上面这条 + `python scripts/audit_css.py`（必须报「所有模板 class 都有对应样式」）
  + `python scripts/check.py --quick`。
- **shell 的 `bash` 工具 PATH 是坏的**，先 `export PATH="/usr/bin:/bin:$PATH"`。

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

## CSS 补丁流程（多 agent 并行轮次的惯例）
1. 并行 agent 只把自己的样式写进 `.scratch/css-patch-<名字>.css`，**不碰 `style.css`**，只用已有令牌。
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
