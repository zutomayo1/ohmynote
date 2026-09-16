"""博客互动：代码块语言标签、点赞/阅读计数、JSON-LD 结构化数据。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import repo
from app.main import app
from app.markdown_render import render as render_md
from tests.test_agent import db_conn  # noqa: F401  fixture 随模块导入

PREFIX = "博客互动-"  # 专属前缀：断言按前缀定位，不碰全局聚合


def _public_note(db_conn, title: str, content: str) -> dict:
    note = repo.create_note(db_conn, title=title, content=content, is_public=True, status="saved")
    db_conn.commit()
    return note


# ---------------------------------------------------------------------------
# 代码块语言标签（superfences custom_fences）
# ---------------------------------------------------------------------------
def test_fence_with_lang_gets_label_and_tokens(db_conn):
    html = render_md("```python\nprint(1)\n```").html
    assert 'class="codehilite"' in html
    assert '<span class="code-lang">python</span>' in html
    assert "nb" in html  # pygments token 类名还在，highlight.css 配色不受影响


def test_fence_without_lang_has_no_label(db_conn):
    html = render_md("```\nplain\n```").html
    assert "code-lang" not in html
    assert "codehilite" in html


def test_mermaid_fence_still_becomes_mermaid_div(db_conn):
    """回归：mermaid 围栏在 markdown 之前就被抽走，superfences 不能吃掉它。"""
    note = _public_note(db_conn, PREFIX + "图", "```mermaid\ngraph TD; A-->B\n```")
    from app.services import content as content_service

    rendered = content_service.render_note(db_conn, note, public=True)
    assert '<div class="mermaid">' in rendered.html


def test_unknown_lang_still_escapes_code(db_conn):
    """未知语言：pygments 回退默认词法器，代码必须原样转义、不能当 HTML 执行。"""
    html = render_md("```not-a-lang\n<b>&x</b>\n```").html
    assert "&lt;b&gt;" in html
    assert "<b>" not in html


# ---------------------------------------------------------------------------
# 点赞 / 阅读计数
# ---------------------------------------------------------------------------
def test_like_route_increments_and_rate_limits(db_conn):
    note = _public_note(db_conn, PREFIX + "点赞", "正文")
    anon = TestClient(app)

    first = anon.post(f"/blog/{note['slug']}/like")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["ok"] is True and body["likes"] == 1 and body["dup"] is False

    # 同 IP 立刻再点：计数不涨（限频），但接口不报错
    second = anon.post(f"/blog/{note['slug']}/like").json()
    assert second["likes"] == 1 and second["dup"] is True

    assert anon.post("/blog/不存在/like").status_code == 404


def test_read_count_increments_for_anon_only(db_conn, auth_client):
    note = _public_note(db_conn, PREFIX + "阅读", "正文")
    anon = TestClient(app)
    anon.get(f"/blog/{note['slug']}")
    anon.get(f"/blog/{note['slug']}")
    assert repo.blog_stats_get(db_conn, note["slug"])["reads"] == 2

    # 登录用户（作者）访问不计数（auth_client/csrf 是 conftest 函数级 fixture）
    page = auth_client.get(f"/blog/{note['slug']}")
    assert page.status_code == 200
    assert repo.blog_stats_get(db_conn, note["slug"])["reads"] == 2


# ---------------------------------------------------------------------------
# JSON-LD 结构化数据
# ---------------------------------------------------------------------------
def test_post_page_has_jsonld(db_conn):
    note = _public_note(db_conn, PREFIX + "结构化", "正文")
    anon = TestClient(app)
    page = anon.get(f"/blog/{note['slug']}")
    assert page.status_code == 200
    marker = 'type="application/ld+json"'
    assert marker in page.text
    blob = page.text.split(marker, 1)[1].split(">", 1)[1].split("</script>", 1)[0]
    data = json.loads(blob)
    assert data["@type"] == "BlogPosting"
    assert data["headline"] == PREFIX + "结构化"
    assert data["inLanguage"] == "zh-CN"
    assert data["datePublished"] and data["dateModified"]


def test_post_page_shows_like_and_reads(db_conn):
    note = _public_note(db_conn, PREFIX + "展示", "正文")
    anon = TestClient(app)
    anon.post(f"/blog/{note['slug']}/like")
    anon.get(f"/blog/{note['slug']}")
    page = anon.get(f"/blog/{note['slug']}")
    assert "post-like__btn" in page.text
    assert "2 次阅读" in page.text
