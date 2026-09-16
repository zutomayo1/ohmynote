"""公开博客：首页、标签筛选、归档、文章详情。无需登录。"""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import repo, search as search_mod
from ..config import settings
from ..deps import PageParam, current_session, db_conn
from ..services import content as content_service
from ..templating import absolute, render
from ..utils import total_pages

router = APIRouter(tags=["blog"])

# 点赞限频：同一 IP 对同一篇 60 秒内只记一次（内存表，重启即清，防灌水够用）
_LIKE_COOLDOWN = 60.0
_like_seen: dict[str, dict[str, float]] = {}


def _iso(ts: str | None) -> str | None:
    """『YYYY-MM-DD HH:MM:SS』→ ISO8601（结构化数据要求 T 分隔）。"""
    if ts and len(ts) >= 19 and ts[10] == " ":
        return ts[:10] + "T" + ts[11:19]
    return ts


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
    # 阅读计数：匿名访问 +1（作者自己看不计，免得天天刷自己的数字）
    if not current_session(request):
        repo.blog_stats_add_read(conn, note["slug"])
    stats = repo.blog_stats_get(conn, note["slug"])
    published = _iso(note["published_at"] or note["created_at"])
    jsonld = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": note["title"],
        "datePublished": published,
        "dateModified": _iso(note["updated_at"]),
        "author": {"@type": "Person", "name": settings.site_title},
        "inLanguage": "zh-CN",
        "wordCount": int(note["word_count"] or 0),
        "mainEntityOfPage": absolute(note["blog_url"]),
    }
    context = content_service.note_page_context(conn, note, public=True)
    return render(request, "blog/post.html", jsonld=jsonld, stats=stats, **context)


@router.post("/blog/{slug}/like")
def blog_like(request: Request, slug: str, conn: sqlite3.Connection = Depends(db_conn)):
    """公开点赞：匿名可点（blog 路由不挂登录/CSRF），前端 localStorage + 服务端限频双重防重。"""
    note = repo.get_note_by_slug(conn, slug, public_only=True)
    if note is None:
        raise HTTPException(status_code=404, detail="这篇文章不存在，或者还没有公开")
    ip = request.client.host if request.client else "anon"
    now = time.monotonic()
    seen = _like_seen.setdefault(slug, {})
    if len(seen) > 4096:   # 防内存膨胀：整表清掉（只影响限频精度，不影响正确性）
        seen.clear()
    dup = now - seen.get(ip, -_LIKE_COOLDOWN) < _LIKE_COOLDOWN
    if not dup:
        seen[ip] = now
        likes = repo.blog_stats_like(conn, slug)
    else:
        likes = repo.blog_stats_get(conn, slug)["likes"]
    return {"ok": True, "likes": likes, "dup": dup}
