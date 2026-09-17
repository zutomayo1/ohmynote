"""锁定笔记（单篇密码）的测试。

覆盖：
1. 密码派生与校验（pbkdf2、库里没有明文、错密码/空值/坏格式都 False）；
2. 解锁 cookie：签名校验、防篡改、用途标记、过期；
3. 设锁的效果：locked=1、自动取消公开、正文从 FTS 索引里撤下；
4. 门禁：另一个会话打开阅读页 / 编辑器 / 单篇导出 / 保存接口都被挡且不泄漏正文；
5. 解锁：错密码 403、对密码 303 并种 cookie、解锁后能看正文、重新锁定后又挡住；
6. 不可见面：搜索、博客、RSS/sitemap、列表摘要都不出现锁定内容；
7. 解除锁定：locked=0、正文回到搜索索引。
"""

from __future__ import annotations

import re

import pytest

from app import db as db_mod
from app import repo, search
from app.services import note_lock
from tests.test_agent import db_conn  # noqa: F401  fixture 随模块导入

PASSWORD = "test-password"
SECRET_TEXT = "绝密口令"
LOCK_PASSWORD = "s3cret"


@pytest.fixture()
def app_client():
    """另一个浏览器会话：独立 cookie jar，用来验证「换个浏览器就要密码」。"""
    from fastapi.testclient import TestClient

    from app.main import app as the_app

    with TestClient(the_app) as fresh:
        fresh.post("/login", data={"password": PASSWORD})
        yield fresh


def csrf_of(client) -> str:
    return re.search(r'name="csrf-token" content="([^"]*)"', client.get("/notes").text).group(1)


@pytest.fixture()
def locked_note(db_conn, auth_client):
    """建一篇公开笔记并设锁，返回 (note_id, csrf)。"""
    note = repo.create_note(db_conn, title="锁定测试笔记", content=f"正文里有 {SECRET_TEXT} 三个字",
                            is_public=True, status="saved")
    db_conn.commit()
    token = csrf_of(auth_client)
    response = auth_client.post(f"/notes/{note['id']}/lock",
                               data={"_csrf": token, "password": LOCK_PASSWORD},
                               follow_redirects=False)
    assert response.status_code == 303
    yield note["id"], token
    with db_mod.db() as conn:
        conn.execute("DELETE FROM notes WHERE id = ?", (note["id"],))
        conn.commit()


# ---------------------------------------------------------------------------
# 1. 密码派生与校验
# ---------------------------------------------------------------------------

def test_hash_is_pbkdf2_and_hides_plaintext():
    stored = note_lock.hash_password(LOCK_PASSWORD)
    assert stored.startswith("pbkdf2$")
    assert LOCK_PASSWORD not in stored
    assert note_lock.verify_password(LOCK_PASSWORD, stored)
    assert not note_lock.verify_password("wrong", stored)
    assert not note_lock.verify_password("", stored)


def test_hash_is_salted_per_call():
    assert note_lock.hash_password("same") != note_lock.hash_password("same")


@pytest.mark.parametrize("bad", ["", "garbage", "pbkdf2$abc", "md5$1$2$3", "pbkdf2$120000$!!!$!!!"])
def test_verify_rejects_malformed(bad):
    assert not note_lock.verify_password("x", bad)


# ---------------------------------------------------------------------------
# 2. 解锁 cookie
# ---------------------------------------------------------------------------

def test_unlock_cookie_is_signed_and_scoped():
    token = note_lock.cookie_value([3, 5])
    assert note_lock._read(token) == [3, 5]
    assert note_lock._read(token[:-5] + "00000") == [], "篡改必须被拒"
    other = note_lock._sign({"k": "别的用途", "notes": [1], "exp": 4102444800})
    assert note_lock._read(other) == [], "别的用途的签名不能当解锁用"
    expired = note_lock._sign({"k": "note-unlock", "notes": [1], "exp": 1})
    assert note_lock._read(expired) == [], "过期要失效"


# ---------------------------------------------------------------------------
# 3. 设锁的效果
# ---------------------------------------------------------------------------

def test_lock_forces_private_and_drops_index(db_conn, auth_client, locked_note):
    note_id, _token = locked_note
    note = repo.get_note(db_conn, note_id)
    assert note["locked"] == 1
    assert not note["is_public"], "锁定要自动取消公开"
    assert LOCK_PASSWORD not in (note["lock_hash"] or ""), "库里不能有明文"
    if search.FTS_ENABLED:
        row = db_conn.execute("SELECT 1 FROM notes_fts WHERE note_id = ?", (note_id,)).fetchone()
        assert row is None, "锁定后正文要从搜索索引里撤掉"


# ---------------------------------------------------------------------------
# 4. 门禁：另一个浏览器会话
# ---------------------------------------------------------------------------

