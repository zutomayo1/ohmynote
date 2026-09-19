# -*- coding: utf-8 -*-
from __future__ import annotations
"""agent 的工具层：33 个工具闭包 + 声明式 ToolSpec 注册表。

安全属性（writes / confirm / 确认卡片文案 / subject_key）全部声明在
`_TOOL_SPECS` 并由此派生 _WRITE_TOOLS、_ALL_CONFIRMABLE —— 加新工具只改这一处，
不可能再漏配安全属性（2026-09-18 架构升级；description/params 从旧注册表 AST 提取）。
"""
import logging
import re
import sqlite3
from datetime import date, timedelta
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable

from ... import repo

logger = logging.getLogger("inknote.agent")

OBSERVE_LIMIT = 1200
READ_WINDOW = 8000
READ_WINDOW_MAX = 30000
READ_OBSERVE_LIMIT = READ_WINDOW + 600
SEARCH_LIMIT_MAX = 10
CONFIRM_BULK_THRESHOLD = 10
CONFIRM_REWRITE_MIN_CHARS = 200
CONFIRM_REWRITE_RATIO = 0.35


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整声明：功能 + 参数 + 安全属性，单一事实来源。

    confirm: None=不确认；True=机制层总确认；可调用 (params, conn)->bool=条件确认。
    confirm_card: callable(params) -> (label, consequence)，生成确认卡片文案。
    subject_key: 确认卡片主体取哪个参数里的笔记（None=批量/无主体笔记）。
    """

    name: str
    description: str
    params: dict
    run: Callable[[dict], dict]
    observe_limit: int = OBSERVE_LIMIT
    writes: bool = False
    confirm: bool | Callable[[dict, sqlite3.Connection], bool] | None = None
    confirm_card: Callable[[dict], tuple] | None = None
    subject_key: str | None = "note_id"


def _note_brief(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": note.get("id"),
        "title": note.get("title"),
        "tags": note.get("tags") or [],
        "category": note.get("category") or "",
        "is_public": bool(note.get("is_public")),
    }

def _as_int(value: Any) -> int:
    return int(value)

def _opt_int(value: Any, default: int) -> int:
    """可选整数参数：给不出合法整数就用默认值，不抛异常。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _as_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.replace("，", ",").split(",")]
    if not isinstance(value, list):
        return []
    return [str(part).strip() for part in value if str(part).strip()][:10]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

def _heading_lines(content: str) -> list[tuple[int, int, str]]:
    """[(行号, 级别, 标题文字)]，给 rewrite_section 定位小节用。"""
    heads: list[tuple[int, int, str]] = []
    for i, line in enumerate(str(content or "").split("\n")):
        m = _HEADING_RE.match(line.strip())
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip().rstrip("#").strip()))
    return heads

def _locate_section(content: str, section: str) -> tuple[int, int, str] | None:
    """按标题文字定位小节，返回 (标题行号, 结束行号[不含], 原节正文)；找不到返回 None。

    结束行号 = 下一个级别不高于它的标题行（更深的子标题 ### 属于本节）；
    没有下一个标题就是全文末尾。同名小节取第一处。
    """
    heads = _heading_lines(content)
    matches = [(i, lvl, txt) for i, lvl, txt in heads if txt == section]
    if not matches:
        lowered = section.lower()
        matches = [(i, lvl, txt) for i, lvl, txt in heads if txt.lower() == lowered]
    if not matches:
        return None
    idx, _level, _txt = matches[0]
    lines = str(content or "").split("\n")
    end = len(lines)
    for j, lvl2, _t in heads:
        if j > idx and lvl2 <= _level:
            end = j
            break
    old_body = "\n".join(lines[idx + 1:end]).strip()
    return idx, end, old_body


# ---------------------------------------------------------------------------
# 条件确认的判定与卡片文案（被下方 _TOOL_SPECS 引用）
# ---------------------------------------------------------------------------
def _confirm_if_bulk(params: dict, conn: sqlite3.Connection) -> bool:
    ids = params.get("note_ids")
    return isinstance(ids, list) and len(ids) > CONFIRM_BULK_THRESHOLD


def _confirm_mass_replace(params: dict, conn: sqlite3.Connection) -> bool:
    find = str(params.get("find") or "")
    if not find:
        return False
    note = repo.get_note(conn, _as_int(params.get("note_id")))
    if note is None:
        return False
    total = str(note.get("content") or "").count(find)
    count = _opt_int(params.get("count"), 0)
    planned = total if count <= 0 else min(count, total)
    return planned >= CONFIRM_BULK_THRESHOLD


def _confirm_section_rewrite(params: dict, conn: sqlite3.Connection) -> bool:
    section = str(params.get("section") or "").strip().lstrip("#").strip()
    new_body = str(params.get("content") or "").strip("\n")
    note = repo.get_note(conn, _as_int(params.get("note_id")))
    if not section or note is None:
        return False
    located = _locate_section(str(note.get("content") or ""), section)
    if located is None:
        return False
    _idx, _end, old_body = located
    if len(old_body) < CONFIRM_REWRITE_MIN_CHARS:
        return False
    if new_body and old_body == new_body:
        return False
    return SequenceMatcher(None, old_body[:4000], new_body[:4000]).ratio() < CONFIRM_REWRITE_RATIO


def _confirm_big_rewrite(params: dict, conn: sqlite3.Connection) -> bool:
    """整篇重写判定：原文够长且新旧相似度低 → 先确认（防止 agent 把长文整篇换掉）。"""
    content = params.get("content")
    if content is None:
        return False
    note = repo.get_note(conn, _as_int(params.get("note_id")))
    old = str((note or {}).get("content") or "")
    if len(old) < CONFIRM_REWRITE_MIN_CHARS:
        return False
    new = str(content)
    if new.rstrip() == old.rstrip():
        return False
    return SequenceMatcher(None, old[:4000], new[:4000]).ratio() < CONFIRM_REWRITE_RATIO


