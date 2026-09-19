# -*- coding: utf-8 -*-
"""agent 执行历史独立表（agent_runs）：审计从 meta JSON 迁出后的存储层行为（2026-09-19）。"""

from __future__ import annotations

import json

import pytest

from app.services import agent


@pytest.fixture()
def db_conn(client, monkeypatch):
    monkeypatch.setattr(agent.ai, "is_enabled", lambda: True)
    from app import db

    with db.db() as conn:
        yield conn


def _record(conn, task="任务", **kw):
    agent._record_run(conn, task,
                      ok=kw.pop("ok", True), answer=kw.pop("answer", "完成"),
                      steps=kw.pop("steps", [{"tool": "search_notes", "summary": "命中 1 篇",
                                              "duration_ms": 5}]),
                      error=kw.pop("error", ""), read_only=kw.pop("read_only", False),
                      dry_run=kw.pop("dry_run", False),
                      involved=kw.pop("involved", {3: "标题"}),
                      duration_ms=kw.pop("duration_ms", 120),
                      cancelled=kw.pop("cancelled", False))


def test_agent_runs_table_exists():
    from app import db

    with db.db() as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "agent_runs" in names


def test_record_and_list_roundtrip(db_conn):
    """落库→读取往返：字段与 meta 时代完全一致（路由与旧测试零改动）。共享库按前后增量断言。"""
    before = len(agent.list_runs(db_conn))
    _record(db_conn)
    runs = agent.list_runs(db_conn)
    assert len(runs) == before + 1
    run = runs[0]        # 最新插入的排在最前
    assert run["task"] == "任务" and run["ok"] is True and run["answer"] == "完成"
    assert run["duration_ms"] == 120 and run["read_only"] is False
    assert run["cancelled"] is False and run["dry_run"] is False and run["error"] == ""
    assert run["steps"][0]["tool"] == "search_notes" and run["steps"][0]["duration_ms"] == 5
    assert run["notes"] == [{"id": 3, "title": "标题"}]
    assert run["id"] and run["at"]     # 单条删除与展示依赖这两个字段


def test_new_runs_come_first_and_limit_works(db_conn):
    for i in range(3):
        _record(db_conn, task=f"顺序任务{i}")
    runs = agent.list_runs(db_conn)
    assert [r["task"] for r in runs[:3]] == ["顺序任务2", "顺序任务1", "顺序任务0"]
    assert [r["task"] for r in agent.list_runs(db_conn, limit=2)] == ["顺序任务2", "顺序任务1"]


def test_history_is_pruned_to_max_runs(db_conn):
    """落库自动裁剪：表里与返回值都只保留最近 MAX_RUNS 条，不无限堆积。"""
    for i in range(agent.MAX_RUNS + 5):
        _record(db_conn, task=f"T{i}")
    runs = agent.list_runs(db_conn)
    assert len(runs) == agent.MAX_RUNS
    assert runs[0]["task"] == f"T{agent.MAX_RUNS + 4}"          # 最新的还在
    with db_conn as c:
        n = c.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
    assert n == agent.MAX_RUNS


def test_delete_and_clear(db_conn):
    agent.clear_runs(db_conn)          # 先清（共享库约定：不只清自己的，也顺便清历史遗留）
    _record(db_conn, task="A")
    _record(db_conn, task="B")
    target = agent.list_runs(db_conn)[0]["id"]
    assert agent.delete_run(db_conn, target) is True
    assert agent.delete_run(db_conn, "不存在的id") is False
    assert [r["task"] for r in agent.list_runs(db_conn)] == ["A"]
    agent.clear_runs(db_conn)
    assert agent.list_runs(db_conn) == []


def test_legacy_meta_history_migrates_automatically(db_conn):
    """meta 时代的旧历史在首次读取时自动搬进表，旧键删除，幂等。"""
    legacy = [
        {"id": "abc123", "at": "2026-09-18T10:00:00", "task": "旧任务", "ok": True,
         "error": "", "answer": "旧的回答", "read_only": False, "dry_run": False,
         "cancelled": False, "duration_ms": 33,
         "steps": [{"tool": "read_note", "summary": "已读取", "duration_ms": 9}],
         "notes": [{"id": 7, "title": "旧笔记"}]},
        {"task": "缺 id 的更早记录", "ok": False, "error": "boom", "answer": "",
         "read_only": False, "dry_run": False, "cancelled": False,
         "duration_ms": 0, "steps": [], "notes": []},
    ]
    agent.repo.save_meta_map(db_conn, {"runs": json.dumps(legacy, ensure_ascii=False)},
                             prefix="agent.")
    agent.ensure_run_ids(db_conn)                 # 路由在列历史前都会调它
    runs = agent.list_runs(db_conn)
    # 迁移行拿到最大 id → 排最前，保持新→旧（共享库里可能有别的行，只看前两条）
    assert [r["task"] for r in runs[:2]] == ["旧任务", "缺 id 的更早记录"]
    assert runs[0]["notes"] == [{"id": 7, "title": "旧笔记"}]
    assert runs[0]["id"] == "abc123"
    assert runs[1]["id"]                          # 缺 id 的补上了
    from app.repo.meta import get_meta_map
    assert "runs" not in get_meta_map(db_conn, "agent.")   # 旧键已删（幂等判据）
    agent.ensure_run_ids(db_conn)                 # 再跑一次：无事发生（不重复迁移）
    n = len(agent.list_runs(db_conn))
    agent.ensure_run_ids(db_conn)
    assert len(agent.list_runs(db_conn)) == n
