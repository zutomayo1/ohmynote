"""用量统计的详情维度、迁移、CSV 导出、失败记账。

要点：**不假设库是干净的** —— session 级 client 让整个会话共用一个库，
别的用例早就留了记录。所以要么用「前后增量」断言，要么直接按自己造的
时间戳/模型名去查。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest


@pytest.fixture()
def usage_db(client):
    """拿到连接（借 client 触发建表），并确保 ai_usage 表存在。"""
    from app import db
    from app.services import ai_usage

    _ = client
    with db.db() as conn:
        ai_usage.ensure(conn)
        yield conn


def _insert(conn, *, model, task, prompt=10, completion=5, when, latency_ms=0, ok=1, error=""):
    conn.execute(
        "INSERT INTO ai_usage (model, task, prompt_tokens, completion_tokens, total_tokens,"
        " created_at, latency_ms, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (model, task, prompt, completion, prompt + completion, when, latency_ms, ok, error),
    )


# ---------------------------------------------------------------------------
# 维度：按小时 / 按星期 / 按月 / 模型×任务
# ---------------------------------------------------------------------------
def test_summary_breaks_down_by_hour_weekday_and_month(usage_db):
    from app.services import ai_usage

    conn = usage_db
    today = date.today()
    # 造三条落在确定时间点上的记录（今早 9 点 / 昨天 21 点 / 上个月）
    stamp_today = f"{today.isoformat()} 09:30:00"
    yesterday = today - timedelta(days=1)
    stamp_yesterday = f"{yesterday.isoformat()} 21:05:00"
    prev_month_day = (today.replace(day=1) - timedelta(days=1)).isoformat()
    stamp_prev = f"{prev_month_day} 12:00:00"
    for model, when in (("dim-A", stamp_today), ("dim-A", stamp_yesterday), ("dim-B", stamp_prev)):
        _insert(conn, model=model, task="ask", when=when)
    conn.commit()

    stats = ai_usage.summary(conn)

    hour_map = {item["hour"]: item for item in stats["by_hour"]}
    assert len(hour_map) == 24, "24 个小时都要在（没记录的小时补 0）"
    assert hour_map["09"]["calls"] >= 1
    assert hour_map["21"]["calls"] >= 1
    assert hour_map["09"]["tokens"] >= 15

    weekday_labels = [item["label"] for item in stats["by_weekday"]]
    assert weekday_labels == ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    assert sum(item["calls"] for item in stats["by_weekday"]) >= 2

    months = [item["month"] for item in stats["by_month"]]
    assert len(months) == 6 and months[-1] == today.strftime("%Y-%m"), months
    prev_key = prev_month_day[:7]
    assert months[-2] == prev_key
    assert next(item for item in stats["by_month"] if item["month"] == prev_key)["calls"] >= 1


def test_summary_model_task_cross_tab(usage_db):
    from app.services import ai_usage

    conn = usage_db
    _insert(conn, model="cross-A", task="agent", when=f"{date.today().isoformat()} 10:00:00")
    _insert(conn, model="cross-A", task="summary", when=f"{date.today().isoformat()} 11:00:00")
    conn.commit()

    stats = ai_usage.summary(conn)
    pairs = {(item["model"], item["task"]) for item in stats["by_model_task"]}
    assert ("cross-A", "agent") in pairs
    assert ("cross-A", "summary") in pairs


# ---------------------------------------------------------------------------
# 质量：耗时 / 失败率 / 规模
# ---------------------------------------------------------------------------
def test_quality_metrics_count_failures_and_latency(usage_db):
    from app.services import ai_usage

    conn = usage_db
    stamp = f"{date.today().isoformat()} 08:00:00"
    _insert(conn, model="q-A", task="ask", prompt=100, completion=50,
            when=stamp, latency_ms=2000, ok=1)
    _insert(conn, model="q-A", task="ask", when=stamp, latency_ms=4000, ok=0, error="超时")
    conn.commit()

    quality = ai_usage.summary(conn)["quality"]
    assert quality["failed"] >= 1
    assert 0 < quality["fail_rate"] <= 100
    assert quality["success_rate"] + quality["fail_rate"] == pytest.approx(100.0, abs=0.2)
    assert quality["avg_latency_ms"] > 0
    assert quality["max_latency_ms"] >= quality["avg_latency_ms"]
    assert quality["max_tokens"] >= 60


def test_recent_and_failures_lists(usage_db):
    from app.services import ai_usage

    conn = usage_db
    _insert(conn, model="rec-A", task="agent", when=f"{date.today().isoformat()} 07:00:00",
            latency_ms=1500, ok=1)
    _insert(conn, model="rec-A", task="agent", when=f"{date.today().isoformat()} 07:01:00",
            latency_ms=900, ok=0, error="连不上这个地址：拒绝连接")
    conn.commit()

    stats = ai_usage.summary(conn)
    assert stats["recent"] and len(stats["recent"]) <= 20
    assert all("latency_ms" in row and "ok" in row for row in stats["recent"])
    fails = stats["recent_failures"]
    assert fails and fails[0]["error"], "失败列表要带错误原因"


def test_windows_and_delta(usage_db):
    """今日 / 本周 / 本月 / 上月 四个窗口，以及环比。"""
    from app.services import ai_usage

    conn = usage_db
    today = date.today()
    _insert(conn, model="win-A", task="ask", when=f"{today.isoformat()} 06:00:00")
    prev_month_day = (today.replace(day=1) - timedelta(days=1)).isoformat()
    _insert(conn, model="win-A", task="ask", when=f"{prev_month_day} 06:00:00")
    conn.commit()

    stats = ai_usage.summary(conn)
    assert stats["today"]["calls"] >= 1
    assert stats["week"]["calls"] >= stats["today"]["calls"]
    assert stats["month"]["calls"] >= stats["today"]["calls"]
    assert stats["prev_month"]["calls"] >= 1
    assert stats["all"]["calls"] >= stats["month"]["calls"]
    # 有上期数据，环比就该是个数（而不是 None）
    assert isinstance(stats["calls_delta_pct"], int)


def test_delta_is_none_without_previous_month(usage_db):
    from app.services import ai_usage

    assert ai_usage._delta_pct(0, 0) is None
    assert ai_usage._delta_pct(5, 0) == 100
    assert ai_usage._delta_pct(5, 10) == -50
    assert ai_usage._delta_pct(15, 10) == 50


# ---------------------------------------------------------------------------
# 老库迁移 + 流式记账 + CSV 导出
# ---------------------------------------------------------------------------
def test_ensure_migrates_old_table(tmp_path):
    """老库（没有 latency_ms / ok / error 三列）也要能用。"""
    from app.services import ai_usage

    db_file = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    # 故意用「升级前」的表结构
    conn.executescript(
        "CREATE TABLE ai_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL DEFAULT '',"
        " task TEXT NOT NULL DEFAULT '', prompt_tokens INTEGER NOT NULL DEFAULT 0,"
        " completion_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL);"
    )
    conn.execute(
        "INSERT INTO ai_usage (model, task, prompt_tokens, completion_tokens, total_tokens,"
        " created_at) VALUES ('old', 'ask', 3, 4, 7, '2026-01-01 10:00:00')"
    )
    conn.commit()

    ai_usage.ensure(conn)  # 幂等：补列
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(ai_usage)")}
    assert {"latency_ms", "ok", "error"} <= columns

    # 老记录仍在，新列走默认值；新记录带耗时也能写进去
    old_row = conn.execute("SELECT * FROM ai_usage WHERE model='old'").fetchone()
    assert old_row["total_tokens"] == 7 and old_row["ok"] == 1 and old_row["latency_ms"] == 0
    ai_usage.record(conn, model="new", task="agent", usage={"total_tokens": 9}, latency_ms=500)
    assert conn.execute("SELECT latency_ms FROM ai_usage WHERE model='new'").fetchone()[0] == 500

    stats = ai_usage.summary(conn)  # 迁移后汇总函数照样能跑
    assert stats["all_calls"] == 2
    conn.close()


def test_record_stream_usage_writes_row(usage_db, monkeypatch):
    from app.services import ai, ai_usage

    conn = usage_db
    before = ai_usage.summary(conn)["all_calls"]
    ai.record_stream_usage(
        {"usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}},
        conn=conn, task="ask", model="stream-model", latency_ms=1234, ok=True,
    )
    ai.record_stream_usage({}, conn=conn, task="ask", model="stream-model",
                           latency_ms=250, ok=False, error="流断了")
    conn.commit()
    after = ai_usage.summary(conn)
    assert after["all_calls"] == before + 2
    assert after["recent"][0]["ok"] == 0 and after["recent"][0]["error"] == "流断了"
    assert after["recent"][1]["total_tokens"] == 50
    assert after["recent"][1]["latency_ms"] == 1234


def test_usage_csv_export(auth_client):
    from app import db
    from app.services import ai_usage

    with db.db() as conn:
        ai_usage.ensure(conn)
        _insert(conn, model="csv-model", task="agent", prompt=7, completion=3,
                when=f"{date.today().isoformat()} 05:00:00", latency_ms=800, ok=1)
        conn.commit()

    res = auth_client.get("/settings/ai/usage.csv")
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    assert "attachment" in res.headers["content-disposition"]
    body = res.text
    assert body.startswith("\ufeff"), "带 BOM，Excel 打开不乱码"
    assert "时间,模型,任务,输入token,输出token,合计token,耗时ms,状态,错误" in body
    assert "csv-model" in body
    assert "笔记助手" in body, "任务名应该翻译成中文"
    assert "成功" in body


def test_usage_csv_needs_login(client):
    """新开一个没登录的客户端 —— 会话级 client 可能已经被别的用例登录过了。"""
    from fastapi.testclient import TestClient

    fresh = TestClient(client.app)
    res = fresh.get("/settings/ai/usage.csv", follow_redirects=False)
    assert res.status_code == 303
    assert "/login" in res.headers.get("location", "")
