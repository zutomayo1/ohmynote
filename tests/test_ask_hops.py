"""「问笔记」多跳检索（反思循环）测试。

两组策略，分别照抄仓库里已有的两种假服务模式：
- 反思循环本身（ai_chat.reflect_and_expand）：monkeypatch ai.chat + 检索间谍，
  精确断言检索次数 / 来源合并 / 容错 / 开关（参考 test_agent.py 的 ScriptedChat）。
- 端到端：假 OpenAI HTTP 服务 + 检索间谍，确认 /ask/stream 仍流式出回答、来源并集正确
  （参考 test_ai_ask.py 的 FakeAI）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services import ai as ai_service
from app.services import ai_chat


# ---------------------------------------------------------------------------
# 服务层：脚本化 ai.chat + 检索间谍
# ---------------------------------------------------------------------------
class ScriptedChat:
    """按顺序吐出预设回复，并记录每次收到的 messages（模拟 ai.chat 签名）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list = []

    def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        return self.replies.pop(0)


class RetrieveSpy:
    """统计检索调用次数，并按词返回预设来源（模拟 retrieve_sources 签名）。"""

    def __init__(self, per_term: dict):
        self.per_term = per_term
        self.terms: list[str] = []
        self.calls = 0

    def __call__(self, conn, term, limit=6):
        self.calls += 1
        self.terms.append(term)
        return list(self.per_term.get(term, [])), "semantic"


@pytest.fixture()
def db_conn(client, monkeypatch):
    """可用的数据库连接：依赖 client 确保 schema 已初始化（参考 test_agent.py）。"""
    monkeypatch.setattr(ai_service, "is_enabled", lambda: True)
    from app import db

    with db.db() as conn:
        yield conn


def _seed():
    return (
        [{"id": 1, "title": "首轮笔记", "content": "首轮正文"}],
        [{"id": 1, "title": "首轮笔记"}],
    )


# 1. enough=true 的假模型：行为与旧版一致（不再额外检索）
def test_enough_true_no_extra_retrieval(db_conn, monkeypatch):
    script = ScriptedChat(['{"enough": true}'])
    spy = RetrieveSpy({"q": _seed()[0]})
    monkeypatch.setattr(ai_service, "chat", script)

    ctx0, src0 = _seed()
    ctx, src, engine = ai_chat.reflect_and_expand(
        db_conn, "q", ctx0, src0, "semantic",
        extra_hops=2, retrieve=spy, conn_usage=db_conn,
    )
    assert spy.calls == 0, "enough=true 不应再检索"
    assert ctx == ctx0 and src == src0
    assert len(script.calls) == 1  # 只做了一次判断


# 2. 第一轮回 false：检索执行第二次、来源合并、最终上下文含两轮
def test_one_hop_retrieves_again_and_merges(db_conn, monkeypatch):
    script = ScriptedChat(['{"enough": false, "search": "更精确的词"}', '{"enough": true}'])
    spy = RetrieveSpy({
        "q": [{"id": 1, "title": "首轮笔记", "content": "首轮正文"}],
        "更精确的词": [{"id": 2, "title": "细节笔记", "content": "细节正文"}],
    })
    monkeypatch.setattr(ai_service, "chat", script)

    ctx0, src0 = _seed()
    ctx, src, engine = ai_chat.reflect_and_expand(
        db_conn, "q", ctx0, src0, "semantic",
        extra_hops=2, retrieve=spy, conn_usage=db_conn,
    )
    assert spy.calls == 1, "应只追加一跳"
    assert spy.terms == ["更精确的词"]
    ids = [ai_chat._src_id(s) for s in src]
    assert 1 in ids and 2 in ids
    assert len(src) == 2
    assert len(ctx) == 2


