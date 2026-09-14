"""AI 用量统计：每次调用记一笔，设置页显示「本月用了多少次 / 多少 token」。

单独一个模块，自己建表（惰性 CREATE TABLE IF NOT EXISTS），
这样它和主 schema 解耦，也方便向量检索等其它 AI 子模块各自管自己的表。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from datetime import timedelta

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
    created_at   TEXT NOT NULL,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL DEFAULT 1,
    error             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_time ON {TABLE} (created_at);
CREATE INDEX IF NOT EXISTS idx_ai_usage_task ON {TABLE} (task);
"""

# 老库补列（CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段）
COLUMNS = {
    "latency_ms": "INTEGER NOT NULL DEFAULT 0",
    "ok": "INTEGER NOT NULL DEFAULT 1",
    "error": "TEXT NOT NULL DEFAULT ''",
}


def ensure(conn: sqlite3.Connection) -> None:
    """建表（幂等）+ 给老库补列。每个对外函数都会先调一次，所以不用依赖启动顺序。"""
    conn.executescript(SCHEMA)
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
    for column, definition in COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} {definition}")


def record(
    conn: sqlite3.Connection,
    *,
    model: str = "",
    task: str = "",
    usage: dict[str, Any] | None = None,
    latency_ms: int = 0,
    ok: bool = True,
    error: str = "",
) -> None:
    """记一次调用。

    usage 用服务商返回的 {prompt_tokens, completion_tokens, total_tokens}。
    latency_ms / ok / error 是本地量出来的：失败也要记 —— 只记成功的调用，
    「失败率」「到底慢在哪」这些就永远看不到。
    """
    usage = usage or {}
    prompt = _to_int(usage.get("prompt_tokens"))
    completion = _to_int(usage.get("completion_tokens"))
    total = _to_int(usage.get("total_tokens")) or (prompt + completion)
    ensure(conn)
    conn.execute(
        f"INSERT INTO {TABLE} (model, task, prompt_tokens, completion_tokens, total_tokens,"
        f" created_at, latency_ms, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (model or "", task or "", prompt, completion, total, now_iso(),
         max(0, _to_int(latency_ms)), 1 if ok else 0, (error or "")[:300]),
    )


def _to_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        # 服务商返回的 token 字段偶尔是 "abc" / None：按 0 记账，但留一条线索
        logger.debug("用量字段不是整数（value=%r），按 0 处理", value)
        return 0


def _stat(row) -> dict[str, int]:
    return {
        "calls": int(row["calls"] or 0),
        "tokens": int(row["tokens"] or 0),
        "prompt": int(row["prompt"] or 0),
        "completion": int(row["completion"] or 0),
    }


_SELECT_STAT = (
    "SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens,"
    " COALESCE(SUM(prompt_tokens), 0) AS prompt,"
    " COALESCE(SUM(completion_tokens), 0) AS completion FROM {table}"
)


def _window(conn: sqlite3.Connection, start: str = "", end: str = "") -> dict[str, int]:
    """一个时间窗口的合计。start 为空表示全时段。"""
    where, params = "", []
    if start:
        where = " WHERE created_at >= ?"
        params.append(start)
        if end:
            where += " AND created_at < ?"
            params.append(end)
    row = conn.execute(_SELECT_STAT.format(table=TABLE) + where, params).fetchone()
    return _stat(row)


def _delta_pct(current: int, previous: int) -> int | None:
    """环比百分比。上期为 0 时返回 None（没有可比性，别硬算成 +100%）。"""
    if not previous:
        return None if not current else 100
    return int(round((current - previous) * 100 / previous))


