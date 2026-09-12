"""「问笔记」的多轮会话存储。

和笔记本身一样存在同一个 SQLite 里，所以刷新、重启都还在。
表是**惰性创建**的（`CREATE TABLE IF NOT EXISTS`），不改 db.py：

    ai_conversations(id, title, created_at, updated_at)                -- 一次提问线程
    ai_messages(id, conversation_id, role, content, sources, engine, created_at)

`ai_messages.sources` 存 JSON 字符串：这轮回答引用到的笔记 `[{title,url,snippet}]`；
`engine` 记这次检索用的是 `semantic`（向量）还是 `keyword`（关键词）。

对外接口（其他模块只应该用这些）：
    ensure / create / add_message / history / get / recent / rename / delete / clear_all
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from ..utils import now_iso

logger = logging.getLogger("inknote.ai_chat")

# 会话标题默认取第一句问题，最多留这么多字
TITLE_LIMIT = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS ai_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role            TEXT,
    content         TEXT,
    sources         TEXT,
    engine          TEXT,
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_messages_conversation ON ai_messages (conversation_id, id);
"""


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------
def _tables_ready(conn: sqlite3.Connection) -> bool:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('ai_conversations', 'ai_messages')"
    ).fetchall()
    return len(rows) >= 2


def ensure(conn: sqlite3.Connection) -> None:
    """确保两张表存在（幂等）。已经建好时不再执行脚本，避免重复提交事务。"""
    if _tables_ready(conn):
        return
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------
def _title_from(question: str) -> str:
    text = " ".join((question or "").split())
    return text[:TITLE_LIMIT] or "新对话"


def _new_id(cursor: sqlite3.Cursor) -> int:
    return int(cursor.lastrowid or 0)


def _dump_sources(sources) -> str:
    if not sources:
        return ""
    try:
        return json.dumps(list(sources), ensure_ascii=False)
    except (TypeError, ValueError):
        # 来源列表里有不可序列化的对象：存空串不影响这轮回答，但留痕方便查来源为什么丢了
        logger.debug(
            "会话来源无法序列化为 JSON（类型 %s），已存空字符串",
            type(sources).__name__,
            exc_info=True,
        )
        return ""


