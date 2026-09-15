# -*- coding: utf-8 -*-
"""建立联系（双链）：标题补全查询 + 建链 / 取消链接端点。

约定：会话级 DB 共享，禁止断言全局聚合 —— 一律用专属前缀定位自己造的笔记。
"""

from __future__ import annotations

import pytest

from app import repo

PREFIX = "连链-"


@pytest.fixture()
def db_conn(client):
    from app import db

    with db.db() as conn:
        yield conn


def _make(db, title, content=""):
    note = repo.create_note(db, title=title, content=content)
    db.commit()
    return note


# ---------------------------------------------------------------- search_titles
def test_search_titles_ranking_and_exclude(db_conn):
    key = PREFIX + "甲"
    _make(db_conn, key)                       # 完全相等
    _make(db_conn, key + " 前缀命中")          # 前缀命中
    _make(db_conn, "学 " + key + " 的日子")     # 包含
    me = _make(db_conn, key + " 自己")

    rows = repo.search_titles(db_conn, key, limit=10)
    titles = [r["title"] for r in rows]
    assert titles[0] == key
    assert titles.index(key + " 前缀命中") < titles.index("学 " + key + " 的日子")

    excluded = repo.search_titles(db_conn, key, limit=10, exclude_id=me["id"])
    assert me["title"] not in [r["title"] for r in excluded]


def test_search_titles_empty_query_returns_recent(db_conn):
    note = _make(db_conn, PREFIX + "最近")
    rows = repo.search_titles(db_conn, "", limit=5)
    assert rows and all({"id", "title", "updated_at"} <= set(r) for r in rows)
    assert note["id"] in [r["id"] for r in rows]


def test_search_titles_escapes_like_wildcards(db_conn):
    only = _make(db_conn, PREFIX + "百分号%标题")
    _make(db_conn, PREFIX + "普通标题")
    rows = repo.search_titles(db_conn, "%", limit=10)
    # 没转义的话 % 会命中一切
    assert [r["id"] for r in rows] == [only["id"]]


def test_search_titles_excludes_trash(db_conn):
    note = _make(db_conn, PREFIX + "待删")
    repo.soft_delete(db_conn, note["id"])
    db_conn.commit()
    assert note["id"] not in [r["id"] for r in repo.search_titles(db_conn, PREFIX + "待删", limit=10)]


