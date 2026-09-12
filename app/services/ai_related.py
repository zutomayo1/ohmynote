"""详情页「相关笔记」的语义实现。

复用 ``ai_embed`` 已经存好的向量和余弦相似度，不重写索引 / 相似度逻辑。
对外只暴露 ``related_notes``：任何不可用 / 失败都返回 None，让路由回退
``repo.related_notes`` 的关键词逻辑，页面绝不因为语义推荐而 500。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from . import ai_embed

logger = logging.getLogger("inknote.ai_related")

ENGINE = "semantic"
REASON = "语义相关"


def related_notes(
    conn: sqlite3.Connection, note: dict[str, Any], *, limit: int = 5
) -> list[dict[str, Any]] | None:
    """语义优先的相关笔记；不可用返回 None（调用方回退关键词逻辑）。

    结果在 ``ai_embed.similar_notes`` 的结构上补 ``engine="semantic"`` 和
    ``reason``，字段保持详情页 ``link_list`` 需要的 id / title / url / score / snippet。
    """
    if not isinstance(note, dict):
        return None
    note_id = note.get("id")
    if not isinstance(note_id, int) or isinstance(note_id, bool):
        return None
    try:
        items = ai_embed.similar_notes(conn, note_id, limit=limit)
    except Exception:
        # similar_notes 自己也会兜底；这里再拦一层，保证路由绝不会因为语义推荐 500
        logger.warning("语义相关笔记计算异常，回退关键词推荐（note_id=%s）", note_id, exc_info=True)
        return None
    if not items:
        return None
    output: list[dict[str, Any]] = []
    for item in items:
        entry = dict(item)
        entry["engine"] = ENGINE
        entry.setdefault("reason", REASON)
        output.append(entry)
    return output
