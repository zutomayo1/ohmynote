"""Markdown 渲染管线：安全性、双链、目录、字数、摘要。"""

from __future__ import annotations

import pytest

from app.markdown_render import (
    WikiRef,
    count_words,
    extract_wikilinks,
    make_excerpt,
    map_outside_code,
    preview_payload,
    render,
    strip_markdown,
)


def test_html_is_escaped():
    result = render("<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>")
    assert "<script>" not in result.html
    assert "onerror=alert" in result.html  # 作为纯文本出现
    assert "&lt;script&gt;" in result.html
    assert "<img" not in result.html


@pytest.mark.parametrize(
    "source",
    [
        "`<script>alert(1)</script>",
        "``<img src=x onerror=alert(42)>",
        "前置文本 `<script>alert(1)</script>",
        "`````<script>alert(1)</script>",
        "第一行 `<script>a</script>\n\n第二行 ``<img src=x onerror=b>",
        "``<script>a</script>`` 然后 <b>裸标签</b>",
    ],
)
def test_unclosed_backtick_cannot_disable_escaping(source):
    """回归：行内代码以前用「反引号一开一关」的朴素切换，一行里只要有未闭合的反引号，
    后面的裸 HTML 就不再转义（真 XSS）。现在只把「长度配对」的反引号段当代码。"""
    html = render(source).html
    assert "<script" not in html
    assert "onerror" not in html or "<img" not in html


def test_closed_inline_code_still_works():
    html = render("`<script>` 与 **粗体**").html
    assert "<code>&lt;script&gt;</code>" in html
    assert "<strong>粗体</strong>" in html


def test_safety_net_neutralises_dangerous_tags():
    """兜底防线：script / iframe / style 一律不出现在正文里；svg 放行但必须被清洗
    （内联 SVG 支持后，事件属性由 _sanitize_svg_fragment 剥掉，图形本体保留）。"""
    for source in ["<iframe src=x></iframe>", "<style>body{}</style>"]:
        html = render(source).html
        assert "<iframe" not in html
        assert "<style" not in html
    html = render("<svg onload=alert(1)></svg>").html
    assert "onload" not in html, "SVG 的事件属性必须被剥掉"
    assert "<script" not in html


def test_raw_html_with_handler_is_escaped_not_executed():
    """裸 HTML 会整体转义成纯文本 —— 这是更彻底的做法。"""
    html = render('<a href="https://example.com" onclick="alert(1)">x</a>').html
    assert "&lt;a" in html
    assert "<a " not in html


def test_safety_net_strips_event_attributes_and_tags():
    """直接单测兜底防线：危险标签转成文本、事件属性被删掉。"""
    from app.markdown_render import _neutralise_dangerous_tags

    assert _neutralise_dangerous_tags('<img src=x onerror="alert(1)">') == "<img src=x>"
    assert _neutralise_dangerous_tags('<script>alert(1)</script>').startswith("&lt;script")
    assert _neutralise_dangerous_tags('<iframe src=x></iframe>').startswith("&lt;iframe")
    # 合法标签不受影响
    assert _neutralise_dangerous_tags('<a href="https://x.com" target="_blank">y</a>') == (
        '<a href="https://x.com" target="_blank">y</a>'
    )


def test_dangerous_url_scheme_is_neutralised():
    html = render("[点我](javascript:alert(1))").html
    assert "javascript:" not in html
    assert 'href="#"' in html
    assert render("[x](vbscript:msgbox)").html.count("vbscript:") == 0
    # data: 图片也禁止
    assert "data:text/html" not in render("![x](data:text/html;base64,PHNjcmlwdD4=)").html


def test_external_link_gets_target_and_rel():
    html = render("[外链](https://example.com/a)").html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html


def test_image_gets_lazy_loading():
    html = render("![图](/media/a.png)").html
    assert 'loading="lazy"' in html
    assert "/media/a.png" in html


def test_table_wrapped_for_horizontal_scroll():
    html = render("| a | b |\n| - | - |\n| 1 | 2 |").html
    assert '<div class="table-wrap">' in html
    assert "</table></div>" in html


def test_task_list_and_strikethrough_and_mark():
    html = render("- [x] 完成\n- [ ] 未完成\n\n~~删除~~ 和 ==高亮==").html
    assert "task-list-item" in html
    assert "checked" in html
    assert "<del>删除</del>" in html
    assert "<mark>高亮</mark>" in html


