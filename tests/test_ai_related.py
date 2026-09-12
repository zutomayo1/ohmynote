"""语义相关笔记（app/services/ai_related.py + 详情页回退）的测试。

假 embedding 服务用确定性主题向量：两组主题方向正交，且同一主题用两套
互不重叠的关键词（「缓存/失效」vs「Redis/TTL」），这样才能断言
「用词完全不同但语义相近」的笔记被排到第一位。
"""

from __future__ import annotations

import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import db as db_mod
from app import repo
from app.services import ai, ai_embed, ai_related

DIM = 8

CACHE_TOPIC = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
TRAVEL_TOPIC = [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]

# 同一主题两套词：两组词在字面上完全没有交集
_TOPICS = (
    (("缓存", "失效", "过期"), CACHE_TOPIC),
    (("redis", "ttl", "cache"), CACHE_TOPIC),
    (("旅行", "旅游", "travel"), TRAVEL_TOPIC),
)


def _normalise(vector):
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return [0.0] * DIM
    return [value / norm for value in vector]


def vector_for(text):
    lowered = (text or "").lower()
    vector = [0.0] * DIM
    hit = False
    for words, topic in _TOPICS:
        if any(word in lowered for word in words):
            vector = [left + right for left, right in zip(vector, topic)]
            hit = True
    if not hit:
        # 没有主题词的笔记用确定性哈希，保证与两个主题都不相似
        for position, char in enumerate(lowered):
            vector[(ord(char) + position) % DIM] += 1.0
    return _normalise(vector)


class _FakeEmbedHandler(BaseHTTPRequestHandler):
    mode = "ok"
    requests: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        inputs = payload.get("input") or []
        if isinstance(inputs, str):
            inputs = [inputs]
        type(self).requests.append([str(item) for item in inputs])
        if self.mode == "error":
            self._send(500, b'{"error":"boom"}')
            return
        data = [
            {"index": index, "embedding": vector_for(str(item))}
            for index, item in enumerate(inputs)
        ]
        data.reverse()  # 故意乱序，逼客户端按 index 还原
        self._send(200, json.dumps({"object": "list", "data": data}).encode("utf-8"))

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 别往测试输出刷日志
        pass


