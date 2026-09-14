"""任务清单点击回写：渲染打标 / 切换函数 / HTTP 端点 / 版本历史兜底。"""

from conftest import csrf_of


def _create_note(auth_client, csrf: str, content: str) -> int:
    response = auth_client.post(
        "/notes",
        data={"_csrf": csrf, "title": "任务清单测试", "action": "save", "content": content},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    # 303 落到 /notes/{id}/edit
    note_id = response.headers["location"].split("/")[-2]
    return int(note_id)


# ---------------------------------------------------------------------------
# 渲染打标
# ---------------------------------------------------------------------------

def test_render_marks_task_indexes_sequentially():
    import re

    from app import markdown_render as mr

    rendered = mr.render("- [ ] 甲\n- [x] 乙\n\n- [ ] 丙", title="t")
    indexes = re.findall(r'data-task-index="(\d+)"', rendered.html)
    assert indexes == ["0", "1", "2"]


def test_render_marks_checked_boxes_too():
    import re

    from app import markdown_render as mr

    rendered = mr.render("- [x] 只有勾选的", title="t")
    assert 'data-task-index="0"' in rendered.html
    assert "checked" in rendered.html


# ---------------------------------------------------------------------------
# toggle_task_item
# ---------------------------------------------------------------------------

def test_toggle_task_item_check_and_uncheck():
    from app import markdown_render as mr

    content = "- [ ] 甲\n- [x] 乙"
    new_content, checked = mr.toggle_task_item(content, 0)
    assert checked is True and "- [x] 甲" in new_content and "- [x] 乙" in new_content

    new_content, checked = mr.toggle_task_item(content, 1)
    assert checked is False and "- [ ] 乙" in new_content


def test_toggle_task_item_skips_code_blocks():
    from app import markdown_render as mr

    content = "```\n- [ ] 代码里的不算\n```\n\n- [ ] 正文里的"
    result = mr.toggle_task_item(content, 0)
    assert result is not None
    new_content, checked = result
    # 切的是正文那个，代码块原样
    assert checked is True
    assert "- [ ] 代码里的不算" in new_content
    assert "- [x] 正文里的" in new_content


def test_toggle_task_item_out_of_range():
    from app import markdown_render as mr

    assert mr.toggle_task_item("- [ ] 甲", 5) is None
    assert mr.toggle_task_item("没有任务标记", 0) is None


def test_toggle_task_item_keeps_uppercase_x():
    from app import markdown_render as mr

    content = "- [X] 大写勾选"
    _, checked = mr.toggle_task_item(content, 0)
    assert checked is False  # X → 空格


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------

def test_task_toggle_roundtrip(auth_client, csrf):
    note_id = _create_note(auth_client, csrf, "# 计划\n\n- [ ] 第一步\n- [ ] 第二步")

    response = auth_client.post(
        f"/notes/{note_id}/task-toggle",
        data={"index": "0"},
        headers={"X-CSRF-Token": csrf_of(auth_client, f"/notes/{note_id}")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True and payload["checked"] is True

    # 数据库里的正文真的变了
    from app import db, repo

    with db.db() as conn:
        note = repo.get_note(conn, note_id)
        assert "- [x] 第一步" in note["content"]
        assert "- [ ] 第二步" in note["content"]

        # 版本历史兜底：改动前后的版本都在
        versions = repo.list_versions(conn, note_id)
        assert len(versions) >= 1


def test_task_toggle_uncheck(auth_client, csrf):
    note_id = _create_note(auth_client, csrf, "- [x] 已完成")

    response = auth_client.post(
        f"/notes/{note_id}/task-toggle",
        data={"index": "0"},
        headers={"X-CSRF-Token": csrf_of(auth_client, f"/notes/{note_id}")},
    )
    assert response.json() == {"ok": True, "checked": False}

    from app import db, repo

    with db.db() as conn:
        assert "- [ ] 已完成" in repo.get_note(conn, note_id)["content"]


def test_task_toggle_bad_index(auth_client, csrf):
    note_id = _create_note(auth_client, csrf, "- [ ] 甲")

    out_of_range = auth_client.post(
        f"/notes/{note_id}/task-toggle",
        data={"index": "99"},
        headers={"X-CSRF-Token": csrf_of(auth_client, f"/notes/{note_id}")},
    )
    assert out_of_range.status_code == 400

    negative = auth_client.post(
        f"/notes/{note_id}/task-toggle",
        data={"index": "-1"},
        headers={"X-CSRF-Token": csrf_of(auth_client, f"/notes/{note_id}")},
    )
    assert negative.status_code == 400


def test_task_toggle_requires_auth(client):
    response = client.post("/notes/1/task-toggle", data={"index": "0"})
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# 页面接线
# ---------------------------------------------------------------------------

def test_detail_page_has_task_hook(auth_client, csrf):
    note_id = _create_note(auth_client, csrf, "- [ ] 甲")
    page = auth_client.get(f"/notes/{note_id}").text
    assert 'data-task-note="' in page
    assert 'data-task-index="0"' in page


def test_blog_post_has_no_task_hook(auth_client, csrf):
    """公开博客是匿名橱窗：不带回写钩子，访客点了也不会尝试写库。"""
    from app import db, repo

    with db.db() as conn:
        note = repo.create_note(
            conn, title="任务清单测试", content="- [ ] 甲",
            is_public=True, slug="task-hook-test", status="saved",
        )
        note_id = note["id"]

    post = auth_client.get("/blog/task-hook-test").text
    assert 'data-task-note="' not in post
    # 渲染出来的复选框仍在（只是不可点）
    assert "task-list-item" in post
