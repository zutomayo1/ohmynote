"""工具函数与安全组件：slug、标签解析、分页、时间、diff、密码与会话。"""

from __future__ import annotations

import time

import pytest

from app.security import (
    LoginThrottle,
    hash_password,
    make_session,
    read_session,
    verify_password,
)
from app.utils import (
    as_bool,
    escape_like,
    extract_inline_tags,
    fmt_datetime_cn,
    format_number,
    human_size,
    line_diff,
    normalize_tag,
    page_window,
    parse_tags,
    rel_time,
    safe_next,
    sanitize_slug,
    slugify,
    total_pages,
    truncate,
    url_with_params,
    url_with_query,
)


# ---------------------------------------------------------------------------
# slug
# ---------------------------------------------------------------------------
def test_slugify_keeps_cjk_and_latin():
    assert slugify("Python 学习笔记") == "python-学习笔记"
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("多  空格   合并") == "多-空格-合并"
    assert slugify("!!!") == ""
    assert len(slugify("a" * 200)) <= 80


def test_sanitize_slug_strips_dangerous_chars():
    assert sanitize_slug("my/post name?x=1") == "my-post-name-x-1"
    assert sanitize_slug("读书 笔记") == "读书-笔记"
    assert sanitize_slug("/../etc/passwd") == "etc-passwd"
    assert sanitize_slug("A.B..C") == "abc"
    assert sanitize_slug("  ") == ""


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------
def test_parse_tags_splits_and_dedupes():
    assert parse_tags("a, b、c d") == ["a", "b", "c", "d"]
    # 标签去重不区分大小写，先出现的写法胜出
    assert parse_tags("#python, python, PYTHON") == ["python"]
    assert parse_tags("笔记, 笔记") == ["笔记"]
    assert parse_tags("") == []
    assert parse_tags(None) == []
    assert len(parse_tags([f"t{i}" for i in range(30)])) == 12


def test_normalize_tag_removes_hash_and_limits():
    assert normalize_tag("  #标签  ") == "标签"
    assert len(normalize_tag("x" * 100)) == 40


def test_extract_inline_tags():
    found = extract_inline_tags("正文 #异步 和 #Python，#中英mixed 都在，C# 不算。")
    assert "异步" in found
    assert "Python" in found
    assert "中英mixed" in found
    assert "不算" not in found


def test_extract_inline_tags_skips_code_and_headings():
    assert extract_inline_tags("```\n#不是标签\n```") == []
    assert extract_inline_tags("`#不是标签`") == []
    assert extract_inline_tags("# 标题不是标签") == []


# ---------------------------------------------------------------------------
# 分页 / URL
# ---------------------------------------------------------------------------
def test_page_window():
    assert page_window(1, 1) == [1]
    assert page_window(5, 20) == [1, None, 3, 4, 5, 6, 7, None, 20]
    assert page_window(1, 3) == [1, 2, 3]


def test_total_pages():
    assert total_pages(0, 10) == 1
    assert total_pages(10, 10) == 1
    assert total_pages(11, 10) == 2
    assert total_pages(5, 0) == 1


def test_url_helpers():
    assert url_with_query("/notes", page=2) == "/notes?page=2"
    assert url_with_query("/notes?q=a", page=2) == "/notes?q=a&page=2"
    assert url_with_query("/notes", page=None, q="") == "/notes"
    assert url_with_params("/notes", {"weird-key": "1"}).endswith("weird-key=1")


def test_safe_next_blocks_open_redirect():
    assert safe_next("/notes?page=2") == "/notes?page=2"
    assert safe_next("https://evil.com") == "/notes"
    assert safe_next("//evil.com") == "/notes"
    assert safe_next("") == "/notes"
    assert safe_next(None, "/blog") == "/blog"


def test_escape_like():
    assert escape_like("100%") == "100\\%"
    assert escape_like("a_b") == "a\\_b"
    assert escape_like("c\\d") == "c\\\\d"


