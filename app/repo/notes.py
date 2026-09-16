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
from .versions import snapshot  # update_note 快照（versions 不反向依赖 notes 的模块级）


# ---------------------------------------------------------------------------
# 单篇读取
# ---------------------------------------------------------------------------
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
    from ..markdown_render import render

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
    from ..markdown_render import render

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
# 置顶顺序
# ---------------------------------------------------------------------------
def reorder_pinned(conn: sqlite3.Connection, ids: list[int]) -> int:
    """按传入顺序把**置顶笔记**的 ``notes.sort_order`` 写成 ``0..n-1``。

    - 只更新 ``is_pinned = 1`` 的行：非置顶笔记的 sort_order 保持 0，
      这样它们的时间/字数排序完全不受影响（sort_order 只当置顶区内的次序键）。
    - id 校验与跳过规则同 :func:`reorder_templates`（防 OverflowError → 500）。
    - 返回值 = 实际更新到的行数。
    """
    # 序号只分给**确实是置顶**的 id：非置顶 / 不存在的不能占号，
    # 否则它们会让后面的置顶笔记整体后移（第一版就栽在这——WHERE 挡住了
    # SQL，挡不住 enumerate 的计数）。
    ordered: list[int] = []
    for raw in ids:
        if isinstance(raw, bool):        # bool 是 int 的子类，单独排除
            continue
        if not isinstance(raw, int):
            continue
        if not 1 <= raw <= MAX_SQLITE_INT:
            continue
        row = conn.execute(
            "SELECT is_pinned FROM notes WHERE id = ?", (raw,)
        ).fetchone()
        if row is None or not row["is_pinned"]:
            continue
        ordered.append(raw)
    if not ordered:
        return 0
    updated = 0
    for index, note_id in enumerate(ordered):
        cursor = conn.execute(
            "UPDATE notes SET sort_order = ? WHERE id = ?", (index, note_id)
        )
        updated += cursor.rowcount or 0
    conn.commit()
    return updated
