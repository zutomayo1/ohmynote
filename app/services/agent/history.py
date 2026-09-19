# -*- coding: utf-8 -*-
from __future__ import annotations
"""agent 的执行历史：落库审计（独立 agent_runs 表）与跨轮记忆。

存储沿革：2026-09-18 前存在 meta 表的 agent.runs（JSON 列表，上限 20、不可查询、
每次落库都要整表重写）；2026-09-19 迁到独立 agent_runs 表（见 db.py SCHEMA）——
单条 INSERT、可按行删、旧数据在首次读取时自动搬运（_migrate_legacy_meta）。
对外函数签名与返回形状保持不变。
"""
import json
import logging
import sqlite3
from typing import Any

from ... import repo
from ...utils import now_iso
from uuid import uuid4

logger = logging.getLogger("inknote.agent")

MAX_RECORDED_STEPS = 20        # 落库时每条执行最多记多少步（= core.MAX_STEPS，架构守卫钉住）

MAX_RUNS = 20                # 执行历史最多保留多少条（审计用；超出从表里裁掉）

MAX_HISTORY_TURNS = 5
MAX_HISTORY_CHARS = 600

def _notes_list(involved: dict[int, str]) -> list[dict[str, Any]]:
    return [{"id": note_id, "title": title} for note_id, title in involved.items()]

def _migrate_legacy_meta(conn: sqlite3.Connection) -> None:
    """把 meta 表 agent.runs 里的旧执行历史搬进 agent_runs 表（幂等，搬完删旧键）。

    旧列表是新→旧排序；倒序插入让「最新」拿到最大 id，与表的 ORDER BY id DESC 对齐。
    旧记录可能缺 id（ensure_run_ids 时代的历史遗留），补一个。
    """
    try:
        raw = str(repo.get_meta_map(conn, "agent.").get("runs") or "[]")
        runs = json.loads(raw)
        if not isinstance(runs, list) or not runs:
            return
        for run in reversed(runs):
            if not isinstance(run, dict):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO agent_runs (run_id, created_at, task, ok, error, answer,"
                " read_only, dry_run, cancelled, duration_ms, steps_json, notes_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(run.get("id") or uuid4().hex[:10]), str(run.get("at") or now_iso()),
                 str(run.get("task") or "")[:500], int(bool(run.get("ok"))),
                 str(run.get("error") or "")[:300], str(run.get("answer") or "")[:300],
                 int(bool(run.get("read_only"))), int(bool(run.get("dry_run"))),
                 int(bool(run.get("cancelled"))), int(run.get("duration_ms") or 0),
                 json.dumps(run.get("steps") or [], ensure_ascii=False),
                 json.dumps(run.get("notes") or [], ensure_ascii=False)))
        repo.delete_meta(conn, ["agent.runs"])
    except Exception:
        logger.warning("agent 旧执行历史（meta）迁移失败", exc_info=True)

def _last_run_recap(conn: sqlite3.Connection) -> str:
    """把最近一次任务压成一小段存档，给「继续 / 刚才那篇」这类跨轮指代兜底。"""
    try:
        runs = list_runs(conn, limit=1)
    except Exception:
        return ""
    if not runs:
        return ""
    last = runs[0]
    notes = [f"#{n.get('id')}《{n.get('title')}》"
             for n in (last.get("notes") or []) if isinstance(n, dict) and n.get("id")]
    parts = [f"任务：{str(last.get('task') or '').strip()[:120]}"]
    if notes:
        parts.append("涉及的笔记：" + "、".join(notes[:6]))
    answer = str(last.get("answer") or "").strip()
    if answer:
        parts.append("上次的结果：" + answer[:200])
    if last.get("error"):
        parts.append("上次中止原因：" + str(last.get("error"))[:80])
    if len(parts) == 1 and not notes:
        return ""
    return ("（上一轮任务的存档：只有用户说「继续 / 刚才那篇 / 再加点」这类指代时才参考，"
            "不要当成新任务重复执行）\n" + "\n".join(parts))