def test_code_block_is_highlighted():
    html = render('```python\ndef f():\n    return 1\n```').html
    assert "codehilite" in html
    assert "def" in html
    assert '<span class="k">def</span>' in html


def test_toc_tokens_and_anchor_ids():
    result = render("# 一级\n\n## 二级\n\n### 三级\n\n## 另一个二级")
    levels = [item["level"] for item in result.toc]
    assert levels == [1, 2, 3, 2]
    assert result.toc[1]["text"] == "二级"
    assert f'id="{result.toc[1]["id"]}"' in result.html


def test_leading_h1_matching_title_is_removed():
    result = render("# 我的标题\n\n正文", title="我的标题")
    assert "<h1" not in result.html
    # 标题不同就保留
    assert "<h1" in render("# 别的标题\n\n正文", title="我的标题").html


def test_wikilink_resolution():
    def resolver(title: str):
        if title == "已存在":
            return WikiRef(title=title, alias=title, note_id=3, exists=True, url="/notes/3")
        return None

    result = render("看看 [[已存在]] 和 [[不存在|别名]]", resolver=resolver)
    assert '<a class="wikilink" href="/notes/3">已存在</a>' in result.html
    assert 'class="wikilink wikilink--missing"' in result.html
    assert ">别名</span>" in result.html
    assert [(ref.title, ref.exists) for ref in result.wikilinks] == [("已存在", True), ("不存在", False)]


def test_wikilink_inside_code_is_ignored():
    result = render("`[[行内]]`\n\n```\n[[围栏]]\n```")
    assert result.wikilinks == []
    assert "[[行内]]" in result.html
    assert "[[围栏]]" in result.html


def test_wikilink_inside_emphasis_still_works():
    result = render("**[[粗体里的双链]]**")
    assert result.wikilinks[0].title == "粗体里的双链"
    assert "wikilink--missing" in result.html


def test_extract_wikilinks_skips_code():
    refs = extract_wikilinks("[[A|别名]] 与 `[[B]]`")
    assert [(ref.title, ref.alias) for ref in refs] == [("A", "别名")]


def test_hashtag_is_not_a_wikilink_and_stays_text():
    result = render("#标签 不是双链")
    assert result.wikilinks == []
    assert "#标签" in result.html
    assert "<h1" not in result.html


def test_hash_without_space_is_not_a_heading():
    """`#标题` 按 CommonMark 是普通文本，`# 标题` 才是标题。"""
    assert "<h1" not in render("#标题").html
    assert "<h2" not in render("##标题").html
    assert "<h1" in render("# 标题").html
    assert "<h2" in render("## 标题").html


def test_word_count_counts_cjk_by_char_and_latin_by_word():
    assert count_words("你好世界") == 4
    assert count_words("hello world") == 2
    assert count_words("你好 hello") == 3
    # 代码块不计入正文统计
    assert count_words("正文\n\n```\nprint('ignored')\n```") == 2


def test_reading_minutes_is_at_least_one():
    assert render("一点点内容").reading_minutes == 1
    assert render("").reading_minutes == 0


def test_excerpt_cuts_at_sentence_end():
    text = "。" * 0 + "第一句话。第二句话。第三句话。" * 5
    excerpt = make_excerpt(text, 30)
    assert excerpt.endswith("。")
    assert len(excerpt) <= 31


def test_strip_markdown_removes_syntax():
    plain = strip_markdown("## 标题\n\n**加粗** 和 [链接](https://x.com) 与 ![图](/a.png)")
    assert "#" not in plain
    assert "**" not in plain
    assert "https://x.com" not in plain
    assert "链接" in plain
    assert "图" in plain


def test_map_outside_code():
    text = "a `b` c\n```\nd\n```\ne"
    result = map_outside_code(text, str.upper)
    assert result == "A `b` C\n```\nd\n```\nE"


def test_preview_payload_shape():
    payload = preview_payload("## 标题", title="")
    assert set(payload) == {"html", "toc", "word_count", "reading_minutes", "excerpt", "empty"}
    assert payload["empty"] is False
    assert preview_payload("   ")["empty"] is True


def test_footnote_renders():
    html = render("正文[^1]\n\n[^1]: 注释内容").html
    assert "footnote" in html
