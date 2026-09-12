"""语义搜索：给搜索页的「按意思搜」提供入口。

对外只有两个函数：
    semantic_search(conn, query, *, limit=20) -> dict | None
        命中返回 {"items": [...], "total": n, "engine": "semantic"}；
        items 和 repo.search_notes 同构（在 ai_embed 的向量召回结果上补齐
        score / snippet / tokens，模板里的 note_card 宏可以直接用）。
        没配向量模型、没有索引、调用失败……一律返回 None，绝不抛异常，
        由 /search 路由回退关键词检索。

    diagnose(conn) -> {"reason": str, "needs_setup": bool}
        语义不可用时给页面用的人话原因；needs_setup 为真表示要去设置页
        配置向量模型并重建索引。同样绝不抛异常。

分页取舍（一次性取前 N 条）：
    ai_embed.retrieve 每次调用都要把问题向量化、再和全库算一遍余弦。
    翻页时重算不划算，所以路由按 per_page 放大后传一个 N，这里一次取回
    N 条候选，路由再在内存里按页切片。代价是排在第 N 名之后的语义结果
    翻不到，页面上以「共 N 篇」为准——对个人笔记规模足够，也更省向量调用。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from . import ai_embed

logger = logging.getLogger("inknote.ai_search")

ENGINE = "semantic"


def _similarity(value: Any) -> float:
    """把余弦相似度收敛到 0~1，方便页面显示成「匹配度 N%」。"""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score != score:  # NaN
        return 0.0
    return max(0.0, min(1.0, score))


def semantic_search(conn: sqlite3.Connection, query: str, *, limit: int = 20) -> dict | None:
    """语义检索；不可用/失败返回 None，绝不抛异常。"""
    query = str(query or "").strip()
    if not query:
        return None
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 20
    try:
        # ai_embed.retrieve 已经保证「不可用 → None、出错 → None」，
        # 它就是我们要复用的语义检索入口。
        hits = ai_embed.retrieve(conn, query, limit=limit)
        if not hits:
            return None

        from .. import search as search_mod

        tokens = search_mod.tokenize(query)
        items: list[dict] = []
        for note in hits[:limit]:
            item = dict(note)
            item["score"] = round(_similarity(item.get("score")), 4)
            if not item.get("snippet"):
                # 兜底：保证模板里的 note_card 一定有 snippet 可渲染
                item["snippet"] = search_mod.make_snippet(item.get("content") or "", tokens)
            item["tokens"] = tokens
            items.append(item)
        if not items:
            return None
        return {"items": items, "total": len(items), "engine": ENGINE}
    except Exception:  # noqa: BLE001 - 语义搜挂掉只是降级，绝不能把搜索页拖垮
        logger.debug("语义检索失败（query=%r, limit=%s），调用方将回退关键词", query, limit, exc_info=True)
        return None


def diagnose(conn: sqlite3.Connection) -> dict:
    """语义不可用时的排障信息：人话原因 + 是否需要去设置页配置/重建索引。"""
    try:
        status = ai_embed.status(conn)
        if not status.get("configured"):
            return {"reason": "还没有配置向量模型", "needs_setup": True}
        if int(status.get("indexed") or 0) <= 0:
            return {"reason": "向量索引还没建", "needs_setup": True}
        if status.get("error"):
            return {"reason": str(status["error"]), "needs_setup": False}
        return {"reason": "向量服务这次没能返回结果", "needs_setup": False}
    except Exception:  # noqa: BLE001 - 排障信息读失败也要能让页面继续渲染
        logger.debug("读取语义检索排障信息失败", exc_info=True)
        return {"reason": "语义检索暂时不可用", "needs_setup": False}
