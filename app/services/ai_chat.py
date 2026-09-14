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
import re
import sqlite3
from typing import Any

from ..utils import now_iso

logger = logging.getLogger("inknote.ai_chat")

# 多跳检索用的「是否需要再查」判断系统提示：要求模型只回一行 JSON，
# 不用各家 API 的原生 function calling（兼容性原因，项目刻意如此）。
SYSTEM_REFLECT = (
    "你是一个只依据给定资料判断「还需要不需要再查资料」的中文助手。"
    "下面已经贴了当前检索到的资料和你的问题。"
    "规则：如果资料已经足够回答，只回 {\"enough\": true}；"
    "如果资料还不够，只回 {\"enough\": false, \"search\": \"更精确、且和上一轮不同的检索词\"}。"
    "只输出这一行 JSON，不要任何解释、不要 Markdown 围栏、不要多余文字。"
)

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


# ---------------------------------------------------------------------------
# 多跳检索（反思循环）：检索 → 让模型判断是否够 → 不够就再查
# ---------------------------------------------------------------------------
def _src_id(item) -> int | None:
    """取一条来源的笔记 id，用于按笔记去重合并；拿不到就返回 None。"""
    if not isinstance(item, dict):
        return None
    for key in ("note_id", "id"):
        value = item.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _norm_term(term: str) -> str:
    return (term or "").strip().lower()


def _parse_enough(raw: str) -> dict:
    """从「是否需要再查」判断调用的回复里稳健解析出 {"enough": bool, "search": str}。

    任何解析失败（不是 JSON / 没字段 / 空文本 / 没给新词）一律当
    {"enough": True}——绝不允许因为判断失败把正常问答搞坏。
    """
    text = (raw or "").strip()
    if not text:
        return {"enough": True}
    # 去掉可能的 ```json 围栏
    text = re.sub(r"^```(?:json)?[ \t]*\n?|\n?[ \t]*```$", "", text, flags=re.I).strip()
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {"enough": True}
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return {"enough": True}
    if not isinstance(data, dict):
        return {"enough": True}
    if data.get("enough") is True:
        return {"enough": True}
    if data.get("enough") is False:
        search = (data.get("search") or "").strip()
        if not search:
            # 模型说不够却没给新词：当够了，避免空检索词空转
            return {"enough": True}
        return {"enough": False, "search": search}
    return {"enough": True}


def reflect_and_expand(
    conn,
    question,
    contexts,
    sources,
    engine,
    *,
    extra_hops: int = 2,
    retrieve=None,
    conn_usage=None,
):
    """多跳检索：在首轮资料上做「是否需要再查」的反思，最多追加 extra_hops 次检索。

    返回 (contexts, sources, engine)。extra_hops<=0 或没给 retrieve 时原样返回（保持旧行为）。
    - 每跳检索词必须和已用过的（首轮问题 + 之前各跳）不同，无效/重复词直接视为够了
    - 新资料按笔记 id 与首轮结果去重合并（来源 = 所有轮次并集）
    - 判断调用失败 / 解析失败一律当 enough=True，绝不挡最终回答
    - 判断调用走 ai.chat（非流式、temperature=0、max_tokens 小），用量照旧由它记录
    """
    hops = max(0, int(extra_hops))
    if hops <= 0 or not callable(retrieve):
        return contexts, sources, engine

    from . import ai

    current_ctx = list(contexts or [])
    current_src = list(sources or [])
    current_engine = engine or ""
    used_terms = {_norm_term(question)}

    for _hop in range(hops):
        # 让模型基于「已检索资料 + 问题」判断是否还缺线索
        messages = ai.build_qa_messages(question, current_ctx, history=None)
        messages = [{"role": "system", "content": SYSTEM_REFLECT}, *messages[1:]]
        try:
            verdict = ai.chat(
                messages,
                temperature=0,
                max_tokens=120,
                task="answer",
                conn=conn_usage,
            )
        except Exception:
            # 判断调用失败：按「够了」处理，不挡正常回答
            logger.warning("问笔记多跳判断调用异常，按 enough=true 继续", exc_info=True)
            break

        decision = _parse_enough(verdict)
        if decision.get("enough"):
            break

        term = decision.get("search") or ""
        if _norm_term(term) in used_terms:
            # 无效 / 重复检索词：直接视为够了，避免无意义多查
            break
        used_terms.add(_norm_term(term))

        new_hits, new_engine = retrieve(conn, term, limit=6)
        if not new_hits:
            # 这一跳啥都没查到：直接结束，不再烧 token 反复判断
            break

        # 按笔记 id 去重合并进上下文与来源
        existing_ids = {_src_id(item) for item in current_src}
        merged_any = False
        for hit in new_hits:
            hid = _src_id(hit)
            if hid and hid not in existing_ids:
                existing_ids.add(hid)
                current_src.append(hit)
                current_ctx.append(hit)
                merged_any = True
        if new_engine:
            current_engine = new_engine
        if not merged_any:
            # 全是重复资料：没有新增，不必再查
            break

    return current_ctx, current_src, current_engine
