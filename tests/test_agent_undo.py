# -*- coding: utf-8 -*-
"""agent 步骤级撤销：写操作压栈，undo_last 把受影响笔记恢复到该步之前（2026-09-19）。"""

from __future__ import annotations

import json

import pytest

from app.services import agent
from app.services.agent import undo as agent_undo


@pytest.fixture()
def db_conn(client, monkeypatch):
    monkeypatch.setattr(agent.ai, "is_enabled", lambda: True)
    from app import db

    with db.db() as conn:
        yield conn


class _ScriptedChat:
    """按顺序吐出预设回复（与 test_agent_v2 同款）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        return self.replies.pop(0)


def _tool(conn, name, params):
    """与循环层相同的 undo 包裹路径执行单个工具（快照 → 执行 → 压栈）。"""
    spec = agent._make_tools(conn)[name]
    ctx = agent_undo.prepare(conn, name, params) if spec.writes else None
    out = spec.run(params)
    if ctx is not None and not (isinstance(out, dict) and out.get("error")):
        agent_undo.commit(conn, ctx, name, out, f"（测试）{name}")
    return out


# ---------------------------------------------------------------------------
# 各类写操作的撤销语义
# ---------------------------------------------------------------------------

def test_replace_in_note_undo_restores_content(db_conn):
    note = agent.repo.create_note(db_conn, title="撤销试验", content="第一段。\n\n第二段旧词。")
    _tool(db_conn, "replace_in_note", {"note_id": note["id"], "find": "旧词",
                                            "replace_with": "新词"})
    result = agent.undo_last(db_conn)
    assert result["ok"] is True and result["tool"] == "replace_in_note"
    assert note["id"] in result["changed"]
    assert agent.repo.get_note(db_conn, note["id"])["content"] == "第一段。\n\n第二段旧词。"
    # 恢复本身存版本历史（reason=agent-undo），可再翻回去
    versions = agent.repo.list_versions(db_conn, note["id"])
    assert any(v["reason"] == "agent-undo" for v in versions)


def test_add_tags_undo_removes_added_keeps_existing(db_conn):
    note = agent.repo.create_note(db_conn, title="标签撤销", content="x", tags=["原有"])
    _tool(db_conn, "add_tags", {"note_id": note["id"], "tags": ["新加"]})
    assert agent.undo_last(db_conn)["ok"] is True
    tags = agent.repo.get_note(db_conn, note["id"])["tags"]
    assert "新加" not in tags and "原有" in tags


def test_bulk_set_category_undo(db_conn):
    a = agent.repo.create_note(db_conn, title="A", content="x", category="旧")
    b = agent.repo.create_note(db_conn, title="B", content="x", category="技术")
    _tool(db_conn, "bulk_set_category", {"note_ids": [a["id"], b["id"]],
                                              "category": "技术"})
    result = agent.undo_last(db_conn)
    assert result["ok"] is True
    assert agent.repo.get_note(db_conn, a["id"])["category"] == "旧"
    assert agent.repo.get_note(db_conn, b["id"])["category"] == "技术"
    # 没变化的那篇不产生 agent-undo 版本（逐篇比对，无变化跳过）
    assert not any(v["reason"] == "agent-undo"
                   for v in agent.repo.list_versions(db_conn, b["id"]))


def test_trash_note_undo_restores(db_conn):
    note = agent.repo.create_note(db_conn, title="回收站撤销", content="x")
    _tool(db_conn, "trash_note", {"note_id": note["id"]})
    assert agent.repo.get_note(db_conn, note["id"]) is None            # 已进回收站
    assert agent.undo_last(db_conn)["ok"] is True
    assert agent.repo.get_note(db_conn, note["id"]) is not None        # 回来了


def test_restore_note_undo_retrashes(db_conn):
    note = agent.repo.create_note(db_conn, title="再入回收站", content="x")
    agent.repo.soft_delete(db_conn, note["id"])
    _tool(db_conn, "restore_note", {"note_id": note["id"]})
    assert agent.undo_last(db_conn)["ok"] is True
    assert agent.repo.get_note(db_conn, note["id"]) is None            # 又回回收站了


def test_create_note_undo_trashes_it(db_conn):
    out = _tool(db_conn, "create_note", {"title": "新建的", "content": "内容"})
    result = agent.undo_last(db_conn)
    assert result["ok"] is True and result["tool"] == "create_note"
    assert agent.repo.get_note(db_conn, out["note_id"]) is None            # 移入回收站
    assert agent.repo.get_note(db_conn, out["note_id"],
                               include_deleted=True) is not None           # 可再恢复


def test_publish_note_undo_unpublishes(db_conn):
    note = agent.repo.create_note(db_conn, title="发布撤销", content="x")
    _tool(db_conn, "publish_note", {"note_id": note["id"], "public": True})
    result = agent.undo_last(db_conn)
    assert result["ok"] is True
    assert agent.repo.get_note(db_conn, note["id"])["is_public"] is False


def test_merge_notes_undo_restores_target_and_sources(db_conn):
    target = agent.repo.create_note(db_conn, title="目标", content="目标正文。")
    source = agent.repo.create_note(db_conn, title="源", content="源正文。")
    _tool(db_conn, "merge_notes", {"target_id": target["id"],
                                        "source_ids": [source["id"]]})
    result = agent.undo_last(db_conn)
    assert result["ok"] is True and result["tool"] == "merge_notes"
    assert agent.repo.get_note(db_conn, target["id"])["content"] == "目标正文。"
    assert agent.repo.get_note(db_conn, source["id"]) is not None          # 从回收站恢复


def test_undo_empty_stack(db_conn):
    db_conn.execute("DELETE FROM agent_undo")   # 共享库：别的用例可能留了条目
    result = agent.undo_last(db_conn)
    assert result["ok"] is False and "没有可撤销" in result["error"]


# ---------------------------------------------------------------------------
# 接线：循环层记录、只读不记录、确认执行路径也记录
# ---------------------------------------------------------------------------

def test_loop_records_undo_and_read_only_does_not(db_conn, monkeypatch):
    note = agent.repo.create_note(db_conn, title="循环层", content="原文。")
    chat = _ScriptedChat([
        json.dumps({"action": "add_tags",
                    "params": {"note_id": note["id"], "tags": ["循环"]}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "完成"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent.ai, "chat", chat)
    assert agent.run_agent(db_conn, "加标签")["ok"] is True
    assert agent.undo_last(db_conn)["ok"] is True                  # 循环里的写进栈了

    # 只读模式不产生 undo 记录：清栈后按「前后增量」断言（共享库约定）
    db_conn.execute("DELETE FROM agent_undo")
    before = db_conn.execute("SELECT COUNT(*) FROM agent_undo").fetchone()[0]
    chat2 = _ScriptedChat([
        json.dumps({"action": "replace_in_note",
                    "params": {"note_id": note["id"], "find": "原文", "replace_with": "改"}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "只读做不了"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent.ai, "chat", chat2)
    result2 = agent.run_agent(db_conn, "试试改", read_only=True)
    assert any("已拦截写操作" in s["summary"] for s in result2["steps"])
    after = db_conn.execute("SELECT COUNT(*) FROM agent_undo").fetchone()[0]
    assert after == before, "只读模式不该产生 undo 记录"
    assert agent.undo_last(db_conn)["ok"] is False                 # 栈仍是空的


def test_final_event_carries_undoable_flag(db_conn, monkeypatch):
    """final 事件带 undoable：写过为 True（按钮按需出现），只读为 False。"""
    note = agent.repo.create_note(db_conn, title="标记试验", content="原文。")
    chat = _ScriptedChat([
        json.dumps({"action": "add_tags",
                    "params": {"note_id": note["id"], "tags": ["标记"]}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "完成"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent.ai, "chat", chat)
    result = agent.run_agent(db_conn, "加标签")
    assert result["ok"] is True and result["undoable"] is True
    assert agent.undo_last(db_conn)["ok"] is True

    # 只读任务 + 清空栈：没有可撤销的写操作 → undoable False
    db_conn.execute("DELETE FROM agent_undo")
    chat2 = _ScriptedChat([
        json.dumps({"action": "read_note", "params": {"note_id": note["id"]}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "读完了"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent.ai, "chat", chat2)
    result2 = agent.run_agent(db_conn, "读一下")
    assert result2["ok"] is True and result2["undoable"] is False


def test_confirmed_execute_also_undoable(db_conn):
    """用户在确认卡片上点「确认执行」的写操作，同样可撤销。"""
    note = agent.repo.create_note(db_conn, title="确认后撤销", content="原文" * 150)
    pending = agent._save_pending_op(db_conn, "update_note",
                                     {"note_id": note["id"], "content": "完全不同的新内容"},
                                     {"id": note["id"], "title": note["title"]})
    done = agent.execute_pending(db_conn, pending["id"])
    assert done["ok"] is True
    result = agent.undo_last(db_conn)
    assert result["ok"] is True and result["tool"] == "update_note"
    assert agent.repo.get_note(db_conn, note["id"])["content"] == "原文" * 150


def test_undo_stack_pruned_to_max(db_conn):
    note = agent.repo.create_note(db_conn, title="裁剪", content="x")
    for i in range(agent.MAX_UNDO + 5):
        _tool(db_conn, "set_category", {"note_id": note["id"], "category": f"类{i}"})
    with db_conn as c:
        n = c.execute("SELECT COUNT(*) FROM agent_undo").fetchone()[0]
    assert n == agent.MAX_UNDO


# ---------------------------------------------------------------------------
# HTTP 端点（CSRF；写事务先提交再走 HTTP —— 路由用另一个连接）
# ---------------------------------------------------------------------------

def test_undo_endpoint_http(auth_client, csrf):
    from app import db as db_mod

    with db_mod.db() as conn:
        conn.execute("DELETE FROM agent_undo")                     # 共享库：先清栈
        note = agent.repo.create_note(conn, title="HTTP 撤销", content="第一段。")
        _tool(conn, "replace_in_note",
              {"note_id": note["id"], "find": "第一段", "replace_with": "改掉"})

    res = auth_client.post("/api/agent/undo", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["tool"] == "replace_in_note"
    assert body["remaining"] == 0
    with db_mod.db() as conn:
        assert agent.repo.get_note(conn, note["id"])["content"] == "第一段。"

    res2 = auth_client.post("/api/agent/undo", headers={"X-CSRF-Token": csrf})
    assert res2.status_code == 200 and res2.json()["ok"] is False
