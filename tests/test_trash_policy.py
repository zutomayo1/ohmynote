"""E · 回收站保留策略：purge_countdown 过滤器边界 + /trash 页面渲染。"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from app import db as db_mod
from app import repo
from app.config import settings
from app.templating import purge_countdown
from app.utils import ISO_FMT, now


def _ago(*, days: int = 0, seconds: int = 0) -> str:
    """生成与 purge_expired_trash 同格式的删除时间字符串。"""
    return (now() - timedelta(days=days, seconds=seconds)).strftime(ISO_FMT)


# ---------------------------------------------------------------------------
# 1. 过滤器单测：边界全部对齐 repo.purge_expired_trash
# ---------------------------------------------------------------------------
def test_countdown_trash_days_30():
    assert purge_countdown(_ago(seconds=3), trash_days=30) == "还有 30 天"
    assert purge_countdown(_ago(days=20), trash_days=30) == "还有 10 天"
    assert purge_countdown(_ago(days=29), trash_days=30) == "明天清理"
    # 删满 30 天：deleted_at 只精确到秒，会落在 purge 的严格 < 截止点之后 → 已过期
    assert purge_countdown(_ago(days=30), trash_days=30) == "已过期，下次启动时清理"
    assert purge_countdown(_ago(days=31), trash_days=30) == "已过期，下次启动时清理"


@pytest.mark.parametrize("days", [0, -1, -30])
def test_countdown_no_auto_purge(days):
    assert purge_countdown(_ago(days=100), trash_days=days) == "不自动清理"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "   ",
        "not-a-date",
        "2024-13-45 99:99:99",
        "2024/01/02",
        "2024-01-01T00:00:00+00:00",  # 带时区：与朴素 now() 不可比，按解析失败兜底
        [],
        {},
    ],
)
def test_countdown_bad_input_returns_empty(bad):
    assert purge_countdown(bad, trash_days=30) == ""


def test_countdown_bad_trash_days_never_raises():
    assert purge_countdown("2024-01-01 00:00:00", trash_days="abc") == ""
    assert purge_countdown("2024-01-01 00:00:00", trash_days=object()) == ""


def test_countdown_reads_settings_by_default(monkeypatch):
    """Jinja 只传一个参数，此时应读 settings.trash_days。"""
    monkeypatch.setattr(settings, "trash_days", 0)
    assert purge_countdown(_ago(days=1)) == "不自动清理"
    monkeypatch.setattr(settings, "trash_days", 30)
    assert purge_countdown(_ago(seconds=3)) == "还有 30 天"


def test_countdown_matches_purge_expired_trash(tmp_path):
    """反证：purge 会清的笔记，过滤器必须说已过期；purge 留下的，倒计时仍在。"""
    path = tmp_path / "trash.db"
    db_mod.init_db(path)
    with db_mod.db(path) as conn:
        keep = repo.create_note(conn, title="还剩一天", content="x")
        gone = repo.create_note(conn, title="已经过期", content="x")
        conn.execute("UPDATE notes SET deleted_at = ? WHERE id = ?", (_ago(days=29), keep["id"]))
        conn.execute("UPDATE notes SET deleted_at = ? WHERE id = ?", (_ago(days=31), gone["id"]))

        assert purge_countdown(_ago(days=29), trash_days=30) == "明天清理"
        assert purge_countdown(_ago(days=31), trash_days=30) == "已过期，下次启动时清理"

        assert repo.purge_expired_trash(conn, 30) == 1
        assert repo.get_note(conn, keep["id"], include_deleted=True) is not None
        assert repo.get_note(conn, gone["id"], include_deleted=True) is None


# ---------------------------------------------------------------------------
# 2. 页面级：倒计时文案 / 三个原有表单
#
# test_smoke.test_logout 会把会话级 auth_client 登出，所以这里用一个全新、
# 自己登录的 TestClient：既不依赖也不会污染共享会话。
# ---------------------------------------------------------------------------
PASSWORD = "test-password"


@pytest.fixture()
def logged_client(client):
    fresh = client.__class__(client.app)
    response = fresh.post(
        "/login", data={"password": PASSWORD, "next": "/notes"}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return fresh


def _csrf_of(logged_client) -> str:
    page = logged_client.get("/notes")
    match = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
    assert match, "登录后页面里没有 csrf-token"
    return match.group(1)


def _make_trashed(client, csrf, title: str) -> int:
    created = client.post(
        "/notes",
        data={"_csrf": csrf, "title": title, "content": "倒计时正文", "action": "save"},
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text
    note_id = int(re.search(r"/notes/(\d+)", created.headers["location"]).group(1))
    deleted = client.post(
        f"/notes/{note_id}/delete",
        data={"_csrf": csrf, "next": "/notes"},
        follow_redirects=False,
    )
    assert deleted.status_code == 303, deleted.text
    return note_id


def test_trash_page_shows_countdown(logged_client):
    csrf = _csrf_of(logged_client)
    note_id = _make_trashed(logged_client, csrf, "倒计时页面用例")
    try:
        page = logged_client.get("/trash")
        assert page.status_code == 200
        days = settings.trash_days
        expected = (
            f"还有 {days} 天" if days >= 2 else ("明天清理" if days == 1 else "不自动清理")
        )
        assert expected in page.text
        assert "下次启动服务时清理" in page.text
        assert f"保留 {days} 天" in page.text
    finally:
        logged_client.post(
            f"/notes/{note_id}/purge", data={"_csrf": csrf}, follow_redirects=False
        )


def test_trash_page_no_auto_purge_when_zero(logged_client, monkeypatch):
    monkeypatch.setattr(settings, "trash_days", 0)
    csrf = _csrf_of(logged_client)
    note_id = _make_trashed(logged_client, csrf, "不自动清理页面用例")
    try:
        page = logged_client.get("/trash")
        assert page.status_code == 200
        assert "当前设置为不自动清理" in page.text
        assert "不自动清理" in page.text
    finally:
        logged_client.post(
            f"/notes/{note_id}/purge", data={"_csrf": csrf}, follow_redirects=False
        )


def test_trash_three_forms_keep_non_empty_csrf(logged_client):
    csrf = _csrf_of(logged_client)
    note_id = _make_trashed(logged_client, csrf, "表单保留用例")
    try:
        page = logged_client.get("/trash")
        assert page.status_code == 200
        html = page.text

        for action in ("/trash/empty", f"/notes/{note_id}/restore", f"/notes/{note_id}/purge"):
            match = re.search(
                r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>',
                html,
                re.S,
            )
            assert match, f"缺少原有表单 {action}"
            token = re.search(r'name="_csrf"[^>]*value="([^"]*)"', match.group(1))
            assert token and token.group(1).strip(), f"{action} 的 _csrf 为空"

        tokens = re.findall(r'name="_csrf"[^>]*value="([^"]*)"', html)
        assert tokens
        assert all(value.strip() for value in tokens), "页面上存在空的 _csrf"
    finally:
        logged_client.post(
            f"/notes/{note_id}/purge", data={"_csrf": csrf}, follow_redirects=False
        )
