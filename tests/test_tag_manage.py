"""标签可管理：rename_tag / delete_tag / purge_unused_tags 与 /tags 管理路由。"""

from __future__ import annotations

import re

import pytest

from app import db as db_mod, repo


@pytest.fixture()
def conn(tmp_path):
    """隔离数据库，专门跑数据层用例，避免和冒烟测试的共享库互相污染。"""
    path = tmp_path / "tag-manage.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


def _tags_of(conn, note_id):
    note = repo.get_note(conn, note_id)
    assert note is not None
    return note["tags"]


# ---------------------------------------------------------------------------
# 数据层：重命名 / 合并
# ---------------------------------------------------------------------------
def test_rename_changes_every_note(conn):
    first = repo.create_note(conn, title="甲的第一篇", content="x", tags="甲")
    second = repo.create_note(conn, title="甲的第二篇", content="x", tags="甲")

    result = repo.rename_tag(conn, "甲", "乙")

    assert result == {"renamed": 1, "merged": False, "notes": 2, "name": "乙"}
    assert _tags_of(conn, first["id"]) == ["乙"]
    assert _tags_of(conn, second["id"]) == ["乙"]
    # tags 表里的旧行也没了
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM tags WHERE name = ? COLLATE NOCASE", ("甲",)
    ).fetchone()
    assert row["c"] == 0


def test_rename_merges_instead_of_duplicating(conn):
    old_notes = [
        repo.create_note(conn, title=f"甲的第 {index} 篇", content="x", tags="甲")
        for index in range(3)
    ]
    existing = repo.create_note(conn, title="原本就有乙", content="x", tags="乙")

    result = repo.rename_tag(conn, "甲", "乙")

    assert result["renamed"] == 1
    assert result["merged"] is True
    assert result["notes"] == 3
    assert result["name"] == "乙"

    notes = repo.list_notes(conn, per_page=100)[0]
    by_id = {note["id"]: note for note in notes}
    assert set(by_id) == {note["id"] for note in old_notes} | {existing["id"]}
    for note in by_id.values():
        assert note["tags"] == ["乙"]

    # 关系必须是并集且没有重复（schema 主键也保证不会出现重复行）
    duplicates = conn.execute(
        "SELECT COUNT(*) AS c FROM ("
        " SELECT note_id, tag_id FROM note_tags GROUP BY note_id, tag_id HAVING COUNT(*) > 1"
        ")"
    ).fetchone()
    assert duplicates["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM tags WHERE name = ? COLLATE NOCASE", ("甲",)
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM tags WHERE name = ? COLLATE NOCASE", ("乙",)
    ).fetchone()["c"] == 1


def test_merge_handles_notes_that_already_have_both(conn):
    both = repo.create_note(conn, title="两边都有", content="x", tags="甲, 乙")
    only_old = repo.create_note(conn, title="只有甲", content="x", tags="甲")

    result = repo.rename_tag(conn, "甲", "乙")

    assert result["merged"] is True
    assert result["notes"] == 2
    assert _tags_of(conn, both["id"]) == ["乙"]
    assert _tags_of(conn, only_old["id"]) == ["乙"]
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM tags WHERE name = ? COLLATE NOCASE", ("甲",)
    ).fetchone()["c"] == 0


def test_rename_can_change_only_case(conn):
    note = repo.create_note(conn, title="大小写", content="x", tags="python")

    result = repo.rename_tag(conn, "python", "Python")

    assert result == {"renamed": 1, "merged": False, "notes": 1, "name": "Python"}
    assert _tags_of(conn, note["id"]) == ["Python"]
    assert conn.execute("SELECT name FROM tags").fetchone()["name"] == "Python"


@pytest.mark.parametrize("bad_new", ["", "   ", "x" * 41])
def test_rename_rejects_invalid_new_names_without_touching_data(conn, bad_new):
    note = repo.create_note(conn, title="非法输入", content="x", tags="甲")

    with pytest.raises(ValueError):
        repo.rename_tag(conn, "甲", bad_new)

    assert _tags_of(conn, note["id"]) == ["甲"]
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM tags WHERE name = ? COLLATE NOCASE", ("甲",)
    ).fetchone()["c"] == 1


def test_rename_missing_old_raises(conn):
    with pytest.raises(ValueError):
        repo.rename_tag(conn, "根本没有", "新名字")


# ---------------------------------------------------------------------------
# 数据层：删除 / 清理
# ---------------------------------------------------------------------------
def test_delete_removes_tag_from_all_notes(conn):
    first = repo.create_note(conn, title="待删甲一", content="x", tags="甲")
    second = repo.create_note(conn, title="待删甲二", content="x", tags="甲")

    result = repo.delete_tag(conn, "甲")

    assert result == {"deleted": True, "notes": 2}
    assert "甲" not in _tags_of(conn, first["id"])
    assert "甲" not in _tags_of(conn, second["id"])
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM tags WHERE name = ? COLLATE NOCASE", ("甲",)
    ).fetchone()["c"] == 0


def test_delete_missing_tag_is_a_noop(conn):
    assert repo.delete_tag(conn, "根本没有") == {"deleted": False, "notes": 0}
    assert repo.delete_tag(conn, "") == {"deleted": False, "notes": 0}


