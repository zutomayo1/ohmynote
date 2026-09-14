"""跨笔记待办聚合 / 批量操作扩展 / 批量补摘要。"""

from conftest import csrf_of


def _create(auth_client, csrf: str, title: str, content: str, **fields) -> int:
    data = {"_csrf": csrf, "title": title, "action": "save", "content": content}
    data.update(fields)
    response = auth_client.post("/notes", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].split("/")[-2])


# ---------------------------------------------------------------------------
# ① 跨笔记待办聚合
# ---------------------------------------------------------------------------

def test_collect_tasks_groups_and_excludes(client):
    from app import db, repo

    with db.db() as conn:
        # session 库共享：别的用例也会留下带任务清单的笔记，
        # 所以只按 note_id 认定本用例的笔记，并断言排除项不在结果里。
        open_note = repo.create_note(
            conn, title="待办笔记", content="- [ ] 甲\n- [x] 乙\n- [ ] 丙", status="saved")
        repo.create_note(conn, title="无任务", content="纯文本", status="saved")
        gone = repo.create_note(conn, title="回收站", content="- [ ] 不该出现", status="saved")
        repo.soft_delete(conn, gone["id"])
        archived = repo.create_note(conn, title="归档", content="- [ ] 也不该出现", status="saved")
        repo.set_archived(conn, archived["id"], True)

        groups = repo.collect_tasks(conn)
        ids = [g["note_id"] for g in groups]
        assert open_note["id"] in ids
        assert gone["id"] not in ids and archived["id"] not in ids
        group = next(g for g in groups if g["note_id"] == open_note["id"])
        assert group["open_count"] == 2 and group["done_count"] == 1 and group["total"] == 3
        # 默认只给未完成
        assert [t["text"] for t in group["tasks"]] == ["甲", "丙"]
        # index 与 task-toggle 口径一致（乙 是第 1 个）
        assert [t["index"] for t in group["tasks"]] == [0, 2]

        with_done = repo.collect_tasks(conn, include_done=True)
        group_with_done = next(g for g in with_done if g["note_id"] == open_note["id"])
        assert [t["text"] for t in group_with_done["tasks"]] == ["甲", "乙", "丙"]


def test_todos_page_and_toggle_roundtrip(auth_client, csrf):
    from app import db, repo

    note_id = _create(auth_client, csrf, "聚合测试", "- [ ] 第一件\n- [ ] 第二件")

    page = auth_client.get("/todos")
    assert page.status_code == 200
    assert "第一件" in page.text and "第二件" in page.text
    assert "todo-item__box" in page.text
    assert f'data-task-note="{note_id}"' in page.text
    assert "2</strong> 项待办" in page.text or ">2<" in page.text

    # 复用既有端点勾选第 0 项 → 回写原笔记
    token = csrf_of(auth_client, f"/notes/{note_id}")
    response = auth_client.post(
        f"/notes/{note_id}/task-toggle",
        data={"index": "0"},
        headers={"X-CSRF-Token": token},
    )
    assert response.json() == {"ok": True, "checked": True}
    with db.db() as conn:
        assert "- [x] 第一件" in repo.get_note(conn, note_id)["content"]

    # 未完成只剩 1 项
    page2 = auth_client.get("/todos")
    assert "第一件" not in page2.text
    # 显示已完成时能看到（划线态）
    page3 = auth_client.get("/todos?done=1")
    assert "第一件" in page3.text and "is-done" in page3.text


def test_todos_empty_state(auth_client):
    from app import db

    with db.db() as conn:
        conn.execute("DELETE FROM notes")
    page = auth_client.get("/todos")
    assert page.status_code == 200
    assert "没有待办事项" in page.text


# ---------------------------------------------------------------------------
# ④ 批量操作扩展
# ---------------------------------------------------------------------------

