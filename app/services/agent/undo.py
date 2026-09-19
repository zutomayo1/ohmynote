# -*- coding: utf-8 -*-
from __future__ import annotations
"""agent 的步骤级撤销：把最近一次写操作的作用对象恢复到该步之前的状态。

undo 栈存独立 agent_undo 表（新→旧）。每次写工具**成功执行后**，把受影响笔记的
「执行前快照」压栈；撤销 = 弹出栈顶逐篇恢复。恢复本身也存版本历史
（reason=agent-undo），随时可再从版本历史翻回去。上限 MAX_UNDO 条，超出裁掉最旧。

快照口径：整篇笔记（内容 + 分类/摘要 + 公开/置顶/星标/归档 + 标签 + 回收站归属）。
恢复时逐篇先比对，状态没变的跳过——所以「旁观笔记」被误快照也无副作用。
"""
import json
import logging
import sqlite3
from typing import Any

from ... import repo
from ...utils import now_iso
from .history import _record_run

logger = logging.getLogger("inknote.agent")

MAX_UNDO = 50                 # undo 栈最多保留多少步

# params 里哪些键是「受影响的笔记 id」（刻意排除 count/limit/version_id 这类非 id 整数）
_ID_PARAM_KEYS = ("note_id", "target_id")
_LIST_PARAM_KEYS = ("note_ids", "source_ids")
# observation 里哪些键能认出受影响笔记（与 core._collect 同源，bulk 结果逐条带 note_id）
_ID_OBS_KEYS = ("note_id", "id", "target_id")
_LIST_OBS_KEYS = ("notes", "trashed", "results")
# 只有这些工具允许出现「这步新建的笔记」（快照里没有、撤销 = 移入回收站）
_CREATES_TOOLS = frozenset({"create_note"})

_SNAPSHOT_KEYS = ("id", "title", "content", "summary", "category", "status",
                  "is_public", "is_pinned", "is_starred", "is_archived",
                  "deleted_at", "tags")

def _snapshot(note: dict[str, Any]) -> dict[str, Any]:
    return {k: note.get(k) for k in _SNAPSHOT_KEYS}

def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0

