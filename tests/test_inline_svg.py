# -*- coding: utf-8 -*-
"""内联 SVG 渲染：代码块外的 <svg> 清洗后放行；其余裸 HTML 仍转义。"""
from app.markdown_render import render

CLEAN_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="40">\n'
    '  <rect x="5" y="5" width="90" height="30" rx="6" fill="#B5533C"/>\n'
    '  <text x="50" y="26" text-anchor="middle" fill="#fff" font-size="14">DB</text>\n'
    "</svg>"
)


def test_clean_svg_passes_through():
    html = render("前置说明\n\n" + CLEAN_SVG + "\n\n后置说明").html
    assert "<svg" in html and "<rect" in html, html[:300]
    assert "{{svg" not in html, "占位符没有被恢复"


def test_dangerous_parts_are_stripped():
    dirty = (
        CLEAN_SVG.replace("<rect", '<rect onclick="alert(1)"')
        + "\n<script>alert('xss')</script>\n"
        + '<a href="javascript:alert(2)">x</a>'
        + "\n<iframe src=\"https://evil.example\"></iframe>"
    )
    html = render(dirty).html
    assert "<script" not in html.lower()
    assert "onclick" not in html.lower()
    assert 'href="javascript:' not in html.lower()
    assert "<iframe" not in html.lower()
    assert "<rect" in html, "图形本体不能被误伤"


def test_svg_inside_code_fence_stays_code():
    html = render("```\n<svg><rect/></svg>\n```").html
    assert "<rect" not in html
    assert "&lt;svg&gt;" in html


def test_multiple_svgs_and_surrounding_text():
    html = render("前\n\n" + CLEAN_SVG + "\n\n中\n\n" + CLEAN_SVG + "\n\n后").html
    # 按查看容器计数（按钮里的图标也是 <svg>，不能按标签数）
    assert html.count('<figure class="svg-view">') == 2
    assert "前" in html and "中" in html and "后" in html


def test_multiline_svg_keeps_opening_tag():
    """开标签与内容分行：曾因闭合时覆盖 blocks 丢过开标签（只剩 rect 没有 <svg>）。"""
    html = render(CLEAN_SVG).html
    assert '<svg xmlns="http://www.w3.org/2000/svg"' in html
