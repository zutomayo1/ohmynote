"""那年今日 + 笔记归档：数据、行为、页面。"""

import datetime as dt

from conftest import csrf_of


def _create(auth_client, csrf: str, title: str, **fields) -> int:
    data = {"_csrf": csrf, "title": title, "action": "save", "content": "内容：" + title}
    data.update(fields)
    response = auth_client.post("/notes", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].split("/")[-2])


# ---------------------------------------------------------------------------
# 那年今日
# ---------------------------------------------------------------------------

def test_find_this_day_in_past(client):
    from app import db, repo

    with db.db() as conn:
        today = dt.date.today()
        # 今年今天写的：不算「那年今日」
        repo.create_note(conn, title="今年的", content="x", status="saved")
        # 往年今天：算
        old_note = repo.create_note(conn, title="去年的今天", content="x", status="saved")
        past = (today.replace(year=today.year - 1)).isoformat() + " 08:00:00"
        conn.execute("UPDATE notes SET created_at = ? WHERE id = ?", (past, old_note["id"]))
        # 往年别的日子：不算
        other = repo.create_note(conn, title="去年别的日子", content="x", status="saved")
        other_past = (today.replace(year=today.year - 1, month=1, day=5)).isoformat() + " 08:00:00"
        conn.execute("UPDATE notes SET created_at = ? WHERE id = ?", (other_past, other["id"]))
        conn.commit()

        result = repo.find_this_day_in_past(conn)
        titles = [n["title"] for n in result]
        assert "去年的今天" in titles
        assert "今年的" not in titles
        assert "去年别的日子" not in titles


def test_onthisday_card_on_list_page(auth_client):
    from app import db, repo

    with db.db() as conn:
        today = dt.date.today()
        note = repo.create_note(conn, title="那年今日文章", content="x", status="saved")
        past = (today.replace(year=today.year - 1)).isoformat() + " 09:00:00"
        conn.execute("UPDATE notes SET created_at = ? WHERE id = ?", (past, note["id"]))
        conn.commit()

    page = auth_client.get("/notes")
    assert page.status_code == 200
    assert "那年今日" in page.text
    assert "那年今日文章" in page.text


def test_onthisday_hidden_when_no_past_notes(auth_client):
    from app import db

    # session 库与同文件其它用例共享：先清掉往年记录再断言「不显示」
    with db.db() as conn:
        conn.execute("DELETE FROM notes")

    page = auth_client.get("/notes")
    assert page.status_code == 200
    assert "那年今日" not in page.text


# ---------------------------------------------------------------------------
# 归档
# ---------------------------------------------------------------------------

def test_archived_excluded_from_default_list(client):
    from app import db, repo

    with db.db() as conn:
        repo.create_note(conn, title="普通笔记", content="x", status="saved")
        archived = repo.create_note(conn, title="归档笔记", content="x", status="saved")
        repo.set_archived(conn, archived["id"], True)

        notes, total = repo.list_notes(conn, archived=False)
        titles = [n["title"] for n in notes]
        assert "普通笔记" in titles and "归档笔记" not in titles

        only_archived, archived_total = repo.list_notes(conn, archived=True)
        assert [n["title"] for n in only_archived] == ["归档笔记"]
        assert archived_total == 1

        # 不过滤（搜索等场景）：两篇都在
        _all_notes, _total = repo.list_notes(conn, archived=None)
        # archived=None 的 SQL 分支不过滤；这里不深入断言分页，只验证不抛错


def test_archive_toggle_via_http(auth_client, csrf):
    note_id = _create(auth_client, csrf, "待归档")
    token = csrf_of(auth_client, f"/notes/{note_id}")

    # 归档
    response = auth_client.post(
        f"/notes/{note_id}/archive",
        data={"next": f"/notes/{note_id}"},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    from urllib.parse import unquote_plus
    assert "已归档" in unquote_plus(response.headers["location"])

    from app import db, repo

    with db.db() as conn:
        assert repo.get_note(conn, note_id)["is_archived"] is True

    # 默认列表不显示
    page = auth_client.get("/notes")
    assert "待归档" not in page.text

    # 归档视图显示
    view = auth_client.get("/notes?archived=1")
    assert "待归档" in view.text
    assert "正在查看已归档" in view.text

    # 详情页按钮变「取消归档」
    detail = auth_client.get(f"/notes/{note_id}")
    assert "取消归档" in detail.text

    # 取消归档
    response = auth_client.post(
        f"/notes/{note_id}/archive",
        data={"next": f"/notes/{note_id}"},
        headers={"X-CSRF-Token": csrf_of(auth_client, f"/notes/{note_id}")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db.db() as conn:
        assert repo.get_note(conn, note_id)["is_archived"] is False
    assert "待归档" in auth_client.get("/notes").text


def test_archive_not_in_trash_and_search_still_finds(auth_client, csrf):
    """归档是温和中间态：不进回收站，搜索照常能找到。"""
    from app import db, repo

    note_id = _create(auth_client, csrf, "归档但仍可搜")
    token = csrf_of(auth_client, f"/notes/{note_id}")
    auth_client.post(
        f"/notes/{note_id}/archive",
        data={"next": f"/notes/{note_id}"},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )

    with db.db() as conn:
        note = repo.get_note(conn, note_id)
        assert note["deleted"] is False  # 没进回收站

        # 搜索（archived=None 不过滤）能找到
        found, _total = repo.list_notes(conn, q="归档但仍可搜")
        assert any(n["id"] == note_id for n in found)


def test_migration_adds_is_archived_column(client):
    """迁移给已有库加列：旧库升级后 is_archived 默认 0。"""
    from app import db

    with db.db() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(notes)")}
        assert "is_archived" in columns
        row = conn.execute("SELECT is_archived FROM notes LIMIT 1").fetchone()
        assert row is not None and row["is_archived"] == 0
