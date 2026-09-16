"""置顶顺序拖拽：repo.reorder_pinned + POST /notes/reorder + 排序口径。

约定（见 conftest.py 与 tests/test_agent.py 的 db_conn fixture）：
- db_conn 的未提交事务对应用的请求连接不可见 → 路由用例断言数据前先 commit。
- 禁止断言全局聚合：只用本文件造的笔记（专属前缀），按主键定位。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import repo
from app.main import app
from tests.test_agent import db_conn  # noqa: F401  fixture 随模块导入

PREFIX = "排序E2E-"


def _make(conn, title: str) -> dict:
    return repo.create_note(conn, title=PREFIX + title, content="内容")


def _pin(conn, note_id: int, order: int | None = None) -> None:
    if order is None:
        conn.execute("UPDATE notes SET is_pinned = 1 WHERE id = ?", (note_id,))
    else:
        conn.execute(
            "UPDATE notes SET is_pinned = 1, sort_order = ? WHERE id = ?",
            (order, note_id),
        )
    conn.commit()


def _order_in_list(conn, sort: str = "updated") -> list[int]:
    notes, _total = repo.list_notes(conn, sort=sort, page=1, per_page=50)
    return [note["id"] for note in notes]


# ---------------------------------------------------------------------------
# repo.reorder_pinned
# ---------------------------------------------------------------------------
def test_reorder_pinned_only_touches_pinned(db_conn):
    a = _make(db_conn, "甲")
    b = _make(db_conn, "乙")
    c = _make(db_conn, "丙")
    _pin(db_conn, a["id"])
    _pin(db_conn, b["id"])

    # 传入顺序里混着非置顶的丙：必须被跳过，不能给它写 sort_order
    updated = repo.reorder_pinned(db_conn, [c["id"], b["id"], a["id"]])
    assert updated == 2

    rows = {
        row["id"]: row["sort_order"]
        for row in db_conn.execute("SELECT id, sort_order FROM notes")
    }
    assert rows[b["id"]] == 0 and rows[a["id"]] == 1
    assert rows[c["id"]] == 0, "非置顶笔记的 sort_order 必须保持 0"

    # 列表顺序：置顶区按手工顺序（乙→甲），丙按时间排最后
    order = _order_in_list(db_conn)
    assert (
        order.index(b["id"]) < order.index(a["id"]) < order.index(c["id"])
    ), order


def test_reorder_pinned_skips_bad_ids(db_conn):
    note = _make(db_conn, "唯一")
    _pin(db_conn, note["id"])
    updated = repo.reorder_pinned(db_conn, ["x", True, 0, -5, 2**63, note["id"]])
    assert updated == 1
    row = db_conn.execute(
        "SELECT sort_order FROM notes WHERE id = ?", (note["id"],)
    ).fetchone()
    assert row["sort_order"] == 0


def test_reorder_pinned_empty_or_garbage(db_conn):
    note = _make(db_conn, "空入参")
    _pin(db_conn, note["id"])
    assert repo.reorder_pinned(db_conn, []) == 0
    assert repo.reorder_pinned(db_conn, ["abc", None, 3.5]) == 0
    row = db_conn.execute(
        "SELECT sort_order FROM notes WHERE id = ?", (note["id"],)
    ).fetchone()
    assert row["sort_order"] == 0


# ---------------------------------------------------------------------------
# 排序口径（Python 侧，与 SORTS 对齐）
# ---------------------------------------------------------------------------
def test_sort_notes_uses_sort_order_within_pinned():
    notes = [
        {"id": 1, "is_pinned": True, "sort_order": 1, "updated_at": "2026-01-02 10:00:00"},
        {"id": 2, "is_pinned": True, "sort_order": 0, "updated_at": "2026-01-03 10:00:00"},
        {"id": 3, "is_pinned": False, "sort_order": 0, "updated_at": "2026-01-04 10:00:00"},
    ]
    ordered = repo.sort_notes(notes, sort="updated")
    assert [n["id"] for n in ordered] == [2, 1, 3], (
        "置顶区按 sort_order（乙 0 在前），非置顶不受影响"
    )


def test_sort_notes_without_sort_order_keeps_time_order():
    notes = [
        {"id": 1, "is_pinned": True, "sort_order": 0, "updated_at": "2026-01-02 10:00:00"},
        {"id": 2, "is_pinned": True, "sort_order": 0, "updated_at": "2026-01-03 10:00:00"},
        {"id": 3, "is_pinned": False, "sort_order": 0, "updated_at": "2026-01-04 10:00:00"},
    ]
    ordered = repo.sort_notes(notes, sort="updated")
    # 都没拖过（sort_order 全 0）→ 置顶区内部保持时间序，不乱插
    assert [n["id"] for n in ordered] == [2, 1, 3]


# ---------------------------------------------------------------------------
# 路由 POST /notes/reorder
# ---------------------------------------------------------------------------
def test_reorder_route_requires_login_and_csrf(auth_client, csrf, db_conn):
    note = _make(db_conn, "权限")
    _pin(db_conn, note["id"])
    anon = TestClient(app, follow_redirects=False)
    assert anon.post("/notes/reorder", json={"ids": [note["id"]]}).status_code == 303
    assert (
        auth_client.post("/notes/reorder", json={"ids": [note["id"]]}).status_code == 403
    )


def test_reorder_route_updates_and_validates(auth_client, csrf, db_conn):
    a = _make(db_conn, "路由甲")
    b = _make(db_conn, "路由乙")
    _pin(db_conn, a["id"])
    _pin(db_conn, b["id"])
    db_conn.commit()   # 让应用的请求连接看得到这几篇

    headers = {"X-CSRF-Token": csrf}
    resp = auth_client.post(
        "/notes/reorder", json={"ids": [b["id"], a["id"]]}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["updated"] == 2

    db_conn.commit()
    rows = {
        row["id"]: row["sort_order"]
        for row in db_conn.execute("SELECT id, sort_order FROM notes")
    }
    assert rows[b["id"]] == 0 and rows[a["id"]] == 1

    bad = auth_client.post("/notes/reorder", json={"ids": "x"}, headers=headers)
    assert bad.status_code == 400 and bad.json()["ok"] is False


def test_pinned_card_renders_real_draggable_attribute(auth_client, csrf, db_conn):
    """回归：draggable 的引号曾经过 {{ }} 被转义成 &quot;，属性值非法 → 真实鼠标拖不动。

    draggable 是枚举属性，值必须是裸的 true；渲染结果里不允许出现转义引号。
    """
    a = _make(db_conn, "回归拖甲")
    b = _make(db_conn, "回归拖乙")
    _pin(db_conn, a["id"])
    _pin(db_conn, b["id"])
    db_conn.commit()

    page = auth_client.get("/notes", headers={"X-CSRF-Token": csrf})
    assert page.status_code == 200
    html = page.text
    assert "&quot;true&quot;" not in html, "draggable 的值被自动转义了（曾导致拖拽失效）"
    # 库里可能有其它测试留下的置顶笔记，按 id 定位本测试的两篇
    for note_id in (a["id"], b["id"]):
        assert (
            f'data-note-id="{note_id}" draggable="true"' in html
        ), f"笔记 {note_id} 的卡片缺裸 draggable=\"true\""
