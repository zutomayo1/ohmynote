# -*- coding: utf-8 -*-
"""使用说明.md 的守卫：正文目录不能是死链、标题锚点必须可读且稳定。

背景：文档正文里手写了一份「目录」，链接写成 `#一怎么打开` 这种形式。
渲染器原先用默认 slug，中文标题会退化成位置编号 `_1`/`_2`（加个标题就全错位），
于是那份目录一直是死链、而且没人发现。改成 slugify_unicode 后锚点变成
「文字本身」，链接才对得上。这几条守卫把它钉住。
"""

import re
from pathlib import Path

from app.config import BASE_DIR
from app.markdown_render import render

MANUAL = BASE_DIR / "使用说明.md"


def _rendered():
    text = MANUAL.read_text(encoding="utf-8")
    return text, render(text, title=None)


def test_manual_exists_and_renders():
    text, out = _rendered()
    assert MANUAL.is_file() and text.strip()
    assert out.html and out.toc
    # 代码围栏必须配对，否则后半篇会被吞进代码块
    assert len(re.findall(r"^```", text, re.M)) % 2 == 0


def test_body_toc_links_all_resolve():
    """正文里手写的每一处 `[文字](#锚点)` 都要能对上真实标题 id。"""
    text, out = _rendered()
    ids = {item["id"] for item in out.toc}
    links = re.findall(r"^\s*\d+\.\s*\[[^\]]+\]\(#([^)]+)\)", text, re.M)
    assert links, "没找到正文目录（预期是 `1. [xx](#yy)` 形式的列表）"
    missing = sorted({a for a in links if a not in ids})
    assert not missing, f"这些目录锚点对不上任何标题：{missing}"


def test_heading_anchors_are_readable_not_positional():
    """锚点要用标题文字（`一怎么打开`），不能是 `_1`/`_2` 这种位置编号。"""
    _, out = _rendered()
    positional = [i["id"] for i in out.toc if i["id"].startswith("_")]
    assert not positional, f"标题 id 退化成了位置编号：{positional[:5]}"


def test_manual_toc_covers_every_section():
    """正文目录要覆盖全部二级标题（加了新章节记得同步目录）。"""
    text, _rendered_out = _rendered()
    h2 = re.findall(r"^##\s+(?!#)(.+)$", text, re.M)
    # 跳过「目录」自身
    sections = [s.strip() for s in h2 if s.strip() != "目录"]
    listed = re.findall(r"^\s*\d+\.\s*\[([^\]]+)\]\(#", text, re.M)
    assert len(listed) == len(sections), (
        f"目录 {len(listed)} 条，章节 {len(sections)} 个："
        f"目录={listed}\n章节={sections}"
    )
