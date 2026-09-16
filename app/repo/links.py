"""数据访问层子模块：由 repo.py 按分区拆出，函数签名与行为不变。"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from .. import search as search_mod
from ..deps import MAX_SQLITE_INT
from ..config import settings
from ..markdown_render import WikiRef, make_excerpt, text_stats
from ..utils import (
    MAX_TAG_LEN,
    MAX_TAGS,
    escape_like,
    extract_inline_tags,
    month_label,
    now,
    now_iso,
    parse_dt,
    parse_tags,
    slugify,
    sanitize_slug,
)

from .common import *  # noqa: F401,F403  共享常量与行->字典工具
from .common import _tags_map  # noqa: F401  下划线名不随 * 导出
from .notes import get_note, update_note


# ---------------------------------------------------------------------------
# 双链 / 相关笔记
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 双链 / 相关笔记
# ---------------------------------------------------------------------------
def backlinks(conn: sqlite3.Connection, note_id: int, *, public_only: bool = False) -> list[dict[str, Any]]:
    conditions = ["l.target_id = ?", "n.deleted_at IS NULL"]
    if public_only:
        conditions.append("n.is_public = 1")
    rows = conn.execute(
        f"SELECT n.* FROM note_links l JOIN notes n ON n.id = l.source_id "
        f"WHERE {' AND '.join(conditions)} ORDER BY n.updated_at DESC LIMIT 50",
        (note_id,),
    ).fetchall()
    return hydrate(conn, rows)


def link_refs_to(
    conn: sqlite3.Connection, note_id: int, old_title: str
) -> list[tuple[dict[str, Any], int]]:
    """反向链接里**正文还写着旧标题**的笔记（含处数）。

    「改标题后一并更新链接」的数据源：``note_links`` 指向本篇（改名不丢），
    但正文里的 ``[[旧标题]]`` 渲染时按标题解析，改完名就断了——这个函数
    把「要改的正文」精确找出来（忽略大小写；代码块内的不算）。
    """
    import re

    from ..markdown_render import map_outside_code

    old_title = (old_title or "").strip()
    if not old_title:
        return []
    pattern = re.compile(
        r"\[\[\s*" + re.escape(old_title) + r"\s*(\|[^\[\]]*)?\]\]",
        re.IGNORECASE,
    )
    result: list[tuple[dict[str, Any], int]] = []
    for note in backlinks(conn, note_id):
        counter = {"hits": 0}

        def count(chunk: str) -> str:   # 只数代码块外的正文
            counter["hits"] += len(pattern.findall(chunk))
            return chunk

        map_outside_code(note["content"] or "", count)
        if counter["hits"]:
            result.append((note, counter["hits"]))
    return result


def rename_link_refs(
    conn: sqlite3.Connection, note_id: int, old_title: str, new_title: str
) -> tuple[int, int, int]:
    """把所有 ``[[old_title]]`` / ``[[old_title|别名]]`` 改写成新标题（别名保留）。

    - 每篇都走 :func:`update_note`，版本历史照记，反悔可回滚；
    - 代码块内的 ``[[...]]`` 不动（复用 markdown_render.map_outside_code）；
    - 单篇失败跳过、不拖垮整批，失败数单独返回让页面提示；
    - 返回 (更新篇数, 更新处数, 失败篇数)。
    """
    import re

    from ..markdown_render import map_outside_code

    old_title = (old_title or "").strip()
    new_title = (new_title or "").strip()
    if not old_title or not new_title or old_title.lower() == new_title.lower():
        return (0, 0, 0)

    pattern = re.compile(
        r"\[\[\s*" + re.escape(old_title) + r"\s*(\|[^\[\]]*)?\]\]",
        re.IGNORECASE,
    )

    def rewrite(chunk: str) -> str:
        return pattern.sub(
            lambda match: "[[" + new_title + (match.group(1) or "") + "]]", chunk
        )

    updated_notes = 0
    updated_refs = 0
    failed = 0
    for note, hits in link_refs_to(conn, note_id, old_title):
        content = note["content"] or ""
        try:
            new_content = map_outside_code(content, rewrite)
        except Exception:
            failed += 1
            continue
        if new_content == content:
            continue
        try:
            if update_note(conn, note["id"], content=new_content, reason="rename") is not None:
                updated_notes += 1
                updated_refs += hits
            else:
                failed += 1
        except Exception:
            failed += 1
    return (updated_notes, updated_refs, failed)


def search_titles(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 8,
    exclude_id: int | None = None,
) -> list[dict[str, Any]]:
    """按**标题**找笔记（给「[[ ]]」补全和建立联系用，不搜正文）。

    排序：完全相等 > 前缀命中 > 包含，同档内短标题优先、再按最近更新。
    ``query`` 为空时返回最近更新的几篇（一打开就有候选可挑）。
    """
    limit = max(1, min(int(limit or 8), 30))
    if exclude_id is not None:
        try:
            exclude_id = int(exclude_id)
        except (TypeError, ValueError):
            exclude_id = None

    q = (query or "").strip()
    params: list[Any] = []
    where = ["deleted_at IS NULL"]
    if exclude_id is not None:
        where.append("id <> ?")
        params.append(exclude_id)

    if not q:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM notes WHERE "
            + " AND ".join(where)
            + " ORDER BY updated_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # 转义 LIKE 通配符，否则用户输入 % 会命中一切
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like_any = f"%{escaped}%"
    like_prefix = f"{escaped}%"
    rows = conn.execute(
        "SELECT id, title, updated_at,"
        " CASE WHEN title = ? COLLATE NOCASE THEN 0"
        "      WHEN title LIKE ? ESCAPE '\\' COLLATE NOCASE THEN 1"
        "      ELSE 2 END AS rank"
        " FROM notes WHERE "
        + " AND ".join(where)
        + " AND title LIKE ? ESCAPE '\\' COLLATE NOCASE"
        " ORDER BY rank ASC, length(title) ASC, updated_at DESC LIMIT ?",
        (q, like_prefix, *params, like_any, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def outgoing_links(conn: sqlite3.Connection, note_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT l.target_id, l.target_title, n.title AS resolved_title, n.deleted_at, n.is_public"
        " FROM note_links l LEFT JOIN notes n ON n.id = l.target_id"
        " WHERE l.source_id = ? ORDER BY l.target_title COLLATE NOCASE",
        (note_id,),
    ).fetchall()
    result = []
    for row in rows:
        exists = bool(row["target_id"]) and not row["deleted_at"]
        result.append(
            {
                "title": row["resolved_title"] or row["target_title"],
                "note_id": row["target_id"] if exists else None,
                "exists": exists,
                "url": f"/notes/{row['target_id']}" if exists else "",
            }
        )
    return result


def collect_tasks(
    conn: sqlite3.Connection,
    *,
    include_done: bool = False,
    limit_notes: int = 500,
) -> list[dict[str, Any]]:
    """聚合全部笔记里的任务清单（排除回收站与归档），按笔记分组。

    每组的 tasks 里 index 与 /notes/{id}/task-toggle 的序号口径一致，可直接回写。
    """
    from ..markdown_render import extract_tasks

    rows = conn.execute(
        "SELECT id, title, content FROM notes "
        "WHERE deleted_at IS NULL AND is_archived = 0 AND ("
        " content LIKE '%[ ]%' OR content LIKE '%[x]%' OR content LIKE '%[X]%') "
        "ORDER BY updated_at DESC LIMIT ?",
        (max(1, int(limit_notes)),),
    ).fetchall()

    groups: list[dict[str, Any]] = []
    for row in rows:
        items = extract_tasks(row["content"] or "")
        if not items:
            continue
        open_count = sum(1 for item in items if not item["done"])
        done_count = len(items) - open_count
        if open_count == 0 and not include_done:
            continue
        shown = items if include_done else [item for item in items if not item["done"]]
        groups.append(
            {
                "note_id": row["id"],
                "title": row["title"],
                "url": f"/notes/{row['id']}",
                "tasks": shown,
                "open_count": open_count,
                "done_count": done_count,
                "total": len(items),
            }
        )

    # 未完成多的排前面
    groups.sort(key=lambda g: (-g["open_count"], g["title"]))
    return groups


def find_this_day_in_past(
    conn: sqlite3.Connection, *, limit: int = 3
) -> list[dict[str, Any]]:
    """那年今日：往年同月日创建的笔记（排除回收站），新的在前。"""
    import datetime as _dt

    today = _dt.date.today()
    md = today.strftime("%m-%d")
    rows = conn.execute(
        "SELECT * FROM notes WHERE deleted_at IS NULL "
        "AND substr(created_at, 6, 5) = ? AND substr(created_at, 1, 4) < ? "
        "ORDER BY created_at DESC LIMIT ?",
        (md, str(today.year), limit),
    ).fetchall()
    return hydrate(conn, rows)


def daily_note_counts(conn: sqlite3.Connection, days: int = 371) -> dict[str, int]:
    """最近 days 天每天创建的笔记篇数：{'YYYY-MM-DD': n}，排除回收站。"""
    import datetime as _dt

    since = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT substr(created_at, 1, 10) AS d, COUNT(*) AS c FROM notes "
        "WHERE deleted_at IS NULL AND substr(created_at, 1, 10) >= ? GROUP BY d",
        (since,),
    ).fetchall()
    return {row["d"]: int(row["c"]) for row in rows}


def notes_grouped_by_tags(
    conn: sqlite3.Connection,
    tag_names: Sequence[str],
    *,
    per_page: int = 5,
) -> dict[str, list[dict[str, Any]]]:
    """按标签批量取每个标签下最新的几篇（替代「每标签一次 list_notes」的 N+1）。

    排序口径与 list_notes 默认一致（置顶优先，再按更新时间）。
    返回 {标签名: [笔记, ...]}，一篇笔记出现在它命中的每个标签下（与逐个查询行为相同）。
    """
    names = [str(n) for n in tag_names if str(n).strip()][:60]
    if not names:
        return {}
    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"""
        SELECT * FROM (
            SELECT n.*, t.name AS _tag_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY t.name COLLATE NOCASE
                       ORDER BY n.is_pinned DESC, n.updated_at DESC, n.id DESC
                   ) AS _rn
            FROM notes n
            JOIN note_tags nt ON nt.note_id = n.id
            JOIN tags t ON t.id = nt.tag_id
            WHERE n.deleted_at IS NULL AND t.name COLLATE NOCASE IN ({placeholders})
        ) WHERE _rn <= ?
        """,
        [*names, per_page],
    ).fetchall()
    tag_names_in_order = [row["_tag_name"] for row in rows]
    hydrated = hydrate(conn, rows)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for name, note in zip(tag_names_in_order, hydrated):
        grouped.setdefault(name, []).append(note)
    return grouped


def notes_grouped_by_months(
    conn: sqlite3.Connection,
    month_keys: Sequence[str],
    *,
    per_page: int = 100,
    public_only: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """按月份批量取每月最新的几篇公开笔记（替代「每月一次 list_notes」的 N+1）。"""
    keys = [str(k) for k in month_keys if len(str(k)) >= 7][:120]
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    public_clause = " AND is_public = 1" if public_only else ""
    rows = conn.execute(
        f"""
        SELECT * FROM (
            SELECT n.*, substr(n.updated_at, 1, 7) AS _month,
                   ROW_NUMBER() OVER (
                       PARTITION BY substr(n.updated_at, 1, 7)
                       ORDER BY n.updated_at DESC, n.id DESC
                   ) AS _rn
            FROM notes n
            WHERE n.deleted_at IS NULL{public_clause}
        ) WHERE _rn <= ? AND _month IN ({placeholders})
        """,
        [per_page, *keys],
    ).fetchall()
    months_in_order = [row["_month"] for row in rows]
    hydrated = hydrate(conn, rows)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for month, note in zip(months_in_order, hydrated):
        grouped.setdefault(month, []).append(note)
    return grouped


def related_notes(conn: sqlite3.Connection, note: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    """相关笔记 = 标签重合度 * 2 + 双链重合度 * 1。"""
    scores: dict[int, float] = {}
    reasons: dict[int, str] = {}
    notes: dict[int, dict[str, Any]] = {}

    tag_rows = conn.execute(
        "SELECT n.*, COUNT(*) AS shared, GROUP_CONCAT(t.name, '、') AS shared_names"
        " FROM note_tags a JOIN note_tags b ON b.tag_id = a.tag_id AND b.note_id <> a.note_id"
        " JOIN notes n ON n.id = b.note_id JOIN tags t ON t.id = a.tag_id"
        " WHERE a.note_id = ? AND n.deleted_at IS NULL GROUP BY n.id"
        " ORDER BY shared DESC, n.updated_at DESC LIMIT 20",
        (note["id"],),
    ).fetchall()
    for row in tag_rows:
        item = row_to_note(row)
        notes[item["id"]] = item
        scores[item["id"]] = scores.get(item["id"], 0) + 2.0 * float(row["shared"] or 0)
        reasons[item["id"]] = f"共同标签：{row['shared_names']}"

    link_rows = conn.execute(
        "SELECT n.*, COUNT(*) AS shared FROM note_links a"
        " JOIN note_links b ON b.target_title = a.target_title AND b.source_id <> a.source_id"
        " JOIN notes n ON n.id = b.source_id"
        " WHERE a.source_id = ? AND n.deleted_at IS NULL GROUP BY n.id LIMIT 20",
        (note["id"],),
    ).fetchall()
    for row in link_rows:
        item = notes.get(row["id"]) or row_to_note(row)
        notes[item["id"]] = item
        scores[item["id"]] = scores.get(item["id"], 0) + float(row["shared"] or 0)
        if row["id"] not in reasons:
            reasons[row["id"]] = "引用了同一篇笔记"

    tags_map = _tags_map(conn, list(notes.keys()))
    for item in notes.values():
        item["tags"] = tags_map.get(item["id"], [])

    ordered = sorted(
        scores.items(), key=lambda pair: (pair[1], notes[pair[0]]["updated_at"] or ""), reverse=True
    )
    output: list[dict[str, Any]] = []
    for note_id, score in ordered[:limit]:
        item = notes[note_id]
        item["score"] = score
        item["reason"] = reasons.get(note_id, "内容相关")
        output.append(item)
    return output
