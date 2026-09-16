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
# restore_version 需要 update_note：函数内延迟导入避免 notes↔versions 循环


# ---------------------------------------------------------------------------
# 版本历史
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 版本历史
# ---------------------------------------------------------------------------
def snapshot(conn: sqlite3.Connection, note: dict[str, Any], *, reason: str = "manual", force: bool = False) -> None:
    """把笔记的「当前状态」存成一个历史版本。"""
    if not force and reason == "autosave":
        row = conn.execute(
            "SELECT id, created_at, reason FROM note_versions WHERE note_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (note["id"],),
        ).fetchone()
        if row is not None and row["reason"] == "autosave":
            created = parse_dt(row["created_at"])
            if created and (now() - created).total_seconds() < AUTOSAVE_COALESCE_SECONDS:
                return  # 同一次连续编辑只留一个快照
    conn.execute(
        "INSERT INTO note_versions (note_id, title, content, tags, summary, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            note["id"],
            note["title"],
            note["content"],
            "，".join(note.get("tags") or []),
            note["summary"],
            reason,
            now_iso(),
        ),
    )
    prune_versions(conn, note["id"])


def prune_versions(conn: sqlite3.Connection, note_id: int, keep: int | None = None) -> None:
    keep = keep or settings.version_keep
    rows = conn.execute(
        "SELECT id FROM note_versions WHERE note_id = ? ORDER BY created_at DESC, id DESC", (note_id,)
    ).fetchall()
    for row in rows[keep:]:
        conn.execute("DELETE FROM note_versions WHERE id = ?", (row["id"],))


def list_versions(conn: sqlite3.Connection, note_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, note_id, title, tags, summary, reason, created_at, length(content) AS size"
        " FROM note_versions WHERE note_id = ? ORDER BY created_at DESC, id DESC",
        (note_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_version(conn: sqlite3.Connection, note_id: int, version_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM note_versions WHERE id = ? AND note_id = ?", (version_id, note_id)
    ).fetchone()
    return dict(row) if row else None


def restore_version(conn: sqlite3.Connection, note_id: int, version_id: int) -> dict[str, Any] | None:
    # 延迟导入：notes 模块级依赖本模块的 snapshot，反向导入只能放函数内
    from .notes import get_note, update_note

    version = get_version(conn, note_id, version_id)
    if version is None:
        return None
    current = get_note(conn, note_id, include_deleted=True)
    if current is None:
        return None
    snapshot(conn, current, reason="before-restore", force=True)
    return update_note(
        conn,
        note_id,
        title=version["title"],
        content=version["content"],
        tags=version["tags"],
        summary=version["summary"] or "",
        reason="restore",
    )
