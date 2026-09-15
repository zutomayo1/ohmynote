"""onboarding.js 的静态契约守卫（不依赖浏览器，单独可跑）。

只验证「能静态断言」的东西：
1) JS 文件存在；
2) 不含危险模式（eval / new Function / 字符串 setTimeout / innerHTML 拼用户输入）；
3) JS 里字面量创建的所有类名，在 app/static/css/style.css 中都有对应规则；
4) localStorage 键名符合 `inknote.` 前缀约定（且正是 inknote.onboarded）。

注：引导样式原先写在 `.scratch/css-patch-onboarding.css`，合并进 style.css 之后
就改成断言 style.css —— `.scratch/` 是可随时清理的临时目录，测试不该依赖它。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "app/static/js/onboarding.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")

# onboarding.js 实际用到的全部类名（含 h()/buildPop 里以变量传入的，
# 正则抓不到，所以这里显式列出，确保补丁一个不落）。
# 注：位置（上/下/左/右）由 JS 用内联样式定位，无需单独的修饰类；
# 仅窄屏固定条用 .onboard-pop--fixed。
EXPECTED_CLASSES = [
    "onboard-pop",
    "onboard-pop--fixed",
    "onboard-pop__head", "onboard-pop__title", "onboard-pop__body",
    "onboard-pop__dots", "onboard-dot", "onboard-dot--active",
    "onboard-pop__count", "onboard-pop__actions", "onboard-pop__btn",
    "onboard-pop__btn--skip", "onboard-pop__btn--prev", "onboard-pop__btn--next",
    "onboard-pop__close", "onboard-spot",
]

DANGEROUS = [
    r"\beval\s*\(",
    r"new\s+Function\s*\(",
    r"setTimeout\s*\(\s*['\"]",
    r"setInterval\s*\(\s*['\"]",
    r"\.innerHTML\s*=",
    r"document\.write\s*\(",
]


def test_onboarding_js_exists():
    assert (ROOT / "app/static/js/onboarding.js").is_file()


def test_no_dangerous_patterns_in_onboarding():
    for pat in DANGEROUS:
        assert not re.search(pat, JS), f"onboarding.js 含危险模式：/{pat}/"


def test_iife_and_strict():
    assert re.search(r"\(function\s*\(\)\s*\{\s*'use strict';", JS), "应当为带 'use strict' 的 IIFE"


def test_all_expected_classes_are_styled():
    missing = sorted(c for c in EXPECTED_CLASSES if f".{c}" not in CSS)
    assert not missing, f"这些类名在 style.css 里没有样式：{missing}"


def test_js_literal_classes_are_styled():
    """用和 test_js_class_guard 相同的正则，扫 JS 字面量创建的类名，确认 style.css 里有样式。"""
    found: set[str] = set()
    for m in re.finditer(r"className\s*=\s*'([^']+)'", JS):
        found.update(m.group(1).split())
    for m in re.finditer(r"classList\.(?:add|toggle|remove)\(\s*'([^']+)'", JS):
        found.add(m.group(1))
    # '--bottom' 等是拼接前缀（'onboard-pop--' + place），样式里的完整类名才包含它们
    gap = sorted(n for n in found if f".{n}" not in CSS)
    assert not gap, f"JS 字面量类名在 style.css 里缺少样式：{gap}"


def test_localstorage_key_uses_inknote_prefix():
    keys = re.findall(r"['\"](inknote\.[a-zA-Z0-9._-]+)['\"]", JS)
    assert keys, "onboarding.js 应当用到 inknote. 前缀的 localStorage 键"
    assert "inknote.onboarded" in keys, "应当使用 inknote.onboarded 作为完成标记"
    bad = [k for k in keys if not k.startswith("inknote.")]
    assert not bad, f"出现非 inknote. 前缀的键：{bad}"
