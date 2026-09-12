"""AI 用量统计：每次调用记一笔，设置页显示「本月用了多少次 / 多少 token」。

单独一个模块，自己建表（惰性 CREATE TABLE IF NOT EXISTS），
这样它和主 schema 解耦，也方便向量检索等其它 AI 子模块各自管自己的表。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from ..utils import now, now_iso

logger = logging.getLogger("inknote.ai_usage")

TABLE = "ai_usage"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    model        TEXT NOT NULL DEFAULT '',
    task         TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_time ON {TABLE} (created_at);
"""


def ensure(conn: sqlite3.Connection) -> None:
    """建表（幂等）。每个对外函数都会先调一次，所以不用依赖启动顺序。"""
    conn.executescript(SCHEMA)


def record(
    conn: sqlite3.Connection,
    *,
    model: str = "",
    task: str = "",
    usage: dict[str, Any] | None = None,
) -> None:
    """记一次调用。usage 用服务商返回的 {prompt_tokens, completion_tokens, total_tokens}。"""
    usage = usage or {}
    prompt = _to_int(usage.get("prompt_tokens"))
    completion = _to_int(usage.get("completion_tokens"))
    total = _to_int(usage.get("total_tokens")) or (prompt + completion)
    ensure(conn)
    conn.execute(
        f"INSERT INTO {TABLE} (model, task, prompt_tokens, completion_tokens, total_tokens, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (model or "", task or "", prompt, completion, total, now_iso()),
    )


def _to_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        # 服务商返回的 token 字段偶尔是 "abc" / None：按 0 记账，但留一条线索
        logger.debug("用量字段不是整数（value=%r），按 0 处理", value)
        return 0


def summary(conn: sqlite3.Connection, *, days: int = 30) -> dict[str, Any]:
    """本月合计 + 按任务拆分 + 最近 N 天按天拆分（给设置页显示）。"""
    ensure(conn)
    month_start = now().strftime("%Y-%m-01 00:00:00")
    window_start = _days_ago(days)

    total = conn.execute(
        f"SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens,"
        f" COALESCE(SUM(prompt_tokens), 0) AS prompt,"
        f" COALESCE(SUM(completion_tokens), 0) AS completion"
        f" FROM {TABLE} WHERE created_at >= ?",
        (month_start,),
    ).fetchone()

    by_task = [
        {"task": row["task"] or "其它", "calls": int(row["calls"]), "tokens": int(row["tokens"])}
        for row in conn.execute(
            f"SELECT task, COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens"
            f" FROM {TABLE} WHERE created_at >= ? GROUP BY task ORDER BY calls DESC",
            (month_start,),
        )
    ]

    by_day = [
        {"day": row["day"], "calls": int(row["calls"]), "tokens": int(row["tokens"])}
        for row in conn.execute(
            f"SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS calls,"
            f" COALESCE(SUM(total_tokens), 0) AS tokens"
            f" FROM {TABLE} WHERE created_at >= ? GROUP BY day ORDER BY day",
            (window_start,),
        )
    ]

    last = conn.execute(
        f"SELECT model, task, created_at FROM {TABLE} ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return {
        "month_calls": int(total["calls"] or 0),
        "month_tokens": int(total["tokens"] or 0),
        "month_prompt": int(total["prompt"] or 0),
        "month_completion": int(total["completion"] or 0),
        "by_task": by_task,
        "by_day": by_day,
        "last_call": dict(last) if last else None,
    }


def _days_ago(days: int) -> str:
    from datetime import timedelta

    return (now() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")


def reset(conn: sqlite3.Connection) -> int:
    """清空记录（设置页的「重置用量统计」）。"""
    ensure(conn)
    cursor = conn.execute(f"DELETE FROM {TABLE}")
    return int(cursor.rowcount or 0)
