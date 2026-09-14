"""公开博客：首页、标签筛选、归档、文章详情。无需登录。"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import repo, search as search_mod
from ..config import settings
from ..deps import PageParam, db_conn
from ..services import content as content_service
from ..templating import render
from ..utils import total_pages

router = APIRouter(tags=["blog"])


@router.get("/blog")
def blog_index(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    q: str = "",
    tag: str = "",
    month: str = "",
    page: PageParam = 1,
):
    per_page = settings.per_page
    notes, total = repo.list_notes(
        conn,
        q=q,
        tag=tag,
        month=month,
        public_only=True,
        page=page,
        per_page=per_page,
    )
    tokens = search_mod.tokenize(q)
    return render(
        request,
        "blog/index.html",
        notes=notes,
        total=total,
        page=max(1, page),
        pages=total_pages(total, per_page),
        tags=repo.list_tags(conn, public_only=True, limit=30),
        months=repo.archive_months(conn)[:12],
        categories=repo.list_categories(conn, public_only=True),
        q=q,
        tag=tag,
        month=month,
        tokens=tokens,
        is_feature=bool(notes) and page == 1 and not (q or tag or month),
    )


@router.get("/blog/archive")
def blog_archive(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    months = repo.archive_months(conn)
    # 批量一次查完（窗口函数按月份分区），替代逐月的 N+1 查询
    grouped = repo.notes_grouped_by_months(conn, [item["key"] for item in months], per_page=100)
    groups = [
        {"month": item, "notes": grouped.get(item["key"], [])}
        for item in months
    ]
    return render(
        request,
        "blog/archive.html",
        groups=groups,
        total=sum(item["count"] for item in months),
    )


@router.get("/blog/tags")
def blog_tags(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    return render(request, "blog/tags.html", tags=repo.list_tags(conn, public_only=True))


@router.get("/blog/{slug}")
def blog_post(request: Request, slug: str, conn: sqlite3.Connection = Depends(db_conn)):
    note = repo.get_note_by_slug(conn, slug, public_only=True)
    if note is None:
        raise HTTPException(status_code=404, detail="这篇文章不存在，或者还没有公开")
    context = content_service.note_page_context(conn, note, public=True)
    return render(request, "blog/post.html", **context)
