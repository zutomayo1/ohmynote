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


# ---------------------------------------------------------------------------
# 标签 / 分类 / 归档
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 标签 / 分类 / 归档
# ---------------------------------------------------------------------------
def list_tags(
    conn: sqlite3.Connection,
    *,
    public_only: bool = False,
    limit: int = 200,
    q: str = "",
    sort: str = "",
) -> list[dict[str, Any]]:
    """标签云数据。

    - 默认行为与以前完全一致：按使用次数倒序、名称升序，只含仍被「未删除笔记」引用的标签。
    - `q`：按标签名做大小写不敏感的子串筛选（LIKE，参数化 + 转义）。
    - `sort`：`"name"` 按名称；其余（含默认空串）按使用次数。
    """
    conditions = ["n.deleted_at IS NULL"]
    params: list[Any] = []
    if public_only:
        conditions.append("n.is_public = 1")
    keyword = (q or "").strip()
    if keyword:
        conditions.append("t.name LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like(keyword)}%")
    clause = " AND ".join(conditions)
    # 排序分支只来自固定映射，不接受调用方的任意 SQL 片段。
    order = "t.name COLLATE NOCASE ASC" if sort == "name" else "count DESC, t.name COLLATE NOCASE"
    rows = conn.execute(
        f"SELECT t.name AS name, COUNT(DISTINCT n.id) AS count FROM tags t "
        f"JOIN note_tags nt ON nt.tag_id = t.id JOIN notes n ON n.id = nt.note_id "
        f"WHERE {clause} GROUP BY t.id ORDER BY {order} LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [{"name": row["name"], "count": int(row["count"])} for row in rows]


def _valid_tag_name(raw: str) -> str:
    """校验并规范化「新标签名」，口径与 set_tags / normalize_tag 一致。

    去首尾空白、去开头的 #、把内部连续空白压成一个空格；空名或超过 MAX_TAG_LEN 抛 ValueError。
    """
    name = " ".join((raw or "").strip().lstrip("#").strip().split())
    if not name:
        raise ValueError("标签名不能为空")
    if len(name) > MAX_TAG_LEN:
        raise ValueError(f"标签名不能超过 {MAX_TAG_LEN} 个字符")
    return name


def _find_tag(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """按名字找标签（大小写不敏感）。"""
    return conn.execute(
        "SELECT id, name FROM tags WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()


def _tag_note_count(conn: sqlite3.Connection, tag_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT note_id) AS c FROM note_tags WHERE tag_id = ?", (tag_id,)
    ).fetchone()
    return int(row["c"] or 0)


def rename_tag(conn: sqlite3.Connection, old: str, new: str) -> dict[str, Any]:
    """把标签 old 改名成 new；new 已存在（大小写不敏感）时自动合并。

    合并规则：把 old 上的 note_tags 关系并入 new（`INSERT OR IGNORE` 去重），
    再删除 old 的关系与 tags 行；因此同一篇笔记不会出现重复关系，最终名字取已存在的 new 行。

    大小写处理：`python -> Python` 视为同一标签的「改大小写」，仍会写入新的大小写。
    old 不存在、new 为空/纯空白/超过 MAX_TAG_LEN 都抛 ValueError，且不改动任何数据。
    返回 {"renamed": 1, "merged": bool, "notes": 受影响笔记数, "name": 最终名字}。
    """
    old_name = " ".join((old or "").strip().lstrip("#").strip().split())
    if not old_name:
        raise ValueError("原标签名不能为空")
    new_name = _valid_tag_name(new)

    old_row = _find_tag(conn, old_name)
    if old_row is None:
        raise ValueError(f"标签「{old_name}」不存在")
    old_id = int(old_row["id"])
    notes = _tag_note_count(conn, old_id)

    # 除自己以外，是否已经有同名（忽略大小写）的标签？有 => 合并。
    existing = conn.execute(
        "SELECT id, name FROM tags WHERE name = ? COLLATE NOCASE AND id <> ?",
        (new_name, old_id),
    ).fetchone()
    if existing is None:
        # 单纯改名，包含「只改大小写」：同一行原地更新即可。
        conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, old_id))
        return {"renamed": 1, "merged": False, "notes": notes, "name": new_name}

    target_id = int(existing["id"])
    target_name = str(existing["name"])
    # 关系并集：旧标签已有的笔记全部指向目标标签；目标标签已有的关系靠 OR IGNORE 去重。
    conn.execute(
        "INSERT OR IGNORE INTO note_tags (note_id, tag_id) "
        "SELECT note_id, ? FROM note_tags WHERE tag_id = ?",
        (target_id, old_id),
    )
    conn.execute("DELETE FROM note_tags WHERE tag_id = ?", (old_id,))
    conn.execute("DELETE FROM tags WHERE id = ?", (old_id,))
    return {"renamed": 1, "merged": True, "notes": notes, "name": target_name}


def delete_tag(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """从所有笔记上摘掉标签并删除 tags 行（含孤儿关系）。

    不存在（含空名）时返回 {"deleted": False, "notes": 0}，不抛异常。
    """
    clean = " ".join((name or "").strip().lstrip("#").strip().split())
    row = _find_tag(conn, clean) if clean else None
    if row is None:
        return {"deleted": False, "notes": 0}
    tag_id = int(row["id"])
    notes = _tag_note_count(conn, tag_id)
    conn.execute("DELETE FROM note_tags WHERE tag_id = ?", (tag_id,))
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    return {"deleted": True, "notes": notes}


def purge_unused_tags(conn: sqlite3.Connection) -> int:
    """清掉没有任何 note_tags 关系的残留标签行，返回删除条数。

    list_tags 的 JOIN 本来就不会显示它们，但行会一直滞留在库里；这里集中清理。
    """
    cursor = conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM note_tags)")
    return max(0, int(cursor.rowcount or 0))


def list_categories(conn: sqlite3.Connection, *, public_only: bool = False) -> list[dict[str, Any]]:
    conditions = ["deleted_at IS NULL", "category <> ''"]
    if public_only:
        conditions.append("is_public = 1")
    rows = conn.execute(
        f"SELECT category AS name, COUNT(*) AS count FROM notes WHERE {' AND '.join(conditions)} "
        f"GROUP BY category ORDER BY count DESC, category LIMIT 100"
    ).fetchall()
    return [{"name": row["name"], "count": int(row["count"])} for row in rows]


def archive_months(conn: sqlite3.Connection, *, public_only: bool = True) -> list[dict[str, Any]]:
    conditions = ["deleted_at IS NULL"]
    if public_only:
        conditions.append("is_public = 1")
    rows = conn.execute(
        f"SELECT substr(updated_at, 1, 7) AS ym, COUNT(*) AS count FROM notes "
        f"WHERE {' AND '.join(conditions)} GROUP BY ym ORDER BY ym DESC LIMIT 120"
    ).fetchall()
    months: list[dict[str, Any]] = []
    for row in rows:
        key = row["ym"] or ""
        if len(key) < 7:
            continue
        months.append(
            {
                "key": key,
                "year": key[:4],
                "month": key[5:7],
                "label": month_label(key),
                "count": int(row["count"]),
            }
        )
    return months