def parse_sources(raw: Any) -> list[dict]:
    """把库里存的 JSON 字符串还原成列表；坏数据一律当空列表。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        # 历史坏数据（比如手工改过库）当空列表，不能让「问笔记」页面直接炸
        logger.debug("会话消息里的 sources 不是合法 JSON，按空列表处理（raw=%r）", str(raw)[:80])
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _message_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "role": row["role"] or "",
        "content": row["content"] or "",
        "sources": parse_sources(row["sources"]),
        "engine": row["engine"] or "",
        "created_at": row["created_at"] or "",
    }


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
def create(conn: sqlite3.Connection, question: str) -> int:
    """新建一次会话，标题默认取第一句问题（截 40 字）。返回会话 id。"""
    ensure(conn)
    stamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO ai_conversations (title, created_at, updated_at) VALUES (?, ?, ?)",
        (_title_from(question), stamp, stamp),
    )
    return _new_id(cursor)


def add_message(
    conn: sqlite3.Connection,
    conversation_id: int,
    *,
    role: str,
    content: str,
    sources=None,
    engine: str = "",
) -> int:
    """往会话里追加一条消息，并刷新会话的 updated_at。返回消息 id。"""
    ensure(conn)
    cid = int(conversation_id)
    stamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO ai_messages (conversation_id, role, content, sources, engine, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (cid, str(role or ""), str(content or ""), _dump_sources(sources), str(engine or ""), stamp),
    )
    conn.execute("UPDATE ai_conversations SET updated_at = ? WHERE id = ?", (stamp, cid))
    return _new_id(cursor)


def history(conn: sqlite3.Connection, conversation_id: int, limit: int = 6) -> list[dict]:
    """取最近 limit 条消息，按时间正序返回 `[{"role","content"}, ...]`，直接喂给 ai.answer。"""
    ensure(conn)
    try:
        count = max(0, int(limit))
    except (TypeError, ValueError):
        # limit 由调用方代码给，理论上一直是 int；真传坏了就退回默认值并留痕
        logger.debug("history 的 limit 不是整数（limit=%r），改用默认值 6", limit)
        count = 6
    if count <= 0:
        return []
    rows = conn.execute(
        "SELECT role, content FROM ai_messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (int(conversation_id), count),
    ).fetchall()
    return [{"role": row["role"] or "", "content": row["content"] or ""} for row in reversed(rows)]


def get(conn: sqlite3.Connection, conversation_id: int) -> dict | None:
    """读整个会话（含全部消息）；不存在返回 None。"""
    ensure(conn)
    cid = int(conversation_id)
    row = conn.execute("SELECT * FROM ai_conversations WHERE id = ?", (cid,)).fetchone()
    if row is None:
        return None
    messages = conn.execute(
        "SELECT * FROM ai_messages WHERE conversation_id = ? ORDER BY id", (cid,)
    ).fetchall()
    return {
        "id": int(row["id"]),
        "title": row["title"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "messages": [_message_row_to_dict(item) for item in messages],
    }


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """侧栏「最近提问」：按更新时间倒序，附消息条数与最后一句问题。"""
    ensure(conn)
    try:
        count = max(0, int(limit))
    except (TypeError, ValueError):
        # 同 history：坏 limit 退回默认 20，不影响侧栏渲染
        logger.debug("recent 的 limit 不是整数（limit=%r），改用默认值 20", limit)
        count = 20
    rows = conn.execute(
        "SELECT c.id AS id, c.title AS title, c.created_at AS created_at, c.updated_at AS updated_at,"
        " (SELECT COUNT(*) FROM ai_messages m WHERE m.conversation_id = c.id) AS message_count,"
        " (SELECT m.content FROM ai_messages m WHERE m.conversation_id = c.id AND m.role = 'user'"
        "  ORDER BY m.id DESC LIMIT 1) AS last_question"
        " FROM ai_conversations c ORDER BY c.updated_at DESC, c.id DESC LIMIT ?",
        (count,),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "title": row["title"] or "",
            "created_at": row["created_at"] or "",
            "updated_at": row["updated_at"] or "",
            "message_count": int(row["message_count"] or 0),
            "last_question": row["last_question"] or "",
        }
        for row in rows
    ]


def rename(conn: sqlite3.Connection, conversation_id: int, title: str) -> None:
    """改会话标题（留空则回到「新对话」）。"""
    ensure(conn)
    clean = " ".join((title or "").split())[:TITLE_LIMIT] or "新对话"
    conn.execute(
        "UPDATE ai_conversations SET title = ?, updated_at = ? WHERE id = ?",
        (clean, now_iso(), int(conversation_id)),
    )


def delete(conn: sqlite3.Connection, conversation_id: int) -> bool:
    """删掉一次会话及其全部消息；真的删到了才返回 True。"""
    ensure(conn)
    cid = int(conversation_id)
    cursor = conn.execute("DELETE FROM ai_conversations WHERE id = ?", (cid,))
    conn.execute("DELETE FROM ai_messages WHERE conversation_id = ?", (cid,))
    return int(cursor.rowcount or 0) > 0


def clear_all(conn: sqlite3.Connection) -> int:
    """清空所有会话（测试与维护用），返回删掉的会话数。"""
    ensure(conn)
    cursor = conn.execute("DELETE FROM ai_conversations")
    conn.execute("DELETE FROM ai_messages")
    return int(cursor.rowcount or 0)


# ---------------------------------------------------------------------------
# 存成笔记时需要的「一轮问答」
# ---------------------------------------------------------------------------
def get_turn(conn: sqlite3.Connection, message_id: int) -> dict | None:
    """给 /ask/save 用：由回答消息 id 找出「问题 + 回答 + 引用来源」。

    同时兼容传进来的是用户消息 id（往后找紧跟的回答）。
    """
    ensure(conn)
    row = conn.execute("SELECT * FROM ai_messages WHERE id = ?", (int(message_id),)).fetchone()
    if row is None:
        return None
    cid = int(row["conversation_id"])
    if (row["role"] or "") == "assistant":
        question_row = conn.execute(
            "SELECT content FROM ai_messages WHERE conversation_id = ? AND id < ? AND role = 'user'"
            " ORDER BY id DESC LIMIT 1",
            (cid, row["id"]),
        ).fetchone()
        return {
            "conversation_id": cid,
            "question": (question_row["content"] if question_row else "") or "",
            "answer": row["content"] or "",
            "sources": parse_sources(row["sources"]),
            "engine": row["engine"] or "",
        }

    answer_row = conn.execute(
        "SELECT * FROM ai_messages WHERE conversation_id = ? AND id > ? AND role = 'assistant'"
        " ORDER BY id ASC LIMIT 1",
        (cid, row["id"]),
    ).fetchone()
    return {
        "conversation_id": cid,
        "question": row["content"] or "",
        "answer": (answer_row["content"] if answer_row else "") or "",
        "sources": parse_sources(answer_row["sources"]) if answer_row else [],
        "engine": (answer_row["engine"] if answer_row else "") or "",
    }
