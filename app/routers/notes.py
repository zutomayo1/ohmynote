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

from .. import repo, search as search_mod
from ..services import graph as graph_service
from ..config import settings
from ..deps import (
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

from ..services import ai, ai_related, note_templates
from ..markdown_render import toggle_task_item
from ..services import note_export
from ..services import content as content_service
from ..services import export as export_service
from ..templating import render
from ..utils import (
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


# ---------------------------------------------------------------------------
# 批量操作
# ---------------------------------------------------------------------------
@router.post("/notes/batch")
async def batch_notes(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
):
    """一次处理多篇笔记：打/删标签、公开/取消、置顶/取消、星标/取消、归档/取消归档、
    设为分类（action_category）、补摘要（backfill_summary）、移入回收站。

    表单字段：note_ids（可重复）、action、可选 action_tag、next。
    还支持 all=1 + 当前筛选参数（q / tag / category / status / fav / sort），意思是
    「把符合这些条件的全部 id 取出来处理」——过滤逻辑直接复用 repo.list_notes。
    all=1 且没有任何筛选条件时必须 confirm_all=1 显式确认，单次最多 BATCH_ALL_LIMIT 篇。
    非数字 / 超过 SQLite 64 位 / 重复 / 查不到的 id 一律跳过，
    最后 flash 汇报「已处理 N 篇、跳过 M 篇」，绝不因为脏数据 500。
    """
    form = await request.form()  # csrf_protect 已解析过，这里直接复用缓存
    action = str(form.get("action") or "")
    # 批量动作要用的标签名走 action_tag；老调用方只传 tag，这里保持兼容。
    raw_action_tag = form.get("action_tag")
    if raw_action_tag is None:
        raw_action_tag = form.get("tag") or ""
    tag_name = str(raw_action_tag).strip().lstrip("#").strip()
    category_name = str(form.get("action_category") or "").strip()[:80]
    target = safe_next(str(form.get("next") or ""), "/notes")
    note_ids = [str(value) for value in form.getlist("note_ids")]

    if action not in BATCH_ACTIONS:
        raise HTTPException(status_code=400, detail="未知的批量操作")

    # ---- 「选中当前筛选出的全部 N 篇」：按同一套条件取 id（不另写 SQL）----
    overflow = 0
    if _form_flag(form.get("all")):
        filters = {
            "q": str(form.get("q") or "").strip(),
            "tag": str(form.get("tag") or "").strip().lstrip("#").strip(),
            "category": str(form.get("category") or "").strip(),
            "status": str(form.get("status") or "").strip(),
            "fav": str(form.get("fav") or "").strip(),
            "sort": str(form.get("sort") or "").strip() or "updated",
        }
        has_filter = _has_batch_filter(
            q=filters["q"],
            tag=filters["tag"],
            category=filters["category"],
            status=filters["status"],
            fav=filters["fav"],
        )
        if not has_filter and not _form_flag(form.get("confirm_all")):
            # 一个筛选条件都没有 = 会动整个库，必须显式确认，避免误点
            return RedirectResponse(
                url_with_query(target, msg="这会处理全部笔记，请勾选『全部筛选结果』确认"),
                status_code=303,
            )
        matched, matched_total = repo.list_notes(
            conn,
            q=filters["q"],
            tag=filters["tag"],
            category=filters["category"],
            status=filters["status"],
            fav=filters["fav"],
            sort=filters["sort"],
            page=1,
            per_page=BATCH_ALL_LIMIT,  # 上限内的 id 才进入处理
        )
        note_ids = [str(note["id"]) for note in matched]
        overflow = max(0, int(matched_total) - len(note_ids))

    if action in {"add_tag", "remove_tag"} and not tag_name:
        return RedirectResponse(
            url_with_query(target, msg="没有填写标签名，未执行任何操作"), status_code=303
        )

    ids, skipped = _parse_note_ids(note_ids)

    # 「补摘要」是唯一要调模型的动作：每篇一次，单次上限 BACKFILL_LIMIT，剩下的提示再点一次。
    # 必须丢线程池 —— batch_notes 是 async，同步的 urllib 模型调用会把事件循环一起卡住，
    # 博客访客的请求也会被拖住（和 agent 循环是同一类问题）。
    if action == "backfill_summary":
        if not ai.is_enabled():
            return RedirectResponse(
                url_with_query(target, msg="还没配置 AI 服务，无法补摘要", kind="warn"),
                status_code=303,
            )
        result = await run_in_threadpool(
            ai.backfill_summaries, conn, note_ids=ids, limit=ai.BACKFILL_LIMIT
        )
        parts = [f"已补 {result['done']} 篇摘要"]
        if result["skipped"]:
            parts.append(f"{result['skipped']} 篇本来就有摘要")
        if result["failed"]:
            parts.append(f"{result['failed']} 篇失败")
        if result["remaining"]:
            parts.append(f"还剩 {result['remaining']} 篇，可以再点一次")
        return RedirectResponse(
            url_with_query(target, msg="，".join(parts), kind="warn" if result["failed"] else "ok"),
            status_code=303,
        )

    done = 0
    for note_id in ids:
        note = repo.get_note(conn, note_id)  # 不存在 / 已在回收站 -> None -> 跳过
        if note is None:
            skipped += 1
            continue
        if action == "add_tag":
            names = list(note["tags"])
            if tag_name.casefold() not in {name.casefold() for name in names}:
                names.append(tag_name)
            repo.set_tags(conn, note_id, names)
        elif action == "remove_tag":
            names = [name for name in note["tags"] if name.casefold() != tag_name.casefold()]
            repo.set_tags(conn, note_id, names)
        elif action == "publish":
            repo.set_flags(conn, note_id, is_public=True)
        elif action == "unpublish":
            repo.set_flags(conn, note_id, is_public=False)
        elif action == "pin":
            repo.set_flags(conn, note_id, is_pinned=True)
        elif action == "unpin":
            repo.set_flags(conn, note_id, is_pinned=False)
        elif action == "star":
            repo.set_flags(conn, note_id, is_starred=True)
        elif action == "unstar":
            repo.set_flags(conn, note_id, is_starred=False)
        elif action == "archive":
            repo.set_archived(conn, note_id, True)
        elif action == "unarchive":
            repo.set_archived(conn, note_id, False)
        elif action == "set_category":
            repo.update_note(conn, note_id, category=category_name, reason="batch-category")
        elif action == "trash":
            if not repo.soft_delete(conn, note_id):
                skipped += 1
                continue
        done += 1

    msg = f"已处理 {done} 篇、跳过 {skipped} 篇"
    if overflow:
        msg += f"（另有 {overflow} 篇超出单次上限 {BATCH_ALL_LIMIT}，未处理）"
    return RedirectResponse(url_with_query(target, msg=msg), status_code=303)


# ---------------------------------------------------------------------------
# 回收站
# ---------------------------------------------------------------------------
@router.get("/trash")
def trash(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    page: PageParam = 1,
):
    per_page = settings.per_page
    notes, total = repo.list_notes(conn, include_deleted=True, page=page, per_page=per_page)
    for note in notes:
        note["days_left"] = repo.trash_days_left(note["deleted_at"])
    return render(
        request,
        "trash.html",
        notes=notes,
        total=total,
        page=max(1, page),
        pages=total_pages(total, per_page),
        trash_days=settings.trash_days,
    )


@router.post("/trash/empty")
def empty_trash(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    count = repo.empty_trash(conn)
    return RedirectResponse(
        url_with_query("/trash", msg=f"已彻底删除 {count} 篇笔记"), status_code=303
    )


# ---------------------------------------------------------------------------
# 详情 / 编辑
# ---------------------------------------------------------------------------
@router.get("/notes/{note_id}")
def note_detail(request: Request, note_id: NoteId, conn: sqlite3.Connection = Depends(db_conn)):
    note = _note_or_404(conn, note_id)
    context = content_service.note_page_context(conn, note, public=False)
    # 语义相关笔记优先；拿不到（未配置向量模型 / 没建索引 / 服务异常）原样回退关键词推荐
    related_engine = "keyword"
    semantic = None
    try:
        semantic = ai_related.related_notes(conn, note, limit=5)
    except Exception:
        # 语义推荐失败不能静默：留 warning，页面仍用关键词结果
        logger.warning("语义相关推荐失败，回退关键词推荐（note_id=%s）", note_id, exc_info=True)
    if semantic:
        context["related"] = semantic
        related_engine = "semantic"
    versions = repo.list_versions(conn, note_id)
    # 局部关系图：这篇笔记的邻居（借鉴 Obsidian / Quartz 的 local graph）；
    # ?graph_depth=2 看两跳（邻居的邻居），默认 1 跳
    try:
        graph_depth = int(request.query_params.get("graph_depth") or 1)
    except ValueError:
        graph_depth = 1
    graph_depth = 1 if graph_depth < 2 else 2
    local_graph = graph_service.ego_graph(conn, note_id, depth=graph_depth)
    return render(
        request,
        "notes/detail.html",
        versions=versions[:5],
        version_count=len(versions),
        related_engine=related_engine,
        local_graph=local_graph,
        graph_depth=graph_depth,
        local_graph_json=(
            json.dumps(local_graph, ensure_ascii=False).replace("</", "<\\/")
            if local_graph
            else ""
        ),
        **context,
    )


@router.get("/notes/{note_id}/edit")
def edit_note(request: Request, note_id: NoteId, conn: sqlite3.Connection = Depends(db_conn)):
    note = _note_or_404(conn, note_id)
    rename_from = (request.query_params.get("rename_from") or "").strip()
    rename_ref_notes = (
        [(n["title"], hits) for n, hits in repo.link_refs_to(conn, note_id, rename_from)]
        if rename_from
        else []
    )
    return render(
        request,
        "notes/editor.html",
        note=note,
        is_new=False,
        templates=repo.list_templates(conn),
        current_template=None,
        known_tags=[item["name"] for item in repo.list_tags(conn, limit=60)],
        categories=repo.list_categories(conn),
        rename_from=rename_from if rename_ref_notes else "",
        rename_ref_notes=rename_ref_notes,
    )


@router.post("/notes/reorder")
async def reorder_pinned_notes(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    """列表页拖动置顶笔记后写回顺序（只影响置顶区，非置顶笔记不动）。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体必须是 JSON"}, status_code=400)
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list):
        return JSONResponse({"ok": False, "error": "ids 必须是数组"}, status_code=400)
    updated = repo.reorder_pinned(conn, ids)
    return JSONResponse({"ok": True, "updated": updated})


@router.post("/notes/{note_id}")
def update_note(
    request: Request,
    note_id: NoteId,
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
    existing = _note_or_404(conn, note_id, include_deleted=True)
    if existing["deleted_at"]:
        raise HTTPException(status_code=409, detail="这篇笔记在回收站里，请先恢复再编辑")
    note = repo.update_note(
        conn,
        note_id,
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
        reason="manual",
    )
    if note is None:
        raise HTTPException(status_code=404, detail="这篇笔记不存在")

    # 标题改了：正文里引用旧标题的 [[链接]] 不会自己跟上，带回去让用户一键更新
    old_title = (existing["title"] or "").strip()
    new_title = (note["title"] or "").strip()
    rename_from = ""
    if old_title and new_title and old_title != new_title:
        if repo.link_refs_to(conn, note_id, old_title):
            rename_from = old_title

    if action == "view":
        return RedirectResponse(
            url_with_query(f"/notes/{note_id}", msg="已保存", rename_from=rename_from),
            status_code=303,
        )
    return RedirectResponse(
        url_with_query(f"/notes/{note_id}/edit", msg="已保存", rename_from=rename_from),
        status_code=303,
    )


@router.post("/notes/{note_id}/rename-links")
def rename_note_links(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    old_title: str = Form(""),
):
    """把引用了旧标题的 [[链接]] 一并改写成新标题（别名保留、走版本历史）。"""
    note = _note_or_404(conn, note_id)
    old_title = (old_title or "").strip()
    duplicate = conn.execute(
        "SELECT id FROM notes WHERE title = ? AND id != ? AND deleted_at IS NULL",
        (note["title"], note_id),
    ).fetchone()
    if duplicate:
        return RedirectResponse(
            url_with_query(
                f"/notes/{note_id}/edit",
                msg=f"已经有另一篇笔记也叫《{note['title']}》，先给它改个别的名字，"
                    "否则更新链接会指到那边",
                msg_kind="warn",
            ),
            status_code=303,
        )
    updated_notes, updated_refs, failed = repo.rename_link_refs(
        conn, note_id, old_title, note["title"]
    )
    if failed:
        return RedirectResponse(
            url_with_query(
                f"/notes/{note_id}/edit",
                msg=f"更新了 {updated_notes} 篇，另有 {failed} 篇失败（可在它的历史版本里找回）",
                msg_kind="warn",
            ),
            status_code=303,
        )
    return RedirectResponse(
        url_with_query(
            f"/notes/{note_id}/edit",
            msg=f"已把 {updated_notes} 篇笔记里的 {updated_refs} 处链接更新为新标题",
        ),
        status_code=303,
    )


# ---------------------------------------------------------------------------
# 开关 / 删除 / 恢复
# ---------------------------------------------------------------------------
@router.post("/notes/{note_id}/flag")
def toggle_flag(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    flag: str = Form(...),
    next: str = Form(""),
    value: str = Form("toggle"),
):
    field = FLAG_FIELDS.get(flag)
    if field is None:
        raise HTTPException(status_code=400, detail="未知的开关类型")
    note = _note_or_404(conn, note_id)
    current = bool(note[field])
    desired = (not current) if value == "toggle" else as_bool(value)
    repo.set_flags(conn, note_id, **{field: desired})
    labels = {"public": "公开状态", "pin": "置顶", "star": "星标"}
    return RedirectResponse(
        url_with_query(safe_next(next, f"/notes/{note_id}"), msg=f"{labels[flag]}已更新"),
        status_code=303,
    )


@router.post("/notes/{note_id}/flag.json")
def toggle_flag_json(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    flag: str = Form(...),
    value: str = Form("toggle"),
):
    """无刷新开关接口：返回 JSON，供前端 fetch 后就地更新按钮状态（不再整页 303 刷新）。

    沿用路由级的登录 + CSRF 依赖（require_login / csrf_protect）。
    note_id 由 NoteId 做 1..MAX_SQLITE_INT 范围校验，越界直接 422 而非 int() 溢出变 500；
    flag 必须在 FLAG_FIELDS 内，否则 400。返回体便于前端同步 is-on / 徽标等显示。
    """
    field = FLAG_FIELDS.get(flag)
    if field is None:
        raise HTTPException(status_code=400, detail="未知的开关类型")
    note = _note_or_404(conn, note_id)
    current = bool(note[field])
    desired = (not current) if value == "toggle" else as_bool(value)
    repo.set_flags(conn, note_id, **{field: desired})
    return JSONResponse(
        {
            "ok": True,
            "note_id": note_id,
            "flag": flag,
            "field": field,
            "value": desired,
            "is_on": desired,
        }
    )


@router.post("/notes/{note_id}/archive")
def toggle_archive(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    next: str = Form(""),
):
    """归档 / 取消归档（温和中间态：不进默认列表，可搜索、可恢复）。"""
    note = _note_or_404(conn, note_id)
    desired = not bool(note.get("is_archived"))
    repo.set_archived(conn, note_id, desired)
    msg = "已归档，可在「已归档」筛选里找到" if desired else "已取消归档"
    return RedirectResponse(
        url_with_query(safe_next(next, f"/notes/{note_id}"), msg=msg),
        status_code=303,
    )


@router.post("/notes/{note_id}/delete")
def delete_note(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    next: str = Form(""),
):
    _note_or_404(conn, note_id)
    repo.soft_delete(conn, note_id)
    return RedirectResponse(
        url_with_query(safe_next(next, "/notes"), msg="已移入回收站，可在回收站里恢复"),
        status_code=303,
    )


def _resolve_link_target(
    conn: sqlite3.Connection,
    *,
    target_id: str,
    target_title: str,
    source_id: int,
) -> tuple[dict[str, Any] | None, str]:
    """把「目标 id / 目标标题」解析成一篇真实笔记。返回 (笔记, 错误提示)。"""
    raw_id = (target_id or "").strip()
    if raw_id:
        try:
            found = repo.get_note(conn, int(raw_id))
        except (TypeError, ValueError):
            found = None
        if found is None:
            return None, "要连接的笔记不存在（可能已被删除）"
        if found["id"] == source_id:
            return None, "不能连接到笔记自己"
        return found, ""

    title = (target_title or "").strip()
    if not title:
        return None, "先填要连接的笔记标题，或从下拉里选一篇"
    # 标题解析顺序：完全相等 → 唯一命中 → 否则列出候选让用户选
    matches = repo.search_titles(conn, title, limit=5, exclude_id=source_id)
    exact = [m for m in matches if m["title"].casefold() == title.casefold()]
    if exact:
        return repo.get_note(conn, exact[0]["id"]), ""
    if len(matches) == 1:
        return repo.get_note(conn, matches[0]["id"]), ""
    if not matches:
        return None, f"没找到标题含「{title}」的笔记"
    names = "、".join("《%s》" % m["title"] for m in matches[:3])
    return None, f"「{title}」匹配到多篇：{names} —— 写得更完整一点，或从下拉里选"


@router.post("/notes/{note_id}/link")
def link_note(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    target_id: str = Form(""),
    target_title: str = Form(""),
    next: str = Form(""),
):
    """在正文末尾追加一条 [[目标标题]]，即建立双链。

    不直接改链接表 —— 走正常的正文保存（版本历史里能回退），
    反向链接由 sync_derived 重新解析出来，和手写的 [[ ]] 完全等价。
    """
    note = _note_or_404(conn, note_id)
    target, error = _resolve_link_target(
        conn, target_id=target_id, target_title=target_title, source_id=note_id
    )
    back = safe_next(next, f"/notes/{note_id}")
    if target is None:
        return RedirectResponse(url_with_query(back, msg=error, kind="warn"), status_code=303)

    title = target["title"]
    content = note["content"] or ""
    marker = f"[[{title}]]"
    # 已连过（正文里已出现同名链接）就不重复追加
    if re.search(r"\[\[\s*" + re.escape(title) + r"\s*(\|[^\[\]]*)?\]\]", content, re.IGNORECASE):
        return RedirectResponse(
            url_with_query(back, msg=f"这篇里已经连到《{title}》了", kind="warn"), status_code=303
        )

    appended = marker if not content.strip() else content.rstrip() + "\n\n" + marker + "\n"
    repo.update_note(conn, note_id, content=appended, reason="link")
    return RedirectResponse(
        url_with_query(back, msg=f"已建立链接：这篇 →《{title}》（可在历史版本里撤销）"),
        status_code=303,
    )


@router.post("/notes/{note_id}/link.json")
def link_note_json(
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    target_id: str = Form(""),
    target_title: str = Form(""),
):
    """无刷新建链接口：返回 JSON，供图谱页在图上直接把两篇连起来。

    与 link_note 共用同一套解析与落地逻辑（正文末尾追加 [[标题]]，走版本历史）；
    只是不 303 跳转。已连过时不重复追加，用 ok=True + already=True 回报，
    免得前端把它当错误弹红。
    """
    note = _note_or_404(conn, note_id)
    target, error = _resolve_link_target(
        conn, target_id=target_id, target_title=target_title, source_id=note_id
    )
    if target is None:
        return JSONResponse({"ok": False, "error": error}, status_code=400)

    title = target["title"]
    content = note["content"] or ""
    if re.search(r"\[\[\s*" + re.escape(title) + r"\s*(\|[^\[\]]*)?\]\]", content, re.IGNORECASE):
        return JSONResponse(
            {
                "ok": True,
                "already": True,
                "source_id": note_id,
                "target_id": target["id"],
                "target_title": title,
                "message": f"这篇里已经连到《{title}》了",
            }
        )

    appended = f"[[{title}]]" if not content.strip() else content.rstrip() + f"\n\n[[{title}]]\n"
    repo.update_note(conn, note_id, content=appended, reason="link")
    return JSONResponse(
        {
            "ok": True,
            "already": False,
            "source_id": note_id,
            "target_id": target["id"],
            "target_title": title,
            "message": f"已建立链接：这篇 →《{title}》（可在历史版本里撤销）",
        }
    )


@router.post("/notes/{note_id}/unlink")
def unlink_note(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    target_id: str = Form(""),
    next: str = Form(""),
):
    """把正文里指向某篇的 [[标题]] 全部去掉（保留别的正文）。"""
    note = _note_or_404(conn, note_id)
    back = safe_next(next, f"/notes/{note_id}")
    try:
        target = repo.get_note(conn, int(target_id or 0))
    except (TypeError, ValueError):
        target = None
    if target is None:
        return RedirectResponse(
            url_with_query(back, msg="要取消的链接不存在", kind="warn"), status_code=303
        )

    content = note["content"] or ""
    pattern = re.compile(
        r"\[\[\s*" + re.escape(target["title"]) + r"\s*(?:\|[^\[\]]*)?\]\]", re.IGNORECASE
    )
    stripped, count = pattern.subn("", content)
    if not count:
        return RedirectResponse(
            url_with_query(back, msg=f"正文里没找到指向《{target['title']}》的 [[链接]]", kind="warn"),
            status_code=303,
        )
    # 收拾留下的空行，别越删越乱
    cleaned = re.sub(r"[ \t]+\n", "\n", stripped)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    repo.update_note(conn, note_id, content=cleaned, reason="link")
    return RedirectResponse(
        url_with_query(back, msg=f"已取消指向《{target['title']}》的 {count} 处链接"),
        status_code=303,
    )


@router.post("/notes/{note_id}/restore")
def restore_note(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    next: str = Form(""),
):
    _note_or_404(conn, note_id, include_deleted=True)
    repo.restore(conn, note_id)
    return RedirectResponse(
        url_with_query(safe_next(next, "/trash"), msg="已恢复"), status_code=303
    )


@router.post("/notes/{note_id}/purge")
def purge_note(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    next: str = Form(""),
):
    _note_or_404(conn, note_id, include_deleted=True)
    repo.purge(conn, note_id)
    return RedirectResponse(
        url_with_query(safe_next(next, "/trash"), msg="已彻底删除"), status_code=303
    )


# ---------------------------------------------------------------------------
# 历史版本
# ---------------------------------------------------------------------------
@router.get("/notes/{note_id}/versions")
def version_list(request: Request, note_id: NoteId, conn: sqlite3.Connection = Depends(db_conn)):
    note = _note_or_404(conn, note_id, include_deleted=True)
    versions = repo.list_versions(conn, note_id)
    return render(
        request,
        "notes/versions.html",
        note=note,
        versions=versions,
        keep=settings.version_keep,
    )


@router.get("/notes/{note_id}/versions/{version_id}")
def version_detail(
    request: Request, note_id: NoteId, version_id: VersionId, conn: sqlite3.Connection = Depends(db_conn)
):
    note = _note_or_404(conn, note_id, include_deleted=True)
    version = repo.get_version(conn, note_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="这个历史版本不存在")
    diff = line_diff(version["content"], note["content"])
    return render(
        request,
        "notes/version_detail.html",
        note=note,
        version=version,
        diff=diff,
        added=sum(1 for kind, _ in diff if kind == "add"),
        removed=sum(1 for kind, _ in diff if kind == "del"),
    )


@router.post("/notes/{note_id}/versions/{version_id}/restore")
def version_restore(
    request: Request,
    note_id: NoteId,
    version_id: VersionId,
    conn: sqlite3.Connection = Depends(db_conn),
    next: str = Form(""),
):
    _note_or_404(conn, note_id, include_deleted=True)
    restored = repo.restore_version(conn, note_id, version_id)
    if restored is None:
        raise HTTPException(status_code=404, detail="回滚失败：版本或笔记不存在")
    return RedirectResponse(
        url_with_query(
            safe_next(next, f"/notes/{note_id}"), msg="已回滚到该版本（当前内容已存入历史）"
        ),
        status_code=303,
    )


# ---------------------------------------------------------------------------
# 单篇导出
# ---------------------------------------------------------------------------
@router.get("/notes/{note_id}/export.md")
def export_note(note_id: NoteId, conn: sqlite3.Connection = Depends(db_conn)):
    note = _note_or_404(conn, note_id)
    body = export_service.note_markdown(note)
    filename = export_service.note_filename(note)
    ascii_name = f"note-{note_id}.md"
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'
            )
        },
    )
