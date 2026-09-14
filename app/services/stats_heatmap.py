"""写作热力图：按周分组的年度贡献格（GitHub 风格，周一起始）。

纯数据计算，不查库（counts 由调用方传入 repo.daily_note_counts 的结果）。
"""

import datetime as _dt

WEEKS = 53          # 覆盖 53 列（周）
LEVEL_CAPS = (1, 3, 6)  # 分档：0 / 1 / 2-3 / 4-6 / 7+


def _level(count: int) -> int:
    if count <= 0:
        return 0
    if count <= LEVEL_CAPS[0]:
        return 1
    if count <= LEVEL_CAPS[1]:
        return 2
    if count <= LEVEL_CAPS[2]:
        return 3
    return 4


def build_heatmap(counts: dict[str, int], *, today: _dt.date | None = None) -> list[list[dict | None]]:
    """生成 53×7 的周结构（周一起始，今天为最后一格）。

    每格 {"date": "YYYY-MM-DD", "count": n, "level": 0..4}；
    今天之后的格子为 None（渲染为透明占位，保持网格对齐）。
    """
    today = today or _dt.date.today()
    monday = today - _dt.timedelta(days=today.weekday())  # 本周一
    start = monday - _dt.timedelta(weeks=WEEKS - 1)

    weeks: list[list[dict | None]] = []
    day = start
    for _week in range(WEEKS):
        column: list[dict | None] = []
        for _dow in range(7):
            if day > today:
                column.append(None)
            else:
                count = int(counts.get(day.isoformat(), 0))
                column.append({"date": day.isoformat(), "count": count, "level": _level(count)})
            day += _dt.timedelta(days=1)
        weeks.append(column)
    return weeks