def test_purge_unused_tags_removes_orphans(conn):
    conn.execute(
        "INSERT INTO tags (name, created_at) VALUES (?, ?)",
        ("孤儿标签", "2024-01-01 00:00:00"),
    )

    assert repo.purge_unused_tags(conn) == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM tags").fetchone()["c"] == 0
    # 再清一次不会误报
    assert repo.purge_unused_tags(conn) == 0


def test_list_tags_search_and_sort_keep_default_order(conn):
    repo.create_note(conn, title="苹果", content="x", tags="apple")
    repo.create_note(conn, title="香蕉一", content="x", tags="banana")
    repo.create_note(conn, title="香蕉二", content="x", tags="banana")

    assert [item["name"] for item in repo.list_tags(conn, q="ban")] == ["banana"]
    assert [item["name"] for item in repo.list_tags(conn, sort="name")] == ["apple", "banana"]
    # 默认行为不变：使用次数最多的排最前
    assert [item["name"] for item in repo.list_tags(conn)][0] == "banana"


# ---------------------------------------------------------------------------
# HTTP：路由与页面
# ---------------------------------------------------------------------------
def _create_http_note(auth_client, csrf: str, title: str, tags: str) -> int:
    response = auth_client.post(
        "/notes",
        data={"_csrf": csrf, "title": title, "content": "正文", "tags": tags, "action": "save"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    match = re.search(r"/notes/(\d+)", response.headers["location"])
    assert match, response.headers["location"]
    return int(match.group(1))


def _purge_http_note(auth_client, csrf: str, note_id: int) -> None:
    auth_client.post(
        f"/notes/{note_id}/purge",
        data={"_csrf": csrf, "next": "/notes"},
        follow_redirects=False,
    )


def test_http_rename_route(auth_client, csrf):
    note_id = _create_http_note(auth_client, csrf, "HTTP 改名", "HTTP旧名")
    try:
        response = auth_client.post(
            "/tags/rename",
            data={"_csrf": csrf, "old": "HTTP旧名", "new": "HTTP新名"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "/tags" in response.headers["location"]
        with db_mod.db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM tags WHERE name = ? COLLATE NOCASE", ("HTTP旧名",)
            ).fetchone()["c"] == 0
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM tags WHERE name = ? COLLATE NOCASE", ("HTTP新名",)
            ).fetchone()["c"] == 1
    finally:
        _purge_http_note(auth_client, csrf, note_id)


def test_http_delete_route(auth_client, csrf):
    note_id = _create_http_note(auth_client, csrf, "HTTP 删除", "HTTP待删")
    try:
        response = auth_client.post(
            "/tags/delete",
            data={"_csrf": csrf, "name": "HTTP待删"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with db_mod.db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS c FROM tags WHERE name = ? COLLATE NOCASE", ("HTTP待删",)
            ).fetchone()["c"] == 0
    finally:
        _purge_http_note(auth_client, csrf, note_id)


def test_http_cleanup_route(auth_client, csrf):
    with db_mod.db() as conn:
        conn.execute(
            "INSERT INTO tags (name, created_at) VALUES (?, ?)",
            ("HTTP孤儿", "2024-01-01 00:00:00"),
        )

    response = auth_client.post(
        "/tags/cleanup", data={"_csrf": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    with db_mod.db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM tags WHERE name = ? COLLATE NOCASE", ("HTTP孤儿",)
        ).fetchone()["c"] == 0


def test_http_routes_require_login(client):
    fresh = client.__class__(client.app)
    for path, data in (
        ("/tags/rename", {"old": "a", "new": "b"}),
        ("/tags/delete", {"name": "a"}),
        ("/tags/cleanup", {}),
    ):
        response = fresh.post(path, data=data, follow_redirects=False)
        assert response.status_code == 303, f"{path} -> {response.status_code}"
        assert "/login" in response.headers.get("location", "")


def test_http_routes_require_csrf(auth_client):
    for path, data in (
        ("/tags/rename", {"old": "a", "new": "b"}),
        ("/tags/delete", {"name": "a"}),
        ("/tags/cleanup", {}),
    ):
        response = auth_client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 403, f"{path} -> {response.status_code}"


def test_http_bad_input_never_500(auth_client, csrf):
    cases = (
        ("/tags/rename", {"_csrf": csrf, "old": "不存在", "new": "x"}),
        ("/tags/rename", {"_csrf": csrf, "old": "不存在", "new": "   "}),
        ("/tags/rename", {"_csrf": csrf, "old": "不存在", "new": "x" * 41}),
        ("/tags/delete", {"_csrf": csrf, "name": "不存在"}),
        ("/tags/delete", {"_csrf": csrf, "name": ""}),
    )
    for path, data in cases:
        response = auth_client.post(path, data=data, follow_redirects=False)
        assert response.status_code != 500, f"{path} 仍然 500"
        assert response.status_code in (303, 400), f"{path} -> {response.status_code}"


def test_tags_page_has_management_ui(auth_client, csrf):
    note_id = _create_http_note(auth_client, csrf, "页面标签测试", "页面管理标签")
    try:
        page = auth_client.get("/tags")
        assert page.status_code == 200
        text = page.text
        assert 'action="/tags/rename"' in text
        assert 'action="/tags/delete"' in text
        assert 'action="/tags/cleanup"' in text
        assert "管理标签" in text
        assert "tag-pill" in text
        assert (
            'data-confirm="删除标签《页面管理标签》？所有笔记上的这个标签都会被移除。"'
            in text
        )
    finally:
        _purge_http_note(auth_client, csrf, note_id)
