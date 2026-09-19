# -*- coding: utf-8 -*-
"""agent 包架构守卫：声明式注册表是安全属性的单一事实来源（2026-09-18 拆包后钉住）。"""

from __future__ import annotations

import sqlite3

import pytest

from app.config import BASE_DIR
from app.services import agent


@pytest.fixture()
def db_conn(client, monkeypatch):
    monkeypatch.setattr(agent.ai, "is_enabled", lambda: True)
    from app import db

    with db.db() as conn:
        yield conn


def test_declarations_cover_all_tools(db_conn):
    """声明表与工具闭包一一对应：33 个公开工具，无多无漏。"""
    tools = agent._make_tools(db_conn)
    assert len(agent._TOOL_SPECS) == 33
    assert set(tools) == set(agent._TOOL_SPECS)
    for spec in tools.values():
        assert spec.description and isinstance(spec.params, dict) and callable(spec.run)


def test_every_confirmable_has_card_text():
    """凡是有确认策略的工具，必须同时声明确认卡片文案——防止出现"会拦截但卡片没话"的半成品。"""
    for name, decl in agent._TOOL_SPECS.items():
        if decl.get("confirm") is not None:
            assert decl.get("confirm_card") is not None, f"{name} 有确认策略但没有卡片文案"


def test_write_and_confirm_derive_from_declarations():
    """写工具/可确认集合必须从声明派生（单一事实来源），而不是另立一份手写清单。"""
    expect_writes = {n for n, d in agent._TOOL_SPECS.items() if d.get("writes")}
    expect_confirm = {n for n, d in agent._TOOL_SPECS.items() if d.get("confirm") is not None}
    assert agent._WRITE_TOOLS == expect_writes
    assert agent._ALL_CONFIRMABLE == expect_confirm
    # 抽查关键成员，防止声明表被误清空后派生集合"碰巧一致"
    assert {"update_note", "trash_note", "merge_notes", "replace_in_note",
            "bulk_replace_text"} <= agent._WRITE_TOOLS
    assert "search_notes" not in agent._WRITE_TOOLS
    assert {"trash_note", "publish_note", "merge_notes", "bulk_replace_text",
            "update_note", "replace_in_note", "rewrite_section"} <= agent._ALL_CONFIRMABLE


def test_confirm_strategies_beaviour(db_conn):
    """条件确认的行为抽查：大改写拦、小改不拦；取消公开不拦；发布拦。"""
    tools = agent._make_tools(db_conn)
    long_old = "原有正文。" * 60
    note = agent.repo.create_note(db_conn, title="确认策略", content=long_old)

    from app.services.agent.safety import needs_confirm

    # update_note：整篇换成无关内容 → 确认
    assert needs_confirm(tools["update_note"],
                         {"note_id": note["id"], "content": "完全无关"}, db_conn) is True
    # 在原文基础上追加 → 不确认
    assert needs_confirm(tools["update_note"],
                         {"note_id": note["id"], "content": long_old + "\n\n补充。"}, db_conn) is False
    # publish_note：发布拦、取消公开不拦
    assert needs_confirm(tools["publish_note"],
                         {"note_id": note["id"], "public": True}, db_conn) is True
    assert needs_confirm(tools["publish_note"],
                         {"note_id": note["id"], "public": False}, db_conn) is False
    # trash_note：机制层总确认
    assert needs_confirm(tools["trash_note"], {"note_id": note["id"]}, db_conn) is True
    # search_notes 不是写工具
    assert tools["search_notes"].writes is False


def test_public_api_surface():
    """路由层（ai_admin）与既有测试依赖的公共 API 不因拆包而丢失。"""
    required = [
        "run_agent", "iter_agent_events", "execute_pending", "cancel_pending",
        "list_runs", "clear_runs", "delete_run", "ensure_run_ids",
        "request_cancel", "new_run_id", "SYSTEM_PROMPT",
        "MAX_STEPS", "MAX_ACTIONS_PER_TURN", "MAX_REPEAT_STEPS", "MAX_FORMAT_RETRIES",
        "OBSERVE_LIMIT", "PLAN_MAX_CHARS", "MAX_HISTORY_TURNS", "MAX_HISTORY_CHARS", "MAX_RUNS",
        "_make_tools", "_describe_tools", "_extract_json", "_clean_history", "_today_label",
        "_record_run", "_notes_list", "_last_run_recap", "_locate_section", "_heading_lines",
        "_as_int", "_as_tags", "_note_brief", "_opt_int", "_planned_actions",
        "_chat_with_retry", "_partial_answer", "_register_run", "_release_run",
        "_save_pending_op", "_get_pending_op", "_take_pending_op", "_clear_pending_op",
        "_WRITE_TOOLS", "_ALL_CONFIRMABLE", "ToolSpec", "_TOOL_SPECS", "repo", "ai",
    ]
    missing = [name for name in required if not hasattr(agent, name)]
    assert not missing, f"拆包后丢失公共 API：{missing}"


def test_history_step_budget_matches_loop_budget():
    """执行历史每条最多记的步数与循环预算一致（两处常量必须同步）。"""
    from app.services.agent import core, history

    assert history.MAX_RECORDED_STEPS == core.MAX_STEPS


def test_package_layout():
    """agent 已是包而非单文件（架构守卫：防止有人把 1700 行又拼回一个文件）。"""
    pkg = BASE_DIR / "app" / "services" / "agent"
    assert pkg.is_dir()
    for name in ("__init__.py", "core.py", "tools.py", "safety.py", "history.py", "prompt.py"):
        assert (pkg / name).is_file(), f"缺少 {name}"
    assert not (BASE_DIR / "app" / "services" / "agent.py").exists()
