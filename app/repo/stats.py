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
# 统计面板
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 统计面板
# ---------------------------------------------------------------------------
def activity_dates(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT date(created_at) AS d FROM notes "
        "UNION SELECT date(updated_at) AS d FROM notes "
        "UNION SELECT date(created_at) AS d FROM note_versions"
    ).fetchall()
    return {row["d"] for row in rows if row["d"]}


def writing_streak(conn: sqlite3.Connection) -> int:
    days = activity_dates(conn)
    if not days:
        return 0
    today = date.today()
    start = today if today.isoformat() in days else today - timedelta(days=1)
    if start.isoformat() not in days:
        return 0
    streak = 0
    cursor = start
    while cursor.isoformat() in days and streak < 3650:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def dashboard_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END) AS drafts,"
        " SUM(CASE WHEN is_public = 1 THEN 1 ELSE 0 END) AS public_count,"
        " SUM(CASE WHEN is_starred = 1 THEN 1 ELSE 0 END) AS starred,"
        " COALESCE(SUM(word_count), 0) AS words"
        " FROM notes WHERE deleted_at IS NULL"
    ).fetchone()
    today = now().strftime("%Y-%m-%d")
    today_row = conn.execute(
        "SELECT COUNT(*) AS updated, COALESCE(SUM(CASE WHEN date(created_at) = ? THEN word_count ELSE 0 END), 0) AS words"
        " FROM notes WHERE deleted_at IS NULL AND date(updated_at) = ?",
        (today, today),
    ).fetchone()
    trash_row = conn.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE deleted_at IS NOT NULL"
    ).fetchone()
    tag_row = conn.execute("SELECT COUNT(*) AS c FROM tags").fetchone()
    return {
        "total": int(row["total"] or 0),
        "drafts": int(row["drafts"] or 0),
        "saved": int(row["total"] or 0) - int(row["drafts"] or 0),
        "public_count": int(row["public_count"] or 0),
        "starred": int(row["starred"] or 0),
        "words": int(row["words"] or 0),
        "tags": int(tag_row["c"] or 0),
        "trash": int(trash_row["c"] or 0),
        "today_updated": int(today_row["updated"] or 0),
        "today_words": int(today_row["words"] or 0),
        "streak": writing_streak(conn),
        "active_days": len(activity_dates(conn)),
        "reading_minutes": math.ceil(int(row["words"] or 0) / 400) if row["words"] else 0,
    }


def find_notes_by_title(conn: sqlite3.Connection, keyword: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """按标题模糊匹配（「待创建」的反向场景：用标题找已存在的笔记）。"""
    rows = conn.execute(
        "SELECT * FROM notes WHERE deleted_at IS NULL AND title LIKE ? ESCAPE '\\'"
        " ORDER BY updated_at DESC LIMIT ?",
        (f"%{escape_like(keyword)}%", limit),
    ).fetchall()
    return hydrate(conn, rows)
