"""笔记 Agent：JSON 协议循环、工具执行、API 入口。"""

from __future__ import annotations

import json

import pytest

from app.services import agent as agent_service


# ---------------------------------------------------------------------------
# JSON 抽取
# ---------------------------------------------------------------------------
def test_extract_json_plain():
    assert agent_service._extract_json('{"action": "final", "answer": "好"}')["action"] == "final"


def test_extract_json_with_fence_and_noise():
    raw = '好的，我来处理：\n```json\n{"action": "final", "answer": "完成"}\n```\n以上。'
    assert agent_service._extract_json(raw)["answer"] == "完成"


def test_extract_json_garbage_returns_none():
    assert agent_service._extract_json("我觉得应该直接创建一篇笔记。") is None
    assert agent_service._extract_json("") is None


# ---------------------------------------------------------------------------
# 服务层循环：monkeypatch ai.chat 返回脚本化回复
# ---------------------------------------------------------------------------
class ScriptedChat:
    """按顺序吐出预设回复，并记录每次收到的 messages。"""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def __call__(self, messages, **kwargs):  # 模拟 ai.chat 签名
        self.calls.append(messages)
        return self.replies.pop(0)


@pytest.fixture()
def seeded_note(db_conn):
    """造一篇可被搜到的笔记，返回 id。"""
    note = agent_service.repo.create_note(
        db_conn, title="Docker 部署手记", content="用 Docker Compose 部署服务。", tags=["docker"]
    )
    return note["id"]


@pytest.fixture()
def db_conn(client, monkeypatch):
    """可用的数据库连接：依赖 client 确保 schema 已初始化；AI 视为已配置。"""
    monkeypatch.setattr(agent_service.ai, "is_enabled", lambda: True)
    from app import db

    with db.db() as conn:
        yield conn


