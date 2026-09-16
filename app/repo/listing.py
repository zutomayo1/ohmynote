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
from .notes import get_note


# ---------------------------------------------------------------------------
# 列表 / 检索
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 列表 / 检索
# ---------------------------------------------------------------------------
def _apply_python_filters(
    note: dict[str, Any],
    *,
    tag: str = "",
    category: str = "",
    status: str = "",
    fav: str = "",
    month: str = "",
) -> bool:
    if tag and tag.lower() not in {name.lower() for name in note["tags"]}:
        return False
    if category and note["category"] != category:
        return False
    if month and not (note.get("updated_at") or "").startswith(month):
        return False
    if status in STATUSES and note["status"] != status:
        return False
    if fav == "starred" and not note["is_starred"]:
        return False
    if fav == "pinned" and not note["is_pinned"]:
        return False
    if fav == "public" and not note["is_public"]:
        return False
    if fav == "draft" and note["status"] != "draft":
        return False
    return True


def filter_notes(
    notes: list[dict[str, Any]],
    *,
    tag: str = "",
    category: str = "",
    status: str = "",
    fav: str = "",
    month: str = "",
) -> list[dict[str, Any]]:
    """按**与列表页完全一致**的口径筛一批已经取回来的笔记。

    搜索结果页用它：关键词/语义检索负责「找出来」，筛选条件负责「再缩小」，
    复用 `_apply_python_filters` 就不会出现「列表页能筛、搜索页筛不出来」的口径分叉。
    """
    return [
        note
        for note in notes
        if _apply_python_filters(
            note, tag=tag, category=category, status=status, fav=fav, month=month
        )
    ]


# 与 SORTS 里的 ORDER BY 一一对应：置顶优先 + 主键 + id 兜底。
_PY_SORT_KEYS = {
    "updated": ("updated_at", True),
    "created": ("created_at", True),
    "words": ("word_count", True),
    "title": ("title", False),
}


def sort_notes(
    notes: list[dict[str, Any]], *, sort: str = "updated", pinned_first: bool = True
) -> list[dict[str, Any]]:
    """在 Python 侧排序，口径对齐 `SORTS`（置顶优先、同键按 id 倒序）。

    搜索结果默认要保持**相关度**顺序，所以调用方只在用户显式选了排序时才用这个函数。
    """
    field, desc = _PY_SORT_KEYS.get(sort, _PY_SORT_KEYS["updated"])
    if field == "word_count":
        key = lambda note: (int(note.get(field) or 0), note.get("id") or 0)  # noqa: E731
    else:
        key = lambda note: (note.get(field) or "", note.get("id") or 0)  # noqa: E731
    ordered = sorted(notes, key=key, reverse=desc)
    if not pinned_first:
        return ordered
    # 口径与 SORTS 一致：置顶在前，置顶区内部再按 sort_order（手工拖拽的顺序）。
    # 必须分两组排——若把 sort_order 混进同一个 key，非置顶笔记的 0 会插在
    # 置顶笔记中间（稳定排序只保证「同键」的相对顺序）。
    pinned = [note for note in ordered if note.get("is_pinned")]
    rest = [note for note in ordered if not note.get("is_pinned")]
    pinned.sort(key=lambda note: int(note.get("sort_order") or 0))
    return pinned + rest


