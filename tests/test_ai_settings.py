"""设置页 2.0（A · 设置页）的测试。

覆盖：预设渲染、4 个新字段保存、local_only 拦截外部地址、redact 生效、
/api/ai/probe 分步结果、/api/ai/models 成功与失败、用量统计展示与重置。

假 AI 服务在本文件里自己起，不依赖 tests/test_smoke.py（那个文件不许改）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import db as db_mod
from app.services import ai, ai_usage


# ---------------------------------------------------------------------------
# 假 AI 服务：/models 返回两个模型；/chat/completions 回固定文本并记下请求体
# ---------------------------------------------------------------------------
class FakeAI:
    def __init__(self) -> None:
        self.models_status = 200
        self.reply = "可用"
        self.last_body: dict = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if outer.models_status != 200:
                    self.send_response(outer.models_status)
                    self.end_headers()
                    return
                body = json.dumps(
                    {"object": "list", "data": [{"id": "test-model"}, {"id": "other-model"}]}
                ).encode("utf-8")
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
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
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
    """AI 配置是模块级全局状态：每个用例跑完都清掉页面配置并还原，别污染别的测试文件。"""
    original = dict(ai.current())
    sources = dict(ai._sources)
    yield
    auth_client.post("/settings/ai", data={"_csrf": csrf, "clear_all": "1"}, follow_redirects=False)
    ai.configure(original, sources)


def save_ai(client, csrf: str, **fields):
    values = {"_csrf": csrf}
    values.update(fields)
    return client.post("/settings/ai", data=values, follow_redirects=False)


# ---------------------------------------------------------------------------
# 预设 / 页面渲染
# ---------------------------------------------------------------------------
def test_settings_page_renders_presets_and_sections(auth_client):
    page = auth_client.get("/settings")
    assert page.status_code == 200

    # 服务商预设：下拉列表（2026-09-18 起由卡片改为 select），option 用 data-* 带地址/模型
    for name in ("硅基流动", "智谱 GLM", "本地 Ollama", "DeepSeek", "OpenAI", "通义千问",
                 "月之暗面 Kimi", "火山方舟（豆包）", "腾讯混元", "百度文心", "Google Gemini",
                 "OpenRouter", "Groq", "Mistral", "零一万物 Yi", "本地 LM Studio", "本地 vLLM"):
        assert name in page.text, f"{name} 不在服务商下拉里"
    assert 'id="ai-provider"' in page.text
    assert page.text.count("<option") >= 17       # 17 家预设 + 「自定义」
    assert 'data-base-url="https://api.deepseek.com/v1"' in page.text
    assert 'data-model="deepseek-chat"' in page.text
    assert 'data-embed-model="BAAI/bge-m3"' in page.text  # 硅基流动免费向量
    assert 'data-key-url="https://platform.deepseek.com/api_keys"' in page.text
    assert 'id="ai-provider-hint"' in page.text
    assert "ai-preset-card" not in page.text      # 卡片已整体移除
    assert "ai-presets-more" not in page.text

    # 模型选择：自绘下拉面板（不再用 <datalist>，那控件点一下不弹、只做前缀匹配）
    assert 'data-model-combo' in page.text
    assert page.text.count('data-model-combo') == 5  # 通用 + 摘要 + 打标签 + 问答 + 向量
    assert page.text.count("combo__panel") == 5
    assert "combo__toggle" in page.text
    assert "datalist" not in page.text.lower()
    assert 'id="ai-fetch-models"' in page.text

    # 分任务模型 + 实际生效的模型
    assert 'name="model_summary"' in page.text
    assert 'name="model_tags"' in page.text
    assert 'name="model_answer"' in page.text
    # 2026-09-18：三行重复的「实际生效：xxx」压成一行（都在跟随通用模型时是同一句话）
    assert "留空则用通用模型" in page.text

    # 两个开关 + 向量模型
    assert 'name="local_only"' in page.text
    assert 'name="redact"' in page.text
    assert 'name="embed_model"' in page.text
    assert "只允许本机模型" in page.text
    assert "发送前脱敏" in page.text

    # 用量统计 / 向量索引 / 脚本
    assert "用量统计" in page.text
    assert "重置用量统计" in page.text
    assert "向量索引" in page.text
    assert "/static/js/settings.js" in page.text


def test_provider_select_preselects_current(auth_client, csrf, fake_ai):
    """当前地址命中某个服务商时，下拉里那一项要 preselected。"""
    # 用一个预设地址（不联网保存即可，local_only 不开不会请求网络）
    save_ai(auth_client, csrf, base_url="https://api.deepseek.com/v1", model="deepseek-chat", timeout="20")
    page = auth_client.get("/settings")
    marker = page.text.find('data-base-url="https://api.deepseek.com/v1"')
    assert marker != -1
    option_start = page.text.rfind("<option", 0, marker)
    option_end = page.text.find("</option>", marker)
    assert option_start != -1 and option_end != -1
    assert "selected" in page.text[option_start:option_end], "当前使用的服务商没有 preselected"


# ---------------------------------------------------------------------------
# 保存新字段
# ---------------------------------------------------------------------------
def test_save_four_new_fields(auth_client, csrf, fake_ai):
    saved = save_ai(
        auth_client,
        csrf,
        base_url=fake_ai.base_url,
        model="test-model",
        model_summary="cheap-sum",
        model_tags="cheap-tags",
        model_answer="big-answer",
        embed_model="text-embedding-3-small",
        timeout="20",
        local_only="1",
    )
    assert saved.status_code == 303, saved.text

    assert ai.current()["model_summary"] == "cheap-sum"
    assert ai.current()["model_tags"] == "cheap-tags"
    assert ai.current()["model_answer"] == "big-answer"
    assert ai.current()["embed_model"] == "text-embedding-3-small"
    assert ai.current()["local_only"] is True
    # 分任务模型确实生效（留空回退通用模型也行）
    assert ai.models_for("summary") == "cheap-sum"
    assert ai.models_for("tags") == "cheap-tags"
    assert ai.models_for("answer") == "big-answer"
    assert ai.credentials()["embed_model"] == "text-embedding-3-small"

    page = auth_client.get("/settings")
    assert "cheap-sum" in page.text
    assert "cheap-tags" in page.text
    assert "big-answer" in page.text
    assert "text-embedding-3-small" in page.text


def test_task_model_falls_back_to_general_model(auth_client, csrf, fake_ai):
    save_ai(auth_client, csrf, base_url=fake_ai.base_url, model="only-model", timeout="20")
    described = ai.describe()
    assert described["tasks"] == {"summary": "only-model", "tags": "only-model", "answer": "only-model"}
    page = auth_client.get("/settings")
    # 分任务模型区的说明里点名了通用模型（三个任务留空都跟随它）
    marker = page.text.find("留空则用通用模型")
    assert marker != -1, "分任务模型区没有「留空则用通用模型」的说明"
    assert "only-model" in page.text[marker:marker + 160], "说明里没给出实际生效的模型名"


def test_blank_api_key_keeps_saved_key(auth_client, csrf, fake_ai):
    save_ai(
        auth_client, csrf,
        base_url=fake_ai.base_url, model="test-model",
        api_key="sk-keep-1234567890abcd", timeout="20",
    )
    assert ai.current()["api_key"] == "sk-keep-1234567890abcd"

    save_ai(auth_client, csrf, base_url=fake_ai.base_url, model="test-model", api_key="", timeout="20")
    assert ai.current()["api_key"] == "sk-keep-1234567890abcd"  # 留空 = 不修改

    page = auth_client.get("/settings")
    assert "sk-keep-1234567890abcd" not in page.text  # 永远不回显完整密钥


# ---------------------------------------------------------------------------
# local_only
# ---------------------------------------------------------------------------
def test_local_only_blocks_external_address(auth_client, csrf, fake_ai):
    save_ai(auth_client, csrf, base_url=fake_ai.base_url, model="test-model", timeout="20")
    assert ai.current()["base_url"] == fake_ai.base_url

    blocked = save_ai(
        auth_client, csrf,
        base_url="https://api.openai.com/v1", model="gpt-4o-mini",
        local_only="1", timeout="20",
    )
    assert blocked.status_code == 303
    page = auth_client.get(blocked.headers["location"])
    assert "只允许本机" in page.text

    # 坏配置没有落库：还是之前那个本地地址，local_only 也没被打开
    assert ai.current()["base_url"] == fake_ai.base_url
    assert ai.current()["local_only"] is False


def test_local_only_blocks_probe_of_external_address(auth_client, csrf, fake_ai):
    save_ai(
        auth_client, csrf,
        base_url=fake_ai.base_url, model="test-model", timeout="20", local_only="1",
    )
    assert ai.current()["local_only"] is True

    # 存了 local_only=True 之后，即使换个临时地址去 probe，也应被拦下
    res = auth_client.post(
        "/api/ai/probe",
        json={"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "timeout": "10"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["ok"] is False
    joined = " ".join(step.get("detail", "") for step in payload["steps"])
    assert "只允许本机" in joined


def test_local_only_allows_localhost(auth_client, csrf, fake_ai):
    saved = save_ai(
        auth_client, csrf,
        base_url=fake_ai.base_url, model="test-model", timeout="20", local_only="1",
    )
    assert saved.status_code == 303
    assert ai.current()["local_only"] is True
    assert ai.is_enabled() is True


# ---------------------------------------------------------------------------
# redact
# ---------------------------------------------------------------------------
def test_redact_masks_secrets_before_sending(auth_client, csrf, fake_ai):
    save_ai(
        auth_client, csrf,
        base_url=fake_ai.base_url, model="test-model", timeout="20", redact="1",
    )
    assert ai.current()["redact"] is True

    secret = "sk-abcdefghijklmnopqrst"
    res = auth_client.post(
        "/api/ai/summarize",
        json={"title": "测试", "content": f"我的密钥是 {secret}，帮我总结"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    sent = " ".join(str(m.get("content", "")) for m in fake_ai.last_body.get("messages", []))
    assert secret not in sent
    assert "sk-***" in sent


# ---------------------------------------------------------------------------
# /api/ai/probe
# ---------------------------------------------------------------------------
def test_probe_api_returns_steps(auth_client, csrf, fake_ai):
    ok = auth_client.post(
        "/api/ai/probe",
        json={"base_url": fake_ai.base_url, "model": "test-model", "timeout": "10"},
        headers={"X-CSRF-Token": csrf},
    )
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert payload["ok"] is True
    assert [step["name"] for step in payload["steps"]] == ["连通性与密钥", "模型名", "对话"]
    assert [step["status"] for step in payload["steps"]] == ["ok", "ok", "ok"]

    bad = auth_client.post(
        "/api/ai/probe",
        json={"base_url": fake_ai.base_url, "model": "不存在的模型", "timeout": "10"},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad.status_code == 200
    failed = bad.json()
    assert failed["ok"] is False
    assert any(step["status"] == "fail" for step in failed["steps"])


def test_probe_api_requires_base_url(auth_client, csrf):
    res = auth_client.post(
        "/api/ai/probe",
        json={"base_url": "", "model": ""},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 400
    assert res.json()["ok"] is False


# ---------------------------------------------------------------------------
# /api/ai/models
# ---------------------------------------------------------------------------
def test_models_api_success(auth_client, fake_ai):
    res = auth_client.get("/api/ai/models", params={"base_url": fake_ai.base_url, "use_saved": 0})
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload["ok"] is True
    assert payload["models"] == ["other-model", "test-model"]
    # 响应带免费模型标记（fake_ai 是本机地址，无已知免费名单 -> 空列表）
    assert payload["free"] == []


def test_free_models_for_known_providers():
    """已知服务商的免费模型快照：硅基流动向量、智谱 Flash 系列。"""
    from app.services import ai

    assert "BAAI/bge-m3" in ai.free_models_for("https://api.siliconflow.cn/v1")
    assert "glm-4.7-flash" in ai.free_models_for("https://open.bigmodel.cn/api/paas/v4")
    assert "glm-4.7-flash" in ai.free_models_for("https://api.z.ai/api/paas/v4/")
    assert ai.free_models_for("https://api.example.com/v1") == []
    assert ai.free_models_for("") == []


def test_models_api_failure_cannot_connect(auth_client):
    res = auth_client.get("/api/ai/models", params={"base_url": "http://127.0.0.1:1/v1", "use_saved": 0})
    assert res.status_code == 400
    payload = res.json()
    assert payload["ok"] is False
    assert payload["error"]


def test_models_api_reports_unsupported_endpoint(auth_client, fake_ai):
    fake_ai.models_status = 404
    res = auth_client.get("/api/ai/models", params={"base_url": fake_ai.base_url, "use_saved": 0})
    assert res.status_code == 400
    payload = res.json()
    assert payload["ok"] is False
    assert "接口" in payload["error"]


def test_models_api_without_base_url(auth_client):
    res = auth_client.get("/api/ai/models", params={"use_saved": 0})
    assert res.status_code == 400
    assert res.json()["ok"] is False


# ---------------------------------------------------------------------------
# 用量统计
# ---------------------------------------------------------------------------
def test_usage_summary_page_and_reset(auth_client, csrf, fake_ai):
    save_ai(auth_client, csrf, base_url=fake_ai.base_url, model="test-model", timeout="20")

    # 先清空历史，让数字确定
    cleared = auth_client.post(
        "/settings/ai/usage/reset", data={"_csrf": csrf}, follow_redirects=False
    )
    assert cleared.status_code == 303

    # 走一次真实接口：假服务返回 usage 11 / 7 / 18，应该自动记进用量表
    called = auth_client.post(
        "/api/ai/summarize",
        json={"title": "用量测试", "content": "随便写点内容，只是为了触发一次调用。"},
        headers={"X-CSRF-Token": csrf},
    )
    assert called.status_code == 200, called.text

    page = auth_client.get("/settings")
    assert page.status_code == 200
    assert "本月" in page.text
    assert "18" in page.text
    assert "输入 11" in page.text
    assert "输出 7" in page.text
    assert "按任务拆分" in page.text
    assert "摘要" in page.text
    assert "ai-chart__bar" in page.text
    assert "最近 30 天" in page.text
    # 完整版（2026-09-18 复评后恢复）：10 个小节都在
    for needle in ("今日", "本周", "本月", "累计", "本月质量", "平均耗时",
                   "按模型拆分", "按任务拆分", "最近 30 天（按天）", "导出 CSV 明细",
                   "最近 20 次调用", "模型 × 任务", "时间规律",
                   "近 6 个月", "近 14 天明细"):
        assert needle in page.text, f"用量统计里应该有「{needle}」"
    # 「最近的失败」只在真有失败记录时才渲染（这个用例的调用全是成功的）
    # 其中 8 个收进 3 个折叠子块：内容全在，只是不一路铺到底
    assert page.text.count("settings-sub__summary") == 3

    # 重置按钮：清空后页面回到「还没有调用记录」
    reset = auth_client.post(
        "/settings/ai/usage/reset", data={"_csrf": csrf}, follow_redirects=False
    )
    assert reset.status_code == 303
    with db_mod.db() as conn:
        assert ai_usage.summary(conn)["month_calls"] == 0
    assert "还没有任何调用记录" in auth_client.get("/settings").text


def test_usage_reset_requires_csrf(auth_client):
    res = auth_client.post("/settings/ai/usage/reset", follow_redirects=False)
    assert res.status_code == 403