def test_agent_search_and_tag_flow(db_conn, seeded_note, monkeypatch):
    """搜到笔记 → 追加标签 → 汇报。三步循环全部生效。"""
    script = ScriptedChat([
        json.dumps({"action": "search_notes", "params": {"query": "Docker"}}, ensure_ascii=False),
        json.dumps({"action": "add_tags", "params": {"note_id": seeded_note, "tags": ["部署"]}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "已给《Docker 部署手记》加上「部署」标签。"},
                   ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)

    result = agent_service.run_agent(db_conn, "给 Docker 相关的笔记加上部署标签")
    assert result["ok"] is True
    assert [s["tool"] for s in result["steps"]] == ["search_notes", "add_tags"]
    assert "部署" in agent_service.repo.get_note(db_conn, seeded_note)["tags"]
    # 观察结果回喂给了模型（第 3 次调用前应有两条 assistant/user 交换）
    assert len(script.calls[2]) == 2 + 2 * 2


def test_agent_create_note(db_conn, monkeypatch):
    script = ScriptedChat([
        json.dumps({"action": "create_note", "params": {
            "title": "读书清单", "content": "《三体》\n《置身事内》", "tags": ["阅读"]}},
            ensure_ascii=False),
        json.dumps({"action": "final", "answer": "已创建《读书清单》。"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)

    result = agent_service.run_agent(db_conn, "新建一篇读书清单")
    assert result["ok"] is True
    found = agent_service.repo.find_notes_by_title(db_conn, "读书清单")
    assert found, "应该能按标题找到新建的笔记"
    note = agent_service.repo.get_note(db_conn, found[0]["id"])
    assert note is not None and note["title"] == "读书清单"
    assert "阅读" in note["tags"]


def test_agent_fabricated_note_id_is_caught(db_conn, seeded_note, monkeypatch):
    """模型编造不存在的 note_id：工具报错并回喂，模型改用 search 自纠。"""
    script = ScriptedChat([
        json.dumps({"action": "add_tags", "params": {"note_id": 99999, "tags": ["x"]}},
                   ensure_ascii=False),
        json.dumps({"action": "search_notes", "params": {"query": "Docker"}}, ensure_ascii=False),
        json.dumps({"action": "add_tags", "params": {"note_id": seeded_note, "tags": ["部署"]}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "好了"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)

    result = agent_service.run_agent(db_conn, "加标签")
    assert result["ok"] is True
    assert "失败" in result["steps"][0]["summary"]  # 第一次失败被如实汇报


def test_agent_stops_after_max_steps(db_conn, monkeypatch):
    """模型一直不收尾：到步数上限就安全停下，绝不无限循环。"""
    script = ScriptedChat([
        json.dumps({"action": "list_recent", "params": {}}) for _ in range(20)
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)

    result = agent_service.run_agent(db_conn, "随便看看", max_steps=6)
    assert result["ok"] is True
    # 同一调用连着重复会被「防空转」提前拦下，所以步数只会 <= 上限，绝不无限循环
    assert 0 < len(result["steps"]) <= 6
    assert "停" in result["answer"]


def test_agent_unformat_reply_becomes_answer(db_conn, monkeypatch):
    """模型没按 JSON 回：把原话当最终回答，不算失败。"""
    monkeypatch.setattr(agent_service.ai, "chat", ScriptedChat(["我觉得没必要加标签。"]))
    result = agent_service.run_agent(db_conn, "加标签")
    assert result["ok"] is True
    assert result["steps"] == []
    assert "没必要" in result["answer"]


def test_agent_requires_ai(db_conn, monkeypatch):
    monkeypatch.setattr(agent_service.ai, "is_enabled", lambda: False)  # 覆盖 fixture 的 True
    result = agent_service.run_agent(db_conn, "加标签")
    assert result["ok"] is False and "尚未配置" in result["error"]


# ---------------------------------------------------------------------------
# HTTP 入口
# ---------------------------------------------------------------------------
def test_agent_page(auth_client):
    page = auth_client.get("/agent")
    assert page.status_code == 200
    assert "agent-task" in page.text
    assert "/static/js/agent.js" in page.text


def test_agent_page_needs_login(client):
    fresh = client.__class__(client.app)
    assert fresh.get("/agent", follow_redirects=False).status_code == 303


def test_agent_api_needs_auth(client):
    res = client.post("/api/agent/run", json={"task": "x"})
    assert res.status_code in (401, 403)


def test_agent_api_rejects_empty_task(auth_client):
    res = auth_client.post(
        "/api/agent/run", json={"task": " "}, headers={"X-CSRF-Token": _csrf(auth_client)}
    )
    assert res.status_code == 400


def test_agent_api_runs_loop(auth_client, monkeypatch):
    script = ScriptedChat([
        json.dumps({"action": "final", "answer": "没有需要动的笔记。"}, ensure_ascii=False),
    ])
    from app.services import ai as ai_service
    monkeypatch.setattr(ai_service, "chat", script)
    monkeypatch.setattr(ai_service, "is_enabled", lambda: True)

    res = auth_client.post(
        "/api/agent/run",
        json={"task": "看看最近有什么笔记"},
        headers={"X-CSRF-Token": _csrf(auth_client)},
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True and payload["answer"]


def _csrf(client) -> str:
    import re

    page = client.get("/agent")
    match = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
    return match.group(1)


# ---------------------------------------------------------------------------
# 流式生成器：iter_agent_events 服务级测试
# ---------------------------------------------------------------------------
def test_iter_agent_events_yields_steps_then_final(db_conn, seeded_note, monkeypatch):
    """每完成一步就 yield step，最后 yield final；写操作真实生效、final.steps 完整。"""
    script = ScriptedChat([
        json.dumps({"action": "search_notes", "params": {"query": "Docker"}}, ensure_ascii=False),
        json.dumps({"action": "add_tags", "params": {"note_id": seeded_note, "tags": ["部署"]}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "已给《Docker 部署手记》加上「部署」标签。"},
                   ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)

    events = list(agent_service.iter_agent_events(db_conn, "给 Docker 相关的笔记加上部署标签"))
    types = [e["type"] for e in events]
    assert types == ["step", "step", "final"]

    final = events[-1]
    assert final["ok"] is True
    assert [s["tool"] for s in final["steps"]] == ["search_notes", "add_tags"]
    assert "部署" in agent_service.repo.get_note(db_conn, seeded_note)["tags"]


def test_iter_agent_events_yields_error_when_ai_disabled(db_conn, monkeypatch):
    """AI 没配置：直接 yield 一个 error 事件，没有 step 也没有 final。"""
    monkeypatch.setattr(agent_service.ai, "is_enabled", lambda: False)
    events = list(agent_service.iter_agent_events(db_conn, "加标签"))
    assert events == [{"type": "error", "error": "尚未配置 AI 服务"}]


def test_run_agent_still_matches_old_shape(db_conn, seeded_note, monkeypatch):
    """run_agent 退化为 iter_agent_events 的消费者，返回值结构和以前一致。"""
    script = ScriptedChat([
        json.dumps({"action": "search_notes", "params": {"query": "Docker"}}, ensure_ascii=False),
        json.dumps({"action": "final", "answer": "完成"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)
    result = agent_service.run_agent(db_conn, "搜 Docker 笔记")
    assert set(result) == {"ok", "answer", "steps", "error"}
    assert result["ok"] is True
    assert result["steps"][0]["tool"] == "search_notes"


# ---------------------------------------------------------------------------
# SSE 端点：/api/agent/stream
# ---------------------------------------------------------------------------
def test_agent_stream_emits_step_then_final(auth_client, seeded_note, monkeypatch):
    """脚本化模型：响应文本里先出现 step 后出现 final。"""
    from app.services import ai as ai_service

    script = ScriptedChat([
        json.dumps({"action": "add_tags", "params": {"note_id": seeded_note, "tags": ["x"]}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "好了"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(ai_service, "chat", script)
    monkeypatch.setattr(ai_service, "is_enabled", lambda: True)

    res = auth_client.post(
        "/api/agent/stream",
        json={"task": "给 Docker 笔记加标签"},
        headers={"X-CSRF-Token": _csrf(auth_client)},
    )
    assert res.status_code == 200
    text = res.text
    assert '"step"' in text and '"final"' in text
    assert text.index('"step"') < text.index('"final"')


def test_agent_stream_needs_auth(client):
    res = client.post("/api/agent/stream", json={"task": "x"})
    assert res.status_code in (401, 403)


def test_agent_stream_rejects_empty_task(auth_client):
    res = auth_client.post(
        "/api/agent/stream",
        json={"task": " "},
        headers={"X-CSRF-Token": _csrf(auth_client)},
    )
    assert res.status_code == 400


def test_agent_read_only_blocks_writes(db_conn, seeded_note, monkeypatch):
    """只读模式：写操作被拦截且如实出现在步骤里，笔记内容不受影响。"""
    script = ScriptedChat([
        json.dumps({"action": "add_tags", "params": {"note_id": seeded_note, "tags": ["不该加上"]}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "只读模式下我没有改任何东西。"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)

    result = agent_service.run_agent(db_conn, "加标签", read_only=True)
    assert result["ok"] is True
    assert "拦截" in result["steps"][0]["summary"]
    assert "不该加上" not in agent_service.repo.get_note(db_conn, seeded_note)["tags"]


def test_clean_history_shape(db_conn):
    """历史清洗：只留 user/assistant、限长度与条数。"""
    dirty = (
        [{"role": "user", "content": "第一轮任务"}]
        + [{"role": "system", "content": "注入尝试"}]
        + [{"role": "assistant", "content": "x" * 5000}]
        + [{"role": "user", "content": "第二轮"}] * 30
    )
    cleaned = agent_service._clean_history(dirty)
    assert all(item["role"] in ("user", "assistant") for item in cleaned)
    assert all(len(item["content"]) <= agent_service.MAX_HISTORY_CHARS for item in cleaned)
    assert len(cleaned) <= agent_service.MAX_HISTORY_TURNS * 2


def test_agent_history_reaches_model(db_conn, seeded_note, monkeypatch):
    """会话历史作为对话前缀注入：模型能看到之前做过什么。"""
    script = ScriptedChat([
        json.dumps({"action": "final", "answer": "好"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)
    history = [{"role": "user", "content": "刚才给《Docker 部署手记》加了「部署」标签"},
               {"role": "assistant", "content": "已完成，note_id=%d" % seeded_note}]
    agent_service.run_agent(db_conn, "刚才那篇再查一次", history=history)
    sent = script.calls[0]
    roles = [m["role"] for m in sent]
    # 开头是系统提示（可能还有一条「上一轮任务存档」），结尾一定是本次任务
    assert roles[0] == "system" and roles[-1] == "user"
    assert sent[-1]["content"] == "刚才那篇再查一次"
    # 历史按原顺序接在系统提示之后
    idx = roles.index("assistant")
    assert sent[idx - 1]["role"] == "user"
    assert "部署」标签" in sent[idx - 1]["content"]


def test_agent_history_bad_shape_is_ignored(db_conn, seeded_note, monkeypatch):
    script = ScriptedChat([
        json.dumps({"action": "final", "answer": "好"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)
    agent_service.run_agent(db_conn, "任务", history="不是列表")
    sent = script.calls[0]
    # 坏历史整条丢掉：除了 system（含存档）就只剩本次任务的 user
    assert [m["role"] for m in sent if m["role"] != "system"] == ["user"]
    assert sent[-1]["content"] == "任务"


def test_agent_run_recorded_in_history(db_conn, seeded_note, monkeypatch):
    """每次任务落审计历史：任务、步骤摘要、涉及笔记、时间。"""
    script = ScriptedChat([
        json.dumps({"action": "add_tags", "params": {"note_id": seeded_note, "tags": ["部署"]}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "好了"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)
    agent_service.clear_runs(db_conn)  # 其它用例也会落历史，先清空
    agent_service.run_agent(db_conn, "给 Docker 笔记加标签")

    runs = agent_service.list_runs(db_conn)
    assert len(runs) == 1
    run = runs[0]
    assert run["task"] == "给 Docker 笔记加标签"
    assert run["ok"] is True
    assert run["steps"][0]["tool"] == "add_tags"
    assert {"id": seeded_note, "title": "Docker 部署手记"} in run["notes"]
    assert run["at"]


def test_agent_retry_on_model_error(db_conn, monkeypatch):
    """模型调用失败自动重试一次：第二次成功则任务照常完成。"""
    calls = {"n": 0}

    def flaky_chat(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise agent_service.ai.AIError("网络抖动")
        return json.dumps({"action": "final", "answer": "重试成功"}, ensure_ascii=False)

    monkeypatch.setattr(agent_service.ai, "chat", flaky_chat)
    result = agent_service.run_agent(db_conn, "任意任务")
    assert result["ok"] is True and result["answer"] == "重试成功"
    assert calls["n"] == 2


def test_agent_runs_endpoints(auth_client, monkeypatch):
    from app import db
    with db.db() as conn:
        agent_service.clear_runs(conn)
        agent_service._record_run(
            conn, "审计任务", ok=True, answer="done", steps=[{"tool": "final", "summary": "s"}],
            error="", read_only=False)
    res = auth_client.get("/api/agent/runs")
    assert res.status_code == 200
    runs = res.json()["runs"]
    assert runs and runs[0]["task"] == "审计任务"

    clear = auth_client.post("/api/agent/runs/clear", headers={"X-CSRF-Token": _csrf(auth_client)})
    assert clear.status_code == 200
    assert auth_client.get("/api/agent/runs").json()["runs"] == []


def test_agent_final_event_carries_notes(db_conn, seeded_note, monkeypatch):
    script = ScriptedChat([
        json.dumps({"action": "read_note", "params": {"note_id": seeded_note}}, ensure_ascii=False),
        json.dumps({"action": "final", "answer": "读完了"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)
    events = list(agent_service.iter_agent_events(db_conn, "读一下"))
    notes = [e.get("notes") for e in events if e["type"] == "final"][0]
    assert {"id": seeded_note, "title": "Docker 部署手记"} in notes


# ---------------------------------------------------------------------------
# 「变强」后的新行为：观察结果不截断正文 / 防空转 / 失败保留进度 / 跨轮存档 / 新工具
# ---------------------------------------------------------------------------
def test_agent_read_note_content_reaches_model_in_full(db_conn, monkeypatch):
    """正文必须完整送到模型面前。

    原来所有工具结果统一截到 OBSERVE_LIMIT（800 字），read_note 的正文也逃不过 ——
    模型每次只看到前 800 字，只能不停换 offset 反复读同一篇，6 步预算就这么烧光了
    （真实记录：总结一篇 2794 字的笔记撞了步数上限）。
    """
    tail = "这是正文结尾的暗号ZZZ"
    body = "开头：" + ("笔记内容。" * 700) + tail
    note = agent_service.repo.create_note(db_conn, title="长笔记", content=body)
    assert len(body) > agent_service.OBSERVE_LIMIT * 2

    script = ScriptedChat([
        json.dumps({"action": "read_note", "params": {"note_id": note["id"]}}, ensure_ascii=False),
        json.dumps({"action": "final", "answer": "读完了"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)
    agent_service.run_agent(db_conn, "读一下这篇")

    # 第二次调用时模型已经拿到工具结果：整段正文都要在，且明说「读完了」
    observation = json.dumps(script.calls[1], ensure_ascii=False)
    assert tail in observation, "正文结尾被截断了"
    assert "has_more" in observation and "false" in observation
    assert "不要再读这篇" in observation


def test_agent_read_note_whole_note_is_one_step(db_conn, seeded_note, monkeypatch):
    """短笔记一次读完就够：第一步读、第二步收尾，不再反复翻页。"""
    script = ScriptedChat([
        json.dumps({"action": "read_note", "params": {"note_id": seeded_note}}, ensure_ascii=False),
        json.dumps({"action": "final", "answer": "好"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)
    result = agent_service.run_agent(db_conn, "读一下")
    assert len(result["steps"]) == 1  # 只有那一次 read_note


def test_agent_repeated_call_is_blocked(db_conn, seeded_note, monkeypatch):
    """同一工具 + 完全一样的参数反复调：拦下来并提前收场，不烧完预算。"""
    script = ScriptedChat([
        json.dumps({"action": "list_recent", "params": {"limit": 3}}, ensure_ascii=False)
        for _ in range(20)
    ])
    monkeypatch.setattr(agent_service.ai, "chat", script)
    result = agent_service.run_agent(db_conn, "看看最近", max_steps=16)
    assert result["ok"] is True
    # 第一步正常执行，后面全是重复 → 连撞 MAX_REPEAT_STEPS 次就停
    assert len(result["steps"]) <= agent_service.MAX_REPEAT_STEPS + 1
    assert "重复" in result["answer"]
    assert any("跳过重复" in step["summary"] for step in result["steps"])


def test_agent_partial_answer_when_model_dies(db_conn, seeded_note, monkeypatch):
    """模型中途挂掉：已完成的步骤要交代清楚，并给出补救建议，而不是交白卷。"""
    calls = {"n": 0}

    def flaky(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"action": "read_note", "params": {"note_id": seeded_note}},
                              ensure_ascii=False)
        raise agent_service.ai.AIError("等待超过 45 秒还没响应")

    monkeypatch.setattr(agent_service.ai, "chat", flaky)
    result = agent_service.run_agent(db_conn, "读一下再改")
    assert result["ok"] is False
    assert "45 秒" in result["error"]
    assert result["answer"], "失败也要给用户交代，不能是空回答"
    assert "没能跑完" in result["answer"]
    assert "已读取" in result["answer"]          # 已完成的步骤写进去了
    assert "超时" in result["answer"]            # 补救建议


def test_agent_recap_reaches_model_for_continuation(db_conn, seeded_note, monkeypatch):
    """跨轮存档：用户说「继续」时，上一轮动过的 note_id 要能找回来。"""
    agent_service.clear_runs(db_conn)
    agent_service._record_run(
        db_conn, "帮我总结《Docker 部署手记》", ok=True, answer="总结已写回笔记",
        steps=[{"tool": "read_note", "summary": "已读取《Docker 部署手记》"}],
        error="", read_only=False, involved={seeded_note: "Docker 部署手记"},
    )
    script = ScriptedChat([json.dumps({"action": "final", "answer": "接着说"}, ensure_ascii=False)])
    monkeypatch.setattr(agent_service.ai, "chat", script)
    agent_service.run_agent(db_conn, "继续")

    sent = script.calls[0]
    recap = [m for m in sent if m["role"] == "system" and "上一轮任务的存档" in m["content"]]
    assert recap, "应该注入上一轮任务的存档"
    assert f"#{seeded_note}" in recap[0]["content"]
    assert "Docker 部署手记" in recap[0]["content"]
    assert "继续" in sent[-1]["content"]


def test_agent_recap_empty_without_history(db_conn, monkeypatch):
    agent_service.clear_runs(db_conn)
    script = ScriptedChat([json.dumps({"action": "final", "answer": "好"}, ensure_ascii=False)])
    monkeypatch.setattr(agent_service.ai, "chat", script)
    agent_service.run_agent(db_conn, "新任务")
    assert not [m for m in script.calls[0] if "上一轮任务的存档" in m["content"]]


def test_agent_prompt_carries_today(db_conn, monkeypatch):
    """系统提示词要带今天的日期，否则「这周/最近三天」这类任务没法算。"""
    script = ScriptedChat([json.dumps({"action": "final", "answer": "好"}, ensure_ascii=False)])
    monkeypatch.setattr(agent_service.ai, "chat", script)
    agent_service.run_agent(db_conn, "总结这周写的笔记")
    system = script.calls[0][0]["content"]
    assert agent_service._today_label() in system
    assert "是 " in system or "今天是" in system


def test_agent_management_tools_work(db_conn, seeded_note):
    """补齐的管理类工具都能真正改到数据。"""
    tools = agent_service._make_tools(db_conn)
    for name in ("remove_tags", "add_tags", "set_category", "archive_note", "pin_note",
                 "star_note", "trash_note", "restore_note", "list_tags", "list_categories",
                 "note_stats", "update_note"):
        assert name in tools, f"缺少工具 {name}"

    nid = seeded_note
    assert tools["add_tags"]["run"]({"note_id": nid, "tags": ["部署", "运维"]})["updated"] is True
    removed = tools["remove_tags"]["run"]({"note_id": nid, "tags": ["运维"]})
    assert removed["removed"] == ["运维"] and "运维" not in removed["tags"]
    assert tools["set_category"]["run"]({"note_id": nid, "category": "运维"})["category"] == "运维"
    assert tools["archive_note"]["run"]({"note_id": nid})["archived"] is True
    assert tools["archive_note"]["run"]({"note_id": nid, "archived": False})["archived"] is False
    assert tools["pin_note"]["run"]({"note_id": nid})["pinned"] is True
    assert tools["star_note"]["run"]({"note_id": nid, "starred": True})["starred"] is True
    # update_note 现在也能改分类/标签（tags 是整体替换）
    assert tools["update_note"]["run"]({"note_id": nid, "tags": ["只剩这个"]})["updated"] is True
    assert agent_service.repo.get_note(db_conn, nid)["tags"] == ["只剩这个"]

    # 回收站往返
    assert tools["trash_note"]["run"]({"note_id": nid})["trashed"] is True
    assert agent_service.repo.get_note(db_conn, nid) is None
    assert tools["restore_note"]["run"]({"note_id": nid})["restored"] is True
    assert agent_service.repo.get_note(db_conn, nid) is not None

    # 只读性质的汇总工具
    assert "tags" in tools["list_tags"]["run"]({})
    assert "categories" in tools["list_categories"]["run"]({})
    stats = tools["note_stats"]["run"]({})
    assert stats["notes"] >= 1 and "words" in stats


def test_agent_remove_tags_is_a_noop_when_absent(db_conn, seeded_note):
    tools = agent_service._make_tools(db_conn)
    result = tools["remove_tags"]["run"]({"note_id": seeded_note, "tags": ["根本没有的标签"]})
    assert result["updated"] is False and "本来就没有" in result["note"]


def test_agent_search_notes_supports_filters(db_conn):
    """按标签 / 分类 / 最近天数检索（以前只能按关键词搜）。"""
    tools = agent_service._make_tools(db_conn)
    repo = agent_service.repo
    a = repo.create_note(db_conn, title="筛选甲", content="内容甲")
    repo.update_note(db_conn, a["id"], tags=["筛选用"], category="筛选分类")
    b = repo.create_note(db_conn, title="筛选乙", content="内容乙")

    by_tag = tools["search_notes"]["run"]({"tag": "筛选用"})
    assert [n["id"] for n in by_tag["notes"]] == [a["id"]]
    by_cat = tools["search_notes"]["run"]({"category": "筛选分类"})
    assert [n["id"] for n in by_cat["notes"]] == [a["id"]]
    by_q = tools["search_notes"]["run"]({"query": "筛选乙"})
    assert [n["id"] for n in by_q["notes"]] == [b["id"]]
    # 一个条件都不给要报错，别让模型拿全库当结果
    assert "error" in tools["search_notes"]["run"]({})
    # days=30 能覆盖刚建的笔记
    assert "notes" in tools["search_notes"]["run"]({"days": 30})


def test_agent_read_note_offset_beyond_end_is_safe(db_conn, seeded_note):
    tools = agent_service._make_tools(db_conn)
    result = tools["read_note"]["run"]({"note_id": seeded_note, "offset": 999999})
    assert result["has_more"] is False and result["content"] == ""


def test_agent_write_tools_cover_new_actions():
    for name in ("remove_tags", "archive_note", "trash_note", "restore_note",
                 "pin_note", "star_note"):
        assert name in agent_service._WRITE_TOOLS, f"{name} 应该算写操作（只读模式要拦）"
    assert "search_notes" not in agent_service._WRITE_TOOLS


def test_agent_usage_is_attributed_to_agent(db_conn, monkeypatch):
    """笔记助手的用量要单独归因，不能再和问笔记混在同一个任务名下。

    原来 agent 也记成 task="answer"，于是「用量统计」里根本看不出助手花了多少。
    """
    import json

    from app.services import agent as agent_mod
    from app.services import ai

    seen: dict = {}

    def fake_chat(messages, **kwargs):
        seen.update(kwargs)
        return json.dumps({"action": "final", "answer": "好"}, ensure_ascii=False)

    monkeypatch.setattr(ai, "chat", fake_chat)
    agent_mod.run_agent(db_conn, "随便做点什么")
    assert seen.get("task") == "agent"


def test_agent_page_header_has_no_ask_button(auth_client):
    """/agent 右上角的「问笔记」按钮是冗余入口（导航里已有），删掉后不该回来。"""
    page = auth_client.get("/agent")
    assert page.status_code == 200
    head = page.text.split("</h1>", 1)[1].split("agent-layout", 1)[0]
    assert "问笔记" not in head, "页头不应再有问笔记按钮"


def test_agent_history_section_is_collapsible(auth_client):
    """本轮会话区块要能收起（details + summary + 计数徽标）。"""
    page = auth_client.get("/agent")
    assert '<details class="agent-history" id="agent-history" hidden>' in page.text
    assert "agent-history-count" in page.text