def list_notes(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    tag: str = "",
    category: str = "",
    status: str = "",
    fav: str = "",
    month: str = "",
    sort: str = "updated",
    page: int = 1,
    per_page: int = 12,
    public_only: bool = False,
    include_deleted: bool = False,
    archived: bool | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """返回 (当前页笔记, 命中总数)。带关键词时走全文检索，其余筛选在 Python 里做。

    archived=None 不过滤（搜索等场景）；False 排除归档（默认列表）；True 只看归档。
    """
    # 页码在最里面也夹一次：无论调用方传了什么，都不会出现巨大的 OFFSET
    page = max(1, min(int(page or 1), 1_000_000))
    per_page = max(1, min(int(per_page or 12), 1000))

    if q.strip():
        rows = [
            row
            for row, _score, _snippet in search_mod.search(
                conn, q, public_only=public_only, include_deleted=include_deleted, limit=400
            )
        ]
        notes = hydrate(conn, rows)
        notes = [
            note
            for note in notes
            if (archived is None or bool(note.get("is_archived")) == archived)
            and _apply_python_filters(
                note, tag=tag, category=category, status=status, fav=fav, month=month
            )
        ]
        total = len(notes)
        start = (page - 1) * per_page
        return notes[start : start + per_page], total

    where: list[str] = []
    params: list[Any] = []
    if include_deleted:
        where.append("n.deleted_at IS NOT NULL")
    else:
        where.append("n.deleted_at IS NULL")
    if public_only:
        where.append("n.is_public = 1")
    if status in STATUSES:
        where.append("n.status = ?")
        params.append(status)
    if category:
        where.append("n.category = ?")
        params.append(category)
    if month:
        where.append("substr(n.updated_at, 1, 7) = ?")
        params.append(month)
    if archived is not None:
        where.append("n.is_archived = ?" if archived else "n.is_archived = 0")
        if archived:
            params.append(1)
    if tag:
        where.append(
            "EXISTS (SELECT 1 FROM note_tags nt JOIN tags t ON t.id = nt.tag_id "
            "WHERE nt.note_id = n.id AND t.name = ? COLLATE NOCASE)"
        )
        params.append(tag)
    if fav in {"starred", "pinned", "public"}:
        column = {"starred": "is_starred", "pinned": "is_pinned", "public": "is_public"}[fav]
        where.append(f"n.{column} = 1")

    clause = " AND ".join(where)
    total = int(conn.execute(f"SELECT COUNT(*) AS c FROM notes n WHERE {clause}", params).fetchone()["c"])
    order = SORTS.get(sort, SORTS["updated"])[0]
    if public_only:
        order = order.replace("n.is_pinned DESC, ", "")
    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT n.* FROM notes n WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, per_page, offset],
    ).fetchall()
    return hydrate(conn, rows), total


def search_notes(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 30,
    public_only: bool = False,
) -> list[dict[str, Any]]:
    """给搜索结果页 / 命令面板用：带 snippet 与 score。"""
    results = search_mod.search(conn, query, public_only=public_only, limit=limit)
    if not results:
        return []
    notes = hydrate(conn, [row for row, _score, _snippet in results])
    by_id = {note["id"]: note for note in notes}
    output: list[dict[str, Any]] = []
    for row, score, snippet in results:
        note = by_id.get(row["id"])
        if not note:
            continue
        note["score"] = score
        note["snippet"] = snippet
        note["tokens"] = search_mod.highlight_tokens(query)
        output.append(note)
    return output


def retrieve_notes(conn: sqlite3.Connection, question: str, *, limit: int = 6) -> list[dict[str, Any]]:
    """给「问笔记」用的粗排检索，返回带 snippet 的笔记。"""
    results = search_mod.retrieve(conn, question, limit=limit)
    if not results:
        return []
    notes = hydrate(conn, [row for row, _score, _snippet in results])
    by_id = {note["id"]: note for note in notes}
    output: list[dict[str, Any]] = []
    for row, score, snippet in results:
        note = by_id.get(row["id"])
        if not note:
            continue
        note["score"] = score
        note["snippet"] = snippet
        output.append(note)
    return output


def adjacent_notes(conn: sqlite3.Connection, note: dict[str, Any], *, public_only: bool = False) -> tuple[dict | None, dict | None]:
    """上一篇（更新）/ 下一篇（更旧）。"""
    conditions = ["deleted_at IS NULL"]
    params: list[Any] = []
    if public_only:
        conditions.append("is_public = 1")
    clause = " AND ".join(conditions)
    older = conn.execute(
        f"SELECT * FROM notes WHERE {clause} AND (updated_at < ? OR (updated_at = ? AND id < ?))"
        " ORDER BY updated_at DESC, id DESC LIMIT 1",
        (*params, note["updated_at"], note["updated_at"], note["id"]),
    ).fetchone()
    newer = conn.execute(
        f"SELECT * FROM notes WHERE {clause} AND (updated_at > ? OR (updated_at = ? AND id > ?))"
        " ORDER BY updated_at ASC, id ASC LIMIT 1",
        (*params, note["updated_at"], note["updated_at"], note["id"]),
    ).fetchone()
    return (hydrate(conn, [newer])[0] if newer else None, hydrate(conn, [older])[0] if older else None)


def all_notes(
    conn: sqlite3.Connection,
    *,
    sort: str = "created",
    public_only: bool = False,
) -> list[dict[str, Any]]:
    """不分页地取回全部笔记（导出 / 订阅源用，避免被 per_page 上限截断）。"""
    where = ["n.deleted_at IS NULL"]
    if public_only:
        where.append("n.is_public = 1")
    order = SORTS.get(sort, SORTS["updated"])[0]
    if public_only:
        order = order.replace("n.is_pinned DESC, ", "")
    rows = conn.execute(
        f"SELECT n.* FROM notes n WHERE {' AND '.join(where)} ORDER BY {order}"
    ).fetchall()
    return hydrate(conn, rows)
