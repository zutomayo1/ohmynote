"""用量图表的数据与标记：悬浮提示要给出页面上没有的信息。

为什么单独一个文件：图表的「好看」没法用单元测试断言，但**数据和标记的契约可以** ——
悬浮提示里的每一行都来自 `data-tip-*`，而这些字段又来自 `ai_usage.summary()`。
这条链路一断（改了 SQL 少列、改了模板属性名），提示就会静默变成空壳，
页面看起来「还是好的」，只有悬停上去才发现没内容。

同样的道理：`title` 必须保留 —— 没有 JS 的环境靠它兜底（渐进增强）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest


@pytest.fixture()
def usage_db(client):
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


def _expected_latency(conn, where: str, *params) -> int:
    """自己按同一口径算一遍平均耗时，再和 summary 比。

    **不能写死数字**：session 级 client 让整个会话共用一个库，
    别的用例（尤其是走真 ai.chat 桩、会记真实耗时的那些）也会往今天写记录，
    平均值随时被摊薄。断言绝对数值会变成「单独跑绿、全量跑红」。
    """
    row = conn.execute(
        f"SELECT COALESCE(AVG(NULLIF(latency_ms, 0)), 0) AS v FROM ai_usage WHERE {where}",
        params,
    ).fetchone()
    return int(round(float(row["v"] or 0)))


def _naive_latency(conn, where: str, *params) -> float:
    """不排除 0 的朴素平均 —— 用来证明「失败那次的 0 没被算进去」。"""
    row = conn.execute(
        f"SELECT COALESCE(AVG(latency_ms), 0) AS v FROM ai_usage WHERE {where}", params
    ).fetchone()
    return float(row["v"] or 0)


DAY = "substr(created_at, 1, 10) = ?"
HOUR = "substr(created_at, 1, 10) = ? AND substr(created_at, 12, 2) = ?"


# ---------------------------------------------------------------------------
# 数据：每个时间维度都要带上「失败数 / 平均耗时」
# ---------------------------------------------------------------------------
def test_by_day_carries_failed_and_latency(usage_db):
    from app.services import ai_usage

    conn = usage_db
    stamp = f"{date.today().isoformat()} 08:20:00"
    _insert(conn, model="chart-day", task="ask", when=stamp, latency_ms=1500)
    _insert(conn, model="chart-day", task="ask", when=stamp, latency_ms=2500)
    _insert(conn, model="chart-day", task="ask", when=stamp, latency_ms=0, ok=0, error="超时")
    conn.commit()

    day = next(item for item in ai_usage.summary(conn)["by_day"] if item["day"] == stamp[:10])
    assert day["calls"] >= 3
    assert day["failed"] >= 1
    assert day["avg_latency"] == _expected_latency(conn, DAY, stamp[:10])
    # 且**不低于**朴素平均 —— 失败那次的 latency 是 0，
    # 不排除的话会把均值拉低，这正是 NULLIF 存在的理由
    assert day["avg_latency"] >= _naive_latency(conn, DAY, stamp[:10])
    assert day["avg_latency"] > 0


def test_hour_weekday_and_month_carry_extras(usage_db):
    from app.services import ai_usage

    conn = usage_db
    today = date.today()
    stamp = f"{today.isoformat()} 07:45:00"
    _insert(conn, model="chart-hour", task="agent", when=stamp, latency_ms=4200)
    _insert(conn, model="chart-hour", task="agent", when=stamp, ok=0, error="挂了")
    conn.commit()

    stats = ai_usage.summary(conn)

    hour = next(item for item in stats["by_hour"] if item["hour"] == "07")
    assert hour["calls"] >= 2
    assert hour["failed"] >= 1
    assert hour["avg_latency"] == _expected_latency(conn, HOUR, stamp[:10], "07")
    assert hour["avg_latency"] > 0
    # 没记录的小时仍然要补 0（而不是缺键，模板会直接读它）
    for item in stats["by_hour"]:
        for key in ("calls", "tokens", "prompt", "completion", "failed", "avg_latency"):
            assert key in item, f"by_hour[{item['hour']}] 缺 {key}"

    weekday_label = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[today.weekday()]
    weekday = next(item for item in stats["by_weekday"] if item["label"] == weekday_label)
    assert weekday["calls"] >= 2 and "failed" in weekday and "avg_latency" in weekday

    month = next(item for item in stats["by_month"] if item["month"] == today.strftime("%Y-%m"))
    assert month["calls"] >= 2 and month["failed"] >= 1


def test_by_model_carries_extras(usage_db):
    from app.services import ai_usage

    conn = usage_db
    _insert(conn, model="chart-model", task="ask", when=f"{date.today().isoformat()} 06:00:00",
            latency_ms=900)
    conn.commit()
    model = next(m for m in ai_usage.summary(conn)["by_model"] if m["model"] == "chart-model")
    assert model["avg_latency"] >= 900 and "failed" in model


# ---------------------------------------------------------------------------
# 30 天柱状图的补零与高度计算
# ---------------------------------------------------------------------------
def test_usage_chart_fills_gaps_and_labels_weekday():
    from app.routers.ai_admin import _usage_chart
    from app.utils import now

    today = now().date()
    day = (today - timedelta(days=3)).isoformat()
    series = _usage_chart([
        {"day": day, "calls": 4, "tokens": 1000, "prompt": 750, "completion": 250,
         "failed": 2, "avg_latency": 1200},
    ], days=30)

    assert len(series) == 30
    assert series[-1]["day"] == today.isoformat() and series[-1]["label"] == today.isoformat()[5:]
    assert [item["empty"] for item in series].count(True) == 29

    filled = next(item for item in series if item["day"] == day)
    assert filled["empty"] is False
    assert filled["percent"] == 100                      # 唯一有数据的一天就是峰值
    assert filled["prompt_pct"] == 75
    assert filled["failed"] == 2 and filled["avg_latency"] == 1200
    # 星期几要跟着日期走（提示的标题里要显示）
    assert filled["weekday"] == ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[
        (today - timedelta(days=3)).weekday()]

    # 只有峰值那根带常驻数值标注（30 天里相近的数字挤在一起反而看不清）
    assert sum(1 for item in series if item["is_peak"]) == 1
    assert filled["is_peak"] is True

    # 空的那天不能给 6% 的假高度
    blank = next(item for item in series if item["empty"])
    assert blank["percent"] == 0 and blank["prompt_pct"] == 0 and blank["is_peak"] is False


def test_usage_chart_marks_one_peak_even_when_tied():
    """数值打平时也只标一根 —— 否则「峰值」标注会把整张图铺满。"""
    from app.routers.ai_admin import _usage_chart
    from app.utils import now

    today = now().date()
    tied = [{"day": (today - timedelta(days=offset)).isoformat(), "calls": 1, "tokens": 500,
             "prompt": 400, "completion": 100} for offset in range(4)]
    series = _usage_chart(tied, days=4)
    assert sum(1 for item in series if item["is_peak"]) == 1


def test_usage_chart_handles_zero_tokens():
    """有调用但 token 全为 0：不能除零，也不该崩。"""
    from app.routers.ai_admin import _usage_chart
    from app.utils import now

    series = _usage_chart([{"day": now().date().isoformat(), "calls": 2, "tokens": 0}], days=3)
    today_bar = series[-1]
    assert today_bar["percent"] == 6 and today_bar["prompt_pct"] == 0
    assert today_bar["empty"] is False


# ---------------------------------------------------------------------------
# 标记：悬浮提示的数据属性 + 原生 title 兜底
# ---------------------------------------------------------------------------
def test_settings_charts_expose_tip_data_and_fall_back_to_title(client):
    from conftest import login

    login(client)
    html = client.get("/settings").text

    assert html.count("data-tip-nav") == 2, "柱状图与小时图要能键盘逐柱浏览"
    for attr in ("data-tip-title=", "data-tip-calls=", "data-tip-tokens=",
                 "data-tip-prompt=", "data-tip-completion=", "data-tip-failed=",
                 "data-tip-latency="):
        assert attr in html, f"缺 {attr}（悬浮提示会变空壳）"

    # 30 天 + 24 小时 + 7 星期 + 6 个月 = 67 个可悬停目标（模型条看有没有数据）
    assert html.count("data-tip-title=") >= 67

    # 无 JS 兜底：原生 title 仍在
    assert 'title="' in html
    assert "柱高按 token 计" in html and "方向键" in html


def test_chart_tip_script_is_loaded_on_settings(client):
    from conftest import login

    login(client)
    html = client.get("/settings").text
    assert "/static/js/chart-tip.js" in html


def test_chart_tip_css_covers_referenced_colors():
    """提示里的色条要和柱子同色，靠的就是这几个类名，别写错。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    css = (root / "app/static/css/style.css").read_text(encoding="utf-8")
    for name in (".chart-tip__seg--prompt", ".chart-tip__seg--completion"):
        assert name in css, f"{name} 缺样式"
    # 与柱子的两段共用同一套色（都是 brand 的混色）
    assert ".ai-chart__seg--prompt" in css and ".ai-chart__seg--completion" in css


