"""夜间整理 agent：复用后台线程，每天自动做两件小事。

1. 给「分类为空」的笔记用 AI 推荐并写入一个分类（复用你已有的分类，可编辑可撤销）
2. 把打了「收件箱」标签的笔记换上「已归档」标签（正文不动）

幂等：meta 表记 last_run 时间戳，interval 内不重复跑；AI 未启用或
INKNOTE_TIDY=0 时整体跳过。所有写入都走 repo（有版本历史可回退）。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
from typing import Any

from .. import repo
from ..config import settings
from . import ai

logger = logging.getLogger("inknote.tidy")

META_KEY = "tidy.last_run"
INBOX_TAG = "收件箱"
ARCHIVE_TAG = "已归档"
CATEGORIES_PER_RUN = 20      # 每次最多给多少篇没分类的笔记补分类（控制 token 成本）
INBOX_PER_RUN = 50           # 每次最多归档多少篇收件箱笔记


def _now(value: Any = None) -> _dt.datetime:
    """统一成「时区感知」的时间，避免 naive/aware 混用相减报错。

    - value 为 None：取当前 UTC 时间；
    - value 是 naive：当作本地时间转换为时区感知；
    - value 已经是 aware：原样采用。
    """
    if value is None:
        return _dt.datetime.now(_dt.timezone.utc)
    if value.tzinfo is None:
        return value.astimezone()
    return value


def _seconds_since(last_run: str) -> float:
    try:
        then = _dt.datetime.fromisoformat(last_run)
    except ValueError:
        return float("inf")
    delta = _now() - then
    return max(delta.total_seconds(), 0.0)


def _load_state(conn: sqlite3.Connection) -> dict[str, str]:
    return repo.get_meta_map(conn, "tidy.")


def _existing_categories(conn: sqlite3.Connection) -> list[str]:
    return [item["name"] for item in repo.list_categories(conn)]


def _iter_notes(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    page = 1
    per_page = 100
    while len(notes) < limit:
        page_notes, total = repo.list_notes(conn, page=page, per_page=per_page)
        if not page_notes:
            break
        notes.extend(page_notes)
        if page * per_page >= total:
            break
        page += 1
    return notes[:limit]


def _categorize(conn: sqlite3.Connection, notes: list[dict[str, Any]]) -> int:
    existing = _existing_categories(conn)
    done = 0
    for note in notes:
        if note.get("category"):
            continue
        title = str(note.get("title") or "")
        content = str(note.get("content") or "")
        try:
            category = ai.suggest_category(title, content, existing=existing, conn=conn)
        except ai.AIError as exc:
            logger.warning("自动分类失败（note=%s）：%s", note.get("id"), exc)
            continue
        category = category.strip().strip("「」\"'")
        if not category:
            continue
        repo.update_note(conn, int(note["id"]), category=category, reason="tidy")
        if category not in existing:
            existing.append(category)
        done += 1
        if done >= CATEGORIES_PER_RUN:
            break
    return done


def _archive_inbox(conn: sqlite3.Connection, notes: list[dict[str, Any]]) -> int:
    done = 0
    for note in notes:
        tags = list(note.get("tags") or [])
        if INBOX_TAG not in tags:
            continue
        new_tags = [tag for tag in tags if tag != INBOX_TAG]
        if ARCHIVE_TAG not in new_tags:
            new_tags.append(ARCHIVE_TAG)
        repo.update_note(conn, int(note["id"]), tags=new_tags, reason="tidy")
        done += 1
        if done >= INBOX_PER_RUN:
            break
    return done


def maybe_tidy(
    conn: sqlite3.Connection,
    *,
    interval_hours: int = 24,
    now: Any = None,
) -> dict[str, Any] | None:
    """该跑就跑一轮整理，跑完记时间戳；没到时间 / 被禁用 / 没配 AI 都返回 None。"""
    if not getattr(settings, "tidy_enabled", True):
        return None
    if not ai.is_enabled():
        return None

    state = _load_state(conn)
    last_run = str(state.get("last_run") or "")
    if last_run:
        elapsed = _seconds_since(last_run)
        if elapsed < interval_hours * 3600:
            return None

    notes = _iter_notes(conn, limit=500)
    if not notes:
        return None

    categorized = _categorize(conn, notes)
    archived = _archive_inbox(conn, notes)
    result = {"categorized": categorized, "archived": archived}

    stamp = _now(now).isoformat()
    result_with_time = {
        "categorized": categorized,
        "archived": archived,
        "at": stamp,
    }
    repo.save_meta_map(
        conn,
        {
            "last_run": stamp,
            "last_result": json.dumps(result_with_time, ensure_ascii=False),
        },
        prefix="tidy.",
    )
    logger.info("夜间整理完成：%s", result)
    return result


def describe(conn: sqlite3.Connection) -> dict:
    """设置页展示用：夜间整理的开关状态与最近一次结果。

    只读展示用，绝不抛异常——meta 读不出来就全部给默认值，
    不能让设置页因为它挂掉。
    """
    enabled = bool(getattr(settings, "tidy_enabled", True))

    try:
        ai_ready = bool(ai.is_enabled())
    except Exception:
        ai_ready = False

    try:
        state = repo.get_meta_map(conn, "tidy.")
    except Exception:
        state = {}

    last_run = str(state.get("last_run") or "")

    last_result: dict[str, Any] | None = None
    raw = state.get("last_result")
    if raw:
        try:
            parsed = json.loads(raw)
            last_result = {
                "categorized": int(parsed.get("categorized", 0) or 0),
                "archived": int(parsed.get("archived", 0) or 0),
                "at": str(parsed.get("at") or ""),
            }
        except Exception:
            last_result = None

    return {
        "enabled": enabled,
        "ai_ready": ai_ready,
        "last_run": last_run,
        "last_result": last_result,
    }
