"""批量操作（POST /notes/batch）与卡片历史入口的测试。

约定：整个测试会话共用一个临时数据库，且本文件按字母序排在 test_smoke.py 之前，
所以自己创建的笔记一定要在建好后清掉（purge），不能把别人的用例带下水。
"""

from __future__ import annotations

import re

import pytest

from conftest import PASSWORD, csrf_of  # noqa: F401  （PASSWORD 供别人参考，保持导入风格一致）

# 明显不存在的 id：确定性跳过
MISSING_ID = 987654321


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _note_state(note_id: int) -> dict:
    """直接从数据库读一篇笔记（含回收站），断言用。"""
    from app import db as db_mod
    from app import repo

    with db_mod.db() as conn:
        note = repo.get_note(conn, note_id, include_deleted=True)
    assert note is not None, f"笔记 {note_id} 应该存在"
    return note


def _batch(client, csrf: str, ids, action: str, *, tag: str = "", next_url: str = "/notes"):
    """提交一次批量操作，返回未跟随跳转的响应。"""
    data: dict = {"_csrf": csrf, "action": action, "next": next_url}
    if tag:
        data["tag"] = tag          # 之前漏了这行，导致「没有填写标签名」被拒
    data["note_ids"] = [str(item) for item in ids]
    return client.post("/notes/batch", data=data, follow_redirects=False)


def _flash_text(client, response) -> str:
    """跟随 303 回到列表页，返回页面正文（用来断言 flash）。"""
    assert response.status_code == 303, response.text
    page = client.get(response.headers["location"])
    assert page.status_code == 200, page.text
    return page.text


@pytest.fixture()
def make_note(auth_client, csrf):
    """按需造笔记，测试结束统一彻底删除（不污染后续用例）。"""
    created: list[int] = []

    def _make(title: str, **extra) -> int:
        data = {"_csrf": csrf, "title": title, "content": "批量测试正文", "action": "save"}
        data.update(extra)
        response = auth_client.post("/notes", data=data, follow_redirects=False)
        assert response.status_code == 303, response.text
        match = re.search(r"/notes/(\d+)", response.headers["location"])
        assert match, response.headers["location"]
        note_id = int(match.group(1))
        created.append(note_id)
        return note_id

    yield _make

    for note_id in created:
        auth_client.post(
            f"/notes/{note_id}/purge", data={"_csrf": csrf}, follow_redirects=False
        )


# ---------------------------------------------------------------------------
# 9 个 action 各一条
# ---------------------------------------------------------------------------
def test_action_add_tag(auth_client, csrf, make_note):
    first = make_note("批测加标签 A")
    second = make_note("批测加标签 B")
    response = _batch(auth_client, csrf, [first, second], "add_tag", tag="批量标签")
    text = _flash_text(auth_client, response)
    found = re.findall(r"已处理[^<\"]{0,40}", text)
    assert "已处理 2 篇" in text
    for note_id in (first, second):
        assert "批量标签" in _note_state(note_id)["tags"]


def test_action_remove_tag(auth_client, csrf, make_note):
    note_id = make_note("批测删标签", tags="批量标签, 保留标签")
    assert "批量标签" in _note_state(note_id)["tags"]
    response = _batch(auth_client, csrf, [note_id], "remove_tag", tag="批量标签")
    text = _flash_text(auth_client, response)
    assert "已处理 1 篇" in text
    tags = _note_state(note_id)["tags"]
    assert "批量标签" not in tags
    assert "保留标签" in tags


def test_action_publish(auth_client, csrf, make_note):
    note_id = make_note("批测公开")
    response = _batch(auth_client, csrf, [note_id], "publish")
    text = _flash_text(auth_client, response)
    assert "已处理 1 篇" in text
    note = _note_state(note_id)
    assert note["is_public"] is True
    assert note["status"] == "saved"
    assert note["published_at"]


def test_action_unpublish(auth_client, csrf, make_note):
    note_id = make_note("批测取消公开", is_public="1")
    assert _note_state(note_id)["is_public"] is True
    response = _batch(auth_client, csrf, [note_id], "unpublish")
    assert "已处理 1 篇" in _flash_text(auth_client, response)
    assert _note_state(note_id)["is_public"] is False


def test_action_pin(auth_client, csrf, make_note):
    note_id = make_note("批测置顶")
    response = _batch(auth_client, csrf, [note_id], "pin")
    assert "已处理 1 篇" in _flash_text(auth_client, response)
    assert _note_state(note_id)["is_pinned"] is True


def test_action_unpin(auth_client, csrf, make_note):
    note_id = make_note("批测取消置顶", is_pinned="1")
    assert _note_state(note_id)["is_pinned"] is True
    response = _batch(auth_client, csrf, [note_id], "unpin")
    assert "已处理 1 篇" in _flash_text(auth_client, response)
    assert _note_state(note_id)["is_pinned"] is False


def test_action_star(auth_client, csrf, make_note):
    note_id = make_note("批测星标")
    response = _batch(auth_client, csrf, [note_id], "star")
    assert "已处理 1 篇" in _flash_text(auth_client, response)
    assert _note_state(note_id)["is_starred"] is True


def test_action_unstar(auth_client, csrf, make_note):
    note_id = make_note("批测取消星标", is_starred="1")
    assert _note_state(note_id)["is_starred"] is True
    response = _batch(auth_client, csrf, [note_id], "unstar")
    assert "已处理 1 篇" in _flash_text(auth_client, response)
    assert _note_state(note_id)["is_starred"] is False


