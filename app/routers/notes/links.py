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
