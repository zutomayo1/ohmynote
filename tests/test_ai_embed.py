"""向量检索（app/services/ai_embed.py + /api/ai/embed 路由）的测试。

假 embedding 服务用确定性向量：
- 预设几组「语义主题」，文本里出现任一词就累加对应主题向量；
- 表里没有的主题按字符哈希生成 8 维向量；
- 返回时故意打乱 data 顺序，但带 index，逼客户端按 index 还原。
这样「问缓存失效 → Redis TTL 那篇排第一」这种语义排序才可断言。
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


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return [0.0] * DIM
    return [value / norm for value in vector]


# 主题向量：不同主题方向尽量正交，方便断言排序
TOPICS: list[tuple[tuple[str, ...], list[float]]] = [
    (("缓存", "cache", "redis", "ttl"), [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    (("python", "装饰器", "decorator"), [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    (("旅行", "旅游", "travel"), [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
]


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
    reverse = True
    batches: list[int] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        if self.mode == "garbage":
            self._send(200, b"this is not json")
            return
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
        if self.reverse:
            data.reverse()  # 服务商可能不保证顺序，用 index 标记
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
    _EmbedHandler.reverse = True
    _EmbedHandler.batches = []
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "embed.db"
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


def _seed_vector_row(conn, note_id: int) -> None:
    """塞一行假向量，确保 retrieve() 真的会走到「调用外部服务」那一步。"""
    ai_embed.ensure(conn)
    conn.execute(
        "INSERT OR REPLACE INTO note_embeddings "
        "(note_id, chunk_index, chunk, vector, model, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (note_id, 0, "占位", ai_embed._pack_vector(ai_embed._normalise([1.0, 0.0, 0.0])), "test-embed", ""),
    )


# ---------------------------------------------------------------------------
# 1. 未配置 / 不可用
# ---------------------------------------------------------------------------
def test_unconfigured_embed_model_returns_none(conn):
    ai.configure({})
    assert ai_embed.available(conn) is False
    assert ai_embed.retrieve(conn, "缓存失效怎么处理") is None

    # 只配了聊天模型、没配 embed_model：也不能当向量模型用
    ai.configure({"base_url": "http://127.0.0.1:1/v1", "model": "chat-only"})
    assert ai_embed.available(conn) is False
    assert ai_embed.retrieve(conn, "缓存失效怎么处理") is None


# ---------------------------------------------------------------------------
# 2. 索引与状态
# ---------------------------------------------------------------------------
def test_rebuild_indexes_notes_and_status_is_correct(conn, embed_server):
    _configure(embed_server)
    note_a = repo.create_note(conn, title="Redis TTL 踩坑", content="到点失效后大量请求打到数据库。")
    note_b = repo.create_note(conn, title="Python 装饰器", content="用 functools.wraps 保留元信息。")

    assert ai_embed.available(conn) is False
    result = ai_embed.rebuild(conn)
    assert result["ok"] is True, result
    assert result["total"] == 2
    assert result["indexed"] == 2
    assert result["built"] == 2
    assert ai_embed.available(conn) is True

    status = ai_embed.status(conn)
    assert status["configured"] is True
    assert status["model"] == "test-embed"
    assert status["total"] == 2
    assert status["indexed"] == 2
    assert status["running"] is False
    assert status["progress"] is None
    assert status["error"] == ""
    assert status["last_built_at"]

    stored = conn.execute("SELECT COUNT(*) AS c FROM note_embeddings").fetchone()["c"]
    assert stored >= 2
    ids = {row["note_id"] for row in conn.execute("SELECT note_id FROM note_embeddings")}
    assert ids == {note_a["id"], note_b["id"]}

    # 第二次重建：没有变化，全部跳过
    again = ai_embed.rebuild(conn)
    assert again["ok"] is True
    assert again["built"] == 0
    assert again["skipped"] == 2


def test_embed_texts_follows_index_order(conn, embed_server):
    _configure(embed_server)
    vectors = ai_embed._embed_texts(["缓存失效怎么处理", "Python 装饰器"])
    assert len(vectors) == 2
    cache_score = _dot(vectors[0], [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    python_score = _dot(vectors[1], [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    assert cache_score > 0.99
    assert python_score > 0.99


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def test_embed_texts_batches_by_sixteen(conn, embed_server):
    _configure(embed_server)
    vectors = ai_embed._embed_texts([f"第 {index} 条文本" for index in range(20)])
    assert len(vectors) == 20
    assert _EmbedHandler.batches == [16, 4]


def test_force_rebuild_recalculates_unchanged(conn, embed_server):
    _configure(embed_server)
    repo.create_note(conn, title="强制重建", content="Python 装饰器")
    assert ai_embed.rebuild(conn)["built"] == 1
    assert ai_embed.rebuild(conn)["built"] == 0

    forced = ai_embed.rebuild(conn, force=True)
    assert forced["ok"] is True
    assert forced["built"] == 1
    assert forced["skipped"] == 0


# ---------------------------------------------------------------------------
# 3. 语义排序：字面不重合也能排第一
# ---------------------------------------------------------------------------
def test_semantic_ranking_beats_literal_keywords(conn, embed_server):
    _configure(embed_server)
    target = repo.create_note(
        conn,
        title="Redis TTL 踩坑",
        content="Redis 到点过期后大量请求打到数据库，记录一下排查过程。",
    )
    repo.create_note(conn, title="Python 装饰器", content="用 functools.wraps 保留元信息。")
    repo.create_note(conn, title="周末旅行清单", content="记得带充电器和雨伞。")
    assert ai_embed.rebuild(conn)["ok"] is True

    question = "缓存失效怎么处理"
    keyword_hits = repo.retrieve_notes(conn, question, limit=3)
    assert target["id"] not in {item["id"] for item in keyword_hits}

    results = ai_embed.retrieve(conn, question, limit=3)
    assert results is not None
    first = results[0]
    assert first["id"] == target["id"]
    assert first["score"] > 0.99
    for key in ("id", "title", "content", "url", "snippet", "score", "is_public", "updated_at"):
        assert key in first
    assert first["snippet"]


# ---------------------------------------------------------------------------
# 4. 外部失败：返回 None，不抛异常
# ---------------------------------------------------------------------------
def test_retrieve_returns_none_when_service_down(conn):
    note = repo.create_note(conn, title="断网笔记", content="缓存 Redis TTL")
    _seed_vector_row(conn, note["id"])
    ai.configure({"base_url": "http://127.0.0.1:1/v1", "embed_model": "test-embed", "timeout": 5})
    assert ai_embed.retrieve(conn, "缓存失效") is None


def test_retrieve_returns_none_on_garbage_response(conn, embed_server):
    note = repo.create_note(conn, title="垃圾响应", content="缓存 Redis TTL")
    _seed_vector_row(conn, note["id"])
    ai.configure({"base_url": embed_server, "embed_model": "test-embed", "timeout": 5})
    _EmbedHandler.mode = "garbage"
    assert ai_embed.retrieve(conn, "缓存失效") is None


def test_rebuild_records_error_instead_of_raising(conn, embed_server):
    repo.create_note(conn, title="失败重建", content="缓存 Redis TTL")
    ai.configure({"base_url": embed_server, "embed_model": "test-embed", "timeout": 5})
    _EmbedHandler.mode = "error"
    result = ai_embed.rebuild(conn)
    assert result["ok"] is False
    assert result["error"]
    status = ai_embed.status(conn)
    assert status["error"]


# ---------------------------------------------------------------------------
# 5. 路由：后台重建 + 轮询 status
# ---------------------------------------------------------------------------
def test_background_rebuild_via_api(auth_client, csrf, embed_server):
    _configure(embed_server)
    for index in range(2):
        created = auth_client.post(
            "/notes",
            data={
                "_csrf": csrf,
                "title": f"后台向量笔记 {index}",
                "content": f"第 {index} 篇：Python 装饰器用法。",
                "action": "save",
            },
            follow_redirects=False,
        )
        assert created.status_code in (200, 303), created.text

    started = auth_client.post("/api/ai/embed/rebuild", headers={"X-CSRF-Token": csrf})
    assert started.status_code == 200, started.text
    payload = started.json()
    assert payload["ok"] is True
    assert payload["started"] is True
    assert payload["total"] >= 2

    deadline = time.time() + 15
    status = {}
    while time.time() < deadline:
        status = auth_client.get("/api/ai/embed/status").json()
        if not status["running"]:
            break
        time.sleep(0.1)

    assert status.get("running") is False, status
    assert status.get("error") == ""
    assert status["indexed"] == status["total"] >= 2
    assert status["last_built_at"]


def test_rebuild_returns_409_while_running(auth_client, csrf):
    from app.routers import ai_embed as embed_router

    with embed_router._rebuild_lock:
        embed_router._rebuild_state["running"] = True
    try:
        response = auth_client.post("/api/ai/embed/rebuild", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 409
        assert response.json()["error"] == "正在重建中"
    finally:
        with embed_router._rebuild_lock:
            embed_router._rebuild_state["running"] = False


def test_clear_api_removes_vectors(auth_client, csrf, embed_server):
    _configure(embed_server)
    response = auth_client.post(
        "/api/ai/embed/rebuild", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    deadline = time.time() + 15
    while time.time() < deadline:
        if not auth_client.get("/api/ai/embed/status").json()["running"]:
            break
        time.sleep(0.1)

    cleared = auth_client.post("/api/ai/embed/clear", headers={"X-CSRF-Token": csrf})
    assert cleared.status_code == 200
    body = cleared.json()
    assert body["ok"] is True
    assert body["removed"] >= 1
    assert auth_client.get("/api/ai/embed/status").json()["indexed"] == 0


# ---------------------------------------------------------------------------
# 6. 删除笔记后清掉残留向量
# ---------------------------------------------------------------------------
def test_deleted_note_is_purged_on_rebuild(conn, embed_server):
    _configure(embed_server)
    keep = repo.create_note(conn, title="保留的笔记", content="Python 装饰器用法。")
    gone = repo.create_note(conn, title="要删除的笔记", content="缓存 Redis TTL 踩坑。")
    first = ai_embed.rebuild(conn)
    assert first["ok"] is True
    assert first["indexed"] == 2

    assert repo.soft_delete(conn, gone["id"]) is True
    second = ai_embed.rebuild(conn)
    assert second["ok"] is True
    assert second["removed"] >= 1
    leftover = conn.execute(
        "SELECT COUNT(*) AS c FROM note_embeddings WHERE note_id = ?", (gone["id"],)
    ).fetchone()["c"]
    assert leftover == 0
    status = ai_embed.status(conn)
    assert status["total"] == 1
    assert status["indexed"] == 1
    kept_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM note_embeddings WHERE note_id = ?", (keep["id"],)
    ).fetchone()["c"]
    assert kept_rows >= 1


# ---------------------------------------------------------------------------
# 分块规则
# ---------------------------------------------------------------------------
def test_chunk_note_respects_size_overlap_and_cap():
    chunks = ai_embed.chunk_note("超长笔记", "Redis TTL 缓存失效 " * 3000)
    assert 1 <= len(chunks) <= ai_embed.MAX_CHUNKS
    assert all(len(chunk) <= ai_embed.CHUNK_MAX for chunk in chunks)
    if len(chunks) == ai_embed.MAX_CHUNKS:
        # 超长笔记被截断
        assert ai_embed.MAX_CHUNKS == 20
    if len(chunks) > 1:
        # 相邻块有约 100 字重叠
        assert chunks[1][:20] in chunks[0]
