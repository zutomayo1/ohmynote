"""向量检索路由（/api/ai/embed/*）。

这个文件已经注册进 main.py；本文件只做三件事：
- GET  status：把 service 层的索引状态 + 后台线程进度合并返回；
- POST rebuild：起 threading.Thread 后台重建，前台立刻返回；
- POST clear：清空向量索引。

SQLite 连接不能跨线程共用：后台线程里自己 `with db_mod.db() as conn:` 开新连接。
"""

from __future__ import annotations

import logging
import sqlite3
import threading

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import csrf_protect, db_conn, require_login_api
from ..services import ai_embed

router = APIRouter(
    prefix="/api",
    dependencies=[Depends(require_login_api), Depends(csrf_protect)],
)

logger = logging.getLogger("inknote.ai_embed_router")

# 后台重建状态。模块级 + 锁：请求线程读、重建线程写。
_rebuild_lock = threading.Lock()
_rebuild_state: dict = {"running": False, "progress": None, "error": ""}


def _json(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


def _rebuild_worker(force: bool) -> None:
    """后台线程入口：新开数据库连接，同步调用 ai_embed.rebuild。"""
    from .. import db as db_mod

    try:
        with db_mod.db() as conn:

            def on_progress(done: int, total: int) -> None:
                with _rebuild_lock:
                    _rebuild_state["progress"] = {"done": int(done), "total": int(total)}

            result = ai_embed.rebuild(conn, progress=on_progress, force=force)
        if not result.get("ok"):
            with _rebuild_lock:
                _rebuild_state["error"] = str(result.get("error") or "向量索引重建失败")
    except Exception as exc:  # 线程里的任何异常都不能掀掉服务
        logger.warning("向量索引：后台重建线程异常（force=%s）", force, exc_info=True)
        with _rebuild_lock:
            _rebuild_state["error"] = str(exc) or "向量索引重建失败"
    finally:
        with _rebuild_lock:
            _rebuild_state["running"] = False
            _rebuild_state["progress"] = None


@router.get("/ai/embed/status")
def embed_status(conn: sqlite3.Connection = Depends(db_conn)):
    """向量索引状态（设置页轮询这个）。"""
    payload = ai_embed.status(conn)
    with _rebuild_lock:
        if _rebuild_state["running"]:
            payload["running"] = True
            payload["progress"] = (
                dict(_rebuild_state["progress"]) if _rebuild_state["progress"] else payload.get("progress")
            )
        if _rebuild_state["error"]:
            payload["error"] = _rebuild_state["error"]
    return _json(payload)


@router.post("/ai/embed/rebuild")
def embed_rebuild(request: Request, conn: sqlite3.Connection = Depends(db_conn), force: int = 0):
    """重建索引（后台线程跑，前端轮询 status 看进度）；?force=1 全量重建。"""
    del request
    with _rebuild_lock:
        if _rebuild_state["running"] or ai_embed.is_running():
            return _json({"ok": False, "error": "正在重建中"}, 409)
        try:
            total = int(ai_embed.status(conn).get("total") or 0)
        except Exception:
            # status 只用来显示进度总数，读不到就按 0，但不能静默
            logger.warning("向量索引：读取索引状态失败，重建总数按 0 处理", exc_info=True)
            total = 0
        _rebuild_state["running"] = True
        _rebuild_state["progress"] = {"done": 0, "total": total}
        _rebuild_state["error"] = ""

    thread = threading.Thread(
        target=_rebuild_worker,
        args=(bool(force),),
        name="ai-embed-rebuild",
        daemon=True,
    )
    thread.start()
    return _json({"ok": True, "started": True, "total": total})


@router.post("/ai/embed/clear")
def embed_clear(conn: sqlite3.Connection = Depends(db_conn)):
    """清空向量索引。"""
    removed = ai_embed.clear(conn)
    with _rebuild_lock:
        _rebuild_state["error"] = ""
    return _json({"ok": True, "removed": int(removed)})
