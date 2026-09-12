"""E 组：提示词可定制（meta `ai.prompt.*`）+ 模型回退链。

假 AI 服务自己起（参考 tests/test_ai_core.py 的 fake_server，不改它）：
- 记下每次 /chat/completions 的请求体；
- fail_models 里的模型返回 404，其它模型正常返回，用来验证回退链。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import db as db_mod
from app.services import ai, ai_usage


# ---------------------------------------------------------------------------
# 假 AI 服务
# ---------------------------------------------------------------------------
class _PromptAIHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    fail_models: set[str] = set()
    reply = "假模型回答"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        type(self).requests.append(payload if isinstance(payload, dict) else {})
        model = str((payload or {}).get("model") or "")
        if model in type(self).fail_models:
            body = json.dumps(
                {"error": {"message": f"model {model} does not exist"}}, ensure_ascii=False
            ).encode("utf-8")
            self._send(404, body)
            return
        body = json.dumps(
            {
                "choices": [{"message": {"content": type(self).reply}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self._send(200, body)

    def do_GET(self):  # noqa: N802
        body = json.dumps({"object": "list", "data": [{"id": "general-model"}]}).encode("utf-8")
        self._send(200, body)

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 别往测试输出里刷日志
        pass


@pytest.fixture()
def fake_ai():
    _PromptAIHandler.requests = []
    _PromptAIHandler.fail_models = set()
    _PromptAIHandler.reply = "假模型回答"
    server = HTTPServer(("127.0.0.1", 0), _PromptAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()


def base_url(server: HTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_port}/v1"


# ---------------------------------------------------------------------------
# 公共装置
# ---------------------------------------------------------------------------
@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "ai-controls.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


@pytest.fixture(autouse=True)
def restore_ai_state():
    """AI 配置 / 提示词都是模块级全局，测完必须还原。"""
    original = dict(ai.current())
    sources = dict(ai._sources)
    prompts = dict(ai._prompts)
    yield
    ai.configure(original, sources)
    ai._prompts = prompts


@pytest.fixture()
def clean_app_prompts():
    """设置页测试写的是应用真库，前后都清干净，避免污染别的测试文件。"""
    with db_mod.db() as connection:
        ai.reset_prompts(connection)
    ai._prompts = {}
    try:
        yield
    finally:
        with db_mod.db() as connection:
            ai.reset_prompts(connection)
        ai._prompts = {}


# ---------------------------------------------------------------------------
# 1. 默认模板
# ---------------------------------------------------------------------------
def test_default_prompts_are_builtin():
    ai._prompts = {}
    assert ai.prompt_template("summary") == ai.DEFAULT_PROMPTS["summary"]
    described = ai.describe_prompts()
    assert described["summary"]["custom"] is False
    assert described["tags"]["custom"] is False
    assert described["answer"]["custom"] is False
    assert described["title"]["custom"] is False
    assert described["category"]["custom"] is False
    assert set(ai.PROMPT_TASKS) == {"summary", "tags", "title", "category", "answer"}
    for task, needed in ai.PROMPT_PLACEHOLDERS.items():
        for name in needed:
            assert "{" + name + "}" in ai.DEFAULT_PROMPTS[task]
    assert ai.prompt_template("summary").startswith("笔记标题：")
    with pytest.raises(ai.AIError):
        ai.prompt_template("not-a-task")


# ---------------------------------------------------------------------------
# 2. 自定义模板真的会进请求
# ---------------------------------------------------------------------------
def test_custom_summary_prompt_is_sent(conn, fake_ai):
    custom = "自定义摘要指令｜{title}｜{content}｜只回一句话"
    saved = ai.save_prompts(conn, {"summary": custom})
    assert saved["summary"] == custom
    assert ai.prompt_template("summary") == custom
    assert ai.describe_prompts()["summary"]["custom"] is True

    ai.configure({"base_url": base_url(fake_ai), "model": "general-model", "timeout": 10})
    assert ai.summarize("我的标题", "正文内容ABC", conn=conn) == "假模型回答"

    assert len(_PromptAIHandler.requests) == 1
    sent = " ".join(
        str(message.get("content") or "")
        for message in _PromptAIHandler.requests[-1].get("messages", [])
    )
    assert "自定义摘要指令" in sent
    assert "我的标题" in sent
    assert "正文内容ABC" in sent


# ---------------------------------------------------------------------------
# 3. 非法模板被拒，且原模板不变
# ---------------------------------------------------------------------------
def test_custom_tags_and_answer_prompts_are_sent(conn, fake_ai):
    ai.save_prompts(
        conn,
        {
            "tags": "自定义标签｜{title}｜{content}｜已有：{existing}",
            "answer": "自定义问答｜资料：{contexts}｜问题：{question}",
        },
    )
    ai.configure({"base_url": base_url(fake_ai), "model": "general-model", "timeout": 10})

    tags = ai.suggest_tags("标签标题", "标签正文", ["旧标签"], conn=conn)
    assert isinstance(tags, list)
    tag_sent = " ".join(
        str(message.get("content") or "")
        for message in _PromptAIHandler.requests[-1].get("messages", [])
    )
    assert "自定义标签" in tag_sent
    assert "标签标题" in tag_sent and "标签正文" in tag_sent
    assert "旧标签" in tag_sent

    ai.answer("问答问题", [{"title": "来源标题", "content": "来源正文"}], conn=conn)
    answer_sent = " ".join(
        str(message.get("content") or "")
        for message in _PromptAIHandler.requests[-1].get("messages", [])
    )
    assert "自定义问答" in answer_sent
    assert "问答问题" in answer_sent
    assert "来源正文" in answer_sent


def test_save_prompts_validates_tags_and_answer(conn):
    with pytest.raises(ai.AIError) as excinfo:
        ai.save_prompts(conn, {"tags": "没有占位符"})
    assert "existing" in str(excinfo.value) or "content" in str(excinfo.value)

    with pytest.raises(ai.AIError) as excinfo:
        ai.save_prompts(conn, {"answer": "只有问题 {question}"})
    assert "contexts" in str(excinfo.value)


def test_invalid_prompt_rejected_and_original_kept(conn):
    original = ai.prompt_template("summary")
    with pytest.raises(ai.AIError) as excinfo:
        ai.save_prompts(conn, {"summary": "只有标题 {title}"})
    assert "content" in str(excinfo.value)
    assert ai.prompt_template("summary") == original

    too_long = "x" * (ai.PROMPT_MAX_CHARS + 1) + "{title}{content}"
    with pytest.raises(ai.AIError) as excinfo:
        ai.save_prompts(conn, {"tags": too_long})
    assert "太长" in str(excinfo.value) or str(ai.PROMPT_MAX_CHARS) in str(excinfo.value)
    assert ai.prompt_template("tags") == ai.DEFAULT_PROMPTS["tags"]

    # 失败以后库里不该留下任何东西
    ai.bootstrap(conn)
    assert ai.describe_prompts()["summary"]["custom"] is False


# ---------------------------------------------------------------------------
# 4. 模板里多写占位符导致 format 爆炸 → 回退默认模板，功能照常
# ---------------------------------------------------------------------------
def test_broken_format_falls_back_to_default(conn, fake_ai):
    ai.save_prompts(conn, {"summary": "自定义 {title} {content} {oops}"})
    assert "{oops}" in ai.prompt_template("summary")

    ai.configure({"base_url": base_url(fake_ai), "model": "general-model", "timeout": 10})
    assert ai.summarize("标题", "正文", conn=conn) == "假模型回答"

    sent = " ".join(
        str(message.get("content") or "")
        for message in _PromptAIHandler.requests[-1].get("messages", [])
    )
    assert "笔记正文" in sent
    assert "{oops}" not in sent
    assert "自定义 {title}" not in sent


# ---------------------------------------------------------------------------
# 5. 恢复默认
# ---------------------------------------------------------------------------
def test_reset_prompts_back_to_default(conn):
    ai.save_prompts(conn, {"summary": "自定义 {title} {content}"})
    assert ai.describe_prompts()["summary"]["custom"] is True

    ai.reset_prompts(conn)
    assert ai.prompt_template("summary") == ai.DEFAULT_PROMPTS["summary"]
    assert ai.describe_prompts()["summary"]["custom"] is False

    # 再 bootstrap 一次也不该复活
    ai.bootstrap(conn)
    assert ai.prompt_template("summary") == ai.DEFAULT_PROMPTS["summary"]


# ---------------------------------------------------------------------------
# 6. 回退链：分任务模型 404 → 通用模型顶上
# ---------------------------------------------------------------------------
def test_chat_falls_back_to_general_model(conn, fake_ai):
    _PromptAIHandler.fail_models = {"不存在"}
    ai.configure(
        {
            "base_url": base_url(fake_ai),
            "model": "general-model",
            "model_summary": "不存在",
            "timeout": 10,
        }
    )
    assert ai.summarize("标题", "正文", conn=conn) == "假模型回答"

    assert len(_PromptAIHandler.requests) == 2
    assert _PromptAIHandler.requests[0]["model"] == "不存在"
    assert _PromptAIHandler.requests[1]["model"] == "general-model"

    stats = ai_usage.summary(conn)
    assert stats["month_calls"] == 1
    assert stats["last_call"]["model"] == "general-model"


def test_chat_raises_when_both_models_fail(conn, fake_ai):
    _PromptAIHandler.fail_models = {"不存在", "general-model"}
    ai.configure(
        {
            "base_url": base_url(fake_ai),
            "model": "general-model",
            "model_summary": "不存在",
            "timeout": 10,
        }
    )
    with pytest.raises(ai.AIError):
        ai.summarize("标题", "正文", conn=conn)
    assert len(_PromptAIHandler.requests) == 2


# ---------------------------------------------------------------------------
# 7. 设置页「提示词」区块 + 保存 / 恢复
# ---------------------------------------------------------------------------
def test_settings_page_has_prompts_block(auth_client, clean_app_prompts):
    page = auth_client.get("/settings")
    assert page.status_code == 200
    assert "提示词" in page.text
    assert "已自定义 0 项" in page.text
    for name in ("summary", "tags", "answer"):
        assert f'name="{name}"' in page.text
    assert "保存提示词" in page.text
    assert "恢复默认" in page.text
    for placeholder in ("{title}", "{content}", "{question}", "{contexts}"):
        assert placeholder in page.text


def test_prompts_route_saves_and_resets(auth_client, csrf, clean_app_prompts):
    custom = "路由自定义 {title} {content}"
    saved = auth_client.post(
        "/settings/ai/prompts",
        data={"_csrf": csrf, "summary": custom, "tags": "", "answer": ""},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert ai.prompt_template("summary") == custom

    page = auth_client.get("/settings")
    assert "已自定义 1 项" in page.text
    assert custom in page.text

    invalid = auth_client.post(
        "/settings/ai/prompts",
        data={"_csrf": csrf, "summary": "缺少占位符", "tags": "", "answer": ""},
        follow_redirects=False,
    )
    assert invalid.status_code == 400
    assert "缺少占位符" in invalid.text
    assert ai.prompt_template("summary") == custom  # 非法保存没有落库

    reset = auth_client.post(
        "/settings/ai/prompts",
        data={"_csrf": csrf, "reset": "1"},
        follow_redirects=False,
    )
    assert reset.status_code == 303
    assert ai.prompt_template("summary") == ai.DEFAULT_PROMPTS["summary"]


# ---------------------------------------------------------------------------
# 8. 未登录 / 缺 CSRF 被挡
# ---------------------------------------------------------------------------
def test_prompts_route_requires_login(client):
    fresh = client.__class__(client.app)
    response = fresh.post(
        "/settings/ai/prompts",
        data={"summary": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_prompts_route_requires_csrf(auth_client, clean_app_prompts):
    response = auth_client.post(
        "/settings/ai/prompts",
        data={"summary": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert ai.describe_prompts()["summary"]["custom"] is False


# ---------------------------------------------------------------------------
# 5. 新增任务：起标题 / 推荐分类
#    （编辑器里「AI 起标题」「AI 推荐分类」用的就是这两个；提示词同样可改）
# ---------------------------------------------------------------------------
def test_suggest_title_cleans_model_noise(conn, fake_ai):
    """模型很爱回「标题：xxx」还套上引号，直接写进输入框很难看，必须洗成干净的一行。"""
    _PromptAIHandler.reply = '标题：  "异步编程入门"  \n后面这句是它自己加的废话'
    ai.configure({"base_url": base_url(fake_ai), "model": "general-model", "timeout": 10})

    assert ai.suggest_title("", "正文内容ABC", conn=conn) == "异步编程入门"
    sent = " ".join(
        str(m.get("content") or "") for m in _PromptAIHandler.requests[-1].get("messages", [])
    )
    assert "正文内容ABC" in sent  # 正文要真的发给模型


def test_suggest_category_hints_existing_and_does_not_leak_other_tasks(conn, fake_ai):
    _PromptAIHandler.reply = "分类：技术"
    ai.configure({"base_url": base_url(fake_ai), "model": "general-model", "timeout": 10})

    assert ai.suggest_category("我的标题", "正文内容", ["技术", "生活"], conn=conn) == "技术"
    sent = " ".join(
        str(m.get("content") or "") for m in _PromptAIHandler.requests[-1].get("messages", [])
    )
    assert "技术" in sent and "生活" in sent  # 已有分类要提示给模型，好让它复用
    assert "写一句话摘要" not in sent  # 别串到摘要任务上


def test_title_and_category_prompts_are_customizable(conn, fake_ai):
    """新任务和摘要/标签一样，用户在设置页改了模板就要真的生效。"""
    saved = ai.save_prompts(
        conn,
        {
            "title": "自定义标题指令｜{title}｜{content}",
            "category": "自定义分类指令｜{title}｜{content}｜已有：{existing}",
        },
    )
    assert saved["title"].startswith("自定义标题指令")
    ai.configure({"base_url": base_url(fake_ai), "model": "general-model", "timeout": 10})

    ai.suggest_title("原标题", "正文X", conn=conn)
    sent = " ".join(
        str(m.get("content") or "") for m in _PromptAIHandler.requests[-1].get("messages", [])
    )
    assert "自定义标题指令" in sent and "原标题" in sent and "正文X" in sent

    ai.suggest_category("原标题", "正文X", ["已有分类甲"], conn=conn)
    sent = " ".join(
        str(m.get("content") or "") for m in _PromptAIHandler.requests[-1].get("messages", [])
    )
    assert "自定义分类指令" in sent and "已有分类甲" in sent


@pytest.mark.parametrize("raw", ["", "   ", "\n\n", "```\n```", '"  "', "标题：", "分类：", "#"])
def test_clean_inline_never_returns_junk(raw):
    """模型回空/只回噪音时要返回空串，让调用方走「模型没给出」的提示，而不是写垃圾进输入框。"""
    assert ai._clean_inline(raw) == ""
