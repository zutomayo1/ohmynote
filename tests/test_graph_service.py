# -*- coding: utf-8 -*-
"""图谱服务层（社区检测 / 邻域子图）与图谱页新参数的测试。

约定：会话级 DB 共享，禁止断言全局聚合数量 —— 这里一律用专属标题前缀
定位自己造的笔记，或直接对纯函数（louvain）用合成图。
"""

from __future__ import annotations

import json
import re

import pytest

from app import repo
from app.services import graph as gs
from app.utils import tag_color

PREFIX = "图谱SVC-"


@pytest.fixture()
def db_conn(client):
    """可用的数据库连接：依赖 client 确保 schema 已初始化。"""
    from app import db

    with db.db() as conn:
        yield conn


def _clique(base, k):
    return [(base + i, base + j) for i in range(k) for j in range(i + 1, k)]


def _make(db, title, content, tags=None):
    note = repo.create_note(db, title=title, content=content, tags=tags or [])
    db.commit()
    return note


# ---------------------------------------------------------------- Louvain
def test_louvain_splits_two_cliques_joined_by_a_bridge():
    edges = _clique(0, 5) + _clique(10, 5) + [(0, 10)]
    belong = gs.louvain(edges)
    groups = {}
    for node, label in belong.items():
        groups.setdefault(label, set()).add(node)
    assert len(groups) == 2, groups
    assert set(groups[0]) in ({0, 1, 2, 3, 4}, {10, 11, 12, 13, 14})


def test_louvain_three_cliques_chain():
    edges = _clique(0, 4) + _clique(20, 4) + _clique(40, 4) + [(0, 20), (20, 40)]
    labels = set(gs.louvain(edges).values())
    assert len(labels) == 3


def test_louvain_is_deterministic():
    edges = _clique(0, 5) + _clique(10, 5) + [(0, 10)]
    first = gs.louvain(edges)
    assert all(gs.louvain(edges) == first for _ in range(5))


def test_louvain_community_ids_sorted_by_size():
    edges = _clique(0, 6) + _clique(100, 3) + [(0, 100)]
    sizes = {}
    for label in gs.louvain(edges).values():
        sizes[label] = sizes.get(label, 0) + 1
    assert sizes[0] >= sizes[1]


def test_louvain_edge_cases():
    assert gs.louvain([]) == {}
    assert gs.louvain([(5, 5)]) == {}                       # 自环忽略
    assert len(set(gs.louvain([(1, 2)]).values())) == 1      # 单边 → 一个社区


def test_louvain_terminates_on_symmetric_graph():
    """三角形是对称图：早期实现会在这里来回搬家、永不收敛（曾把进程挂死）。"""
    assert gs.louvain([(0, 1), (1, 2), (0, 2)])             # 能返回就说明没死循环
    # 两个等大的完全图
    assert len(set(gs.louvain(_clique(0, 4) + _clique(9, 4)).values())) == 2


# ---------------------------------------------------------------- build_graph
def test_build_graph_degrees_and_links(db_conn):
    center = _make(db_conn, PREFIX + "中心", "[[{}甲]] [[{}乙]]".format(PREFIX, PREFIX))
    leaf = _make(db_conn, PREFIX + "甲", "[[{}中心]]".format(PREFIX))
    other = _make(db_conn, PREFIX + "乙", "无出链")
    lonely = _make(db_conn, PREFIX + "孤立", "谁也不连")

    g = gs.build_graph(db_conn)
    nodes = {n["id"]: n for n in g["nodes"]}
    assert center["id"] in nodes and leaf["id"] in nodes and lonely["id"] in nodes

    c = nodes[center["id"]]
    assert c["out_degree"] == 2 and c["in_degree"] == 1 and c["degree"] == 3
    assert c["has_links"] is True
    assert c["community"] >= 0

    assert nodes[other["id"]]["has_links"] is True          # 被引用也算有链接
    assert nodes[lonely["id"]]["degree"] == 0
    assert nodes[lonely["id"]]["has_links"] is False
    assert nodes[lonely["id"]]["community"] == -1           # 孤立节点不参与着色

    edge = [e for e in g["edges"] if e["source"] == center["id"] and e["target"] == leaf["id"]]
    assert edge
    assert g["max_degree"] >= 3


def test_build_graph_tag_color_matches_template_filter(db_conn):
    note = _make(db_conn, PREFIX + "配色", "x", tags=["部署"])
    g = gs.build_graph(db_conn)
    node = next(n for n in g["nodes"] if n["id"] == note["id"])
    # 图谱节点配色与标签药丸的色组必须是同一个（否则同一标签两处不同色）
    assert node["tag_color"] == tag_color("部署")
    plain = _make(db_conn, PREFIX + "无标签", "x")
    g2 = gs.build_graph(db_conn)     # 第二篇是一分钟后建的，得重新取一次
    assert next(n for n in g2["nodes"] if n["id"] == plain["id"])["tag_color"] == -1


