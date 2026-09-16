"""全文搜索：中文短词、权重、公开过滤、摘要与高亮。"""

from __future__ import annotations

import pytest

from app import db as db_mod, repo, search


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "search.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


@pytest.fixture()
def seeded(conn):
    repo.create_note(
        conn,
        title="Python 异步编程",
        content="关于 asyncio 事件循环的笔记，异步编程很实用。",
        tags="python, 异步",
        is_public=True,
    )
    repo.create_note(
        conn,
        title="读书笔记",
        content="一本讲编程思想的书，值得一读。",
        tags="读书, 编程",
        is_public=True,
    )
    repo.create_note(conn, title="私人草稿", content="这里也提到了编程与异步。")
    return conn


def test_fts_is_enabled(conn):
    del conn
    assert search.FTS_ENABLED is True
    assert search.FTS_TOKENIZER in {"bigram", "trigram", "unicode61"}


def test_title_match_ranks_first(seeded):
    results = repo.search_notes(seeded, "读书", limit=10)
    assert results[0]["title"] == "读书笔记"


def test_two_char_chinese_query_works(seeded):
    """中文双字词必须能搜到（trigram 模式下会退回 LIKE）。"""
    results = repo.search_notes(seeded, "异步", limit=10)
    assert {item["title"] for item in results} >= {"Python 异步编程", "私人草稿"}


def test_tag_is_searchable_and_weighted(seeded):
    results = repo.search_notes(seeded, "编程", limit=10)
    titles = [item["title"] for item in results]
    assert "Python 异步编程" in titles
    assert "读书笔记" in titles
    assert all(item["score"] > 0 for item in results)


def test_multi_keyword_requires_all_terms(seeded):
    assert repo.search_notes(seeded, "异步 事件循环", limit=10)
    assert repo.search_notes(seeded, "异步 不存在的词", limit=10) == []


def test_public_only_filter(seeded):
    public_hits = repo.search_notes(seeded, "编程", public_only=True, limit=10)
    assert all(item["is_public"] for item in public_hits)
    assert len(public_hits) == 2


def test_deleted_note_disappears_from_search(seeded):
    note = repo.search_notes(seeded, "私人草稿")[0]
    repo.soft_delete(seeded, note["id"])
    assert repo.search_notes(seeded, "私人草稿") == []


def test_snippet_contains_keyword(seeded):
    item = repo.search_notes(seeded, "事件循环", limit=1)[0]
    assert "事件循环" in item["snippet"]


def test_highlight_escapes_html():
    rendered = search.highlight("<b>脚本</b> 与关键词", ["关键词"])
    assert "&lt;b&gt;" in rendered
    assert "<b>" not in rendered
    assert "<mark>关键词</mark>" in rendered


def test_highlight_does_not_break_mark_tag():
    """关键词命中 <mark> 自身不会产生嵌套标签。"""
    rendered = search.highlight("mark ar ma", ["ma", "ar", "mark"])
    assert rendered.count("<mark>") == rendered.count("</mark>")


def test_tokenize_dedupes_and_limits():
    tokens = search.tokenize("a b c d e f g h")
    assert tokens == ["a", "b", "c", "d", "e", "f"]


def test_question_terms_for_chinese():
    terms = search.question_terms("我关于 SQLite 的笔记有哪些踩坑记录？")
    assert any("踩坑" in term for term in terms)
    assert any("sqlite" in term for term in terms)
    assert "的" not in terms


def test_retrieve_ranks_relevant_note_first(seeded):
    results = repo.retrieve_notes(seeded, "异步编程踩过什么坑？", limit=5)
    assert results
    assert results[0]["title"] in {"Python 异步编程", "私人草稿"}


def test_rebuild_fts_index(seeded):
    count = search.rebuild(seeded)
    assert count == 3
    assert repo.search_notes(seeded, "异步")
