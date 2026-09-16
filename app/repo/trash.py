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
from .notes import get_note, set_flags, sync_derived
from .versions import snapshot


# ---------------------------------------------------------------------------
# 回收站
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 回收站
# ---------------------------------------------------------------------------
def soft_delete(conn: sqlite3.Connection, note_id: int) -> bool:
    note = get_note(conn, note_id)
    if note is None:
        return False
    conn.execute("UPDATE notes SET deleted_at = ?, is_public = 0 WHERE id = ?", (now_iso(), note_id))
    search_mod.remove_note(conn, note_id)
    return True


def restore(conn: sqlite3.Connection, note_id: int) -> bool:
    note = get_note(conn, note_id, include_deleted=True)
    if note is None or not note["deleted_at"]:
        return False
    conn.execute("UPDATE notes SET deleted_at = NULL, updated_at = ? WHERE id = ?", (now_iso(), note_id))
    fresh = get_note(conn, note_id)
    if fresh:
        sync_derived(conn, fresh, None)
    return True


def purge(conn: sqlite3.Connection, note_id: int) -> bool:
    note = get_note(conn, note_id, include_deleted=True)
    if note is None:
        return False
    search_mod.remove_note(conn, note_id)
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM note_tags)")
    return True


def empty_trash(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT id FROM notes WHERE deleted_at IS NOT NULL").fetchall()
    for row in rows:
        purge(conn, row["id"])
    return len(rows)


def purge_expired_trash(conn: sqlite3.Connection, days: int | None = None) -> int:
    days = days or settings.trash_days
    deadline = (now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT id FROM notes WHERE deleted_at IS NOT NULL AND deleted_at < ?", (deadline,)
    ).fetchall()
    for row in rows:
        purge(conn, row["id"])
    return len(rows)


def trash_days_left(deleted_at: str | None) -> int:
    dt = parse_dt(deleted_at)
    if not dt:
        return settings.trash_days
    elapsed = (now() - dt).days
    return max(0, settings.trash_days - elapsed)
