"""通用小工具：时间格式、slug、标签解析、分页、跳转提示等。"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode

ISO_FMT = "%Y-%m-%d %H:%M:%S"


def tag_color(name: str) -> int:
    """标签名 → 0..7 的稳定色组编号（md5，跨进程稳定）。

    全项目唯一实现：模板过滤器 ``tag_color``、图谱节点配色都用它，
    这样「图谱里某个簇的颜色」和「标签药丸的颜色」是同一个色组。
    """
    import hashlib

    digest = hashlib.md5((name or "").encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 8

logger = logging.getLogger("inknote.utils")

# 中日韩字符（用于字数统计与摘要长度判断）
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")


# ---------------------------------------------------------------------------
# 真值解析（全项目唯一的口径）
# ---------------------------------------------------------------------------
# 以前表单 / 配置 / 导入包里各写了一套，口径还不一样，其中
# ``bool(表单字符串)`` 恒真是真的把「取消勾选」当成勾选、把笔记误公开过。
TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
FALSY_VALUES = frozenset({"0", "false", "no", "off", ""})


def as_bool(value: object, *, default: bool = False) -> bool:
    """把五花八门的真值写法统一成 bool。

    - 真正的 ``bool`` / 数字按 Python 语义走（``0`` 为假，其余为真）；
    - 字符串忽略大小写与首尾空白：``1/true/yes/on`` 为真，``0/false/no/off/空`` 为假；
    - ``None`` 与无法识别的字符串返回 ``default``。

    想沿用别的默认值时显式传 ``default=``（例如环境变量未设置时按 True 处理）。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in TRUTHY_VALUES:
        return True
    if text in FALSY_VALUES:
        return False
    return default


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------
def now() -> datetime:
    return datetime.now()


def now_iso() -> str:
    return now().strftime(ISO_FMT)