@pytest.fixture()
def embed_server():
    server = HTTPServer(("127.0.0.1", 0), _FakeEmbedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _FakeEmbedHandler.mode = "ok"
    _FakeEmbedHandler.requests = []
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "related.db"
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


def _make_notes(conn):
    """目标 + 语义相近（用词不同）+ 无关，共 3 篇。"""
    target = repo.create_note(
        conn, title="缓存失效复盘", content="缓存失效之后数据库压力很大，记录一下排查过程。"
    )
    similar = repo.create_note(
        conn, title="Redis TTL 实战", content="Redis TTL 到点过期，请求全打到 MySQL。"
    )
    other = repo.create_note(conn, title="周末旅行清单", content="记得带充电器和雨伞。")
    return target, similar, other


def _create_note_via_http(client, csrf: str, *, title: str, content: str, tags: str = "") -> int:
    response = client.post(
        "/notes",
        data={"_csrf": csrf, "title": title, "content": content, "tags": tags, "action": "view"},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303), response.text
    match = re.search(r"/notes/(\d+)", response.headers.get("location", ""))
    assert match, response.headers.get("location")
    return int(match.group(1))


def _related_section(html: str) -> str:
    start = html.find("你可能还想看")
    if start < 0:
        return ""
    end = html.find("反向链接", start)
    return html[start : end if end > start else start + 3000]


# ---------------------------------------------------------------------------
# 1. 没配向量模型：返回 None，详情页回退关键词且不 500
# ---------------------------------------------------------------------------
def test_unconfigured_returns_none(conn):
    target, similar, other = _make_notes(conn)
    ai.configure({})

    assert ai_embed.embedding_for_note(conn, target["id"]) is None
    assert ai_embed.similar_notes(conn, target["id"]) is None
    assert ai_related.related_notes(conn, target) is None


def test_detail_page_falls_back_to_keyword_when_unconfigured(auth_client, csrf):
    ai.configure({})
    tag = "相关回退标签"
    first = _create_note_via_http(
        auth_client, csrf, title="关键词回退甲", content="甲的内容", tags=tag
    )
    _create_note_via_http(auth_client, csrf, title="关键词回退乙", content="乙的内容", tags=tag)

    page = auth_client.get(f"/notes/{first}")
    assert page.status_code == 200
    section = _related_section(page.text)
    assert "关键词回退乙" in section


# ---------------------------------------------------------------------------
# 2. 配好 + 建索引：用词完全不同但语义相近的排第一，且不含自己
# ---------------------------------------------------------------------------
def test_semantic_ranking_excludes_self(conn, embed_server):
    _configure(embed_server)
    target, similar, other = _make_notes(conn)
    assert ai_embed.rebuild(conn)["ok"] is True

    items = ai_embed.similar_notes(conn, target["id"], limit=5)
    assert items is not None
    assert items[0]["id"] == similar["id"]
    assert items[0]["score"] > 0.99
    assert items[0]["snippet"]
    assert all(item["id"] != target["id"] for item in items)

    # 关键词逻辑命中不到「用词完全不同」的 similar（甚至不是第一），语义能
    keyword_ids = [item["id"] for item in repo.related_notes(conn, target, limit=5)]
    assert keyword_ids[:1] != [similar["id"]]

    related = ai_related.related_notes(conn, target, limit=5)
    assert related is not None
    assert related[0]["engine"] == "semantic"
    assert related[0]["reason"] == "语义相关"
    assert related[0]["id"] == similar["id"]


# ---------------------------------------------------------------------------
# 3. 回收站里的笔记不能出现在推荐里
# ---------------------------------------------------------------------------
def test_trashed_note_is_excluded(conn, embed_server):
    _configure(embed_server)
    target = repo.create_note(conn, title="目标", content="缓存失效排查")
    keep = repo.create_note(conn, title="Redis TTL 保留", content="Redis TTL 到点过期")
    gone = repo.create_note(conn, title="缓存过期待删", content="缓存 过期 处理")
    assert ai_embed.rebuild(conn)["ok"] is True

    assert repo.soft_delete(conn, gone["id"]) is True
    items = ai_embed.similar_notes(conn, target["id"], limit=5)
    ids = {item["id"] for item in items or []}
    assert gone["id"] not in ids
    assert keep["id"] in ids


# ---------------------------------------------------------------------------
# 4. embedding 服务挂了：返回 None / 空，详情页仍 200，绝不 500
# ---------------------------------------------------------------------------
def test_service_down_returns_none(conn):
    target, *_ = _make_notes(conn)
    ai.configure({"base_url": "http://127.0.0.1:1/v1", "embed_model": "test-embed", "timeout": 1})

    # 没有索引 / 服务不可达：读取路径直接降级，不抛异常
    assert ai_embed.similar_notes(conn, target["id"]) is None
    assert ai_related.related_notes(conn, target) is None


def test_related_notes_swallows_embedding_failure(conn, monkeypatch):
    target, *_ = _make_notes(conn)
    _configure("http://127.0.0.1:1/v1")

    def boom(*args, **kwargs):
        raise RuntimeError("embedding service down")

    monkeypatch.setattr(ai_embed, "similar_notes", boom)
    assert ai_related.related_notes(conn, target) is None


def test_detail_page_still_200_when_semantic_unavailable(auth_client, csrf):
    ai.configure({"base_url": "http://127.0.0.1:1/v1", "embed_model": "test-embed", "timeout": 1})
    note_id = _create_note_via_http(
        auth_client, csrf, title="语义不可用详情页", content="缓存失效排查"
    )
    page = auth_client.get(f"/notes/{note_id}")
    assert page.status_code == 200
    assert "你可能还想看" in page.text


# ---------------------------------------------------------------------------
# 5. 辅助函数本身：平均多块向量、没有向量返回 None
# ---------------------------------------------------------------------------
def test_embedding_for_note_averages_chunks(conn):
    note = repo.create_note(conn, title="多块笔记", content="正文")
    ai_embed.ensure(conn)
    conn.execute(
        "INSERT OR REPLACE INTO note_embeddings "
        "(note_id, chunk_index, chunk, vector, model, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (note["id"], 0, "块一", ai_embed._pack_vector([1.0, 0.0]), "test-embed", ""),
    )
    conn.execute(
        "INSERT OR REPLACE INTO note_embeddings "
        "(note_id, chunk_index, chunk, vector, model, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (note["id"], 1, "块二", ai_embed._pack_vector([0.0, 1.0]), "test-embed", ""),
    )
    assert ai_embed.embedding_for_note(conn, note["id"]) == [0.5, 0.5]
    assert ai_embed.embedding_for_note(conn, 999999) is None
