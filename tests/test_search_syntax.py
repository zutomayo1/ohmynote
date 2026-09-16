"""搜索语法（tag:/title:/cat:/is:/after:/before:/短语/排除）+ 中文二元索引。"""

from __future__ import annotations

import pytest

from app import db as db_mod, repo, search


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "syntax.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


@pytest.fixture()
def seeded(conn):
    repo.create_note(
        conn, title="Docker 部署手记", content="用 docker compose 部署服务。",
        tags="部署, 运维", category="技术", is_starred=True,
    )
    repo.create_note(
        conn, title="读书笔记", content="一本讲深度工作的书。", tags="读书",
        category="随笔",
    )
    repo.create_note(conn, title="私人草稿", content="随手记点东西。", status="draft")
    return conn


# ---------------------------------------------------------------------------
# parse_query 单元
# ---------------------------------------------------------------------------
def test_parse_query_prefixes():
    parsed = search.parse_query("tag:读书 title:周报 cat:技术 is:starred -草稿 关键词")
    assert parsed["tags"] == ["读书"]
    assert parsed["titles"] == ["周报"]
    assert parsed["categories"] == ["技术"]
    assert parsed["is"] == {"starred": True}
    assert parsed["excluded"] == ["草稿"]
    assert parsed["terms"] == ["关键词"]


def test_parse_query_hash_tag_and_time():
    parsed = search.parse_query("#读书 after:2026-01 before:2026-09")
    assert parsed["tags"] == ["读书"]
    assert parsed["after"] == "2026-01"
    assert parsed["before"] == "2026-09"


def test_parse_query_phrase_kept_whole():
    parsed = search.parse_query('"精确 短语" tag:读书')
    assert parsed["phrases"] == ["精确 短语"]
    assert parsed["tags"] == ["读书"]


def test_parse_query_unknown_prefix_falls_back_to_term():
    parsed = search.parse_query("foo:bar")
    assert parsed["terms"] == ["foo:bar"]


def test_parse_query_terms_deduped_and_capped():
    parsed = search.parse_query("a b c d e f g h")
    assert len(parsed["terms"]) == 6


# ---------------------------------------------------------------------------
# 集成：语法真的能筛
# ---------------------------------------------------------------------------
def test_search_by_tag(conn, seeded):
    results = search.search(conn, "tag:读书")
    assert [row["title"] for row, _s, _sn in results] == ["读书笔记"]


def test_search_by_hash_tag(conn, seeded):
    results = search.search(conn, "#部署")
    assert [row["title"] for row, _s, _sn in results] == ["Docker 部署手记"]


def test_search_by_title(conn, seeded):
    results = search.search(conn, "title:读书")
    assert [row["title"] for row, _s, _sn in results] == ["读书笔记"]


def test_search_by_category(conn, seeded):
    results = search.search(conn, "cat:随笔")
    assert [row["title"] for row, _s, _sn in results] == ["读书笔记"]


def test_search_by_is_starred(conn, seeded):
    results = search.search(conn, "is:starred")
    assert [row["title"] for row, _s, _sn in results] == ["Docker 部署手记"]


def test_search_excluded_word(conn, seeded):
    results = search.search(conn, "-草稿")
    titles = [row["title"] for row, _s, _sn in results]
    assert "私人草稿" not in titles
    assert len(titles) == 2


def test_search_excluded_word_narrows_results(conn, seeded):
    # 「部署」命中 docker 篇；排除后应只剩 0 篇
    assert len(search.search(conn, "部署")) == 1
    assert len(search.search(conn, "部署 -部署")) == 0


def test_search_phrase(conn, seeded):
    results = search.search(conn, '"docker compose"')
    assert [row["title"] for row, _s, _sn in results] == ["Docker 部署手记"]


def test_search_tag_combined_with_term(conn, seeded):
    results = search.search(conn, "tag:部署 深度工作")
    assert results == []   # 标签与关键词不相交 → 空
    results = search.search(conn, "tag:部署 Docker")
    assert [row["title"] for row, _s, _sn in results] == ["Docker 部署手记"]


def test_search_time_range(conn, seeded):
    # 全部笔记都是今天创建/更新的，任意晚的起点都搜不到
    assert search.search(conn, "after:2099-01") == []
    # 只给年份：起点 1 月 1 日 → 全命中
    assert len(search.search(conn, "after:2020")) == 3


def test_search_pure_structured_query(conn, seeded):
    """没有关键词、只有条件（如 tag:读书）也要返回结果。"""
    results = search.search(conn, "tag:读书")
    assert len(results) == 1


# ---------------------------------------------------------------------------
# 中文短词：二元索引让 1~2 字查询不再走 LIKE 兜底
# ---------------------------------------------------------------------------
def test_single_char_chinese_query(conn, seeded):
    results = search.search(conn, "书")
    titles = [row["title"] for row, _s, _sn in results]
    assert "读书笔记" in titles


def test_two_char_chinese_query(conn, seeded):
    results = search.search(conn, "部署")
    assert [row["title"] for row, _s, _sn in results] == ["Docker 部署手记"]


def test_long_chinese_substring(conn, seeded):
    # 三字以上 = 相邻二元组 AND，仍是连续子串语义
    results = search.search(conn, "部署手记")
    assert [row["title"] for row, _s, _sn in results] == ["Docker 部署手记"]


# ---------------------------------------------------------------------------
# 索引格式升级：旧 trigram 表自动重建为新格式
# ---------------------------------------------------------------------------
def test_ensure_schema_migrates_old_trigram_table(conn, seeded):
    conn.execute("DROP TABLE notes_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE notes_fts USING fts5("
        "note_id UNINDEXED, title, content, tags, tokenize='trigram')"
    )
    search.ensure_schema(conn)
    assert search.FTS_TOKENIZER == "bigram"
    assert search.FTS_ENABLED is True
    # 重建后索引可用
    results = search.search(conn, "读书")
    assert len(results) == 1
