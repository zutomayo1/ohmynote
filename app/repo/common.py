"""数据访问层：笔记、标签、版本、双链、模板、统计。

约定：所有函数都接收一个已打开的 sqlite3.Connection，事务由调用方（db.db() 上下文）负责。
"""

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

# 每条都以「置顶优先 + 置顶区内手工顺序（sort_order）」开头，主排序键在后。
# 非置顶笔记的 sort_order 恒为 0，所以它们的主排序键不受影响。
SORTS = {
    "updated": ("n.is_pinned DESC, n.sort_order ASC, n.updated_at DESC, n.id DESC", "最近更新"),
    "created": ("n.is_pinned DESC, n.sort_order ASC, n.created_at DESC, n.id DESC", "创建时间"),
    "words": ("n.is_pinned DESC, n.sort_order ASC, n.word_count DESC, n.id DESC", "字数最多"),
    "title": ("n.is_pinned DESC, n.sort_order ASC, n.title ASC", "标题"),
}

FLAGS = ("is_public", "is_pinned", "is_starred")
STATUSES = ("draft", "saved")
AUTOSAVE_COALESCE_SECONDS = 600



from ..markdown_render import map_outside_code  # noqa: F401  (links/notes 等经此复用)

# ---------------------------------------------------------------------------
# 行 -> 字典
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 行 -> 字典
# ---------------------------------------------------------------------------
def row_to_note(row: sqlite3.Row, tags: list[str] | None = None) -> dict[str, Any]:
    note = {key: row[key] for key in row.keys()}
    note["is_public"] = bool(note.get("is_public"))
    note["is_pinned"] = bool(note.get("is_pinned"))
    note["is_starred"] = bool(note.get("is_starred"))
    note["is_archived"] = bool(note.get("is_archived"))
    note["tags"] = tags or []
    note["url"] = f"/notes/{note['id']}"
    note["blog_url"] = f"/blog/{note['slug']}" if note["is_public"] and note.get("slug") else ""
    note["deleted"] = bool(note.get("deleted_at"))
    return note


def _tags_map(conn: sqlite3.Connection, note_ids: Sequence[int]) -> dict[int, list[str]]:
    ids = [int(i) for i in note_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT nt.note_id AS note_id, t.name AS name FROM note_tags nt "
        f"JOIN tags t ON t.id = nt.tag_id WHERE nt.note_id IN ({placeholders}) "
        f"ORDER BY t.name COLLATE NOCASE",
        ids,
    ).fetchall()
    result: dict[int, list[str]] = {i: [] for i in ids}
    for row in rows:
        result.setdefault(row["note_id"], []).append(row["name"])
    return result


def hydrate(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    tags_map = _tags_map(conn, [row["id"] for row in rows])
    return [row_to_note(row, tags_map.get(row["id"], [])) for row in rows]
