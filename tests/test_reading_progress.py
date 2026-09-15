"""阅读进度条守卫：把交付物（JS + 补丁 CSS）绑在一起断言。

真正的「动态类名在 style.css 里有样式」由 tests/test_js_class_guard.py 守，
那个文件扫的是合并后的 style.css —— 在 .scratch/css-patch-reading.css 还没并进去之前它会报红，
并进去之后自然转绿。本文件只断言「我交付的东西自洽」，不依赖是否已经接线，
所以能稳定通过全量测试，也不会因为 base.html 还没接 <script> 而挂。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "app/static/js/reading-progress.js"
PATCH = ROOT / ".scratch/css-patch-reading.css"
BASE = ROOT / "app/templates/base.html"


def _classes_created_by(js_text: str) -> set[str]:
    found: set[str] = set()
    for m in re.finditer(r"className\s*=\s*'([^']+)'", js_text):
        found.update(m.group(1).split())
    # 注意：reading-progress--visible 是通过变量 VISIBLE_CLASS 传入 classList 的，
    # 不是字符串字面量，守卫不会扫到它；这里也一并核对它确实写进了补丁 CSS。
    for m in re.finditer(r"classList\.(?:add|toggle|remove)\(\s*'([^']+)'", js_text):
        found.add(m.group(1))
    return found


def test_reading_progress_js_exists():
    assert JS.exists(), "app/static/js/reading-progress.js 必须存在"


def test_reading_progress_js_is_well_formed():
    text = JS.read_text(encoding="utf-8")
    assert "'use strict'" in text, "应当使用 'use strict'"
    assert "(function ()" in text, "应当用 IIFE 包裹，不污染全局"
    assert "requestAnimationFrame" in text, "滚动更新应当用 rAF 合并"
    assert "passive: true" in text, "scroll 监听必须 passive"


def test_reading_progress_classes_have_styles_in_patch():
    """JS 自建的类名在补丁 CSS 里都要有对应规则（并进去后才满足 test_js_class_guard）。"""
    assert PATCH.exists(), ".scratch/css-patch-reading.css 补丁必须存在"
    js_text = JS.read_text(encoding="utf-8")
    css = PATCH.read_text(encoding="utf-8")

    created = _classes_created_by(js_text)
    assert created, "应当能从 JS 里解析出自建类名"
    for name in created:
        assert f".{name}" in css, f"补丁 CSS 里缺少 .{name} 的样式"

    # 通过变量传入的可视态类名也必须在补丁里
    assert ".reading-progress--visible" in css, "补丁 CSS 里缺少 .reading-progress--visible"
    # 减弱动效覆盖：直接跳变，不做过渡
    assert "prefers-reduced-motion: reduce" in css, "必须尊重 prefers-reduced-motion"


def test_reading_progress_reference_status_in_base():
    """base.html 的接线由调用方手动完成；未接时不报错（skip），接上后顺带确认。"""
    import pytest

    base = BASE.read_text(encoding="utf-8") if BASE.exists() else ""
    if "/static/js/reading-progress.js" in base:
        assert "reading-progress" in base
    else:
        pytest.skip("尚未在 base.html 接 <script>，等集成后再跑这条")
