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

    history = tools["get_note_history"].run({"note_id": seeded_note})
    assert history["count"] >= 1
    version_id = history["versions"][0]["version_id"]

    restored = tools["restore_version"].run({"note_id": seeded_note, "version_id": version_id})
    assert restored["restored"] is True
    # 快照存的是改动前的内容：恢复后回到 original
    assert agent_service.repo.get_note(db_conn, seeded_note)["content"] == original

    bad = tools["restore_version"].run({"note_id": seeded_note, "version_id": 999999})
    assert "error" in bad


def test_restore_version_counts_as_write():
    for name in ("restore_version", "merge_notes"):
        assert name in agent_service._WRITE_TOOLS


def test_list_backlinks_tool(db_conn):
    tools = agent_service._make_tools(db_conn)
    target = agent_service.repo.create_note(db_conn, title="目标页", content="被链接的正文")
    source = agent_service.repo.create_note(db_conn, title="来源页", content="见 [[目标页]]")
    agent_service.repo.update_note(db_conn, source["id"], content="见 [[目标页]]")

    result = tools["list_backlinks"].run({"note_id": target["id"]})
    assert result["count"] >= 1
    assert any(n["id"] == source["id"] for n in result["notes"])


def test_semantic_search_tool_reports_unavailable(db_conn):
    """测试库没向量索引：工具必须给出可行动的错误提示，而不是崩。"""
    tools = agent_service._make_tools(db_conn)
    result = tools["semantic_search"].run({"query": "随便什么"})
    assert "error" in result
    assert "search_notes" in result["error"]      # 告诉模型改走关键词检索


def test_semantic_search_rejects_empty_query(db_conn):
    tools = agent_service._make_tools(db_conn)
    assert "error" in tools["semantic_search"].run({})


def test_merge_notes_tool(db_conn):
    tools = agent_service._make_tools(db_conn)
    target = agent_service.repo.create_note(db_conn, title="主笔记", content="目标内容")
    s1 = agent_service.repo.create_note(db_conn, title="散稿一", content="第一篇的内容")
    s2 = agent_service.repo.create_note(db_conn, title="散稿二", content="第二篇的内容")

    result = tools["merge_notes"].run({"target_id": target["id"], "source_ids": [s1["id"], s2["id"]]})
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
    assert "error" in tools["merge_notes"].run({"target_id": 99999, "source_ids": [note["id"]]})
    assert "error" in tools["merge_notes"].run({"target_id": note["id"], "source_ids": []})
    # 源列表里只有目标自己 = 没有合法源
    assert "error" in tools["merge_notes"].run({"target_id": note["id"], "source_ids": [note["id"]]})


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


# ---------------------------------------------------------------------------
# 2026-09-18 晚：精准编辑与整理五工具
# ---------------------------------------------------------------------------

def _tools(db_conn):
    return agent_service._make_tools(db_conn)


def test_replace_in_note_basic_count_and_zero_hit(db_conn):
    """替换全部 / 限定次数 / 零命中 / 同文拦截。"""
    note = agent_service.repo.create_note(
        db_conn, title="替换试验田", content="旧词一 旧词二 旧词三，其余不动。"
    )
    run = _tools(db_conn)["replace_in_note"].run
    out = run({"note_id": note["id"], "find": "旧词", "replace_with": "新词", "count": 2})
    assert out["replaced"] == 2 and out["occurrences"] == 3
    assert "新词一 新词二 旧词三" in agent_service.repo.get_note(db_conn, note["id"])["content"]
    out = run({"note_id": note["id"], "find": "旧词", "replace_with": "终词"})
    assert out["replaced"] == 1
    assert run({"note_id": note["id"], "find": "查无此物", "replace_with": "x"})["replaced"] == 0
    assert "error" in run({"note_id": note["id"], "find": "新词", "replace_with": "新词"})
    assert "error" in run({"note_id": note["id"], "find": "", "replace_with": "x"})


