"""N+1 消除的行为等价性：批量分组函数的结果必须与逐个查询完全一致。"""

import json

from conftest import csrf_of


def _seed(conn, notes_spec):
    """notes_spec: [(title, content, tags, public)]"""
    for title, content, tags, public in notes_spec:
        note = repo.create_note(
            conn, title=title, content=content, tags=tags,
            status="saved", is_public=public,
        )
    return None


def _make_conn(client):
    from app import db

    with db.db() as conn:
        yield conn


# ---------------------------------------------------------------------------
# notes_grouped_by_tags
# ---------------------------------------------------------------------------

def test_grouped_by_tags_matches_per_tag_queries(client):
    from app import db, repo

    with db.db() as conn:
        repo.create_note(conn, title="甲", content="甲", tags=["工作", "生活"], status="saved")
        repo.create_note(conn, title="乙", content="乙", tags=["工作"], status="saved")
        for i in range(4):
            repo.create_note(conn, title=f"工作{i}", content="x", tags=["工作"], status="saved")

        names = ["工作", "生活", "不存在的标签"]
        grouped = repo.notes_grouped_by_tags(conn, names, per_page=2)

        # 与逐个查询完全一致
        for name in names:
            expected, _total = repo.list_notes(conn, tag=name, per_page=2)
            got = grouped.get(name, [])
            assert [n["id"] for n in got] == [n["id"] for n in expected], name

        # 空标签组在路由层给默认空列表，与旧实现行为一致
        assert grouped.get("不存在的标签", []) == []


def test_grouped_by_tags_pinned_first_and_multi_tag_membership(client):
    from app import db, repo

    with db.db() as conn:
        a = repo.create_note(conn, title="普通", content="x", tags=["工作"], status="saved")
        b = repo.create_note(conn, title="置顶", content="x", tags=["工作"], status="saved")
        repo.update_note(conn, b["id"], is_pinned=True, reason="test")

        grouped = repo.notes_grouped_by_tags(conn, ["工作"], per_page=5)
        ids = [n["id"] for n in grouped["工作"]]
        assert ids[0] == b["id"]  # 置顶优先，与 list_notes 口径一致
        assert a["id"] in ids

        # 一篇笔记挂两个标签：两个组里都出现（与逐个查询行为相同）
        c = repo.create_note(conn, title="双标签", content="x", tags=["工作", "生活"], status="saved")
        grouped = repo.notes_grouped_by_tags(conn, ["工作", "生活"], per_page=5)
        assert c["id"] in [n["id"] for n in grouped["工作"]]
        assert c["id"] in [n["id"] for n in grouped["生活"]]


def test_grouped_by_tags_excludes_deleted(client):
    from app import db, repo

    with db.db() as conn:
        note = repo.create_note(conn, title="会删的", content="x", tags=["临时"], status="saved")
        repo.soft_delete(conn, note["id"])

        grouped = repo.notes_grouped_by_tags(conn, ["临时"], per_page=5)
        assert grouped.get("临时", []) == []


def test_grouped_by_tags_empty_input(client):
    from app import db, repo

    with db.db() as conn:
        assert repo.notes_grouped_by_tags(conn, [], per_page=5) == {}


# ---------------------------------------------------------------------------
# notes_grouped_by_months
# ---------------------------------------------------------------------------

def test_grouped_by_months_matches_per_month_queries(client):
    from app import db, repo

    with db.db() as conn:
        # created_at/updated_at 由 repo 生成（当前月）；手动改月份造多个月
        n1 = repo.create_note(conn, title="八月", content="x", status="saved", is_public=True)
        n2 = repo.create_note(conn, title="九月", content="x", status="saved", is_public=True)
        repo.update_note(conn, n1["id"], reason="test")
        conn.execute("UPDATE notes SET updated_at = '2026-08-01 10:00:00' WHERE id = ?", (n1["id"],))
        conn.commit()

        months = repo.archive_months(conn)
        keys = [m["key"] for m in months]
        assert len(keys) >= 2

        grouped = repo.notes_grouped_by_months(conn, keys, per_page=100)
        for key in keys:
            expected, _total = repo.list_notes(conn, month=key, public_only=True, per_page=100)
            got = grouped.get(key, [])
            assert [n["id"] for n in got] == [n["id"] for n in expected], key


def test_grouped_by_months_private_excluded(client):
    from app import db, repo

    with db.db() as conn:
        pub = repo.create_note(conn, title="公开", content="x", status="saved", is_public=True)
        repo.create_note(conn, title="私密", content="x", status="saved", is_public=False)

        months = repo.archive_months(conn)
        grouped = repo.notes_grouped_by_months(conn, [m["key"] for m in months])
        ids = [n["id"] for group in grouped.values() for n in group]
        assert pub["id"] in ids
        assert all(n["is_public"] for group in grouped.values() for n in group)


# ---------------------------------------------------------------------------
# 页面行为不变
# ---------------------------------------------------------------------------

def test_tags_page_renders_with_groups(auth_client, csrf):
    from app import db, repo

    with db.db() as conn:
        for i in range(3):
            repo.create_note(conn, title=f"标签页笔记{i}", content="x", tags=["页面"], status="saved")

    page = auth_client.get("/tags")
    assert page.status_code == 200
    assert "页面" in page.text
    assert "标签页笔记0" in page.text


def test_blog_archive_renders_with_groups(client):
    from app import db, repo

    with db.db() as conn:
        repo.create_note(conn, title="公开笔记", content="x", status="saved", is_public=True)

    page = client.get("/blog/archive")
    assert page.status_code == 200
    assert "公开笔记" in page.text


def test_related_notes_still_has_tags(client):
    """合并 _tags_map 查询后，相关笔记的 tags 字段照常。"""
    from app import db, repo

    with db.db() as conn:
        base = repo.create_note(conn, title="主笔记", content="x", tags=["共享"], status="saved")
        other = repo.create_note(conn, title="相关笔记", content="x", tags=["共享"], status="saved")

        related = repo.related_notes(conn, repo.get_note(conn, base["id"]), limit=5)
        ids = [n["id"] for n in related]
        assert other["id"] in ids
        item = next(n for n in related if n["id"] == other["id"])
        assert item["tags"] == ["共享"]
        assert "共享" in item["reason"]