def summary(conn: sqlite3.Connection, *, days: int = 30) -> dict[str, Any]:
    """用量总览：分窗口合计 + 环比 + 五个维度的拆分 + 质量指标 + 最近明细。

    维度：按任务 / 按模型 / 模型×任务 / 按天 / 按小时 / 按星期 / 按月。
    质量：平均与单次最大 token、平均与最大耗时、失败次数与失败率、最近几次失败。
    """
    ensure(conn)
    today = now()
    month_start = today.strftime("%Y-%m-01 00:00:00")
    day_start = today.strftime("%Y-%m-%d 00:00:00")
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d 00:00:00")
    prev_first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    prev_month_start = prev_first.strftime("%Y-%m-01 00:00:00")
    window_start = _days_ago(days)

    today_stat = _window(conn, day_start)
    week_stat = _window(conn, week_start)
    month_stat = _window(conn, month_start)
    prev_stat = _window(conn, prev_month_start, month_start)
    all_stat = _window(conn)

    def grouped(column: str, *, since: str, order: str = "tokens") -> list[dict[str, Any]]:
        """按某个时间/维度表达式分组。附带的 failed / avg_latency 是给图表悬浮提示用的。"""
        rows = conn.execute(
            f"SELECT {column} AS key, COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens,"
            f" COALESCE(SUM(prompt_tokens), 0) AS prompt,"
            f" COALESCE(SUM(completion_tokens), 0) AS completion,"
            f" COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failed,"
            f" COALESCE(AVG(NULLIF(latency_ms, 0)), 0) AS avg_latency"
            f" FROM {TABLE} WHERE created_at >= ? GROUP BY key"
            f" ORDER BY {order} DESC, key",
            (since,),
        ).fetchall()
        return [
            {
                "key": str(row["key"] or ""),
                **_stat(row),
                "failed": int(row["failed"] or 0),
                "avg_latency": int(round(float(row["avg_latency"] or 0))),
            }
            for row in rows
        ]

    by_task = [
        {"task": item["key"] or "其它", "calls": item["calls"], "tokens": item["tokens"]}
        for item in grouped("task", since=month_start, order="calls")
    ]

    by_model = [
        {"model": item["key"] or "（未记录模型）", "calls": item["calls"], "tokens": item["tokens"],
         "prompt": item["prompt"], "completion": item["completion"],
         "failed": item["failed"], "avg_latency": item["avg_latency"]}
        for item in grouped("model", since=month_start)
    ]

    # 模型 × 任务：到底哪个模型在干哪类活（归因用）
    by_model_task = [
        {"model": row["model"] or "（未记录模型）", "task": row["task"] or "其它",
         "calls": int(row["calls"]), "tokens": int(row["tokens"])}
        for row in conn.execute(
            f"SELECT model, task, COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens"
            f" FROM {TABLE} WHERE created_at >= ? GROUP BY model, task"
            f" ORDER BY tokens DESC LIMIT 20",
            (month_start,),
        )
    ]

    by_day = [
        {"day": item["key"], "calls": item["calls"], "tokens": item["tokens"],
         "prompt": item["prompt"], "completion": item["completion"],
         "failed": item["failed"], "avg_latency": item["avg_latency"]}
        for item in grouped("substr(created_at, 1, 10)", since=window_start, order="key")
    ]

    # 时间规律：习惯几点用、一周里哪天用得多（看近 N 天，太长的历史会淹没近期习惯）
    hour_rows = {item["key"]: item for item in grouped("substr(created_at, 12, 2)", since=window_start)}
    by_hour = [
        {
            "hour": f"{h:02d}",
            **{k: hour_rows.get(f"{h:02d}", {}).get(k, 0)
               for k in ("calls", "tokens", "prompt", "completion", "failed", "avg_latency")},
        }
        for h in range(24)
    ]

    weekday_rows = {item["key"]: item for item in grouped("strftime('%w', created_at)", since=window_start)}
    # strftime('%w')：0=周日 … 6=周六；按「周一…周日」的阅读顺序排
    by_weekday = [
        {
            "label": label,
            **{k: weekday_rows.get(key, {}).get(k, 0)
               for k in ("calls", "tokens", "prompt", "completion", "failed", "avg_latency")},
        }
        for key, label in (("1", "周一"), ("2", "周二"), ("3", "周三"), ("4", "周四"),
                           ("5", "周五"), ("6", "周六"), ("0", "周日"))
    ]

    # 近 6 个月（含本月），没记录的月补 0
    month_rows = {item["key"]: item for item in grouped("substr(created_at, 1, 7)", since="")}
    by_month: list[dict[str, Any]] = []
    cursor = today.replace(day=1)
    for _ in range(6):
        key = cursor.strftime("%Y-%m")
        by_month.insert(0, {
            "month": key,
            **{k: month_rows.get(key, {}).get(k, 0)
               for k in ("calls", "tokens", "prompt", "completion", "failed", "avg_latency")},
        })
        cursor = (cursor - timedelta(days=1)).replace(day=1)

    quality_row = conn.execute(
        f"SELECT COALESCE(AVG(total_tokens), 0) AS avg_tokens,"
        f" COALESCE(MAX(total_tokens), 0) AS max_tokens,"
        f" COALESCE(AVG(NULLIF(latency_ms, 0)), 0) AS avg_latency,"
        f" COALESCE(MAX(latency_ms), 0) AS max_latency,"
        f" COUNT(*) AS calls, COALESCE(SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END), 0) AS failed"
        f" FROM {TABLE} WHERE created_at >= ?",
        (month_start,),
    ).fetchone()
    calls = int(quality_row["calls"] or 0)
    failed = int(quality_row["failed"] or 0)
    quality = {
        "avg_tokens": int(round(float(quality_row["avg_tokens"] or 0))),
        "max_tokens": int(quality_row["max_tokens"] or 0),
        "avg_latency_ms": int(round(float(quality_row["avg_latency"] or 0))),
        "max_latency_ms": int(quality_row["max_latency"] or 0),
        "failed": failed,
        "fail_rate": (round(failed * 100 / calls, 1) if calls else 0.0),
        "success_rate": (round((calls - failed) * 100 / calls, 1) if calls else 0.0),
    }

    recent = [
        dict(row)
        for row in conn.execute(
            f"SELECT id, model, task, prompt_tokens, completion_tokens, total_tokens,"
            f" created_at, latency_ms, ok, error FROM {TABLE} ORDER BY id DESC LIMIT 20"
        )
    ]
    recent_failures = [
        dict(row)
        for row in conn.execute(
            f"SELECT id, model, task, created_at, error FROM {TABLE}"
            f" WHERE ok = 0 ORDER BY id DESC LIMIT 5"
        )
    ]

    all_time = conn.execute(
        f"SELECT MIN(substr(created_at, 1, 10)) AS since FROM {TABLE}"
    ).fetchone()
    last = conn.execute(
        f"SELECT model, task, created_at, latency_ms, ok FROM {TABLE} ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return {
        # 分窗口合计（保留原来的 month_* / all_* 键名，外部别处还在用）
        "today": today_stat,
        "week": week_stat,
        "month": month_stat,
        "prev_month": prev_stat,
        "all": all_stat,
        "month_calls": month_stat["calls"],
        "month_tokens": month_stat["tokens"],
        "month_prompt": month_stat["prompt"],
        "month_completion": month_stat["completion"],
        "all_calls": all_stat["calls"],
        "all_tokens": all_stat["tokens"],
        "since": (all_time["since"] if all_time else "") or "",
        "tokens_delta_pct": _delta_pct(month_stat["tokens"], prev_stat["tokens"]),
        "calls_delta_pct": _delta_pct(month_stat["calls"], prev_stat["calls"]),
        # 拆分维度
        "by_task": by_task,
        "by_model": by_model,
        "by_model_task": by_model_task,
        "by_day": by_day,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "by_month": by_month,
        # 质量与明细
        "quality": quality,
        "recent": recent,
        "recent_failures": recent_failures,
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
