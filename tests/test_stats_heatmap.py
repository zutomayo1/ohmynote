"""写作热力图：数据函数 / 周结构 / 页面渲染。"""

import datetime as dt

from app.services import stats_heatmap


def _test_conn(client):
    """client fixture 保证 schema 已初始化；这里只是提示用途。"""
    return client


def test_daily_note_counts_and_exclude_deleted(client):
    from app import db, repo

    _ = client  # 确保 schema 初始化
    with db.db() as conn:
        # session 库共享：其它用例可能也建了今天的笔记，用相对计数断言
        before = repo.daily_note_counts(conn, days=30).get(dt.date.today().isoformat(), 0)
        repo.create_note(conn, title="甲", content="x", status="saved")
        repo.create_note(conn, title="乙", content="x", status="saved")
        gone = repo.create_note(conn, title="丙", content="x", status="saved")
        repo.soft_delete(conn, gone["id"])

        counts = repo.daily_note_counts(conn, days=30)
        today = dt.date.today().isoformat()
        assert counts.get(today) == before + 2  # 回收站的不算


def test_heatmap_structure_monday_start_and_today_last(client):
    counts = {dt.date.today().isoformat(): 3}
    weeks = stats_heatmap.build_heatmap(counts, today=dt.date.today())

    assert len(weeks) == 53
    assert all(len(w) == 7 for w in weeks)

    # 最后一列是本周：最后一格 = 今天
    last = [c for c in weeks[-1] if c]
    assert last[-1]["date"] == dt.date.today().isoformat()
    assert last[-1]["level"] == 2  # 3 篇 → 档 2

    # 首格是周一起始
    first = weeks[0][0]
    assert first["date"] == (dt.date.today() - dt.timedelta(days=dt.date.today().weekday() + 364)).isoformat()
    assert first["count"] == 0 and first["level"] == 0

    # 未来格为 None：今天之后的格子（本周内今天右边那些）
    weekday = dt.date.today().weekday()
    assert all(c is None for c in weeks[-1][weekday + 1:])


def test_heatmap_levels():
    assert stats_heatmap._level(0) == 0
    assert stats_heatmap._level(1) == 1
    assert stats_heatmap._level(3) == 2
    assert stats_heatmap._level(6) == 3
    assert stats_heatmap._level(7) == 4


def test_stats_page_renders_heatmap(auth_client, csrf):
    from app import db, repo

    with db.db() as conn:
        repo.create_note(conn, title="热力笔记", content="x", status="saved")

    page = auth_client.get("/stats")
    assert page.status_code == 200
    assert "heatmap" in page.text
    assert "hm-cell" in page.text
    assert "写作热力" in page.text
    # 今天那格的 tooltip（session 内其它用例可能也建了笔记，只校验日期与格式）
    assert f'title="{dt.date.today().isoformat()} · ' in page.text