def _bulk_set_category_card(params: dict) -> tuple:
    ids = params.get("note_ids") if isinstance(params.get("note_ids"), list) else []
    category = str(params.get("category") or "").strip()
    verb = (f"清除 {len(ids)} 篇笔记的分类" if not category
            else f"把 {len(ids)} 篇笔记的分类设为「{category}」")
    return (f"批量设置分类（{len(ids)} 篇）", verb)


def _bulk_replace_card(params: dict) -> tuple:
    ids = params.get("note_ids") if isinstance(params.get("note_ids"), list) else []
    find = str(params.get("find") or "")
    shown = find if len(find) <= 24 else find[:24] + "…"
    return (f"跨篇替换正文（{len(ids)} 篇）",
            f"将把 {len(ids)} 篇笔记正文里的「{shown}」全部替换为新内容")


def _mass_replace_card(params: dict) -> tuple:
    find = str(params.get("find") or "")
    shown = find if len(find) <= 24 else find[:24] + "…"
    return ("批量替换正文", f"将把正文里所有「{shown}」替换为新内容")


def _section_rewrite_card(params: dict) -> tuple:
    section = str(params.get("section") or "").strip()
    return ("重写小节", f"将重写小节「{section}」的内容（标题保留，原文存版本历史）")


# ---------------------------------------------------------------------------
# 工具闭包（verbatim 搬运：全部只依赖 conn）
# ---------------------------------------------------------------------------
def _build_handlers(conn: sqlite3.Connection) -> dict[str, Callable[[dict], dict]]:
    def search_notes(params: dict) -> dict:
        query = str(params.get("query") or "").strip()
        tag = str(params.get("tag") or "").strip()
        category = str(params.get("category") or "").strip()
        status = str(params.get("status") or "").strip()
        days = _opt_int(params.get("days"), 0)
        limit = min(max(_opt_int(params.get("limit"), 8), 1), SEARCH_LIMIT_MAX)
        if not any([query, tag, category, status, days]):
            return {"error": "至少要给一个条件：query / tag / category / status / days"}

        if query:
            candidates = repo.search_notes(conn, query, limit=SEARCH_LIMIT_MAX * 4)
            # 关键词检索按相关度排；再加筛选条件时保持相关度，不再重排
            notes = repo.filter_notes(candidates, tag=tag, category=category, status=status)
        else:
            notes = repo.filter_notes(
                repo.all_notes(conn, sort="updated"), tag=tag, category=category, status=status
            )
        if days:
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            notes = [n for n in notes if str(n.get("updated_at") or "")[:10] >= cutoff]
            if not query:
                notes = repo.sort_notes(notes, sort="updated")
        result = {
            "count": len(notes[:limit]),
            "matched": len(notes),
            "notes": [_note_brief(n) for n in notes[:limit]],
        }
        if not notes:
            result["hint"] = ("零命中：去掉部分条件再试；想按意思找（用词和正文里写的不一样）"
                              "可改用 semantic_search（需要配置向量模型）。")
        return result

    def read_note(params: dict) -> dict:
        note = repo.get_note(conn, _as_int(params.get("note_id")))
        if note is None:
            return {"error": "笔记不存在"}
        content = str(note.get("content") or "")
        total = len(content)
        window = min(max(_opt_int(params.get("max_chars"), READ_WINDOW), 500), READ_WINDOW_MAX)
        offset = min(max(_opt_int(params.get("offset"), 0), 0), total)
        chunk = content[offset : offset + window]
        has_more = offset + len(chunk) < total
        result: dict[str, Any] = {
            **_note_brief(note),
            "summary": note.get("summary") or "",
            "content": chunk,
            "offset": offset,
            "content_chars": total,
            "returned_chars": len(chunk),
            "has_more": has_more,
        }
        if has_more:
            result["next_offset"] = offset + len(chunk)
            result["hint"] = (
                f"正文共 {total} 字，这次给了第 {offset + 1}-{offset + len(chunk)} 字。"
                f"只有确实需要后面的内容才续读 offset={offset + len(chunk)}；否则就动手做正事。"
            )
        else:
            result["hint"] = "整篇已经读完了（has_more=false），不要再读这篇，直接用内容完成任务。"
        return result

    def list_recent(params: dict) -> dict:
        limit = min(max(_as_int(params.get("limit") or 8), 1), 20)
        notes, total = repo.list_notes(conn, page=1, per_page=limit)
        return {"count": len(notes), "total": int(total), "notes": [_note_brief(note) for note in notes]}

    def list_tags(params: dict) -> dict:
        limit = min(max(_opt_int(params.get("limit"), 50), 1), 200)
        tags = repo.list_tags(conn, limit=limit)
        return {
            "count": len(tags),
            "tags": [{"name": t.get("name"), "count": t.get("count")} for t in tags],
        }

    def list_categories(params: dict) -> dict:
        _ = params
        items = repo.list_categories(conn)
        return {
            "count": len(items),
            "categories": [{"name": i.get("name"), "count": i.get("count")} for i in items],
        }

    def note_stats(params: dict) -> dict:
        _ = params
        notes = repo.all_notes(conn, sort="updated")
        categories = repo.list_categories(conn)
        tags = repo.list_tags(conn, limit=200)
        return {
            "notes": len(notes),
            "words": sum(int(n.get("word_count") or 0) for n in notes),
            "drafts": sum(1 for n in notes if n.get("status") == "draft"),
            "published": sum(1 for n in notes if n.get("is_public")),
            "archived": sum(1 for n in notes if n.get("is_archived")),
            "categories": len(categories),
            "tags": len(tags),
            "top_tags": [{"name": t.get("name"), "count": t.get("count")} for t in tags[:5]],
            "recent": [_note_brief(n) for n in notes[:3]],
        }

    def create_note(params: dict) -> dict:
        title = str(params.get("title") or "").strip()[:200]
        content = str(params.get("content") or "")
        if not title and not content.strip():
            return {"error": "标题和内容不能同时为空"}
        note = repo.create_note(
            conn,
            title=title or content.strip().split("\n", 1)[0][:80],
            content=content,
            tags=_as_tags(params.get("tags")),
            category=str(params.get("category") or "").strip()[:80],
            summary=str(params.get("summary") or "").strip()[:300],
            status="saved",
            is_public=bool(params.get("is_public")),
        )
        note = note or {}
        return {"created": True, "note_id": note.get("id"), "title": note.get("title")}

    def update_note(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        kwargs: dict[str, Any] = {"reason": "agent"}
        if params.get("title") is not None:
            kwargs["title"] = str(params["title"]).strip()[:200]
        if params.get("content") is not None:
            kwargs["content"] = str(params["content"])
        if params.get("summary") is not None:
            kwargs["summary"] = str(params["summary"]).strip()[:300]
        if params.get("category") is not None:
            kwargs["category"] = str(params["category"]).strip()[:80]
        if params.get("tags") is not None:
            kwargs["tags"] = _as_tags(params["tags"])
        if params.get("is_public") is not None:
            kwargs["is_public"] = bool(params["is_public"])
        updated = repo.update_note(conn, note_id, **kwargs)
        return {
            "updated": True,
            "note_id": note_id,
            "title": (updated or {}).get("title"),
            "changed": [k for k in kwargs if k != "reason"],
        }

    def add_tags(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        new_tags = _as_tags(params.get("tags"))
        if not new_tags:
            return {"error": "tags 不能为空"}
        merged = list(dict.fromkeys([*(note.get("tags") or []), *new_tags]))
        repo.update_note(conn, note_id, tags=merged, reason="agent")
        return {"updated": True, "note_id": note_id, "tags": merged, "added": new_tags}

    def remove_tags(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        drop = {name.lower() for name in _as_tags(params.get("tags"))}
        if not drop:
            return {"error": "tags 不能为空"}
        current = list(note.get("tags") or [])
        kept = [name for name in current if name.lower() not in drop]
        removed = [name for name in current if name.lower() in drop]
        if not removed:
            return {"updated": False, "note_id": note_id, "tags": current,
                    "note": "这篇笔记本来就没有这些标签，无需改动"}
        repo.update_note(conn, note_id, tags=kept, reason="agent")
        return {"updated": True, "note_id": note_id, "removed": removed, "tags": kept}

    def set_category(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        category = str(params.get("category") or "").strip()[:80]
        repo.update_note(conn, note_id, category=category, reason="agent")
        return {"updated": True, "note_id": note_id, "category": category}

    def publish_note(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        public = bool(params.get("public", True))
        slug = str(params.get("slug") or "").strip()[:120] or None
        repo.update_note(conn, note_id, is_public=public, slug=slug, reason="agent")
        blog_url = f"/blog/{note.get('slug') or slug}" if public and (note.get("slug") or slug) else ""
        return {"updated": True, "note_id": note_id, "is_public": public, "blog_url": blog_url}

    def archive_note(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        if repo.get_note(conn, note_id) is None:
            return {"error": "笔记不存在"}
        archived = bool(params.get("archived", True))
        repo.set_archived(conn, note_id, archived)
        return {"updated": True, "note_id": note_id, "archived": archived}

    def trash_note(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        if repo.get_note(conn, note_id) is None:
            return {"error": "笔记不存在"}
        ok = bool(repo.soft_delete(conn, note_id))
        return {"updated": ok, "note_id": note_id, "trashed": ok,
                "note": "已移入回收站，30 天内可以用 restore_note 找回" if ok else "移入回收站失败"}

    def restore_note(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id, include_deleted=True)
        if note is None:
            return {"error": "笔记不存在"}
        if not note.get("deleted_at"):
            return {"updated": False, "note_id": note_id, "note": "这篇笔记不在回收站里，无需恢复"}
        ok = bool(repo.restore(conn, note_id))
        return {"updated": ok, "note_id": note_id, "restored": ok}

    def pin_note(params: dict) -> dict:
        return _set_flag(params, "is_pinned", "pinned")

    def star_note(params: dict) -> dict:
        return _set_flag(params, "is_starred", "starred")

    def _set_flag(params: dict, column: str, key: str) -> dict:
        note_id = _as_int(params.get("note_id"))
        if repo.get_note(conn, note_id) is None:
            return {"error": "笔记不存在"}
        value = bool(params.get(key, True))
        repo.set_flags(conn, note_id, **{column: value})
        return {"updated": True, "note_id": note_id, key: value}

    def _apply_tags_to_many(params: dict, *, add: bool) -> dict:
        ids = params.get("note_ids")
        if not isinstance(ids, list):
            return {"error": "note_ids 必须是 id 列表（先用 search_notes 找到它们）"}
        note_ids = []
        for raw in ids[:50]:
            value = _as_int(raw)
            if value > 0:
                note_ids.append(value)
        note_ids = list(dict.fromkeys(note_ids))[:50]
        if not note_ids:
            return {"error": "note_ids 里没有合法的笔记 id"}
        tags = _as_tags(params.get("tags"))
        if not tags:
            return {"error": "tags 不能为空"}
        updated, missing = [], []
        for note_id in note_ids:
            note = repo.get_note(conn, note_id)
            if note is None:
                missing.append(note_id)
                continue
            current = list(note.get("tags") or [])
            title = str(note.get("title") or "")
            if add:
                merged = list(dict.fromkeys([*current, *tags]))
                changed = merged != current
                if changed:
                    repo.update_note(conn, note_id, tags=merged, reason="agent")
            else:
                drop = {name.lower() for name in tags}
                kept = [name for name in current if name.lower() not in drop]
                removed = [name for name in current if name.lower() in drop]
                changed = bool(removed)
                if changed:
                    repo.update_note(conn, note_id, tags=kept, reason="agent")
            updated.append({"note_id": note_id, "title": title, "changed": changed})
        return {
            "updated": len([u for u in updated if u["changed"]]),
            "unchanged": len([u for u in updated if not u["changed"]]),
            "missing": missing,
            "tags": tags,
            "notes": updated,
            "hint": "changed=false 表示本来就是这个状态，没有改动。",
        }

    def bulk_add_tags(params: dict) -> dict:
        return _apply_tags_to_many(params, add=True)

    def bulk_remove_tags(params: dict) -> dict:
        return _apply_tags_to_many(params, add=False)

    def append_note(params: dict) -> dict:
        """在笔记末尾追加一段内容。「加一段」不必读全文再整体重写。"""
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        text = str(params.get("content") or "").strip()
        if not text:
            return {"error": "content 不能为空"}
        old = str(note.get("content") or "")
        new = (old.rstrip() + "\n\n" + text) if old.strip() else text
        repo.update_note(conn, note_id, content=new, reason="agent")
        return {"updated": True, "note_id": note_id, "added_chars": len(text),
                "content_chars": len(new), "note": "已在末尾追加，并存了版本历史"}

    def replace_in_note(params: dict) -> dict:
        """查找替换正文片段：逐字匹配，小改动不必整篇重写，也不必先读全文。"""
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        find = str(params.get("find") or "")
        replace_with = str(params.get("replace_with") or "")
        if not find:
            return {"error": "find 不能为空（要与正文逐字一致，包括空格与换行）"}
        if find == replace_with:
            return {"error": "find 与 replace_with 相同，没有要替换的"}
        count = _opt_int(params.get("count"), 0)
        old = str(note.get("content") or "")
        occurrences = old.count(find)
        if occurrences == 0:
            return {"replaced": 0, "note_id": note_id, "title": note.get("title"),
                    "note": "正文里没有找到 find 的内容；要与正文逐字一致，可先用 read_note 核对原文"}
        n = occurrences if count <= 0 else min(count, occurrences)
        new = old.replace(find, replace_with, n)
        repo.update_note(conn, note_id, content=new, reason="agent")
        return {"replaced": n, "occurrences": occurrences, "note_id": note_id,
                "title": note.get("title"), "content_chars": len(new),
                "note": (f"已替换全部 {n} 处" if n == occurrences else f"已替换前 {n} 处（共 {occurrences} 处）")
                        + "，并存了版本历史"}

    def prepend_note(params: dict) -> dict:
        """在笔记开头插入一段内容（如摘要、TL;DR），原有正文全部保留。"""
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        text = str(params.get("content") or "").strip()
        if not text:
            return {"error": "content 不能为空"}
        old = str(note.get("content") or "")
        new = (text + "\n\n" + old.lstrip()) if old.strip() else text
        repo.update_note(conn, note_id, content=new, reason="agent")
        return {"updated": True, "note_id": note_id, "title": note.get("title"),
                "added_chars": len(text), "content_chars": len(new),
                "note": "已在开头插入，并存了版本历史"}

    def rewrite_section(params: dict) -> dict:
        """按标题定位小节，只重写这一节的正文（标题行保留，其它小节不动）。"""
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        section = str(params.get("section") or "").strip().lstrip("#").strip()
        new_body = str(params.get("content") or "").strip("\n")
        if not section:
            return {"error": "section 不能为空（写标题文字，不带 # 号）"}
        if not new_body:
            return {"error": "content 不能为空（新小节正文）；想删掉整节请改用 update_note 并说明"}
        old = str(note.get("content") or "")
        located = _locate_section(old, section)
        if located is None:
            available = [txt for _i, _l, txt in _heading_lines(old)][:15]
            return {"error": f"正文里没有找到标题为「{section}」的小节",
                    "sections": available,
                    "hint": "section 要与标题文字完全一致（不带 # 号）；可先 read_note 看结构，或从 sections 里挑"}
        idx, end, old_body = located
        lines = old.split("\n")
        multi = sum(1 for _i, _l, t in _heading_lines(old) if t == section) > 1
        rebuilt = lines[:idx + 1] + ["", *new_body.split("\n")]
        if end < len(lines):
            rebuilt.append("")
        rebuilt.extend(lines[end:])
        new_content = re.sub(r"\n{3,}", "\n\n", "\n".join(rebuilt)).strip()
        repo.update_note(conn, note_id, content=new_content, reason="agent")
        return {"updated": True, "note_id": note_id, "title": note.get("title"),
                "section": section, "old_body_chars": len(old_body),
                "new_body_chars": len(new_body), "content_chars": len(new_content),
                "multi_matched": multi,
                "note": "已重写该小节（标题行保留），其余内容未动；原文存了版本历史"}

    def bulk_set_category(params: dict) -> dict:
        """给一批笔记设置同一个分类；category 传空字符串 = 清除分类。"""
        ids = params.get("note_ids")
        if not isinstance(ids, list):
            return {"error": "note_ids 必须是 id 列表（先用 search_notes 找到它们）"}
        note_ids: list[int] = []
        for raw in ids[:50]:
            value = _as_int(raw)
            if value > 0:
                note_ids.append(value)
        note_ids = list(dict.fromkeys(note_ids))[:50]
        if not note_ids:
            return {"error": "note_ids 里没有合法的笔记 id"}
        category = str(params.get("category") or "").strip()[:80]
        updated, unchanged, missing, notes = 0, 0, [], []
        for note_id in note_ids:
            note = repo.get_note(conn, note_id)
            if note is None:
                missing.append(note_id)
                continue
            current = str(note.get("category") or "")
            title = str(note.get("title") or "")
            if current == category:
                unchanged += 1
                notes.append({"note_id": note_id, "title": title, "changed": False})
                continue
            repo.update_note(conn, note_id, category=category, reason="agent")
            updated += 1
            notes.append({"note_id": note_id, "title": title, "changed": True})
        return {"updated": updated, "unchanged": unchanged, "missing": missing,
                "category": category, "notes": notes,
                "hint": "changed=false 表示本来就是该分类，没有改动；category 为空表示清除了分类。"}

    def find_similar(params: dict) -> dict:
        """找与某篇相似/可能重复的笔记：语义优先，没配向量时按共同标签/分类/互链打分。"""
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        limit = min(max(_opt_int(params.get("limit"), 6), 1), 10)
        from .. import ai_related
        items: list[dict[str, Any]] | None = None
        engine = "semantic"
        try:
            items = ai_related.related_notes(conn, note, limit=limit)
        except Exception:
            logger.warning("agent find_similar 语义路径失败", exc_info=True)
            items = None
        if not items:
            engine = "keyword"
            tags = {str(t).lower() for t in (note.get("tags") or [])}
            category = str(note.get("category") or "")
            linked = {int(b.get("id")) for b in repo.backlinks(conn, note_id) if b.get("id")}
            scored: list[tuple[float, dict[str, Any]]] = []
            for other in repo.all_notes(conn, sort="updated"):
                oid = int(other.get("id") or 0)
                if not oid or oid == note_id:
                    continue
                otags = {str(t).lower() for t in (other.get("tags") or [])}
                score = 2.0 * len(tags & otags)
                if category and str(other.get("category") or "") == category:
                    score += 1.0
                if oid in linked:
                    score += 3.0
                if score > 0:
                    scored.append((score, other))
            scored.sort(key=lambda pair: (-pair[0], int(pair[1].get("id") or 0)))
            items = [{**other, "score": score} for score, other in scored[:limit]]
        notes_out = []
        for item in (items or [])[:limit]:
            try:
                full = repo.get_note(conn, int(item.get("id")))
            except (TypeError, ValueError):
                continue
            if full is None or int(full["id"]) == note_id:
                continue
            brief = _note_brief(full)
            try:
                brief["score"] = round(float(item.get("score") or 0), 3)
            except (TypeError, ValueError):
                pass
            notes_out.append(brief)
        return {"note_id": note_id, "title": note.get("title"), "engine": engine,
                "count": len(notes_out), "notes": notes_out,
                "hint": "按相似度从高到低；确认内容重复后可用 merge_notes 合并（会生成确认卡片）。"
                        "engine=keyword 表示未配置向量模型，按共同标签/分类/互链打分。"}

    def bulk_replace_text(params: dict) -> dict:
        """跨多篇查找替换正文（逐字匹配，每篇全替换）。影响面大，机制层总是先确认再执行。"""
        ids = params.get("note_ids")
        if not isinstance(ids, list):
            return {"error": "note_ids 必须是 id 列表（先用 search_notes 找到它们）"}
        note_ids: list[int] = []
        for raw in ids[:50]:
            value = _as_int(raw)
            if value > 0:
                note_ids.append(value)
        note_ids = list(dict.fromkeys(note_ids))[:50]
        if not note_ids:
            return {"error": "note_ids 里没有合法的笔记 id"}
        find = str(params.get("find") or "")
        replace_with = str(params.get("replace_with") or "")
        if not find:
            return {"error": "find 不能为空（要与正文逐字一致，包括空格与换行）"}
        if find == replace_with:
            return {"error": "find 与 replace_with 相同，没有要替换的"}
        results: list[dict[str, Any]] = []
        total = 0
        for note_id in note_ids:
            note = repo.get_note(conn, note_id)
            if note is None:
                results.append({"note_id": note_id, "missing": True})
                continue
            old = str(note.get("content") or "")
            n = old.count(find)
            if n == 0:
                results.append({"note_id": note_id, "title": note.get("title"), "replaced": 0})
                continue
            new = old.replace(find, replace_with)
            repo.update_note(conn, note_id, content=new, reason="agent")
            total += n
            results.append({"note_id": note_id, "title": note.get("title"), "replaced": n})
        return {"replaced_total": total,
                "notes_changed": len([r for r in results if r.get("replaced")]),
                "notes_skipped": len([r for r in results if not r.get("replaced")]),
                "results": results,
                "note": "每篇替换前的正文都存了版本历史；replaced=0 表示这篇里没有该片段"}

    def writing_activity(params: dict) -> dict:
        """最近的写作节奏：每天新建几篇、各多少字（回答「最近哪天写得最多」这类问题）。"""
        days = min(max(_opt_int(params.get("days"), 30), 1), 371)
        import datetime as _dt
        since = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
        counts = repo.daily_note_counts(conn, days=days)
        words: dict[str, int] = {}
        for note in repo.all_notes(conn, sort="updated"):
            d = str(note.get("created_at") or "")[:10]
            if d >= since:
                words[d] = words.get(d, 0) + int(note.get("word_count") or 0)
        series = [{"date": d, "notes": counts.get(d, 0), "words": words.get(d, 0)}
                  for d in sorted(set(counts) | set(words)) if d >= since]
        best_notes = max(series, key=lambda x: (x["notes"], x["words"])) if series else None
        best_words = max(series, key=lambda x: x["words"]) if series else None
        return {"days": days, "series": series[-60:],
                "total_notes": sum(x["notes"] for x in series),
                "total_words": sum(x["words"] for x in series),
                "busiest_day": best_notes, "most_words_day": best_words,
                "hint": "notes=当天新建篇数、words=当天新建笔记的字数（按创建日期算，不含回收站）。"}

    def list_trash(params: dict) -> dict:
        limit = min(max(_opt_int(params.get("limit"), 10), 1), 30)
        notes, total = repo.list_notes(conn, page=1, per_page=limit, include_deleted=True)
        items = []
        for note in notes:
            item = _note_brief(note)
            item["days_left"] = repo.trash_days_left(note.get("deleted_at"))
            items.append(item)
        return {"count": len(items), "total": total, "notes": items,
                "hint": "这些笔记在回收站里；restore_note 可恢复，超过剩余天数会被自动清掉。"}

    def get_note_history(params: dict) -> dict:
        """版本历史列表：模型由此拿到 version_id，再决定要不要恢复。"""
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id, include_deleted=True)
        if note is None:
            return {"error": "笔记不存在"}
        versions = repo.list_versions(conn, note_id)[:20]
        return {
            "note_id": note_id,
            "title": note.get("title"),
            "count": len(versions),
            "versions": [{"version_id": v.get("id"), "title": v.get("title"),
                          "reason": v.get("reason"), "created_at": v.get("created_at"),
                          "size": v.get("size")} for v in versions],
            "hint": "restore_version 需要 note_id + version_id；恢复前会自动把当前内容存为新版本，随时可再恢复回来。",
        }

    def restore_version(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        version_id = _as_int(params.get("version_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        restored = repo.restore_version(conn, note_id, version_id)
        if restored is None:
            return {"error": "版本不存在（先用 get_note_history 查到 version_id 再恢复）"}
        return {"restored": True, "note_id": note_id, "version_id": version_id,
                "title": restored.get("title"),
                "note": "已恢复到该版本；恢复前的内容也自动存了版本历史，可再恢复回来"}

    def list_backlinks(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        items = repo.backlinks(conn, note_id)[:20]
        return {"note_id": note_id, "title": note.get("title"), "count": len(items),
                "notes": [_note_brief(n) for n in items],
                "hint": "这些笔记的正文里链接到了本篇（[[双链]]）。"}

    def semantic_search(params: dict) -> dict:
        from .. import ai_embed
        query = str(params.get("query") or "").strip()
        if not query:
            return {"error": "query 不能为空"}
        limit = min(max(_opt_int(params.get("limit"), 6), 1), SEARCH_LIMIT_MAX)
        try:
            hits = ai_embed.retrieve(conn, query, limit=limit)
        except Exception:
            logger.warning("agent semantic_search 失败", exc_info=True)
            hits = None
        if not hits:
            return {"error": "语义检索不可用（向量索引未构建或未配置向量模型），改用 search_notes 关键词检索"}
        notes = []
        for hit in hits[:limit]:
            try:
                brief = _note_brief(hit)
            except Exception:
                continue
            brief["score"] = round(float(hit.get("score") or 0), 3)
            if hit.get("snippet"):
                brief["snippet"] = str(hit["snippet"])[:200]
            notes.append(brief)
        return {"count": len(notes), "query": query, "notes": notes,
                "hint": "语义检索按含义匹配（score 越高越相关），命中词未必出现在正文里。"}

    def merge_notes(params: dict) -> dict:
        """多篇合并进一篇：源笔记进回收站（可恢复），目标原正文存版本历史。"""
        target_id = _as_int(params.get("target_id"))
        target = repo.get_note(conn, target_id)
        if target is None:
            return {"error": "目标笔记不存在"}
        raw_ids = params.get("source_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return {"error": "source_ids 必须是非空的笔记 id 列表（先用 search_notes 找到它们）"}
        source_ids = []
        for raw in raw_ids[:20]:
            value = _as_int(raw)
            if value > 0 and value != target_id:
                source_ids.append(value)
        source_ids = list(dict.fromkeys(source_ids))
        if not source_ids:
            return {"error": "source_ids 里没有合法的源笔记 id（不能包含目标本身）"}
        sources = []
        for source_id in source_ids:
            note = repo.get_note(conn, source_id)
            if note is None:
                return {"error": f"源笔记 #{source_id} 不存在"}
            sources.append(note)
        parts = [str(target.get("content") or "").rstrip()]
        merged_titles = []
        for note in sources:
            title = str(note.get("title") or f"笔记 #{note.get('id')}")
            body = str(note.get("content") or "").strip()
            parts.append(f"## 来自《{title}》\n\n{body}" if body else f"## 来自《{title}》\n\n（原文为空）")
            merged_titles.append(title)
        new_content = "\n\n".join(part for part in parts if part)
        repo.update_note(conn, target_id, content=new_content, reason="agent-merge")
        trashed = []
        for note in sources:
            if repo.soft_delete(conn, int(note["id"])):
                trashed.append({"id": note["id"], "title": note.get("title")})
        return {"merged": True, "target_id": target_id, "target_title": target.get("title"),
                "merged_notes": merged_titles, "trashed": trashed,
                "content_chars": len(new_content),
                "note": "源笔记已移入回收站（30 天内可恢复）；合并前的目标正文存了版本历史"}
    return {"search_notes": search_notes, "read_note": read_note, "list_recent": list_recent, "list_tags": list_tags, "list_categories": list_categories, "note_stats": note_stats, "create_note": create_note, "update_note": update_note, "add_tags": add_tags, "remove_tags": remove_tags, "set_category": set_category, "publish_note": publish_note, "archive_note": archive_note, "trash_note": trash_note, "restore_note": restore_note, "pin_note": pin_note, "star_note": star_note, "_set_flag": _set_flag, "_apply_tags_to_many": _apply_tags_to_many, "bulk_add_tags": bulk_add_tags, "bulk_remove_tags": bulk_remove_tags, "append_note": append_note, "replace_in_note": replace_in_note, "prepend_note": prepend_note, "rewrite_section": rewrite_section, "bulk_set_category": bulk_set_category, "find_similar": find_similar, "bulk_replace_text": bulk_replace_text, "writing_activity": writing_activity, "list_trash": list_trash, "get_note_history": get_note_history, "restore_version": restore_version, "list_backlinks": list_backlinks, "semantic_search": semantic_search, "merge_notes": merge_notes}


# ---------------------------------------------------------------------------
# 声明式注册表：安全属性的单一事实来源（run 由 _make_tools 按名字接上）
# ---------------------------------------------------------------------------
_TOOL_SPECS: dict[str, dict] = {
    "search_notes": dict(description="按关键词和/或标签、分类、状态、最近天数检索笔记，返回 id、标题、标签、分类", params={
               "query": "可选，关键词（搜标题/正文/标签）",
               "tag": "可选，按标签精确筛",
               "category": "可选，按分类精确筛",
               "status": "可选，draft 草稿 / saved 已保存",
               "days": "可选，只看最近 N 天更新过的",
               "limit": "可选，默认 8，最多 10",
           }, subject_key=None),
    "read_note": dict(description="读一篇笔记（一次基本给整篇；超长时可翻页，has_more=false 表示读完）", params={
               "note_id": "必填，笔记 id",
               "offset": "可选，从第几个字开始读（默认 0）",
               "max_chars": "可选，这次读多少字（默认 8000，上限 30000）",
           }, observe_limit=READ_OBSERVE_LIMIT, subject_key='note_id'),
    "list_recent": dict(description="列出最近更新的笔记", params={"limit": "可选，默认 8"}, subject_key=None),
    "list_tags": dict(description="列出所有标签及各自笔记数（想知道有哪些标签、避免拼错时先查这个）", params={"limit": "可选，默认 50"}, subject_key=None),
    "list_categories": dict(description="列出所有分类及各自笔记数", params={}, subject_key=None),
    "note_stats": dict(description="笔记库总览：篇数、总字数、草稿/公开/归档数、分类与标签数量、最近几篇", params={}, subject_key=None),
    "create_note": dict(description="新建一篇笔记", params={"title": "标题", "content": "正文（Markdown）", "tags": "标签列表，可选",
                       "category": "分类，可选", "summary": "摘要，可选", "is_public": "是否公开到博客，默认否"}, writes=True, subject_key='note_id'),
    "update_note": dict(description="修改笔记的标题/正文/摘要/分类/标签（只传要改的字段；tags 是整体替换，自动存版本历史）", params={"note_id": "必填", "title": "可选", "content": "可选", "summary": "可选",
                       "category": "可选", "tags": "可选，整体替换标签", "is_public": "可选"}, writes=True, confirm=_confirm_big_rewrite, confirm_card=lambda params: ("整篇重写笔记", "将把整篇正文替换为新内容（替换前的全文存版本历史）"), subject_key='note_id'),
    "add_tags": dict(description="给笔记追加标签（与现有标签合并，不会覆盖掉原来的）", params={"note_id": "必填", "tags": "要追加的标签列表"}, writes=True, subject_key='note_id'),
    "remove_tags": dict(description="删掉笔记上的指定标签（其它标签保留）", params={"note_id": "必填", "tags": "要删除的标签列表"}, writes=True, subject_key='note_id'),
    "set_category": dict(description="设置笔记的分类（传空字符串表示清除分类）", params={"note_id": "必填", "category": "分类名"}, writes=True, subject_key='note_id'),
    "publish_note": dict(description="把笔记公开到博客（public=false 表示取消公开）", params={"note_id": "必填", "public": "默认 true", "slug": "可选，博客地址名"}, writes=True, confirm=lambda params, conn: bool(params.get("public", True)), confirm_card=lambda params: ("发布到博客", "笔记将公开到博客，任何能访问博客的人都能看到；取消公开即收回"), subject_key='note_id'),
    "archive_note": dict(description="归档 / 取消归档（归档是温和的收起，不是删除）", params={"note_id": "必填", "archived": "默认 true，false 表示取消归档"}, writes=True, subject_key='note_id'),
    "trash_note": dict(description="把笔记移入回收站（软删除，30 天内可恢复）——只在用户明确要求删除时用。"
                          "调用后不会立即执行，会生成确认卡片等用户确认", params={"note_id": "必填"}, writes=True, confirm=True, confirm_card=lambda params: ("移入回收站", "笔记将进入回收站（软删除），30 天内可在回收站恢复，之后自动清除"), subject_key='note_id'),
    "bulk_add_tags": dict(description="给一批笔记批量追加标签（与各自现有标签合并；一次最多 50 篇，先 search_notes 拿 id）", params={"note_ids": "笔记 id 列表", "tags": "要追加的标签列表"}, observe_limit=2600, writes=True, confirm=_confirm_if_bulk, confirm_card=lambda params: (f"批量追加标签（{len(params.get('note_ids') or [])} 篇）", f"将给 {len(params.get('note_ids') or [])} 篇笔记追加指定标签"), subject_key=None),
    "bulk_remove_tags": dict(description="从一批笔记批量删掉指定标签（其它标签保留，一次最多 50 篇）", params={"note_ids": "笔记 id 列表", "tags": "要删除的标签列表"}, observe_limit=2600, writes=True, confirm=_confirm_if_bulk, confirm_card=lambda params: (f"批量删除标签（{len(params.get('note_ids') or [])} 篇）", f"将给 {len(params.get('note_ids') or [])} 篇笔记删除指定标签"), subject_key=None),
    "append_note": dict(description="在笔记末尾追加一段内容（保留原有正文，自动存版本历史）——「加一段」用它，别整篇重写", params={"note_id": "必填", "content": "要追加的正文（Markdown）"}, writes=True, subject_key='note_id'),
    "replace_in_note": dict(description="在正文里查找并替换一段文字（逐字匹配）。小改动用它；命中 "
                          f"{CONFIRM_BULK_THRESHOLD} 处及以上会先生成确认卡片", params={
               "note_id": "必填",
               "find": "要找的原文（逐字一致，含空格换行）",
               "replace_with": "替换成什么（留空 = 删除该片段）",
               "count": "可选，只替换前 N 处；默认全部",
           }, writes=True, confirm=_confirm_mass_replace, confirm_card=lambda params: _mass_replace_card(params), subject_key='note_id'),
    "prepend_note": dict(description="在笔记开头插入一段内容（原有正文全部保留，自动存版本历史）", params={"note_id": "必填", "content": "要插入的正文（Markdown）"}, writes=True, subject_key='note_id'),
    "rewrite_section": dict(description="按标题定位小节，只重写这一节的正文（标题行保留、其它小节不动）。"
                          "大段改写会先生成确认卡片", params={
               "note_id": "必填",
               "section": "小节标题文字（不带 # 号）",
               "content": "新的小节正文（Markdown）",
           }, writes=True, confirm=_confirm_section_rewrite, confirm_card=lambda params: _section_rewrite_card(params), subject_key='note_id'),
    "bulk_set_category": dict(description="给一批笔记设置同一个分类（category 留空 = 清除分类）。"
                          f"超过 {CONFIRM_BULK_THRESHOLD} 篇会先生成确认卡片", params={"note_ids": "必填，id 列表", "category": "分类名；空字符串表示清除"}, writes=True, confirm=_confirm_if_bulk, confirm_card=lambda params: _bulk_set_category_card(params), subject_key=None),
    "find_similar": dict(description="找与某篇相似/可能重复的笔记（语义优先，未配向量则按共同标签/分类/互链打分）；"
                          "确认重复后配合 merge_notes 合并", params={"note_id": "必填", "limit": "可选，默认 6，最多 10"}, subject_key=None),
    "bulk_replace_text": dict(description="跨多篇笔记查找替换正文（逐字匹配，每篇全替换）。影响面大：总会先生成确认卡片，"
                          "用户在页面上确认后才真正执行", params={
               "note_ids": "必填，id 列表（先用 search_notes 找到）",
               "find": "要找的原文（逐字一致）",
               "replace_with": "替换成什么（留空 = 删除该片段）",
           }, writes=True, confirm=True, confirm_card=lambda params: _bulk_replace_card(params), subject_key=None),
    "writing_activity": dict(description="最近 N 天的写作节奏：每天新建几篇、各多少字，并直接给出最忙的一天", params={"days": "可选，默认 30，最多 371"}, observe_limit=3000, subject_key=None),
    "list_trash": dict(description="列出回收站里的笔记（含剩余可恢复天数）", params={"limit": "可选，默认 10"}, subject_key=None),
    "restore_note": dict(description="把笔记从回收站恢复回来", params={"note_id": "必填"}, writes=True, subject_key='note_id'),
    "get_note_history": dict(description="列出笔记的版本历史（version_id、时间、原因、大小）——想撤销修改先查这个", params={"note_id": "必填"}, subject_key=None),
    "restore_version": dict(description="把笔记恢复到某个历史版本（恢复前自动把当前内容存为新版本，可再恢复回来）", params={"note_id": "必填", "version_id": "必填，先 get_note_history 查到"}, writes=True, subject_key='note_id'),
    "list_backlinks": dict(description="列出链接到这篇笔记的其他笔记（[[双链]]引用了它的）", params={"note_id": "必填"}, subject_key=None),
    "semantic_search": dict(description="语义检索：按「意思相近」找笔记，命中词不必出现在正文里（关键词搜不到时用它）", params={"query": "必填，自然语言描述想找的内容", "limit": "可选，默认 6"}, subject_key=None),
    "merge_notes": dict(description="把多篇笔记合并进一篇：源笔记正文并入目标（带来源小节），源笔记移入回收站。"
                          "合并前会生成确认卡片等用户确认", params={"target_id": "必填，合并进哪篇", "source_ids": "必填，被合并的笔记 id 列表"}, writes=True, confirm=True, confirm_card=lambda params: ("合并笔记", "源笔记正文将并入目标笔记，源笔记移入回收站（30 天内可恢复）"), subject_key='target_id'),
    "pin_note": dict(description="置顶 / 取消置顶", params={"note_id": "必填", "pinned": "默认 true，false 表示取消置顶"}, writes=True, subject_key='note_id'),
    "star_note": dict(description="加星标 / 取消星标", params={"note_id": "必填", "starred": "默认 true，false 表示取消星标"}, writes=True, subject_key='note_id'),
}


def _make_tools(conn: sqlite3.Connection) -> dict[str, ToolSpec]:
    handlers = _build_handlers(conn)
    assert set(_TOOL_SPECS) <= set(handlers), set(_TOOL_SPECS) - set(handlers)
    return {name: ToolSpec(name=name, run=handlers[name], **decl)
             for name, decl in _TOOL_SPECS.items()}


def _describe_tools(tools: dict) -> str:
    """把工具清单渲染进系统提示词。"""
    lines = []
    for spec in tools.values():
        params = "、".join(f"{k}（{v}）" for k, v in spec.params.items()) or "无参数"
        lines.append(f"- **{spec.name}**：{spec.description}。参数：{params}")
    return "\n".join(lines)
