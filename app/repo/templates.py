"""数据访问层子模块：由 repo.py 按分区拆出，函数签名与行为不变。"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from .. import search as search_mod
from ..deps import MAX_SQLITE_INT
from ..config import settings
from ..markdown_render import WikiRef, make_excerpt, text_stats
from ..utils import (
    MAX_TAG_LEN,
    MAX_TAGS,
    escape_like,
    extract_inline_tags,
    month_label,
    now,
    now_iso,
    parse_dt,
    parse_tags,
    slugify,
    sanitize_slug,
)

from .common import *  # noqa: F401,F403  共享常量与行->字典工具
from .notes import create_note, set_flags


# ---------------------------------------------------------------------------
# 笔记模板
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 笔记模板
# ---------------------------------------------------------------------------
DEFAULT_TEMPLATES: list[tuple[str, str, str]] = [
    ("空白笔记", "只有标题和内容区，适合随手写", ""),
    (
        "读书笔记",
        "书名 / 核心观点 / 摘抄 / 我的思考",
        """## 基本信息

- 书名：
- 作者：
- 评分：★★★★☆
- 读完日期：

## 一句话概括


## 核心观点

1.
2.

## 摘抄

> 

## 我的思考


## 行动清单

- [ ] 
""",
    ),
    (
        "周报",
        "本周完成 / 进行中 / 风险 / 下周计划",
        """## 本周完成

- [ ] 
- [ ] 

## 进行中


## 问题与风险


## 下周计划

- [ ] 
""",
    ),
    (
        "会议记录",
        "议题 / 结论 / 待办",
        """## 会议信息

- 时间：
- 参与人：
- 议题：

## 讨论要点


## 结论


## 待办

- [ ] 事项 —— 负责人 —— 截止时间
""",
    ),
    (
        "技术笔记 / 踩坑记录",
        "问题现象 / 排查过程 / 结论与参考",
        """## 问题现象


## 环境


## 排查过程

1.
2.

## 结论

```text

```

## 参考

- 
""",
    ),
    (
        "每日复盘",
        "三件好事 / 待改进 / 明日计划",
        """## 今天做了什么


## 三件好事

1.
2.
3.

## 待改进


## 明日计划

- [ ] 
""",
    ),
]


def seed_templates(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS c FROM templates").fetchone()
    if row and int(row["c"]) > 0:
        return
    stamp = now_iso()
    for index, (name, description, content) in enumerate(DEFAULT_TEMPLATES):
        conn.execute(
            "INSERT INTO templates (name, description, content, sort_order, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, content, index * 10, stamp, stamp),
        )


def list_templates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM templates ORDER BY sort_order, id"
    ).fetchall()
    return [dict(row) for row in rows]


def get_template(conn: sqlite3.Connection, template_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    return dict(row) if row else None


def save_template(
    conn: sqlite3.Connection,
    *,
    template_id: int | None = None,
    name: str,
    description: str = "",
    content: str = "",
) -> int:
    stamp = now_iso()
    name = (name or "").strip() or "未命名模板"
    if template_id:
        conn.execute(
            "UPDATE templates SET name = ?, description = ?, content = ?, updated_at = ? WHERE id = ?",
            (name, description.strip(), content, stamp, template_id),
        )
        return template_id
    cursor = conn.execute(
        "INSERT INTO templates (name, description, content, sort_order, created_at, updated_at)"
        " VALUES (?, ?, ?, 100, ?, ?)",
        (name, description.strip(), content, stamp, stamp),
    )
    return int(cursor.lastrowid or 0)


def delete_template(conn: sqlite3.Connection, template_id: int) -> None:
    conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))

# ---------------------------------------------------------------------------
# 模板排序
# ---------------------------------------------------------------------------
def reorder_templates(conn: sqlite3.Connection, ids: list[int]) -> int:
    """按传入顺序把 ``templates.sort_order`` 写成 ``0..n-1``（同一事务）。

    约定（与 :func:`pages._parse_template_id` 一致）：整数 id 先过
    ``MAX_SQLITE_INT`` 范围校验，再绑 SQL——超范围的 id 绑参会抛
    ``OverflowError`` 变成 500，所以这里直接跳过。

    - 非整数 / 超 SQLite 整数上限的 id → 跳过（防 500）；
    - 库里不存在的 id → UPDATE 影响 0 行，同样跳过；
    - 返回值 = 实际更新到的行数。
    """
    ordered: list[int] = []
    for raw in ids:
        if isinstance(raw, bool):  # bool 是 int 的子类，单独排除
            continue
        if not isinstance(raw, int):
            continue
        if not 1 <= raw <= MAX_SQLITE_INT:
            continue
        ordered.append(raw)
    if not ordered:
        return 0
    updated = 0
    for index, tid in enumerate(ordered):
        cursor = conn.execute(
            "UPDATE templates SET sort_order = ? WHERE id = ?", (index, tid)
        )
        updated += cursor.rowcount
    return updated
