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
# 博客互动统计
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 博客互动统计（点赞 / 阅读）：按 slug 计数，公开页专用
# ---------------------------------------------------------------------------
def blog_stats_get(conn: sqlite3.Connection, slug: str) -> dict[str, int]:
    row = conn.execute(
        "SELECT likes, reads FROM blog_stats WHERE slug = ?", (slug,)
    ).fetchone()
    if row is None:
        return {"likes": 0, "reads": 0}
    return {"likes": int(row["likes"] or 0), "reads": int(row["reads"] or 0)}


def blog_stats_like(conn: sqlite3.Connection, slug: str) -> int:
    """点赞 +1（原子），返回最新总数。"""
    conn.execute(
        "INSERT INTO blog_stats (slug, likes) VALUES (?, 1) "
        "ON CONFLICT(slug) DO UPDATE SET likes = likes + 1",
        (slug,),
    )
    conn.commit()
    return blog_stats_get(conn, slug)["likes"]


def blog_stats_add_read(conn: sqlite3.Connection, slug: str) -> None:
    conn.execute(
        "INSERT INTO blog_stats (slug, reads) VALUES (?, 1) "
        "ON CONFLICT(slug) DO UPDATE SET reads = reads + 1",
        (slug,),
    )
    conn.commit()