# 3. 3 连跳上限：连回两次 false 后 true，检索恰好 2 次追加（共 3 次）
def test_three_hop_limit(db_conn, monkeypatch):
    script = ScriptedChat([
        '{"enough": false, "search": "t2"}',
        '{"enough": false, "search": "t3"}',
        '{"enough": true}',
    ])
    spy = RetrieveSpy({
        "q": [{"id": 1, "title": "A", "content": "x"}],
        "t2": [{"id": 2, "title": "B", "content": "y"}],
        "t3": [{"id": 3, "title": "C", "content": "z"}],
    })
    monkeypatch.setattr(ai_service, "chat", script)

    ctx0 = [{"id": 1, "title": "A", "content": "x"}]
    src0 = [{"id": 1, "title": "A"}]
    ctx, src, engine = ai_chat.reflect_and_expand(
        db_conn, "q", ctx0, src0, "semantic",
        extra_hops=2, retrieve=spy, conn_usage=db_conn,
    )
    assert spy.calls == 2, "最多 2 跳追加"
    assert len(src) == 3
    assert {ai_chat._src_id(s) for s in src} == {1, 2, 3}


# 4. 判断调用返回垃圾文本：不影响（当 enough）
def test_garbage_verdict_treated_as_enough(db_conn, monkeypatch):
    script = ScriptedChat(["这根本不是 JSON，随便说点啥都行。"])
    spy = RetrieveSpy({"q": _seed()[0]})
    monkeypatch.setattr(ai_service, "chat", script)

    ctx0, src0 = _seed()
    ctx, src, engine = ai_chat.reflect_and_expand(
        db_conn, "q", ctx0, src0, "semantic",
        extra_hops=2, retrieve=spy, conn_usage=db_conn,
    )
    assert spy.calls == 0
    assert len(script.calls) == 1


# 5. extra_hops=0 时检索只发生一次（这里指零次追加，首轮由调用方完成）
def test_extra_hops_zero_disables(db_conn, monkeypatch):
    script = ScriptedChat(['{"enough": false, "search": "x"}'])
    spy = RetrieveSpy({"q": _seed()[0]})
    monkeypatch.setattr(ai_service, "chat", script)

    ctx0, src0 = _seed()
    ctx, src, engine = ai_chat.reflect_and_expand(
        db_conn, "q", ctx0, src0, "semantic",
        extra_hops=0, retrieve=spy, conn_usage=db_conn,
    )
    assert spy.calls == 0
    assert len(script.calls) == 0, "extra_hops=0 不应做任何判断调用"
    assert ctx == ctx0


# 6. 模型给的检索词与首轮重复：直接视为够了（不空转）
def test_repeated_search_term_stops(db_conn, monkeypatch):
    script = ScriptedChat(['{"enough": false, "search": "q"}'])  # 与首轮问题相同
    spy = RetrieveSpy({"q": _seed()[0]})
    monkeypatch.setattr(ai_service, "chat", script)

    ctx0, src0 = _seed()
    ctx, src, engine = ai_chat.reflect_and_expand(
        db_conn, "q", ctx0, src0, "semantic",
        extra_hops=2, retrieve=spy, conn_usage=db_conn,
    )
    assert spy.calls == 0


# 7. 判断调用直接抛异常：当 enough，不挡回答
def test_judge_call_failure_treated_as_enough(db_conn, monkeypatch):
    def boom(messages, **kwargs):
        raise RuntimeError("连不上判断服务")

    spy = RetrieveSpy({"q": _seed()[0]})
    monkeypatch.setattr(ai_service, "chat", boom)

    ctx0, src0 = _seed()
    ctx, src, engine = ai_chat.reflect_and_expand(
        db_conn, "q", ctx0, src0, "semantic",
        extra_hops=2, retrieve=spy, conn_usage=db_conn,
    )
    assert spy.calls == 0
    assert ctx == ctx0


# 8. 重复资料不计入合并（全是已存在的 id → 不再多查）
def test_duplicate_hits_stop_loop(db_conn, monkeypatch):
    script = ScriptedChat([
        '{"enough": false, "search": "t2"}',
        '{"enough": false, "search": "t3"}',
    ])
    spy = RetrieveSpy({
        "q": [{"id": 1, "title": "A", "content": "x"}],
        # 第二跳返回的全是首轮已有的 id
        "t2": [{"id": 1, "title": "A", "content": "x"}],
    })
    monkeypatch.setattr(ai_service, "chat", script)

    ctx0 = [{"id": 1, "title": "A", "content": "x"}]
    src0 = [{"id": 1, "title": "A"}]
    ctx, src, engine = ai_chat.reflect_and_expand(
        db_conn, "q", ctx0, src0, "semantic",
        extra_hops=2, retrieve=spy, conn_usage=db_conn,
    )
    assert spy.calls == 1  # 第二跳没新资料，循环立即结束
    assert len(src) == 1


