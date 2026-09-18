"""笔记 Agent v2：可取消 / 格式反馈重试 / 单轮多工具 / 确认门泛化 / 新工具 / 计划执行。

v1 的行为契约在 test_agent.py 里守着；这里只测升级新增的机制。
"""

from __future__ import annotations

import json

import pytest

from app.services import agent as agent_service


class ScriptedChat:
    """按顺序吐出预设回复，并记录每次收到的 messages（与 test_agent.py 同款）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        return self.replies.pop(0)


@pytest.fixture()
def db_conn(client, monkeypatch):
    monkeypatch.setattr(agent_service.ai, "is_enabled", lambda: True)
    from app import db

    with db.db() as conn:
        yield conn


@pytest.fixture()
def seeded_note(db_conn):
    note = agent_service.repo.create_note(
        db_conn, title="Docker 部署手记", content="用 Docker Compose 部署服务。", tags=["docker"]
    )
    return note["id"]


def _script(monkeypatch, replies):
    chat = ScriptedChat(replies)
    monkeypatch.setattr(agent_service.ai, "chat", chat)
    return chat


def _csrf(client) -> str:
    import re

    page = client.get("/agent")
    match = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
    return match.group(1)


# ---------------------------------------------------------------------------
# 可取消
# ---------------------------------------------------------------------------
def test_cancel_stops_run_at_step_boundary(db_conn, seeded_note, monkeypatch):
    """取消标志在模型调用期间置位：当前动作不执行，循环安全收场并落审计。"""
    agent_service.clear_runs(db_conn)
    state = {"calls": 0}

    def scripted(messages, **kwargs):
        state["calls"] += 1
        agent_service.request_cancel("test-run")   # 模型「思考」期间用户点了停止
        if state["calls"] == 1:
            return json.dumps({"action": "list_recent", "params": {"limit": 1}},
                              ensure_ascii=False)
        return json.dumps({"action": "final", "answer": "不应该走到这"}, ensure_ascii=False)

    monkeypatch.setattr(agent_service.ai, "chat", scripted)
    result = agent_service.run_agent(db_conn, "慢慢看", run_id="test-run")
    assert result["ok"] is True
    assert result["cancelled"] is True
    assert result["steps"] == []              # 取消在第一步执行前到达：一个动作都不做
    assert state["calls"] == 1                # 没有继续烧第二次模型调用
    assert "取消" in result["answer"]
    runs = agent_service.list_runs(db_conn)
    assert runs and runs[0]["cancelled"] is True
    assert runs[0]["duration_ms"] >= 0


def test_cancel_during_final_call_still_marks_cancelled(db_conn, monkeypatch):
    """模型正在生成最终回答时用户点了取消：回答照常交付，但审计里带取消标记。"""
    agent_service.clear_runs(db_conn)

    def scripted(messages, **kwargs):
        agent_service.request_cancel("final-run")
        return json.dumps({"action": "final", "answer": "这是最终回答"}, ensure_ascii=False)

    monkeypatch.setattr(agent_service.ai, "chat", scripted)
    result = agent_service.run_agent(db_conn, "任务", run_id="final-run")
    assert result["ok"] is True
    assert result["cancelled"] is True
    assert result["answer"] == "这是最终回答"   # 已经生成的回答不丢


def test_request_cancel_unknown_run_is_false(db_conn):
    assert agent_service.request_cancel("不存在的run") is False


def test_stream_carries_run_id_and_cancel_endpoint_works(auth_client, monkeypatch):
    """流式响应头带 X-Run-Id；结束后按这个 id 取消返回 ok=False（已不在运行）。"""
    from app.services import ai as ai_service

    _script(monkeypatch, [json.dumps({"action": "final", "answer": "好"}, ensure_ascii=False)])
    monkeypatch.setattr(ai_service, "is_enabled", lambda: True)

    res = auth_client.post("/api/agent/stream", json={"task": "随便"},
                           headers={"X-CSRF-Token": _csrf(auth_client)})
    assert res.status_code == 200
    run_id = res.headers.get("X-Run-Id")
    assert run_id, "流式响应必须带 X-Run-Id 响应头"
    # 任务已经跑完：取消返回 False
    body = auth_client.post("/api/agent/cancel", json={"run_id": run_id},
                            headers={"X-CSRF-Token": _csrf(auth_client)}).json()
    assert body["ok"] is False
    # 缺 run_id 报 400
    assert auth_client.post("/api/agent/cancel", json={},
                            headers={"X-CSRF-Token": _csrf(auth_client)}).status_code == 400


def test_cancel_endpoint_needs_auth(client):
    assert client.post("/api/agent/cancel", json={"run_id": "x"}).status_code in (401, 403)


# ---------------------------------------------------------------------------
# 单轮多工具（协议 v2：actions[]）
# ---------------------------------------------------------------------------
def test_multi_action_round_executes_all(db_conn, seeded_note, monkeypatch):
    """actions 数组一轮并做多个独立读操作：一步一个事件，只烧一次模型调用。"""
    script = _script(monkeypatch, [
        json.dumps({"actions": [
            {"action": "search_notes", "params": {"query": "Docker"}},
            {"action": "read_note", "params": {"note_id": seeded_note}},
        ]}, ensure_ascii=False),
        json.dumps({"action": "final", "answer": "都看完了"}, ensure_ascii=False),
    ])
    result = agent_service.run_agent(db_conn, "搜一下再读一遍")
    assert result["ok"] is True
    assert [s["tool"] for s in result["steps"]] == ["search_notes", "read_note"]
    assert len(script.calls) == 2              # 两个工具只花了一次模型调用
    # 两个工具的观察结果一次性都喂回去了（messages 里引号会被再转义一层，按裸名字断言）
    observe = json.dumps(script.calls[1], ensure_ascii=False)
    assert "search_notes" in observe and "read_note" in observe


def test_multi_action_caps_at_four(db_conn, monkeypatch):
    """单轮最多并做 MAX_ACTIONS_PER_TURN 个，超出部分忽略。"""
    reply = {"actions": [{"action": "list_recent", "params": {"limit": 1}}] * 6}
    script = _script(monkeypatch, [
        json.dumps(reply, ensure_ascii=False),
        json.dumps({"action": "final", "answer": "好"}, ensure_ascii=False),
    ])
    result = agent_service.run_agent(db_conn, "看")
    assert len(result["steps"]) == agent_service.MAX_ACTIONS_PER_TURN


def test_multi_action_stops_on_confirmation(db_conn, seeded_note, monkeypatch):
    """并做序列里出现需要确认的动作：生成卡片即停，本轮剩余动作不再执行。"""
    agent_service.clear_runs(db_conn)
    _script(monkeypatch, [
        json.dumps({"actions": [
            {"action": "publish_note", "params": {"note_id": seeded_note}},
            {"action": "add_tags", "params": {"note_id": seeded_note, "tags": ["后面这个"]}},
        ]}, ensure_ascii=False),
    ])
    result = agent_service.run_agent(db_conn, "发布并加标签")
    assert result["ok"] is True
    assert len(result["steps"]) == 1                       # 只有 publish 生成了卡片
    assert "等待用户确认" in result["steps"][0]["summary"]
    pending = agent_service._get_pending_op(db_conn)
    assert pending and pending["action"] == "publish_note"
    # 没执行任何写操作
    note = agent_service.repo.get_note(db_conn, seeded_note)
    assert note["is_public"] in (0, False)
    assert "后面这个" not in note["tags"]


# ---------------------------------------------------------------------------
# 确认门泛化：publish / 大批量 / 大改写
# ---------------------------------------------------------------------------
def test_publish_note_requires_confirmation(db_conn, seeded_note, monkeypatch):
    _script(monkeypatch, [
        json.dumps({"action": "publish_note", "params": {"note_id": seeded_note}},
                   ensure_ascii=False),
    ])
    result = agent_service.run_agent(db_conn, "把这篇发到博客")
    assert "等待用户确认" in result["steps"][0]["summary"]
    note = agent_service.repo.get_note(db_conn, seeded_note)
    assert not note["is_public"], "确认前不能真的公开"
    pending = agent_service._get_pending_op(db_conn)
    assert pending and pending["action"] == "publish_note"
    # 卡片文案是发布语义，不是回收站那套
    assert pending["params"] == {"note_id": seeded_note}


def test_execute_pending_really_publishes(db_conn, seeded_note, monkeypatch):
    _script(monkeypatch, [
        json.dumps({"action": "publish_note", "params": {"note_id": seeded_note, "slug": "deploy"}},
                   ensure_ascii=False),
    ])
    agent_service.run_agent(db_conn, "发布")
    pending = agent_service._get_pending_op(db_conn)
    result = agent_service.execute_pending(db_conn, pending["id"])
    assert result["ok"] is True
    note = agent_service.repo.get_note(db_conn, seeded_note)
    assert note["is_public"]
    assert note["slug"] == "deploy"


def test_unpublish_note_runs_without_confirmation(db_conn, seeded_note, monkeypatch):
    """取消公开是低风险操作，不设确认门。"""
    agent_service.repo.update_note(db_conn, seeded_note, is_public=True)
    script = _script(monkeypatch, [
        json.dumps({"action": "publish_note", "params": {"note_id": seeded_note, "public": False}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "已取消公开"}, ensure_ascii=False),
    ])
    result = agent_service.run_agent(db_conn, "把这篇从博客撤下来")
    assert result["ok"] is True
    assert len(script.calls) == 2          # 直接执行了，没有停在确认分支
    assert agent_service.repo.get_note(db_conn, seeded_note)["is_public"] in (0, False)


def test_bulk_over_threshold_requires_confirmation(db_conn, monkeypatch):
    """批量操作超过阈值：先出确认卡片，不直接动 50 篇。"""
    agent_service.clear_runs(db_conn)
    ids = [agent_service.repo.create_note(db_conn, title=f"批{n}", content="x")["id"]
           for n in range(12)]
    _script(monkeypatch, [
        json.dumps({"action": "bulk_add_tags", "params": {"note_ids": ids, "tags": ["批量"]}},
                   ensure_ascii=False),
    ])
    result = agent_service.run_agent(db_conn, "都加上批量标签")
    assert "等待用户确认" in result["steps"][0]["summary"]
    assert agent_service._get_pending_op(db_conn)["action"] == "bulk_add_tags"
    # 确认前一篇都没动
    note = agent_service.repo.get_note(db_conn, ids[0])
    assert "批量" not in note["tags"]
    # 用户确认后真正落地
    pending = agent_service._get_pending_op(db_conn)
    assert agent_service.execute_pending(db_conn, pending["id"])["ok"] is True
    assert "批量" in agent_service.repo.get_note(db_conn, ids[0])["tags"]


def test_bulk_under_threshold_no_confirmation(db_conn, seeded_note, monkeypatch):
    """小额批量（≤10 篇）不打扰用户，直接执行。"""
    script = _script(monkeypatch, [
        json.dumps({"action": "bulk_add_tags",
                    "params": {"note_ids": [seeded_note], "tags": ["小额"]}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "好了"}, ensure_ascii=False),
    ])
    agent_service.run_agent(db_conn, "加标签")
    assert len(script.calls) == 2
    assert "批量" in agent_service.repo.get_note(db_conn, seeded_note)["tags"] or \
        "小额" in agent_service.repo.get_note(db_conn, seeded_note)["tags"]


def test_update_note_big_rewrite_requires_confirmation(db_conn, monkeypatch):
    """把一篇长笔记整篇换成不相干内容：先确认再动手（防整篇重写事故）。"""
    agent_service.clear_runs(db_conn)
    old = ("原有的长正文。" * 60)          # 420 字 > CONFIRM_REWRITE_MIN_CHARS
    note = agent_service.repo.create_note(db_conn, title="长文", content=old)
    _script(monkeypatch, [
        json.dumps({"action": "update_note",
                    "params": {"note_id": note["id"], "content": "完全无关的全新内容"}},
                   ensure_ascii=False),
    ])
    result = agent_service.run_agent(db_conn, "重写这篇")
    assert "等待用户确认" in result["steps"][0]["summary"]
    assert agent_service.repo.get_note(db_conn, note["id"])["content"] == old
    assert agent_service._get_pending_op(db_conn)["action"] == "update_note"


def test_update_note_small_edit_no_confirmation(db_conn, monkeypatch):
    """正常的小修改（在原文基础上补一段）不触发确认门。"""
    old = "原有的长正文。" * 60
    note = agent_service.repo.create_note(db_conn, title="长文", content=old)
    script = _script(monkeypatch, [
        json.dumps({"action": "update_note",
                    "params": {"note_id": note["id"], "content": old + "\n\n补充一段。"}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "改好了"}, ensure_ascii=False),
    ])
    agent_service.run_agent(db_conn, "补一段")
    assert len(script.calls) == 2
    assert agent_service.repo.get_note(db_conn, note["id"])["content"].endswith("补充一段。")


def test_confirm_event_carries_label_and_consequence(db_conn, seeded_note, monkeypatch):
    """确认卡片带动作文案（label/consequence），前端按动作渲染不再写死。"""
    confirm = None
    for event in agent_service.iter_agent_events(
            db_conn, "发布", run_id="pub-run",
            ):
        pass
    # 走一遍流式拿 step 事件
    _script(monkeypatch, [
        json.dumps({"action": "publish_note", "params": {"note_id": seeded_note}},
                   ensure_ascii=False),
    ])
    for event in agent_service.iter_agent_events(db_conn, "发布这篇"):
        if event["type"] == "step" and event.get("confirm"):
            confirm = event["confirm"]
            break
    assert confirm and confirm["label"] == "发布到博客"
    assert "博客" in confirm["consequence"]
    assert confirm["title"]


# ---------------------------------------------------------------------------
# 新工具：版本历史 / 反向链接 / 语义搜索 / 合并
# ---------------------------------------------------------------------------
def test_note_history_and_restore_tools(db_conn, seeded_note):
    tools = agent_service._make_tools(db_conn)
    original = agent_service.repo.get_note(db_conn, seeded_note)["content"]
    agent_service.repo.update_note(db_conn, seeded_note, content="改过一版的内容", reason="manual")

    history = tools["get_note_history"]["run"]({"note_id": seeded_note})
    assert history["count"] >= 1
    version_id = history["versions"][0]["version_id"]

    restored = tools["restore_version"]["run"]({"note_id": seeded_note, "version_id": version_id})
    assert restored["restored"] is True
    # 快照存的是改动前的内容：恢复后回到 original
    assert agent_service.repo.get_note(db_conn, seeded_note)["content"] == original

    bad = tools["restore_version"]["run"]({"note_id": seeded_note, "version_id": 999999})
    assert "error" in bad


def test_restore_version_counts_as_write():
    for name in ("restore_version", "merge_notes"):
        assert name in agent_service._WRITE_TOOLS


def test_list_backlinks_tool(db_conn):
    tools = agent_service._make_tools(db_conn)
    target = agent_service.repo.create_note(db_conn, title="目标页", content="被链接的正文")
    source = agent_service.repo.create_note(db_conn, title="来源页", content="见 [[目标页]]")
    agent_service.repo.update_note(db_conn, source["id"], content="见 [[目标页]]")

    result = tools["list_backlinks"]["run"]({"note_id": target["id"]})
    assert result["count"] >= 1
    assert any(n["id"] == source["id"] for n in result["notes"])


def test_semantic_search_tool_reports_unavailable(db_conn):
    """测试库没向量索引：工具必须给出可行动的错误提示，而不是崩。"""
    tools = agent_service._make_tools(db_conn)
    result = tools["semantic_search"]["run"]({"query": "随便什么"})
    assert "error" in result
    assert "search_notes" in result["error"]      # 告诉模型改走关键词检索


def test_semantic_search_rejects_empty_query(db_conn):
    tools = agent_service._make_tools(db_conn)
    assert "error" in tools["semantic_search"]["run"]({})


def test_merge_notes_tool(db_conn):
    tools = agent_service._make_tools(db_conn)
    target = agent_service.repo.create_note(db_conn, title="主笔记", content="目标内容")
    s1 = agent_service.repo.create_note(db_conn, title="散稿一", content="第一篇的内容")
    s2 = agent_service.repo.create_note(db_conn, title="散稿二", content="第二篇的内容")

    result = tools["merge_notes"]["run"]({"target_id": target["id"], "source_ids": [s1["id"], s2["id"]]})
    assert result["merged"] is True

    merged = agent_service.repo.get_note(db_conn, target["id"])
    assert "目标内容" in merged["content"]
    assert "第一篇的内容" in merged["content"] and "第二篇的内容" in merged["content"]
    assert "来自《散稿一》" in merged["content"]
    # 源笔记进了回收站（可恢复），目标合并前的正文存了版本历史
    assert agent_service.repo.get_note(db_conn, s1["id"]) is None
    assert agent_service.repo.get_note(db_conn, s2["id"]) is None
    assert agent_service.repo.list_versions(db_conn, target["id"])


def test_merge_notes_rejects_bad_input(db_conn):
    tools = agent_service._make_tools(db_conn)
    note = agent_service.repo.create_note(db_conn, title="A", content="a")
    assert "error" in tools["merge_notes"]["run"]({"target_id": 99999, "source_ids": [note["id"]]})
    assert "error" in tools["merge_notes"]["run"]({"target_id": note["id"], "source_ids": []})
    # 源列表里只有目标自己 = 没有合法源
    assert "error" in tools["merge_notes"]["run"]({"target_id": note["id"], "source_ids": [note["id"]]})


def test_merge_in_loop_requires_confirmation(db_conn, monkeypatch):
    """循环里调 merge_notes：出卡片不动手，确认后才真合并。"""
    agent_service.clear_runs(db_conn)
    target = agent_service.repo.create_note(db_conn, title="合并目标", content="目标")
    source = agent_service.repo.create_note(db_conn, title="合并来源", content="来源内容")
    _script(monkeypatch, [
        json.dumps({"action": "merge_notes",
                    "params": {"target_id": target["id"], "source_ids": [source["id"]]}},
                   ensure_ascii=False),
    ])
    result = agent_service.run_agent(db_conn, "把来源并进目标")
    assert "等待用户确认" in result["steps"][0]["summary"]
    assert agent_service.repo.get_note(db_conn, source["id"]) is not None   # 确认前不动
    pending = agent_service._get_pending_op(db_conn)
    assert agent_service.execute_pending(db_conn, pending["id"])["ok"] is True
    assert agent_service.repo.get_note(db_conn, source["id"]) is None       # 确认后真合并
    merged = agent_service.repo.get_note(db_conn, target["id"])
    assert "来源内容" in merged["content"]


# ---------------------------------------------------------------------------
# 计划执行 / 提示词 / 用量透明
# ---------------------------------------------------------------------------
def test_confirmed_plan_reaches_model(db_conn, seeded_note, monkeypatch):
    """「按计划执行」：计划文本作为系统消息注入，模型被要求严格照做。"""
    plan = "1. search_notes 搜 Docker\n2. add_tags 加「部署」标签"
    script = _script(monkeypatch, [
        json.dumps({"action": "final", "answer": "按计划做完了"}, ensure_ascii=False),
    ])
    agent_service.run_agent(db_conn, "继续", confirmed_plan=plan)
    system_texts = [m["content"] for m in script.calls[0] if m["role"] == "system"]
    assert any("按计划执行" in t and "Docker" in t for t in system_texts)


def test_system_prompt_has_examples_and_multi_action(db_conn, monkeypatch):
    """升级后的提示词带示例与多工具协议说明。"""
    script = _script(monkeypatch, [
        json.dumps({"action": "final", "answer": "好"}, ensure_ascii=False),
    ])
    agent_service.run_agent(db_conn, "任务")
    system = script.calls[0][0]["content"]
    assert "示例" in system
    assert "actions" in system
    assert agent_service._today_label() in system


def test_chat_max_tokens_raised(db_conn, monkeypatch):
    """final 回答上限 1200 → 2000：长总结不再被截断。"""
    seen = {}

    def fake_chat(messages, **kwargs):
        seen.update(kwargs)
        return json.dumps({"action": "final", "answer": "好"}, ensure_ascii=False)

    monkeypatch.setattr(agent_service.ai, "chat", fake_chat)
    agent_service.run_agent(db_conn, "任务")
    assert seen.get("max_tokens") == 2000


def test_run_record_has_duration(db_conn, monkeypatch):
    agent_service.clear_runs(db_conn)
    _script(monkeypatch, [
        json.dumps({"action": "final", "answer": "好"}, ensure_ascii=False),
    ])
    agent_service.run_agent(db_conn, "计时任务")
    run = agent_service.list_runs(db_conn)[0]
    assert "duration_ms" in run and run["duration_ms"] >= 0
    assert "cancelled" in run


# ---------------------------------------------------------------------------
# 编辑页快捷入口 / 页面元素
# ---------------------------------------------------------------------------
def test_editor_has_agent_entry(auth_client, seeded_note, db_conn):
    """编辑页有「让助手处理这篇」入口，带笔记 id 与标题。"""
    db_conn.commit()   # 编辑页路由用另一个连接，先让请求能看到这篇笔记
    page = auth_client.get(f"/notes/{seeded_note}/edit")
    assert page.status_code == 200
    assert f"/agent?note={seeded_note}" in page.text


def test_agent_page_has_cancel_button(auth_client):
    page = auth_client.get("/agent")
    assert page.status_code == 200
    assert 'id="agent-cancel"' in page.text