# ---------------------------------------------------------------- ego_graph
def test_ego_graph_one_hop_and_focus(db_conn):
    hub = _make(db_conn, PREFIX + "枢纽", " ".join(
        "[[{}{}]]".format(PREFIX, t) for t in ("一", "二", "三")))
    one = _make(db_conn, PREFIX + "一", "[[{}枢纽]]".format(PREFIX))
    _make(db_conn, PREFIX + "二", "")
    _make(db_conn, PREFIX + "三", "")

    ego = gs.ego_graph(db_conn, hub["id"], depth=1)
    ids = {n["id"] for n in ego["nodes"]}
    assert hub["id"] in ids and one["id"] in ids
    assert len(ids) == 4
    assert ego["focus"] == hub["id"]
    assert ego["focus_title"] == PREFIX + "枢纽"
    assert ego["neighbor_count"] == 3
    assert ego["truncated"] is False
    assert [n["id"] for n in ego["nodes"] if n["focus"]] == [hub["id"]]


def test_ego_graph_depth_is_clamped(db_conn):
    a = _make(db_conn, PREFIX + "链头", "[[{}链尾]]".format(PREFIX))
    _make(db_conn, PREFIX + "链尾", "")
    for bad in (0, -3, 99, None):
        ego = gs.ego_graph(db_conn, a["id"], depth=bad)
        assert 1 <= ego["depth"] <= 2


def test_ego_graph_two_hops_reaches_grandchild(db_conn):
    a = _make(db_conn, PREFIX + "两层甲", "[[{}两层乙]]".format(PREFIX))
    b = _make(db_conn, PREFIX + "两层乙", "[[{}两层丙]]".format(PREFIX))
    c = _make(db_conn, PREFIX + "两层丙", "")
    one = gs.ego_graph(db_conn, a["id"], depth=1)
    two = gs.ego_graph(db_conn, a["id"], depth=2)
    assert c["id"] not in {n["id"] for n in one["nodes"]}
    assert c["id"] in {n["id"] for n in two["nodes"]}
    assert b["id"] in {n["id"] for n in two["nodes"]}


def test_ego_graph_truncates_and_flags(db_conn):
    hub = repo.create_note(db_conn, title=PREFIX + "大头", content="", tags=[])
    for i in range(12):
        leaf = repo.create_note(db_conn, title="{}{}叶{}".format(PREFIX, "T", i),
                                content="[[{}大头]]".format(PREFIX), tags=[])
        assert leaf
    db_conn.commit()
    ego = gs.ego_graph(db_conn, hub["id"], depth=1, max_nodes=5)
    assert len(ego["nodes"]) <= 5
    assert ego["truncated"] is True
    assert ego["neighbor_count"] == 12       # 真实邻居数如实上报


def test_ego_graph_missing_note_returns_none(db_conn):
    assert gs.ego_graph(db_conn, 99999999) is None


# ---------------------------------------------------------------- 路由 / 模板
def _graph_json(html):
    m = re.search(r'id="graph-data"[^>]*>(.*?)</script>', html, re.S)
    assert m, "找不到 graph-data"
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_graph_page_focus_param(auth_client, db_conn):
    note = _make(db_conn, PREFIX + "定位", "x")
    resp = auth_client.get("/graph?focus={}".format(note["id"]))
    assert resp.status_code == 200
    assert "已定位到" in resp.text
    assert 'data-focus="{}"'.format(note["id"]) in resp.text


def test_graph_page_ignores_unknown_focus(auth_client, db_conn):
    resp = auth_client.get("/graph?focus=99999999")
    assert resp.status_code == 200
    assert "已定位到" not in resp.text


def test_graph_page_has_new_toolbar(auth_client, db_conn):
    _make(db_conn, PREFIX + "工具", "[[{}工具乙]]".format(PREFIX))
    _make(db_conn, PREFIX + "工具乙", "")
    resp = auth_client.get("/graph")
    text = resp.text
    for needle in ("graph-search", "graph-color", "graph-legend", "graph-fit",
                   "graph-relayout", "graph-zoom-in", "graph-zoom-out"):
        assert needle in text, needle
    # 节点度数 / 社区进了 JSON（前端据此缩放与着色）
    payload = _graph_json(text)
    node = payload["nodes"][0]
    assert {"degree", "in_degree", "out_degree", "community", "tag_color"} <= set(node)
    assert "community_count" in payload


def test_note_detail_shows_local_graph_when_linked(auth_client, db_conn):
    a = _make(db_conn, PREFIX + "局部甲", "[[{}局部乙]]".format(PREFIX))
    _make(db_conn, PREFIX + "局部乙", "")
    resp = auth_client.get("/notes/{}".format(a["id"]))
    assert resp.status_code == 200
    assert 'id="local-graph-stage"' in resp.text
    assert 'id="local-graph-data"' in resp.text
    assert "局部关系图" in resp.text
    m = re.search(r'id="local-graph-data"[^>]*>(.*?)</script>', resp.text, re.S)
    data = json.loads(m.group(1).replace("<\\/", "</"))
    assert data["focus"] == a["id"]
    assert len(data["nodes"]) == 2


