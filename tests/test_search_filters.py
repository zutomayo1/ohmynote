"""搜索页补齐：结果卡片可操作 + 与列表页同口径的筛选/排序。

回归背景：搜索页以前是死胡同 —— 结果卡片 `actions=False`（没有星标/置顶/公开/编辑按钮），
而且 `/search` 只认 `q/page/mode`，不能像列表页那样按标签/分类/状态/星标缩小范围。
"""

from __future__ import annotations

import re

import pytest

from app import db as db_mod


@pytest.fixture()
def search_notes(auth_client, csrf):
    """造四篇有区分度的笔记（标题用 ASCII 方便断言排序）。

    字段刻意铺开：星标 / 置顶 / 公开 / 草稿，两个分类，两个标签。
    """
    specs = [
        ("ccharlie 星标", "共同关键词 异步", "标签甲", "技术", {"is_starred": "1"}),
        ("aalpha 置顶", "共同关键词 异步", "标签乙", "技术", {"is_pinned": "1"}),
        ("ddelta 公开", "共同关键词 异步", "标签甲", "生活", {"is_public": "1"}),
        ("bbravo 草稿", "共同关键词 异步", "标签乙", "生活", {"action": "draft"}),
    ]
    created: dict[str, int] = {}
    for title, content, tags, category, extra in specs:
        data = {"_csrf": csrf, "title": title, "content": content, "tags": tags,
                "category": category, "action": "save", **extra}
        response = auth_client.post("/notes", data=data, follow_redirects=False)
        assert response.status_code == 303, response.text
        match = re.search(r"/notes/(\d+)", response.headers["location"])
        assert match, response.headers["location"]
        created[title.split()[0]] = int(match.group(1))
    try:
        yield created
    finally:
        for note_id in created.values():
            auth_client.post(f"/notes/{note_id}/purge",
                             data={"_csrf": csrf, "next": "/notes"}, follow_redirects=False)


def _ids_in(page_text: str) -> list[int]:
    """页面里出现的笔记 id（按出现顺序）。"""
    return [int(m) for m in re.findall(r'href="/notes/(\d+)"', page_text)]


def test_results_are_actionable(auth_client, search_notes):
    """结果卡片要带操作按钮（以前 actions=False，只剩个光卡片）。"""
    page = auth_client.get("/search", params={"q": "异步"})
    assert page.status_code == 200
    text = page.text
    for note_id in search_notes.values():
        assert f'action="/notes/{note_id}/flag"' in text, f"{note_id} 没有置顶/星标表单"
        assert f'href="/notes/{note_id}/edit"' in text, f"{note_id} 没有编辑入口"


@pytest.mark.parametrize(
    ("fav", "expected"),
    [("starred", "ccharlie"), ("pinned", "aalpha"), ("public", "ddelta"), ("draft", "bbravo")],
)
def test_fav_chips_use_the_same_vocabulary_as_the_list_page(auth_client, search_notes, fav, expected):
    page = auth_client.get("/search", params={"q": "异步", "fav": fav})
    assert page.status_code == 200
    ids = _ids_in(page.text)
    assert search_notes[expected] in ids, f"fav={fav} 没筛出 {expected}"
    # 其它三篇都不该出现
    for name, note_id in search_notes.items():
        if name != expected:
            assert note_id not in ids, f"fav={fav} 不该带出 {name}"


def test_tag_category_status_filters(auth_client, search_notes):
    tagged = auth_client.get("/search", params={"q": "异步", "tag": "标签甲"})
    assert sorted(_ids_in(tagged.text)) == sorted(
        [search_notes["ccharlie"], search_notes["ddelta"]]
    )

    by_category = auth_client.get("/search", params={"q": "异步", "category": "技术"})
    assert sorted(_ids_in(by_category.text)) == sorted(
        [search_notes["ccharlie"], search_notes["aalpha"]]
    )

    drafts = auth_client.get("/search", params={"q": "异步", "status": "draft"})
    assert _ids_in(drafts.text) == [search_notes["bbravo"]]


def test_filters_combine_and_report_the_filtered_total(auth_client, search_notes):
    page = auth_client.get("/search", params={"q": "异步", "tag": "标签甲", "category": "生活"})
    assert page.status_code == 200
    assert _ids_in(page.text) == [search_notes["ddelta"]]
    assert "命中 1 篇" in page.text
    assert "已叠加筛选" in page.text


def test_default_order_is_relevance_then_sort_is_opt_in(auth_client, search_notes):
    """默认不动顺序（相关度更有用）；显式给 sort 才排序，且置顶优先。"""
    default_page = auth_client.get("/search", params={"q": "异步"})
    assert len(_ids_in(default_page.text)) == 4

    by_title = auth_client.get("/search", params={"q": "异步", "sort": "title"})
    # 置顶优先，其余按标题升序：aalpha(置顶) -> bbravo -> ccharlie -> ddelta
    assert _ids_in(by_title.text) == [
        search_notes["aalpha"], search_notes["bbravo"],
        search_notes["ccharlie"], search_notes["ddelta"],
    ]

    by_updated = auth_client.get("/search", params={"q": "异步", "sort": "updated"})
    assert len(_ids_in(by_updated.text)) == 4


@pytest.mark.parametrize("fav", ["", "starred", "不存在的值", "1", "0"])
def test_weird_fav_never_500(auth_client, search_notes, fav):
    page = auth_client.get("/search", params={"q": "异步", "fav": fav})
    assert page.status_code == 200


def test_empty_filtered_result_offers_a_reset(auth_client, search_notes):
    """筛空了要给人台阶下：给一句人话 + 重置入口。"""
    page = auth_client.get("/search", params={"q": "异步", "tag": "根本没有这个标签"})
    assert page.status_code == 200
    assert "当前筛选条件下没有匹配的笔记" in page.text
    assert "重置筛选条件" in page.text


def test_filters_keep_the_search_query_and_mode(auth_client, search_notes):
    """点筛选（或用 GET 表单提交）之后，关键词和「按意思搜」不能被丢掉。"""
    page = auth_client.get("/search", params={"q": "异步", "mode": "semantic", "fav": "starred"})
    assert page.status_code == 200
    assert 'name="q" value="异步"' in page.text
    assert 'name="mode" value="semantic"' in page.text
    assert 'name="fav" value="starred"' in page.text
