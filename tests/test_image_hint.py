"""A4 的测试：image_count 过滤器 + 页面上的图片提示。"""

from __future__ import annotations

import re

import pytest

from app.templating import _f_image_count


# ---------------------------------------------------------------------------
# 过滤器单测
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "content,expected",
    [
        ("", 0),
        (None, 0),
        (12345, 0),
        ("没有图片的正文。", 0),
        ("![示意图](/media/2026/09/a.png)", 1),
        ('<img src="/media/2026/09/b.jpg" alt="x">', 1),
        ("裸链接 /media/2026/09/c.gif 也算", 1),
        # 去重：同一张图出现多次只算一张
        ("![a](/media/x.png) 又见 ![](/media/x.png)", 1),
        # 多张
        ("![a](/media/a.png)\n![b](/media/b.png)\n![c](/media/c.png)", 3),
        # 指向外站的图片不算
        ("![外链](https://example.com/x.png)", 0),
        # /media 后面紧跟中文标点时要能正确截断
        ("图：/media/a.png，然后还有一张 /media/b.png。", 2),
        # 混合
        ("![m](/media/m.png)\n<img src=\"/media/n.png\">\n![外](https://x.com/o.png)", 2),
    ],
)
def test_image_count(content, expected):
    assert _f_image_count(content) == expected


def test_image_count_never_raises():
    """任何奇怪输入都不能抛异常（模板里会直接调它）。"""
    for weird in (object(), [], {}, b"bytes", "\x00", "/media/" * 50):
        assert isinstance(_f_image_count(weird), int)


# ---------------------------------------------------------------------------
# 页面级
# ---------------------------------------------------------------------------
@pytest.fixture()
def make_note(auth_client, csrf):
    created: list[int] = []

    def _make(title: str, content: str) -> int:
        response = auth_client.post(
            "/notes",
            data={"_csrf": csrf, "title": title, "content": content, "action": "save"},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        note_id = int(re.search(r"/notes/(\d+)", response.headers["location"]).group(1))
        created.append(note_id)
        return note_id

    yield _make
    for note_id in created:
        auth_client.post(f"/notes/{note_id}/purge", data={"_csrf": csrf}, follow_redirects=False)


def test_detail_page_shows_image_hint(auth_client, make_note):
    note_id = make_note(
        "带图的笔记",
        "正文\n\n![一](/media/2026/09/a.png)\n![二](/media/2026/09/b.png)\n",
    )
    page = auth_client.get(f"/notes/{note_id}")
    assert page.status_code == 200
    assert "本文引用 2 张图片" in page.text
    assert 'href="/images"' in page.text
    # 删除确认里也要带上
    assert "正文引用了 2 张图片" in page.text


def test_detail_page_without_images_has_no_hint(auth_client, make_note):
    note_id = make_note("没图的笔记", "纯文字，没有任何图片。")
    page = auth_client.get(f"/notes/{note_id}")
    assert page.status_code == 200
    assert "本文引用" not in page.text
    assert "正文引用了" not in page.text
    # 原来的确认文案保持原样
    assert "把《没图的笔记》移入回收站？" in page.text


def test_card_delete_confirm_mentions_images(auth_client, make_note):
    make_note("卡上有图", "![x](/media/2026/09/c.png)")
    page = auth_client.get("/notes")
    assert page.status_code == 200
    assert re.search(r"正文引用了 1 张图片", page.text), "卡片上的删除确认应提示引用了几张图"
    # 没图的卡片不该出现提示
    assert page.text.count("正文引用了") == 1
