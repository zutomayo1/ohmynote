# -*- coding: utf-8 -*-
"""关系图谱：把 ``[[双链]]`` 抽成图，并做社区聚类 / 邻域子图。

本项目前端坚持零依赖，所以这里是**借算法和交互设计，不引库**。参考了这些开源实现：

- **graphology-communities-louvain**（MIT）：``louvain()`` 的目标函数与聚合流程照它的思路写；
  社区 id 按规模排序，方便前端「最大的几个簇各占一个颜色」。
- **graphology-layout-forceatlas2**（MIT）：布局参数（引力 / 缩放比 / 阻尼）的划分方式，
  由前端 ``graph.js`` 落地。
- **d3-force**（BSD）：节点碰撞（collide）与分区受力，同样在前端。
- **Quartz / Logseq**：「局部图」这一交互形态（只看当前笔记的邻居）。

关于复杂度：个人笔记量级（几百篇）用精确的两两斥力就够，只有上千节点才需要
Barnes–Hut 近似 —— 前端按节点数自动切换。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any, Iterable

from .. import repo
from ..utils import tag_color

# 局部图默认最多画多少个节点（太多就不是「局部」了）
EGO_MAX_NODES = 60
EGO_MAX_DEPTH = 2

# 每层最多扫多少轮（正常几轮就收敛，纯粹是防御性的）
_MAX_PASSES = 50


# ---------------------------------------------------------------------------
# 取图
# ---------------------------------------------------------------------------
def _load(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[tuple[int, int, str]]]:
    """读出全部笔记与「两端都还在」的双链。直接读 note_links，不重解析正文。"""
    notes = repo.all_notes(conn)
    by_id = {n["id"]: n for n in notes}
    rows = conn.execute(
        "SELECT source_id, target_id, target_title FROM note_links"
    ).fetchall()

    edges: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int]] = set()
    for row in rows:
        target_id = row["target_id"]
        if target_id is None:
            continue
        source_id, target_id = int(row["source_id"]), int(target_id)
        key = (source_id, target_id)
        if key in seen or source_id not in by_id or target_id not in by_id:
            continue
        seen.add(key)
        edges.append((source_id, target_id, str(row["target_title"] or "")))
    return notes, edges


def _adjacency(edges: Iterable[tuple[int, int, str]]) -> dict[int, set[int]]:
    adj: dict[int, set[int]] = defaultdict(set)
    for source, target, _ in edges:
        adj[source].add(target)
        adj[target].add(source)
    return adj


# ---------------------------------------------------------------------------
# Louvain 社区检测（纯 Python，确定性）
# ---------------------------------------------------------------------------
def _one_level(adj: dict[int, dict[int, float]]) -> dict[int, int]:
    """一层局部移动：返回 {节点: 社区}。迭代顺序按度降序，保证结果可复现。"""
    total = {g: sum(weights.values()) for g, weights in adj.items()}
    m2 = sum(total.values())  # = 2m（自环按 2 计，天然满足）
    community = {g: g for g in adj}
    order = sorted(adj, key=lambda g: (-total[g], g))

    # 局部移动。注意：候选**包含「留在原社区」**，且只在严格更优时才搬——
    # 否则对称图（三角形、两个等大的簇）里 k_i,in 相同会让节点来回搬家，永不收敛。
    passes = 0
    while passes < _MAX_PASSES:
        passes += 1
        moved = False
        for g in order:
            current = community[g]
            total[current] -= total[g]
            # 到各邻居社区的权重和
            weights: dict[int, float] = {}
            for nbr, weight in adj[g].items():
                label = community[nbr]
                weights[label] = weights.get(label, 0.0) + weight

            # ΔQ 的单调部分：k_i,in - Σtot · k_i / 2m
            best_label = current
            best_value = weights.get(current, 0.0) - (
                total[current] * total[g] / m2 if m2 else 0.0
            )
            for label, weight in weights.items():
                if label == current:
                    continue
                value = weight - (total[label] * total[g] / m2 if m2 else 0.0)
                if value > best_value + 1e-12:
                    best_value, best_label = value, label

            if best_label != current:
                moved = True
                community[g] = best_label
            total[best_label] += total[g]
        if not moved:
            break
    return community


def _aggregate(
    adj: dict[int, dict[int, float]], community: dict[int, int]
) -> dict[int, dict[int, float]]:
    """把同一社区的节点合并成超级节点。社区内权重记成自环（乘 2 以维持度数语义）。"""
    merged: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for g, weights in adj.items():
        cg = community[g]
        for h, weight in weights.items():
            ch = community[h]
            merged[cg][ch] += weight
    # 自环在下一层的度数里要算两次
    out: dict[int, dict[int, float]] = {}
    for c, weights in merged.items():
        row: dict[int, float] = {}
        for h, weight in weights.items():
            row[h] = weight * 2 if h == c else weight
        out[c] = row
    return out


def louvain(edges: Iterable[tuple[int, int]], *, max_levels: int = 4) -> dict[int, int]:
    """社区检测。``edges`` 是无向边列表，返回 ``{节点: 社区 id}``（id 按社区规模降序）。

    规模最大的社区拿到 0，方便前端「前 8 个社区各配一个颜色」。
    """
    nodes: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for a, b in edges:
        if a == b:
            continue
        nodes.add(a)
        nodes.add(b)
        pairs.append((a, b))
    if not nodes:
        return {}

    adj: dict[int, dict[int, float]] = {n: {} for n in sorted(nodes)}
    for a, b in pairs:
        adj[a][b] = adj[a].get(b, 0.0) + 1.0
        adj[b][a] = adj[b].get(a, 0.0) + 1.0

    groups: dict[int, set[int]] = {n: {n} for n in adj}
    level = 0
    while level < max_levels and len(adj) > 1:
        community = _one_level(adj)
        if all(community[g] == g for g in adj):
            break  # 没有任何移动，收敛
        merged: dict[int, set[int]] = defaultdict(set)
        for g, label in community.items():
            merged[label] |= groups[g]
        groups = dict(merged)
        adj = _aggregate(adj, community)
        level += 1

    # 规模降序（同规模按最小节点 id）→ 社区 id 0,1,2…
    ordered = sorted(groups.values(), key=lambda members: (-len(members), min(members)))
    belong: dict[int, int] = {}
    for index, members in enumerate(ordered):
        for node in members:
            belong[node] = index
    return belong


# ---------------------------------------------------------------------------
# 对外：全图
# ---------------------------------------------------------------------------
def build_graph(conn: sqlite3.Connection) -> dict[str, Any]:
    """全图：节点带连接数（in / out / 合计）与社区 id，边带目标标题。"""
    notes, edges = _load(conn)
    out_degree: dict[int, int] = defaultdict(int)
    in_degree: dict[int, int] = defaultdict(int)
    for source, target, _ in edges:
        out_degree[source] += 1
        in_degree[target] += 1

    belong = louvain([(s, t) for s, t, _ in edges])

    nodes = []
    for note in notes:
        nid = note["id"]
        degree = out_degree[nid] + in_degree[nid]
        nodes.append(
            {
                "id": nid,
                "title": note["title"],
                "tags": list(note.get("tags") or []),
                "category": note.get("category") or "",
                "updated_at": note.get("updated_at") or "",
                "in_degree": in_degree[nid],
                "out_degree": out_degree[nid],
                "degree": degree,
                "has_links": degree > 0,
                # 孤立节点给 -1：前端一律弱化显示，不参与着色
                "community": belong.get(nid, -1),
                # 第一个标签的色组（与标签药丸同一个色板）；没有标签给 -1
                "tag_color": tag_color(note["tags"][0]) if note.get("tags") else -1,
            }
        )

    title_of = {n["id"]: n["title"] for n in nodes}
    edge_payload = [
        {"source": s, "target": t, "title": title or title_of.get(t, "")}
        for s, t, title in edges
    ]
    communities = {n["community"] for n in nodes if n["community"] >= 0}
    return {
        "nodes": nodes,
        "edges": edge_payload,
        "community_count": len(communities),
        "max_degree": max((n["degree"] for n in nodes), default=0),
    }


# ---------------------------------------------------------------------------
# 对外：局部图（一篇笔记的邻域）
# ---------------------------------------------------------------------------
def ego_graph(
    conn: sqlite3.Connection,
    note_id: int,
    *,
    depth: int = 1,
    max_nodes: int = EGO_MAX_NODES,
) -> dict[str, Any] | None:
    """以 ``note_id`` 为中心的邻域子图（默认 1 跳，最多 2 跳）。

    借鉴 Obsidian / Quartz 的「局部图」：在笔记页旁边画它的一两跳邻居，比全图更有用。
    节点数超过 ``max_nodes`` 时按「连通度 + 最近更新」保留最有价值的那些。
    """
    depth = max(1, min(int(depth or 1), EGO_MAX_DEPTH))
    notes, edges = _load(conn)
    by_id = {n["id"]: n for n in notes}
    if note_id not in by_id:
        return None

    adj = _adjacency(edges)
    keep: set[int] = {note_id}
    frontier: set[int] = {note_id}
    for _ in range(depth):
        nxt: set[int] = set()
        for node in frontier:
            nxt |= adj[node]
        frontier = nxt - keep
        keep |= nxt

    neighbors = adj[note_id]
    truncated = 0
    if len(keep) > max_nodes:
        # 按「在子图里的连通度降序，其次最近更新」保留最有价值的一批
        others = sorted(
            keep - {note_id},
            key=lambda n: (-len(adj[n]), str(by_id[n].get("updated_at") or ""), n),
            reverse=False,
        )
        keep = {note_id} | set(others[: max_nodes - 1])
        truncated = 1

    in_sub: dict[int, int] = defaultdict(int)
    sub_edges = []
    for source, target, _ in edges:
        if source in keep and target in keep:
            sub_edges.append((source, target))
            in_sub[source] += 1
            in_sub[target] += 1

    nodes = [
        {
            "id": nid,
            "title": by_id[nid]["title"],
            "tags": list(by_id[nid].get("tags") or []),
            "category": by_id[nid].get("category") or "",
            "updated_at": by_id[nid].get("updated_at") or "",
            "degree": in_sub[nid],
            "has_links": in_sub[nid] > 0 or nid == note_id,
            "community": -1,
            "tag_color": tag_color(by_id[nid]["tags"][0]) if by_id[nid].get("tags") else -1,
            "focus": nid == note_id,
        }
        for nid in sorted(keep)
    ]
    return {
        "nodes": nodes,
        "edges": [{"source": s, "target": t, "title": by_id[t]["title"]} for s, t in sub_edges],
        "community_count": 0,
        "max_degree": max((n["degree"] for n in nodes), default=0),
        "focus": note_id,
        "focus_title": by_id[note_id]["title"],
        "neighbor_count": len(neighbors),
        "depth": depth,
        "truncated": bool(truncated),
        "total_nodes": len(keep),
    }
