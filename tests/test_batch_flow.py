"""批量操作流：一次勾选排队多个动作，服务端按序执行、分步汇报。"""
from __future__ import annotations

import json
from urllib.parse import unquote_plus

import pytest

from app import repo
from tests.test_agent import db_conn  # noqa: F401  fixture 随模块导入

PREFIX = "操作流-"  # 专属前缀：断言按前缀定位，不碰全局聚合


@pytest.fixture
def two_notes(db_conn):
    a = repo.create_note(db_conn, title=PREFIX + "流甲", content="x")
    b = repo.create_note(db_conn, title=PREFIX + "流乙", content="x")
    db_conn.commit()
    return a, b


def _post_batch(auth_client, csrf, ids, flow, follow=True):
    """POST /notes/batch；follow=True 时跟随重定向，返回带 flash 的最终页面。"""
    resp = auth_client.post(
        "/notes/batch",
        data={
            "note_ids": [str(n["id"]) for n in ids],
            "flow": json.dumps(flow),
            "next": "/notes",
        },
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    if follow:
        return auth_client.get(resp.headers["location"], headers={"X-CSRF-Token": csrf})
    return resp


def _flash(page):
    import re

    m = re.search(r'id="flash"[^>]*>(.*?)</div>', page.text, re.S)
    return m.group(1).strip() if m else ""


def test_flow_runs_steps_in_order(auth_client, csrf, db_conn, two_notes):
    a, b = two_notes
    page = _post_batch(auth_client, csrf, [a, b], [
        {"action": "star"},
        {"action": "set_category", "category": "技术"},
        {"action": "add_tag", "tag": "整理"},
    ])
    assert page.status_code == 200
    assert "操作流完成（3 步）" in _flash(page)
    assert "①" in _flash(page) and "③" in _flash(page)
    for note in (a, b):
        fresh = repo.get_note(db_conn, note["id"])
        assert fresh["is_starred"] is True
        assert fresh["category"] == "技术"
        assert "整理" in fresh["tags"]


def test_flow_single_step_keeps_old_message(auth_client, csrf, two_notes):
    """只有一步时不套「操作流」外壳，消息与旧单动作一致。"""
    a, _ = two_notes
    resp = _post_batch(auth_client, csrf, [a], [{"action": "star"}], follow=False)
    assert resp.status_code == 303
    assert "已处理 1 篇" in unquote_plus(resp.headers["location"])


def test_flow_trash_step_makes_later_steps_skip(db_conn, auth_client, csrf, two_notes):
    """流里先移入回收站：后续步骤对这些 id 只能跳过（get_note 查不到）。"""
    a, b = two_notes
    page = _post_batch(auth_client, csrf, [a, b], [
        {"action": "trash"},
        {"action": "star"},
    ])
    assert repo.get_note(db_conn, a["id"]) is None          # 已进回收站
    assert repo.get_note(db_conn, b["id"]) is None          # 两篇都进了回收站
    assert "已处理 0 篇、跳过 2 篇" in _flash(page)          # 第二步全部跳过


def test_flow_rejects_bad_payload(auth_client, csrf):
    for flow in ("not-json", '{"action": "star"}', '["star"]',
                 json.dumps([{"action": "star"}] * 6)):
        resp = auth_client.post(
            "/notes/batch",
            data={"flow": flow, "next": "/notes"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 400, flow


def test_flow_without_flow_field_still_works(auth_client, csrf, two_notes):
    """兼容旧调用方：只有 action 字段（无 flow）时行为不变。"""
    a, _ = two_notes
    resp = auth_client.post(
        "/notes/batch",
        data={"note_ids": str(a["id"]), "action": "star", "next": "/notes"},
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "已处理 1 篇" in unquote_plus(resp.headers["location"])
