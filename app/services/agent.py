"""墨痕的笔记 Agent：让模型带着工具多步干活，而不只是回答。

协议刻意不依赖各家 API 的原生 function calling（兼容性参差），用最朴素的
JSON 循环：模型每轮只回一个 JSON——要么调工具，要么给最终回答；服务端执行
工具后把观察结果喂回去，最多 MAX_STEPS 轮。

安全边界：
* 所有写操作都走 repo（update_note 会记版本历史，删错可从回收站捞回）
* 只暴露白名单工具，参数在服务端做类型/范围收敛，绝不把模型输出直接拼进 SQL
* 观察结果截断，防止长笔记把上下文撑爆
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from datetime import date, timedelta
from difflib import SequenceMatcher
from uuid import uuid4
from typing import Any, Callable

from .. import repo
from ..utils import now_iso
from . import ai

logger = logging.getLogger("inknote.agent")

MAX_STEPS = 16               # 单次任务最多几步（原来 6：读一篇 + 写回就撞墙）
READ_WINDOW = 8000           # read_note 一次给模型看多少字（绝大多数笔记一次读完）
READ_WINDOW_MAX = 30000      # read_note 单次上限（长笔记可以显式要更多）
OBSERVE_LIMIT = 1200         # 一般工具结果给模型看多少字符
# read_note 的观察结果**不能**按 OBSERVE_LIMIT 截断：截了模型就只看到前 800 字，
# 只能不停换 offset 反复读同一篇 —— 这正是「总结一篇 2794 字的笔记烧光 6 步」的真因。
# 它按 READ_WINDOW 自己控制体量，这里给足空间把整段正文送到模型面前。
READ_OBSERVE_LIMIT = READ_WINDOW + 600
MAX_HISTORY_TURNS = 5        # 会话记忆最多带几轮之前的任务
MAX_HISTORY_CHARS = 600      # 每条历史消息最多带多少字
MAX_RUNS = 20                # 执行历史最多保留多少条（审计用）
CHAT_RETRIES = 2             # 模型调用失败自动重试次数（总共尝试 N 次）
SEARCH_LIMIT_MAX = 10
MAX_REPEAT_STEPS = 3         # 连续这么多步都在重复调用就收场，别把预算烧光
MAX_FORMAT_RETRIES = 2       # 模型没按 JSON 协议回：带反馈重试这么多次后才降级
MAX_ACTIONS_PER_TURN = 4     # 单轮最多并做几个工具（协议 v2：actions[] 数组）
PLAN_MAX_CHARS = 4000        # 「按计划执行」计划文本上限
CONFIRM_BULK_THRESHOLD = 10  # 批量操作超过这个篇数需要用户确认
CONFIRM_REWRITE_MIN_CHARS = 200   # 原文短于这个字数不触发「大改写」确认
CONFIRM_REWRITE_RATIO = 0.35      # 新旧正文相似度低于它 = 大改写，需要确认

# ---------------------------------------------------------------------------
# 可取消：run_id -> threading.Event。单进程应用，模块级注册表足够；
# SSE 端点把 run_id 通过响应头交给前端，取消端点按 id 置位，循环每步检查。
# ---------------------------------------------------------------------------
_CANCEL_EVENTS: dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()


def new_run_id() -> str:
    return uuid4().hex[:12]


def _register_run(run_id: str) -> threading.Event:
    with _CANCEL_LOCK:
        event = _CANCEL_EVENTS.get(run_id)
        if event is None:
            event = threading.Event()
            _CANCEL_EVENTS[run_id] = event
        # 顺手清理已结束的残留（防御性，正常都会在 finally 里摘掉）
        if len(_CANCEL_EVENTS) > 64:
            for key in [k for k, v in _CANCEL_EVENTS.items() if k != run_id and v.is_set()]:
                _CANCEL_EVENTS.pop(key, None)
        return event


def _release_run(run_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(run_id, None)


def request_cancel(run_id: str) -> bool:
    """请求取消一个正在跑的任务。返回是否找到了还在跑的 run。"""
    with _CANCEL_LOCK:
        event = _CANCEL_EVENTS.get(str(run_id or ""))
    if event is None:
        return False
    event.set()
    return True

_WRITE_TOOLS = frozenset({
    "create_note", "update_note", "add_tags", "remove_tags", "set_category",
    "publish_note", "archive_note", "trash_note", "restore_note", "pin_note", "star_note",
    "bulk_add_tags", "bulk_remove_tags", "append_note",
    "restore_version", "merge_notes",
})

# 这些写操作不可逆或影响面大，机制层强制「先确认再执行」：
# 模型调用只会生成一张确认卡片（meta: agent.pending），用户在页面点「确认执行」
# 才真正落地（execute_pending，绕过模型）——提示词约束之外的最后一道闸
_CONFIRM_TOOLS = frozenset({"trash_note", "publish_note", "merge_notes"})
# 确认卡片的文案按动作生成：会发生什么、怎么后悔（不再写死回收站一套话）
_CONFIRM_META = {
    "trash_note": {
        "label": "移入回收站",
        "consequence": "笔记将进入回收站（软删除），30 天内可在回收站恢复，之后自动清除",
    },
    "publish_note": {
        "label": "发布到博客",
        "consequence": "笔记将公开到博客，任何能访问博客的人都能看到；取消公开即收回",
    },
    "merge_notes": {
        "label": "合并笔记",
        "consequence": "源笔记正文将并入目标笔记，源笔记移入回收站（30 天内可恢复）",
    },
}
PENDING_TTL_SECONDS = 600      # 确认卡片有效期：10 分钟没用就作废

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _today_label() -> str:
    """给模型一个明确的「今天」。没有它，「这周/最近三天」这类任务没法算。"""
    today = date.today()
    return f"{today.isoformat()}（{_WEEKDAYS[today.weekday()]}）"


SYSTEM_PROMPT = """你是墨痕笔记应用里的笔记助手 Agent。用户用自然语言给你任务，你通过调用工具多步完成。
今天是 {today}。

## 可用工具

{tools}

## 输出协议（严格遵守）

每一轮你只能输出**一个** JSON 对象，不要输出任何别的文字、不要用代码块包裹：

1. 调一个工具：{{"action": "工具名", "params": {{...}}}}
2. 一次并做多个独立工具（最多 {max_actions} 个，只能是读类或互不依赖的操作）：
   {{"actions": [{{"action": "工具名", "params": {{...}}}}, ...]}}
3. 任务完成：{{"action": "final", "answer": "给用户看的最终回答"}}

## 示例

用户：给提到 Docker 的笔记加上「部署」标签
正确第一步：{{"action": "search_notes", "params": {{"query": "Docker"}}}}
拿到结果后：{{"action": "add_tags", "params": {{"note_id": 12, "tags": ["部署"]}}}}
做完后：{{"action": "final", "answer": "已给《Docker 部署手记》加上「部署」标签。"}}

用户：随便聊聊什么是知识管理
直接：{{"action": "final", "answer": "知识管理是…"}}（无需工具就别调工具）

## 行动准则

- **先查再写**：写操作（create/update/…）前先用 search_notes 或 read_note 确认目标存在，
  绝不凭空编造 note_id。但也不要反复确认同一件事——查一次就够。