# ---------------------------------------------------------------------------
# 文本
# ---------------------------------------------------------------------------
def test_truncate_and_numbers():
    assert truncate("abcdef", 3) == "abc…"
    assert truncate("abc", 5) == "abc"
    assert format_number(1234567) == "1,234,567"
    assert format_number(None) == "0"
    assert human_size(0) == "0 B"
    assert human_size(2048) == "2.0 KB"


def test_rel_time_buckets():
    from app.utils import now

    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    assert rel_time(stamp) == "刚刚"
    assert rel_time(None) == ""
    assert rel_time("2001-02-03 04:05:06") == "2001年2月3日"


def test_datetime_formatting():
    assert fmt_datetime_cn("2026-09-01 08:05:00") == "2026年9月1日 08:05"
    assert fmt_datetime_cn("bad-value") == ""


# ---------------------------------------------------------------------------
# 真值解析（全项目唯一口径）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True), ("true", True), ("TRUE", True), (" on ", True), ("yes", True),
        ("0", False), ("false", False), ("FALSE", False), (" off ", False), ("no", False),
        ("", False), ("   ", False), (None, False),
        (1, True), (0, False), (2, True), (True, True), (False, False),
        ("随便什么", False),
    ],
)
def test_as_bool_covers_every_writing_style(value, expected):
    """回归：以前各处口径不一，``bool("0")`` 恒真把「取消公开」当成公开过。"""
    assert as_bool(value) is expected


def test_as_bool_unknown_falls_back_to_default():
    assert as_bool("看不懂", default=True) is True
    assert as_bool(None, default=True) is True
    # 能识别的值不受 default 影响
    assert as_bool("0", default=True) is False


def test_line_diff():
    diff = line_diff("a\nb\nc", "a\nc\nd")
    kinds = [kind for kind, _ in diff]
    assert "add" in kinds and "del" in kinds
    assert ("del", "b") in diff
    assert ("add", "d") in diff


# ---------------------------------------------------------------------------
# 安全
# ---------------------------------------------------------------------------
def test_password_hash_roundtrip():
    stored = hash_password("s3cret", iterations=1000)
    assert stored.startswith("pbkdf2_sha256$1000$")
    assert verify_password("s3cret", stored) is True
    assert verify_password("wrong", stored) is False
    assert verify_password("", stored) is False
    assert verify_password("x", "garbage") is False
    assert verify_password("x", "md5$1$aa$bb") is False
    # 同一个密码两次哈希结果不同（随机盐）
    assert hash_password("same", iterations=1000) != hash_password("same", iterations=1000)


def test_session_roundtrip_and_tampering():
    secret = "unit-secret-key-abcdefghijklmn"
    token, csrf = make_session(secret, max_age=3600)
    payload = read_session(secret, token)
    assert payload is not None
    assert payload["csrf"] == csrf
    assert payload["sub"] == "owner"

    assert read_session("other-secret-key-abcdefghijkl", token) is None
    assert read_session(secret, token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert read_session(secret, "garbage") is None
    assert read_session(secret, "") is None


def test_session_expiry():
    secret = "unit-secret-key-abcdefghijklmn"
    token, _csrf = make_session(secret, max_age=-1)
    assert read_session(secret, token) is None


def test_login_throttle_blocks_after_limit():
    throttle = LoginThrottle(limit=3, window=60, lockout=60)
    assert throttle.blocked_for() == 0
    throttle.register_failure()
    throttle.register_failure()
    assert throttle.blocked_for() == 0
    remaining = throttle.register_failure()
    assert remaining > 0
    assert throttle.blocked_for() > 0
    throttle.reset()
    assert throttle.blocked_for() == 0


def test_login_throttle_window_expires():
    throttle = LoginThrottle(limit=2, window=1, lockout=1)
    throttle.register_failure()
    throttle.register_failure()
    assert throttle.blocked_for() > 0
    time.sleep(1.1)
    assert throttle.blocked_for() == 0
