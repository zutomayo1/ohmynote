"""关系图谱页 /graph 的测试。

约定：会话级 DB 共享，禁止断言全局聚合数量。
这里用专属标题前缀定位自己创建的笔记，再核对图谱 JSON 里的节点 / 边。
"""

from __future__ import annotations

import json

import pytest

from app import repo

PREFIX = "图谱测试-"


@pytest.fixture()
def db_conn(client):
    """可用的数据库连接：依赖 client 确保 schema 已初始化。"""
    from app import db

    with db.db() as conn:
        yield conn


def _make(db, title, content, tags=None):
    note = repo.create_note(db, title=title, content=content, tags=tags or [])
    db.commit()  # 让路由里独立的连接也能读到（db.db() 在 fixture 退出时才整体提交）
    return note


def test_graph_requires_login():
    # 用全新客户端（不共享会话 cookie），避免被并行用例的登录态污染
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as anon:
        resp = anon.get("/graph", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
        assert "/login" in resp.headers.get("location", "")


def test_graph_renders(auth_client, db_conn):
    # 造两篇互相链接的笔记，保证有链接数据与 SSR 列表
    _make(db_conn, PREFIX + "中心", "指向 [[{}收件]]".format(PREFIX), tags=["图谱"])
    _make(db_conn, PREFIX + "收件", "回指 [[{}中心]]".format(PREFIX), tags=["图谱"])

    resp = auth_client.get("/graph")
    assert resp.status_code == 200
    text = resp.text
    assert "关系图谱" in text
    assert "篇笔记" in text and "条链接" in text
    # JSON 数据块存在且可解析
    assert 'id="graph-data"' in text
    assert "/static/js/graph.js" in text


def test_graph_json_matches_created_notes(auth_client, db_conn):
    center = _make(db_conn, PREFIX + "A中心", "链接到 [[{}B收件]]".format(PREFIX), tags=["图谱"])
    leaf = _make(db_conn, PREFIX + "B收件", "无出链", tags=["图谱"])

    resp = auth_client.get("/graph")
    assert resp.status_code == 200
    # 取出 <script id="graph-data"> 里的 JSON
    import re

    m = re.search(r'id="graph-data"[^>]*>(.*?)</script>', resp.text, re.S)
    assert m, "找不到 graph-data JSON"
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    ids = {n["id"] for n in payload["nodes"]}
    assert center["id"] in ids
    assert leaf["id"] in ids

    # 边：A中心 -> B收件
    edge = [e for e in payload["edges"] if e["source"] == center["id"] and e["target"] == leaf["id"]]
    assert edge, "应当存在 A中心 -> B收件 的链接边"

    # 有链接的笔记被标记
    center_node = next(n for n in payload["nodes"] if n["id"] == center["id"])
    assert center_node["has_links"] is True
    leaf_node = next(n for n in payload["nodes"] if n["id"] == leaf["id"])
    assert leaf_node["has_links"] is True  # 被反向链接也算有链接


def test_graph_no_js_list_visible(auth_client, db_conn):
    _make(db_conn, PREFIX + "列表A", "[[{}列表B]]".format(PREFIX))
    _make(db_conn, PREFIX + "列表B", "ok")

    resp = auth_client.get("/graph")
    assert resp.status_code == 200
    # SSR 兜底列表存在、带 no-js-only（无 JS 时可见），且列有「最近有链接的笔记」
    assert "graph-fallback" in resp.text
    assert "no-js-only" in resp.text
    assert "最近有链接的笔记" in resp.text
    # 我创建的链接关系确实进入了图谱 JSON（边存在即代表有链接笔记）
    import re
    m = re.search(r'id="graph-data"[^>]*>(.*?)</script>', resp.text, re.S)
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    by_title = {n["title"]: n["id"] for n in payload["nodes"]}
    assert PREFIX + "列表A" in by_title
    assert PREFIX + "列表B" in by_title
    a_id, b_id = by_title[PREFIX + "列表A"], by_title[PREFIX + "列表B"]
    assert any(e["source"] == a_id and e["target"] == b_id for e in payload["edges"])


def test_graph_filter_state_default_on(auth_client, db_conn):
    _make(db_conn, PREFIX + "筛选X", "[[{}筛选Y]]".format(PREFIX))
    _make(db_conn, PREFIX + "筛选Y", "ok")

    resp = auth_client.get("/graph")
    assert "graph-only-linked" in resp.text
    # 默认勾选「只看有链接的笔记」
    assert 'id="graph-only-linked" checked' in resp.text
