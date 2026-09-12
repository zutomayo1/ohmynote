"""回归测试：页面渲染出来的每个 POST 表单，CSRF 字段都必须是「有值」的。

为什么单独一条：
    Jinja 的宏（`{% from '_macros.html' import ... %}`）默认**看不到调用方的上下文**，
    所以宏里的 `{{ csrf }}` 会渲染成空字符串。之前所有测试都是用 httpx 显式带 token
    提交的，恰好绕过了「页面里渲染出来的表单」，于是卡片上的置顶/星标/移入回收站
    一直点不动（提交后 403 表单已过期）却没人发现。
    这条测试直接检查渲染结果：任何 name="_csrf" 的隐藏域都不能为空。
"""

from __future__ import annotations

import re

import pytest

# 登录后能看到、且带 POST 表单的页面
PAGES = ["/notes", "/trash", "/tags", "/templates", "/settings", "/images", "/backup"]


@pytest.fixture()
def make_note(auth_client, csrf):
    """临时造笔记，用完彻底删掉（这个 fixture 在 test_batch.py 里也有一份，各自独立）。"""
    created: list[int] = []

    def _make(title: str, **extra) -> int:
        data = {"_csrf": csrf, "title": title, "content": "回归测试正文", "action": "save"}
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
        auth_client.post(f"/notes/{note_id}/purge", data={"_csrf": csrf}, follow_redirects=False)


@pytest.mark.parametrize("path", PAGES)
def test_every_rendered_csrf_field_has_value(auth_client, path, make_note):
    page = auth_client.get(path)
    assert page.status_code == 200, f"{path} 打不开：{page.status_code}"

    values = re.findall(r'name="_csrf"[^>]*value="([^"]*)"', page.text)
    if values:
        empties = [value for value in values if not value.strip()]
        assert not empties, f"{path} 上有 {len(empties)} 个空的 _csrf（宏拿不到上下文？）"

    # 反过来的写法（value 在 name 前面）也要覆盖
    values2 = re.findall(r'value="([^"]*)"[^>]*name="_csrf"', page.text)
    empties2 = [value for value in values2 if not value.strip()]
    assert not empties2, f"{path} 上有 {len(empties2)} 个空的 _csrf"


def test_card_action_forms_are_submittable(auth_client, make_note):
    """卡片上的置顶/星标/公开/删除四个表单，都要带非空 _csrf 和正确的 action。"""
    make_note("回归用笔记")
    page = auth_client.get("/notes")
    assert page.status_code == 200

    forms = re.findall(r'<form[^>]*action="([^"]*)"[^>]*>(.*?)</form>', page.text, re.S)
    card_forms = [
        (action, body)
        for action, body in forms
        if action.endswith(("/flag", "/delete"))
    ]
    assert card_forms, "列表页卡片上应该有置顶/星标/公开/删除这些表单"

    for action, body in card_forms:
        token = re.search(r'name="_csrf"[^>]*value="([^"]*)"', body)
        assert token, f"{action} 表单里没有 _csrf 字段"
        assert token.group(1).strip(), f"{action} 表单的 _csrf 是空的（宏里拿不到 csrf）"


def test_card_delete_button_actually_moves_note_to_trash(auth_client, csrf, make_note):
    """端到端：用页面上真实渲染出来的 token 去删，必须 303 而不是 403。"""
    note_id = make_note("回归用待删笔记")
    page = auth_client.get("/notes")

    match = re.search(
        r'<form[^>]*action="' + re.escape(f"/notes/{note_id}/delete") + r'"[^>]*>(.*?)</form>',
        page.text,
        re.S,
    )
    assert match, "找不到这篇笔记的删除表单"
    token = re.search(r'name="_csrf"[^>]*value="([^"]*)"', match.group(1)).group(1)

    response = auth_client.post(
        f"/notes/{note_id}/delete",
        data={"_csrf": token, "next": "/notes"},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"应该 303，实际 {response.status_code}"

    from app import db as db_mod
    from app import repo

    with db_mod.db() as conn:
        note = repo.get_note(conn, note_id, include_deleted=True)
    assert note is not None and note["deleted_at"], "笔记应该被移入回收站"
