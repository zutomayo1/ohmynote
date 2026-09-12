"""笔记正文渲染与详情页上下文（个人详情页与公开博客页共用）。

公开页面的双链只指向已公开的笔记，并且换成 /blog/<slug> 地址，
不会泄露草稿的存在。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import repo
from ..markdown_render import render as render_markdown


def render_note(conn: sqlite3.Connection, note: dict[str, Any], *, public: bool = False):
    """public=True 时只解析已公开的笔记，链接指向 /blog/<slug>。"""
    resolver = repo.make_resolver(conn, public_only=public)
    return render_markdown(note.get("content") or "", title=note.get("title"), resolver=resolver)


def _to_public_urls(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """公开页面上的链接必须指向 /blog/<slug>，否则点进去会被登录墙挡住。"""
    for item in items:
        if item.get("blog_url"):
            item["url"] = item["blog_url"]
    return items


def note_page_context(conn: sqlite3.Connection, note: dict[str, Any], *, public: bool = False) -> dict[str, Any]:
    """笔记详情 / 博客文章页需要的全部数据。"""
    rendered = render_note(conn, note, public=public)
    previous, following = repo.adjacent_notes(conn, note, public_only=public)

    if public:
        # 只保留已公开的推荐/反链，并把地址统一换成博客地址
        related = _to_public_urls(
            [
                item
                for item in repo.related_notes(conn, note, limit=12)
                if item["is_public"] and item["blog_url"]
            ]
        )[:5]
        backlinks = _to_public_urls(repo.backlinks(conn, note["id"], public_only=True))
    else:
        related = repo.related_notes(conn, note, limit=5)
        backlinks = repo.backlinks(conn, note["id"])

    outgoing = repo.outgoing_links(conn, note["id"])
    missing = [item for item in outgoing if not item["exists"]]

    return {
        "note": note,
        "rendered": rendered,
        "html": rendered.html,
        "toc": rendered.toc,
        "previous": previous,
        "next": following,
        "related": related,
        "backlinks": backlinks,
        "outgoing": outgoing,
        "missing": missing,
    }
