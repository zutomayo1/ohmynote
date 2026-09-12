"""AI 推荐标签的解析回归测试。

真踩过的坑：模型回空数组 `[]`（它觉得这篇没什么好标的）时，解析器会退回「按标点切原文」，
切出一个名叫 `[]` 的垃圾标签填进标签框 —— 用户看到的就是「标签功能有问题」。
"""

from __future__ import annotations

import pytest

from app.services.ai import _parse_tag_list


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 空结果（这次的 bug）
        ("[]", []),
        ("[ ]", []),
        ("", []),
        ("   ", []),
        ("```json\n[]\n```", []),
        # 正常结果
        ('["缓存","Redis"]', ["缓存", "Redis"]),
        ('```json\n["缓存", "Redis"]\n```', ["缓存", "Redis"]),
        ('["#缓存", "#Redis 实战"]', ["缓存", "Redis 实战"]),
        ("缓存、Redis、TTL", ["缓存", "Redis", "TTL"]),
        ('["缓存"]\n\n说明：这些标签覆盖了…', ["缓存"]),
    ],
)
def test_parse_tag_list(raw, expected):
    assert _parse_tag_list(raw) == expected


def test_parse_tag_list_never_returns_junk():
    """纯标点/括号不能变成标签。"""
    for raw in ('[]', "[[]]", "（）", "...", "---", '[""]', "[null]"):
        for tag in _parse_tag_list(raw):
            assert tag.strip(), f"{raw!r} 解析出了空标签"
            assert any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in tag), \
                f"{raw!r} 解析出了纯标点标签 {tag!r}"


def test_parse_tag_list_dedupes_and_caps():
    many = '["A","a","B","C","D","E","F","G"]'
    tags = _parse_tag_list(many)
    assert tags[0] == "A" and "a" not in tags  # 大小写去重
    assert len(tags) <= 5                       # 最多 5 个