# ---------------------------------------------------------------------------
# 端到端：假 OpenAI HTTP 服务 + 检索间谍
# ---------------------------------------------------------------------------
STREAM_PIECES = ["这是", "多跳", "回答"]


class _HopHandler(BaseHTTPRequestHandler):
    judge_script: list[str] = []
    requests: list[dict] = []

    def do_POST(self):
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

        reply = type(self).judge_script.pop(0) if type(self).judge_script else '{"enough": true}'
        data = json.dumps(
            {"choices": [{"message": {"content": reply}}]}, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        data = json.dumps({"object": "list", "data": [{"id": "test-model"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class HopServer:
    def __init__(self):
        self.server = HTTPServer(("127.0.0.1", 0), _HopHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.stopped = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    @property
    def requests(self) -> list:
        return _HopHandler.requests

    def start(self) -> "HopServer":
        _HopHandler.requests = []
        _HopHandler.judge_script = []
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
def hop_server():
    server = HopServer().start()
    try:
        yield server
    finally:
        server.stop()


def _set_ai(client, csrf, base_url, model="test-model") -> None:
    r = client.post(
        "/settings/ai",
        data={"_csrf": csrf, "base_url": base_url, "model": model, "timeout": "10"},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text


def _clear_ai(client, csrf) -> None:
    client.post("/settings/ai", data={"_csrf": csrf, "clear_all": "1"}, follow_redirects=False)


def _clear_chat() -> None:
    from app import db as db_mod

    with db_mod.db() as conn:
        ai_chat.ensure(conn)
        ai_chat.clear_all(conn)


@pytest.fixture()
def clean_ai(auth_client, csrf):
    _clear_ai(auth_client, csrf)
    _clear_chat()
    try:
        yield
    finally:
        _clear_ai(auth_client, csrf)
        _clear_chat()


# 9. 端到端：一跳后来源合并、流式回答正常
def test_stream_multi_hop_merges_sources(auth_client, csrf, clean_ai, hop_server, monkeypatch):
    _set_ai(auth_client, csrf, hop_server.base_url)
    _HopHandler.judge_script = ['{"enough": false, "search": "细节"}', '{"enough": true}']

    counter = {"n": 0}
    notes_by_query = {
        "紫色独角兽": [{
            "id": 10, "note_id": 10, "title": "首轮笔记",
            "url": "/notes/10", "snippet": "s", "content": "首轮正文",
        }],
        "细节": [{
            "id": 20, "note_id": 20, "title": "细节笔记",
            "url": "/notes/20", "snippet": "d", "content": "细节正文",
        }],
    }

    def fake_retrieve(conn, question, limit=6):
        counter["n"] += 1
        return list(notes_by_query.get(question, [])), "semantic"

    monkeypatch.setattr("app.routers.ask.retrieve_sources", fake_retrieve)

    resp = auth_client.post(
        "/ask/stream", json={"question": "紫色独角兽"}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 200

    events = [
        json.loads(line[5:].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]
    sources_evt = [e for e in events if "sources" in e][0]
    titles = [s.get("title") for s in sources_evt["sources"]]
    assert "首轮笔记" in titles and "细节笔记" in titles
    assert "".join(e.get("delta", "") for e in events) == "".join(STREAM_PIECES)
    assert counter["n"] == 2  # 首轮 + 一跳


# 10. 端到端：extra_hops=0 时检索只发生一次
def test_stream_extra_hops_zero_single_retrieval(auth_client, csrf, clean_ai, hop_server, monkeypatch):
    _set_ai(auth_client, csrf, hop_server.base_url)
    _HopHandler.judge_script = ['{"enough": false, "search": "细节"}']

    counter = {"n": 0}

    def fake_retrieve(conn, question, limit=6):
        counter["n"] += 1
        return [{
            "id": 10, "note_id": 10, "title": "首轮笔记",
            "url": "/notes/10", "snippet": "s", "content": "x",
        }], "semantic"

    monkeypatch.setattr("app.routers.ask.retrieve_sources", fake_retrieve)

    resp = auth_client.post(
        "/ask/stream",
        json={"question": "紫色独角兽", "extra_hops": 0},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert counter["n"] == 1  # 不进入反思循环
