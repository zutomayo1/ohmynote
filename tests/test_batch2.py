"""A5 批量增强：`POST /notes/batch` 的 all=1（选中当前筛选出的全部 N 篇）。

和 test_batch.py 一样的约定：整个测试会话共用一个临时数据库，本文件自己造的笔记
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


def _count_notes() -> int:
    """库里（不含回收站）的笔记总数。"""
    from app import db as db_mod
    from app import repo

    with db_mod.db() as conn:
        _notes, total = repo.list_notes(conn, page=1, per_page=1)
    return int(total)


def _all_states() -> dict:
    """全库状态快照，用来断言「被拒绝时零改动」。"""
    from app import db as db_mod
    from app import repo

    with db_mod.db() as conn:
        notes, _total = repo.list_notes(conn, page=1, per_page=1000)
    return {
        note["id"]: (
            note["is_starred"],
            note["is_pinned"],
            note["is_public"],
            note["status"],
            note["deleted_at"],
        )
        for note in notes
    }


def _post_all(
    client,
    csrf: str,
    action: str,
    *,
    tag: str = "",
    confirm: bool = True,
    extra: dict | None = None,
):
    """提交一次「全部筛选结果」批量操作（all=1）。"""
    data: dict = {"_csrf": csrf, "action": action, "all": "1", "next": "/notes"}
    if tag:
        data["tag"] = tag
    if confirm:
        data["confirm_all"] = "1"
    if extra:
        data.update(extra)
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
        data = {"_csrf": csrf, "title": title, "content": "A5 批量正文", "action": "save"}
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
# 1. all=1 + 筛选条件：只动符合条件的
# ---------------------------------------------------------------------------
def test_all_with_tag_filter_only_touches_tagged(auth_client, csrf, make_note):
    tagged = [make_note(f"A5 有标签 {i}", tags="A5筛选标签") for i in range(3)]
    plain = [make_note(f"A5 无标签 {i}") for i in range(2)]

    response = _post_all(auth_client, csrf, "star", tag="A5筛选标签")
    text = _flash_text(auth_client, response)
    assert "已处理 3 篇" in text

    for note_id in tagged:
        assert _note_state(note_id)["is_starred"] is True
    for note_id in plain:
        assert _note_state(note_id)["is_starred"] is False


# ---------------------------------------------------------------------------
# 2. all=1 但没确认：被拒绝 + 零改动
# ---------------------------------------------------------------------------
def test_all_without_confirm_is_rejected_and_changes_nothing(auth_client, csrf, make_note):
    ids = [make_note(f"A5 防呆 {i}") for i in range(3)]
    before = _all_states()

    response = _post_all(auth_client, csrf, "star", confirm=False)
    assert response.status_code == 303
    text = _flash_text(auth_client, response)
    assert "这会处理全部笔记" in text
    assert "确认" in text

    assert _all_states() == before  # 一篇都没动
    for note_id in ids:
        assert _note_state(note_id)["is_starred"] is False


# ---------------------------------------------------------------------------
# 3. 没有任何筛选条件 + confirm_all=1：处理全部
# ---------------------------------------------------------------------------
def test_all_without_filters_processes_whole_library(auth_client, csrf, make_note):
    make_note("A5 全库 1")
    make_note("A5 全库 2")
    total = _count_notes()
    assert total >= 2

    response = _post_all(auth_client, csrf, "unpin")
    text = _flash_text(auth_client, response)
    assert f"已处理 {total} 篇" in text
    assert "跳过 0 篇" in text


# ---------------------------------------------------------------------------
# 4. 超过上限：用 monkeypatch 把 500 调成 3
# ---------------------------------------------------------------------------
def test_all_enforces_upper_limit(auth_client, csrf, make_note, monkeypatch):
    from app.routers.notes import batch as notes_router

    monkeypatch.setattr(notes_router, "BATCH_ALL_LIMIT", 3)
    ids = [make_note(f"A5 上限 {i}", tags="A5上限标签") for i in range(5)]

    response = _post_all(auth_client, csrf, "star", tag="A5上限标签")
    text = _flash_text(auth_client, response)
    assert "已处理 3 篇" in text
    assert "另有 2 篇超出单次上限 3" in text

    starred = [note_id for note_id in ids if _note_state(note_id)["is_starred"]]
    assert len(starred) == 3


# ---------------------------------------------------------------------------
# 5. 列表页：渲染筛选总数 N + 复选框 + 筛选参数隐藏域
# ---------------------------------------------------------------------------
def test_dashboard_renders_select_all_with_filter_total(auth_client, csrf, make_note):
    from app.config import settings

    wanted = settings.per_page + 2  # 超过一页，证明 N 是筛选总数而不是本页数量
    for i in range(wanted):
        make_note(f"A5 渲染 {i}", tags="A5渲染标签")

    page = auth_client.get("/notes", params={"tag": "A5渲染标签"})
    assert page.status_code == 200
    assert "选中全部筛选结果" in page.text
    assert 'data-batch-select-all' in page.text
    assert 'name="all"' in page.text
    assert f"全部 {wanted} 篇" in page.text
    # 服务端把筛选参数渲染成隐藏域，勾上复选框后原生提交也能带上
    assert 'name="tag" value="A5渲染标签"' in page.text
