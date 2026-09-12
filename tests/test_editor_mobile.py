"""H 组：手机编辑器工具栏「更多」折叠 —— 模板结构 + 补丁 CSS 断言。

纯 CSS + 隐藏 checkbox 实现（无 JS），所以这里不跑浏览器：
1) 断言渲染出的 HTML 里有 checkbox / label / 次要按钮容器；
2) 用正则切出 .md-tool__rest 段，确认常用工具没有被收进去；
3) 确认原来的 16 个 data-md 工具一个都没丢，upload / toolbar 还在。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDITOR_PATH = "/notes/new"

# 原来工具栏里所有带 data-md 的工具，一个都不能丢
ALL_MD_TOOLS = [
    "bold", "italic", "strike",
    "h1", "h2", "h3",
    "ul", "ol", "task", "quote",
    "code", "codeblock", "link", "wiki", "table", "hr",
]

# 手机上必须留在外面（常用）的工具
PRIMARY_MD_TOOLS = ["bold", "italic", "code", "link"]

# 每个 .md-tool__rest 容器里都直接包一个 .md-tool__group（允许带 --end）
_REST_RE = re.compile(
    r'<span class="md-tool__rest"[^>]*>\s*'
    r'<span class="md-tool__group[^"]*">'
    r"(.*?)"
    r"</span>\s*</span>",
    re.S,
)


def _editor_html(auth_client) -> str:
    response = auth_client.get(EDITOR_PATH)
    assert response.status_code == 200, response.text
    return response.text


def _more_panel_html(html: str) -> str:
    """把所有「更多」容器（.md-tool__rest）的内容拼起来，供「不在里面」断言用。"""
    parts = _REST_RE.findall(html)
    assert parts, "没有找到任何 .md-tool__rest 容器"
    return "\n".join(parts)


def test_more_toggle_markup_exists(auth_client):
    html = _editor_html(auth_client)
    assert 'id="md-more-toggle"' in html
    assert 'class="md-tool md-tool__more"' in html
    assert 'class="md-tool__rest"' in html
    assert "更多" in html


def test_toggle_is_sr_only_and_not_hidden(auth_client):
    html = _editor_html(auth_client)
    match = re.search(r'<input[^>]*id="md-more-toggle"[^>]*>', html)
    assert match, "找不到 md-more-toggle 这个 input"
    tag = match.group(0)
    assert 'class="sr-only md-more__checkbox"' in tag
    # 必须是 .sr-only（可聚焦），不能 hidden / display:none。
    # 注意 base.html 里有别处的 hidden（offline-banner 等），所以只检查这个标签本身。
    assert "hidden" not in tag


def test_primary_tools_stay_out_of_more(auth_client):
    html = _editor_html(auth_client)
    panel = _more_panel_html(html)
    for tool in PRIMARY_MD_TOOLS:
        assert f'data-md="{tool}"' not in panel, f"{tool} 不应被收进「更多」"
    # 图片（upload）也留在外面
    assert 'id="upload-input"' not in panel
    # 反向确认：次要工具确实进了「更多」
    for tool in ("strike", "h1", "ul", "codeblock", "table", "hr"):
        assert f'data-md="{tool}"' in panel, f"{tool} 应被收进「更多」"


def test_all_original_tools_still_present(auth_client):
    html = _editor_html(auth_client)
    for tool in ALL_MD_TOOLS:
        assert f'data-md="{tool}"' in html, f"工具栏少了 data-md={tool}"
    assert 'id="upload-input"' in html
    assert 'id="md-toolbar"' in html


def test_style_sheet_uses_sibling_selector():
    """样式表里有「兄弟选择器展开」这条规则 —— 纯 CSS 生效的关键。

    注意：别去断言 `.scratch/css-patch-*.css` 那种临时补丁文件 —— 它们合并进
    style.css 之后就会被删掉，测试会莫名其妙变红（踩过）。
    """
    css = (ROOT / "app" / "static" / "css" / "style.css").read_text(encoding="utf-8")
    assert "#md-more-toggle:checked ~ .md-tool__rest" in css, "缺少 :checked ~ 兄弟选择器"
    assert ".md-tool__rest" in css
    assert "max-width: 640px" in css
    assert "min-width: 641px" in css