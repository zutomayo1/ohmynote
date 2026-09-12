"""登录 / 登出。

个人自用：没有用户表，口令存在三个地方（优先级从高到低）：

    设置页保存的 meta（``account.password_hash``） → ``INKNOTE_PASSWORD_HASH`` → ``INKNOTE_PASSWORD``

登录接口本身不做 CSRF 校验（此时还没有会话），用限流 + SameSite=Lax 兜住风险。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..config import settings
from ..deps import current_session, db_conn, login_throttle
from ..security import make_session, verify_password, verify_plain
from ..services import account
from ..templating import render
from ..utils import safe_next

router = APIRouter(tags=["auth"])


def verify_login(password: str, conn: sqlite3.Connection | None = None) -> bool:
    """先走 ``account``（库里的哈希 → .env 哈希 → .env 明文），再回退原 .env 校验。"""
    if not password:
        return False
    try:
        if account.verify(conn, password):
            return True
        # 页面改过密码时不能再回退 .env，否则旧密码照样能登进来
        if account.is_db_managed(conn):
            return False
    except Exception:  # noqa: BLE001 - 回退路径仍在下面，登录不能因为库出错而崩
        pass
    # 原来的 .env 校验路径，行为保持不变
    if settings.password_hash:
        return verify_password(password, settings.password_hash)
    return verify_plain(password, settings.password)


def _set_session(response: RedirectResponse) -> None:
    token, _csrf = make_session(settings.secret_key, max_age=settings.session_max_age)
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=settings.session_max_age,
        httponly=True,
        samesite="lax",
        path="/",
    )


@router.get("/login")
def login_page(request: Request, next: str = "/notes"):
    if current_session(request):
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", next_url=safe_next(next), error="")


@router.post("/login")
def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form("/notes"),
    conn: sqlite3.Connection = Depends(db_conn),
):
    target = safe_next(next)
    blocked = login_throttle.blocked_for()
    if blocked:
        return render(
            request,
            "login.html",
            status_code=429,
            next_url=target,
            error=f"失败次数过多，请 {blocked} 秒后再试。",
        )

    if not verify_login(password, conn):
        remaining = login_throttle.register_failure()
        message = "密码不正确。"
        if remaining:
            message = f"密码不正确，已暂时锁定，请 {remaining} 秒后再试。"
        return render(request, "login.html", status_code=401, next_url=target, error=message)

    login_throttle.reset()
    response = RedirectResponse(target, status_code=303)
    _set_session(response)
    return response


@router.post("/logout")
def logout(request: Request):
    del request
    response = RedirectResponse("/blog", status_code=303)
    response.delete_cookie(settings.session_cookie, path="/")
    return response