def test_batch_archive_and_category(auth_client, csrf):
    from app import db, repo

    a = _create(auth_client, csrf, "批量A", "内容A")
    b = _create(auth_client, csrf, "批量B", "内容B")
    token = csrf_of(auth_client, "/notes")

    def batch(action: str, **extra):
        data = {"_csrf": csrf, "action": action, "next": "/notes",
                "note_ids": [str(a), str(b)]}
        data.update(extra)
        return auth_client.post("/notes/batch", data=data, follow_redirects=False)

    response = batch("archive")
    assert response.status_code == 303, response.text
    with db.db() as conn:
        assert repo.get_note(conn, a)["is_archived"] is True
        assert repo.get_note(conn, b)["is_archived"] is True

    # 取消归档
    batch("unarchive")
    with db.db() as conn:
        assert repo.get_note(conn, a)["is_archived"] is False

    # 设为分类
    batch("set_category", action_category="技术")
    with db.db() as conn:
        assert repo.get_note(conn, a)["category"] == "技术"
        assert repo.get_note(conn, b)["category"] == "技术"


def test_batch_unknown_action_rejected(auth_client, csrf):
    response = auth_client.post(
        "/notes/batch",
        data={"_csrf": csrf, "action": "not-a-real-action", "next": "/notes"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_list_page_has_new_batch_options(auth_client):
    page = auth_client.get("/notes")
    assert 'value="archive"' in page.text
    assert 'value="set_category"' in page.text
    assert "data-batch-category" in page.text


# ---------------------------------------------------------------------------
# ⑦ 批量补摘要（在列表页的批量操作里，不在设置页）
# ---------------------------------------------------------------------------

def _batch_backfill(auth_client, csrf, *note_ids):
    data = {"_csrf": csrf, "action": "backfill_summary", "next": "/notes",
            "note_ids": [str(i) for i in note_ids]}
    return auth_client.post("/notes/batch", data=data, follow_redirects=False)


def test_backfill_is_not_in_settings_anymore(auth_client):
    """它是「对笔记的批量操作」，不该出现在设置页（配置项才属于设置）。"""
    page = auth_client.get("/settings")
    assert page.status_code == 200
    assert "批量补摘要" not in page.text
    assert "/settings/ai/backfill-summaries" not in page.text


def test_batch_backfill_summary_is_an_option_in_the_batch_bar(auth_client):
    page = auth_client.get("/notes")
    assert 'value="backfill_summary"' in page.text


def test_batch_backfill_summary_requires_ai(auth_client, csrf, monkeypatch):
    from app.services import ai

    monkeypatch.setattr(ai, "is_enabled", lambda: False)
    response = _batch_backfill(auth_client, csrf, 1)
    assert response.status_code == 303
    from urllib.parse import unquote_plus
    assert "还没配置" in unquote_plus(response.headers["location"])


def test_batch_backfill_summary_fills_selected_only(auth_client, csrf, monkeypatch):
    """只补选中的、且确实缺摘要的那几篇。"""
    from app import db, repo
    from app.services import ai

    missing = _create(auth_client, csrf, "缺摘要甲", "正文够长了，可以生成摘要的一段话。")
    missing2 = _create(auth_client, csrf, "缺摘要乙", "另一篇也够长了，可以生成摘要。")
    has_it = _create(auth_client, csrf, "已有摘要丙", "这篇的摘要是我自己写的。")
    untouched = _create(auth_client, csrf, "没选中丁", "没选中的不该被动。")
    with db.db() as conn:
        repo.update_note(conn, has_it, summary="我亲手写的摘要")

    monkeypatch.setattr(ai, "is_enabled", lambda: True)
    monkeypatch.setattr(ai, "summarize",
                        lambda title, content, conn=None: f"AI 摘要：{title}")

    response = _batch_backfill(auth_client, csrf, missing, missing2, has_it)
    assert response.status_code == 303
    from urllib.parse import unquote_plus
    msg = unquote_plus(response.headers["location"])
    assert "已补 2 篇摘要" in msg
    assert "1 篇本来就有摘要" in msg

    with db.db() as conn:
        assert repo.get_note(conn, missing)["summary"] == "AI 摘要：缺摘要甲"
        assert repo.get_note(conn, missing2)["summary"] == "AI 摘要：缺摘要乙"
        assert repo.get_note(conn, has_it)["summary"] == "我亲手写的摘要"
        assert repo.get_note(conn, untouched)["summary"] != "AI 摘要：没选中丁"


def test_batch_backfill_summary_caps_and_reports_remaining(auth_client, csrf, monkeypatch):
    """单次只补 N 篇，剩下的如实告诉用户「可以再点一次」。"""
    from app import db, repo
    from app.services import ai

    monkeypatch.setattr(ai, "BACKFILL_LIMIT", 2)
    ids = [_create(auth_client, csrf, f"限量补摘要{i}", f"第 {i} 篇的正文够长了。") for i in range(3)]
    monkeypatch.setattr(ai, "is_enabled", lambda: True)
    monkeypatch.setattr(ai, "summarize", lambda title, content, conn=None: "AI 摘要")

    response = _batch_backfill(auth_client, csrf, *ids)
    from urllib.parse import unquote_plus
    msg = unquote_plus(response.headers["location"])
    assert "已补 2 篇摘要" in msg, msg
    assert "还剩 1 篇" in msg, msg

    with db.db() as conn:
        filled = sum(1 for i in ids if repo.get_note(conn, i)["summary"] == "AI 摘要")
    assert filled == 2


def test_batch_backfill_summary_counts_failures(auth_client, csrf, monkeypatch):
    from app.services import ai

    note_id = _create(auth_client, csrf, "补摘要会失败", "正文够长了，可以生成摘要。")
    monkeypatch.setattr(ai, "is_enabled", lambda: True)

    def boom(title, content, conn=None):
        raise ai.AIError("模型挂了")

    monkeypatch.setattr(ai, "summarize", boom)
    response = _batch_backfill(auth_client, csrf, note_id)
    from urllib.parse import unquote_plus
    msg = unquote_plus(response.headers["location"])
    assert "1 篇失败" in msg, msg


def test_summary_is_missing_semantics():
    """「缺摘要」的判定：空、或仍等于自动摘录，都算缺。"""
    from app.markdown_render import make_excerpt
    from app.services.ai import summary_is_missing

    content = "写点够长的正文，好让自动摘要有个东西可摘。"
    assert summary_is_missing({"summary": "", "content": content}) is True
    assert summary_is_missing({"summary": make_excerpt(content), "content": content}) is True
    assert summary_is_missing({"summary": "用户自己写的摘要", "content": content}) is False


def test_backfill_service_respects_note_ids(client, monkeypatch):
    """服务层：给 note_ids 就只在这几篇里找；不给就是全库。"""
    from app import db, repo
    from app.services import ai

    _ = client  # 借 session 客户端确保建表
    with db.db() as conn:
        a = repo.create_note(conn, title="服务层甲", content="正文够长，可以生成摘要的一段话。")
        b = repo.create_note(conn, title="服务层乙", content="正文也够长，可以生成摘要的一段话。")
        monkeypatch.setattr(ai, "is_enabled", lambda: True)
        monkeypatch.setattr(ai, "summarize", lambda title, content, conn=None: "服务层摘要")

        result = ai.backfill_summaries(conn, note_ids=[a["id"]])
        assert result["done"] == 1
        assert repo.get_note(conn, a["id"])["summary"] == "服务层摘要"
        assert repo.get_note(conn, b["id"])["summary"] != "服务层摘要"

        # 不传 note_ids：全库范围（此时乙也该被补上）
        second = ai.backfill_summaries(conn, note_ids=None, limit=50)
        assert repo.get_note(conn, b["id"])["summary"] == "服务层摘要"
        assert second["done"] >= 1
