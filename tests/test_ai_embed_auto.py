"""向量索引自动跟进（maybe_auto_index / auto_index_state / pending_count）的测试。

假 embedding 服务沿用 tests/test_ai_embed.py 的确定性主题向量写法，
额外记录请求次数，用来断言「没配 embed_model 时一次 HTTP 都不发」。
"""

from __future__ import annotations

import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import db as db_mod
from app import repo
from app.services import ai, ai_embed

DIM = 8

# 主题向量：和 tests/test_ai_embed.py 一致，不同主题方向尽量正交
TOPICS: list[tuple[tuple[str, ...], list[float]]] = [
    (("缓存", "cache", "redis", "ttl"), [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    (("python", "装饰器", "decorator"), [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    (("旅行", "旅游", "travel"), [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
]


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return [0.0] * DIM
    return [value / norm for value in vector]


def _hash_vector(text: str) -> list[float]:
    vector = [0.0] * DIM
    for position, char in enumerate(text):
        vector[(ord(char) + position) % DIM] += 1.0
    return _normalise(vector)


def vector_for(text: str) -> list[float]:
    lowered = (text or "").lower()
    vector = [0.0] * DIM
    hit = False
    for words, topic in TOPICS:
        if any(word.lower() in lowered for word in words):
            vector = [left + right for left, right in zip(vector, topic)]
            hit = True
    return _normalise(vector) if hit else _hash_vector(lowered)


class _EmbedHandler(BaseHTTPRequestHandler):
    mode = "ok"
    requests: int = 0
    batches: list[int] = []

    def do_POST(self):  # noqa: N802
        type(self).requests += 1
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        if self.mode == "error":
            self._send(500, b'{"error":"boom"}')
            return
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        inputs = payload.get("input") or []
        if isinstance(inputs, str):
            inputs = [inputs]
        type(self).batches.append(len(inputs))
        data = [
            {"index": index, "embedding": vector_for(str(text))}
            for index, text in enumerate(inputs)
        ]
        data.reverse()  # 服务商不保证顺序，用 index 标记
        self._send(200, json.dumps({"object": "list", "data": data}).encode("utf-8"))

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 别往测试输出里刷日志
        pass


@pytest.fixture()
def embed_server():
    server = HTTPServer(("127.0.0.1", 0), _EmbedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _EmbedHandler.mode = "ok"
    _EmbedHandler.requests = 0
    _EmbedHandler.batches = []
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "embed_auto.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


@pytest.fixture(autouse=True)
def restore_ai_config():
    """AI 配置是模块级全局状态，测完必须还原，避免污染其它测试。"""
    original = dict(ai.current())
    sources = dict(ai._sources)
    yield
    ai.configure(original, sources)


def _configure(server: str, *, model: str = "test-embed") -> None:
    ai.configure(
        {
            "base_url": server,
            "api_key": "sk-test-1234567890",
            "model": "chat-model",
            "embed_model": model,
            "timeout": 5,
        }
    )


# ---------------------------------------------------------------------------
# 1. 没配 embed_model：不报错、不发请求、不留痕迹
# ---------------------------------------------------------------------------
def test_unconfigured_returns_none_without_any_http(conn, embed_server):
    # 只配聊天模型，故意不配 embed_model
    ai.configure({"base_url": embed_server, "model": "chat-only"})
    repo.create_note(conn, title="不会索引的笔记", content="缓存 Redis TTL")

    result = ai_embed.maybe_auto_index(conn, interval_minutes=0)

    assert result is None
    assert _EmbedHandler.requests == 0
    # 「不留痕迹」：没有写自动索引时间戳
    assert ai_embed.auto_index_state(conn)["last_run_at"] == ""


# ---------------------------------------------------------------------------
# 2. 改正文后自动增量重建：只重算改动那篇，且检索命中新内容
# ---------------------------------------------------------------------------
def test_auto_index_picks_up_edited_note(conn, embed_server):
    _configure(embed_server)
    edited = repo.create_note(conn, title="缓存笔记", content="Redis 到点过期后大量请求打到数据库。")
    repo.create_note(conn, title="Python 笔记", content="用 functools.wraps 保留元信息。")

    first = ai_embed.maybe_auto_index(conn, interval_minutes=0)
    assert first is not None, first
    assert first["indexed"] == 2
    assert first["total"] == 2

    # now_iso 只有秒级精度，睡一下确保 updated_at 真的变了
    time.sleep(1.05)
    repo.update_note(
        conn,
        edited["id"],
        content="新的正文：缓存失效后要加随机 TTL 抖动，避免同时过期。",
    )

    second = ai_embed.maybe_auto_index(conn, interval_minutes=0)
    assert second is not None, second
    assert second["indexed"] == 1, second  # 只重算被改的那一篇
    assert second["total"] == 2
    assert second["pruned"] == 0

    # 不用手动点「重建索引」，检索就能命中新内容
    results = ai_embed.retrieve(conn, "缓存失效怎么处理", limit=3)
    assert results is not None
    assert results[0]["id"] == edited["id"]
    assert "随机 TTL 抖动" in results[0]["content"]


# ---------------------------------------------------------------------------
# 3. 幂等：刚跑完，interval 内再调返回 None
# ---------------------------------------------------------------------------
def test_auto_index_is_idempotent_within_interval(conn, embed_server):
    _configure(embed_server)
    repo.create_note(conn, title="幂等笔记", content="Python 装饰器")

    first = ai_embed.maybe_auto_index(conn, interval_minutes=0)
    assert first is not None
    assert first["indexed"] == 1

    assert ai_embed.maybe_auto_index(conn, interval_minutes=30) is None
    assert ai_embed.auto_index_state(conn)["last_run_at"]


# ---------------------------------------------------------------------------
# 4. embedding 服务挂掉：不抛异常、errors 非空、不写坏时间戳、可重试
# ---------------------------------------------------------------------------
def test_auto_index_survives_embedding_service_down(conn, embed_server):
    _configure(embed_server)
    note = repo.create_note(conn, title="断网笔记", content="缓存 Redis TTL")

    assert ai_embed.maybe_auto_index(conn, interval_minutes=0)["indexed"] == 1
    time.sleep(1.05)
    repo.update_note(conn, note["id"], content="改过的正文：缓存 Redis TTL 又变了")

    _EmbedHandler.mode = "error"  # 服务开始报 500
    before = ai_embed.auto_index_state(conn)["last_run_at"]
    assert before  # 上一次成功留下的时间戳

    result = ai_embed.maybe_auto_index(conn, interval_minutes=0)  # 绝不能抛
    assert result is None or result.get("errors"), result
    # 失败不覆盖时间戳
    assert ai_embed.auto_index_state(conn)["last_run_at"] == before
    assert ai_embed.pending_count(conn) == 1

    _EmbedHandler.mode = "ok"  # 服务恢复后还能重试成功
    again = ai_embed.maybe_auto_index(conn, interval_minutes=0)
    assert again is not None and again["indexed"] == 1, again
    assert ai_embed.pending_count(conn) == 0


# ---------------------------------------------------------------------------
# 5. pending_count：建索引前后 / 改笔记前后数字变化正确
# ---------------------------------------------------------------------------
def test_pending_count_tracks_edits(conn, embed_server):
    _configure(embed_server)
    note = repo.create_note(conn, title="待办笔记", content="Python 装饰器")

    assert ai_embed.pending_count(conn) == 1  # 还没建索引
    assert ai_embed.maybe_auto_index(conn, interval_minutes=0)["indexed"] == 1
    assert ai_embed.pending_count(conn) == 0

    time.sleep(1.05)
    repo.update_note(conn, note["id"], content="Python 装饰器（改过）")
    assert ai_embed.pending_count(conn) == 1
