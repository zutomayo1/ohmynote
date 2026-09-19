# -*- coding: utf-8 -*-
from __future__ import annotations
"""agent 的执行历史：落库审计（meta 表 agent.runs）与跨轮记忆。"""
import json
import logging
import sqlite3
from typing import Any

from ... import repo
from ...utils import now_iso
from uuid import uuid4

logger = logging.getLogger("inknote.agent")

MAX_RECORDED_STEPS = 20        # 落库时每条执行最多记多少步（= core.MAX_STEPS，架构守卫钉住）

MAX_RUNS = 20                # 执行历史最多保留多少条（审计用）

MAX_HISTORY_TURNS = 5
MAX_HISTORY_CHARS = 600

def _notes_list(involved: dict[int, str]) -> list[dict[str, Any]]:
    return [{"id": note_id, "title": title} for note_id, title in involved.items()]

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
        runs = list_runs(conn)
        runs.insert(0, {
            "id": uuid4().hex[:10],
            "at": now_iso(),
            "task": (task or "").strip()[:500],
            "ok": bool(ok),
            "error": (error or "")[:300],
            "answer": (answer or "")[:300],
            "read_only": bool(read_only),
            "dry_run": bool(dry_run),
            "cancelled": bool(cancelled),
            "duration_ms": int(duration_ms or 0),
            "steps": [{"tool": s.get("tool"), "summary": s.get("summary"),
                       "duration_ms": s.get("duration_ms")} for s in steps][:MAX_RECORDED_STEPS],
            "notes": [{"id": n.get("id"), "title": n.get("title")}
                      for n in _notes_list(involved or {})],
        })
        runs = runs[:MAX_RUNS]
        repo.save_meta_map(conn, {"runs": json.dumps(runs, ensure_ascii=False)}, prefix="agent.")
    except Exception:
        logger.warning("agent 执行历史落库失败", exc_info=True)

def list_runs(conn: sqlite3.Connection, limit: int = MAX_RUNS) -> list[dict[str, Any]]:
    """最近的执行历史（新→旧）。坏数据静默跳过。"""
    try:
        meta = repo.get_meta_map(conn, "agent.")
        runs = json.loads(str(meta.get("runs") or "[]"))
    except Exception:
        return []
    if not isinstance(runs, list):
        return []
    return [r for r in runs if isinstance(r, dict)][: max(0, min(limit, MAX_RUNS))]

def clear_runs(conn: sqlite3.Connection) -> None:
    try:
        repo.save_meta_map(conn, {"runs": "[]"}, prefix="agent.")
    except Exception:
        logger.warning("agent 执行历史清空失败", exc_info=True)

def delete_run(conn: sqlite3.Connection, run_id: str) -> bool:
    """删掉执行历史里的某一条。返回是否真的删了。"""
    try:
        runs = list_runs(conn)
        kept = [run for run in runs if run.get("id") != str(run_id)]
        if len(kept) == len(runs):
            return False
        repo.save_meta_map(conn, {"runs": json.dumps(kept, ensure_ascii=False)}, prefix="agent.")
        return True
    except Exception:
        logger.warning("agent 执行历史单条删除失败", exc_info=True)
        return False

def ensure_run_ids(conn: sqlite3.Connection) -> None:
    """给没有 id 的历史记录补上 id（早期记录没有这个字段，单条删除需要它）。

    读时归一化：只在确实缺 id 时才写回，幂等。
    """
    try:
        runs = list_runs(conn)
        if not any(not run.get("id") for run in runs):
            return
        for run in runs:
            if not run.get("id"):
                run["id"] = uuid4().hex[:10]
        repo.save_meta_map(conn, {"runs": json.dumps(runs, ensure_ascii=False)}, prefix="agent.")
    except Exception:
        logger.warning("agent 执行历史补 id 失败", exc_info=True)
