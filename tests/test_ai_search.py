"""语义搜索（app/services/ai_search.py + /search?mode=semantic）测试。

假 embedding 服务沿用 tests/test_ai_embed.py 的风格：按「主题词 → 正交向量」
累加，返回时故意打乱顺序但带 index。这样「文案不重叠、按意思命中」的排序
才可断言；查询「缓存失效」走「缓存」主题，目标笔记只写同主题的 Redis/TTL，
字面上并不重合。
"""

from __future__ import annotations

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import db as db_mod, repo
from app.services import ai, ai_embed, ai_search

DIM = 8

# 主题向量：不同主题方向尽量正交，方便断言排序
TOPICS: list[tuple[tuple[str, ...], list[float]]] = [
    (("缓存", "cache", "redis", "ttl"), [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    (("python", "装饰器", "decorator"), [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    (("旅行", "旅游", "travel"), [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
]


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm > 0 else [0.0] * DIM


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

    def do_POST(self):  # noqa: N802
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
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "ai-search.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


@pytest.fixture(autouse=True)
def restore_ai_config():
    """AI 配置是模块级全局状态，测完必须还原，避免污染其它测试文件。"""
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
# 1. 没配向量模型 / 没索引 / 调用失败 → None + 页面回退
# ---------------------------------------------------------------------------
def test_semantic_search_none_without_model(conn):
    ai.configure({})
    assert ai_search.semantic_search(conn, "缓存失效怎么处理") is None
    assert ai_search.diagnose(conn) == {"reason": "还没有配置向量模型", "needs_setup": True}


def test_search_semantic_falls_back_without_model(auth_client):
    ai.configure({})
    response = auth_client.get("/search", params={"q": "测试", "mode": "semantic"})
    assert response.status_code == 200
    assert "已回退关键词检索" in response.text
    assert "语义搜索需要先在设置页配置向量模型并重建索引" in response.text
    assert 'href="/#ai"' in response.text


def test_search_semantic_without_index_hints_setup(auth_client, embed_server):
    _configure(embed_server)
    with db_mod.db() as conn:
        ai_embed.clear(conn)
    response = auth_client.get("/search", params={"q": "缓存", "mode": "semantic"})
    assert response.status_code == 200
    assert "已回退关键词检索" in response.text
    assert "语义搜索需要先在设置页配置向量模型并重建索引" in response.text


def test_semantic_search_none_on_service_error(conn, embed_server):
    _configure(embed_server)
    repo.create_note(conn, title="Redis TTL", content="到点过期后请求全压到数据库。")
    assert ai_embed.rebuild(conn)["ok"] is True
    ai.configure({"base_url": "http://127.0.0.1:1/v1", "embed_model": "test-embed", "timeout": 5})
    assert ai_search.semantic_search(conn, "缓存失效怎么处理") is None


def test_search_semantic_falls_back_on_service_error(auth_client, embed_server):
    _configure(embed_server)
    with db_mod.db() as conn:
        repo.create_note(conn, title="Redis TTL 笔记", content="到点过期后请求全压到数据库。")
        assert ai_embed.rebuild(conn)["ok"] is True
    ai.configure({"base_url": "http://127.0.0.1:1/v1", "embed_model": "test-embed", "timeout": 5})

    response = auth_client.get("/search", params={"q": "Redis", "mode": "semantic"})
    assert response.status_code == 200
    assert "已回退关键词检索" in response.text
    # 索引其实已经建好，只是这次调用失败：不该再提示去配置/重建
    assert "语义搜索需要先在设置页配置向量模型并重建索引" not in response.text


# ---------------------------------------------------------------------------
# 2. 配好 + 建索引：按意思命中（字面不重合）
# ---------------------------------------------------------------------------
def test_semantic_search_hits_by_meaning(conn, embed_server):
    _configure(embed_server)
    target = repo.create_note(
        conn,
        title="Redis TTL 踩坑",
        content="到点过期后大量请求打到数据库，记录一下排查过程。",
    )
    repo.create_note(conn, title="Python 装饰器", content="用 functools.wraps 保留元信息。")
    assert ai_embed.rebuild(conn)["ok"] is True

    # 关键词检索字面不重合，命中不了目标
    keyword_titles = {item["title"] for item in repo.search_notes(conn, "缓存失效怎么处理", limit=10)}
    assert target["title"] not in keyword_titles

    result = ai_search.semantic_search(conn, "缓存失效怎么处理", limit=5)
    assert result is not None
    assert result["engine"] == "semantic"
    assert result["total"] == len(result["items"]) >= 1
    first = result["items"][0]
    assert first["id"] == target["id"]
    assert first["score"] > 0.9
    # 模板 note_card 要用的字段都在（同构 repo.search_notes）
    for key in ("id", "title", "content", "url", "score", "snippet", "tokens"):
        assert key in first
    assert first["snippet"]


def test_search_route_semantic_engine(auth_client, embed_server):
    _configure(embed_server)
    with db_mod.db() as conn:
        repo.create_note(
            conn,
            title="语义命中 TTL 笔记",
            content="Redis 到点过期后请求都压到数据库，得处理一下。",
        )
        assert ai_embed.rebuild(conn)["ok"] is True

    response = auth_client.get("/search", params={"q": "缓存失效怎么处理", "mode": "semantic"})
    assert response.status_code == 200
    assert "语义检索" in response.text
    assert "匹配度" in response.text
    assert "语义命中 TTL 笔记" in response.text
    assert "已回退关键词检索" not in response.text


# ---------------------------------------------------------------------------
# 3. 不传 mode：仍然是关键词路径
# ---------------------------------------------------------------------------
def test_search_route_default_is_keyword(auth_client, embed_server):
    _configure(embed_server)
    with db_mod.db() as conn:
        repo.create_note(conn, title="关键词路径笔记", content="Redis 缓存与 TTL。")
        assert ai_embed.rebuild(conn)["ok"] is True

    response = auth_client.get("/search", params={"q": "Redis"})
    assert response.status_code == 200
    assert "关键词检索" in response.text
    assert "语义检索" not in response.text


# ---------------------------------------------------------------------------
# 4. score / snippet 字段存在（模板能渲染）
# ---------------------------------------------------------------------------
def test_semantic_items_have_score_and_snippet(conn, embed_server):
    _configure(embed_server)
    repo.create_note(conn, title="缓存笔记", content="Redis 过期以后请求打到数据库。")
    assert ai_embed.rebuild(conn)["ok"] is True

    result = ai_search.semantic_search(conn, "缓存怎么失效处理", limit=3)
    assert result is not None
    item = result["items"][0]
    assert isinstance(item["score"], float)
    assert 0.0 <= item["score"] <= 1.0
    assert isinstance(item["snippet"], str) and item["snippet"]