# ---------------------------------------------------------------- /api/note-titles
def test_api_note_titles(auth_client, db_conn):
    note = _make(db_conn, PREFIX + "接口候选")
    resp = auth_client.get("/api/note-titles", params={"q": PREFIX + "接口", "limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert note["id"] in [item["id"] for item in data["items"]]

    excluded = auth_client.get(
        "/api/note-titles", params={"q": PREFIX + "接口", "exclude": note["id"]}
    ).json()
    assert note["id"] not in [item["id"] for item in excluded["items"]]
    # 没关键词时给最近更新的候选（打开输入框就有东西可选）
    assert auth_client.get("/api/note-titles").json()["items"]


# ---------------------------------------------------------------- 建链
def _links_of(db, note_id):
    return {
        (row["target_id"], row["target_title"])
        for row in db.execute(
            "SELECT target_id, target_title FROM note_links WHERE source_id = ?", (note_id,)
        ).fetchall()
    }


def test_link_by_target_id_appends_and_syncs(auth_client, csrf, db_conn):
    src = _make(db_conn, PREFIX + "源笔记", "原有正文")
    dst = _make(db_conn, PREFIX + "目标笔记")
    resp = auth_client.post(
        f"/notes/{src['id']}/link", data={"_csrf": csrf, "target_id": str(dst["id"])}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "msg=" in resp.headers["location"]

    after = repo.get_note(db_conn, src["id"])
    assert after["content"].startswith("原有正文")
    assert f"[[{PREFIX}目标笔记]]" in after["content"]
    # 链接表同步（反向链接靠它）
    assert (dst["id"], PREFIX + "目标笔记") in _links_of(db_conn, src["id"])
    # 目标那篇能看到反向链接
    assert src["id"] in [b["id"] for b in repo.backlinks(db_conn, dst["id"])]


def test_link_rejects_self_and_missing(auth_client, csrf, db_conn):
    src = _make(db_conn, PREFIX + "自连")
    from urllib.parse import unquote

    r1 = auth_client.post(f"/notes/{src['id']}/link", data={"_csrf": csrf, "target_id": str(src["id"])}, follow_redirects=False)
    assert "不能连接" in unquote(r1.headers["location"])
    r2 = auth_client.post(f"/notes/{src['id']}/link", data={"_csrf": csrf, "target_id": "99999999"}, follow_redirects=False)
    assert r2.status_code == 303
    assert "不存在" in unquote(r2.headers["location"])
    assert repo.get_note(db_conn, src["id"])["content"].strip() == ""


def test_link_is_idempotent(auth_client, csrf, db_conn):
    src = _make(db_conn, PREFIX + "重复源")
    dst = _make(db_conn, PREFIX + "重复目标")
    auth_client.post(f"/notes/{src['id']}/link", data={"_csrf": csrf, "target_id": str(dst["id"])}, follow_redirects=False)
    content_once = repo.get_note(db_conn, src["id"])["content"]
    from urllib.parse import unquote

    resp = auth_client.post(f"/notes/{src['id']}/link", data={"_csrf": csrf, "target_id": str(dst["id"])}, follow_redirects=False)
    assert "已经连到" in unquote(resp.headers["location"])
    assert repo.get_note(db_conn, src["id"])["content"] == content_once


def test_link_by_title_exact_and_unique_prefix(auth_client, csrf, db_conn):
    src = _make(db_conn, PREFIX + "标题源")
    dst = _make(db_conn, PREFIX + "标题目标甲")
    auth_client.post(f"/notes/{src['id']}/link", data={"_csrf": csrf, "target_title": PREFIX + "标题目标甲"}, follow_redirects=False)
    assert f"[[{PREFIX}标题目标甲]]" in repo.get_note(db_conn, src["id"])["content"]

    # 唯一命中也能连上
    dst2 = _make(db_conn, PREFIX + "独一份")
    auth_client.post(f"/notes/{src['id']}/link", data={"_csrf": csrf, "target_title": PREFIX + "独一份"}, follow_redirects=False)
    assert f"[[{PREFIX}独一份]]" in repo.get_note(db_conn, src["id"])["content"]
    assert dst["id"] and dst2["id"]


def test_link_by_title_ambiguous_and_not_found(auth_client, csrf, db_conn):
    src = _make(db_conn, PREFIX + "歧义源")
    _make(db_conn, PREFIX + "重名甲")
    _make(db_conn, PREFIX + "重名乙")
    before = repo.get_note(db_conn, src["id"])["content"]

    from urllib.parse import unquote

    r1 = auth_client.post(f"/notes/{src['id']}/link", data={"_csrf": csrf, "target_title": "重名"}, follow_redirects=False)
    assert r1.status_code == 303 and repo.get_note(db_conn, src["id"])["content"] == before
    assert "匹配到多篇" in unquote(r1.headers["location"])

    r2 = auth_client.post(f"/notes/{src['id']}/link", data={"_csrf": csrf, "target_title": "根本不存在的标题"}, follow_redirects=False)
    assert r2.status_code == 303 and repo.get_note(db_conn, src["id"])["content"] == before

    r3 = auth_client.post(f"/notes/{src['id']}/link", data={"_csrf": csrf, "target_title": ""}, follow_redirects=False)
    assert r3.status_code == 303 and repo.get_note(db_conn, src["id"])["content"] == before


def test_link_into_empty_note_has_no_leading_blank(auth_client, csrf, db_conn):
    src = _make(db_conn, PREFIX + "空正文")
    dst = _make(db_conn, PREFIX + "空正文目标")
    auth_client.post(f"/notes/{src['id']}/link", data={"_csrf": csrf, "target_id": str(dst["id"])}, follow_redirects=False)
    content = repo.get_note(db_conn, src["id"])["content"]
    assert content == f"[[{PREFIX}空正文目标]]"


# ---------------------------------------------------------------- 取消链接
def test_unlink_removes_marker_and_tidies(auth_client, csrf, db_conn):
    dst = _make(db_conn, PREFIX + "取消目标")
    src = _make(
        db_conn,
        PREFIX + "取消源",
        "第一段\n\n[[{}取消目标]]\n\n第三段".format(PREFIX),
    )
    resp = auth_client.post(
        f"/notes/{src['id']}/unlink", data={"_csrf": csrf, "target_id": str(dst["id"])}, follow_redirects=False
    )
    assert resp.status_code == 303
    content = repo.get_note(db_conn, src["id"])["content"]
    assert "取消目标" not in content
    assert "第一段" in content and "第三段" in content
    assert "\n\n\n" not in content
    assert not _links_of(db_conn, src["id"])


def test_unlink_handles_alias_and_reports_missing(auth_client, csrf, db_conn):
    dst = _make(db_conn, PREFIX + "别名目标")
    src = _make(db_conn, PREFIX + "别名源", "看这里 [[{}别名目标|换个说法]] 就行".format(PREFIX))
    auth_client.post(f"/notes/{src['id']}/unlink", data={"_csrf": csrf, "target_id": str(dst["id"])}, follow_redirects=False)
    content = repo.get_note(db_conn, src["id"])["content"]
    assert "别名目标" not in content and "[[".strip() not in content

    # 再取消一次：已经没有可删的
    resp = auth_client.post(f"/notes/{src['id']}/unlink", data={"_csrf": csrf, "target_id": str(dst["id"])}, follow_redirects=False)
    assert resp.status_code == 303
    assert repo.get_note(db_conn, src["id"])["content"] == content


# ---------------------------------------------------------------- 页面
def test_note_detail_has_link_form_and_unlink(auth_client, db_conn):
    dst = _make(db_conn, PREFIX + "页面目标")
    src = _make(db_conn, PREFIX + "页面源", "[[{}页面目标]]".format(PREFIX))
    resp = auth_client.get(f"/notes/{src['id']}")
    assert resp.status_code == 200
    text = resp.text
    assert 'data-link-add-input' in text
    assert f'action="/notes/{src["id"]}/link"' in text
    assert f'action="/notes/{src["id"]}/unlink"' in text
    assert 'link-del__btn' in text
    assert dst["id"] > 0
