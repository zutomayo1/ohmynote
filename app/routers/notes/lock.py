"""锁定笔记：设密码 / 解锁 / 重新锁定 / 解除锁定。

四个动作都是表单 POST（带 ``_csrf``；登录与 CSRF 依赖挂在父 router 上，
见 app/routers/notes/__init__.py），完成后 303 回原处并 flash 结果。
语义见 app/services/note_lock.py 顶部说明。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ... import repo
from ...deps import NoteId, db_conn
from ...services import note_lock
from ...templating import render, url_with_query
from ...utils import safe_next

router = APIRouter()


def _note_or_404(conn: sqlite3.Connection, note_id: int) -> dict:
    note = repo.get_note(conn, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="这篇笔记不存在或已被删除")
    return note


def _unlock_response(request: Request, note_id: int, url: str, message: str | None = None) -> RedirectResponse:
    """跳回去，并把这篇标记为「本会话已解锁」。"""
    response = RedirectResponse(url_with_query(url, msg=message) if message else url, status_code=303)
    response.set_cookie(
        note_lock.UNLOCK_COOKIE,
        note_lock.cookie_value(note_lock.add_unlocked(request, note_id)),
        max_age=note_lock.UNLOCK_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


def locked_page(request: Request, note: dict, next_url: str, message: str = ""):
    """「这篇锁着」的提示页（阅读页 / 编辑页 / 保存接口共用），密码错误返回 403。"""
    return render(
        request,
        "notes/locked.html",
        note=note,
        next_url=next_url,
        error=message,
        note_lock_hours=note_lock.UNLOCK_MAX_AGE // 3600,
        status_code=403,
    )


@router.post("/notes/{note_id}/lock")
def lock_note(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    password: str = Form(""),
    next: str = Form(""),
):
    """给笔记设密码；已锁定且本会话没解锁时，先要求解锁（改密码也要先证明身份）。"""
    note = _note_or_404(conn, note_id)
    back = safe_next(next, f"/notes/{note_id}")

    if note.get("locked") and not note_lock.is_open(request, note):
        return locked_page(request, note, back, "这篇已锁定，先解锁再改密码。")
    if len(password) < 4:
        return RedirectResponse(url_with_query(back, msg="密码至少 4 位", kind="error"), status_code=303)

    was_public = bool(note.get("is_public"))
    note_lock.set_lock(conn, note_id, password)
    msg = "已锁定：再看正文需要密码"
    if was_public:
        msg += "；同时已取消公开（不会出现在博客与 RSS 里）"
    # 刚输过密码，顺手把本会话标记为已解锁
    return _unlock_response(request, note_id, back, msg)


@router.post("/notes/{note_id}/unlock")
def unlock_note(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    password: str = Form(""),
    next: str = Form(""),
):
    """输入密码解锁，只对当前浏览器会话有效。"""
    note = _note_or_404(conn, note_id)
    back = safe_next(next, f"/notes/{note_id}")
    if not note.get("locked"):
        return RedirectResponse(back, status_code=303)
    if not note_lock.verify_password(password, note.get("lock_hash")):
        return locked_page(request, note, back, "密码不对")
    return _unlock_response(request, note_id, back)


@router.post("/notes/{note_id}/relock")
def relock_note(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    next: str = Form(""),
):
    """撤销本会话的解锁状态（笔记仍然是锁定的，别人来还是要密码）。"""
    _note_or_404(conn, note_id)
    back = safe_next(next, f"/notes/{note_id}")
    response = RedirectResponse(
        url_with_query(back, msg="已重新锁定，再看需要重新输入密码"), status_code=303
    )
    ids = note_lock.drop_unlocked(request, note_id)
    if ids:
        response.set_cookie(
            note_lock.UNLOCK_COOKIE, note_lock.cookie_value(ids),
            max_age=note_lock.UNLOCK_MAX_AGE, httponly=True,
            samesite="lax", secure=request.url.scheme == "https",
        )
    else:
        response.delete_cookie(note_lock.UNLOCK_COOKIE)
    return response


@router.post("/notes/{note_id}/unlock/remove")
def remove_lock(
    request: Request,
    note_id: NoteId,
    conn: sqlite3.Connection = Depends(db_conn),
    password: str = Form(""),
    next: str = Form(""),
):
    """彻底解除锁定（再输一次密码），并把正文补回搜索索引。"""
    note = _note_or_404(conn, note_id)
    back = safe_next(next, f"/notes/{note_id}")
    if not note_lock.verify_password(password, note.get("lock_hash")):
        return locked_page(request, note, back, "密码不对，解除锁定失败")
    note_lock.clear_lock(conn, note_id)
    response = RedirectResponse(
        url_with_query(back, msg="已解除锁定，正文重新参与搜索"), status_code=303
    )
    response.delete_cookie(note_lock.UNLOCK_COOKIE)
    return response
