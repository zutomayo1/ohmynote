"""模板变量 / 默认模板 / 存为模板 / 每日笔记。"""

from __future__ import annotations

import datetime
import re

import pytest

from app.services import note_templates


# ---------------------------------------------------------------------------
# 变量渲染（纯函数）
# ---------------------------------------------------------------------------
def test_render_variables_basic():
    now = datetime.datetime(2026, 9, 13, 14, 30)  # 星期日
    out = note_templates.render_variables(
        "{{date}} {{time}} {{year}}-{{month}} {{weekday}} {{datetime}}", now=now
    )
    assert out == "2026-09-13 14:30 2026-09 星期日 2026-09-13 14:30"


def test_render_variables_unknown_and_spaced_placeholder_untouched():
    """未知的 {{nope}} 原样保留；带空格的 {{ date }} 是受支持的宽容写法。"""
    out = note_templates.render_variables(
        "{{nope}} {{ date }} 100%", now=datetime.datetime(2026, 9, 13)
    )
    assert "{{nope}}" in out
    assert "2026-09-13" in out


def test_clean_text_passthrough():
    assert note_templates.render_variables("没有变量的正文") == "没有变量的正文"
    assert note_templates.render_variables("") == ""


# ---------------------------------------------------------------------------
# 页面与流程
# ---------------------------------------------------------------------------
def _csrf(client, path="/notes") -> str:
    page = client.get(path)
    return re.search(r'name="csrf-token" content="([^"]*)"', page.text).group(1)


def _today() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


def _template_id_by_name(client, name: str) -> int:
    """直接查数据库拿模板 id（比解析 HTML 稳）。"""
    from app import db, repo

    with db.db() as conn:
        for tpl in repo.list_templates(conn):
            if tpl["name"] == name:
                return int(tpl["id"])
    raise AssertionError(f"模板 {name!r} 不存在")


@pytest.fixture()
def db_conn(client, monkeypatch):
    from app import db

    with db.db() as conn:
        yield conn


def test_new_note_with_template_renders_variables(auth_client, csrf):
    csrf = _csrf(auth_client, "/templates")
    auth_client.post(
        "/templates/save",
        data={"_csrf": csrf, "name": "变量模板", "content": "# {{date}}\n\n今天是 {{weekday}}"},
    )
    template_id = _template_id_by_name(auth_client, "变量模板")
    page = auth_client.get(f"/notes/new?template={template_id}")
    assert page.status_code == 200
    assert _today() in page.text
    assert "星期" in page.text


def test_default_template_applied_on_new_note(auth_client, csrf):
    auth_client.post(
        "/templates/save",
        data={"_csrf": csrf, "name": "默认模板", "content": "默认内容标记 DEFAULT-CONTENT-XYZ {{date}}"},
    )
    template_id = _template_id_by_name(auth_client, "默认模板")
    auth_client.post("/templates/default", data={"_csrf": csrf, "template_id": str(template_id)})

    tpl_page = auth_client.get("/templates")
    assert "默认" in tpl_page.text

    page = auth_client.get("/notes/new")
    assert "DEFAULT-CONTENT-XYZ" in page.text
    assert _today() in page.text

    # 取消默认后不再套用
    auth_client.post("/templates/default", data={"_csrf": csrf, "template_id": "0"})
    page2 = auth_client.get("/notes/new")
    assert "DEFAULT-CONTENT-XYZ" not in page2.text


def test_today_note_creates_then_reuses(auth_client, csrf):
    auth_client.post(
        "/templates/save",
        data={"_csrf": csrf, "name": "每日笔记", "content": "# {{date}} 日记\n\n今天星期 {{weekday}}"},
    )
    first = auth_client.get("/notes/today", follow_redirects=False)
    assert first.status_code == 303
    edit_url = first.headers["location"]

    editor = auth_client.get(edit_url)
    assert _today() in editor.text
    assert "日记" in editor.text

    second = auth_client.get("/notes/today", follow_redirects=False)
    assert second.headers["location"] == edit_url

    from app import db

    with db.db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM notes WHERE title = ?", (_today(),)
        ).fetchone()["c"]
    assert count == 1


def test_save_note_as_template(auth_client, csrf):
    created = auth_client.post(
        "/notes",
        data={"_csrf": csrf, "title": "周报结构", "content": "## 本周\n## 下周", "action": "save"},
        follow_redirects=False,
    )
    assert created.status_code in (200, 303)
    note_id = re.search(r"/notes/(\d+)", created.headers.get("location", "/notes/1")).group(1)

    saved = auth_client.post(
        f"/notes/{note_id}/save-as-template",
        headers={"X-CSRF-Token": _csrf(auth_client, f"/notes/{note_id}/edit")},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert "/templates" in saved.headers["location"]

    tpl_page = auth_client.get("/templates")
    assert "周报结构" in tpl_page.text


def test_editor_has_save_as_template_button(auth_client, csrf):
    created = auth_client.post(
        "/notes",
        data={"_csrf": csrf, "title": "带模板按钮", "action": "save"},
        follow_redirects=False,
    )
    note_id = re.search(r"/notes/(\d+)", created.headers.get("location", "/notes/1")).group(1)
    editor = auth_client.get(f"/notes/{note_id}/edit")
    assert "save-as-template" in editor.text


def test_nav_has_today_entry(auth_client):
    assert "/notes/today" in auth_client.get("/notes").text