def test_action_trash(auth_client, csrf, make_note):
    note_id = make_note("批测移入回收站")
    response = _batch(auth_client, csrf, [note_id], "trash")
    assert "已处理 1 篇" in _flash_text(auth_client, response)
    assert _note_state(note_id)["deleted_at"]
    # 移入回收站后详情页应 404
    assert auth_client.get(f"/notes/{note_id}").status_code == 404


# ---------------------------------------------------------------------------
# 健壮性
# ---------------------------------------------------------------------------
def test_empty_note_ids_does_not_crash(auth_client, csrf):
    response = _batch(auth_client, csrf, [], "pin")
    text = _flash_text(auth_client, response)
    assert "已处理 0 篇" in text


def test_single_value_field_is_accepted(auth_client, csrf, make_note):
    """只传一个 note_ids（而不是重复字段）也要能处理。"""
    note_id = make_note("批测单值")
    response = auth_client.post(
        "/notes/batch",
        data={"_csrf": csrf, "action": "star", "note_ids": str(note_id), "next": "/notes"},
        follow_redirects=False,
    )
    assert "已处理 1 篇" in _flash_text(auth_client, response)
    assert _note_state(note_id)["is_starred"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "abc",  # 非数字
        "12.5",  # 小数
        "",  # 空串
        "-3",  # 负数
        "9223372036854775808",  # 2^63（越界 1）
        "99999999999999999999999",  # 超长数字
        "²",  # isdigit() 为真但 int() 会炸的字符
    ],
)
def test_bad_note_ids_never_500(auth_client, csrf, make_note, bad):
    note_id = make_note("批测脏数据")
    response = _batch(auth_client, csrf, [note_id, bad], "pin")
    text = _flash_text(auth_client, response)
    assert "已处理 1 篇" in text
    assert "跳过 1 篇" in text
    assert _note_state(note_id)["is_pinned"] is True


def test_duplicate_ids_are_dropped(auth_client, csrf, make_note):
    note_id = make_note("批测重复 id")
    response = _batch(auth_client, csrf, [note_id, note_id, note_id], "star")
    text = _flash_text(auth_client, response)
    assert "已处理 1 篇" in text
    assert "跳过 2 篇" in text


def test_missing_id_is_skipped(auth_client, csrf, make_note):
    note_id = make_note("批测存在")
    response = _batch(auth_client, csrf, [note_id, MISSING_ID], "pin")
    text = _flash_text(auth_client, response)
    assert "已处理 1 篇" in text
    assert "跳过 1 篇" in text


def test_tag_action_without_tag_is_reported(auth_client, csrf, make_note):
    note_id = make_note("批测空标签")
    response = _batch(auth_client, csrf, [note_id], "add_tag", tag="")
    assert response.status_code == 303
    page = auth_client.get(response.headers["location"])
    assert "标签" in page.text  # 给出「没有填写标签名」的提示
    assert _note_state(note_id)["tags"] == []


def test_batch_requires_login(client):
    fresh = client.__class__(client.app)
    response = fresh.post(
        "/notes/batch", data={"action": "pin", "note_ids": "1"}, follow_redirects=False
    )
    assert response.status_code in (303, 401, 403)
    if response.status_code == 303:
        assert "/login" in response.headers.get("location", "")


# ---------------------------------------------------------------------------
# 前端契约
# ---------------------------------------------------------------------------
def test_batch_redirects_to_next(auth_client, csrf, make_note):
    note_id = make_note("批测 next")
    response = _batch(auth_client, csrf, [note_id], "star", next_url="/notes?fav=starred")
    assert response.status_code == 303
    assert "/notes?fav=starred" in response.headers["location"]


def test_dashboard_renders_batch_ui(auth_client, make_note):
    note_id = make_note("批测卡片渲染")
    page = auth_client.get("/notes")
    assert page.status_code == 200
    # 批量表单与提交目标
    assert 'id="batch-form"' in page.text
    assert 'action="/notes/batch"' in page.text
    # 卡片里的复选框（name=note_ids）与历史入口
    assert f'name="note_ids" value="{note_id}"' in page.text
    assert f'href="/notes/{note_id}/versions"' in page.text
    assert 'name="action"' in page.text
    # 无 JS 也需要能提交
    assert 'method="post"' in page.text


def test_batch_form_next_carries_filters(auth_client, make_note):
    make_note("批测筛选带回", tags="批测标签")
    page = auth_client.get(
        "/notes", params={"q": "批测", "status": "saved", "sort": "created", "page": "1"}
    )
    assert page.status_code == 200
    # 路由用 urlencode 拼好的 next 要出现在隐藏字段里（& 被 HTML 转义成 &amp;）
    assert 'name="next"' in page.text
    assert "q=%E6%89%B9%E6%B5%8B" in page.text
    assert "sort=created" in page.text


def test_batch_script_is_included(auth_client):
    page = auth_client.get("/notes")
    assert "/static/js/batch.js" in page.text


def test_tags_and_search_pages_render_without_checkboxes(auth_client, make_note):
    make_note("批测标签页", tags="批测标签")
    tags_page = auth_client.get("/tags", params={"tag": "批测标签"})
    assert tags_page.status_code == 200
    search_page = auth_client.get("/search", params={"q": "批测标签"})
    assert search_page.status_code == 200
    # 这两个页面不渲染批量复选框，避免出现没有归属表单的孤儿复选框
    assert 'name="note_ids"' not in tags_page.text
    assert 'name="note_ids"' not in search_page.text
