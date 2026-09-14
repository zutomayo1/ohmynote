"""模板变量：让模板里的 {{date}} 之类占位符在「新建笔记」时自动替换。

刻意保持极简：不做语法、不做嵌套，未知的 {{...}} 原样保留（用户可能真想写花括号）。
"""

from __future__ import annotations

import datetime as _dt
import re

# 变量名 -> 文档说明（模板页的速查表直接用它渲染）
VARIABLE_DOCS: list[tuple[str, str]] = [
    ("{{date}}", "今天日期，如 2026-09-13"),
    ("{{time}}", "当前时间，如 14:30"),
    ("{{datetime}}", "日期 + 时间"),
    ("{{year}}", "年份，如 2026"),
    ("{{month}}", "月份，如 09"),
    ("{{weekday}}", "星期几，如 星期日"),
]

DAILY_NAME = "每日笔记"          # 每日笔记入口使用的模板名（存在才用）

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

_VAR_RE = re.compile(r"\{\{\s*(date|time|datetime|year|month|weekday)\s*\}\}")


def _now(now: _dt.datetime | None) -> _dt.datetime:
    if isinstance(now, _dt.datetime):
        return now
    return _dt.datetime.now()


def _value(name: str, now: _dt.datetime) -> str:
    if name == "date":
        return now.strftime("%Y-%m-%d")
    if name == "time":
        return now.strftime("%H:%M")
    if name == "datetime":
        return now.strftime("%Y-%m-%d %H:%M")
    if name == "year":
        return now.strftime("%Y")
    if name == "month":
        return now.strftime("%m")
    if name == "weekday":
        return _WEEKDAYS[now.weekday()]
    return ""


def default_template_id(conn) -> int | None:
    """默认模板 id（meta 表 template.default），坏值当没有。"""
    from .. import repo  # 延迟导入避免与 repo 的循环依赖

    try:
        raw = str(repo.get_meta_map(conn, "template.").get("default") or "").strip()
        return int(raw) if raw else None
    except (ValueError, TypeError):
        return None


def render_variables(text: str, now: _dt.datetime | None = None) -> str:
    """把模板内容/标题里的 {{变量}} 替换成实际值；未知占位符原样保留。"""
    if not text or "{{" not in text:
        return text or ""
    now = _now(now)

    def replace(match: re.Match[str]) -> str:
        return _value(match.group(1), now)

    return _VAR_RE.sub(replace, text)
