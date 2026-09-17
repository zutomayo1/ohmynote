"""提示块（callout）与 Markdown 增强的渲染测试。

语法：`> [!NOTE] 标题`（GitHub / Obsidian 风格，也认中文别名）、
`> [!TIP]- 标题`（折叠）、原生 `!!! note "标题"` / `??? note "标题"`。

覆盖：类型与别名映射、折叠、无标题时的兜底标题、块内多段/列表/代码、
**代码块里的示例不被翻译**、嵌套引用不动、未知类型退回 note、
正文转义（callout 内的裸 HTML 不能变成真标签），以及 abbr 缩写。
"""

from __future__ import annotations

import pytest

from app.markdown_render import CALLOUT_TYPES, render


def html_of(source: str) -> str:
    return render(source).html


# ---------------------------------------------------------------------------
# 1. 基本形态
# ---------------------------------------------------------------------------

def test_github_style_alert_with_title():
    out = html_of("> [!NOTE] 小提示\n> 这是**正文**。\n")
    assert '<div class="admonition note">' in out
    assert '<p class="admonition-title">小提示</p>' in out
    assert "<strong>正文</strong>" in out, "块内 Markdown 要正常渲染"


@pytest.mark.parametrize("raw,expected", [
    ("NOTE", "note"), ("note", "note"), ("TIP", "tip"), ("SUCCESS", "success"),
    ("WARNING", "warning"), ("DANGER", "danger"), ("CAUTION", "danger"),
    ("QUOTE", "quote"),
])
def test_english_types(raw, expected):
    out = html_of(f"> [!{raw}] 标题\n> 内容\n")
    assert f'class="admonition {expected}"' in out


@pytest.mark.parametrize("raw,expected", [
    ("注意", "note"), ("说明", "note"), ("提示", "tip"), ("技巧", "tip"),
    ("成功", "success"), ("警告", "warning"), ("危险", "danger"),
    ("错误", "danger"), ("引用", "quote"),
])
def test_chinese_aliases(raw, expected):
    out = html_of(f"> [!{raw}] 标题\n> 内容\n")
    assert f'class="admonition {expected}"' in out


def test_chinese_alias_without_title_uses_alias_as_title():
    out = html_of("> [!危险]\n> 删了就没了。\n")
    assert '<p class="admonition-title">危险</p>' in out, "没写标题时用中文别名，比 Danger 顺眼"


def test_unknown_type_falls_back_to_note():
    out = html_of("> [!WHATEVER] 随便\n> 内容还在。\n")
    assert 'class="admonition note"' in out
    assert "内容还在" in out, "未知类型不能把内容吃掉"


# ---------------------------------------------------------------------------
# 2. 折叠（pymdownx.details 的 details 形态）
# ---------------------------------------------------------------------------

def test_collapsible_marker():
    collapsed = html_of("> [!TIP]- 点我看更多\n> 折叠里的内容。\n")
    assert '<details class="tip">' in collapsed
    assert "<summary>点我看更多</summary>" in collapsed

    expanded = html_of("> [!TIP]+ 默认展开\n> 内容。\n")
    assert '<div class="admonition tip">' in expanded, "+ 应该是展开型"


def test_native_admonition_syntax_still_works():
    assert 'class="admonition note"' in html_of('!!! note "原生写法"\n    内容。\n')
    assert "<details class=\"note\">" in html_of('??? note "原生折叠"\n    内容。\n')


# ---------------------------------------------------------------------------
# 3. 块内容与边界
# ---------------------------------------------------------------------------

def test_multiline_content_with_list_and_code():
    out = html_of(
        "> [!NOTE] 标题\n"
        "> 第一段。\n"
        ">\n"
        "> - 列表项一\n"
        "> - 列表项二\n"
        ">\n"
        "> ```python\n"
        "> print(1)\n"
        "> ```\n"
    )
    assert "<li>列表项一</li>" in out
    assert 'class="codehilite"' in out, "块内代码块要照常高亮"
    assert out.count('class="admonition note"') == 1


def test_callout_ends_when_blockquote_ends():
    out = html_of("> [!NOTE] 标题\n> 内容\n\n后面的普通段落。\n")
    assert 'class="admonition note"' in out
    assert "<p>后面的普通段落。</p>" in out
    assert "admonition" not in out.split("</div>")[-1], "块外内容不该被卷进提示块"


def test_example_inside_code_fence_is_not_translated():
    out = html_of("```\n> [!NOTE] 这是示例\n```\n")
    assert "admonition" not in out
    assert "[!NOTE]" in out, "示例代码要原样保留"


def test_nested_blockquote_not_touched():
    out = html_of("> 引用里再说一句\n> > [!NOTE] 嵌套的\n")
    assert "admonition" not in out, "嵌套引用里的写法不当提示块处理（保持原样更安全）"


def test_raw_html_inside_callout_is_escaped():
    out = html_of("> [!NOTE] 标题\n> <script>alert(1)</script>\n")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_type_map_has_no_empty_targets():
    """别名表里的目标类型必须都是 CSS 里有样式的类型，否则会渲染成没样式的块。"""
    assert set(CALLOUT_TYPES.values()) <= {"note", "tip", "success", "warning", "danger", "quote"}


# ---------------------------------------------------------------------------
# 4. Markdown 增强：缩写
# ---------------------------------------------------------------------------

def test_abbr_renders():
    out = html_of("*[HTML]: HyperText Markup Language\n\nHTML 是标记语言。\n")
    assert "<abbr" in out and 'title="HyperText Markup Language"' in out