# ---------------------------------------------------------------------------
# 悬浮面板统一（chart-tip 全站化）
# ---------------------------------------------------------------------------
def test_chart_tip_is_loaded_once_globally():
    """chart-tip.js 挂在 base.html 全站加载；任何模板都不许再局部引第二份
    （两份脚本会各建一个浮层，悬停时出双份提示）。"""
    from pathlib import Path

    templates = Path(__file__).resolve().parent.parent / "app/templates"
    hits = []
    for path in templates.rglob("*.html"):
        content = path.read_text(encoding="utf-8")
        n = content.count("static/js/chart-tip.js")
        if n:
            hits.append((path.name, n))
    assert hits == [("base.html", 1)], hits


def test_heatmap_cells_carry_tip_attrs(auth_client):
    """/stats 的热力图格子要带 data-tip（title 同时保留作无 JS 兜底）。"""
    page = auth_client.get("/stats")
    assert page.status_code == 200
    assert 'data-tip-title="' in page.text
    assert "data-tip-lines=" in page.text
    assert 'title="' in page.text            # 无 JS 兜底仍在


def test_usage_failure_badge_carry_tip_note(auth_client):
    """设置页明细表的「失败」标记走自绘浮层（错误原因进备注块）。"""
    page = auth_client.get("/settings")
    assert page.status_code == 200
    assert 'data-tip-title="失败原因"' in page.text
    assert 'data-tip-note="' in page.text