def test_locked_note_gated_for_other_session(app_client, db_conn, locked_note):
    note_id, _token = locked_note
    page = app_client.get(f"/notes/{note_id}")
    assert page.status_code == 403
    assert "这篇笔记已锁定" in page.text
    assert SECRET_TEXT not in page.text, "密码页不能把正文带出来"
    assert SECRET_TEXT not in app_client.get(f"/notes/{note_id}/edit").text
    assert app_client.get(f"/notes/{note_id}/export.md").status_code == 403


def test_owner_session_can_read_right_after_locking(auth_client, locked_note):
    """设锁的那次请求已经证明身份，不该立刻再问一遍密码。"""
    note_id, _token = locked_note
    page = auth_client.get(f"/notes/{note_id}")
    assert page.status_code == 200
    assert SECRET_TEXT in page.text
    assert "已锁定" in page.text


# ---------------------------------------------------------------------------
# 5. 解锁 / 重新锁定
# ---------------------------------------------------------------------------

def test_unlock_flow(app_client, locked_note):
    note_id, _token = locked_note
    token = csrf_of(app_client)

    wrong = app_client.post(f"/notes/{note_id}/unlock",
                            data={"_csrf": token, "password": "wrong"}, follow_redirects=False)
    assert wrong.status_code == 403
    assert "密码不对" in wrong.text

    ok = app_client.post(f"/notes/{note_id}/unlock",
                         data={"_csrf": token, "password": LOCK_PASSWORD, "next": f"/notes/{note_id}"},
                         follow_redirects=False)
    assert ok.status_code == 303
    assert SECRET_TEXT in app_client.get(f"/notes/{note_id}").text

    relock = app_client.post(f"/notes/{note_id}/relock",
                             data={"_csrf": token, "next": f"/notes/{note_id}"}, follow_redirects=False)
    assert relock.status_code == 303
    assert "这篇笔记已锁定" in app_client.get(f"/notes/{note_id}").text


def test_save_is_blocked_for_other_session(app_client, locked_note):
    note_id, _token = locked_note
    token = csrf_of(app_client)
    response = app_client.post(f"/notes/{note_id}",
                               data={"_csrf": token, "title": "被改的标题", "content": "偷偷改的正文"},
                               follow_redirects=False)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 6. 不可见面
# ---------------------------------------------------------------------------

def test_locked_note_hidden_from_search_blog_and_feeds(auth_client, locked_note):
    note_id, _token = locked_note
    # 搜索页会把关键词回显在 <title> 里，所以直接查底层检索，别拿整页 HTML 断言
    from app import search as search_mod

    with db_mod.db() as conn:
        hits = search_mod.search(conn, SECRET_TEXT)
    assert note_id not in [row["id"] for row in hits], "锁定笔记不该出现在检索结果里"
    assert "锁定测试笔记" not in auth_client.get(f"/search?q={SECRET_TEXT}").text
    assert "锁定测试笔记" not in auth_client.get("/blog").text
    assert "锁定测试笔记" not in auth_client.get("/feed.xml").text
    assert "锁定测试笔记" not in auth_client.get("/rss.xml").text
    assert "锁定测试笔记" not in auth_client.get("/sitemap.xml").text


def test_list_hides_summary_of_locked_note(auth_client, locked_note):
    page = auth_client.get("/notes")
    assert "已锁定 · 摘要不显示" in page.text
    assert SECRET_TEXT not in page.text, "列表页不能出现锁定笔记的正文片段"


def test_graph_skips_locked_notes(auth_client, locked_note):
    from app.services import graph

    with db_mod.db() as conn:
        notes, _links = graph._load(conn)
    assert "锁定测试笔记" not in [n.get("title") for n in notes]


# ---------------------------------------------------------------------------
# 7. 解除锁定
# ---------------------------------------------------------------------------

def test_remove_lock_restores_index(auth_client, db_conn, locked_note):
    note_id, token = locked_note
    response = auth_client.post(f"/notes/{note_id}/unlock/remove",
                                data={"_csrf": token, "password": LOCK_PASSWORD},
                                follow_redirects=False)
    assert response.status_code == 303
    note = repo.get_note(db_conn, note_id)
    assert note["locked"] == 0 and not note["lock_hash"]
    if search.FTS_ENABLED:
        row = db_conn.execute("SELECT 1 FROM notes_fts WHERE note_id = ?", (note_id,)).fetchone()
        assert row is not None, "解除锁定后正文要回到索引"


def test_remove_lock_needs_password(auth_client, db_conn, locked_note):
    note_id, token = locked_note
    response = auth_client.post(f"/notes/{note_id}/unlock/remove",
                                data={"_csrf": token, "password": "wrong"}, follow_redirects=False)
    assert response.status_code == 403
    assert repo.get_note(db_conn, note_id)["locked"] == 1, "密码不对不能解绑"