def parse_dt(value: str | None) -> datetime | None:
    """把数据库里的 'YYYY-MM-DD HH:MM:SS' 解析成 datetime，失败返回 None。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # fromisoformat 认不出的格式再逐个试，属正常的多格式解析流程
        logger.debug("utils：fromisoformat 解析失败，尝试其它格式（value=%r）", value, exc_info=True)
    for fmt in (ISO_FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            # 该格式不匹配，继续试下一个（同一解析流程的一部分）
            continue
    # 所有格式都解析不了：数据有问题值得 warning，但只影响展示，不往上抛
    logger.warning("utils：日期时间解析失败，返回 None（value=%r）", value)
    return None


def fmt_date_cn(value: str | None) -> str:
    """跨平台安全的日期格式（Windows 的 strftime 不支持 %-m）。"""
    dt = parse_dt(value)
    if not dt:
        return ""
    return f"{dt.year}年{dt.month}月{dt.day}日"


def fmt_datetime_cn(value: str | None, with_year: bool = True) -> str:
    dt = parse_dt(value)
    if not dt:
        return ""
    stamp = f"{dt.month}月{dt.day}日 {dt.hour:02d}:{dt.minute:02d}"
    return f"{dt.year}年{stamp}" if with_year else stamp


def rel_time(value: str | None) -> str:
    """列表页用的相对时间。"""
    dt = parse_dt(value)
    if not dt:
        return ""
    delta = now() - dt
    seconds = delta.total_seconds()
    if seconds < 0:
        return fmt_datetime_cn(value)
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400 and dt.date() == date.today():
        return f"{int(seconds // 3600)} 小时前"
    if dt.date() == date.today() - timedelta(days=1):
        return f"昨天 {dt.hour:02d}:{dt.minute:02d}"
    if dt.date() == date.today() - timedelta(days=2):
        return "前天"
    if dt.year == now().year:
        return f"{dt.month}月{dt.day}日"
    return f"{dt.year}年{dt.month}月{dt.day}日"


def month_key(value: str | None) -> str:
    dt = parse_dt(value)
    return f"{dt.year:04d}-{dt.month:02d}" if dt else ""


def month_label(key: str) -> str:
    try:
        year, month = key.split("-")
        return f"{int(year)}年{int(month)}月"
    except (ValueError, AttributeError):
        # key 正常是 repo 生成的 'YYYY-MM'；异常值只影响展示，留痕但不打断页面
        logger.warning("utils：月份标签解析失败（key=%r）", key, exc_info=True)
        return key or ""


# ---------------------------------------------------------------------------
# slug
# ---------------------------------------------------------------------------
_SLUG_DROP = re.compile(r"[^0-9a-z\u4e00-\u9fff\u3400-\u4dbf\-]+", re.IGNORECASE)


def slugify(text: str, *, max_length: int = 80) -> str:
    """把标题变成 URL 片段：保留中英文与数字，其余丢弃。

    中文标题会得到 `读书笔记` 这样的可读 slug；若整个标题没有一个可用字符，
    由调用方兜底成 `note-<id>`。
    """
    text = (text or "").strip().lower()
    text = re.sub(r"[\s_·、，,。.：:；;/\\|]+", "-", text)
    text = _SLUG_DROP.sub("", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:max_length].strip("-")


_SLUG_SEP_RE = re.compile(r"[\s/\\?&=#+:;|,，、（）()\[\]【】]+")
_SLUG_SAFE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff\u3400-\u4dbf\-_~]+")


def sanitize_slug(text: str, *, max_length: int = 120) -> str:
    """清理用户手填的 slug：分隔符换成连字符，其余危险字符直接丢掉。

    保留中文（`/blog/读书笔记` 是可读的），但会把 `.`、`/`、`?` 之类挡掉，
    因此不可能通过 slug 跳出路径。
    """
    text = (text or "").strip().strip("/").lower()
    text = _SLUG_SEP_RE.sub("-", text)
    text = _SLUG_SAFE.sub("", text)
    text = re.sub(r"-{2,}", "-", text)
    return text[:max_length].strip("-.")


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------
TAG_SPLIT_RE = re.compile(r"[,，、;；\n\r\t|]+|\s+")
_INLINE_TAG_RE = re.compile(
    r"(?<![\w#\-])#([A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff]{0,39})"
)
MAX_TAGS = 12
MAX_TAG_LEN = 40


def normalize_tag(name: str) -> str:
    name = (name or "").strip().lstrip("#").strip()
    name = re.sub(r"\s+", " ", name)
    return name[:MAX_TAG_LEN]


def parse_tags(raw: str | list[str] | None) -> list[str]:
    """把 'a, b、c  d' 之类的输入切成去重后的标签列表（保持原顺序）。"""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else TAG_SPLIT_RE.split(str(raw))
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = normalize_tag(str(item))
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
        if len(result) >= MAX_TAGS:
            break
    return result


def extract_inline_tags(markdown_text: str) -> list[str]:
    """从正文里收集 `#标签` 写法（代码块与行内代码里的不会被收集）。"""
    from .markdown_render import map_outside_code  # 延迟导入，避免循环依赖

    found: list[str] = []

    def collect(chunk: str) -> str:
        for match in _INLINE_TAG_RE.finditer(chunk):
            found.append(match.group(1))
        return chunk

    map_outside_code(markdown_text or "", collect)
    return found


# ---------------------------------------------------------------------------
# 文本
# ---------------------------------------------------------------------------
def truncate(text: str, length: int = 100, suffix: str = "…") -> str:
    text = (text or "").strip()
    if len(text) <= length:
        return text
    return text[:length].rstrip() + suffix


def escape_like(value: str) -> str:
    """转义 LIKE 通配符，配合 ESCAPE '\\\\' 使用。"""
    return (value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def human_size(num_bytes: int) -> str:
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_number(value: int | float | None) -> str:
    try:
        return f"{int(value or 0):,}"
    except (TypeError, ValueError):
        # 纯展示用的兜底：数字算不出来就显示 0，不打断模板渲染
        logger.debug("utils：数字格式化失败，显示 0（value=%r）", value, exc_info=True)
        return "0"


def safe_next(url: str | None, default: str = "/notes") -> str:
    """只允许跳回站内路径，避免开放重定向。"""
    if not url:
        return default
    url = url.strip()
    if not url.startswith("/") or url.startswith("//") or "\\" in url:
        return default
    return url.replace("\r", "").replace("\n", "")


def build_query(params: dict) -> str:
    clean = {key: value for key, value in params.items() if value not in (None, "", False)}
    return urlencode(clean)


def url_with_query(url: str, **params) -> str:
    """在 URL 上合并查询参数，值为 None / 空串 / False 的参数会被去掉。"""
    return url_with_params(url, params)


def url_with_params(url: str, params: dict) -> str:
    """同 url_with_query，但接受任意（可能含非法标识符字符的）键名。"""
    query = build_query(params)
    if not query:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"


def flash_url(url: str, message: str, kind: str = "ok") -> str:
    return url_with_query(url, msg=message, kind=kind)


def page_window(current: int, total_pages: int, width: int = 2) -> list[int | None]:
    """生成分页条目的页码序列，None 表示省略号。"""
    if total_pages <= 0:
        return []
    current = max(1, min(current, total_pages))
    pages: set[int] = {1, total_pages, current}
    for offset in range(1, width + 1):
        pages.add(current - offset)
        pages.add(current + offset)
    ordered = sorted(page for page in pages if 1 <= page <= total_pages)
    result: list[int | None] = []
    previous = 0
    for page in ordered:
        if previous and page - previous > 1:
            result.append(None)
        result.append(page)
        previous = page
    return result


def total_pages(total_items: int, per_page: int) -> int:
    if per_page <= 0:
        return 1
    return max(1, math.ceil(total_items / per_page))


def quote_path(value: str) -> str:
    return quote(value or "", safe="/@:+-._~")


def line_diff(old_text: str, new_text: str, context: int = 2) -> list[tuple[str, str]]:
    """生成逐行差异，返回 [(kind, text)]，kind ∈ add / del / same / meta。

    add = 新版本里新增的行，del = 只存在于旧版本的行。
    """
    import difflib

    old_lines = (old_text or "").splitlines()
    new_lines = (new_text or "").splitlines()
    result: list[tuple[str, str]] = []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=context):
        if line.startswith("+++") or line.startswith("---"):
            result.append(("meta", line))
        elif line.startswith("@@"):
            result.append(("meta", line))
        elif line.startswith("+"):
            result.append(("add", line[1:]))
        elif line.startswith("-"):
            result.append(("del", line[1:]))
        else:
            result.append(("same", line[1:] if line.startswith(" ") else line))
    return result