- **省步数**：拿到 note_id 直接动手；互不依赖的读操作合并进 actions 一次做完。
- **read_note 一次读完**：返回里 content_chars 是总字数、has_more=false 表示读完。
  has_more=false 就绝不再读同一篇；只有确实需要后续内容才按 next_offset 续读（最多一两次），
  并在回答里说明「只读了前 N 字」。
- **改对字段**：update_note 只传要改的字段，没提到的保持不变；tags 是**整体替换**，
  add_tags 追加，remove_tags 删除指定标签；「在末尾加一段」用 append_note，别整篇重写。
- **危险操作**：移入回收站、发布到博客、合并笔记（以及超大范围的改写/批量操作）会先生成
  确认卡片并结束本轮任务，不会直接执行——不要重复调用，等用户在页面上确认。
- **信息不够就反问**：任务含糊到无法安全执行（比如"改一下那篇笔记"但搜不到明确目标），
  用 final 提一个具体的问题，宁可少做不可做错。
- **跨轮上下文**：对话里可能带之前任务的记录；用户说"继续 / 刚才那篇 / 再加点"时从上下文找
  note_id，找不到就 search_notes 重新定位。若给了「已确认的计划」，严格按计划执行，
  可以微调参数但不要扩大范围。
- answer 用简洁的中文说清楚做了什么、结果如何；列出一批笔记时带上标题。"""


def _note_brief(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": note.get("id"),
        "title": note.get("title"),
        "tags": note.get("tags") or [],
        "category": note.get("category") or "",
        "is_public": bool(note.get("is_public")),
    }


def _as_int(value: Any) -> int:
    return int(value)


def _opt_int(value: Any, default: int) -> int:
    """可选整数参数：给不出合法整数就用默认值，不抛异常。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.replace("，", ",").split(",")]
    if not isinstance(value, list):
        return []
    return [str(part).strip() for part in value if str(part).strip()][:10]


