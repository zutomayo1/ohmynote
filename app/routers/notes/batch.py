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
