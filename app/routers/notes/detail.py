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
from ...services import note_lock
from .lock import locked_page
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
# 详情 / 编辑
# ---------------------------------------------------------------------------
@router.get("/notes/{note_id}")
def note_detail(request: Request, note_id: NoteId, conn: sqlite3.Connection = Depends(db_conn)):
    note = _note_or_404(conn, note_id)
    if not note_lock.is_open(request, note):   # 锁定且本会话没解锁：只给密码页
        return locked_page(request, note, f"/notes/{note_id}")
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
    # 改标题后从保存页跳过来（?rename_from=旧标题）：详情页也要提示「还有链接
    # 引用旧标题」——之前只渲染在编辑页，而保存默认落到详情页，用户什么都看不到
    rename_from = (request.query_params.get("rename_from") or "").strip()
    rename_ref_notes = (
        [(n["title"], hits) for n, hits in repo.link_refs_to(conn, note_id, rename_from)]
        if rename_from
        else []
    )
    return render(
        request,
        "notes/detail.html",
        rename_from=rename_from if rename_ref_notes else "",
        rename_ref_notes=rename_ref_notes,
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
    if not note_lock.is_open(request, note):   # 编辑器同样要解锁，否则等于绕开密码
        return locked_page(request, note, f"/notes/{note_id}/edit")
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
    # 锁定且本会话没解锁：连保存都不许（否则等于用一个表单绕开密码改正文）
    if not note_lock.is_open(request, _note_or_404(conn, note_id)):
        return locked_page(request, _note_or_404(conn, note_id), f"/notes/{note_id}")
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
    back: str = Form(""),
):
    """把引用了旧标题的 [[链接]] 一并改写成新标题（别名保留、走版本历史）。"""
    note = _note_or_404(conn, note_id)
    # 从详情页提交时带 back，更新完回详情页而不是编辑页
    back = (back or "").strip()
    if not back.startswith("/") or back.startswith("//"):
        back = ""
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
            back or f"/notes/{note_id}/edit",
            msg=f"已把 {updated_notes} 篇笔记里的 {updated_refs} 处链接更新为新标题",
        ),
        status_code=303,
    )
