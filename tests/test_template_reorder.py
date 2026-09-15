"""模板拖拽排序：后端 /templates/reorder 端点 + repo.reorder_templates。

约定（见任务说明）：
- session 库共享，禁止断言全局聚合数量；
- 用专属前缀命名 + 按主键定位专属模板，避免污染别的用例；
- 非法 id → 4xx 人话；不存在但合法的 id → 跳过且不 500。
"""

from __future__ import annotations

import pytest

from app import db as db_mod
from app import repo

PREFIX = "ZTMP排序_"  # 专属前缀，便于在共享库里认出本用例造的模板


@pytest.fixture()
def tpl_ids(client):
    """造 3 个专属模板，返回其 id（按创建顺序），teardown 时删掉。"""
    with db_mod.db() as conn:
        ids = [
            repo.save_template(conn, name=f"{PREFIX}{i}", description="", content=f"内容{i}")
            for i in range(3)
        ]
    yield ids
    with db_mod.db() as conn:
        for tid in ids:
            repo.delete_template(conn, tid)


def _ordered_ids(conn, wanted: list[int]) -> list[int]:
    return [t["id"] for t in repo.list_templates(conn) if t["id"] in wanted]


def test_reorder_needs_login(client):
    # client 是 session 共享、可能已被别的用例登录过，这里另起一个无 cookie 的实例
    fresh = client.__class__(client.app)
    res = fresh.post("/templates/reorder", json={"ids": [1, 2]}, follow_redirects=False)
    assert res.status_code in (303, 401, 403)


def test_reorder_needs_csrf(auth_client):
    res = auth_client.post("/templates/reorder", json={"ids": [1, 2]})
    assert res.status_code == 403


def test_reorder_empty_is_400(auth_client, csrf):
    res = auth_client.post("/templates/reorder", json={"ids": []}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 400
    assert "error" in res.json()


def test_reorder_normal_changes_order(auth_client, csrf, tpl_ids):
    with db_mod.db() as conn:
        before = _ordered_ids(conn, tpl_ids)
    assert before == tpl_ids  # 创建顺序即初始展示顺序

    reversed_ids = list(reversed(tpl_ids))
    res = auth_client.post(
        "/templates/reorder", json={"ids": reversed_ids}, headers={"X-CSRF-Token": csrf}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["updated"] == len(tpl_ids)  # 三个都更新到了

    with db_mod.db() as conn:
        after = _ordered_ids(conn, tpl_ids)
    assert after == reversed_ids, "服务端顺序应翻转为 reversed"


def test_reorder_rejects_non_integer_id_with_4xx(auth_client, csrf):
    """数组里夹非整数 → FastAPI 校验给出 4xx，而不是 500。"""
    res = auth_client.post(
        "/templates/reorder", json={"ids": ["不是数字", 1]}, headers={"X-CSRF-Token": csrf}
    )
    assert 400 <= res.status_code < 500


def test_reorder_skips_missing_id_without_500(auth_client, csrf, tpl_ids):
    """合法范围但库里不存在的 id 被跳过：返回 200，updated 只算实际更新的。"""
    payload = tpl_ids + [999999]
    res = auth_client.post(
        "/templates/reorder", json={"ids": payload}, headers={"X-CSRF-Token": csrf}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["updated"] == len(tpl_ids)  # 999999 没对应行，不计入

    with db_mod.db() as conn:
        after = _ordered_ids(conn, tpl_ids)
    assert after == tpl_ids  # 顺序未被不存在的 id 打乱


def test_reorder_skips_out_of_range_id_without_500(auth_client, csrf, tpl_ids):
    """超过 SQLite 整数上限的 id 在 repo 里被跳过，绝不 OverflowError→500。"""
    huge = 2**70  # 远超 MAX_SQLITE_INT
    payload = tpl_ids + [huge]
    res = auth_client.post(
        "/templates/reorder", json={"ids": payload}, headers={"X-CSRF-Token": csrf}
    )
    assert res.status_code == 200
    assert res.json()["updated"] == len(tpl_ids)
