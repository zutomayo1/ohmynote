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
import time
from datetime import date, timedelta
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

_WRITE_TOOLS = frozenset({
    "create_note", "update_note", "add_tags", "remove_tags", "set_category",
    "publish_note", "archive_note", "trash_note", "restore_note", "pin_note", "star_note",
})

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _today_label() -> str:
    """给模型一个明确的「今天」。没有它，「这周/最近三天」这类任务没法算。"""
    today = date.today()
    return f"{today.isoformat()}（{_WEEKDAYS[today.weekday()]}）"


SYSTEM_PROMPT = """你是墨痕笔记应用里的笔记助手 Agent。用户会用自然语言给你任务，
你通过调用工具多步完成任务。今天是 {today}。可用工具：

{tools}

每一轮你只能输出一个 JSON 对象（不要输出任何别的文字、不要用代码块包裹）：

1. 调工具：{{"action": "工具名", "params": {{...}}}}
2. 任务完成：{{"action": "final", "answer": "给用户看的最终回答"}}

行动准则：
- 动手前先查一次就够：写操作（create/update/…）之前，先用 search_notes 或 read_note
  确认目标笔记确实存在，绝对不要凭空编造 note_id。不要反复确认同一件事。
- 一步能做完就别拆开：拿到 note_id 后直接动手，把能合并的操作并到尽量少的步骤里。
- read_note 一次基本就把整篇给你了（返回里的 content_chars 是总字数，
  returned_chars 是这次给了多少）。**has_more=false 就说明这篇已经读完了，
  绝对不要再用不同 offset 反复读同一篇**；只有 has_more=true 且确实需要后面的内容时
  才续读，最多续读一两次，并在最终回答里说明「只读了前 N 字」。
- update_note 只传需要修改的字段，没有提到的保持不变。注意语义差别：
  update_note 的 tags 是**整体替换**，add_tags 是追加，remove_tags 是删除指定标签。
- 危险操作（trash_note 移入回收站）只在用户明确要求时做，回答里要说清楚去哪找回来。
- 信息不够就反问：如果任务含糊到无法安全执行（比如"改一下那篇笔记"但搜不到明确目标），
  用 final 提一个具体的问题让用户补充，宁可少做不可做错。
- 对话可能包含之前的任务记录：用户说"继续 / 刚才那篇 / 再加点"时，从上下文里找对应的
  note_id；找不到就用 search_notes 重新定位。
- answer 用简洁的中文说清楚你做了什么、结果如何；列出一批笔记时带上标题。"""


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
            "description": "把笔记移入回收站（软删除，30 天内可恢复）——只在用户明确要求删除时用",
            "params": {"note_id": "必填"},
            "run": trash_note,
        },
        "restore_note": {
            "description": "把笔记从回收站恢复回来",
            "params": {"note_id": "必填"},
            "run": restore_note,
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
            return ai.chat(messages, task="agent", conn=conn, temperature=0.0, max_tokens=1200), None
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
    if result.get("updated"):
        return f"已更新笔记 #{result.get('note_id')}"
    return f"{name} 完成"


# ---------------------------------------------------------------------------
# 执行历史（落库审计）：meta 表 agent.runs 存最近 MAX_RUNS 条
# ---------------------------------------------------------------------------
def _notes_list(involved: dict[int, str]) -> list[dict[str, Any]]:
    return [{"id": note_id, "title": title} for note_id, title in involved.items()]


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


def iter_agent_events(
    conn: sqlite3.Connection,
    task: str,
    *,
    max_steps: int = MAX_STEPS,
    read_only: bool = False,
    dry_run: bool = False,
    history: list[dict[str, str]] | None = None,
):
    """agent 循环的流式版本：每完成一步就 yield 一个事件 dict，而不是干等。

    事件类型（SSE 里每个 `data: ` 行的内容）：
      {"type": "step", "tool": ..., "params": {...}, "summary": ...}
      {"type": "final", "ok": bool, "answer": ..., "steps": [...], "error": ...}
      {"type": "error", "error": ...}（未配置 AI 等前置失败，没有 final）

    `run_agent` 就是它的消费者：吃掉 step/final/error 后拼回原来的 dict。
    """
    if not ai.is_enabled():
        yield {"type": "error", "error": "尚未配置 AI 服务"}
        return
    task = (task or "").strip()
    if not task:
        yield {"type": "error", "error": "任务不能为空"}
        return

    tools = _make_tools(conn)
    system = SYSTEM_PROMPT.format(tools=_describe_tools(tools), today=_today_label())
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    recap = _last_run_recap(conn)
    if recap:
        # 补上「上一轮动过哪篇笔记」——history 里只有最终回答，note_id 常常已经丢了，
        # 于是用户说「继续」时模型只能反问（真实踩过：0 步就交白卷）
        messages.append({"role": "system", "content": recap})
    messages.extend(_clean_history(history))
    messages.append({"role": "user", "content": task})

    steps: list[dict[str, Any]] = []
    involved: dict[int, str] = {}   # 本次任务动过/读过的笔记 id -> 标题（给前端做链接）
    seen_calls: set[str] = set()    # 已成功执行过的「工具+参数」签名，用来拦住重复空转
    repeat_streak = 0               # 连续多少步是重复调用

    def _push_observe(payload: dict, limit_chars: int) -> None:
        """把工具结果喂回模型：按工具自己的上限截断，别把长正文一刀切掉。"""
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > limit_chars:
            text = text[:limit_chars] + "…（结果过长已截断）"
        messages.append({"role": "user", "content": "工具结果：" + text})

    for _step_no in range(max_steps):
        raw, last_error = _chat_with_retry(conn, messages)
        if last_error is not None:
            # 模型调用重试后仍挂：**不丢掉已完成的步骤**，把进度和补救建议一起交付
            answer = _partial_answer(steps, last_error)
            _record_run(conn, task, ok=False, answer=answer, steps=steps,
                        error=str(last_error), read_only=read_only, dry_run=dry_run, involved=involved)
            yield {"type": "final", "ok": False, "answer": answer, "steps": steps,
                   "error": str(last_error), "notes": _notes_list(involved)}
            return

        data = _extract_json(raw)
        if data is None:
            # 模型没按格式回：直接把原话当最终回答收场，不再空转
            _record_run(conn, task, ok=True, answer=(raw or "").strip(), steps=steps,
                        error="", read_only=read_only, dry_run=dry_run, involved=involved)
            yield {"type": "final", "ok": True, "answer": (raw or "").strip(),
                  "steps": steps, "error": "", "notes": _notes_list(involved)}
            return

        action = str(data.get("action") or "").strip()
        if action == "final":
            _record_run(conn, task, ok=True, answer=str(data.get("answer") or "").strip(),
                        steps=steps, error="", read_only=read_only, dry_run=dry_run, involved=involved)
            yield {"type": "final", "ok": True,
                  "answer": str(data.get("answer") or "").strip(), "steps": steps, "error": "",
                  "notes": _notes_list(involved)}
            return

        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        signature = action + ":" + json.dumps(params, ensure_ascii=False, sort_keys=True)
        spec = tools.get(action)

        if signature in seen_calls and spec is not None:
            # 同一个工具 + 完全一样的参数又调一次：结果不会变，别把步数烧在这儿
            repeat_streak += 1
            observation = {
                "error": "这一步刚才已经执行过（工具和参数完全相同），再执行结果也不会变。"
                         "请换个做法，或者直接用 final 给出答案。"
            }
            summary = f"跳过重复的 {action} 调用"
        elif dry_run:
            # 干跑：所有工具都只「说要做什么」，不真正执行。模型会拿到固定的规划提示，
            # 想清楚全部步骤后用 final 输出「将要做的事」清单 —— 适合先审后放。
            repeat_streak = 0
            observation = {
                "dry_run": True,
                "note": "干跑模式：本工具没有被真正调用。请继续规划后续步骤；"
                        "全部想清楚后用 final 输出「将要做的事」清单（不要声称已执行）。",
            }
            summary = f"（干跑）将执行 {action}"
            seen_calls.add(signature)
        elif read_only and action in _WRITE_TOOLS:
            repeat_streak = 0
            observation = {"error": "只读模式：本次任务不执行写操作，如需修改请关闭只读模式后重试"}
            summary = f"已拦截写操作 {action}（只读模式）"
            seen_calls.add(signature)
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

        if isinstance(observation, dict):
            _collect(observation.get("note_id"), observation.get("title"))
            _collect(observation.get("id"), observation.get("title"))
            for item in observation.get("notes") or []:
                if isinstance(item, dict):
                    _collect(item.get("id"), item.get("title"))

        step = {"tool": action, "summary": summary, "params": params}
        steps.append(step)
        # 先把这一步推给前端，再准备下一轮——这就是「流式」的核心
        yield {"type": "step", "tool": action, "summary": summary, "params": params,
              "notes": _notes_list(involved)}
        messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
        _push_observe(
            observation if isinstance(observation, dict) else {"result": observation},
            int((spec or {}).get("observe_limit") or OBSERVE_LIMIT),
        )

        if repeat_streak >= MAX_REPEAT_STEPS:
            answer = (
                f"我卡在重复操作上了（同一个调用连着做了 {repeat_streak} 次，结果不会变），先停下来。"
                "已经完成的操作都生效了。你可以把任务说得更具体一点，或者告诉我下一步做什么。"
            )
            _record_run(conn, task, ok=True, answer=answer, steps=steps, error="",
                        read_only=read_only, dry_run=dry_run, involved=involved)
            yield {"type": "final", "ok": True, "answer": answer, "steps": steps, "error": "",
                  "notes": _notes_list(involved)}
            return

    # 步数耗尽还没收尾：安全停下，绝不无限循环
    answer = "步骤太多，我先停下来了。已经完成的操作都生效了，你可以继续给我补充指令。"
    _record_run(conn, task, ok=True, answer=answer, steps=steps, error="",
                read_only=read_only, dry_run=dry_run, involved=involved)
    yield {"type": "final", "ok": True, "answer": answer, "steps": steps, "error": "",
          "notes": _notes_list(involved)}


def run_agent(
    conn: sqlite3.Connection,
    task: str,
    *,
    max_steps: int = MAX_STEPS,
    read_only: bool = False,
    dry_run: bool = False,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """跑一次 agent 循环。等价于消费 `iter_agent_events` 并拼回 {ok, answer, steps, error}。

    保留旧签名与返回值结构，原有 14 个测试无需改动即可全绿。
    """
    final: dict[str, Any] | None = None
    for event in iter_agent_events(conn, task, max_steps=max_steps, read_only=read_only, dry_run=dry_run, history=history):
        if event["type"] == "error":
            return {"ok": False, "answer": "", "steps": [], "error": event["error"]}
        if event["type"] == "final":
            final = event
            break
        # step 事件：步骤已包含在最终 final 的 steps 里，这里忽略即可
    if final is None:
        return {"ok": False, "answer": "", "steps": [], "error": "agent 没有产生结果"}
    return {
        "ok": bool(final.get("ok")),
        "answer": final.get("answer") or "",
        "steps": final.get("steps") or [],
        "error": final.get("error") or "",
    }
