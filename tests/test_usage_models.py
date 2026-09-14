"""AI 用量统计增强：按模型拆分、累计总量、页面渲染。"""

import pytest


@pytest.fixture()
def _test_conn(client):
    from app import db

    _ = client  # 确保 schema 初始化
    with db.db() as conn:
        yield conn


def test_summary_includes_by_model_and_all_time(_test_conn):
    from app.services import ai_usage

    with _test_conn as conn:
        # session 库是共享的：别的用例也往 ai_usage 里写过记录，
        # 所以模型名取本用例专属的，总量断言一律看「前后增量」。
        before = ai_usage.summary(conn)

        ai_usage.record(conn, model="models-alpha", task="summary",
                        usage={"prompt_tokens": 100, "completion_tokens": 50})
        ai_usage.record(conn, model="models-alpha", task="answer",
                        usage={"prompt_tokens": 10, "completion_tokens": 5})
        ai_usage.record(conn, model="models-beta", task="answer",
                        usage={"prompt_tokens": 7, "completion_tokens": 3})

        summary = ai_usage.summary(conn)

        # 按模型拆分：token 降序、输入输出分开
        models = {item["model"]: item for item in summary["by_model"]}
        assert {"models-alpha", "models-beta"} <= set(models)
        assert models["models-alpha"]["calls"] == 2
        assert models["models-alpha"]["tokens"] == 165
        assert models["models-alpha"]["prompt"] == 110
        assert models["models-beta"]["tokens"] == 10
        assert summary["by_model"][0]["tokens"] >= summary["by_model"][-1]["tokens"]

        # 累计总量（跨月）：只断言本次新增的部分
        assert summary["all_calls"] - before["all_calls"] == 3
        assert summary["all_tokens"] - before["all_tokens"] == 175
        assert summary["since"]  # 最早记录日期


def test_summary_empty_db(_test_conn):
    from app.services import ai_usage

    with _test_conn as conn:
        ai_usage.reset(conn)  # session 库共享，先清空
        summary = ai_usage.summary(conn)
        assert summary["by_model"] == []
        assert summary["all_calls"] == 0 and summary["all_tokens"] == 0
        assert summary["since"] == ""


def test_settings_page_renders_model_breakdown(auth_client):
    from app import db
    from app.services import ai_usage

    with db.db() as conn:
        ai_usage.record(conn, model="test-model", task="summary",
                        usage={"prompt_tokens": 20, "completion_tokens": 8})

    page = auth_client.get("/settings")
    assert page.status_code == 200
    assert "按模型拆分" in page.text
    assert "test-model" in page.text
    assert "累计" in page.text
