"""全站 JS 守卫：JS 动态创建的类名必须有样式，自绘下拉的渐进增强契约不能被改坏。

为什么单独一个文件：`scripts/audit_css.py` 只扫服务端渲染出来的 HTML，
`className = '...'` / `classList.add('...')` 这类**完全由 JS 生成的类名它扫不到** ——
类名写错就会静默变成没加样式的裸元素（`.tag-chip__*` 那次就是这么漏的）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "app/static/js"
CSS = (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")

# 已知的「死类」：JS 建了，但样式表里没有对应规则。
# 不在这里动样式是因为那会改到别的页面（问笔记 / 笔记助手）的观感，
# 等确认后再补样式或删类名。**这个列表只许变短，不许加长。**
# 曾在这里的两个遗留死类已补上样式（2026-09-14）：agent-steps__tool、is-typing。
# 白名单保持为空 —— 再扫出没样式的动态类直接报错。
KNOWN_UNSTYLED: set[str] = set()


def _classes_created_by(js_path: Path) -> set[str]:
    source = js_path.read_text(encoding="utf-8")
    found: set[str] = set()
    for match in re.finditer(r"className\s*=\s*'([^']+)'", source):
        found.update(match.group(1).split())
    for match in re.finditer(r"classList\.(?:add|toggle|remove)\(\s*'([^']+)'", source):
        found.add(match.group(1))
    return found


def test_js_files_are_all_covered():
    """新增 JS 文件时别忘了它也被守卫扫到（这里只是防止 glob 悄悄失灵）。"""
    names = {path.name for path in JS_DIR.glob("*.js")}
    assert {"app.js", "select.js", "batch.js", "chart-tip.js"} <= names, names


def test_js_created_classes_are_styled():
    missing: dict[str, list[str]] = {}
    for js_path in sorted(JS_DIR.glob("*.js")):
        created = _classes_created_by(js_path)
        gap = sorted(n for n in created if f".{n}" not in CSS and n not in KNOWN_UNSTYLED)
        if gap:
            missing[js_path.name] = gap
    assert not missing, f"这些 JS 动态类名在 style.css 里没有样式：{missing}"


def test_known_unstyled_list_only_shrinks():
    """白名单里的名字要么已经补上样式（该删了），要么确实还在 JS 里用着。"""
    still_used = set()
    for js_path in JS_DIR.glob("*.js"):
        still_used |= _classes_created_by(js_path)
    stale = sorted(n for n in KNOWN_UNSTYLED if n not in still_used)
    assert not stale, f"白名单里这些名字 JS 已经不用了，删掉它们：{stale}"


def test_select_enhancer_names_and_wiring():
    """select.js 造的那套类名 + base.html 的引入 + 只看视觉的改法。"""
    js = (JS_DIR / "select.js").read_text(encoding="utf-8")
    for name in ("select-combo", "select-combo__trigger", "select-combo__label",
                 "select-combo__chevron", "select-combo__panel", "combo__panel",
                 "combo__item", "select-combo__native"):
        assert name in js, f"select.js 里应该用 {name}"

    base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    assert "/static/js/select.js" in base, "base.html 应该引入 select.js"

    # 只换视觉：原生 select 必须还在流里（不 hidden）当尺寸基准，只是不可点、不进 tab
    assert "pointer-events" in (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")
    assert ".select-combo__native" in CSS
    assert "select.hidden" not in js.replace(" ", ""), "原生 select 不该被 hidden（它要撑尺寸）"


def test_native_selects_still_in_html_for_no_js(client):
    """无 JS 时退回原生下拉：三页的 select 与选项必须照旧渲染出来。

    自绘是纯前端渐进增强，服务端渲染一个字都不该变（这也保证没有 JS 的环境可用）。
    """
    from conftest import login

    login(client)
    for path in ("/notes", "/search?q=x", "/tags"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert '<select class="select"' in page.text, f"{path} 里应该有原生 select（无 JS 兜底）"
        assert "<option" in page.text
