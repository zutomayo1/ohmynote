"""数据访问层：笔记、标签、版本、双链、模板、统计。

约定：所有函数都接收一个已打开的 sqlite3.Connection，事务由调用方（db.db() 上下文）负责。
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from . import search as search_mod
from .deps import MAX_SQLITE_INT
from .config import settings
from .markdown_render import WikiRef, make_excerpt, text_stats
from .utils import (
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

SORTS = {
    "updated": ("n.is_pinned DESC, n.updated_at DESC, n.id DESC", "最近更新"),
    "created": ("n.is_pinned DESC, n.created_at DESC, n.id DESC", "创建时间"),
    "words": ("n.is_pinned DESC, n.word_count DESC, n.id DESC", "字数最多"),
    "title": ("n.is_pinned DESC, n.title ASC", "标题"),
}

FLAGS = ("is_public", "is_pinned", "is_starred")
STATUSES = ("draft", "saved")
AUTOSAVE_COALESCE_SECONDS = 600


# ---------------------------------------------------------------------------
# 行 -> 字典
# ---------------------------------------------------------------------------
def row_to_note(row: sqlite3.Row, tags: list[str] | None = None) -> dict[str, Any]:
    note = {key: row[key] for key in row.keys()}
    note["is_public"] = bool(note.get("is_public"))
    note["is_pinned"] = bool(note.get("is_pinned"))
    note["is_starred"] = bool(note.get("is_starred"))
    note["is_archived"] = bool(note.get("is_archived"))
    note["tags"] = tags or []
    note["url"] = f"/notes/{note['id']}"
    note["blog_url"] = f"/blog/{note['slug']}" if note["is_public"] and note.get("slug") else ""
    note["deleted"] = bool(note.get("deleted_at"))
    return note


def _tags_map(conn: sqlite3.Connection, note_ids: Sequence[int]) -> dict[int, list[str]]:
    ids = [int(i) for i in note_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT nt.note_id AS note_id, t.name AS name FROM note_tags nt "
        f"JOIN tags t ON t.id = nt.tag_id WHERE nt.note_id IN ({placeholders}) "
        f"ORDER BY t.name COLLATE NOCASE",
        ids,
    ).fetchall()
    result: dict[int, list[str]] = {i: [] for i in ids}
    for row in rows:
        result.setdefault(row["note_id"], []).append(row["name"])
    return result


def hydrate(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    tags_map = _tags_map(conn, [row["id"] for row in rows])
    return [row_to_note(row, tags_map.get(row["id"], [])) for row in rows]


# ---------------------------------------------------------------------------
# 单篇读取
# ---------------------------------------------------------------------------
def get_note(conn: sqlite3.Connection, note_id: int, *, include_deleted: bool = False) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        return None
    if row["deleted_at"] and not include_deleted:
        return None
    return row_to_note(row, _tags_map(conn, [row["id"]]).get(row["id"], []))


def get_note_by_slug(conn: sqlite3.Connection, slug: str, *, public_only: bool = True) -> dict[str, Any] | None:
    sql = "SELECT * FROM notes WHERE slug = ? AND deleted_at IS NULL"
    params: list[Any] = [slug]
    if public_only:
        sql += " AND is_public = 1"
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return row_to_note(row, _tags_map(conn, [row["id"]]).get(row["id"], []))


def slug_exists(conn: sqlite3.Connection, slug: str, *, exclude_id: int | None = None) -> bool:
    sql = "SELECT 1 FROM notes WHERE slug = ?"
    params: list[Any] = [slug]
    if exclude_id:
        sql += " AND id <> ?"
        params.append(exclude_id)
    return conn.execute(sql, params).fetchone() is not None


def unique_slug(conn: sqlite3.Connection, base: str, *, exclude_id: int | None = None) -> str:
    candidate = sanitize_slug(base) or slugify(base)
    if not candidate:
        candidate = "note"
    if not slug_exists(conn, candidate, exclude_id=exclude_id):
        return candidate
    index = 2
    while index < 500:
        attempt = f"{candidate}-{index}"
        if not slug_exists(conn, attempt, exclude_id=exclude_id):
            return attempt
        index += 1
    return f"{candidate}-{now().strftime('%H%M%S')}"


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def merged_tags(explicit: str | list[str] | None, content: str) -> list[str]:
    """显式标签 + 正文里的 `#标签`，去重后合并。"""
    names = parse_tags(explicit)
    seen = {name.lower() for name in names}
    for inline in extract_inline_tags(content or ""):
        name = inline.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names[:MAX_TAGS]


def set_tags(conn: sqlite3.Connection, note_id: int, names: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = (raw or "").strip().lstrip("#").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        normalized.append(name[:40])
        if len(normalized) >= MAX_TAGS:
            break

    conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
    for name in normalized:
        conn.execute(
            "INSERT INTO tags (name, created_at) VALUES (?, ?) ON CONFLICT (name) DO NOTHING",
            (name, now_iso()),
        )
        row = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        if row is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)", (note_id, row["id"])
        )
    # 清理没有笔记引用的空标签
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM note_tags)")
    return normalized


def sync_derived(conn: sqlite3.Connection, note: dict[str, Any], refs: list[WikiRef] | None = None) -> None:
    """同步全文索引与双链表。"""
    search_mod.sync_note(conn, note["id"], note["title"], note["content"], " ".join(note.get("tags", [])))
    if refs is not None:
        conn.execute("DELETE FROM note_links WHERE source_id = ?", (note["id"],))
        for ref in refs:
            conn.execute(
                "INSERT OR REPLACE INTO note_links (source_id, target_id, target_title) VALUES (?, ?, ?)",
                (note["id"], ref.note_id, ref.title),
            )


def resolve_title(conn: sqlite3.Connection, title: str, *, public_only: bool = False) -> WikiRef | None:
    """按标题找笔记（[[双链]] 用）。找不到返回 None，表示「待创建」。"""
    clean = (title or "").strip()
    if not clean:
        return None
    sql = (
        "SELECT id, title, slug, is_public FROM notes WHERE deleted_at IS NULL "
        "AND title = ? COLLATE NOCASE"
    )
    if public_only:
        sql += " AND is_public = 1"
    sql += " ORDER BY updated_at DESC LIMIT 1"
    row = conn.execute(sql, (clean,)).fetchone()
    if row is None:
        return None
    url = f"/blog/{row['slug']}" if public_only else f"/notes/{row['id']}"
    return WikiRef(
        title=row["title"],
        alias=row["title"],
        note_id=row["id"],
        exists=True,
        url=url,
    )


def make_resolver(conn: sqlite3.Connection, *, public_only: bool = False):
    """带缓存的标题解析器，渲染一篇文章时避免重复查库。"""
    cache: dict[str, WikiRef | None] = {}

    def resolve(title: str) -> WikiRef | None:
        key = (title or "").strip().lower()
        if not key:
            return None
        if key not in cache:
            cache[key] = resolve_title(conn, title, public_only=public_only)
        found = cache[key]
        if found is None:
            return None
        return WikiRef(title=found.title, alias=found.alias, note_id=found.note_id, exists=True, url=found.url)

    return resolve


def _attach_dangling_links(conn: sqlite3.Connection, note: dict[str, Any]) -> None:
    """新建/改名后，把之前指向这个标题的悬空链接接上。"""
    conn.execute(
        "UPDATE note_links SET target_id = ? WHERE target_id IS NULL AND target_title = ? COLLATE NOCASE",
        (note["id"], note["title"]),
    )


def create_note(
    conn: sqlite3.Connection,
    *,
    title: str = "",
    content: str = "",
    tags: str | list[str] | None = None,
    summary: str = "",
    category: str = "",
    meta_description: str = "",
    status: str = "draft",
    is_public: bool = False,
    is_pinned: bool = False,
    is_starred: bool = False,
    slug: str = "",
) -> dict[str, Any]:
    from .markdown_render import render

    title = (title or "").strip() or "无标题笔记"
    status = status if status in STATUSES else "draft"
    if is_public:
        # 公开的东西不该同时是「草稿」，状态自动升为已保存
        status = "saved"
    stamp = now_iso()
    stats = text_stats(content)
    summary = (summary or "").strip() or make_excerpt(content)
    wanted_slug = sanitize_slug(slug)

    cursor = conn.execute(
        "INSERT INTO notes (title, slug, content, summary, category, meta_description, status,"
        " is_public, is_pinned, is_starred, word_count, reading_minutes, created_at, updated_at, published_at)"
        " VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            title,
            content,
            summary,
            category.strip(),
            meta_description.strip(),
            status,
            int(is_public),
            int(is_pinned),
            int(is_starred),
            stats.words,
            stats.minutes,
            stamp,
            stamp,
            stamp if is_public else None,
        ),
    )
    note_id = int(cursor.lastrowid or 0)
    final_slug = unique_slug(conn, wanted_slug or slugify(title) or f"note-{note_id}", exclude_id=note_id)
    conn.execute("UPDATE notes SET slug = ? WHERE id = ?", (final_slug, note_id))

    names = set_tags(conn, note_id, merged_tags(tags, content))
    note = get_note(conn, note_id)
    assert note is not None
    note["tags"] = names
    rendered = render(content, title=title, resolver=make_resolver(conn))
    sync_derived(conn, note, rendered.wikilinks)
    _attach_dangling_links(conn, note)
    return get_note(conn, note_id) or note


def update_note(
    conn: sqlite3.Connection,
    note_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    tags: str | list[str] | None = None,
    summary: str | None = None,
    category: str | None = None,
    meta_description: str | None = None,
    status: str | None = None,
    is_public: bool | None = None,
    is_pinned: bool | None = None,
    is_starred: bool | None = None,
    slug: str | None = None,
    reason: str = "manual",
) -> dict[str, Any] | None:
    from .markdown_render import render

    current = get_note(conn, note_id, include_deleted=True)
    if current is None:
        return None

    new_title = (title if title is not None else current["title"]).strip() or "无标题笔记"
    new_content = content if content is not None else current["content"]
    new_category = (category if category is not None else current["category"]).strip()
    new_meta = (meta_description if meta_description is not None else current["meta_description"]).strip()
    new_status = (status or current["status"]) if (status or current["status"]) in STATUSES else "draft"
    new_public = bool(is_public) if is_public is not None else current["is_public"]
    if new_public:
        new_status = "saved"
    new_pinned = bool(is_pinned) if is_pinned is not None else current["is_pinned"]
    new_starred = bool(is_starred) if is_starred is not None else current["is_starred"]

    stats = text_stats(new_content)
    if summary is not None:
        new_summary = summary.strip() or make_excerpt(new_content)
    elif _looks_auto_summary(current["summary"], current["content"]):
        # 用户没手写过摘要，正文变了就重新自动生成
        new_summary = make_excerpt(new_content)
    else:
        new_summary = current["summary"]

    name_list = merged_tags(tags, new_content) if tags is not None else None
    old_names = current["tags"]
    if name_list is None:
        # 正文里新写的 #标签 也要收进来
        name_list = merged_tags(old_names, new_content)

    changed = (
        new_title != current["title"]
        or new_content != current["content"]
        or new_category != current["category"]
        or new_meta != current["meta_description"]
        or new_summary != current["summary"]
        or sorted(name_list) != sorted(old_names)
    )

    if changed:
        snapshot(conn, current, reason=reason)

    published_at = current["published_at"]
    if new_public and not published_at:
        published_at = now_iso()

    final_slug = current["slug"]
    if slug is not None and sanitize_slug(slug) and sanitize_slug(slug) != current["slug"]:
        final_slug = unique_slug(conn, slug, exclude_id=note_id)
    if not final_slug:
        final_slug = unique_slug(conn, slugify(new_title) or f"note-{note_id}", exclude_id=note_id)

    conn.execute(
        "UPDATE notes SET title = ?, slug = ?, content = ?, summary = ?, category = ?, meta_description = ?,"
        " status = ?, is_public = ?, is_pinned = ?, is_starred = ?, word_count = ?, reading_minutes = ?,"
        " updated_at = ?, published_at = ? WHERE id = ?",
        (
            new_title,
            final_slug,
            new_content,
            new_summary,
            new_category,
            new_meta,
            new_status,
            int(new_public),
            int(new_pinned),
            int(new_starred),
            stats.words,
            stats.minutes,
            now_iso(),
            published_at,
            note_id,
        ),
    )
    if tags is not None or name_list != old_names:
        set_tags(conn, note_id, name_list)

    note = get_note(conn, note_id)
    assert note is not None
    rendered = render(new_content, title=new_title, resolver=make_resolver(conn))
    sync_derived(conn, note, rendered.wikilinks)
    if new_title != current["title"]:
        _attach_dangling_links(conn, note)
    return get_note(conn, note_id)


def _looks_auto_summary(summary: str, content: str) -> bool:
    """判断摘要是「自动生成的」还是「用户手写的」：与自动摘要一致即视为自动。"""
    if not (summary or "").strip():
        return True
    return summary.strip() == make_excerpt(content).strip()


def set_flags(conn: sqlite3.Connection, note_id: int, **flags: Any) -> dict[str, Any] | None:
    """切换 公开 / 置顶 / 星标。首次公开时记录 published_at。

    故意不更新 updated_at：切换开关不算「编辑」。
    """
    note = get_note(conn, note_id)
    if note is None:
        return None
    updates = {key: bool(value) for key, value in flags.items() if key in FLAGS}
    if not updates:
        return note
    assignments = ", ".join(f"{key} = ?" for key in updates)
    params: list[Any] = [int(value) for value in updates.values()]
    if updates.get("is_public"):
        if not note["published_at"]:
            assignments += ", published_at = ?"
            params.append(now_iso())
        if note["status"] != "saved":
            assignments += ", status = 'saved'"
    params.append(note_id)
    conn.execute(f"UPDATE notes SET {assignments} WHERE id = ?", params)
    return get_note(conn, note_id)


def set_archived(conn: sqlite3.Connection, note_id: int, archived: bool) -> dict[str, Any] | None:
    """归档 / 取消归档。不算「编辑」，不更新 updated_at。"""
    note = get_note(conn, note_id)
    if note is None:
        return None
    conn.execute("UPDATE notes SET is_archived = ? WHERE id = ?", (int(bool(archived)), note_id))
    return get_note(conn, note_id)


def touch(conn: sqlite3.Connection, note_id: int) -> None:
    conn.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (now_iso(), note_id))


# ---------------------------------------------------------------------------
# 回收站
# ---------------------------------------------------------------------------
def soft_delete(conn: sqlite3.Connection, note_id: int) -> bool:
    note = get_note(conn, note_id)
    if note is None:
        return False
    conn.execute("UPDATE notes SET deleted_at = ?, is_public = 0 WHERE id = ?", (now_iso(), note_id))
    search_mod.remove_note(conn, note_id)
    return True


def restore(conn: sqlite3.Connection, note_id: int) -> bool:
    note = get_note(conn, note_id, include_deleted=True)
    if note is None or not note["deleted_at"]:
        return False
    conn.execute("UPDATE notes SET deleted_at = NULL, updated_at = ? WHERE id = ?", (now_iso(), note_id))
    fresh = get_note(conn, note_id)
    if fresh:
        sync_derived(conn, fresh, None)
    return True


def purge(conn: sqlite3.Connection, note_id: int) -> bool:
    note = get_note(conn, note_id, include_deleted=True)
    if note is None:
        return False
    search_mod.remove_note(conn, note_id)
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM note_tags)")
    return True


def empty_trash(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT id FROM notes WHERE deleted_at IS NOT NULL").fetchall()
    for row in rows:
        purge(conn, row["id"])
    return len(rows)


def purge_expired_trash(conn: sqlite3.Connection, days: int | None = None) -> int:
    days = days or settings.trash_days
    deadline = (now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT id FROM notes WHERE deleted_at IS NOT NULL AND deleted_at < ?", (deadline,)
    ).fetchall()
    for row in rows:
        purge(conn, row["id"])
    return len(rows)


def trash_days_left(deleted_at: str | None) -> int:
    dt = parse_dt(deleted_at)
    if not dt:
        return settings.trash_days
    elapsed = (now() - dt).days
    return max(0, settings.trash_days - elapsed)


# ---------------------------------------------------------------------------
# 列表 / 检索
# ---------------------------------------------------------------------------
def _apply_python_filters(
    note: dict[str, Any],
    *,
    tag: str = "",
    category: str = "",
    status: str = "",
    fav: str = "",
    month: str = "",
) -> bool:
    if tag and tag.lower() not in {name.lower() for name in note["tags"]}:
        return False
    if category and note["category"] != category:
        return False
    if month and not (note.get("updated_at") or "").startswith(month):
        return False
    if status in STATUSES and note["status"] != status:
        return False
    if fav == "starred" and not note["is_starred"]:
        return False
    if fav == "pinned" and not note["is_pinned"]:
        return False
    if fav == "public" and not note["is_public"]:
        return False
    if fav == "draft" and note["status"] != "draft":
        return False
    return True


def filter_notes(
    notes: list[dict[str, Any]],
    *,
    tag: str = "",
    category: str = "",
    status: str = "",
    fav: str = "",
    month: str = "",
) -> list[dict[str, Any]]:
    """按**与列表页完全一致**的口径筛一批已经取回来的笔记。

    搜索结果页用它：关键词/语义检索负责「找出来」，筛选条件负责「再缩小」，
    复用 `_apply_python_filters` 就不会出现「列表页能筛、搜索页筛不出来」的口径分叉。
    """
    return [
        note
        for note in notes
        if _apply_python_filters(
            note, tag=tag, category=category, status=status, fav=fav, month=month
        )
    ]


# 与 SORTS 里的 ORDER BY 一一对应：置顶优先 + 主键 + id 兜底。
_PY_SORT_KEYS = {
    "updated": ("updated_at", True),
    "created": ("created_at", True),
    "words": ("word_count", True),
    "title": ("title", False),
}


def sort_notes(
    notes: list[dict[str, Any]], *, sort: str = "updated", pinned_first: bool = True
) -> list[dict[str, Any]]:
    """在 Python 侧排序，口径对齐 `SORTS`（置顶优先、同键按 id 倒序）。

    搜索结果默认要保持**相关度**顺序，所以调用方只在用户显式选了排序时才用这个函数。
    """
    field, desc = _PY_SORT_KEYS.get(sort, _PY_SORT_KEYS["updated"])
    if field == "word_count":
        key = lambda note: (int(note.get(field) or 0), note.get("id") or 0)  # noqa: E731
    else:
        key = lambda note: (note.get(field) or "", note.get("id") or 0)  # noqa: E731
    ordered = sorted(notes, key=key, reverse=desc)
    if pinned_first:
        # sorted 是稳定排序，所以「置顶」这一步不会打乱上面排好的相对顺序
        ordered.sort(key=lambda note: not note.get("is_pinned"))
    return ordered


def list_notes(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    tag: str = "",
    category: str = "",
    status: str = "",
    fav: str = "",
    month: str = "",
    sort: str = "updated",
    page: int = 1,
    per_page: int = 12,
    public_only: bool = False,
    include_deleted: bool = False,
    archived: bool | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """返回 (当前页笔记, 命中总数)。带关键词时走全文检索，其余筛选在 Python 里做。

    archived=None 不过滤（搜索等场景）；False 排除归档（默认列表）；True 只看归档。
    """
    # 页码在最里面也夹一次：无论调用方传了什么，都不会出现巨大的 OFFSET
    page = max(1, min(int(page or 1), 1_000_000))
    per_page = max(1, min(int(per_page or 12), 1000))

    if q.strip():
        rows = [
            row
            for row, _score, _snippet in search_mod.search(
                conn, q, public_only=public_only, include_deleted=include_deleted, limit=400
            )
        ]
        notes = hydrate(conn, rows)
        notes = [
            note
            for note in notes
            if (archived is None or bool(note.get("is_archived")) == archived)
            and _apply_python_filters(
                note, tag=tag, category=category, status=status, fav=fav, month=month
            )
        ]
        total = len(notes)
        start = (page - 1) * per_page
        return notes[start : start + per_page], total

    where: list[str] = []
    params: list[Any] = []
    if include_deleted:
        where.append("n.deleted_at IS NOT NULL")
    else:
        where.append("n.deleted_at IS NULL")
    if public_only:
        where.append("n.is_public = 1")
    if status in STATUSES:
        where.append("n.status = ?")
        params.append(status)
    if category:
        where.append("n.category = ?")
        params.append(category)
    if month:
        where.append("substr(n.updated_at, 1, 7) = ?")
        params.append(month)
    if archived is not None:
        where.append("n.is_archived = ?" if archived else "n.is_archived = 0")
        if archived:
            params.append(1)
    if tag:
        where.append(
            "EXISTS (SELECT 1 FROM note_tags nt JOIN tags t ON t.id = nt.tag_id "
            "WHERE nt.note_id = n.id AND t.name = ? COLLATE NOCASE)"
        )
        params.append(tag)
    if fav in {"starred", "pinned", "public"}:
        column = {"starred": "is_starred", "pinned": "is_pinned", "public": "is_public"}[fav]
        where.append(f"n.{column} = 1")

    clause = " AND ".join(where)
    total = int(conn.execute(f"SELECT COUNT(*) AS c FROM notes n WHERE {clause}", params).fetchone()["c"])
    order = SORTS.get(sort, SORTS["updated"])[0]
    if public_only:
        order = order.replace("n.is_pinned DESC, ", "")
    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT n.* FROM notes n WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, per_page, offset],
    ).fetchall()
    return hydrate(conn, rows), total


def search_notes(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 30,
    public_only: bool = False,
) -> list[dict[str, Any]]:
    """给搜索结果页 / 命令面板用：带 snippet 与 score。"""
    results = search_mod.search(conn, query, public_only=public_only, limit=limit)
    if not results:
        return []
    notes = hydrate(conn, [row for row, _score, _snippet in results])
    by_id = {note["id"]: note for note in notes}
    output: list[dict[str, Any]] = []
    for row, score, snippet in results:
        note = by_id.get(row["id"])
        if not note:
            continue
        note["score"] = score
        note["snippet"] = snippet
        note["tokens"] = search_mod.tokenize(query)
        output.append(note)
    return output


def retrieve_notes(conn: sqlite3.Connection, question: str, *, limit: int = 6) -> list[dict[str, Any]]:
    """给「问笔记」用的粗排检索，返回带 snippet 的笔记。"""
    results = search_mod.retrieve(conn, question, limit=limit)
    if not results:
        return []
    notes = hydrate(conn, [row for row, _score, _snippet in results])
    by_id = {note["id"]: note for note in notes}
    output: list[dict[str, Any]] = []
    for row, score, snippet in results:
        note = by_id.get(row["id"])
        if not note:
            continue
        note["score"] = score
        note["snippet"] = snippet
        output.append(note)
    return output


def adjacent_notes(conn: sqlite3.Connection, note: dict[str, Any], *, public_only: bool = False) -> tuple[dict | None, dict | None]:
    """上一篇（更新）/ 下一篇（更旧）。"""
    conditions = ["deleted_at IS NULL"]
    params: list[Any] = []
    if public_only:
        conditions.append("is_public = 1")
    clause = " AND ".join(conditions)
    older = conn.execute(
        f"SELECT * FROM notes WHERE {clause} AND (updated_at < ? OR (updated_at = ? AND id < ?))"
        " ORDER BY updated_at DESC, id DESC LIMIT 1",
        (*params, note["updated_at"], note["updated_at"], note["id"]),
    ).fetchone()
    newer = conn.execute(
        f"SELECT * FROM notes WHERE {clause} AND (updated_at > ? OR (updated_at = ? AND id > ?))"
        " ORDER BY updated_at ASC, id ASC LIMIT 1",
        (*params, note["updated_at"], note["updated_at"], note["id"]),
    ).fetchone()
    return (hydrate(conn, [newer])[0] if newer else None, hydrate(conn, [older])[0] if older else None)


def all_notes(
    conn: sqlite3.Connection,
    *,
    sort: str = "created",
    public_only: bool = False,
) -> list[dict[str, Any]]:
    """不分页地取回全部笔记（导出 / 订阅源用，避免被 per_page 上限截断）。"""
    where = ["n.deleted_at IS NULL"]
    if public_only:
        where.append("n.is_public = 1")
    order = SORTS.get(sort, SORTS["updated"])[0]
    if public_only:
        order = order.replace("n.is_pinned DESC, ", "")
    rows = conn.execute(
        f"SELECT n.* FROM notes n WHERE {' AND '.join(where)} ORDER BY {order}"
    ).fetchall()
    return hydrate(conn, rows)


# ---------------------------------------------------------------------------
# 标签 / 分类 / 归档
# ---------------------------------------------------------------------------
def list_tags(
    conn: sqlite3.Connection,
    *,
    public_only: bool = False,
    limit: int = 200,
    q: str = "",
    sort: str = "",
) -> list[dict[str, Any]]:
    """标签云数据。

    - 默认行为与以前完全一致：按使用次数倒序、名称升序，只含仍被「未删除笔记」引用的标签。
    - `q`：按标签名做大小写不敏感的子串筛选（LIKE，参数化 + 转义）。
    - `sort`：`"name"` 按名称；其余（含默认空串）按使用次数。
    """
    conditions = ["n.deleted_at IS NULL"]
    params: list[Any] = []
    if public_only:
        conditions.append("n.is_public = 1")
    keyword = (q or "").strip()
    if keyword:
        conditions.append("t.name LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like(keyword)}%")
    clause = " AND ".join(conditions)
    # 排序分支只来自固定映射，不接受调用方的任意 SQL 片段。
    order = "t.name COLLATE NOCASE ASC" if sort == "name" else "count DESC, t.name COLLATE NOCASE"
    rows = conn.execute(
        f"SELECT t.name AS name, COUNT(DISTINCT n.id) AS count FROM tags t "
        f"JOIN note_tags nt ON nt.tag_id = t.id JOIN notes n ON n.id = nt.note_id "
        f"WHERE {clause} GROUP BY t.id ORDER BY {order} LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [{"name": row["name"], "count": int(row["count"])} for row in rows]


def _valid_tag_name(raw: str) -> str:
    """校验并规范化「新标签名」，口径与 set_tags / normalize_tag 一致。

    去首尾空白、去开头的 #、把内部连续空白压成一个空格；空名或超过 MAX_TAG_LEN 抛 ValueError。
    """
    name = " ".join((raw or "").strip().lstrip("#").strip().split())
    if not name:
        raise ValueError("标签名不能为空")
    if len(name) > MAX_TAG_LEN:
        raise ValueError(f"标签名不能超过 {MAX_TAG_LEN} 个字符")
    return name


def _find_tag(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """按名字找标签（大小写不敏感）。"""
    return conn.execute(
        "SELECT id, name FROM tags WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()


def _tag_note_count(conn: sqlite3.Connection, tag_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT note_id) AS c FROM note_tags WHERE tag_id = ?", (tag_id,)
    ).fetchone()
    return int(row["c"] or 0)


def rename_tag(conn: sqlite3.Connection, old: str, new: str) -> dict[str, Any]:
    """把标签 old 改名成 new；new 已存在（大小写不敏感）时自动合并。

    合并规则：把 old 上的 note_tags 关系并入 new（`INSERT OR IGNORE` 去重），
    再删除 old 的关系与 tags 行；因此同一篇笔记不会出现重复关系，最终名字取已存在的 new 行。

    大小写处理：`python -> Python` 视为同一标签的「改大小写」，仍会写入新的大小写。
    old 不存在、new 为空/纯空白/超过 MAX_TAG_LEN 都抛 ValueError，且不改动任何数据。
    返回 {"renamed": 1, "merged": bool, "notes": 受影响笔记数, "name": 最终名字}。
    """
    old_name = " ".join((old or "").strip().lstrip("#").strip().split())
    if not old_name:
        raise ValueError("原标签名不能为空")
    new_name = _valid_tag_name(new)

    old_row = _find_tag(conn, old_name)
    if old_row is None:
        raise ValueError(f"标签「{old_name}」不存在")
    old_id = int(old_row["id"])
    notes = _tag_note_count(conn, old_id)

    # 除自己以外，是否已经有同名（忽略大小写）的标签？有 => 合并。
    existing = conn.execute(
        "SELECT id, name FROM tags WHERE name = ? COLLATE NOCASE AND id <> ?",
        (new_name, old_id),
    ).fetchone()
    if existing is None:
        # 单纯改名，包含「只改大小写」：同一行原地更新即可。
        conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, old_id))
        return {"renamed": 1, "merged": False, "notes": notes, "name": new_name}

    target_id = int(existing["id"])
    target_name = str(existing["name"])
    # 关系并集：旧标签已有的笔记全部指向目标标签；目标标签已有的关系靠 OR IGNORE 去重。
    conn.execute(
        "INSERT OR IGNORE INTO note_tags (note_id, tag_id) "
        "SELECT note_id, ? FROM note_tags WHERE tag_id = ?",
        (target_id, old_id),
    )
    conn.execute("DELETE FROM note_tags WHERE tag_id = ?", (old_id,))
    conn.execute("DELETE FROM tags WHERE id = ?", (old_id,))
    return {"renamed": 1, "merged": True, "notes": notes, "name": target_name}


def delete_tag(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """从所有笔记上摘掉标签并删除 tags 行（含孤儿关系）。

    不存在（含空名）时返回 {"deleted": False, "notes": 0}，不抛异常。
    """
    clean = " ".join((name or "").strip().lstrip("#").strip().split())
    row = _find_tag(conn, clean) if clean else None
    if row is None:
        return {"deleted": False, "notes": 0}
    tag_id = int(row["id"])
    notes = _tag_note_count(conn, tag_id)
    conn.execute("DELETE FROM note_tags WHERE tag_id = ?", (tag_id,))
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    return {"deleted": True, "notes": notes}


def purge_unused_tags(conn: sqlite3.Connection) -> int:
    """清掉没有任何 note_tags 关系的残留标签行，返回删除条数。

    list_tags 的 JOIN 本来就不会显示它们，但行会一直滞留在库里；这里集中清理。
    """
    cursor = conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM note_tags)")
    return max(0, int(cursor.rowcount or 0))


def list_categories(conn: sqlite3.Connection, *, public_only: bool = False) -> list[dict[str, Any]]:
    conditions = ["deleted_at IS NULL", "category <> ''"]
    if public_only:
        conditions.append("is_public = 1")
    rows = conn.execute(
        f"SELECT category AS name, COUNT(*) AS count FROM notes WHERE {' AND '.join(conditions)} "
        f"GROUP BY category ORDER BY count DESC, category LIMIT 100"
    ).fetchall()
    return [{"name": row["name"], "count": int(row["count"])} for row in rows]


def archive_months(conn: sqlite3.Connection, *, public_only: bool = True) -> list[dict[str, Any]]:
    conditions = ["deleted_at IS NULL"]
    if public_only:
        conditions.append("is_public = 1")
    rows = conn.execute(
        f"SELECT substr(updated_at, 1, 7) AS ym, COUNT(*) AS count FROM notes "
        f"WHERE {' AND '.join(conditions)} GROUP BY ym ORDER BY ym DESC LIMIT 120"
    ).fetchall()
    months: list[dict[str, Any]] = []
    for row in rows:
        key = row["ym"] or ""
        if len(key) < 7:
            continue
        months.append(
            {
                "key": key,
                "year": key[:4],
                "month": key[5:7],
                "label": month_label(key),
                "count": int(row["count"]),
            }
        )
    return months


# ---------------------------------------------------------------------------
# 版本历史
# ---------------------------------------------------------------------------
def snapshot(conn: sqlite3.Connection, note: dict[str, Any], *, reason: str = "manual", force: bool = False) -> None:
    """把笔记的「当前状态」存成一个历史版本。"""
    if not force and reason == "autosave":
        row = conn.execute(
            "SELECT id, created_at, reason FROM note_versions WHERE note_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (note["id"],),
        ).fetchone()
        if row is not None and row["reason"] == "autosave":
            created = parse_dt(row["created_at"])
            if created and (now() - created).total_seconds() < AUTOSAVE_COALESCE_SECONDS:
                return  # 同一次连续编辑只留一个快照
    conn.execute(
        "INSERT INTO note_versions (note_id, title, content, tags, summary, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            note["id"],
            note["title"],
            note["content"],
            "，".join(note.get("tags") or []),
            note["summary"],
            reason,
            now_iso(),
        ),
    )
    prune_versions(conn, note["id"])


def prune_versions(conn: sqlite3.Connection, note_id: int, keep: int | None = None) -> None:
    keep = keep or settings.version_keep
    rows = conn.execute(
        "SELECT id FROM note_versions WHERE note_id = ? ORDER BY created_at DESC, id DESC", (note_id,)
    ).fetchall()
    for row in rows[keep:]:
        conn.execute("DELETE FROM note_versions WHERE id = ?", (row["id"],))


def list_versions(conn: sqlite3.Connection, note_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, note_id, title, tags, summary, reason, created_at, length(content) AS size"
        " FROM note_versions WHERE note_id = ? ORDER BY created_at DESC, id DESC",
        (note_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_version(conn: sqlite3.Connection, note_id: int, version_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM note_versions WHERE id = ? AND note_id = ?", (version_id, note_id)
    ).fetchone()
    return dict(row) if row else None


def restore_version(conn: sqlite3.Connection, note_id: int, version_id: int) -> dict[str, Any] | None:
    version = get_version(conn, note_id, version_id)
    if version is None:
        return None
    current = get_note(conn, note_id, include_deleted=True)
    if current is None:
        return None
    snapshot(conn, current, reason="before-restore", force=True)
    return update_note(
        conn,
        note_id,
        title=version["title"],
        content=version["content"],
        tags=version["tags"],
        summary=version["summary"] or "",
        reason="restore",
    )


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
    from .markdown_render import extract_tasks

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


# ---------------------------------------------------------------------------
# 笔记模板
# ---------------------------------------------------------------------------
DEFAULT_TEMPLATES: list[tuple[str, str, str]] = [
    ("空白笔记", "只有标题和内容区，适合随手写", ""),
    (
        "读书笔记",
        "书名 / 核心观点 / 摘抄 / 我的思考",
        """## 基本信息

