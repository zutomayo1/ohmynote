"""AI 核心配置层（app/services/ai.py + ai_usage.py）的单元测试。

这一层是三个并行改动（设置页 / 向量检索 / 问笔记）共同的地基，
所以单独用一组测试锁住行为：地址规范化、多模型选择、脱敏、错误翻译、
配置读写与来源追踪、local_only 拦截、用量统计。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import db as db_mod
from app.services import ai, ai_usage


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "ai.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


@pytest.fixture(autouse=True)
def restore_ai_config():
    """AI 配置是模块级全局状态，测完必须还原，否则会影响别的测试文件。"""
    original = dict(ai.current())
    sources = dict(ai._sources)
    yield
    ai.configure(original, sources)


# ---------------------------------------------------------------------------
# 地址与模型
# ---------------------------------------------------------------------------
def test_api_root_strips_suffixes():
    assert ai.api_root("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1"
    assert ai.api_root("https://x.com/v1/chat/completions") == "https://x.com/v1"
    assert ai.api_root("https://x.com/v1/embeddings") == "https://x.com/v1"
    assert ai.api_root("https://x.com/v1/models") == "https://x.com/v1"
    assert ai.api_root("") == ""
    assert ai.api_root("  http://127.0.0.1:11434/v1  ") == "http://127.0.0.1:11434/v1"


def test_endpoint_join():
    assert ai.endpoint("https://x.com/v1", "chat/completions") == "https://x.com/v1/chat/completions"
    assert ai.endpoint("https://x.com/v1/", "/models") == "https://x.com/v1/models"


def test_models_for_falls_back_to_general_model():
    ai.configure({"base_url": "http://x/v1", "model": "big-model"})
    assert ai.models_for("summary") == "big-model"
    assert ai.models_for("tags") == "big-model"
    assert ai.models_for("answer") == "big-model"
    assert ai.models_for("embed") == "big-model"

    ai.configure(
        {
            "base_url": "http://x/v1",
            "model": "big-model",
            "model_summary": "cheap-model",
            "model_tags": "cheap-model",
        }
    )
    assert ai.models_for("summary") == "cheap-model"
    assert ai.models_for("tags") == "cheap-model"
    assert ai.models_for("answer") == "big-model"  # 没单独配就回退
    assert ai.credentials()["embed_model"] == "big-model"


def test_normalise_clamps_timeout_and_coerces_bools():
    conf = ai._normalise({"timeout": "9999", "local_only": "1", "redact": "off"})
    assert conf["timeout"] == 600
    assert conf["local_only"] is True
    assert conf["redact"] is False
    assert ai._normalise({"timeout": "1"})["timeout"] == 5
    assert ai._normalise({"timeout": "垃圾"})["timeout"] == ai.DEFAULTS["timeout"]


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,secret",
    [
        ("密钥是 sk-abcdefghijklmnopqrst", "sk-abcdefghijklmnopqrst"),
        ("password=hunter2", "hunter2"),
        ("api_key: abcd1234", "abcd1234"),
        ("手机号 13812345678", "13812345678"),
        ("身份证 11010119900307123X", "11010119900307123X"),
        ("卡号 6222021234567890123", "6222021234567890123"),
        ("token=ghp_abcdefghijklmnop", "abcdefghijklmnop"),
    ],
)
def test_redact_text_masks_secrets(text, secret):
    cleaned, hits = ai.redact_text(text)
    assert hits >= 1
    assert secret not in cleaned
    assert "***" in cleaned


def test_redact_text_leaves_normal_notes_alone():
    source = "今天学了 Python 的异步编程，事件循环很有意思。"
    cleaned, hits = ai.redact_text(source)
    assert cleaned == source
    assert hits == 0
    assert ai.redact_text("") == ("", 0)


# ---------------------------------------------------------------------------
# 错误翻译
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,keyword",
    [
        (401, "密钥"),
        (403, "权限"),
        (404, "地址或模型不存在"),
        (429, "频率限制"),
        (500, "服务端"),
        (502, "Base URL"),
        (599, "HTTP 599"),
    ],
)
def test_humanize_error(status, keyword):
    message = ai.humanize_error(status, "raw detail")
    assert keyword in message
    assert "raw detail" in message  # 原始信息要保留，方便排查


def test_humanize_error_without_detail():
    assert "密钥" in ai.humanize_error(401)


# ---------------------------------------------------------------------------
# 配置读写与来源
# ---------------------------------------------------------------------------
def test_save_and_bootstrap_roundtrip(conn):
    ai.save(
        conn,
        {
            "base_url": "https://api.example.com/v1/",
            "api_key": "sk-test-1234567890",
            "model": "main-model",
            "model_summary": "cheap",
            "timeout": "30",
            "local_only": "1",
            "redact": "1",
        },
    )
    assert ai.is_enabled() is True
    assert ai.current()["model"] == "main-model"
    assert ai.current()["timeout"] == 30
    assert ai.current()["local_only"] is True
    assert ai.models_for("summary") == "cheap"

    # 模拟重启：重新从数据库读
    ai.configure(ai.DEFAULTS, {})
    ai.bootstrap(conn)
    assert ai.current()["model"] == "main-model"
    assert ai.current()["base_url"] == "https://api.example.com/v1"

    described = ai.describe()
    assert described["sources"]["model"] == "db"
    assert described["has_key"] is True
    assert described["key_mask"].endswith("7890")
    assert "sk-test-1234567890" not in described["key_mask"]


def test_env_is_used_when_db_is_empty(conn, monkeypatch):
    monkeypatch.setattr(ai.settings, "ai_base_url", "https://env.example.com/v1")
    monkeypatch.setattr(ai.settings, "ai_model", "env-model")
    ai.bootstrap(conn)
    assert ai.current()["base_url"] == "https://env.example.com/v1"
    assert ai.describe()["sources"]["base_url"] == "env"
    assert ai.is_enabled() is True


def test_db_overrides_env(conn, monkeypatch):
    monkeypatch.setattr(ai.settings, "ai_base_url", "https://env.example.com/v1")
    monkeypatch.setattr(ai.settings, "ai_model", "env-model")
    ai.save(conn, {"base_url": "https://db.example.com/v1", "model": "db-model"})
    assert ai.current()["base_url"] == "https://db.example.com/v1"
    assert ai.current()["model"] == "db-model"
    # .env 依然被识别出来（页面上会提示）
    assert "INKNOTE_AI_BASE_URL" in ai.describe()["env_keys"]


def test_clear_config_disables_ai(conn):
    ai.save(conn, {"base_url": "http://127.0.0.1:1234/v1", "model": "m"})
    assert ai.is_enabled() is True
    # 清空页面配置（留空字符串）后应回到未配置
    ai.save(conn, {key: "" for key in ai.FIELDS})
    assert ai.is_enabled() is False
    assert ai.current()["base_url"] == ""


# ---------------------------------------------------------------------------
# local_only
# ---------------------------------------------------------------------------
def test_local_only_blocks_remote_address():
    ai.configure({"base_url": "https://api.openai.com/v1", "model": "m", "local_only": True})
    with pytest.raises(ai.AIError) as excinfo:
        ai.chat([{"role": "user", "content": "hi"}])
    assert "只允许本机" in str(excinfo.value)


def test_local_only_allows_localhost():
    ai.configure({"base_url": "http://127.0.0.1:1/v1", "model": "m", "local_only": True})
    # 端口 1 上没人监听，应该抛「连不上」而不是「只允许本机」
    with pytest.raises(ai.AIError) as excinfo:
        ai.chat([{"role": "user", "content": "hi"}])
    assert "只允许本机" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 假服务：/models 与 /chat/completions
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    models_status = 200
    chat_reply = "可用"
    last_body: dict = {}

    def do_GET(self):  # noqa: N802
        if self.models_status != 200:
            self.send_response(self.models_status)
            self.end_headers()
            return
        payload = {"data": [{"id": "test-model"}, {"id": "other-model"}]}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        type(self).last_body = json.loads(raw or b"{}")
        payload = {
            "choices": [{"message": {"content": self.chat_reply}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def fake_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.models_status = 200
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()


def test_list_models(fake_server):
    models = ai.list_models(fake_server, "sk-x", 10)
    assert models == ["other-model", "test-model"]


@pytest.mark.parametrize("status", [404, 405, 501])
def test_list_models_reports_unsupported(fake_server, status):
    _Handler.models_status = status
    with pytest.raises(ai.AIUnsupported):
        ai.list_models(fake_server, "", 10)
    _Handler.models_status = 200


def test_probe_steps_all_ok(fake_server):
    result = ai.probe({"base_url": fake_server, "model": "test-model", "timeout": "10"})
    assert result["ok"] is True
    names = [step["name"] for step in result["steps"]]
    assert names == ["连通性与密钥", "模型名", "对话"]
    assert all(step["status"] == "ok" for step in result["steps"])
    assert result["reply"] == "可用"


def test_probe_reports_wrong_model(fake_server):
    result = ai.probe({"base_url": fake_server, "model": "不存在的模型", "timeout": "10"})
    assert result["ok"] is False
    failed = [step for step in result["steps"] if step["status"] == "fail"]
    assert failed and failed[0]["name"] == "模型名"
    assert "test-model" in failed[0]["detail"]  # 给出可用候选


def test_probe_skips_when_models_unsupported(fake_server):
    _Handler.models_status = 404
    try:
        result = ai.probe({"base_url": fake_server, "model": "test-model", "timeout": "10"})
        assert result["ok"] is True
        assert result["steps"][0]["status"] == "skip"
    finally:
        _Handler.models_status = 200


def test_probe_without_base_url():
    result = ai.probe({"base_url": "", "model": "m"})
    assert result["ok"] is False
    assert "Base URL" in result["steps"][0]["detail"]


def test_chat_records_usage(fake_server, conn):
    ai.configure({"base_url": fake_server, "model": "test-model", "timeout": 10})
    reply = ai.chat([{"role": "user", "content": "hi"}], conn=conn)
    assert reply == "可用"

    stats = ai_usage.summary(conn)
    assert stats["month_calls"] == 1
    assert stats["month_tokens"] == 18
    assert stats["month_prompt"] == 11
    assert stats["month_completion"] == 7
    assert stats["by_task"][0]["calls"] == 1
    assert stats["last_call"]["model"] == "test-model"


def test_chat_uses_task_specific_model(fake_server):
    ai.configure(
        {
            "base_url": fake_server,
            "model": "big",
            "model_summary": "cheap",
            "timeout": 10,
        }
    )
    ai.chat([{"role": "user", "content": "hi"}], task="summary")
    assert _Handler.last_body["model"] == "cheap"
    ai.chat([{"role": "user", "content": "hi"}], task="answer")
    assert _Handler.last_body["model"] == "big"


def test_chat_redacts_when_enabled(fake_server):
    ai.configure(
        {"base_url": fake_server, "model": "test-model", "timeout": 10, "redact": True}
    )
    ai.chat([{"role": "user", "content": "我的 key 是 sk-abcdefghijklmnopqrst"}])
    sent = _Handler.last_body["messages"][0]["content"]
    assert "sk-abcdefghijklmnopqrst" not in sent
    assert "sk-***" in sent


def test_chat_without_config_raises():
    ai.configure(ai.DEFAULTS, {})
    with pytest.raises(ai.AIError) as excinfo:
        ai.chat([{"role": "user", "content": "hi"}])
    assert "尚未配置" in str(excinfo.value)


def test_usage_reset(fake_server, conn):
    ai.configure({"base_url": fake_server, "model": "test-model", "timeout": 10})
    ai.chat([{"role": "user", "content": "hi"}], conn=conn)
    assert ai_usage.reset(conn) == 1
    assert ai_usage.summary(conn)["month_calls"] == 0
