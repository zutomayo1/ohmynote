"""夜间整理 agent 的单元 / 服务层测试。

场景：未分类笔记补分类、「收件箱」标签换「已归档」、幂等跳过、
AI 异常不影响其它笔记、INKNOTE_TIDY 关闭 / AI 未启用整体跳过。
"""

from __future__ import annotations

import pytest

from app import config
from app import repo
from app.services import ai as ai_service
from app.services import tidy as tidy_service


@pytest.fixture()
def db_conn(client, monkeypatch):
    """可用的数据库连接：依赖 client 确保 schema 已初始化；AI 视为已配置。

    每个用例开始前清掉 tidy.last_run，避免共享数据库里残留的时间戳让
    「是否该跑」的判断跨用例串味（测试之间互相影响）。
    """
    monkeypatch.setattr(tidy_service.ai, "is_enabled", lambda: True)
    from app import db

    with db.db() as conn:
        repo.delete_meta(conn, ["tidy.last_run"])
        yield conn


def _fake_category(value):
    """脚本化 suggest_category：传字符串则每次都返回它，传 Exception 则每次都抛。"""
    def fake(title, content, *, existing=None, conn=None):
        if isinstance(value, Exception):
            raise value
        return value

    return fake


# ---------------------------------------------------------------------------
# 功能
# ---------------------------------------------------------------------------
def test_uncategorized_note_gets_category(db_conn, monkeypatch):
    """分类为空的笔记被写入 AI 推荐的分类。"""
    monkeypatch.setattr(tidy_service.ai, "suggest_category", _fake_category("技术"))
    note = repo.create_note(db_conn, title="Docker 笔记", content="部署相关内容。")

    result = tidy_service.maybe_tidy(db_conn, interval_hours=24)
    assert result is not None
    assert result["categorized"] >= 1
    assert repo.get_note(db_conn, note["id"])["category"] == "技术"


def test_inbox_tag_is_replaced_by_archived(db_conn, monkeypatch):
    """打了「收件箱」标签的笔记被换成「已归档」，正文保持不变。"""
    monkeypatch.setattr(tidy_service.ai, "suggest_category", _fake_category("工作"))
    note = repo.create_note(
        db_conn, title="待整理", content="正文原封不动。", tags=["收件箱"], category="工作"
    )

    result = tidy_service.maybe_tidy(db_conn, interval_hours=24)
    assert result is not None
    assert result["archived"] >= 1
    fresh = repo.get_note(db_conn, note["id"])
    assert "已归档" in fresh["tags"]
    assert "收件箱" not in fresh["tags"]
    assert fresh["content"] == "正文原封不动。"


def test_idempotent_within_interval(db_conn, monkeypatch):
    """刚跑过立刻再调返回 None（幂等，第二次不传 now 也不该跑）。"""
    monkeypatch.setattr(tidy_service.ai, "suggest_category", _fake_category("技术"))
    repo.create_note(db_conn, title="未分类一", content="内容。")

    first = tidy_service.maybe_tidy(db_conn, interval_hours=24)
    assert first is not None  # 第一次应该真的跑

    second = tidy_service.maybe_tidy(db_conn, interval_hours=24)
    assert second is None  # 间隔内不重复跑


def test_ai_error_does_not_block_others(db_conn, monkeypatch):
    """suggest_category 抛 AIError 时该笔记不写分类，但归档等其它操作照常。"""
    monkeypatch.setattr(
        tidy_service.ai, "suggest_category", _fake_category(ai_service.AIError("模型挂了"))
    )
    uncat = repo.create_note(db_conn, title="会失败的", content="内容。")
    inbox = repo.create_note(
        db_conn, title="收件箱里的", content="另一篇。", tags=["收件箱"], category="工作"
    )

    result = tidy_service.maybe_tidy(db_conn, interval_hours=24)
    assert result is not None
    assert result["categorized"] == 0
    assert result["archived"] >= 1
    # 分类失败的笔记没被改动
    assert repo.get_note(db_conn, uncat["id"])["category"] == ""
    # 归档仍照常发生（不受分类异常影响）
    assert "已归档" in repo.get_note(db_conn, inbox["id"])["tags"]


# ---------------------------------------------------------------------------
# 开关 / 可用性
# ---------------------------------------------------------------------------
def test_disabled_by_env(db_conn, monkeypatch):
    """INKNOTE_TIDY=0（tidy_enabled=False）时整体跳过，返回 None。"""
    monkeypatch.setattr(config.settings, "tidy_enabled", False)
    repo.create_note(db_conn, title="未分类二", content="内容。")
    assert tidy_service.maybe_tidy(db_conn, interval_hours=24) is None


def test_skipped_when_ai_disabled(db_conn, monkeypatch):
    """AI 未启用时整体跳过，返回 None。"""
    monkeypatch.setattr(tidy_service.ai, "is_enabled", lambda: False)
    repo.create_note(db_conn, title="未分类三", content="内容。")
    assert tidy_service.maybe_tidy(db_conn, interval_hours=24) is None


# ---------------------------------------------------------------------------
# 结果持久化 + 设置页可读
# ---------------------------------------------------------------------------
def test_describe_reflects_last_run_and_result(db_conn, monkeypatch):
    """maybe_tidy 跑完之后，describe 能读到 last_run 与上次的分类/归档结果。"""
    monkeypatch.setattr(tidy_service.ai, "suggest_category", _fake_category("技术"))
    repo.create_note(db_conn, title="未分类笔记", content="内容。", tags=["收件箱"])

    result = tidy_service.maybe_tidy(db_conn, interval_hours=24)
    assert result is not None

    info = tidy_service.describe(db_conn)
    assert info["enabled"] is True
    assert info["ai_ready"] is True
    assert info["last_run"]  # 时间戳非空
    assert info["last_result"] is not None
    assert info["last_result"]["categorized"] >= 1
    assert info["last_result"]["archived"] >= 1
    assert info["last_result"]["at"] == info["last_run"]


def test_settings_page_no_longer_renders_tidy_block(auth_client):
    """设置页不再显示「夜间整理」块（2026-09-18 整理）。

    那一块是纯只读状态：开关在 .env 的 INKNOTE_TIDY 里，页面上改不了任何东西，
    却占一个折叠块。自动化本身没动——状态与执行结果仍由本文件其余测试覆盖。
    """
    response = auth_client.get("/settings")
    assert response.status_code == 200
    assert "夜间整理" not in response.text


def test_describe_tolerates_corrupt_last_result(db_conn):
    """tidy.last_result 是坏 JSON 时返回 last_result=None，且不抛异常。"""
    repo.save_meta_map(
        db_conn,
        {"last_run": "2026-09-13T12:00:00+00:00", "last_result": "{坏json"},
        prefix="tidy.",
    )
    info = tidy_service.describe(db_conn)
    assert info["last_result"] is None
    assert info["last_run"] == "2026-09-13T12:00:00+00:00"
