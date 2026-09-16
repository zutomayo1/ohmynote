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
# 站点配置
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 站点配置（存在 meta 表里：设置页写、启动时读）
# ---------------------------------------------------------------------------
def get_meta_map(conn: sqlite3.Connection, prefix: str = "") -> dict[str, str]:
    """取出以 prefix 开头的配置项，键去掉前缀（如 ai.model）。"""
    if prefix:
        pattern = prefix.replace("%", "\\%").replace("_", "\\_") + "%"
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ? ESCAPE '\\'", (pattern,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    return {row["key"][len(prefix):]: row["value"] for row in rows}


def save_meta_map(conn: sqlite3.Connection, values: dict[str, Any], prefix: str = "") -> None:
    for key, value in values.items():
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (f"{prefix}{key}", str(value)),
        )


def delete_meta(conn: sqlite3.Connection, keys: list[str]) -> None:
    for key in keys:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))
