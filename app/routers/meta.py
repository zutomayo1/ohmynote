"""站点级公开资源：Atom / RSS / sitemap / robots / favicon。"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from .. import repo
from ..config import settings
from ..deps import db_conn
from ..services import content as content_service
from ..services import ai
from ..services import feeds
from ..services import pwa

router = APIRouter(tags=["meta"])

FEED_LIMIT = 30


def _feed_entries(conn: sqlite3.Connection, limit: int | None = FEED_LIMIT) -> list[dict]:
    """订阅源用：带渲染后的正文。不分页取公开笔记，避免被每页上限截断。"""
    notes = repo.all_notes(conn, sort="updated", public_only=True)
    if limit:
        notes = notes[:limit]
    entries = []
    for note in notes:
        rendered = content_service.render_note(conn, note, public=True)
        entries.append({"note": note, "html": rendered.html})
    return entries


@router.get("/feed.xml")
def atom(conn: sqlite3.Connection = Depends(db_conn)):
    payload = feeds.atom_feed(_feed_entries(conn))
    return Response(content=payload, media_type="application/atom+xml; charset=utf-8")


@router.get("/rss.xml")
def rss(conn: sqlite3.Connection = Depends(db_conn)):
    payload = feeds.rss_feed(_feed_entries(conn))
    return Response(content=payload, media_type="application/rss+xml; charset=utf-8")


@router.get("/sitemap.xml")
def sitemap(conn: sqlite3.Connection = Depends(db_conn)):
    # sitemap 不需要正文，只取笔记元数据
    notes = repo.all_notes(conn, sort="updated", public_only=True)
    payload = feeds.sitemap(
        [{"note": note, "html": ""} for note in notes],
        extra_paths=["/blog", "/blog/archive", "/blog/tags"],
    )
    return Response(content=payload, media_type="application/xml; charset=utf-8")


@router.get("/robots.txt")
def robots():
    return PlainTextResponse(feeds.robots_txt(), media_type="text/plain; charset=utf-8")


@router.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/favicon.svg", status_code=301)


@router.get("/manifest.webmanifest")
def webmanifest():
    """PWA manifest：安装到桌面 / 手机主屏。

    必须 no-cache：站点名 / 配色改了要立刻反映（浏览器重取 manifest 很便宜）。
    """
    payload = json.dumps(pwa.manifest_payload(), ensure_ascii=False, indent=2)
    return Response(
        payload,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/sw.js")
def service_worker():
    """Service Worker 脚本（离线阅读）。

    同样 no-cache：SW 内容变化才会触发浏览器安装新版本、清掉旧缓存；
    浏览器本身对 SW 脚本的缓存上限是 24 小时，显式 no-cache 更可控。
    """
    return Response(
        pwa.service_worker_js(),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/health")
def health(conn: sqlite3.Connection = Depends(db_conn)):
    count = conn.execute("SELECT COUNT(*) AS c FROM notes").fetchone()["c"]
    return {
        "ok": True,
        "site": settings.site_title,
        "notes": int(count),
        "ai": ai.is_enabled(),
    }


@router.get("/about")
def about(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    from ..templating import render

    posts, total = repo.list_notes(conn, public_only=True, per_page=6)
    return render(
        request,
        "blog/about.html",
        posts=posts,
        total=total,
        tags=repo.list_tags(conn, public_only=True, limit=20),
        stats=repo.dashboard_stats(conn),
    )
