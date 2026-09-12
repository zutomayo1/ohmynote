"""「问笔记」升级测试：多轮追问 + SSE 流式 + 存成笔记 + 降级。

自己起一个假的 OpenAI 兼容服务（参考 test_smoke.py 的写法，但不改它），
覆盖：首轮入库 / 追问带历史 / 流式拼出完整回答 / 流式失败出错误事件 /
存成笔记 / 没配 AI 的降级 / 删除会话 / 未登录跳转。
每个用例结束都恢复 AI 配置与「问笔记」会话表，并清理用例期间新建的笔记。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


# ---------------------------------------------------------------------------
# 假 AI 服务
# ---------------------------------------------------------------------------
STREAM_PIECES = ["这是", "流式", "回答"]


class _FakeAIHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    answer_index = 0

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            payload = {}
        type(self).requests.append(payload if isinstance(payload, dict) else {})

        if payload.get("stream"):
            # 至少 3 个 delta，最后 [DONE]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in STREAM_PIECES:
                chunk = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        type(self).answer_index += 1
        reply = f"这是第{type(self).answer_index}轮的回答。"
        data = json.dumps({"choices": [{"message": {"content": reply}}]}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        data = json.dumps({"object": "list", "data": [{"id": "test-model"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 别往测试输出里刷日志
        pass


class FakeAI:
    """可手动 start / stop 的假服务（测「流式失败」时要先关掉它）。"""

    def __init__(self):
        self.server = HTTPServer(("127.0.0.1", 0), _FakeAIHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.stopped = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    @property
    def requests(self) -> list:
        return _FakeAIHandler.requests

    def start(self) -> "FakeAI":
        _FakeAIHandler.requests = []
        _FakeAIHandler.answer_index = 0
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass


@pytest.fixture()
def ai_server():
    server = FakeAI().start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 公共装置
# ---------------------------------------------------------------------------
def _set_ai(client, csrf, base_url, model="test-model") -> None:
    response = client.post(
        "/settings/ai",
        data={"_csrf": csrf, "base_url": base_url, "model": model, "timeout": "10"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _clear_ai(client, csrf) -> None:
    client.post("/settings/ai", data={"_csrf": csrf, "clear_all": "1"}, follow_redirects=False)


def _clear_chat() -> None:
    from app import db as db_mod
    from app.services import ai_chat

    with db_mod.db() as conn:
        ai_chat.ensure(conn)
        ai_chat.clear_all(conn)


@pytest.fixture()
def clean_ai(auth_client, csrf):
    """清空 AI 配置与会话表；结束后再清一次，并删掉用例期间新建的笔记。"""
    from app import db as db_mod

    _clear_ai(auth_client, csrf)
    _clear_chat()
    with db_mod.db() as conn:
        before = int(conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM notes").fetchone()["m"])
    try:
        yield
    finally:
        _clear_ai(auth_client, csrf)
        _clear_chat()
        from app import repo

        with db_mod.db() as conn:
            rows = conn.execute("SELECT id FROM notes WHERE id > ?", (before,)).fetchall()
            for row in rows:
                repo.purge(conn, int(row["id"]))


@pytest.fixture()
def ai_configured(clean_ai, auth_client, csrf, ai_server):
    _set_ai(auth_client, csrf, ai_server.base_url)
    return ai_server


@pytest.fixture()
def qa_note(auth_client, csrf):
    """写一篇含独特关键词的笔记，保证检索一定命中。"""
    response = auth_client.post(
        "/notes",
        data={
            "_csrf": csrf,
            "title": "问笔记测试笔记",
            "content": "这是一篇关于「紫色独角兽」的笔记，记录了很多细节。",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"]


def _cid_from(location: str) -> int:
    match = re.search(r"[?&]c=(\d+)", location)
    assert match, location
    return int(match.group(1))


# ---------------------------------------------------------------------------
# 1. 首轮提问 → 入库 → /ask?c= 能看到历史
# ---------------------------------------------------------------------------
def test_first_question_persists_and_renders(auth_client, csrf, ai_configured, qa_note):
    response = auth_client.post(
        "/ask", data={"_csrf": csrf, "question": "紫色独角兽"}, follow_redirects=False
    )
    assert response.status_code == 303
    cid = _cid_from(response.headers["location"])

    page = auth_client.get(f"/ask?c={cid}")
    assert page.status_code == 200
    assert "紫色独角兽" in page.text
    assert "这是第1轮的回答。" in page.text
    assert "问笔记测试笔记" in page.text  # 引用来源卡片
    assert f"/ask?c={cid}" in page.text  # 侧栏「最近提问」


# ---------------------------------------------------------------------------
# 2. 第二轮追问：上一轮问答进了 messages
# ---------------------------------------------------------------------------
def test_followup_sends_history(auth_client, csrf, ai_configured, qa_note):
    first = auth_client.post("/ask", data={"_csrf": csrf, "question": "紫色独角兽"}, follow_redirects=False)
    cid = _cid_from(first.headers["location"])

    second = auth_client.post(
        "/ask",
        data={"_csrf": csrf, "question": "紫色独角兽", "conversation_id": str(cid)},
        follow_redirects=False,
    )
    assert second.status_code == 303
    assert _cid_from(second.headers["location"]) == cid

    assert len(ai_configured.requests) >= 2, "第二轮应该真的调用了 AI"
    messages = ai_configured.requests[-1]["messages"]
    assert any(
        item.get("role") == "assistant" and "这是第1轮的回答。" in str(item.get("content") or "")
        for item in messages
    )


# ---------------------------------------------------------------------------
# 3. /ask/stream：SSE 能拼出完整回答，事件里有 sources 与 done
# ---------------------------------------------------------------------------
def test_stream_collects_deltas_and_events(auth_client, csrf, ai_configured, qa_note):
    response = auth_client.post(
        "/ask/stream",
        json={"question": "紫色独角兽"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    events = [
        json.loads(line[5:].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    assert any("sources" in event for event in events)
    assert "".join(event.get("delta", "") for event in events) == "".join(STREAM_PIECES)

    done = [event for event in events if event.get("done")]
    assert done and done[0].get("conversation_id")

    page = auth_client.get(f"/ask?c={done[0]['conversation_id']}")
    assert page.status_code == 200
    assert "".join(STREAM_PIECES) in page.text


# ---------------------------------------------------------------------------
# 4. 流式失败：返回错误事件而不是 500
# ---------------------------------------------------------------------------
def test_stream_error_event_when_ai_down(auth_client, csrf, clean_ai, ai_server):
    _set_ai(auth_client, csrf, ai_server.base_url)
    ai_server.stop()  # 把假服务关掉，模拟连不上
    response = auth_client.post(
        "/ask/stream",
        json={"question": "紫色独角兽"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert '"error"' in response.text
    assert "连不上" in response.text or "AI" in response.text


# ---------------------------------------------------------------------------
# 5. /ask/save：存成笔记后内容含回答与来源
# ---------------------------------------------------------------------------
def test_save_turn_as_note(auth_client, csrf, ai_configured, qa_note):
    response = auth_client.post(
        "/ask", data={"_csrf": csrf, "question": "紫色独角兽"}, follow_redirects=False
    )
    cid = _cid_from(response.headers["location"])
    page = auth_client.get(f"/ask?c={cid}")
    match = re.search(r'name="message_id" value="(\d+)"', page.text)
    assert match, "会话页应该有「存成笔记」表单"

    saved = auth_client.post(
        "/ask/save",
        data={"_csrf": csrf, "conversation_id": str(cid), "message_id": match.group(1)},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"].startswith("/notes/")

    note = auth_client.get(saved.headers["location"])
    assert note.status_code == 200
    assert "紫色独角兽" in note.text            # 标题 = 问题
    assert "这是第1轮的回答。" in note.text      # 正文包含回答
    assert "参考来源" in note.text              # 正文包含来源小节
    assert "问笔记测试笔记" in note.text        # [[双链]] 指向来源笔记


# ---------------------------------------------------------------------------
# 6. 没配 AI 时 /ask 仍显示检索结果（降级）
# ---------------------------------------------------------------------------
def test_ask_without_ai_still_shows_sources(auth_client, csrf, clean_ai, qa_note):
    response = auth_client.post(
        "/ask", data={"_csrf": csrf, "question": "紫色独角兽"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert "AI 服务未配置" in response.text
    assert "问笔记测试笔记" in response.text


# ---------------------------------------------------------------------------
# 7. 删除会话
# ---------------------------------------------------------------------------
def test_delete_conversation(auth_client, csrf, clean_ai, qa_note):
    response = auth_client.post(
        "/ask", data={"_csrf": csrf, "question": "紫色独角兽"}, follow_redirects=False
    )
    cid = _cid_from(response.headers["location"])

    deleted = auth_client.post(
        "/ask/delete", data={"_csrf": csrf, "conversation_id": str(cid)}, follow_redirects=False
    )
    assert deleted.status_code == 303

    page = auth_client.get(f"/ask?c={cid}", follow_redirects=False)
    assert page.status_code == 303
    assert "/ask" in page.headers["location"]


# ---------------------------------------------------------------------------
# 8. 未登录访问 /ask 跳登录页
# ---------------------------------------------------------------------------
def test_ask_requires_login(client):
    fresh = client.__class__(client.app)
    response = fresh.get("/ask", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# 附加：ai_chat 存取本身
# ---------------------------------------------------------------------------
def test_ai_chat_store_roundtrip(clean_ai):
    from app import db as db_mod
    from app.services import ai_chat

    with db_mod.db() as conn:
        long_question = "第一句问题" + "x" * 80
        cid = ai_chat.create(conn, long_question)
        ai_chat.add_message(conn, cid, role="user", content="问", engine="keyword")
        message_id = ai_chat.add_message(
            conn,
            cid,
            role="assistant",
            content="答",
            sources=[{"title": "标题", "url": "/notes/1", "snippet": "片段"}],
            engine="semantic",
        )

        convo = ai_chat.get(conn, cid)
        assert convo is not None
        assert convo["title"] == long_question[:40]
        assert len(convo["messages"]) == 2
        assert convo["messages"][1]["sources"][0]["title"] == "标题"
        assert convo["messages"][1]["engine"] == "semantic"

        assert ai_chat.history(conn, cid) == [
            {"role": "user", "content": "问"},
            {"role": "assistant", "content": "答"},
        ]
        assert ai_chat.recent(conn, limit=5)[0]["id"] == cid
        assert ai_chat.get_turn(conn, message_id)["question"] == "问"

        ai_chat.rename(conn, cid, "改过的标题")
        assert ai_chat.get(conn, cid)["title"] == "改过的标题"
        assert ai_chat.delete(conn, cid) is True
        assert ai_chat.get(conn, cid) is None
