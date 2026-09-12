"""F · 批量跨页勾选（localStorage 持久化）的测试。

前端逻辑（localStorage / 提交前注入 hidden input）没法在 pytest 里跑，所以这里测能测的：
脚本引入 + 关键标识、页面上有计数元素与「清空选择」按钮、服务端多 note_ids 的兼容性、
all=1 路径没被破坏。约定同 test_batch.py：整个会话共用一个临时库，本文件造的笔记
测试结束一律 purge，不把别人的用例带下水。
"""

from __future__ import annotations

import re

import pytest

from conftest import csrf_of  # noqa: F401  （保持和 test_batch.py 一致的导入风格）


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


def _flash_text(client, response) -> str:
    """跟随 303 回到列表页，返回页面正文（用来断言 flash）。"""
    assert response.status_code == 303, response.text
    page = client.get(response.headers["location"])
    assert page.status_code == 200, page.text
    return page.text


def _batch(client, csrf: str, ids, action: str, *, tag: str = "", next_url: str = "/notes"):
    """提交一次批量操作，返回未跟随跳转的响应。"""
    data: dict = {"_csrf": csrf, "action": action, "next": next_url}
    if tag:
        data["tag"] = tag
    data["note_ids"] = [str(item) for item in ids]
    return client.post("/notes/batch", data=data, follow_redirects=False)


@pytest.fixture()
def make_note(auth_client, csrf):
    """按需造笔记，测试结束统一彻底删除（不污染后续用例）。"""
    created: list[int] = []

    def _make(title: str, **extra) -> int:
        data = {"_csrf": csrf, "title": title, "content": "F 跨页勾选正文", "action": "save"}
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
# 1. 脚本被引入，且含持久化的关键标识
# ---------------------------------------------------------------------------
def test_batch_script_has_persistence_markers(auth_client, make_note):
    make_note("F 脚本标识")
    page = auth_client.get("/notes")
    assert page.status_code == 200
    assert "/static/js/batch.js" in page.text

    js = auth_client.get("/static/js/batch.js")
    assert js.status_code == 200, js.text
    body = js.text
    # localStorage + 带站点/路径的 key（跨页读同一份存储）
    assert "localStorage" in body
    assert "inknote.batch.v1" in body
    # 「清空选择」按钮的处理 + 提交成功后清空的标记
    assert "清空选择" in body
    assert "submitted" in body
    # 提交前注入 hidden input 的逻辑（name=note_ids）
    assert "createElement" in body
    assert "note_ids" in body
    assert "hidden" in body
    # 勾了「全部筛选结果（all=1）」时不注入 id 的分支
    assert "allFilteredOn" in body


# ---------------------------------------------------------------------------
# 2. 列表页有计数元素 + 「清空选择」按钮，且原生表单仍可用
# ---------------------------------------------------------------------------
def test_dashboard_has_count_and_clear_button(auth_client, make_note):
    make_note("F 计数与清空")
    page = auth_client.get("/notes")
    assert page.status_code == 200
    assert 'data-batch-count' in page.text       # 操作条计数元素
    assert "已选 0 项" in page.text              # 无 JS / 初始文案
    assert 'data-batch-clear' in page.text       # 清空选择按钮
    assert "清空选择" in page.text
    # 没有 JS 也要能原生提交：复选框 name=note_ids + POST form
    assert 'name="note_ids"' in page.text
    assert 'method="post"' in page.text
    assert 'action="/notes/batch"' in page.text


# ---------------------------------------------------------------------------
# 3. 服务端兼容：一次带多个（跨页模拟）note_ids，只动这些
# ---------------------------------------------------------------------------
def test_multiple_note_ids_only_touch_selected(auth_client, csrf, make_note):
    first = make_note("F 跨页目标 A")
    second = make_note("F 跨页目标 B")
    third = make_note("F 跨页不动 C")

    response = _batch(auth_client, csrf, [first, second], "pin")
    text = _flash_text(auth_client, response)
    assert "已处理 2 篇" in text

    assert _note_state(first)["is_pinned"] is True
    assert _note_state(second)["is_pinned"] is True
    assert _note_state(third)["is_pinned"] is False


# ---------------------------------------------------------------------------
# 4. all=1 路径没被破坏：仍要求 confirm_all
# ---------------------------------------------------------------------------
def test_all_without_confirm_is_rejected(auth_client, csrf, make_note):
    ids = [make_note(f"F all 防呆 {i}") for i in range(2)]

    response = auth_client.post(
        "/notes/batch",
        data={"_csrf": csrf, "action": "star", "all": "1", "next": "/notes"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    text = _flash_text(auth_client, response)
    assert "这会处理全部笔记" in text
    assert "确认" in text
    for note_id in ids:
        assert _note_state(note_id)["is_starred"] is False  # 零改动


# ---------------------------------------------------------------------------
# 5. all=1 只按筛选走：就算表单里混进 note_ids 也不会同时用 id
# ---------------------------------------------------------------------------
def test_all_ignores_explicit_note_ids(auth_client, csrf, make_note):
    hit = make_note("F all 命中", tags="F筛选标签")
    miss = make_note("F all 不命中")

    response = auth_client.post(
        "/notes/batch",
        data={
            "_csrf": csrf,
            "action": "star",
            "all": "1",
            "tag": "F筛选标签",
            "note_ids": [str(miss)],  # 故意携带一个不符合筛选的 id
            "next": "/notes",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    text = _flash_text(auth_client, response)
    assert "已处理 1 篇" in text
    assert _note_state(hit)["is_starred"] is True
    assert _note_state(miss)["is_starred"] is False