def _record_run(
    conn: sqlite3.Connection,
    task: str,
    *,
    ok: bool,
    answer: str,
    steps: list[dict[str, Any]],
    error: str,
    read_only: bool,
    dry_run: bool = False,
    involved: dict[int, str] | None = None,
    duration_ms: int | None = None,
    cancelled: bool = False,
) -> None:
    """把一次任务落进执行历史（审计用）。绝不抛异常——审计挂了不能连累任务。"""
    try:
        _migrate_legacy_meta(conn)
        frozen_steps = [{"tool": s.get("tool"), "summary": s.get("summary"),
                         "duration_ms": s.get("duration_ms")}
                        for s in steps if isinstance(s, dict)][:MAX_RECORDED_STEPS]
        conn.execute(
            "INSERT INTO agent_runs (run_id, created_at, task, ok, error, answer,"
            " read_only, dry_run, cancelled, duration_ms, steps_json, notes_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid4().hex[:10], now_iso(), (task or "").strip()[:500], int(bool(ok)),
             (error or "")[:300], (answer or "")[:300], int(bool(read_only)),
             int(bool(dry_run)), int(bool(cancelled)), int(duration_ms or 0),
             json.dumps(frozen_steps, ensure_ascii=False),
             json.dumps(_notes_list(involved or {}), ensure_ascii=False)))
        # 只留最近 MAX_RUNS 条：审计够用，表不无限长大
        conn.execute(
            "DELETE FROM agent_runs WHERE id NOT IN"
            " (SELECT id FROM agent_runs ORDER BY id DESC LIMIT ?)", (MAX_RUNS,))
    except Exception:
        logger.warning("agent 执行历史落库失败", exc_info=True)

def list_runs(conn: sqlite3.Connection, limit: int = MAX_RUNS) -> list[dict[str, Any]]:
    """最近的执行历史（新→旧）。坏数据静默跳过。返回形状与 meta 时代完全一致。"""
    try:
        _migrate_legacy_meta(conn)
        rows = conn.execute(
            "SELECT run_id, created_at, task, ok, error, answer, read_only, dry_run,"
            " cancelled, duration_ms, steps_json, notes_json"
            " FROM agent_runs ORDER BY id DESC LIMIT ?",
            (max(0, min(int(limit), MAX_RUNS)),)).fetchall()
    except Exception:
        return []
    runs: list[dict[str, Any]] = []
    for row in rows:
        try:
            runs.append({
                "id": row["run_id"],
                "at": row["created_at"],
                "task": row["task"],
                "ok": bool(row["ok"]),
                "error": row["error"],
                "answer": row["answer"],
                "read_only": bool(row["read_only"]),
                "dry_run": bool(row["dry_run"]),
                "cancelled": bool(row["cancelled"]),
                "duration_ms": int(row["duration_ms"] or 0),
                "steps": json.loads(row["steps_json"] or "[]"),
                "notes": json.loads(row["notes_json"] or "[]"),
            })
        except Exception:
            continue
    return runs

def clear_runs(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("DELETE FROM agent_runs")
        # 兜底：若还有没被迁移通道消费的 meta 旧键，一并清掉，别让它以后复活成「幽灵历史」
        repo.delete_meta(conn, ["agent.runs"])
    except Exception:
        logger.warning("agent 执行历史清空失败", exc_info=True)

def delete_run(conn: sqlite3.Connection, run_id: str) -> bool:
    """删掉执行历史里的某一条。返回是否真的删了。"""
    try:
        cursor = conn.execute("DELETE FROM agent_runs WHERE run_id = ?", (str(run_id),))
        return cursor.rowcount > 0
    except Exception:
        logger.warning("agent 执行历史单条删除失败", exc_info=True)
        return False

def ensure_run_ids(conn: sqlite3.Connection) -> None:
    """保证历史记录都有 id（路由在列历史前调用）。

    表存储时代 run_id 是 NOT NULL，天然都有；这个函数的职责收敛为
    「把 meta 时代的旧记录搬进来」（幂等，读时归一化的延续）。
    """
    _migrate_legacy_meta(conn)
