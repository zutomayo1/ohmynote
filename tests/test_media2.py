"""A3 图片库第二轮：分页 / 统计 / 复制 Markdown 链接 / 引用展开。

只走进程内 TestClient，不启动 uvicorn、不起浏览器。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import db as db_mod, repo
from app.config import settings
from app.services import media

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

PER_PAGE = 24
TOTAL_IMAGES = 30


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------
def _seed_images(root: Path, count: int = TOTAL_IMAGES) -> list[str]:
    """直接往 upload_dir 写假图，返回相对路径列表。"""
    month = root / "2026" / "09"
    month.mkdir(parents=True, exist_ok=True)
    rels: list[str] = []
    for i in range(1, count + 1):
        rel = f"2026/09/pic-{i:02d}.png"
        (root / rel).write_bytes(PNG)
        rels.append(rel)
    return rels


@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    _seed_images(root)
    monkeypatch.setattr(settings, "upload_dir", root)
    return root


def _create_note(title: str, content: str) -> int:
    with db_mod.db() as conn:
        return int(repo.create_note(conn, title=title, content=content)["id"])


def _purge_note(note_id: int) -> None:
    with db_mod.db() as conn:
        repo.purge(conn, note_id)


def _card_names(html: str) -> list[str]:
    return re.findall(r'media-card__name" title="([^"]+)"', html)


# ---------------------------------------------------------------------------
# 1. 分页：每页 24 张
# ---------------------------------------------------------------------------
def test_pagination_24_per_page(upload_dir, auth_client):
    page1 = auth_client.get("/images?page=1")
    assert page1.status_code == 200
    assert page1.text.count('<article class="media-card') == PER_PAGE
    names1 = _card_names(page1.text)
    assert len(names1) == PER_PAGE

    page2 = auth_client.get("/images?page=2")
    assert page2.status_code == 200
    assert page2.text.count('<article class="media-card') == TOTAL_IMAGES - PER_PAGE
    names2 = _card_names(page2.text)
    assert len(names2) == TOTAL_IMAGES - PER_PAGE

    # 两页合起来正好是 30 张，不重不漏；排序仍是上传时间倒序
    assert set(names1).isdisjoint(names2)
    assert set(names1) | set(names2) == {f"pic-{i:02d}.png" for i in range(1, 31)}
    assert names1[0] == "pic-30.png"

    # 页码越界收敛到最后一页，不会渲染成假的「还没有图片」
    overflow = auth_client.get("/images?page=99")
    assert overflow.text.count('<article class="media-card') == TOTAL_IMAGES - PER_PAGE
    assert "还没有图片" not in overflow.text


# ---------------------------------------------------------------------------
# 2. orphan 筛选与分页共存
# ---------------------------------------------------------------------------
def test_orphan_filter_and_pagination_coexist(upload_dir, auth_client):
    used = ["2026/09/pic-30.png", "2026/09/pic-29.png"]
    body = "\n".join(f"![x](/media/{rel})" for rel in used)
    note_id = _create_note("引用两张", body)
    try:
        response = auth_client.get("/images?orphan=1&page=1")
        assert response.status_code == 200
        html = response.text
        assert html.count('<article class="media-card') == PER_PAGE
        names1 = _card_names(html)
        assert "pic-30.png" not in names1
        assert "pic-29.png" not in names1
        # 翻页链接必须带着 orphan=1（Jinja 会把 & 转义成 &amp;）
        assert 'href="/images?orphan=1&amp;page=2"' in html
        assert "孤立 <span" in html

        page2 = auth_client.get("/images?orphan=1&page=2")
        assert page2.status_code == 200
        assert page2.text.count('<article class="media-card') == TOTAL_IMAGES - 2 - PER_PAGE
        names2 = _card_names(page2.text)
        assert "pic-30.png" not in names2
        assert "pic-29.png" not in names2
    finally:
        _purge_note(note_id)


# ---------------------------------------------------------------------------
# 3. 引用列表：完整数据 + 前 3 条 + 「还有 N 篇」
# ---------------------------------------------------------------------------
def test_refs_list_is_complete_and_expandable(upload_dir, auth_client):
    target = "2026/09/pic-30.png"
    five_notes = [
        _create_note(f"引用第{i}篇", f"![x](/media/{target})") for i in range(1, 6)
    ]
    five_images_note = _create_note(
        "一篇引用五张图",
        "\n".join(f"![x](/media/2026/09/pic-{i:02d}.png)" for i in range(1, 6)),
    )
    try:
        with db_mod.db() as conn:
            data = media.library(conn, upload_dir=upload_dir)
        by_rel = {item["rel"]: item for item in data["items"]}

        # 服务层/模板拿到的必须是完整列表，而不是被截断的前 3 条
        assert [entry["title"] for entry in by_rel[target]["used_by"]] == [
            f"引用第{i}篇" for i in range(1, 6)
        ]
        assert len(by_rel[target]["used_by"]) == 5

        # 反方向：一篇笔记引用 5 张图，5 张图都能找到这篇笔记
        for i in range(1, 6):
            rel = f"2026/09/pic-{i:02d}.png"
            titles = [entry["title"] for entry in by_rel[rel]["used_by"]]
            assert "一篇引用五张图" in titles

        html = auth_client.get("/images").text
        for i in range(1, 6):
            assert f"引用第{i}篇" in html
        # 5 篇引用 → 默认 3 条 + 隐藏 2 条 + 「还有 2 篇」
        assert "还有 2 篇" in html
        assert "5 篇引用" in html
        assert html.count('class="media-card__link--extra" hidden') == 2
    finally:
        for note_id in five_notes:
            _purge_note(note_id)
        _purge_note(five_images_note)


# ---------------------------------------------------------------------------
# 4. 顶部统计
# ---------------------------------------------------------------------------
def test_stats_numbers(upload_dir, auth_client):
    html = auth_client.get("/images").text
    assert "共 <strong>30</strong> 张" in html
    assert "占用 <strong>" in html
    assert '孤立 <span class="is-orphan">30 张（' in html


def test_stats_numbers_with_used(upload_dir, auth_client):
    used = ["2026/09/pic-30.png", "2026/09/pic-29.png"]
    body = "\n".join(f"![x](/media/{rel})" for rel in used)
    note_id = _create_note("引用两张", body)
    try:
        html = auth_client.get("/images").text
        assert "共 <strong>30</strong> 张" in html
        assert '孤立 <span class="is-orphan">28 张（' in html
        # 两张图各被这一篇笔记引用一次
        assert html.count("1 篇引用") >= 2
    finally:
        _purge_note(note_id)


# ---------------------------------------------------------------------------
# 5. 复制链接 / 极端情况
# ---------------------------------------------------------------------------
def test_copy_button_has_markdown_payload(upload_dir, auth_client):
    html = auth_client.get("/images").text
    assert "media-copy" in html
    assert 'data-copy="![](/media/2026/09/pic-30.png)"' in html
    # 复制实现：clipboard + prompt 回退，且没有 alert
    assert "navigator.clipboard.writeText" in html
    assert "window.prompt" in html
    assert "alert(" not in html


def test_paginate_empty_and_single_item():
    items, page, pages = media.paginate([], page=1)
    assert (items, page, pages) == ([], 1, 1)

    one = [{"rel": "a.png"}]
    items, page, pages = media.paginate(one, page=5)
    assert items == one
    assert (page, pages) == (1, 1)


def test_single_image_then_empty_state(tmp_path, monkeypatch, auth_client):
    root = tmp_path / "uploads-single"
    _seed_images(root, count=1)
    monkeypatch.setattr(settings, "upload_dir", root)

    html = auth_client.get("/images").text
    assert html.count('<article class="media-card') == 1
    assert '孤立 <span class="is-orphan">1 张（' in html

    assert media.delete("2026/09/pic-01.png", upload_dir=root) is True
    empty = auth_client.get("/images").text
    assert "还没有图片" in empty
    assert "共 <strong>0</strong> 张" in empty
    assert '孤立 <span class="is-orphan">0 张（0 B）</span>' in empty
