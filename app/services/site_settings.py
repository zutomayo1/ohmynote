"""站点信息配置：站点名 / 副标题 / 描述 / 作者 / 对外地址 / 每页条数 / 回收站天数。

配置来源优先级（和 ``app/services/ai.py`` 完全一致）：

    设置页保存（meta 表，前缀 ``site.``）  →  .env / 环境变量  →  代码默认值

生效方式：``bootstrap()`` 把当前生效值直接写回 ``app.config.settings`` 的同名属性，
所以模板里到处出现的 ``{{ settings.xxx }}`` 与路由里的 ``settings.per_page``
零改动即可生效（不用重启服务）。

对外接口：
    FIELDS / DEFAULTS / META_PREFIX
    bootstrap(conn)     启动时读 meta，覆盖 settings（conn 可为 None，只取 .env/默认）
    current()           当前生效的值
    describe()          给设置页：值 + 每个字段的来源（db/env/default）
    validate(values)    只校验不写库，返回 (cleaned, errors)
    save(conn, values)  校验后写 meta 并立即生效；非法值抛 SiteSettingsError
    reset(conn)         清掉页面配置，回到 .env / 默认值
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any

from ..config import settings

logger = logging.getLogger("inknote.site_settings")

META_PREFIX = "site."

# 全部可配置字段（顺序即页面渲染顺序）
FIELDS = (
    "site_title",
    "site_subtitle",
    "site_description",
    "author",
    "base_url",
    "per_page",
    "trash_days",
    "appearance_palette",
    "appearance_custom",
    "appearance_mode",
    "appearance_radius",
    "appearance_prose_size",
    "appearance_prose_font",
)

# 外观这一组字段：「只恢复外观」按钮清它，站点名 / 简介 / 每页条数等保持不动
APPEARANCE_FIELDS = (
    "appearance_palette",
    "appearance_custom",
    "appearance_mode",
    "appearance_radius",
    "appearance_prose_size",
    "appearance_prose_font",
)

# 代码默认值（没有 .env、也没在页面保存时的兜底）
DEFAULTS: dict[str, Any] = {
    "site_title": "墨痕",
    "site_subtitle": "一个人的笔记与写作",
    "site_description": "这里是我的个人笔记与博客。",
    "author": "我",
    "base_url": "http://127.0.0.1:8000",
    "per_page": 12,
    "trash_days": 30,
    "appearance_palette": "",
    "appearance_custom": "",
    "appearance_mode": "auto",
    "appearance_radius": "md",
    "appearance_prose_size": "md",
    "appearance_prose_font": "serif",
}

# 需要按整数处理的字段，以及页面允许的范围
INT_FIELDS = ("per_page", "trash_days")
LIMITS: dict[str, tuple[int, int]] = {
    "per_page": (5, 200),
    "trash_days": (0, 3650),
}
LABELS = {
    "site_title": "站点名",
    "site_subtitle": "副标题",
    "site_description": "站点描述",
    "author": "作者",
    "base_url": "对外地址",
    "per_page": "每页条数",
    "trash_days": "回收站保留天数",
    "appearance_palette": "外观主题",
    "appearance_custom": "自定义主题色",
    "appearance_mode": "明暗模式",
    "appearance_radius": "圆角",
}

# 字段 → .env 变量名（用来在页面上标「来自 .env」）
ENV_KEYS = {
    "site_title": "INKNOTE_TITLE",
    "site_subtitle": "INKNOTE_SUBTITLE",
    "site_description": "INKNOTE_DESCRIPTION",
    "author": "INKNOTE_AUTHOR",
    "base_url": "INKNOTE_BASE_URL",
    "per_page": "INKNOTE_PER_PAGE",
    "trash_days": "INKNOTE_TRASH_DAYS",
    "appearance_palette": "INKNOTE_APPEARANCE_PALETTE",
    "appearance_custom": "INKNOTE_APPEARANCE_CUSTOM",
    "appearance_mode": "INKNOTE_APPEARANCE_MODE",
    "appearance_radius": "INKNOTE_APPEARANCE_RADIUS",
    "appearance_prose_size": "INKNOTE_APPEARANCE_PROSE_SIZE",
    "appearance_prose_font": "INKNOTE_APPEARANCE_PROSE_FONT",
}

TITLE_MAX = 60

_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")

# 运行期状态：当前生效值、来源（db/env/default）
_values: dict[str, Any] = {}
_sources: dict[str, str] = {}
# 模块导入时从 settings 里抄一份「.env / 默认值」基线，供 reset / 来源判断使用
_base: dict[str, Any] = {}
_base_sources: dict[str, str] = {}


class SiteSettingsError(ValueError):
    """页面提交的站点配置不合法（message 直接展示给用户）。"""


def _env_source(field: str) -> str:
    """这个字段的基线到底来自 .env 还是代码默认值。"""
    raw = os.environ.get(ENV_KEYS[field])
    if raw is None or raw.strip() == "":
        return "default"
    if field in INT_FIELDS:
        try:
            int(raw.strip())
        except (TypeError, ValueError):
            # .env 写了非整数（config 会退回默认值），记一条便于解释「为什么页面显示默认值」
            logger.debug(
                ".env 里的 %s=%r 不是整数，该字段按默认值处理", ENV_KEYS[field], raw
            )
            return "default"  # .env 写了非整数，config 会退回默认值
    return "env"


def _capture_base() -> None:
    for field in FIELDS:
        _base[field] = getattr(settings, field, DEFAULTS[field])
        _base_sources[field] = _env_source(field)


_capture_base()


def _coerce(field: str, raw: Any) -> Any | None:
    """把 meta 里的字符串转成正确类型；非法返回 None，让调用方回退到基线。"""
    if field in INT_FIELDS:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            # 库里存了非法值（或旧版本写坏）：回退基线，不能让它把 bootstrap 搞崩
            logger.debug("站点配置 %s 的值 %r 不是整数，回退到基线值", field, raw)
            return None
        low, high = LIMITS[field]
        if not low <= value <= high:
            logger.debug(
                "站点配置 %s 的值 %r 超出 %s~%s，回退到基线值", field, value, low, high
            )
            return None
        return value
    return str(raw if raw is not None else "").strip()


def _apply() -> None:
    """把生效值写回 settings 对象（模板 / 路由都在读它）。"""
    for field in FIELDS:
        setattr(settings, field, _values[field])


def _ensure() -> None:
    """没 bootstrap 过（比如 main.py 还没接线）时，先用基线初始化。"""
    if not _values:
        bootstrap(None)


def ensure_ready() -> None:
    """公开版：任何「可能在 bootstrap 之前就被执行」的读取点都该先调它。

    实例：没跑 lifespan 就直接渲染页面（``TestClient(create_app())`` 不带 with、
    启动过程中撞上 404/500 错误页）——那时 ``settings`` 上还没有 ``appearance_*``
    这些字段，模板一读就 AttributeError，把 404 变成 500。
    """
    _ensure()


def ensure_ready() -> None:
    """公开版：任何「可能在 bootstrap 之前就被执行」的读取点都该先调它。

    实例：没跑 lifespan 就直接渲染页面（``TestClient(create_app())`` 不带 with、
    启动过程中撞上 404/500 错误页）——那时 ``settings`` 上还没有 ``appearance_*``
    这些字段，模板一读就 AttributeError，把 404 变成 500。
    """
    _ensure()


def bootstrap(conn: sqlite3.Connection | None = None) -> None:
    """启动时调用：先取 .env / 默认值，再用「设置页」保存的值覆盖。"""
    stored: dict[str, str] = {}
    if conn is not None:
        from .. import repo

        stored = repo.get_meta_map(conn, META_PREFIX)

    for field in FIELDS:
        value = _coerce(field, stored[field]) if field in stored else None
        if value is None:
            _values[field] = _base[field]
            _sources[field] = _base_sources[field]
        else:
            _values[field] = value
            _sources[field] = "db"
    _apply()


def current() -> dict:
    """当前生效的完整配置。"""
    _ensure()
    return dict(_values)


def describe() -> dict:
    """给设置页用：字段值平铺 + ``sources`` 来源（db / env / default）+ ``base`` 基线。

    形状对齐 ``services.ai.describe()``：模板里直接 ``site.site_title``、
    ``site.sources.get('site_title')`` 就能拿到值。
    """
    _ensure()
    data: dict[str, Any] = dict(_values)
    data["sources"] = dict(_sources)
    data["base"] = dict(_base)
    return data


def validate(values: dict) -> tuple[dict, list[str]]:
    """校验页面提交的字段，返回 (cleaned, errors)。

    只处理传入的字段（缺失的字段保持原值）；全部合法时 errors 为空。
    """
    cleaned: dict[str, Any] = {}
    errors: list[str] = []

    for field in FIELDS:
        if field not in values:
            continue
        raw = values.get(field)

        if field == "site_title":
            text = str(raw if raw is not None else "").strip()
            if not text:
                errors.append("站点名不能为空")
            elif len(text) > TITLE_MAX:
                errors.append(f"站点名最多 {TITLE_MAX} 个字")
            else:
                cleaned[field] = text
        elif field == "appearance_palette":
            text = str(raw if raw is not None else "").strip()
            if text not in ("", "bamboo", "ocean", "plum", "amber", "slate"):
                errors.append("外观主题不认识")
            else:
                cleaned[field] = text
        elif field == "appearance_custom":
            text = str(raw if raw is not None else "").strip()
            if text and _HEX_RE.fullmatch(text) is None:
                errors.append("自定义主题色要写成 #RRGGBB，例如 #B5533C")
            else:
                cleaned[field] = text
        elif field == "appearance_mode":
            text = str(raw if raw is not None else "").strip()
            if text not in ("auto", "light", "dark"):
                errors.append("明暗模式只能是 auto / light / dark")
            else:
                cleaned[field] = text
        elif field == "appearance_radius":
            text = str(raw if raw is not None else "").strip()
            if text not in ("sm", "md", "lg"):
                errors.append("圆角只能是 sm / md / lg")
            else:
                cleaned[field] = text
        elif field == "appearance_prose_size":
            text = str(raw if raw is not None else "").strip()
            if text not in ("sm", "md", "lg", "xl"):
                errors.append("正文字号只能是 sm / md / lg / xl")
            else:
                cleaned[field] = text
        elif field == "appearance_prose_font":
            text = str(raw if raw is not None else "").strip()
            if text not in ("serif", "sans"):
                errors.append("正文字体只能是 serif / sans")
            else:
                cleaned[field] = text
        elif field in INT_FIELDS:
            text = str(raw).strip() if raw is not None else ""
            try:
                number = int(text)
            except (TypeError, ValueError):
                errors.append(f"{LABELS[field]}必须是整数")
                continue
            low, high = LIMITS[field]
            if not low <= number <= high:
                errors.append(f"{LABELS[field]}要在 {low}~{high} 之间")
                continue
            cleaned[field] = number
        else:
            cleaned[field] = str(raw if raw is not None else "").strip()

    return cleaned, errors


def save(conn: sqlite3.Connection, values: dict) -> dict:
    """写 meta + 立即生效。非法值抛 SiteSettingsError，绝不写坏配置。"""
    cleaned, errors = validate(values)
    if errors:
        raise SiteSettingsError("；".join(errors))

    if cleaned:
        from .. import repo

        repo.save_meta_map(conn, {field: str(value) for field, value in cleaned.items()}, META_PREFIX)

    bootstrap(conn)
    return current()


def reset(conn: sqlite3.Connection, fields: tuple[str, ...] | None = None) -> None:
    """清掉页面保存的站点配置，回到 .env / 默认值。

    ``fields`` 只清指定的一组字段（如 ``APPEARANCE_FIELDS`` = 只恢复外观），
    留空则清全部。未知字段直接报错——宁可不动作，也不要静默清掉别的配置。
    """
    from .. import repo

    targets = FIELDS if fields is None else tuple(fields)
    unknown = [field for field in targets if field not in FIELDS]
    if unknown:
        raise SiteSettingsError("未知的站点配置字段：%s" % "、".join(unknown))
    repo.delete_meta(conn, [f"{META_PREFIX}{field}" for field in targets])
    bootstrap(conn)
