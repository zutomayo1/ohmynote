"""FastAPI 依赖：数据库连接、登录校验、CSRF 防护，以及带范围约束的参数类型。"""

from __future__ import annotations

import hmac
import sqlite3
from typing import Annotated, Iterator
from urllib.parse import urlencode

from fastapi import HTTPException, Path, Query, Request

from . import db as db_mod
from .config import settings
from .security import LoginThrottle, read_session

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")

# SQLite 的整数是 64 位，超过这个范围绑参会抛 OverflowError（变成 500）。
# 所以所有来自 URL 的整数都在路由入口限住，让 FastAPI 直接返回 422 而不是崩。
MAX_SQLITE_INT = 2**63 - 1
MAX_PAGE = 1_000_000

NoteId = Annotated[int, Path(ge=1, le=MAX_SQLITE_INT, description="笔记 ID")]
VersionId = Annotated[int, Path(ge=1, le=MAX_SQLITE_INT, description="历史版本 ID")]
TemplateId = Annotated[int, Path(ge=1, le=MAX_SQLITE_INT, description="模板 ID")]
PageParam = Annotated[int, Query(ge=1, le=MAX_PAGE, description="页码")]
EditParam = Annotated[int, Query(ge=0, le=MAX_SQLITE_INT, description="要编辑的模板 ID，0 表示新建")]
LimitParam = Annotated[int, Query(ge=1, le=50, description="返回条数上限")]

login_throttle = LoginThrottle(limit=8, window=600, lockout=300)


class RedirectException(Exception):
    """需要跳转（而不是返回 JSON）时抛出，由 main.py 的处理器转成 303。"""

    def __init__(self, url: str, status_code: int = 303) -> None:
        super().__init__(url)
        self.url = url
        self.status_code = status_code


def db_conn() -> Iterator[sqlite3.Connection]:
    """一个请求一个短连接，正常结束提交、异常回滚。"""
    with db_mod.db() as conn:
        yield conn


def current_session(request: Request) -> dict | None:
    """读出已登录会话（挂在 request.state 上，一次请求只解析一次）。"""
    cached = getattr(request.state, "session", None)
    if cached is not None:
        return cached or None
    token = request.cookies.get(settings.session_cookie)
    data = read_session(settings.secret_key, token)
    request.state.session = data or {}
    return data


def csrf_token(request: Request) -> str:
    session = current_session(request)
    return str((session or {}).get("csrf") or "")


def require_login(request: Request) -> dict:
    """未登录时跳转到登录页，并记住原地址。"""
    session = current_session(request)
    if session:
        return session
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    raise RedirectException(f"/login?{urlencode({'next': target})}")


def require_login_api(request: Request) -> dict:
    session = current_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    return session


async def csrf_protect(request: Request) -> None:
    """写操作校验 CSRF：接受 X-CSRF-Token 头、_csrf 表单字段或查询参数。"""
    if request.method in SAFE_METHODS:
        return
    session = current_session(request)
    expected = str((session or {}).get("csrf") or "")
    if not expected:
        raise HTTPException(status_code=403, detail="登录状态已失效，请重新登录后再试")

    token = request.headers.get("x-csrf-token") or request.query_params.get("_csrf") or ""
    if not token:
        content_type = (request.headers.get("content-type") or "").lower()
        if content_type.startswith(FORM_TYPES):
            form = await request.form()
            value = form.get("_csrf")
            token = str(value) if value is not None else ""
    if not token or not hmac.compare_digest(str(token), expected):
        raise HTTPException(status_code=403, detail="表单已过期（CSRF 校验失败），请刷新页面后重试")
