"""公式（KaTeX）与 mermaid 图：渲染管线打标 + 页面按需加载 vendor + 静态资源可用。"""

from __future__ import annotations

import pytest

from app import markdown_render as mr

MATH_CONTENT = "行间公式：\n\n$$E = mc^2$$\n\n行内 \\(a+b\\) 也行。"
MERMAID_CONTENT = "流程图：\n\n```mermaid\ngraph LR\n  A --> B\n```"
PLAIN_CONTENT = "就是一段普通正文。"


# ---------------------------------------------------------------------------
# 渲染管线：has_math / has_mermaid 标记
# ---------------------------------------------------------------------------
def test_render_flags_math_and_mermaid():
    rendered = mr.render(MATH_CONTENT + "\n\n" + MERMAID_CONTENT)
    assert rendered.has_math is True
    assert rendered.has_mermaid is True
    assert '<div class="mermaid">' in rendered.html
    assert "graph LR" in rendered.html  # 原文转义后留给前端渲染
    assert "mermaid-error" not in rendered.html


def test_render_flags_absent_on_plain_note():
    rendered = mr.render(PLAIN_CONTENT)
    assert rendered.has_math is False
    assert rendered.has_mermaid is False
    assert "mermaid" not in rendered.html


def test_single_dollar_does_not_trigger_math():
    """价格里的单 $ 不能触发公式加载，$$ 和 \\(\\) 才算。"""
    assert mr.render("软件收费 $20，折扣 $5。").has_math is False
    assert mr.render("写法是 `$$x$$` 这样子。").has_math is False


def test_mermaid_block_is_not_pygments_highlighted():
    rendered = mr.render(MERMAID_CONTENT)
    assert "codehilite" not in rendered.html  # 围栏被抽走了，不进高亮管线
    assert mr.render("```python\nprint(1)\n```").has_mermaid is False


# ---------------------------------------------------------------------------
# 页面集成：详情页 / 博客页按需加载
# ---------------------------------------------------------------------------
def _create_note(auth_client, csrf, title, content, *, public=False, slug=""):
    data = {
        "_csrf": csrf,
        "title": title,
        "content": content,
        "action": "save",
        "slug": slug,
    }
    if public:
        data["is_public"] = "1"
    response = auth_client.post("/notes", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    # 创建后可能落到 /notes/{id}/edit，统一取详情页地址
    note_id = location.split("/")[2] if location.startswith("/notes/") else location
    return f"/notes/{note_id}"


def test_detail_page_loads_math_vendor(auth_client, csrf):
    url = _create_note(auth_client, csrf, "公式笔记", MATH_CONTENT)
    page = auth_client.get(url)
    assert page.status_code == 200
    assert "/static/vendor/katex/katex.min.js" in page.text
    assert "/static/js/enrich.js" in page.text
    assert "vendor/mermaid" not in page.text  # 没图就不引 mermaid（1.4MB 不白下）


def test_detail_page_loads_mermaid_vendor(auth_client, csrf):
    url = _create_note(auth_client, csrf, "图形笔记", MERMAID_CONTENT)
    page = auth_client.get(url)
    assert page.status_code == 200
    assert "/static/vendor/mermaid/mermaid.min.js" in page.text
    assert 'class="mermaid"' in page.text
    assert "vendor/katex" not in page.text


def test_detail_page_plain_note_loads_neither(auth_client, csrf):
    url = _create_note(auth_client, csrf, "普通笔记", PLAIN_CONTENT)
    page = auth_client.get(url)
    assert page.status_code == 200
    assert "vendor/katex" not in page.text
    assert "vendor/mermaid" not in page.text


def test_blog_post_loads_enrichment(auth_client, csrf):
    _create_note(
        auth_client, csrf, "公开的图形文章", MATH_CONTENT + "\n\n" + MERMAID_CONTENT,
        public=True, slug="enrich-post",
    )
    page = auth_client.get("/blog/enrich-post")
    assert page.status_code == 200
    assert "vendor/katex" in page.text
    assert "vendor/mermaid" in page.text
    assert 'class="mermaid"' in page.text


# ---------------------------------------------------------------------------
# vendor 静态资源确实可达（自托管，不依赖 CDN）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/static/vendor/katex/katex.min.css",
        "/static/vendor/katex/katex.min.js",
        "/static/vendor/katex/auto-render.min.js",
        "/static/vendor/mermaid/mermaid.min.js",
        "/static/js/enrich.js",
    ],
)
def test_vendor_assets_served(auth_client, path):
    response = auth_client.get(path)
    assert response.status_code == 200, path


def test_katex_font_served(auth_client):
    response = auth_client.get("/static/vendor/katex/fonts/KaTeX_AMS-Regular.woff2")
    assert response.status_code == 200
