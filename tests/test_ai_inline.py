"""A · 编辑器内联 AI 的路由 / 提示词测试。

覆盖：未配置降级、/api/ai/continue 的提示词约束、/api/ai/rewrite 五个 mode、
空正文 400、缺 CSRF 403、未登录 401、translate 要求英文。

假 AI 服务照 tests/test_ai_core.py 的 fake_server 写法自己起（http.server，端口 0），
不联网。main.py 里注册 ai_inline.router 由主 agent 负责；为了让本文件在接线前后
都能跑，这里发现路由还没挂上时临时挂一次（已挂则不重复）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.main import app
from app.routers import ai_inline
from app.services import ai

if not any(getattr(route, "path", "") == "/api/ai/continue" for route in app.routes):
    app.include_router(ai_inline.router)

CONTINUE_URL = "/api/ai/continue"
REWRITE_URL = "/api/ai/rewrite"

# mode -> 提示词里必须出现的中文要求（translate 另加断言看「英文」）
MODES = {
    "polish": "润色",
    "concise": "精简",
    "expand": "扩写",
    "translate": "译文",
    "formal": "正式",
}


class FakeAI:
    """OpenAI 兼容的假服务：/chat/completions 回固定文本并记下请求体。"""

    def __init__(self) -> None:
        self.models_status = 200
        self.reply = "这是 AI 返回的文本。"
        self.last_body: dict = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if outer.models_status != 200:
                    self.send_response(outer.models_status)
                    self.end_headers()
                    return
                body = json.dumps({"object": "list", "data": [{"id": "test-model"}]}).encode("utf-8")
                self._send(200, body)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                try:
                    outer.last_body = json.loads(raw or b"{}")
                except ValueError:
                    outer.last_body = {}
                body = json.dumps(
                    {
                        "choices": [{"message": {"content": outer.reply}}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
                    }
                ).encode("utf-8")
                self._send(200, body)

            def _send(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # 别往测试输出里刷日志
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def stop(self) -> None:
        self.server.shutdown()


@pytest.fixture()
def fake_ai():
    server = FakeAI()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def restore_ai_config(auth_client, csrf):
    """AI 配置是模块级全局状态：每个用例跑完都清掉页面配置并还原。"""
    original = dict(ai.current())
    sources = dict(ai._sources)
    yield
    auth_client.post("/settings/ai", data={"_csrf": csrf, "clear_all": "1"}, follow_redirects=False)
    ai.configure(original, sources)


def configure_ai(client, csrf: str, fake_ai: FakeAI) -> None:
    response = client.post(
        "/settings/ai",
        data={"_csrf": csrf, "base_url": fake_ai.base_url, "model": "test-model", "timeout": "10"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert ai.is_enabled() is True


def clear_ai(client, csrf: str) -> None:
    client.post("/settings/ai", data={"_csrf": csrf, "clear_all": "1"}, follow_redirects=False)


def post_json(client, url: str, payload: dict, csrf: str | None = None):
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    return client.post(url, json=payload, headers=headers)


def sent_text(fake_ai: FakeAI) -> str:
    return "\n".join(str(item.get("content", "")) for item in fake_ai.last_body.get("messages", []))


# ---------------------------------------------------------------------------
# 降级：未配置 AI 也不能 500
# ---------------------------------------------------------------------------
def test_unconfigured_routes_degrade_without_500(auth_client, csrf):
    clear_ai(auth_client, csrf)
    assert ai.is_enabled() is False

    continued = post_json(auth_client, CONTINUE_URL, {"title": "标题", "content": "正文"}, csrf)
    assert continued.status_code == 503, continued.text
    assert continued.status_code != 500
    body = continued.json()
    assert body["ok"] is False
    assert body["error"]

    rewritten = post_json(auth_client, REWRITE_URL, {"text": "正文", "mode": "polish"}, csrf)
    assert rewritten.status_code == 503, rewritten.text
    assert rewritten.status_code != 500
    assert rewritten.json()["ok"] is False


# ---------------------------------------------------------------------------
# /api/ai/continue
# ---------------------------------------------------------------------------
def test_continue_returns_text_and_prompt_contains_context(auth_client, csrf, fake_ai):
    configure_ai(auth_client, csrf, fake_ai)
    fake_ai.reply = "这是续写的后半段。"

    response = post_json(
        auth_client,
        CONTINUE_URL,
        {"title": "我的标题", "content": "已经写好的正文。", "instruction": "用轻松一点的语气"},
        csrf,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["text"] == "这是续写的后半段。"

    prompt = sent_text(fake_ai)
    assert "我的标题" in prompt
    assert "已经写好的正文。" in prompt
    assert "只输出续写" in prompt  # 输出约束
    assert "用轻松一点的语气" in prompt  # 额外要求


# ---------------------------------------------------------------------------
# /api/ai/rewrite：五个 mode 都通，且带上对应要求
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode,keyword", list(MODES.items()))
def test_rewrite_modes_carry_requirement(auth_client, csrf, fake_ai, mode, keyword):
    configure_ai(auth_client, csrf, fake_ai)
    fake_ai.reply = "改写后的文本。"

    response = post_json(auth_client, REWRITE_URL, {"text": "原始文本。", "mode": mode}, csrf)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["text"] == "改写后的文本。"

    prompt = sent_text(fake_ai)
    assert "原始文本。" in prompt
    assert keyword in prompt, f"{mode} 的提示词里应包含「{keyword}」"


def test_translate_mode_asks_for_english(auth_client, csrf, fake_ai):
    configure_ai(auth_client, csrf, fake_ai)
    post_json(auth_client, REWRITE_URL, {"text": "你好，世界。", "mode": "translate"}, csrf)

    prompt = sent_text(fake_ai)
    assert "英文" in prompt
    assert "译文" in prompt


def test_rewrite_rejects_unknown_mode(auth_client, csrf, fake_ai):
    configure_ai(auth_client, csrf, fake_ai)
    response = post_json(auth_client, REWRITE_URL, {"text": "正文", "mode": "shrink"}, csrf)
    assert response.status_code == 400
    assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------
def test_empty_payloads_return_400(auth_client, csrf, fake_ai):
    configure_ai(auth_client, csrf, fake_ai)

    empty_continue = post_json(auth_client, CONTINUE_URL, {"title": "t", "content": "   "}, csrf)
    assert empty_continue.status_code == 400, empty_continue.text
    assert empty_continue.json()["ok"] is False

    empty_rewrite = post_json(auth_client, REWRITE_URL, {"text": "", "mode": "polish"}, csrf)
    assert empty_rewrite.status_code == 400, empty_rewrite.text
    assert empty_rewrite.json()["ok"] is False


def test_missing_csrf_is_403(auth_client, csrf, fake_ai):
    configure_ai(auth_client, csrf, fake_ai)
    response = auth_client.post(CONTINUE_URL, json={"title": "t", "content": "正文"})
    assert response.status_code == 403, response.text
    assert response.json()["ok"] is False


def test_anonymous_is_401(client, csrf):
    anonymous = client.__class__(client.app)
    continued = anonymous.post(CONTINUE_URL, json={"title": "t", "content": "正文"})
    assert continued.status_code == 401, continued.text
    assert continued.json()["ok"] is False

    rewritten = anonymous.post(REWRITE_URL, json={"text": "正文", "mode": "polish"})
    assert rewritten.status_code == 401, rewritten.text
    assert rewritten.json()["ok"] is False
