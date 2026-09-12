"""「问笔记」：多轮追问 + 流式输出 + 一键存成笔记。

检索优先走向量（services.ai_embed），不可用/失败时回退关键词全文检索（repo.retrieve_notes），
回答上会标明这轮用的是哪一种。

SSE 事件格式（app/static/js/ask.js 依赖它，改动要同步前端）：

    data: {"sources": [...], "engine": "semantic|keyword", "scope": ""}
    data: {"delta": "..."}                                   × N
    data: {"done": true, "conversation_id": N, "message_id": M, "scope": ""}
    data: {"error": "人话"}                                  （失败时不返回 500）

限定范围（POST 带 note_id / URL 带 note=/ask?note=N）时不再跑全库检索，
sources 只有这一篇，并新增 scope="note" 字段（engine 为空，前端会跳过引擎徽标）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from .. import db as db_mod
from .. import repo
from ..deps import csrf_protect, db_conn, require_login
from ..markdown_render import render as render_markdown
from ..services import ai, ai_chat
from ..templating import render
from ..utils import url_with_query

router = APIRouter(dependencies=[Depends(require_login), Depends(csrf_protect)])

logger = logging.getLogger("inknote.ask")

MAX_QUESTION = 2000
SAMPLE_QUESTIONS = [
    "我最近在学什么？",
    "关于 Python 我记了哪些笔记？",
    "有哪些待办还没做完？",
]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def retrieve_sources(
    conn: sqlite3.Connection, question: str, *, limit: int = 6
) -> tuple[list[dict], str]:
    """返回 (引用来源, 引擎名)。语义检索优先，任何问题都回退关键词。"""
    try:
        from ..services import ai_embed

        hits = ai_embed.retrieve(conn, question, limit=limit)
        if hits:
            return hits, "semantic"
    except Exception:  # 向量检索不可用/未配置时回退关键词，属正常降级，别让页面挂
        logger.debug("问笔记：向量检索不可用，回退关键词检索（question=%r）", question, exc_info=True)
    return repo.retrieve_notes(conn, question, limit=limit), "keyword"


def _decorate(message: dict) -> dict:
    """给助手消息补上渲染后的 HTML（库里只存 Markdown 原文）。"""
    item = dict(message)
    if (item.get("role") or "") == "assistant" and (item.get("content") or "").strip():
        item["html"] = render_markdown(item["content"], title=None).html
    return item


def _to_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        # 表单/JSON 里的 id 缺省就是 ""，非数字按默认值处理，属正常路径
        logger.debug("问笔记：id 解析失败，按默认值 %s 处理（value=%r）", default, value)
        return default


def _note_source(note: dict | None) -> dict | None:
    """把一篇笔记整理成「限定范围」的展示来源（不含正文，避免会话表里重复存大段内容）。"""
    if not note:
        return None
    note_id = _to_int(note.get("id"))
    body = note.get("content") or ""
    snippet = (note.get("summary") or "").strip() or " ".join(body.split())[:160]
    return {
        "id": note_id,
        "note_id": note_id,
        "title": (note.get("title") or "").strip() or "无标题",
        "url": note.get("url") or f"/notes/{note_id}",
        "snippet": snippet[:200],
        "scope": "note",
    }


def _scope_context(conn: sqlite3.Connection, note_id) -> tuple[dict | None, dict | None]:
    """按 note_id 取限定范围用的 (模型上下文, 展示来源)。

    笔记不存在 / 在回收站时返回 (None, None)，调用方按普通模式处理。
    模型上下文里的 content 就是这一篇的正文（标题由 build_qa_messages 写进资料块）。
    """
    nid = _to_int(note_id)
    if not nid:
        return None, None
    note = repo.get_note(conn, nid)
    if note is None:
        return None, None
    source = _note_source(note)
    context = dict(source or {})
    body = (note.get("content") or "").strip()
    context["content"] = body or "（这篇笔记还没有正文，只有标题。）"
    return context, source


def _scope_from_conversation(
    conn: sqlite3.Connection, conversation: dict | None
) -> tuple[dict | None, dict | None]:
    """从会话里恢复 scope：只看最新一轮 AI 回答的 sources 标记。"""
    if not conversation:
        return None, None
    for message in reversed(conversation.get("messages") or []):
        if (message.get("role") or "") != "assistant":
            continue
        for item in message.get("sources") or []:
            if isinstance(item, dict) and item.get("scope") == "note":
                return _scope_context(conn, item.get("note_id") or item.get("id"))
        return None, None  # 最新一轮不是限定范围 → 普通模式
    return None, None


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        # 前端发来空 body / 非法 JSON：按空 payload 处理不 500，但要留痕
        logger.warning("问笔记：请求体 JSON 解析失败（path=%s）", request.url.path, exc_info=True)
        return {}
    return payload if isinstance(payload, dict) else {}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
@router.get("/ask")
def ask_page(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    c: int = 0,
    note: str = "",
):
    conversation = None
    messages: list[dict] = []
    scope_ctx = None
    scope_note = None
    if _to_int(note):
        scope_ctx, scope_note = _scope_context(conn, note)
        if scope_note is None:
            # 笔记不存在或进了回收站：回普通模式并提示
            return RedirectResponse(
                url_with_query("/ask", msg="这篇笔记不存在或已在回收站，已切回问全部笔记", kind="warn"),
                status_code=303,
            )
    if c:
        conversation = ai_chat.get(conn, c)
        if conversation is None:
            return RedirectResponse(
                url_with_query("/ask", msg="这次对话不存在（可能已经被删除了）", kind="warn"),
                status_code=303,
            )
        messages = [_decorate(item) for item in conversation["messages"]]
        if scope_note is None:
            scope_ctx, scope_note = _scope_from_conversation(conn, conversation)

    # 没配 AI 时，把这一篇的正文当检索结果先展示出来（降级不 500）
    scope_html = ""
    if scope_note and not ai.is_enabled():
        body = (scope_ctx or {}).get("content") or ""
        if body.strip():
            scope_html = render_markdown(body, title=scope_note.get("title")).html

    return render(
        request,
        "ask.html",
        ai_ready=ai.is_enabled(),
        conversation=conversation,
        messages=messages,
        recent=ai_chat.recent(conn, limit=20),
        sample_questions=[] if (conversation or messages) else SAMPLE_QUESTIONS,
        scope_note=scope_note,
        scope_html=scope_html,
    )


@router.post("/ask")
def ask_submit(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    question: str = Form(""),
    conversation_id: str = Form(""),
    note_id: str = Form(""),
):
    """没有 JS 时的完整问答（有 JS 时走 /ask/stream）。"""
    del request
    question = (question or "").strip()[:MAX_QUESTION]
    if not question:
        return RedirectResponse(url_with_query("/ask", msg="先输入一个问题", kind="warn"), status_code=303)

    cid = _to_int(conversation_id)
    if cid and ai_chat.get(conn, cid) is None:
        cid = 0

    # 有 note_id 就只问这一篇；前端没带 note_id 时从会话的 sources 标记恢复 scope
    scope_ctx, scope_source = _scope_context(conn, note_id) if _to_int(note_id) else (None, None)
    if scope_ctx is None and cid:
        scope_ctx, scope_source = _scope_from_conversation(conn, ai_chat.get(conn, cid))

    if not cid:
        cid = ai_chat.create(conn, question)
    if scope_ctx:
        # 会话表结构不能改：scope 同时记进标题（人看）和 sources 标记（程序恢复）
        ai_chat.rename(conn, cid, f"关于《{scope_ctx['title']}》的提问")

    history = ai_chat.history(conn, cid)  # 先取历史，再写入本轮问题
    ai_chat.add_message(conn, cid, role="user", content=question)

    if scope_ctx:
        contexts = [scope_ctx]
        sources = [scope_source]
        engine = ""
    else:
        sources, engine = retrieve_sources(conn, question)
        contexts = sources

    answer_text = ""
    if ai.is_enabled():
        try:
            answer_text = ai.answer(question, contexts, history=history, conn=conn)
        except ai.AIError as exc:
            answer_text = f"> AI 生成失败：{exc}\n\n下面只列出检索到的笔记，可以点开核对。"

    ai_chat.add_message(
        conn, cid, role="assistant", content=answer_text, sources=sources, engine=engine
    )
    return RedirectResponse(f"/ask?c={cid}", status_code=303)


@router.post("/ask/delete")
def ask_delete(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    conversation_id: str = Form(""),
):
    del request
    cid = _to_int(conversation_id)
    if cid:
        ai_chat.delete(conn, cid)
    return RedirectResponse(url_with_query("/ask", msg="已删除这次对话"), status_code=303)


# ---------------------------------------------------------------------------
# 流式回答
# ---------------------------------------------------------------------------
def _stream_provider(messages: list[dict]):
    """逐段产出模型输出。不走 ai.chat()，因为要边收边转发。"""
    conf = ai.credentials()
    base = conf.get("base_url") or ""
    model = ai.models_for("answer")
    if not (base and model):
        raise ai.AIError("尚未配置 AI 服务：先去「设置」页填 Base URL 和模型名。")
    ai.assert_base_url_allowed(base)

    outgoing = messages
    if ai.current().get("redact"):
        outgoing = [
            {**item, "content": ai.redact_text(str(item.get("content") or ""))[0]}
            for item in messages
        ]

    payload = {
        "model": model,
        "messages": outgoing,
        "temperature": 0.2,
        "max_tokens": 1200,
        "stream": True,
    }
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if conf.get("api_key"):
        headers["Authorization"] = f"Bearer {conf['api_key']}"

    request = urllib.request.Request(
        ai.endpoint(base, "chat/completions"),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=conf.get("timeout") or 45) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except ValueError:
                    # 部分服务商会插入非 JSON 的 keep-alive 行，跳过即可（debug 避免刷屏）
                    logger.debug("问笔记：忽略非 JSON 流式分片（chunk=%r）", chunk[:120], exc_info=True)
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, list):  # 兼容多段内容
                    piece = "".join(
                        part.get("text", "") for part in piece if isinstance(part, dict)
                    )
                if piece:
                    yield str(piece)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # pragma: no cover
            logger.debug("问笔记：读取 AI 错误响应正文失败", exc_info=True)
        raise ai.AIError(ai.humanize_error(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise ai.AIError(f"连不上这个地址：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ai.AIError("等模型响应超时了，稍后再试或换个更快的模型") from exc


@router.post("/ask/stream")
async def ask_stream(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    note_id: str = "",
):
    payload = await _json_body(request)
    question = str(payload.get("question") or "").strip()[:MAX_QUESTION]
    if not question:
        return JSONResponse({"ok": False, "error": "先输入一个问题"}, status_code=400)

    cid = _to_int(payload.get("conversation_id"))
    if cid and ai_chat.get(conn, cid) is None:
        cid = 0

    # ask.js 不解析隐藏域，流式路径的 note_id 走 data-stream-url 的查询参数；
    # 同时兼容直接把 note_id 放进 JSON body 的调用（比如测试）。
    raw_note = payload.get("note_id", note_id)
    scope_ctx, scope_source = _scope_context(conn, raw_note) if _to_int(raw_note) else (None, None)
    if scope_ctx is None and cid:
        scope_ctx, scope_source = _scope_from_conversation(conn, ai_chat.get(conn, cid))

    if not cid:
        cid = ai_chat.create(conn, question)
    if scope_ctx:
        ai_chat.rename(conn, cid, f"关于《{scope_ctx['title']}》的提问")

    history = ai_chat.history(conn, cid)
    ai_chat.add_message(conn, cid, role="user", content=question)
    if scope_ctx:
        contexts = [scope_ctx]
        sources = [scope_source]
        engine = ""
    else:
        sources, engine = retrieve_sources(conn, question)
        contexts = sources
    messages = ai.build_qa_messages(question, contexts, history)

    # 关键：先把请求级连接里的写入提交掉。否则它会在整个流式响应期间占着写锁，
    # 生成器里那条独立连接落库时会报 "database is locked"（真的踩过这个坑）。
    try:
        conn.commit()
    except sqlite3.Error:
        # 提交失败不挡这次回答，但很可能就是写锁问题，必须留痕
        logger.warning("问笔记：流式回答前提交写入失败（conversation=%s）", cid, exc_info=True)

    def generator():
        yield _sse({"sources": sources, "engine": engine, "scope": "note" if scope_ctx else ""})
        pieces: list[str] = []
        error = ""
        try:
            for piece in _stream_provider(messages):
                pieces.append(piece)
                yield _sse({"delta": piece})
        except ai.AIError as exc:
            error = str(exc)
        except Exception as exc:  # 兜底：任何意外都不要 500
            logger.warning("问笔记：流式输出异常（conversation=%s）", cid, exc_info=True)
            error = f"流式输出失败：{exc}"

        text = "".join(pieces)
        if not text and error:
            # 一个字都没吐出来：先把错误告诉前端（前端会回退到普通问答）
            yield _sse({"error": error})
            return

        body = text or f"> AI 生成失败：{error}\n\n下面只列出检索到的笔记，可以点开核对。"
        message_id = 0
        try:
            # 请求级连接在流式响应结束后可能已经关了，这里自己开一个短连接落库
            with db_mod.db() as fresh:
                message_id = ai_chat.add_message(
                    fresh,
                    cid,
                    role="assistant",
                    content=body,
                    sources=sources,
                    engine=engine,
                )
        except Exception:  # 落库失败不影响这次回答已经显示出来，但必须留痕
            logger.warning("问笔记：流式回答保存失败（conversation=%s）", cid, exc_info=True)
            message_id = 0

        done = {
            "done": True,
            "conversation_id": cid,
            "message_id": message_id,
            "engine": engine,
            "scope": "note" if scope_ctx else "",
        }
        if error:
            done["error"] = error
        yield _sse(done)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# 把某轮回答存成笔记
# ---------------------------------------------------------------------------
@router.post("/ask/save")
def ask_save(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    conversation_id: str = Form(""),
    message_id: str = Form(""),
):
    del request
    mid = _to_int(message_id)
    turn = ai_chat.get_turn(conn, mid) if mid else None
    if not turn:
        return RedirectResponse(url_with_query("/ask", msg="找不到这条回答", kind="warn"), status_code=303)

    question = (turn.get("question") or "问笔记").strip()
    answer = (turn.get("answer") or "").strip()
    sources = turn.get("sources") or []

    lines = [answer or "（这条回答是空的）"]
    titles = [str(item.get("title") or "").strip() for item in sources if item.get("title")]
    if titles:
        lines += ["", "## 参考来源", ""]
        lines += [f"- [[{title}]]" for title in titles]
    content = "\n".join(lines).strip()

    note = repo.create_note(
        conn,
        title=question[:120] or "问笔记",
        content=content,
        tags="问笔记",
        status="saved",
    )
    return RedirectResponse(
        url_with_query(f"/notes/{note['id']}", msg="已存成笔记"), status_code=303
    )