def _make_tools(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """返回 {工具名: {description, params, run, observe_limit}}；run(params) -> 观察结果 dict。"""

    def search_notes(params: dict) -> dict:
        query = str(params.get("query") or "").strip()
        tag = str(params.get("tag") or "").strip()
        category = str(params.get("category") or "").strip()
        status = str(params.get("status") or "").strip()
        days = _opt_int(params.get("days"), 0)
        limit = min(max(_opt_int(params.get("limit"), 8), 1), SEARCH_LIMIT_MAX)
        if not any([query, tag, category, status, days]):
            return {"error": "至少要给一个条件：query / tag / category / status / days"}

        if query:
            candidates = repo.search_notes(conn, query, limit=SEARCH_LIMIT_MAX * 4)
            # 关键词检索按相关度排；再加筛选条件时保持相关度，不再重排
            notes = repo.filter_notes(candidates, tag=tag, category=category, status=status)
        else:
            notes = repo.filter_notes(
                repo.all_notes(conn, sort="updated"), tag=tag, category=category, status=status
            )
        if days:
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            notes = [n for n in notes if str(n.get("updated_at") or "")[:10] >= cutoff]
            if not query:
                notes = repo.sort_notes(notes, sort="updated")
        return {
            "count": len(notes[:limit]),
            "matched": len(notes),
            "notes": [_note_brief(n) for n in notes[:limit]],
        }

    def read_note(params: dict) -> dict:
        note = repo.get_note(conn, _as_int(params.get("note_id")))
        if note is None:
            return {"error": "笔记不存在"}
        content = str(note.get("content") or "")
        total = len(content)
        window = min(max(_opt_int(params.get("max_chars"), READ_WINDOW), 500), READ_WINDOW_MAX)
        offset = min(max(_opt_int(params.get("offset"), 0), 0), total)
        chunk = content[offset : offset + window]
        has_more = offset + len(chunk) < total
        result: dict[str, Any] = {
            **_note_brief(note),
            "summary": note.get("summary") or "",
            "content": chunk,
            "offset": offset,
            "content_chars": total,
            "returned_chars": len(chunk),
            "has_more": has_more,
        }
        if has_more:
            result["next_offset"] = offset + len(chunk)
            result["hint"] = (
                f"正文共 {total} 字，这次给了第 {offset + 1}-{offset + len(chunk)} 字。"
                f"只有确实需要后面的内容才续读 offset={offset + len(chunk)}；否则就动手做正事。"
            )
        else:
            result["hint"] = "整篇已经读完了（has_more=false），不要再读这篇，直接用内容完成任务。"
        return result

    def list_recent(params: dict) -> dict:
        limit = min(max(_as_int(params.get("limit") or 8), 1), 20)
        notes, total = repo.list_notes(conn, page=1, per_page=limit)
        return {"count": len(notes), "total": int(total), "notes": [_note_brief(note) for note in notes]}

    def list_tags(params: dict) -> dict:
        limit = min(max(_opt_int(params.get("limit"), 50), 1), 200)
        tags = repo.list_tags(conn, limit=limit)
        return {
            "count": len(tags),
            "tags": [{"name": t.get("name"), "count": t.get("count")} for t in tags],
        }

    def list_categories(params: dict) -> dict:
        _ = params
        items = repo.list_categories(conn)
        return {
            "count": len(items),
            "categories": [{"name": i.get("name"), "count": i.get("count")} for i in items],
        }

    def note_stats(params: dict) -> dict:
        _ = params
        notes = repo.all_notes(conn, sort="updated")
        categories = repo.list_categories(conn)
        tags = repo.list_tags(conn, limit=200)
        return {
            "notes": len(notes),
            "words": sum(int(n.get("word_count") or 0) for n in notes),
            "drafts": sum(1 for n in notes if n.get("status") == "draft"),
            "published": sum(1 for n in notes if n.get("is_public")),
            "archived": sum(1 for n in notes if n.get("is_archived")),
            "categories": len(categories),
            "tags": len(tags),
            "top_tags": [{"name": t.get("name"), "count": t.get("count")} for t in tags[:5]],
            "recent": [_note_brief(n) for n in notes[:3]],
        }

    def create_note(params: dict) -> dict:
        title = str(params.get("title") or "").strip()[:200]
        content = str(params.get("content") or "")
        if not title and not content.strip():
            return {"error": "标题和内容不能同时为空"}
        note = repo.create_note(
            conn,
            title=title or content.strip().split("\n", 1)[0][:80],
            content=content,
            tags=_as_tags(params.get("tags")),
            category=str(params.get("category") or "").strip()[:80],
            summary=str(params.get("summary") or "").strip()[:300],
            status="saved",
            is_public=bool(params.get("is_public")),
        )
        note = note or {}
        return {"created": True, "note_id": note.get("id"), "title": note.get("title")}

    def update_note(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        kwargs: dict[str, Any] = {"reason": "agent"}
        if params.get("title") is not None:
            kwargs["title"] = str(params["title"]).strip()[:200]
        if params.get("content") is not None:
            kwargs["content"] = str(params["content"])
        if params.get("summary") is not None:
            kwargs["summary"] = str(params["summary"]).strip()[:300]
        if params.get("category") is not None:
            kwargs["category"] = str(params["category"]).strip()[:80]
        if params.get("tags") is not None:
            kwargs["tags"] = _as_tags(params["tags"])
        if params.get("is_public") is not None:
            kwargs["is_public"] = bool(params["is_public"])
        updated = repo.update_note(conn, note_id, **kwargs)
        return {
            "updated": True,
            "note_id": note_id,
            "title": (updated or {}).get("title"),
            "changed": [k for k in kwargs if k != "reason"],
        }

    def add_tags(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        new_tags = _as_tags(params.get("tags"))
        if not new_tags:
            return {"error": "tags 不能为空"}
        merged = list(dict.fromkeys([*(note.get("tags") or []), *new_tags]))
        repo.update_note(conn, note_id, tags=merged, reason="agent")
        return {"updated": True, "note_id": note_id, "tags": merged, "added": new_tags}

    def remove_tags(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        drop = {name.lower() for name in _as_tags(params.get("tags"))}
        if not drop:
            return {"error": "tags 不能为空"}
        current = list(note.get("tags") or [])
        kept = [name for name in current if name.lower() not in drop]
        removed = [name for name in current if name.lower() in drop]
        if not removed:
            return {"updated": False, "note_id": note_id, "tags": current,
                    "note": "这篇笔记本来就没有这些标签，无需改动"}
        repo.update_note(conn, note_id, tags=kept, reason="agent")
        return {"updated": True, "note_id": note_id, "removed": removed, "tags": kept}

    def set_category(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        category = str(params.get("category") or "").strip()[:80]
        repo.update_note(conn, note_id, category=category, reason="agent")
        return {"updated": True, "note_id": note_id, "category": category}

    def publish_note(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        public = bool(params.get("public", True))
        slug = str(params.get("slug") or "").strip()[:120] or None
        repo.update_note(conn, note_id, is_public=public, slug=slug, reason="agent")
        blog_url = f"/blog/{note.get('slug') or slug}" if public and (note.get("slug") or slug) else ""
        return {"updated": True, "note_id": note_id, "is_public": public, "blog_url": blog_url}

    def archive_note(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        if repo.get_note(conn, note_id) is None:
            return {"error": "笔记不存在"}
        archived = bool(params.get("archived", True))
        repo.set_archived(conn, note_id, archived)
        return {"updated": True, "note_id": note_id, "archived": archived}

    def trash_note(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        if repo.get_note(conn, note_id) is None:
            return {"error": "笔记不存在"}
        ok = bool(repo.soft_delete(conn, note_id))
        return {"updated": ok, "note_id": note_id, "trashed": ok,
                "note": "已移入回收站，30 天内可以用 restore_note 找回" if ok else "移入回收站失败"}

    def restore_note(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id, include_deleted=True)
        if note is None:
            return {"error": "笔记不存在"}
        if not note.get("deleted_at"):
            return {"updated": False, "note_id": note_id, "note": "这篇笔记不在回收站里，无需恢复"}
        ok = bool(repo.restore(conn, note_id))
        return {"updated": ok, "note_id": note_id, "restored": ok}

    def pin_note(params: dict) -> dict:
        return _set_flag(params, "is_pinned", "pinned")

    def star_note(params: dict) -> dict:
        return _set_flag(params, "is_starred", "starred")

    def _set_flag(params: dict, column: str, key: str) -> dict:
        note_id = _as_int(params.get("note_id"))
        if repo.get_note(conn, note_id) is None:
            return {"error": "笔记不存在"}
        value = bool(params.get(key, True))
        repo.set_flags(conn, note_id, **{column: value})
        return {"updated": True, "note_id": note_id, key: value}

    def _apply_tags_to_many(params: dict, *, add: bool) -> dict:
        ids = params.get("note_ids")
        if not isinstance(ids, list):
            return {"error": "note_ids 必须是 id 列表（先用 search_notes 找到它们）"}
        note_ids = []
        for raw in ids[:50]:
            value = _as_int(raw)
            if value > 0:
                note_ids.append(value)
        note_ids = list(dict.fromkeys(note_ids))[:50]
        if not note_ids:
            return {"error": "note_ids 里没有合法的笔记 id"}
        tags = _as_tags(params.get("tags"))
        if not tags:
            return {"error": "tags 不能为空"}
        updated, missing = [], []
        for note_id in note_ids:
            note = repo.get_note(conn, note_id)
            if note is None:
                missing.append(note_id)
                continue
            current = list(note.get("tags") or [])
            title = str(note.get("title") or "")
            if add:
                merged = list(dict.fromkeys([*current, *tags]))
                changed = merged != current
                if changed:
                    repo.update_note(conn, note_id, tags=merged, reason="agent")
            else:
                drop = {name.lower() for name in tags}
                kept = [name for name in current if name.lower() not in drop]
                removed = [name for name in current if name.lower() in drop]
                changed = bool(removed)
                if changed:
                    repo.update_note(conn, note_id, tags=kept, reason="agent")
            updated.append({"note_id": note_id, "title": title, "changed": changed})
        return {
            "updated": len([u for u in updated if u["changed"]]),
            "unchanged": len([u for u in updated if not u["changed"]]),
            "missing": missing,
            "tags": tags,
            "notes": updated,
            "hint": "changed=false 表示本来就是这个状态，没有改动。",
        }

    def bulk_add_tags(params: dict) -> dict:
        return _apply_tags_to_many(params, add=True)

    def bulk_remove_tags(params: dict) -> dict:
        return _apply_tags_to_many(params, add=False)

    def append_note(params: dict) -> dict:
        """在笔记末尾追加一段内容。「加一段」不必读全文再整体重写。"""
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        text = str(params.get("content") or "").strip()
        if not text:
            return {"error": "content 不能为空"}
        old = str(note.get("content") or "")
        new = (old.rstrip() + "\n\n" + text) if old.strip() else text
        repo.update_note(conn, note_id, content=new, reason="agent")
        return {"updated": True, "note_id": note_id, "added_chars": len(text),
                "content_chars": len(new), "note": "已在末尾追加，并存了版本历史"}

    def list_trash(params: dict) -> dict:
        limit = min(max(_opt_int(params.get("limit"), 10), 1), 30)
        notes, total = repo.list_notes(conn, page=1, per_page=limit, include_deleted=True)
        items = []
        for note in notes:
            item = _note_brief(note)
            item["days_left"] = repo.trash_days_left(note.get("deleted_at"))
            items.append(item)
        return {"count": len(items), "total": total, "notes": items,
                "hint": "这些笔记在回收站里；restore_note 可恢复，超过剩余天数会被自动清掉。"}

    def get_note_history(params: dict) -> dict:
        """版本历史列表：模型由此拿到 version_id，再决定要不要恢复。"""
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id, include_deleted=True)
        if note is None:
            return {"error": "笔记不存在"}
        versions = repo.list_versions(conn, note_id)[:20]
        return {
            "note_id": note_id,
            "title": note.get("title"),
            "count": len(versions),
            "versions": [{"version_id": v.get("id"), "title": v.get("title"),
                          "reason": v.get("reason"), "created_at": v.get("created_at"),
                          "size": v.get("size")} for v in versions],
            "hint": "restore_version 需要 note_id + version_id；恢复前会自动把当前内容存为新版本，随时可再恢复回来。",
        }

    def restore_version(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        version_id = _as_int(params.get("version_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        restored = repo.restore_version(conn, note_id, version_id)
        if restored is None:
            return {"error": "版本不存在（先用 get_note_history 查到 version_id 再恢复）"}
        return {"restored": True, "note_id": note_id, "version_id": version_id,
                "title": restored.get("title"),
                "note": "已恢复到该版本；恢复前的内容也自动存了版本历史，可再恢复回来"}

    def list_backlinks(params: dict) -> dict:
        note_id = _as_int(params.get("note_id"))
        note = repo.get_note(conn, note_id)
        if note is None:
            return {"error": "笔记不存在"}
        items = repo.backlinks(conn, note_id)[:20]
        return {"note_id": note_id, "title": note.get("title"), "count": len(items),
                "notes": [_note_brief(n) for n in items],
                "hint": "这些笔记的正文里链接到了本篇（[[双链]]）。"}

    def semantic_search(params: dict) -> dict:
        from . import ai_embed
        query = str(params.get("query") or "").strip()
        if not query:
            return {"error": "query 不能为空"}
        limit = min(max(_opt_int(params.get("limit"), 6), 1), SEARCH_LIMIT_MAX)
        try:
            hits = ai_embed.retrieve(conn, query, limit=limit)
        except Exception:
            logger.warning("agent semantic_search 失败", exc_info=True)
            hits = None
        if not hits:
            return {"error": "语义检索不可用（向量索引未构建或未配置向量模型），改用 search_notes 关键词检索"}
        notes = []
        for hit in hits[:limit]:
            try:
                brief = _note_brief(hit)
            except Exception:
                continue
            brief["score"] = round(float(hit.get("score") or 0), 3)
            if hit.get("snippet"):
                brief["snippet"] = str(hit["snippet"])[:200]
            notes.append(brief)
        return {"count": len(notes), "query": query, "notes": notes,
                "hint": "语义检索按含义匹配（score 越高越相关），命中词未必出现在正文里。"}

    def merge_notes(params: dict) -> dict:
        """多篇合并进一篇：源笔记进回收站（可恢复），目标原正文存版本历史。"""
        target_id = _as_int(params.get("target_id"))
        target = repo.get_note(conn, target_id)
        if target is None:
            return {"error": "目标笔记不存在"}
        raw_ids = params.get("source_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return {"error": "source_ids 必须是非空的笔记 id 列表（先用 search_notes 找到它们）"}
        source_ids = []
        for raw in raw_ids[:20]:
            value = _as_int(raw)
            if value > 0 and value != target_id:
                source_ids.append(value)
        source_ids = list(dict.fromkeys(source_ids))
        if not source_ids:
            return {"error": "source_ids 里没有合法的源笔记 id（不能包含目标本身）"}
        sources = []
        for source_id in source_ids:
            note = repo.get_note(conn, source_id)
            if note is None:
                return {"error": f"源笔记 #{source_id} 不存在"}
            sources.append(note)
        parts = [str(target.get("content") or "").rstrip()]
        merged_titles = []
        for note in sources:
            title = str(note.get("title") or f"笔记 #{note.get('id')}")
            body = str(note.get("content") or "").strip()
            parts.append(f"## 来自《{title}》\n\n{body}" if body else f"## 来自《{title}》\n\n（原文为空）")
            merged_titles.append(title)
        new_content = "\n\n".join(part for part in parts if part)
        repo.update_note(conn, target_id, content=new_content, reason="agent-merge")
        trashed = []
        for note in sources:
            if repo.soft_delete(conn, int(note["id"])):
                trashed.append({"id": note["id"], "title": note.get("title")})
        return {"merged": True, "target_id": target_id, "target_title": target.get("title"),
                "merged_notes": merged_titles, "trashed": trashed,
                "content_chars": len(new_content),
                "note": "源笔记已移入回收站（30 天内可恢复）；合并前的目标正文存了版本历史"}

    return {
        "search_notes": {
            "description": "按关键词和/或标签、分类、状态、最近天数检索笔记，返回 id、标题、标签、分类",
            "params": {
                "query": "可选，关键词（搜标题/正文/标签）",
                "tag": "可选，按标签精确筛",
                "category": "可选，按分类精确筛",
                "status": "可选，draft 草稿 / saved 已保存",
                "days": "可选，只看最近 N 天更新过的",
                "limit": "可选，默认 8，最多 10",
            },
            "run": search_notes,
        },
        "read_note": {
            "description": "读一篇笔记（一次基本给整篇；超长时可翻页，has_more=false 表示读完）",
            "params": {
                "note_id": "必填，笔记 id",
                "offset": "可选，从第几个字开始读（默认 0）",
                "max_chars": "可选，这次读多少字（默认 8000，上限 30000）",
            },
            "run": read_note,
            "observe_limit": READ_OBSERVE_LIMIT,
        },
        "list_recent": {
            "description": "列出最近更新的笔记",
            "params": {"limit": "可选，默认 8"},
            "run": list_recent,
        },
        "list_tags": {
            "description": "列出所有标签及各自笔记数（想知道有哪些标签、避免拼错时先查这个）",
            "params": {"limit": "可选，默认 50"},
            "run": list_tags,
        },
        "list_categories": {
            "description": "列出所有分类及各自笔记数",
            "params": {},
            "run": list_categories,
        },
        "note_stats": {
            "description": "笔记库总览：篇数、总字数、草稿/公开/归档数、分类与标签数量、最近几篇",
            "params": {},
            "run": note_stats,
        },
        "create_note": {
            "description": "新建一篇笔记",
            "params": {"title": "标题", "content": "正文（Markdown）", "tags": "标签列表，可选",
                        "category": "分类，可选", "summary": "摘要，可选", "is_public": "是否公开到博客，默认否"},
            "run": create_note,
        },
        "update_note": {
            "description": "修改笔记的标题/正文/摘要/分类/标签（只传要改的字段；tags 是整体替换，自动存版本历史）",
            "params": {"note_id": "必填", "title": "可选", "content": "可选", "summary": "可选",
                        "category": "可选", "tags": "可选，整体替换标签", "is_public": "可选"},
            "run": update_note,
        },
        "add_tags": {
            "description": "给笔记追加标签（与现有标签合并，不会覆盖掉原来的）",
            "params": {"note_id": "必填", "tags": "要追加的标签列表"},
            "run": add_tags,
        },
        "remove_tags": {
            "description": "删掉笔记上的指定标签（其它标签保留）",
            "params": {"note_id": "必填", "tags": "要删除的标签列表"},
            "run": remove_tags,
        },
        "set_category": {
            "description": "设置笔记的分类（传空字符串表示清除分类）",
            "params": {"note_id": "必填", "category": "分类名"},
            "run": set_category,
        },
        "publish_note": {
            "description": "把笔记公开到博客（public=false 表示取消公开）",
            "params": {"note_id": "必填", "public": "默认 true", "slug": "可选，博客地址名"},
            "run": publish_note,
        },
        "archive_note": {
            "description": "归档 / 取消归档（归档是温和的收起，不是删除）",
            "params": {"note_id": "必填", "archived": "默认 true，false 表示取消归档"},
            "run": archive_note,
        },
        "trash_note": {
            "description": "把笔记移入回收站（软删除，30 天内可恢复）——只在用户明确要求删除时用。"
                           "调用后不会立即执行，会生成确认卡片等用户确认",
            "params": {"note_id": "必填"},
            "run": trash_note,
        },
        "bulk_add_tags": {
            "description": "给一批笔记批量追加标签（与各自现有标签合并；一次最多 50 篇，先 search_notes 拿 id）",
            "params": {"note_ids": "笔记 id 列表", "tags": "要追加的标签列表"},
            "run": bulk_add_tags,
            "observe_limit": 2600,
        },
        "bulk_remove_tags": {
            "description": "从一批笔记批量删掉指定标签（其它标签保留，一次最多 50 篇）",
            "params": {"note_ids": "笔记 id 列表", "tags": "要删除的标签列表"},
            "run": bulk_remove_tags,
            "observe_limit": 2600,
        },
        "append_note": {
            "description": "在笔记末尾追加一段内容（保留原有正文，自动存版本历史）——「加一段」用它，别整篇重写",
            "params": {"note_id": "必填", "content": "要追加的正文（Markdown）"},
            "run": append_note,
        },
        "list_trash": {
            "description": "列出回收站里的笔记（含剩余可恢复天数）",
            "params": {"limit": "可选，默认 10"},
            "run": list_trash,
        },
        "restore_note": {
            "description": "把笔记从回收站恢复回来",
            "params": {"note_id": "必填"},
            "run": restore_note,
        },
        "get_note_history": {
            "description": "列出笔记的版本历史（version_id、时间、原因、大小）——想撤销修改先查这个",
            "params": {"note_id": "必填"},
            "run": get_note_history,
        },
        "restore_version": {
            "description": "把笔记恢复到某个历史版本（恢复前自动把当前内容存为新版本，可再恢复回来）",
            "params": {"note_id": "必填", "version_id": "必填，先 get_note_history 查到"},
            "run": restore_version,
        },
        "list_backlinks": {
            "description": "列出链接到这篇笔记的其他笔记（[[双链]]引用了它的）",
            "params": {"note_id": "必填"},
            "run": list_backlinks,
        },
        "semantic_search": {
            "description": "语义检索：按「意思相近」找笔记，命中词不必出现在正文里（关键词搜不到时用它）",
            "params": {"query": "必填，自然语言描述想找的内容", "limit": "可选，默认 6"},
            "run": semantic_search,
        },
        "merge_notes": {
            "description": "把多篇笔记合并进一篇：源笔记正文并入目标（带来源小节），源笔记移入回收站。"
                           "合并前会生成确认卡片等用户确认",
            "params": {"target_id": "必填，合并进哪篇", "source_ids": "必填，被合并的笔记 id 列表"},
            "run": merge_notes,
        },
        "pin_note": {
            "description": "置顶 / 取消置顶",
            "params": {"note_id": "必填", "pinned": "默认 true，false 表示取消置顶"},
            "run": pin_note,
        },
        "star_note": {
            "description": "加星标 / 取消星标",
            "params": {"note_id": "必填", "starred": "默认 true，false 表示取消星标"},
            "run": star_note,
        },
    }

# 主循环
# ---------------------------------------------------------------------------
def _chat_with_retry(conn: sqlite3.Connection, messages: list[dict[str, str]]):
    """调一次模型，失败自动重试。返回 (回复文本, 错误)；后者为 None 表示成功。"""
    last_error: ai.AIError | None = None
    for attempt in range(CHAT_RETRIES):
        try:
            return ai.chat(messages, task="agent", conn=conn, temperature=0.0, max_tokens=2000), None
        except ai.AIError as exc:
            last_error = exc
            if attempt + 1 < CHAT_RETRIES:
                time.sleep(1.0)
    return None, last_error


def _partial_answer(steps: list[dict[str, Any]], error: Exception) -> str:
    """模型挂了但已经做了一些事：把进度如实交付，并给出可操作的补救建议。

    以前这种情况直接返回空回答 —— 用户白等几十秒，连做过什么都看不到。
    """
    lines = [f"没能跑完：{str(error)[:200]}"]
    done = [str(step.get("summary") or "") for step in steps if step.get("summary")]
    if done:
        lines.append("已经完成的步骤：")
        lines.extend(f"- {item}" for item in done[-8:])
        lines.append("上面这些改动都已经生效了。")
    lines.append("可以在设置页把「AI 超时」调大（默认 45 秒），或者把任务拆小一点再试。")
    return "\n".join(lines)


def _last_run_recap(conn: sqlite3.Connection) -> str:
    """把最近一次任务压成一小段存档，给「继续 / 刚才那篇」这类跨轮指代兜底。"""
    try:
        runs = list_runs(conn, limit=1)
    except Exception:
        return ""
    if not runs:
        return ""
    last = runs[0]
    notes = [f"#{n.get('id')}《{n.get('title')}》"
             for n in (last.get("notes") or []) if isinstance(n, dict) and n.get("id")]
    parts = [f"任务：{str(last.get('task') or '').strip()[:120]}"]
    if notes:
        parts.append("涉及的笔记：" + "、".join(notes[:6]))
    answer = str(last.get("answer") or "").strip()
    if answer:
        parts.append("上次的结果：" + answer[:200])
    if last.get("error"):
        parts.append("上次中止原因：" + str(last.get("error"))[:80])
    if len(parts) == 1 and not notes:
        return ""
    return ("（上一轮任务的存档：只有用户说「继续 / 刚才那篇 / 再加点」这类指代时才参考，"
            "不要当成新任务重复执行）\n" + "\n".join(parts))


def _clean_history(history: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """把前端送来的会话历史收敛成安全形状：只留 user/assistant、限条数与长度。"""
    cleaned: list[dict[str, str]] = []
    if not isinstance(history, list):
        return cleaned
    for item in history[-MAX_HISTORY_TURNS * 2:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            cleaned.append({"role": role, "content": content[:MAX_HISTORY_CHARS]})
    return cleaned


def _extract_json(raw: str) -> dict | None:
    """从模型回复里抠出 JSON 对象；容忍代码块包裹和前后废话。"""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text, flags=re.S).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _describe_tools(tools: dict[str, dict]) -> str:
    blocks = []
    for name, spec in tools.items():
        params = "；".join(f"{key}：{desc}" for key, desc in spec["params"].items())
        blocks.append(f"- {name}：{spec['description']}（参数：{params}）")
    return "\n".join(blocks)


def _summarize_step(name: str, result: dict) -> str:
    if result.get("error"):
        return f"{name} 失败：{result['error']}"
    if name == "search_notes" or name == "list_recent":
        titles = "、".join(str(note.get("title")) for note in result.get("notes", [])[:5])
        return f"{name} 命中 {result.get('count', 0)} 篇：{titles}"
    if name == "create_note":
        return f"已创建笔记 #{result.get('note_id')}"
    if name == "read_note":
        return f"已读取《{result.get('title')}》"
    if name == "restore_version":
        return f"已把《{result.get('title')}》恢复到指定版本"
    if name == "merge_notes":
        return f"已合并 {len(result.get('merged_notes') or [])} 篇进《{result.get('target_title')}》"
    if name == "semantic_search":
        return f"语义检索命中 {result.get('count', 0)} 篇"
    if name == "get_note_history":
        return f"查到 {result.get('count', 0)} 个历史版本"
    if name == "list_backlinks":
        return f"查到 {result.get('count', 0)} 篇反向链接"
    if result.get("updated"):
        return f"已更新笔记 #{result.get('note_id')}"
    return f"{name} 完成"


# ---------------------------------------------------------------------------
# 执行历史（落库审计）：meta 表 agent.runs 存最近 MAX_RUNS 条
# ---------------------------------------------------------------------------
def _notes_list(involved: dict[int, str]) -> list[dict[str, Any]]:
    return [{"id": note_id, "title": title} for note_id, title in involved.items()]


def _needs_confirm(action: str, params: dict, conn: sqlite3.Connection) -> bool:
    """危险操作的判定：动作级（_CONFIRM_TOOLS 写死）+ 条件级（批量篇数 / 大改写）。

    条件级只往「多确认」的方向误报（确认总是安全的），判定必须便宜：
    大改写用 4000 字头的相似度近似，避免长正文上的性能坑。
    """
    if action in _CONFIRM_TOOLS:
        if action == "publish_note":
            return bool(params.get("public", True))   # 取消公开（public=false）不拦
        return True
    if action in ("bulk_add_tags", "bulk_remove_tags"):
        ids = params.get("note_ids")
        return isinstance(ids, list) and len(ids) > CONFIRM_BULK_THRESHOLD
    if action == "update_note":
        content = params.get("content")
        if content is None:
            return False
        note = repo.get_note(conn, _as_int(params.get("note_id")))
        old = str((note or {}).get("content") or "")
        if len(old) < CONFIRM_REWRITE_MIN_CHARS:
            return False
        new = str(content)
        if new.rstrip() == old.rstrip():
            return False
        return SequenceMatcher(None, old[:4000], new[:4000]).ratio() < CONFIRM_REWRITE_RATIO
    return False


# 可能出现在确认卡片上的动作全集（execute_pending 的白名单）
_ALL_CONFIRMABLE = frozenset(_CONFIRM_TOOLS) | {"bulk_add_tags", "bulk_remove_tags", "update_note"}


def _confirm_meta(action: str, params: dict) -> tuple[str, str, str]:
    """确认卡片的 (label, consequence, 主体标题)。"""
    meta = _CONFIRM_META.get(action) or {"label": action, "consequence": "该操作影响面较大，请确认"}
    if action in ("bulk_add_tags", "bulk_remove_tags"):
        ids = params.get("note_ids") if isinstance(params.get("note_ids"), list) else []
        verb = "批量追加标签" if action == "bulk_add_tags" else "批量删除标签"
        return (f"{verb}（{len(ids)} 篇）",
                f"将给 {len(ids)} 篇笔记{'追加' if action == 'bulk_add_tags' else '删除'}指定标签",
                f"{len(ids)} 篇笔记")
    return meta["label"], meta["consequence"], ""


def _record_run(
    conn: sqlite3.Connection,
    task: str,
    *,
    ok: bool,
    answer: str,
    steps: list[dict[str, Any]],
    error: str,
    read_only: bool,
    dry_run: bool = False,
    involved: dict[int, str] | None = None,
    duration_ms: int | None = None,
    cancelled: bool = False,
) -> None:
    """把一次任务落进执行历史（审计用）。绝不抛异常——审计挂了不能连累任务。"""
    try:
        runs = list_runs(conn)
        runs.insert(0, {
            "id": uuid4().hex[:10],
            "at": now_iso(),
            "task": (task or "").strip()[:500],
            "ok": bool(ok),
            "error": (error or "")[:300],
            "answer": (answer or "")[:300],
            "read_only": bool(read_only),
            "dry_run": bool(dry_run),
            "cancelled": bool(cancelled),
            "duration_ms": int(duration_ms or 0),
            "steps": [{"tool": s.get("tool"), "summary": s.get("summary")} for s in steps][:MAX_STEPS],
            "notes": [{"id": n.get("id"), "title": n.get("title")}
                      for n in _notes_list(involved or {})],
        })
        runs = runs[:MAX_RUNS]
        repo.save_meta_map(conn, {"runs": json.dumps(runs, ensure_ascii=False)}, prefix="agent.")
    except Exception:
        logger.warning("agent 执行历史落库失败", exc_info=True)


def list_runs(conn: sqlite3.Connection, limit: int = MAX_RUNS) -> list[dict[str, Any]]:
    """最近的执行历史（新→旧）。坏数据静默跳过。"""
    try:
        meta = repo.get_meta_map(conn, "agent.")
        runs = json.loads(str(meta.get("runs") or "[]"))
    except Exception:
        return []
    if not isinstance(runs, list):
        return []
    return [r for r in runs if isinstance(r, dict)][: max(0, min(limit, MAX_RUNS))]


def clear_runs(conn: sqlite3.Connection) -> None:
    try:
        repo.save_meta_map(conn, {"runs": "[]"}, prefix="agent.")
    except Exception:
        logger.warning("agent 执行历史清空失败", exc_info=True)


# ---------------------------------------------------------------------------
# 危险操作确认：meta: agent.pending 存一张待确认卡片；
# 用户在页面点「确认执行」→ execute_pending 真正落地（绕过模型，机制层兜底）
# ---------------------------------------------------------------------------
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
        result = spec["run"](pending.get("params") or {})
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


def ensure_run_ids(conn: sqlite3.Connection) -> None:
    """给没有 id 的历史记录补上 id（早期记录没有这个字段，单条删除需要它）。

    读时归一化：只在确实缺 id 时才写回，幂等。
    """
    try:
        runs = list_runs(conn)
        if not any(not run.get("id") for run in runs):
            return
        for run in runs:
            if not run.get("id"):
                run["id"] = uuid4().hex[:10]
        repo.save_meta_map(conn, {"runs": json.dumps(runs, ensure_ascii=False)}, prefix="agent.")
    except Exception:
        logger.warning("agent 执行历史补 id 失败", exc_info=True)


def delete_run(conn: sqlite3.Connection, run_id: str) -> bool:
    """删掉执行历史里的某一条。返回是否真的删了。"""
    try:
        runs = list_runs(conn)
        kept = [run for run in runs if run.get("id") != str(run_id)]
        if len(kept) == len(runs):
            return False
        repo.save_meta_map(conn, {"runs": json.dumps(kept, ensure_ascii=False)}, prefix="agent.")
        return True
    except Exception:
        logger.warning("agent 执行历史单条删除失败", exc_info=True)
        return False


def _planned_actions(data: dict) -> list[tuple[str, dict]]:
    """协议 v2：单轮可带 actions 数组并做多个独立工具；兼容旧的单数 action。

    最多 MAX_ACTIONS_PER_TURN 个；final 不在这里处理。坏形状返回空列表。
    """
    planned: list[tuple[str, dict]] = []
    raw_list = data.get("actions")
    if isinstance(raw_list, list):
        for item in raw_list[:MAX_ACTIONS_PER_TURN]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("action") or "").strip()
            if not name or name == "final":
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            planned.append((name, params))
    if not planned:
        name = str(data.get("action") or "").strip()
        if name and name != "final":
            params = data.get("params") if isinstance(data.get("params"), dict) else {}
            planned.append((name, params))
    return planned[:MAX_ACTIONS_PER_TURN]


def iter_agent_events(
    conn: sqlite3.Connection,
    task: str,
    *,
    max_steps: int = MAX_STEPS,
    read_only: bool = False,
    dry_run: bool = False,
    history: list[dict[str, str]] | None = None,
    run_id: str | None = None,
    confirmed_plan: str | None = None,
):
    """agent 循环的流式版本：每完成一步就 yield 一个事件 dict，而不是干等。

    事件类型（SSE 里每个 `data: ` 行的内容）：
      {"type": "step", "tool": ..., "params": {...}, "summary": ..., "run_id": ...}
      {"type": "final", "ok": bool, "answer": ..., "steps": [...], "error": ...,
       "run_id": ..., "duration_ms": ..., "cancelled": bool}
      {"type": "error", "error": ...}（未配置 AI 等前置失败，没有 final）

    `run_agent` 就是它的消费者：吃掉 step/final/error 后拼回原来的 dict。

    取消：request_cancel(run_id) 置位后，循环在**下一个步骤边界**安全停下
    （正在进行的模型调用不会被掐断）；run_id 由调用方传入或自动生成。
    """
    if not ai.is_enabled():
        yield {"type": "error", "error": "尚未配置 AI 服务"}
        return
    task = (task or "").strip()
    if not task:
        yield {"type": "error", "error": "任务不能为空"}
        return

    run_id = run_id or new_run_id()
    cancel_event = _register_run(run_id)
    started = time.time()
    tools = _make_tools(conn)
    system = SYSTEM_PROMPT.format(tools=_describe_tools(tools), today=_today_label(),
                                  max_actions=MAX_ACTIONS_PER_TURN)
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    recap = _last_run_recap(conn)
    if recap:
        # 补上「上一轮动过哪篇笔记」——history 里只有最终回答，note_id 常常已经丢了，
        # 于是用户说「继续」时模型只能反问（真实踩过：0 步就交白卷）
        messages.append({"role": "system", "content": recap})
    plan_text = str(confirmed_plan or "").strip()[:PLAN_MAX_CHARS]
    if plan_text:
        # 两段式任务流的后半段：用户在计划里审阅过这份计划，这里严格照做
        messages.append({"role": "system", "content":
            "用户已在计划中审阅并确认了以下计划，请严格按计划执行：可以微调参数，"
            "不要扩大范围、不要添加计划之外的大动作。\n" + plan_text})
    messages.extend(_clean_history(history))
    messages.append({"role": "user", "content": task})

    steps: list[dict[str, Any]] = []
    involved: dict[int, str] = {}   # 本次任务动过/读过的笔记 id -> 标题（给前端做链接）
    seen_calls: set[str] = set()    # 已成功执行过的「工具+参数」签名，用来拦住重复空转
    repeat_streak = 0               # 连续多少步是重复调用
    format_retries = 0              # 已经做过几次「格式失控带反馈重试」

    def _finish(ok: bool, answer: str, error: str = "") -> dict[str, Any]:
        answer = answer or ""
        _record_run(conn, task, ok=ok, answer=answer, steps=steps, error=error,
                    read_only=read_only, dry_run=dry_run, involved=involved,
                    duration_ms=_elapsed_ms(), cancelled=cancel_event.is_set())
        return {"type": "final", "ok": ok, "answer": answer, "steps": steps,
                "error": error, "notes": _notes_list(involved), "run_id": run_id,
                "duration_ms": _elapsed_ms(), "cancelled": cancel_event.is_set()}

    def _elapsed_ms() -> int:
        return int((time.time() - started) * 1000)

    def _push_observe(payload: dict, limit_chars: int) -> None:
        """把工具结果喂回模型：按工具自己的上限截断，别把长正文一刀切掉。"""
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > limit_chars:
            text = text[:limit_chars] + "…（结果过长已截断）"
        messages.append({"role": "user", "content": "工具结果：" + text})

    def _collect(note_id: Any, title: Any) -> None:
        if not isinstance(note_id, int):
            return
        if not title:
            try:
                note = repo.get_note(conn, note_id)
                title = str((note or {}).get("title") or "")
            except Exception:
                title = ""
        involved[note_id] = title or f"笔记 #{note_id}"

    def _execute_one(action: str, params: dict) -> tuple[dict, str, dict | None]:
        """执行单个动作，返回 (观察结果, 步骤摘要, 确认信息或 None)。

        只改外层的 seen_calls / repeat_streak；步数、事件与收尾由外层管。
        """
        nonlocal repeat_streak
        signature = action + ":" + json.dumps(params, ensure_ascii=False, sort_keys=True)
        spec = tools.get(action)
        confirm_info: dict | None = None

        if signature in seen_calls and spec is not None:
            # 同一个工具 + 完全一样的参数又调一次：结果不会变，别把步数烧在这儿
            repeat_streak += 1
            observation = {
                "error": "这一步刚才已经执行过（工具和参数完全相同），再执行结果也不会变。"
                         "请换个做法，或者直接用 final 给出答案。"
            }
            summary = f"跳过重复的 {action} 调用"
        elif dry_run:
            # 计划：所有工具都只「说要做什么」，不真正执行。模型会拿到固定的规划提示，
            # 想清楚全部步骤后用 final 输出「将要做的事」清单 —— 适合先审后放。
            repeat_streak = 0
            observation = {
                "dry_run": True,
                "note": "计划模式：本工具没有被真正调用。请继续规划后续步骤；独立的读操作"
                        "请合并进 actions 一轮做完，减少轮次。全部想清楚后用 final 输出"
                        "「将要做的事」清单（不要声称已执行）。",
            }
            summary = f"（计划）将执行 {action}"
            seen_calls.add(signature)
        elif read_only and action in _WRITE_TOOLS:
            repeat_streak = 0
            observation = {"error": "只读模式：本次任务不执行写操作，如需修改请关闭只读模式后重试"}
            summary = f"已拦截写操作 {action}（只读模式）"
            seen_calls.add(signature)
        elif _needs_confirm(action, params, conn):
            # 危险/大影响操作：不执行，生成确认卡片等用户点「确认执行」（机制层兜底）
            repeat_streak = 0
            ctx_key = "target_id" if action == "merge_notes" else "note_id"
            ctx_note = None
            if params.get(ctx_key) is not None:
                ctx_note = repo.get_note(conn, _as_int(params.get(ctx_key)))
                if ctx_note is None:
                    return {"error": "笔记不存在"}, f"{action} 失败：笔记不存在", None
            existing = _get_pending_op(conn)
            if (existing and existing.get("action") == action
                    and existing.get("params") == params):
                pending = existing
                repeat_streak += 1   # 反复生成同一张确认卡也按空转算
            else:
                pending = _save_pending_op(conn, action, params, ctx_note or {})
            label, consequence, subject = _confirm_meta(action, params)
            if ctx_note is not None:
                subject = str(ctx_note.get("title") or subject)
                summary = f"等待用户确认：{label}《{subject}》"
            else:
                summary = f"等待用户确认：{label}"
            observation = {
                "confirm_required": True,
                "confirm_id": pending["id"],
                "note": "确认卡片已生成，本步没有真正执行。用户在页面上点「确认执行」才会生效。",
            }
            confirm_info = {"id": pending["id"], "note_id": params.get(ctx_key),
                            "title": subject, "label": label, "consequence": consequence}
        elif spec is None:
            repeat_streak = 0
            observation = {"error": f"未知工具 {action!r}，可用工具：{', '.join(tools)}"}
            summary = f"未知工具 {action}"
        else:
            repeat_streak = 0
            try:
                observation = spec["run"](params)  # type: ignore[operator]
            except (TypeError, ValueError, OverflowError) as exc:
                observation = {"error": f"参数不合法：{exc}"}
            except Exception as exc:  # 工具内部出错也不能让整个 agent 崩
                logger.warning("agent 工具 %s 执行失败", action, exc_info=True)
                observation = {"error": f"工具执行失败：{exc}"}
            summary = _summarize_step(action, observation)
            # 只有真的执行过才算「做过」；报错的调用允许换个参数重试
            if not (isinstance(observation, dict) and observation.get("error")):
                seen_calls.add(signature)
        return observation, summary, confirm_info

    try:
        for _step_no in range(max_steps):
            if cancel_event.is_set():
                done = f"已完成 {len(steps)} 步，" if steps else ""
                yield _finish(True, f"任务已按你的要求取消。{done}已完成的操作都生效了。")
                return

            raw, last_error = _chat_with_retry(conn, messages)
            if last_error is not None:
                # 模型调用重试后仍挂：**不丢掉已完成的步骤**，把进度和补救建议一起交付
                yield _finish(False, _partial_answer(steps, last_error), error=str(last_error))
                return

            data = _extract_json(raw)
            if data is None:
                # 模型没按格式回：带反馈让它重试几次；仍不行就把原话当最终回答收场
                if format_retries < MAX_FORMAT_RETRIES:
                    format_retries += 1
                    messages.append({"role": "assistant", "content": (raw or "").strip()[:2000]})
                    messages.append({"role": "user", "content":
                        "你上一条回复不符合约定格式。只输出一个 JSON 对象："
                        '调工具用 {"action": "工具名", "params": {...}}'
                        '（多个独立工具用 {"actions": [...]}），'
                        '完成用 {"action": "final", "answer": "..."}。'
                        "不要输出其他文字或代码块。"})
                    continue
                yield _finish(True, (raw or "").strip())
                return

            action = str(data.get("action") or "").strip()
            if action == "final":
                yield _finish(True, str(data.get("answer") or "").strip())
                return

            planned = _planned_actions(data)
            if not planned:
                if format_retries < MAX_FORMAT_RETRIES:
                    format_retries += 1
                    messages.append({"role": "assistant",
                                     "content": json.dumps(data, ensure_ascii=False)})
                    messages.append({"role": "user", "content":
                        "这一步没有可执行的工具调用（action 不是已知工具）。"
                        "请重新输出一个 JSON 对象。"})
                    continue
                yield _finish(True, "我没有看懂这一步的指令格式，先停下来了。已完成的操作都生效了。")
                return

            # ---- 执行本轮动作（协议 v2：单轮可并做多个独立工具） ----
            format_retries = 0          # 能正常出招了就重置格式重试计数
            round_observations: list[dict[str, Any]] = []
            observe_limits: list[int] = []
            stop_round = False
            last_confirm: dict | None = None
            for sub_action, sub_params in planned:
                if len(steps) >= max_steps:
                    break
                if cancel_event.is_set():
                    stop_round = True
                    break
                observation, summary, confirm_info = _execute_one(sub_action, sub_params)
                if isinstance(observation, dict):
                    _collect(observation.get("note_id"), observation.get("title"))
                    _collect(observation.get("id"), observation.get("title"))
                    for item in observation.get("notes") or []:
                        if isinstance(item, dict):
                            _collect(item.get("id"), item.get("title"))
                    if observation.get("merged"):
                        _collect(observation.get("target_id"), observation.get("target_title"))
                step = {"tool": sub_action, "summary": summary, "params": sub_params}
                steps.append(step)
                # 先把这一步推给前端，再准备下一轮——这就是「流式」的核心
                event: dict[str, Any] = {"type": "step", "tool": sub_action, "summary": summary,
                                         "params": sub_params, "notes": _notes_list(involved),
                                         "run_id": run_id}
                if confirm_info:
                    event["confirm"] = confirm_info
                yield event
                round_observations.append({"action": sub_action, "result": observation})
                observe_limits.append(
                    int((tools.get(sub_action) or {}).get("observe_limit") or OBSERVE_LIMIT))
                if confirm_info:
                    # 等用户确认期间别继续执行本轮剩余动作，避免在未确认状态下叠加操作
                    last_confirm = confirm_info
                    stop_round = True
                    break

            if not round_observations:
                continue   # 本轮没动任何工具（步数耗尽/取消），交给外层判断收尾

            messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
            _push_observe({"results": round_observations},
                          max(observe_limits) if observe_limits else OBSERVE_LIMIT)

            if cancel_event.is_set():
                done = f"已完成 {len(steps)} 步，" if steps else ""
                yield _finish(True, f"任务已按你的要求取消。{done}已完成的操作都生效了。")
                return

            if repeat_streak >= MAX_REPEAT_STEPS:
                answer = (
                    f"我卡在重复操作上了（同一个调用连着做了 {repeat_streak} 次，结果不会变），先停下来。"
                    "已经完成的操作都生效了。你可以把任务说得更具体一点，或者告诉我下一步做什么。"
                )
                yield _finish(True, answer)
                return

            if stop_round and last_confirm:
                # 生成了确认卡片：本轮到此为止，等用户在页面上确认（不再烧模型调用）
                yield _finish(True, "已生成确认卡片，等你在页面上点「确认执行」。确认前我不会继续操作。")
                return

        # 步数耗尽还没收尾：安全停下，绝不无限循环
        answer = "步骤太多，我先停下来了。已经完成的操作都生效了，你可以继续给我补充指令。"
        yield _finish(True, answer)
    finally:
        _release_run(run_id)


def run_agent(
    conn: sqlite3.Connection,
    task: str,
    *,
    max_steps: int = MAX_STEPS,
    read_only: bool = False,
    dry_run: bool = False,
    history: list[dict[str, str]] | None = None,
    run_id: str | None = None,
    confirmed_plan: str | None = None,
) -> dict[str, Any]:
    """跑一次 agent 循环。等价于消费 `iter_agent_events` 并拼回 {ok, answer, steps, error}。

    保留旧签名与返回值结构，原有测试无需改动即可全绿。
    """
    final: dict[str, Any] | None = None
    for event in iter_agent_events(conn, task, max_steps=max_steps, read_only=read_only,
                                   dry_run=dry_run, history=history, run_id=run_id,
                                   confirmed_plan=confirmed_plan):
        if event["type"] == "error":
            return {"ok": False, "answer": "", "steps": [], "error": event["error"]}
        if event["type"] == "final":
            final = event
            break
        # step 事件：步骤已包含在最终 final 的 steps 里，这里忽略即可
    if final is None:
        return {"ok": False, "answer": "", "steps": [], "error": "agent 没有产生结果",
                "cancelled": False, "duration_ms": 0}
    return {
        "ok": bool(final.get("ok")),
        "answer": final.get("answer") or "",
        "steps": final.get("steps") or [],
        "error": final.get("error") or "",
        "cancelled": bool(final.get("cancelled")),
        "duration_ms": int(final.get("duration_ms") or 0),
    }