- 书名：
- 作者：
- 评分：★★★★☆
- 读完日期：

## 一句话概括


## 核心观点

1.
2.

## 摘抄

> 

## 我的思考


## 行动清单

- [ ] 
""",
    ),
    (
        "周报",
        "本周完成 / 进行中 / 风险 / 下周计划",
        """## 本周完成

- [ ] 
- [ ] 

## 进行中


## 问题与风险


## 下周计划

- [ ] 
""",
    ),
    (
        "会议记录",
        "议题 / 结论 / 待办",
        """## 会议信息

- 时间：
- 参与人：
- 议题：

## 讨论要点


## 结论


## 待办

- [ ] 事项 —— 负责人 —— 截止时间
""",
    ),
    (
        "技术笔记 / 踩坑记录",
        "问题现象 / 排查过程 / 结论与参考",
        """## 问题现象


## 环境


## 排查过程

1.
2.

## 结论

```text

```

## 参考

- 
""",
    ),
    (
        "每日复盘",
        "三件好事 / 待改进 / 明日计划",
        """## 今天做了什么


## 三件好事

1.
2.
3.

## 待改进


## 明日计划

- [ ] 
""",
    ),
]


def seed_templates(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS c FROM templates").fetchone()
    if row and int(row["c"]) > 0:
        return
    stamp = now_iso()
    for index, (name, description, content) in enumerate(DEFAULT_TEMPLATES):
        conn.execute(
            "INSERT INTO templates (name, description, content, sort_order, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, content, index * 10, stamp, stamp),
        )


def list_templates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM templates ORDER BY sort_order, id"
    ).fetchall()
    return [dict(row) for row in rows]


def get_template(conn: sqlite3.Connection, template_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    return dict(row) if row else None


def save_template(
    conn: sqlite3.Connection,
    *,
    template_id: int | None = None,
    name: str,
    description: str = "",
    content: str = "",
) -> int:
    stamp = now_iso()
    name = (name or "").strip() or "未命名模板"
    if template_id:
        conn.execute(
            "UPDATE templates SET name = ?, description = ?, content = ?, updated_at = ? WHERE id = ?",
            (name, description.strip(), content, stamp, template_id),
        )
        return template_id
    cursor = conn.execute(
        "INSERT INTO templates (name, description, content, sort_order, created_at, updated_at)"
        " VALUES (?, ?, ?, 100, ?, ?)",
        (name, description.strip(), content, stamp, stamp),
    )
    return int(cursor.lastrowid or 0)


def delete_template(conn: sqlite3.Connection, template_id: int) -> None:
    conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))


def reorder_templates(conn: sqlite3.Connection, ids: list[int]) -> int:
    """按传入顺序把 ``templates.sort_order`` 写成 ``0..n-1``（同一事务）。

    约定（与 :func:`pages._parse_template_id` 一致）：整数 id 先过
    ``MAX_SQLITE_INT`` 范围校验，再绑 SQL——超范围的 id 绑参会抛
    ``OverflowError`` 变成 500，所以这里直接跳过。

    - 非整数 / 超 SQLite 整数上限的 id → 跳过（防 500）；
    - 库里不存在的 id → UPDATE 影响 0 行，同样跳过；
    - 返回值 = 实际更新到的行数。
    """
    ordered: list[int] = []
    for raw in ids:
        if isinstance(raw, bool):  # bool 是 int 的子类，单独排除
            continue
        if not isinstance(raw, int):
            continue
        if not 1 <= raw <= MAX_SQLITE_INT:
            continue
        ordered.append(raw)
    if not ordered:
        return 0
    updated = 0
    for index, tid in enumerate(ordered):
        cursor = conn.execute(
            "UPDATE templates SET sort_order = ? WHERE id = ?", (index, tid)
        )
        updated += cursor.rowcount
    return updated


# ---------------------------------------------------------------------------
# 统计面板
# ---------------------------------------------------------------------------
def activity_dates(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT date(created_at) AS d FROM notes "
        "UNION SELECT date(updated_at) AS d FROM notes "
        "UNION SELECT date(created_at) AS d FROM note_versions"
    ).fetchall()
    return {row["d"] for row in rows if row["d"]}


def writing_streak(conn: sqlite3.Connection) -> int:
    days = activity_dates(conn)
    if not days:
        return 0
    today = date.today()
    start = today if today.isoformat() in days else today - timedelta(days=1)
    if start.isoformat() not in days:
        return 0
    streak = 0
    cursor = start
    while cursor.isoformat() in days and streak < 3650:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def dashboard_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END) AS drafts,"
        " SUM(CASE WHEN is_public = 1 THEN 1 ELSE 0 END) AS public_count,"
        " SUM(CASE WHEN is_starred = 1 THEN 1 ELSE 0 END) AS starred,"
        " COALESCE(SUM(word_count), 0) AS words"
        " FROM notes WHERE deleted_at IS NULL"
    ).fetchone()
    today = now().strftime("%Y-%m-%d")
    today_row = conn.execute(
        "SELECT COUNT(*) AS updated, COALESCE(SUM(CASE WHEN date(created_at) = ? THEN word_count ELSE 0 END), 0) AS words"
        " FROM notes WHERE deleted_at IS NULL AND date(updated_at) = ?",
        (today, today),
    ).fetchone()
    trash_row = conn.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE deleted_at IS NOT NULL"
    ).fetchone()
    tag_row = conn.execute("SELECT COUNT(*) AS c FROM tags").fetchone()
    return {
        "total": int(row["total"] or 0),
        "drafts": int(row["drafts"] or 0),
        "saved": int(row["total"] or 0) - int(row["drafts"] or 0),
        "public_count": int(row["public_count"] or 0),
        "starred": int(row["starred"] or 0),
        "words": int(row["words"] or 0),
        "tags": int(tag_row["c"] or 0),
        "trash": int(trash_row["c"] or 0),
        "today_updated": int(today_row["updated"] or 0),
        "today_words": int(today_row["words"] or 0),
        "streak": writing_streak(conn),
        "active_days": len(activity_dates(conn)),
        "reading_minutes": math.ceil(int(row["words"] or 0) / 400) if row["words"] else 0,
    }


def find_notes_by_title(conn: sqlite3.Connection, keyword: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """按标题模糊匹配（「待创建」的反向场景：用标题找已存在的笔记）。"""
    rows = conn.execute(
        "SELECT * FROM notes WHERE deleted_at IS NULL AND title LIKE ? ESCAPE '\\'"
        " ORDER BY updated_at DESC LIMIT ?",
        (f"%{escape_like(keyword)}%", limit),
    ).fetchall()
    return hydrate(conn, rows)


# ---------------------------------------------------------------------------
# 站点配置（存在 meta 表里：设置页写、启动时读）
# ---------------------------------------------------------------------------
def get_meta_map(conn: sqlite3.Connection, prefix: str = "") -> dict[str, str]:
    """取出以 prefix 开头的配置项，键去掉前缀（如 ai.model）。"""
    if prefix:
        pattern = prefix.replace("%", "\\%").replace("_", "\\_") + "%"
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ? ESCAPE '\\'", (pattern,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    return {row["key"][len(prefix):]: row["value"] for row in rows}


def save_meta_map(conn: sqlite3.Connection, values: dict[str, Any], prefix: str = "") -> None:
    for key, value in values.items():
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (f"{prefix}{key}", str(value)),
        )


def delete_meta(conn: sqlite3.Connection, keys: list[str]) -> None:
    for key in keys:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))