def test_note_detail_hides_local_graph_when_no_links(auth_client, db_conn):
    solo = _make(db_conn, PREFIX + "无链", "谁也不连")
    resp = auth_client.get("/notes/{}".format(solo["id"]))
    assert resp.status_code == 200
    assert 'id="local-graph-stage"' not in resp.text


def test_note_detail_graph_depth_two_hops(auth_client, db_conn):
    a = _make(db_conn, PREFIX + "深度甲", "[[{}深度乙]]".format(PREFIX))
    b = _make(db_conn, PREFIX + "深度乙", "[[{}深度丙]]".format(PREFIX))
    c = _make(db_conn, PREFIX + "深度丙", "")

    one = auth_client.get("/notes/{}".format(a["id"]))
    two = auth_client.get("/notes/{}?graph_depth=2".format(a["id"]))
    assert one.status_code == 200 and two.status_code == 200

    def payload(resp):
        m = re.search(r'id="local-graph-data"[^>]*>(.*?)</script>', resp.text, re.S)
        return json.loads(m.group(1).replace("<\\/", "</"))

    ids_one = {n["id"] for n in payload(one)["nodes"]}
    ids_two = {n["id"] for n in payload(two)["nodes"]}
    assert c["id"] not in ids_one and b["id"] in ids_one
    assert c["id"] in ids_two
    # 切换链接：1 跳页给「看 2 跳」，2 跳页给「只看 1 跳」
    assert "看 2 跳" in one.text and "只看 1 跳" in two.text


def test_note_detail_graph_depth_bad_values_fall_back(auth_client, db_conn):
    a = _make(db_conn, PREFIX + "深度坏值", "[[{}深度坏值乙]]".format(PREFIX))
    _make(db_conn, PREFIX + "深度坏值乙", "")
    for bad in ("abc", "0", "-5", ""):
        resp = auth_client.get("/notes/{}?graph_depth={}".format(a["id"], bad))
        assert resp.status_code == 200
        # 一律退化成 1 跳（页面显示「看 2 跳」）
        assert "看 2 跳" in resp.text


def test_build_graph_category_color(db_conn):
    """按分类着色：同分类同色组，没分类给 -1，且与标签色组用同一套哈希。"""
    a = repo.create_note(db_conn, title=PREFIX + "分类甲", content="", category="读书笔记")
    b = repo.create_note(db_conn, title=PREFIX + "分类乙", content="", category="读书笔记")
    c = repo.create_note(db_conn, title=PREFIX + "无分类", content="")
    db_conn.commit()

    graph = gs.build_graph(db_conn)
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert by_id[a["id"]]["category_color"] == by_id[b["id"]]["category_color"]
    assert by_id[a["id"]]["category_color"] == tag_color("读书笔记")
    assert 0 <= by_id[a["id"]]["category_color"] < 8
    assert by_id[c["id"]]["category_color"] == -1


def test_ego_graph_nodes_carry_category_color(db_conn):
    a = repo.create_note(db_conn, title=PREFIX + "局部分类甲", content="", category="随笔")
    b = repo.create_note(
        db_conn, title=PREFIX + "局部分类乙", content="[[{}]]".format(PREFIX + "局部分类甲"), category="随笔"
    )
    db_conn.commit()
    ego = gs.ego_graph(db_conn, a["id"], depth=1)
    assert ego is not None
    for node in ego["nodes"]:
        assert node["category_color"] == tag_color("随笔")
    assert gs.ego_graph(db_conn, b["id"], depth=1)["neighbor_count"] == 1


def test_graph_page_offers_category_coloring_when_used(auth_client, db_conn):
    """有分类时工具栏才给「按分类着色」这个选项。"""
    repo.create_note(db_conn, title=PREFIX + "有分类的", content="", category="技术杂谈")
    db_conn.commit()
    resp = auth_client.get("/graph")
    assert resp.status_code == 200
    assert "按分类着色" in resp.text
    # 新增的路径 / 导出 / 布局控件都在
    for marker in ('id="graph-path"', 'id="graph-export-png"', 'id="graph-export-svg"',
                   'id="graph-p-repulsion"', 'id="graph-path-actions"', 'id="graph-link-fwd"'):
        assert marker in resp.text, marker


def test_graph_payload_nodes_carry_category(auth_client, db_conn):
    note = repo.create_note(db_conn, title=PREFIX + "载荷分类", content="", category="写作")
    db_conn.commit()
    resp = auth_client.get("/graph")
    match = re.search(r'id="graph-data"[^>]*>(.*?)</script>', resp.text, re.S)
    data = json.loads(match.group(1).replace("<\\/", "</"))
    node = next(n for n in data["nodes"] if n["id"] == note["id"])
    assert node["category"] == "写作"
    assert node["category_color"] == tag_color("写作")
