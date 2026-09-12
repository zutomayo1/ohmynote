"""内联 AI 的 JSON 接口：/api/ai/continue 与 /api/ai/rewrite。

接入方式和返回格式照 ai_admin.py 里的 /api/ai/summarize、/api/ai/tags：

* 依赖 ``require_login_api``（未登录 401 JSON）+ ``csrf_protect``（缺 token 403）；
* 一律返回 ``{ok, ...}``，失败给人话 ``error``，不抛 500。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import csrf_protect, db_conn, require_login_api
from ..services import ai, ai_inline

router = APIRouter(
    prefix="/api",
    dependencies=[Depends(require_login_api), Depends(csrf_protect)],
)

NOT_CONFIGURED = "尚未配置 AI 服务：先去设置页配置 AI 服务"
MODE_HINT = "不支持的改写方式，请用 润色 / 精简 / 扩写 / 翻译 / 更正式"


def _json(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


async def _read_json(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


@router.post("/ai/continue")
async def ai_continue(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    """接着笔记正文往下写。body: {title, content, instruction?}"""
    payload = await _read_json(request)
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content") or "").strip()
    instruction = str(payload.get("instruction") or "").strip()

    if not content:
        return _json({"ok": False, "error": "正文还是空的，先写点内容吧"}, 400)
    if not ai.is_enabled():
        return _json({"ok": False, "error": NOT_CONFIGURED}, 503)
    try:
        text = ai_inline.continue_text(title, content, instruction=instruction, conn=conn)
    except ai.AIError as exc:
        return _json({"ok": False, "error": str(exc)}, 502)
    if not text:
        return _json({"ok": False, "error": "AI 没有返回内容，请重试"}, 502)
    return _json({"ok": True, "text": text})


@router.post("/ai/rewrite")
async def ai_rewrite(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    """按 mode 改写选中文本。body: {text, mode}"""
    payload = await _read_json(request)
    text = str(payload.get("text") or "").strip()
    mode = str(payload.get("mode") or "").strip()

    if not text:
        return _json({"ok": False, "error": "请先选中要改写的文字"}, 400)
    if mode not in ai_inline.REWRITE_MODES:
        return _json({"ok": False, "error": MODE_HINT}, 400)
    if not ai.is_enabled():
        return _json({"ok": False, "error": NOT_CONFIGURED}, 503)
    try:
        result = ai_inline.rewrite(text, mode, conn=conn)
    except ai.AIError as exc:
        return _json({"ok": False, "error": str(exc)}, 502)
    if not result:
        return _json({"ok": False, "error": "AI 没有返回内容，请重试"}, 502)
    return _json({"ok": True, "text": result})