def test_replace_in_note_mass_hits_require_confirm(db_conn, monkeypatch):
    """命中 ≥ 10 处：不直接执行，生成确认卡片；确认后才落地。"""
    body = " ".join(["占位词"] * 12)
    note = agent_service.repo.create_note(db_conn, title="批量替换", content=body)
    chat = ScriptedChat([
        json.dumps({"action": "replace_in_note",
                    "params": {"note_id": note["id"], "find": "占位词", "replace_with": "终"}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "等待确认。"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "is_enabled", lambda: True)
    monkeypatch.setattr(agent_service.ai, "chat", chat)

    result = agent_service.run_agent(db_conn, "把这篇里的占位词都换掉")
    assert result["ok"] is True
    assert "等待用户确认" in result["steps"][0]["summary"]
    # 没有真正执行
    assert "占位词" in agent_service.repo.get_note(db_conn, note["id"])["content"]
    # 用户确认后执行
    pending = agent_service._get_pending_op(db_conn)
    assert pending and pending["action"] == "replace_in_note"
    done = agent_service.execute_pending(db_conn, pending["id"])
    assert done["ok"] is True and done["result"]["replaced"] == 12
    assert "占位词" not in agent_service.repo.get_note(db_conn, note["id"])["content"]
    agent_service._clear_pending_op(db_conn)


def test_prepend_note_tool(db_conn):
    note = agent_service.repo.create_note(db_conn, title="开头插入", content="正文在这里。")
    run = _tools(db_conn)["prepend_note"].run
    assert run({"note_id": note["id"], "content": ""}) .get("error")
    out = run({"note_id": note["id"], "content": "TL;DR：先看这个。"})
    assert out["updated"] is True
    content = agent_service.repo.get_note(db_conn, note["id"])["content"]
    assert content.startswith("TL;DR：先看这个。") and "正文在这里。" in content
    empty = agent_service.repo.create_note(db_conn, title="空笔记", content="")
    out = run({"note_id": empty["id"], "content": "只有这段。"})
    assert agent_service.repo.get_note(db_conn, empty["id"])["content"] == "只有这段。"


def test_rewrite_section_tool(db_conn):
    """重写一节：标题保留、### 子节被替换、其它节不动；找不到时列出可用小节。"""
    content = (
        "# 总标题\n\n## 安装\n\n旧步骤一。\n\n### 依赖\n\n旧依赖说明。\n\n## 使用\n\n使用说明保持不变。\n"
    )
    note = agent_service.repo.create_note(db_conn, title="章节重写", content=content)
    run = _tools(db_conn)["rewrite_section"].run
    miss = run({"note_id": note["id"], "section": "不存在", "content": "x"})
    assert "sections" in miss and "安装" in miss["sections"]
    out = run({"note_id": note["id"], "section": "安装", "content": "新步骤。\n\n### 依赖\n\n新依赖。"})
    assert out["updated"] is True
    new = agent_service.repo.get_note(db_conn, note["id"])["content"]
    assert "## 安装" in new and "新步骤。" in new and "旧步骤一" not in new
    assert "## 使用" in new and "使用说明保持不变。" in new  # 其它节没动
    # 不带 # 号也能匹配；大小写不敏感
    out = run({"note_id": note["id"], "section": "使用", "content": "新使用说明。"})
    assert "新使用说明。" in agent_service.repo.get_note(db_conn, note["id"])["content"]


def test_rewrite_section_big_rewrite_requires_confirm(db_conn):
    """原小节 ≥ 200 字且新内容大改：先生成确认卡片，确认后执行且标题保留。"""
    long_body = "这是一段很长的旧正文。" * 30
    note = agent_service.repo.create_note(
        db_conn, title="大改写", content=f"## 长节\n\n{long_body}\n\n## 别动\n\n保持。"
    )
    chat = ScriptedChat([
        json.dumps({"action": "rewrite_section",
                    "params": {"note_id": note["id"], "section": "长节",
                               "content": "完全不同的新内容。"}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "已生成确认卡片。"}, ensure_ascii=False),
    ])
    import pytest as mp
    from app.services import ai as ai_mod
    m = mp.MonkeyPatch()
    m.setattr(ai_mod, "is_enabled", lambda: True)
    m.setattr(ai_mod, "chat", chat)
    result = agent_service.run_agent(db_conn, "重写长节")
    assert "等待用户确认" in result["steps"][0]["summary"]
    assert long_body in agent_service.repo.get_note(db_conn, note["id"])["content"]
    pending = agent_service._get_pending_op(db_conn)
    done = agent_service.execute_pending(db_conn, pending["id"])
    assert done["ok"] is True
    new = agent_service.repo.get_note(db_conn, note["id"])["content"]
    assert "## 长节" in new and "完全不同的新内容。" in new and "## 别动" in new
    assert long_body not in new
    agent_service._clear_pending_op(db_conn)


def test_bulk_set_category_tool_and_confirm(db_conn):
    """批量设分类：改的才计数、缺的跳过、空串=清除；>10 篇走确认卡。"""
    ids = [agent_service.repo.create_note(db_conn, title=f"批量 {i}", content="x",
                                          category="旧分类")["id"] for i in range(3)]
    ids.append(agent_service.repo.create_note(db_conn, title="已是目标", content="x",
                                              category="技术")["id"])
    run = _tools(db_conn)["bulk_set_category"].run
    out = run({"note_ids": ids, "category": "技术"})
    assert out["updated"] == 3 and out["unchanged"] == 1
    out = run({"note_ids": ids + [99999], "category": ""})
    assert out["updated"] == 4 and out["missing"] == [99999]
    assert all((agent_service.repo.get_note(db_conn, i)["category"] or "") == "" for i in ids)
    # 循环层：11 篇 → 确认卡片
    chat = ScriptedChat([
        json.dumps({"action": "bulk_set_category",
                    "params": {"note_ids": list(range(1, 12)), "category": "归档"}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "等确认。"}, ensure_ascii=False),
    ])
    import pytest as mp
    from app.services import ai as ai_mod
    m = mp.MonkeyPatch()
    m.setattr(ai_mod, "is_enabled", lambda: True)
    m.setattr(ai_mod, "chat", chat)
    result = agent_service.run_agent(db_conn, "把这批都归到归档分类")
    assert "等待用户确认" in result["steps"][0]["summary"]
    agent_service._clear_pending_op(db_conn)


def test_find_similar_keyword_fallback(db_conn, monkeypatch):
    """未配向量时按共同标签/互链打分：共享标签的排出来，无关的不出现。"""
    monkeypatch.setattr("app.services.ai_related.related_notes", lambda *a, **k: None)
    a = agent_service.repo.create_note(db_conn, title="A 主笔记", content="x", tags=["python", "测试"])
    b = agent_service.repo.create_note(db_conn, title="B 相似", content="x", tags=["python"])
    agent_service.repo.create_note(db_conn, title="C 无关", content="x", tags=["生活"])
    out = _tools(db_conn)["find_similar"].run({"note_id": a["id"]})
    assert out["engine"] == "keyword"
    names = [n["title"] for n in out["notes"]]
    assert "B 相似" in names and "C 无关" not in names
    assert out["notes"][0]["score"] >= 2.0
    assert "error" in _tools(db_conn)["find_similar"].run({"note_id": 999999})


def test_search_notes_zero_hit_hint(db_conn):
    out = _tools(db_conn)["search_notes"].run({"query": "绝对查无此词的字符串"})
    assert out["count"] == 0 and "semantic_search" in out.get("hint", "")


def test_new_write_tools_blocked_in_read_only(db_conn):
    """只读模式拦住新写工具，数据不动。"""
    note = agent_service.repo.create_note(db_conn, title="只读试验", content="原文不动。")
    chat = ScriptedChat([
        json.dumps({"action": "replace_in_note",
                    "params": {"note_id": note["id"], "find": "原文", "replace_with": "改"}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "只读模式做不了。"}, ensure_ascii=False),
    ])
    import pytest as mp
    from app.services import ai as ai_mod
    m = mp.MonkeyPatch()
    m.setattr(ai_mod, "is_enabled", lambda: True)
    m.setattr(ai_mod, "chat", chat)
    result = agent_service.run_agent(db_conn, "试试改", read_only=True)
    assert any("已拦截写操作 replace_in_note" in s["summary"] for s in result["steps"])
    assert agent_service.repo.get_note(db_conn, note["id"])["content"] == "原文不动。"


def test_registry_describes_new_tools(db_conn):
    tools = _tools(db_conn)
    for name in ("replace_in_note", "prepend_note", "rewrite_section",
                 "bulk_set_category", "find_similar"):
        spec = tools.get(name)
        assert spec and spec.description and spec.params, f"注册表缺 {name}"
        assert name in agent_service._describe_tools(tools)
    # 写工具进只读黑名单；条件确认动作在确认白名单里
    for name in ("replace_in_note", "prepend_note", "rewrite_section", "bulk_set_category"):
        assert name in agent_service._WRITE_TOOLS
    for name in ("replace_in_note", "rewrite_section", "bulk_set_category"):
        assert name in agent_service._ALL_CONFIRMABLE


# ---------------------------------------------------------------------------
# 2026-09-18 深夜：全面升级——跨篇替换 / 写作节奏 / 每步计时 / 步数预算
# ---------------------------------------------------------------------------

def test_bulk_replace_text_confirm_flow(db_conn, monkeypatch):
    """跨篇替换：机制层总确认 → 确认后逐篇落地；缺的跳过、没命中的计 0。"""
    ids = [agent_service.repo.create_note(
        db_conn, title=f"跨篇 {i}", content="这里有旧词，还有旧词。")["id"] for i in range(3)]
    ids.append(999999)   # 不存在：执行时跳过
    chat = ScriptedChat([
        json.dumps({"action": "bulk_replace_text",
                    "params": {"note_ids": ids, "find": "旧词", "replace_with": "新词"}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "等确认。"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "is_enabled", lambda: True)
    monkeypatch.setattr(agent_service.ai, "chat", chat)
    result = agent_service.run_agent(db_conn, "把这几篇里的旧词都换掉")
    assert "等待用户确认" in result["steps"][0]["summary"]
    assert "旧词" in agent_service.repo.get_note(db_conn, ids[0])["content"]   # 没执行
    pending = agent_service._get_pending_op(db_conn)
    assert pending and pending["action"] == "bulk_replace_text"
    done = agent_service.execute_pending(db_conn, pending["id"])
    assert done["ok"] is True
    out = done["result"]
    assert out["replaced_total"] == 6 and out["notes_changed"] == 3
    assert out["notes_skipped"] == 1 and out["results"][-1]["missing"] is True
    for nid in ids[:3]:
        assert "旧词" not in agent_service.repo.get_note(db_conn, nid)["content"]
    agent_service._clear_pending_op(db_conn)


def test_bulk_replace_text_validation(db_conn):
    run = _tools(db_conn)["bulk_replace_text"].run
    assert "error" in run({"find": "x", "replace_with": "y"})
    assert "error" in run({"note_ids": [1], "find": "", "replace_with": "y"})
    assert "error" in run({"note_ids": [1], "find": "x", "replace_with": "x"})
    assert "error" in run({"note_ids": [], "find": "x", "replace_with": "y"})
    # 非法 id：_as_int 直接抛异常，循环层捕获后回喂「参数不合法」
    with pytest.raises((TypeError, ValueError)):
        run({"note_ids": ["abc"], "find": "x", "replace_with": "y"})


def test_bulk_replace_text_blocked_in_read_only(db_conn, monkeypatch):
    note = agent_service.repo.create_note(db_conn, title="只读跨篇", content="原文。")
    chat = ScriptedChat([
        json.dumps({"action": "bulk_replace_text",
                    "params": {"note_ids": [note["id"]], "find": "原文", "replace_with": "改"}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "只读。"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "is_enabled", lambda: True)
    monkeypatch.setattr(agent_service.ai, "chat", chat)
    result = agent_service.run_agent(db_conn, "跨篇改", read_only=True)
    assert any("已拦截写操作 bulk_replace_text" in s["summary"] for s in result["steps"])
    assert agent_service.repo.get_note(db_conn, note["id"])["content"] == "原文。"


def test_writing_activity_tool(db_conn):
    """按创建日期聚合篇数与字数；给出最忙的一天。"""
    from datetime import date, timedelta
    # 用未来日期做数据桶：其它用例都在「今天」建笔记，只有这个桶是我们独占的
    day_a = (date.today() + timedelta(days=3)).isoformat()   # 两篇
    day_b = (date.today() + timedelta(days=1)).isoformat()   # 一篇
    agent_service.repo.create_note(db_conn, title="桶A的 1", content="五字五字五字五字五字",
                                   created_at=f"{day_a} 10:00:00")
    agent_service.repo.create_note(db_conn, title="桶A的 2", content="三字三字三字",
                                   created_at=f"{day_a} 18:00:00")
    agent_service.repo.create_note(db_conn, title="桶B", content="一笔",
                                   created_at=f"{day_b} 09:00:00")
    out = _tools(db_conn)["writing_activity"].run({"days": 30})
    by_date = {x["date"]: x for x in out["series"]}
    assert by_date[day_a]["notes"] == 2 and by_date[day_a]["words"] >= 16
    assert by_date[day_b]["notes"] == 1
    # 共享库里别的用例可能把「今天」堆得更高：只断言 max 逻辑不小于我们已知的数据桶
    assert out["busiest_day"]["notes"] >= by_date[day_a]["notes"]
    assert out["most_words_day"]["words"] >= by_date[day_a]["words"]


def test_steps_carry_duration(db_conn, monkeypatch):
    """每一步（含执行历史落库）都带 duration_ms，且总步数预算提到 20。"""
    assert agent_service.MAX_STEPS == 20
    note = agent_service.repo.create_note(db_conn, title="计时", content="x")
    chat = ScriptedChat([
        json.dumps({"action": "read_note", "params": {"note_id": note["id"]}},
                   ensure_ascii=False),
        json.dumps({"action": "final", "answer": "读完。"}, ensure_ascii=False),
    ])
    monkeypatch.setattr(agent_service.ai, "is_enabled", lambda: True)
    monkeypatch.setattr(agent_service.ai, "chat", chat)
    result = agent_service.run_agent(db_conn, "读一下")
    assert result["steps"]
    for step in result["steps"]:
        assert isinstance(step["duration_ms"], int) and step["duration_ms"] >= 0
    # 执行历史（审计落库）也保留每步耗时
    runs = agent_service.list_runs(db_conn, limit=1)
    assert runs and all("duration_ms" in s for s in runs[0]["steps"])
