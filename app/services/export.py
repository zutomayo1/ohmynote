"""数据导出：把全部笔记打包成 Markdown 文件（zip），以及单篇导出。

设计目标：导出的文件即使离开这个工具也能直接读——每篇笔记一个 .md，
带 YAML front matter（标题、日期、标签、分类、公开状态），并附带一份 index.md。
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import zipfile
from typing import Any

from .. import repo
from ..config import settings
from ..utils import now_iso, parse_dt

UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_filename(name: str, *, fallback: str = "untitled", max_length: int = 60) -> str:
    cleaned = UNSAFE_NAME_RE.sub("-", (name or "").strip()).strip(" .-")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return (cleaned[:max_length].strip() or fallback)


def front_matter(note: dict[str, Any]) -> str:
    tags = json.dumps(note.get("tags") or [], ensure_ascii=False)
    lines = [
        "---",
        f"title: {json.dumps(note.get('title') or '', ensure_ascii=False)}",
        f"slug: {note.get('slug') or ''}",
        f"created: {note.get('created_at') or ''}",
        f"updated: {note.get('updated_at') or ''}",
        f"status: {note.get('status') or 'draft'}",
        f"public: {'true' if note.get('is_public') else 'false'}",
    ]
    if note.get("category"):
        lines.append(f"category: {json.dumps(note['category'], ensure_ascii=False)}")
    lines.append(f"tags: {tags}")
    if note.get("summary"):
        lines.append(f"summary: {json.dumps(note['summary'], ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def note_markdown(note: dict[str, Any]) -> str:
    return f"{front_matter(note)}\n\n{note.get('content') or ''}".rstrip() + "\n"


def note_filename(note: dict[str, Any]) -> str:
    stamp = parse_dt(note.get("created_at"))
    prefix = stamp.strftime("%Y%m%d") if stamp else "00000000"
    return f"{prefix}-{safe_filename(note.get('title') or '', fallback='note')}.md"


def build_index(notes: list[dict[str, Any]], exported_at: str) -> str:
    lines = [
        f"# {settings.site_title} · 笔记导出",
        "",
        f"导出时间：{exported_at}",
        f"共 {len(notes)} 篇笔记。",
        "",
        "## 目录",
        "",
    ]
    for note in notes:
        marker = " 🌐" if note.get("is_public") else ""
        tags = "、".join(note.get("tags") or [])
        suffix = f" · `{tags}`" if tags else ""
        lines.append(f"- [{note.get('title') or '无标题'}](notes/{note_filename(note)}){marker}{suffix}")
    lines.append("")
    return "\n".join(lines)


def build_zip(conn: sqlite3.Connection) -> bytes:
    """打包全部未删除的笔记（草稿也一起导出，不分页、不截断）。"""
    notes = repo.all_notes(conn, sort="created")
    notes = sorted(notes, key=lambda item: item.get("created_at") or "")
    exported_at = now_iso()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        used: set[str] = set()
        manifest: list[dict[str, Any]] = []
        for note in notes:
            name = note_filename(note)
            index = 2
            while name in used:
                name = f"{name[:-3]}-{index}.md"
                index += 1
            used.add(name)
            archive.writestr(f"notes/{name}", note_markdown(note))
            manifest.append(
                {
                    "id": note["id"],
                    "title": note["title"],
                    "slug": note["slug"],
                    "file": f"notes/{name}",
                    "status": note["status"],
                    "is_public": note["is_public"],
                    "tags": note.get("tags") or [],
                    "word_count": note["word_count"],
                    "created_at": note["created_at"],
                    "updated_at": note["updated_at"],
                }
            )
        archive.writestr("index.md", build_index(notes, exported_at))
        archive.writestr(
            "notes.json",
            json.dumps(
                {
                    "app": "inknote",
                    "count": len(notes),
                    "exported_at": exported_at,
                    "exported_from": settings.site_title,
                    "base_url": settings.base_url,
                    "notes": manifest,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        archive.writestr("README.txt", _readme(len(notes)))
    return buffer.getvalue()


def _readme(count: int) -> str:
    return (
        f"{settings.site_title} 笔记导出\n"
        f"====================\n\n"
        f"共 {count} 篇笔记，每个 .md 文件是一篇，开头是 YAML front matter。\n\n"
        "- index.md    ：目录，按创建时间排列\n"
        "- notes/      ：每篇笔记的 Markdown 原文\n"
        "- notes.json  ：结构化元数据，便于写脚本再处理\n\n"
        "这些文件不依赖本工具，可直接放进 Obsidian / Typora / VS Code / 任何 Markdown 编辑器。\n"
    )
