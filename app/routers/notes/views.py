"""笔记的增删改查、状态开关、历史版本、回收站、单篇导出。

路由顺序有讲究：`/notes/new` 必须注册在 `/notes/{note_id}` 之前，
否则 FastAPI 会把 "new" 当成 note_id 去做 int 校验。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ... import repo, search as search_mod
from ...services import graph as graph_service
from ...config import settings
from ...deps import (
    MAX_SQLITE_INT,
    EditParam,
    NoteId,
    PageParam,
    VersionId,
    csrf_protect,
    db_conn,
    require_login,
)
from starlette.concurrency import run_in_threadpool

from ...services import ai, ai_related, note_templates
from ...markdown_render import toggle_task_item
from ...services import note_export
from ...services import content as content_service
from ...services import export as export_service
from ...templating import render
from ...utils import (
    as_bool,
    line_diff,
    safe_next,
    total_pages,
    url_with_params,
    url_with_query,
)

logger = logging.getLogger("inknote.notes")

router = APIRouter(dependencies=[Depends(require_login), Depends(csrf_protect)])

FLAG_FIELDS = {"public": "is_public", "pin": "is_pinned", "star": "is_starred"}

# /notes/batch 支持的批量动作（全部复用 repo 里已有的函数）
BATCH_ACTIONS = (
    "add_tag",
    "remove_tag",
    "publish",
    "unpublish",
    "pin",
    "unpin",
    "star",
    "unstar",
    "trash",
    "archive",
    "unarchive",
    "set_category",
    "backfill_summary",
)

# 「选中当前筛选出的全部 N 篇」时，单次最多处理的篇数（防止一次改掉整个库）。
# 测试里会把它 monkeypatch 成更小的值，所以必须是模块级常量、在函数里按名读取。
BATCH_ALL_LIMIT = 500

# 「有无筛选条件」只看真正会缩小结果集的参数；sort 只影响顺序，fav 只认这几个值。
BATCH_FAV_VALUES = ("starred", "pinned", "public", "draft")


def _form_flag(raw: object) -> bool:
    """表单复选框 / 隐藏域里常见的真值写法（浏览器默认提交的是 "on"）。

    唯一口径在 ``app.utils.as_bool``；这里只是给本模块留个短名字。
    """
    return as_bool(raw)


def _has_batch_filter(
    *,
    q: str = "",
    tag: str = "",
    category: str = "",
    status: str = "",
    fav: str = "",
) -> bool:
    """判断批量「全部筛选结果」是否带了真正的筛选条件（供路由与模板共用，口径一致）。"""
    return bool(
        (q or "").strip()
        or (tag or "").strip()
        or (category or "").strip()
        or (status or "").strip() in repo.STATUSES
        or (fav or "").strip() in BATCH_FAV_VALUES
    )


def _parse_note_ids(raw_ids: list[str]) -> tuple[list[int], int]:
    """把重复的 note_ids 表单字段解析成去重后的合法 id 列表。

    非数字、超出 SQLite 64 位范围、重复出现的条目都会被丢弃并计入「跳过」，
    绝不把非法值传给 SQL 绑参（否则会 OverflowError 变 500）。
    返回 (合法且不重复的 id 列表, 被丢弃的条目数)。
    """
    valid: list[int] = []
    seen: set[int] = set()
    dropped = 0
    for raw in raw_ids or []:
        text = (raw or "").strip()
        # 只接受纯 ASCII 十进制（isdigit() 会把「²」也算进来，int() 却会炸）
        if not text.isascii() or not text.isdigit() or len(text) > 19:
            dropped += 1
            continue
        value = int(text)
        if value < 1 or value > MAX_SQLITE_INT or value in seen:
            dropped += 1
            continue
        seen.add(value)
        valid.append(value)
    return valid, dropped


def _version_counts(conn: sqlite3.Connection, note_ids: list[int]) -> dict[int, int]:
    """一次查询拿到这些笔记各自的版本数（避免每张卡一次 N+1）。"""
    if not note_ids:
        return {}
    placeholders = ",".join("?" for _ in note_ids)
    rows = conn.execute(
        f"SELECT note_id, COUNT(*) AS c FROM note_versions "
        f"WHERE note_id IN ({placeholders}) GROUP BY note_id",
        note_ids,
    ).fetchall()
    return {int(row["note_id"]): int(row["c"]) for row in rows}


def _note_or_404(conn: sqlite3.Connection, note_id: int, *, include_deleted: bool = False) -> dict:
    note = repo.get_note(conn, note_id, include_deleted=include_deleted)
    if note is None:
        raise HTTPException(status_code=404, detail="这篇笔记不存在或已被删除")
    return note



from ._common import (  # noqa: F401  helpers 跨子模块共享
    _form_flag,
    _has_batch_filter,
    _parse_note_ids,
    _version_counts,
    _note_or_404,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# 列表 / 新建
# ---------------------------------------------------------------------------
@router.get("/notes")
def dashboard(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    q: str = "",
    tag: str = "",
    category: str = "",
    status: str = "",
    fav: str = "",
    sort: str = "updated",
    page: PageParam = 1,
    archived: str = "",
):
    per_page = settings.per_page
    only_archived = archived == "1"
    notes, total = repo.list_notes(
        conn,
        q=q,
        tag=tag,
        category=category,
        status=status,
        fav=fav,
        sort=sort,
        page=page,
        per_page=per_page,
        archived=True if only_archived else False,
    )
    tokens = search_mod.tokenize(q)
    # 批量「选中全部筛选结果」：有真正的筛选条件时才允许直接执行，否则要显式确认
    batch_has_filter = _has_batch_filter(
        q=q, tag=tag, category=category, status=status, fav=fav
    )
    # 批量表单的 next：带回当前的筛选 / 搜索 / 分页参数（去掉 flash 用的 msg/kind）
    next_url = url_with_params(
        request.url.path,
        {key: value for key, value in request.query_params.items() if key not in ("msg", "kind")},
    )
    version_counts = _version_counts(conn, [note["id"] for note in notes])
    onthisday = (
        repo.find_this_day_in_past(conn)
        if not only_archived and not q.strip()
        else []
    )
    return render(
        request,
        "notes/list.html",
        onthisday=onthisday,
        only_archived=only_archived,
        notes=notes,
        total=total,
        page=max(1, page),
        pages=total_pages(total, per_page),
        stats=repo.dashboard_stats(conn),
        tags=repo.list_tags(conn, limit=40),
        categories=repo.list_categories(conn),
        q=q,
        tag=tag,
        category=category,
        status=status,
        fav=fav,
        sort=sort,
        tokens=tokens,
        next_url=next_url,
        batch_has_filter=batch_has_filter,
        version_counts=version_counts,
        sorts=repo.SORTS,
        trash_days=settings.trash_days,
    )


@router.get("/notes/today")
def today_note(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    """每日笔记快捷入口：今天的笔记存在就打开，不存在就从「每日笔记」模板建一篇。

    必须注册在 /notes/{note_id} 之前（同 /notes/new 的路由顺序讲究）。
    """
    import datetime as dt

    title = dt.date.today().strftime("%Y-%m-%d")
    for note in repo.find_notes_by_title(conn, title, limit=5):
        if str(note.get("title")) == title:
            return RedirectResponse(f"/notes/{note['id']}/edit", status_code=303)

    tpl = next(
        (t for t in repo.list_templates(conn) if t["name"] == note_templates.DAILY_NAME),
        None,
    )
    content = note_templates.render_variables(tpl["content"]) if tpl else ""
    note = repo.create_note(conn, title=title, content=content, status="draft")
    return RedirectResponse(f"/notes/{note['id']}/edit", status_code=303)


@router.get("/notes/new")
def new_note(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    template: EditParam = 0,
    title: str = "",
):
    tpl = repo.get_template(conn, template) if template else None
    # 没指定模板时，若设置了「默认模板」（/templates 页可设）就自动套用
    if tpl is None and not template:
        default_id = note_templates.default_template_id(conn)
        tpl = repo.get_template(conn, default_id) if default_id else None
    if tpl:
        # 模板变量（{{date}} 等）在套用那一刻渲染
        tpl = {**tpl,
               "name": note_templates.render_variables(tpl["name"]),
               "content": note_templates.render_variables(tpl["content"])}
    note = {
        "id": None,
        "title": title,
        "content": (tpl or {}).get("content", ""),
        "summary": "",
        "category": "",
        "meta_description": "",
        "slug": "",
        "status": "draft",
        "is_public": False,
        "is_pinned": False,
        "is_starred": False,
        "word_count": 0,
        "reading_minutes": 0,
        "created_at": "",
        "updated_at": "",
        "tags": [],
        "url": "",
        "blog_url": "",
    }
    if tpl:
        note["title"] = title or tpl["name"]
    return render(
        request,
        "notes/editor.html",
        note=note,
        is_new=True,
        templates=repo.list_templates(conn),
        current_template=tpl,
        known_tags=[item["name"] for item in repo.list_tags(conn, limit=60)],
        categories=repo.list_categories(conn),
    )


@router.post("/notes/{note_id}/task-toggle")
def task_toggle(
    request: Request,
    note_id: int,
    conn: sqlite3.Connection = Depends(db_conn),
    index: int = Form(-1),
):
    """点击阅读视图里的任务复选框，把第 index 个任务标记的勾选态写回正文。

    走 repo.update_note（reason="task-toggle"），版本历史自动兜底。
    """
    if index < 0:
        return JSONResponse({"ok": False, "error": "参数不合法"}, status_code=400)
    note = repo.get_note(conn, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    result = toggle_task_item(str(note.get("content") or ""), index)
    if result is None:
        return JSONResponse({"ok": False, "error": "任务序号超出范围"}, status_code=400)
    new_content, checked = result
    repo.update_note(conn, note_id, content=new_content, reason="task-toggle")
    return JSONResponse({"ok": True, "checked": checked})


@router.get("/notes/{note_id}/export.html")
def export_note_html(
    request: Request,
    note_id: int,
    conn: sqlite3.Connection = Depends(db_conn),
):
    """导出单文件精美 HTML（自包含样式，亮暗跟随系统）。"""
    note = repo.get_note(conn, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    rendered = content_service.render_note(conn, note)
    body = note_export.build_export_html(note, rendered.html)
    from urllib.parse import quote

    name = str(note.get("title") or f"note-{note_id}").strip() or f"note-{note_id}"
    filename = quote(f"{name}.html")
    return Response(
        content=body,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.post("/notes/{note_id}/save-as-template")
def save_note_as_template(
    request: Request,
    note_id: int,
    conn: sqlite3.Connection = Depends(db_conn),
    title: str = Form(""),
    content: str = Form(""),
):
    """把当前笔记的内容一键存成模板（编辑器「存为模板」按钮）。"""
    note = repo.get_note(conn, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    name = (title or str(note.get("title") or "")).strip() or "未命名模板"
    template_id = repo.save_template(
        conn, name=name, description="来自笔记《%s》" % note.get("title"),
        content=str(content or note.get("content") or ""),
    )
    return RedirectResponse(url_with_query("/templates", msg="已存为模板「%s」" % name), status_code=303)


@router.post("/notes")
def create_note(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    title: str = Form(""),
    content: str = Form(""),
    tags: str = Form(""),
    summary: str = Form(""),
    category: str = Form(""),
    meta_description: str = Form(""),
    slug: str = Form(""),
    action: str = Form("save"),
    is_public: str | None = Form(None),
    is_pinned: str | None = Form(None),
    is_starred: str | None = Form(None),
):
    note = repo.create_note(
        conn,
        title=title,
        content=content,
        tags=tags,
        summary=summary,
        category=category,
        meta_description=meta_description,
        slug=slug,
        status="draft" if action == "draft" else "saved",
        is_public=as_bool(is_public),
        is_pinned=as_bool(is_pinned),
        is_starred=as_bool(is_starred),
    )
    note_id = note["id"]
    if action == "view":
        return RedirectResponse(
            url_with_query(f"/notes/{note_id}", msg="笔记已创建"), status_code=303
        )
    return RedirectResponse(
        url_with_query(f"/notes/{note_id}/edit", msg="草稿已保存，可以继续写"), status_code=303
    )
