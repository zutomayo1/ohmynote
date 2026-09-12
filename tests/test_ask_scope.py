"""D 组 ·「问这篇」：把检索范围限定到一篇笔记。

自己起一个假的 OpenAI 兼容服务（照 tests/test_ai_ask.py 的写法，不改那个文件），
覆盖：GET scope 页面 / POST 只带这一篇 / 不带 note_id 仍全库检索 /
流式 sources 只有这一篇 / 详情页入口 / 未登录跳转 / 没配 AI 的降级。
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
STREAM_PIECES = ["这篇", "限定", "回答"]


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

    def log_message(self, *args):
        pass


class FakeAI:
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


def _make_note(client, csrf, title: str, content: str) -> int:
    response = client.post(
        "/notes",
        data={"_csrf": csrf, "title": title, "content": content, "action": "save"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    match = re.search(r"/notes/(\d+)", response.headers["location"])
    assert match, response.headers["location"]
    return int(match.group(1))


@pytest.fixture()
def two_notes(clean_ai, auth_client, csrf):
    """两篇都命中「紫色独角兽」，用来区分「问这篇」和「问全部」。"""
    first = _make_note(auth_client, csrf, "范围甲：紫色独角兽", "甲篇正文：关于紫色独角兽的独特细节。")
    second = _make_note(auth_client, csrf, "范围乙：紫色独角兽", "乙篇正文：另一只紫色独角兽的记录。")
    return first, second


def _cid_from(location: str) -> int:
    match = re.search(r"[?&]c=(\d+)", location)
    assert match, location
    return int(match.group(1))


def _prompt_of(server) -> str:
    return "".join(
        str(message.get("content") or "")
        for message in server.requests[-1]["messages"]
    )


# ---------------------------------------------------------------------------
# 1. GET /ask?note=N
# ---------------------------------------------------------------------------
def test_get_ask_scope_page(auth_client, two_notes):
    first, _second = two_notes

    page = auth_client.get(f"/ask?note={first}")
    assert page.status_code == 200
    assert "范围甲：紫色独角兽" in page.text
    assert "切回问全部笔记" in page.text
    assert f'name="note_id" value="{first}"' in page.text
    assert f"/ask/stream?note_id={first}" in page.text

    missing = auth_client.get("/ask?note=999999", follow_redirects=False)
    assert missing.status_code != 500
    assert missing.status_code in (200, 303)


def test_scoped_ask_falls_back_when_note_in_trash(auth_client, csrf, two_notes):
    first, _second = two_notes
    moved = auth_client.post(
        f"/notes/{first}/delete",
        data={"_csrf": csrf, "next": "/notes"},
        follow_redirects=False,
    )
    assert moved.status_code == 303

    response = auth_client.get(f"/ask?note={first}", follow_redirects=True)
    assert response.status_code == 200
    assert "已在回收站" in response.text  # flash 提示
    assert 'class="ask-scope"' not in response.text  # 已回普通模式，没有 scope 信息条


# ---------------------------------------------------------------------------
# 2. POST /ask 带 note_id：只喂这一篇
# ---------------------------------------------------------------------------
def test_scoped_post_sends_only_this_note(ai_configured, two_notes, auth_client, csrf):
    first, _second = two_notes

    response = auth_client.post(
        "/ask",
        data={"_csrf": csrf, "question": "紫色独角兽讲了什么？", "note_id": str(first)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(ai_configured.requests) == 1, "限定范围只应调用一次模型"

    prompt = _prompt_of(ai_configured)
    assert "范围甲：紫色独角兽" in prompt
    assert "甲篇正文" in prompt
    assert "范围乙" not in prompt
    assert "乙篇正文" not in prompt


# ---------------------------------------------------------------------------
# 3. 没有 note_id：行为与改动前一致，仍走全库检索
# ---------------------------------------------------------------------------
def test_unscoped_post_still_retrieves_all(ai_configured, two_notes, auth_client, csrf):
    response = auth_client.post(
        "/ask",
        data={"_csrf": csrf, "question": "紫色独角兽"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    prompt = _prompt_of(ai_configured)
    assert "范围甲" in prompt
    assert "范围乙" in prompt


# ---------------------------------------------------------------------------
# 4. /ask/stream 带 note_id：SSE 兼容 + sources 只有这一篇
# ---------------------------------------------------------------------------
def test_scoped_stream_events(ai_configured, two_notes, auth_client, csrf):
    first, _second = two_notes

    response = auth_client.post(
        "/ask/stream",
        json={"question": "紫色独角兽", "note_id": first},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    events = [
        json.loads(line[5:].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    source_events = [event for event in events if "sources" in event]
    assert source_events, "第一个 SSE 事件应带 sources"
    sources = source_events[0]["sources"]
    assert len(sources) == 1
    assert sources[0]["title"] == "范围甲：紫色独角兽"
    assert source_events[0].get("scope") == "note"

    assert "".join(event.get("delta", "") for event in events) == "".join(STREAM_PIECES)

    done = [event for event in events if event.get("done")]
    assert done and done[0].get("conversation_id")

    # 刷新后仍能从会话里恢复 scope（标题 + sources 标记）
    conversation = auth_client.get(f"/ask?c={done[0]['conversation_id']}")
    assert conversation.status_code == 200
    assert "正在问这篇" in conversation.text
    assert "范围甲：紫色独角兽" in conversation.text


def test_scoped_stream_note_id_from_query_string(ai_configured, two_notes, auth_client, csrf):
    """ask.js 不解析隐藏域：流式路径的 note_id 实际来自 data-stream-url 的查询参数。"""
    first, _second = two_notes

    response = auth_client.post(
        f"/ask/stream?note_id={first}",
        json={"question": "紫色独角兽"},  # 故意不带 note_id，模拟 ask.js 的真实请求
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    events = [
        json.loads(line[5:].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    source_events = [event for event in events if "sources" in event]
    assert source_events
    assert [item["title"] for item in source_events[0]["sources"]] == ["范围甲：紫色独角兽"]
    assert source_events[0].get("scope") == "note"


# ---------------------------------------------------------------------------
# 5. 详情页入口
# ---------------------------------------------------------------------------
def test_detail_page_has_ask_button(auth_client, two_notes):
    first, _second = two_notes

    page = auth_client.get(f"/notes/{first}")
    assert page.status_code == 200
    assert "问这篇" in page.text
    assert f"/ask?note={first}" in page.text


# ---------------------------------------------------------------------------
# 6. 未登录跳转
# ---------------------------------------------------------------------------
def test_ask_scope_requires_login(client):
    fresh = client.__class__(client.app)
    response = fresh.get("/ask?note=1", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# 7. 没配 AI 时：/ask?note=N 仍显示这一篇正文（当检索结果用）
# ---------------------------------------------------------------------------
def test_scoped_without_ai_shows_note_content(clean_ai, auth_client, csrf, two_notes):
    first, _second = two_notes

    page = auth_client.get(f"/ask?note={first}")
    assert page.status_code == 200
    assert "范围甲：紫色独角兽" in page.text
    assert "甲篇正文" in page.text

    # 普通提交也能在 /ask?c= 上恢复 scope 并展示正文
    response = auth_client.post(
        "/ask",
        data={"_csrf": csrf, "question": "紫色独角兽", "note_id": str(first)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert _cid_from(str(response.url)) >= 0
    assert "甲篇正文" in response.text
