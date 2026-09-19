# -*- coding: utf-8 -*-
from __future__ import annotations
"""agent 的安全层：确认判定（从声明派生）与待确认操作的落地。

确认白名单 / 写工具集合不再手工维护 —— 从 tools._TOOL_SPECS 的声明**派生**。
"""
import json
import logging
import sqlite3
import time
from typing import Any

from ... import repo
from .history import _record_run
from .tools import _TOOL_SPECS, _make_tools
from uuid import uuid4

logger = logging.getLogger("inknote.agent")

# 派生集合（原 agent.py 里的手写 frozenset；单一事实来源在 _TOOL_SPECS）
_WRITE_TOOLS = frozenset(n for n, d in _TOOL_SPECS.items() if d.get("writes"))
_ALL_CONFIRMABLE = frozenset(n for n, d in _TOOL_SPECS.items() if d.get("confirm") is not None)


def needs_confirm(spec, params: dict, conn: sqlite3.Connection) -> bool:
    if spec.confirm is None:
        return False
    if spec.confirm is True:
        return True
    return bool(spec.confirm(params, conn))


def confirm_card(spec, params: dict) -> tuple[str, str]:
    return spec.confirm_card(params)


PENDING_TTL_SECONDS = 600      # 确认卡片有效期：10 分钟没用就作废

def _save_pending_op(conn, action: str, params: dict, note: dict) -> dict:
    pending = {
        "id": uuid4().hex[:10],
        "action": action,
        "params": params,
        "created_ts": time.time(),
        "note": {"id": note.get("id"), "title": note.get("title")},
    }
    repo.save_meta_map(conn, {"pending": json.dumps(pending, ensure_ascii=False)}, prefix="agent.")
    return pending

def _get_pending_op(conn) -> dict | None:
    """当前待确认卡片；过期（10 分钟）返回 None 并顺手清掉。同一时刻只留最新一张。"""
    try:
        meta = repo.get_meta_map(conn, "agent.")
        pending = json.loads(str(meta.get("pending") or ""))
    except Exception:
        return None
    if not isinstance(pending, dict) or not pending.get("id"):
        return None
    if time.time() - float(pending.get("created_ts") or 0) > PENDING_TTL_SECONDS:
        _clear_pending_op(conn)
        return None
    return pending

def _take_pending_op(conn, confirm_id: str) -> dict | None:
    """取出指定 id 的待确认卡片（取走即删）。不存在 / 过期 / id 不符都返回 None。"""
    pending = _get_pending_op(conn)
    if pending is None or str(pending.get("id")) != str(confirm_id):
        return None
    _clear_pending_op(conn)
    return pending

def _clear_pending_op(conn) -> None:
    try:
        repo.save_meta_map(conn, {"pending": ""}, prefix="agent.")
    except Exception:
        logger.warning("agent 待确认卡片清理失败", exc_info=True)

def execute_pending(conn: sqlite3.Connection, confirm_id: str) -> dict[str, Any]:
    """用户点「确认执行」后真正落地待确认的写操作（不经过模型）。"""
    pending = _take_pending_op(conn, str(confirm_id))
    if pending is None:
        return {"ok": False, "error": "确认已过期或不存在，请重新发起任务"}
    action = str(pending.get("action") or "")
    if action not in _ALL_CONFIRMABLE:
        return {"ok": False, "error": "该操作不需要确认或已失效"}
    spec = _make_tools(conn).get(action)
    if spec is None:
        return {"ok": False, "error": "工具已不存在"}
    try:
        result = spec.run(pending.get("params") or {})
    except Exception as exc:
        logger.warning("agent 确认执行 %s 失败", action, exc_info=True)
        return {"ok": False, "error": f"执行失败：{exc}"}
    ok = not (isinstance(result, dict) and result.get("error"))
    note = pending.get("note") or {}
    involved = {note["id"]: note.get("title")} if isinstance(note.get("id"), int) else None
    _record_run(
        conn, f"（用户确认后执行）{action}",
        ok=ok,
        answer=str((result or {}).get("note") or f"已执行 {action}")[:300],
        steps=[{"tool": action, "summary": "用户在确认卡片上点「确认执行」"}],
        error="" if ok else str((result or {}).get("error") or ""),
        read_only=False, involved=involved,
    )
    return {"ok": ok, "result": result, "action": action}

def cancel_pending(conn: sqlite3.Connection, confirm_id: str) -> bool:
    """用户点「取消」：作废卡片，不做任何事。"""
    return _take_pending_op(conn, str(confirm_id)) is not None
