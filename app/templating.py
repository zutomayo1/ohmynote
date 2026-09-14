"""Jinja2 模板环境：过滤器、全局函数、统一的 render()。

所有页面都必须通过 render() 输出，这样 base 模板需要的变量
（站点信息、csrf token、CSP nonce、静态资源版本号…）永远不会漏。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
import re
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from .config import STATIC_DIR, TEMPLATES_DIR, settings
from .deps import csrf_token, current_session
from .services import ai
from .utils import (
    fmt_date_cn,
    fmt_datetime_cn,
    format_number,
    human_size,
    month_label,
    now,
    page_window,
    parse_dt,
    rel_time,
    total_pages,
    truncate,
    url_with_params,
    url_with_query,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _compute_asset_version() -> str:
    """按静态文件修改时间算一个版本号，用于 ?v= 破缓存。"""
    digest = hashlib.md5()
    try:
        for path in sorted(STATIC_DIR.rglob("*")):
            if path.is_file():
                digest.update(path.name.encode("utf-8"))
                digest.update(str(path.stat().st_mtime_ns).encode("ascii"))
    except OSError:
        pass
    return digest.hexdigest()[:8]


ASSET_VERSION = _compute_asset_version()


# ---------------------------------------------------------------------------
# 过滤器
# ---------------------------------------------------------------------------
def _f_rel(value: str | None) -> str:
    return rel_time(value)


def _f_date(value: str | None) -> str:
    return fmt_date_cn(value)


def _f_datetime(value: str | None, with_year: bool = True) -> str:
    return fmt_datetime_cn(value, with_year)


def _f_number(value: Any) -> str:
    return format_number(value)


def _f_size(value: Any) -> str:
    try:
        return human_size(int(value or 0))
    except (TypeError, ValueError):
        return ""


def _f_truncate(value: str, length: int = 100) -> str:
    return truncate(value or "", length)


def _f_reading(value: Any) -> str:
    try:
        minutes = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    return f"约 {minutes} 分钟" if minutes else "—"


def _f_iso_month(value: str | None) -> str:
    dt = parse_dt(value)
    return f"{dt.year:04d}-{dt.month:02d}" if dt else ""


def purge_countdown(value: str | None, trash_days: int | None = None) -> str:
    """回收站保留倒计时（纯函数，读 settings.trash_days，不查库）。

    判定与 repo.purge_expired_trash 一致：它用
    ``deleted_at < now() - trash_days``（严格小于、精确到秒）决定下次启动时清谁。
    - trash_days <= 0：未开启自动清理
    - 删除时间为空或解析失败：返回空串
    - 已越过截止点：已过期，下次启动时清理
    - 剩余满 1 个整日：明天清理；不足 1 日：今天会被清理
    """
    try:
        days = settings.trash_days if trash_days is None else trash_days
        days = int(days)
        if days <= 0:
            return "不自动清理"
        deleted = parse_dt(value)
        if not deleted:
            return ""
        current = now()
        if deleted < current - timedelta(days=days):
            return "已过期，下次启动时清理"
        elapsed_days = max(0, (current - deleted).days)
        left = days - elapsed_days
        if left >= 2:
            return f"还有 {left} 天"
        if left == 1:
            return "明天清理"
        return "今天会被清理"
    except Exception:  # noqa: BLE001 - 模板过滤器绝不抛异常
        return ""

_MEDIA_REF_RE = re.compile(r"/media/[^\s\"'()<>\[\]{}，。；]+")


def _f_image_count(value: Any) -> int:
    """数正文里引用了多少张图片（同一张只算一次）。

    认 Markdown 图片 ![](/media/x.png)、HTML <img src="/media/x.png">、"裸"链接。
    只统计 /media/ 开头的站内路径；任何异常都返回 0 —— 模板里不能因为数图把整页搞挂。
    """
    try:
        text_value = value if isinstance(value, str) else ""
        if not text_value:
            return 0
        return len({match.group(0) for match in _MEDIA_REF_RE.finditer(text_value)})
    except Exception:  # noqa: BLE001 - 数不出来就当 0，页面照常渲染
        return 0


_TAG_RE = re.compile(r"<[^>]+>")


def _squash(text: str) -> str:
    """去掉 HTML 标签和所有空白，便于比较「同一段话」。"""
    return re.sub(r"\s+", "", _TAG_RE.sub("", text or ""))


def _f_summary_repeats(summary: Any, rendered_html: Any) -> bool:
    """摘要是不是正文开头那几句（自动摘要几乎都是从第一段抓的）。

    这种情况如果两个都渲染，页面上会连着出现两遍同样的话 —— 观感很差。
    比较前先把标签和空白都去掉，所以「标题 + 段落」被标签隔开也能认出来。
    """
    try:
        left = _squash(summary if isinstance(summary, str) else "")
        right = _squash(rendered_html if isinstance(rendered_html, str) else "")
        if len(left) < 12 or not right:
            return False
        return left[:60] in right
    except Exception:  # noqa: BLE001 - 比较失败就当不重复，页面照常渲染
        return False


def _f_highlight(value: str, tokens: list[str] | None = None) -> str:
    """把命中的关键词包进 <mark>（内部已做 HTML 转义，模板里可安全 |safe）。"""
    from .search import highlight

    return highlight(value or "", tokens or [])


def _f_tag_color(name: str) -> int:
    """标签名 → 0..7 的稳定色组编号（md5，跨进程稳定）。"""
    import hashlib

    digest = hashlib.md5((name or "").encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 8



templates.env.filters.update(
    {
        "rel": _f_rel,
        "date_cn": _f_date,
        "datetime_cn": _f_datetime,
        "number": _f_number,
        "filesize": _f_size,
        "truncate_cn": _f_truncate,
        "reading": _f_reading,
        "iso_month": _f_iso_month,
        "purge_countdown": purge_countdown,
        "purge_in": purge_countdown,
        "month_label": month_label,
        "tag_color": _f_tag_color,
        "highlight": _f_highlight,
        "image_count": _f_image_count,
        "summary_repeats": _f_summary_repeats,
    }
)


# ---------------------------------------------------------------------------
# 全局函数
# ---------------------------------------------------------------------------
def page_url(request: Request, page: int, **extra: Any) -> str:
    """保留当前查询参数，只替换 page（分页链接用）。"""
    params = dict(request.query_params)
    params.pop("msg", None)
    params.pop("kind", None)
    params.update({key: value for key, value in extra.items() if value not in (None, "")})
    params["page"] = page
    return url_with_params(request.url.path, params)


def filter_url(request: Request, **extra: Any) -> str:
    """保留当前筛选条件，覆盖/删除若干参数（筛选 chips 用）。"""
    params = dict(request.query_params)
    params.pop("msg", None)
    params.pop("kind", None)
    for key, value in extra.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    params.pop("page", None)
    return url_with_params(request.url.path, params)


def absolute(path: str) -> str:
    if not path:
        return settings.base_url
    if path.startswith(("http://", "https://")):
        return path
    return f"{settings.base_url}{path if path.startswith('/') else '/' + path}"


templates.env.globals.update(
    {
        "page_url": page_url,
        "filter_url": filter_url,
        "absolute": absolute,
        "page_window": page_window,
        "total_pages": total_pages,
        "url_with_query": url_with_query,
        "urlencode": urlencode,
        "current_year": now().year,
    }
)


# ---------------------------------------------------------------------------
# 渲染入口
# ---------------------------------------------------------------------------
def _nav_trash_count() -> int:
    """回收站里有多少篇笔记。

    每个页面都会走 base_context()（包括登录页、公开博客页），所以这里自己开一个
    短连接；**任何异常都吞掉并返回 0**，绝不能因为角标让页面 500。
    """
    try:
        from . import db as db_mod

        with db_mod.db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM notes WHERE deleted_at IS NOT NULL"
            ).fetchone()
        return int(row["c"] or 0) if row else 0
    except Exception:  # noqa: BLE001 - 角标只是锦上添花，任何失败都不该影响页面
        return 0


def base_context(request: Request) -> dict[str, Any]:
    session = current_session(request)
    return {
        "request": request,
        "settings": settings,
        "nonce": getattr(request.state, "nonce", ""),
        "csrf": csrf_token(request),
        "is_authed": bool(session),
        "asset_v": ASSET_VERSION,
        "current_path": request.url.path,
        "query_params": request.query_params,
        "ai_enabled": ai.is_enabled(),
        "nav_trash": _nav_trash_count(),
        "msg": request.query_params.get("msg", ""),
        "msg_kind": request.query_params.get("kind", "ok"),
    }


def render(
    request: Request,
    template_name: str,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    **context: Any,
) -> HTMLResponse:
    values = base_context(request)
    values.update(context)
    return templates.TemplateResponse(
        request, template_name, values, status_code=status_code, headers=headers
    )