def _param_ids(params: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    for key in _ID_PARAM_KEYS:
        if _is_id(params.get(key)):
            ids.append(params[key])
    for key in _LIST_PARAM_KEYS:
        for item in params.get(key) or []:
            if _is_id(item):
                ids.append(item)
    return list(dict.fromkeys(ids))

def _observation_ids(observation: dict[str, Any]) -> list[int]:
    """从观察结果认出受影响的笔记 id。注意同一键可能是列表也可能是布尔
    （trash_note 的 trashed=True / merge_notes 的 trashed=[{id,…}]），只迭代真列表。"""
    ids: list[int] = []
    for key in _ID_OBS_KEYS:
        if _is_id(observation.get(key)):
            ids.append(observation[key])
    for key in _LIST_OBS_KEYS:
        value = observation.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                candidate = item.get("id", item.get("note_id"))
                if _is_id(candidate):
                    ids.append(candidate)
    return list(dict.fromkeys(ids))

def prepare(conn: sqlite3.Connection, tool: str, params: dict[str, Any]) -> dict[str, Any]:
    """写工具执行前调用：把 params 点到的现有笔记做整快照（不存在的不记）。"""
    snaps: list[dict[str, Any]] = []
    try:
        for nid in _param_ids(params or {}):
            note = repo.get_note(conn, nid, include_deleted=True)
            if note is not None:
                snaps.append(_snapshot(note))
    except Exception:
        logger.warning("agent undo 快照失败（tool=%s）", tool, exc_info=True)
        snaps = []
    return {"tool": tool, "snaps": snaps}

def commit(conn: sqlite3.Connection, ctx: dict[str, Any], tool: str,
           observation: dict[str, Any], summary: str) -> None:
    """写工具成功执行后调用：把受影响笔记的执行前快照压入 undo 栈。绝不抛异常。"""
    try:
        observation = observation or {}
        if observation.get("error"):
            return
        entries: list[dict[str, Any]] = []
        for nid in _observation_ids(observation):
            snap = next((s for s in ctx["snaps"] if s["id"] == nid), None)
            if snap is not None:
                entries.append(snap)
            elif tool in _CREATES_TOOLS:
                entries.append({"id": nid, "missing": True})
        if not entries and ctx["snaps"]:
            # 观察里认不出的工具：退回 params 快照。恢复时逐篇比对，无变化的会跳过，
            # 所以多快照的旁观笔记不会有副作用。
            entries = list(ctx["snaps"])
        if not entries:
            return
        conn.execute(
            "INSERT INTO agent_undo (created_at, tool, summary, payload_json) VALUES (?, ?, ?, ?)",
            (now_iso(), tool, (summary or "")[:200],
             json.dumps({"notes": entries}, ensure_ascii=False)))
        conn.execute(
            "DELETE FROM agent_undo WHERE id NOT IN"
            " (SELECT id FROM agent_undo ORDER BY id DESC LIMIT ?)", (MAX_UNDO,))
    except Exception:
        logger.warning("agent undo 记录失败（tool=%s）", tool, exc_info=True)

def peek(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """栈顶摘要（不弹出）。给 final 事件的 undoable 标记用。"""
    try:
        row = conn.execute(
            "SELECT tool, summary FROM agent_undo ORDER BY id DESC LIMIT 1").fetchone()
        return {"tool": row["tool"], "summary": row["summary"]} if row else None
    except Exception:
        return None

def undo_last(conn: sqlite3.Connection) -> dict[str, Any]:
    """撤销最近一步写操作。返回 {ok, tool, summary, changed, skipped} 或 {ok: False, error}。"""
    try:
        row = conn.execute(
            "SELECT id, tool, summary, payload_json FROM agent_undo ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "没有可撤销的操作"}
        entry = json.loads(row["payload_json"] or "{}")
        changed: list[int] = []
        skipped = 0
        for note_entry in entry.get("notes") or []:
            nid = note_entry.get("id")
            if not isinstance(nid, int):
                continue
            current = repo.get_note(conn, nid, include_deleted=True)
            if note_entry.get("missing"):
                # 这一步新建的笔记：撤销 = 移入回收站（30 天内可恢复，不硬删）
                if current is not None and not current.get("deleted_at"):
                    repo.soft_delete(conn, nid)
                    changed.append(nid)
                else:
                    skipped += 1
                continue
            if current is None:
                skipped += 1
                continue
            snap_trashed = bool(note_entry.get("deleted_at"))
            cur_trashed = bool(current.get("deleted_at"))
            fields_differ = any(note_entry.get(k) != current.get(k)
                                for k in ("title", "content", "summary", "category",
                                          "status", "tags"))
            flags_differ = any(bool(note_entry.get(k)) != bool(current.get(k))
                               for k in ("is_public", "is_pinned", "is_starred",
                                         "is_archived"))
            if not fields_differ and not flags_differ and snap_trashed == cur_trashed:
                skipped += 1      # 现状已是快照状态（或被误快照的旁观笔记）：什么都不用做
                continue
            # 当前在回收站而快照不在：先恢复成可编辑（update_note 对回收站内的笔记会断言失败）
            if cur_trashed and not snap_trashed:
                repo.restore(conn, nid)
            if fields_differ:
                # 内容与编辑类字段（存版本历史，reason=agent-undo，可再翻回去）
                repo.update_note(
                    conn, nid,
                    title=str(note_entry.get("title") or ""),
                    content=str(note_entry.get("content") or ""),
                    summary=str(note_entry.get("summary") or ""),
                    category=str(note_entry.get("category") or ""),
                    status=note_entry.get("status") or None,
                    tags=note_entry.get("tags") or [],
                    reason="agent-undo")
            if flags_differ:
                # 标志类（不算「编辑」，不更新 updated_at）
                repo.set_flags(conn, nid, is_public=bool(note_entry.get("is_public")),
                               is_pinned=bool(note_entry.get("is_pinned")),
                               is_starred=bool(note_entry.get("is_starred")))
                repo.set_archived(conn, nid, bool(note_entry.get("is_archived")))
            # 快照本来就在回收站：最后放回去
            if snap_trashed and not cur_trashed:
                repo.soft_delete(conn, nid)
            changed.append(nid)
        conn.execute("DELETE FROM agent_undo WHERE id = ?", (row["id"],))
        remaining = int(conn.execute("SELECT COUNT(*) FROM agent_undo").fetchone()[0])
        if changed:
            # 撤销本身也进执行历史（审计闭环：谁在什么时候反悔了什么，可追溯）
            involved = {}
            for nid in changed:
                note = repo.get_note(conn, nid, include_deleted=True)
                involved[nid] = str((note or {}).get("title") or f"笔记 #{nid}")
            _record_run(conn, f"（撤销）{row['tool']}", ok=True,
                        answer=f"已撤销上一步：{(row['summary'] or '')[:120]}",
                        steps=[{"tool": "undo_last",
                                "summary": f"已把 {len(changed)} 篇恢复到该步之前",
                                "duration_ms": 0}],
                        error="", read_only=False, involved=involved)
        return {"ok": True, "tool": row["tool"], "summary": row["summary"],
                "changed": changed, "skipped": skipped, "remaining": remaining}
    except Exception as exc:
        logger.warning("agent undo 执行失败", exc_info=True)
        return {"ok": False, "error": f"撤销失败：{exc}"}
