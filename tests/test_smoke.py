"""端到端冒烟：登录、笔记 CRUD、博客、订阅源、搜索、上传、导出、回收站、历史版本。"""

from __future__ import annotations

import io
import json
import re
import zipfile

import pytest
from conftest import PASSWORD, csrf_of, login

NOTE_BODY = """# 会被去掉的重复标题

这是一篇用于测试的笔记，正文包含关键词「异步编程」和一个双链 [[第二篇笔记]]。

## 二级标题

- [x] 任务完成
- [ ] 待办事项

```python
import asyncio
print("异步")
```

## 代码之外

| 列 | 值 |
| --- | --- |
| a | 1 |
"""


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_blog_is_public(client):
    response = client.get("/blog")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_root_redirects_to_blog_when_anonymous(client):
    fresh = client.__class__(client.app)
    response = fresh.get("/", follow_redirects=False)
    assert response.status_code in (200, 303)


def test_notes_requires_login(client):
    fresh = client.__class__(client.app)
    response = fresh.get("/notes", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_login_wrong_password(client):
    fresh = client.__class__(client.app)
    response = fresh.post("/login", data={"password": "nope", "next": "/notes"})
    assert response.status_code == 401
    assert "密码不正确" in response.text


def test_login_ok(auth_client):
    response = auth_client.get("/notes")
    assert response.status_code == 200
    assert "我的笔记" in response.text


def test_security_headers(client):
    response = client.get("/blog")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert "nonce-" in response.headers["Content-Security-Policy"]


# ---------------------------------------------------------------------------
# 笔记 CRUD
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def note_id(client) -> int:
    """会话级：整个会话共用同一篇「第一篇笔记」（后面好几个用例都靠它）。

    会话级 fixture 不能依赖函数级的 auth_client / csrf（pytest 会抛 ScopeMismatch），
    所以这里直接在共享的 client 上登录、取 token。每次都重新登录，保证 token 和当前 cookie 一定匹配。
    """
    login(client)
    csrf = csrf_of(client)
    response = client.post(
        "/notes",
        data={
            "_csrf": csrf,
            "title": "第一篇笔记",
            "content": NOTE_BODY,
            "tags": "测试，异步",
            "category": "技术",
            "action": "view",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    match = re.search(r"/notes/(\d+)", location)
    assert match, location
    return int(match.group(1))


def test_create_note(auth_client, note_id):
    response = auth_client.get(f"/notes/{note_id}")
    assert response.status_code == 200
    assert "第一篇笔记" in response.text
    assert "异步编程" in response.text
    # 正文里与标题不同的 H1 会被保留
    assert "会被去掉的重复标题" in response.text
    # 代码高亮与表格包裹生效
    assert "codehilite" in response.text
    assert "table-wrap" in response.text
    # 任务清单
    assert "task-list" in response.text


def test_wikilink_missing_rendered(auth_client, note_id):
    response = auth_client.get(f"/notes/{note_id}")
    assert 'class="wikilink wikilink--missing"' in response.text
    assert "/notes/new?title=" in response.text


def test_note_dashboard_lists(auth_client, note_id):
    response = auth_client.get("/notes")
    assert f"/notes/{note_id}" in response.text
    assert "草稿" in response.text or "已保存" in response.text


def test_edit_note(auth_client, note_id, csrf):
    response = auth_client.post(
        f"/notes/{note_id}",
        data={
            "_csrf": csrf,
            "title": "第一篇笔记（改名）",
            "content": NOTE_BODY + "\n新增一段。\n",
            "tags": "测试",
            "category": "技术",
            "summary": "手写摘要",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = auth_client.get(f"/notes/{note_id}")
    assert "第一篇笔记（改名）" in page.text
    assert "手写摘要" in page.text


def test_unchecked_public_box_is_not_published(auth_client, csrf):
    """回归：新建笔记时 ``is_public=bool(表单值)`` 恒真，``is_public="0"``（取消勾选）
    也会把笔记公开出去。现在统一走 app.utils.as_bool。"""
    from app import db as db_mod

    for form_value, expected in (("0", 0), ("false", 0), ("1", 1), ("on", 1)):
        title = f"公开开关-{form_value}"
        response = auth_client.post(
            "/notes",
            data={"_csrf": csrf, "title": title, "content": "正文", "tags": "",
                  "is_public": form_value, "action": "save"},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        with db_mod.db() as conn:
            row = conn.execute(
                "SELECT is_public, is_pinned, is_starred FROM notes WHERE title = ?", (title,)
            ).fetchone()
        assert row is not None, f"{title} 没建出来"
        assert row["is_public"] == expected, f"is_public={form_value!r} 落库成 {row['is_public']}"
        # 没传的开关不能被顺手打开
        assert row["is_pinned"] == 0 and row["is_starred"] == 0


def test_autosave_creates_second_note(auth_client, csrf):
    """第二篇笔记：用来验证双链解析与相关笔记。"""
    response = auth_client.post(
        "/notes",
        data={
            "_csrf": csrf,
            "title": "第二篇笔记",
            "content": "关于测试与 [[第一篇笔记（改名）]] 的笔记。\n\n#标签A\n",
            "tags": "测试, 标签A",
            "action": "view",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = auth_client.get(response.headers["location"])
    assert page.status_code == 200
    # 正文里写的 #标签A 被自动收集进标签
    assert "标签A" in page.text
    # 双链指向已存在的笔记
    assert re.search(r'<a class="wikilink" href="/notes/\d+"', page.text)


def test_backlink_shows_on_target(auth_client, note_id):
    page = auth_client.get(f"/notes/{note_id}")
    assert "反向链接" in page.text
    assert "第二篇笔记" in page.text
    assert "你可能还想看" in page.text


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
def test_search_page(auth_client):
    response = auth_client.get("/search", params={"q": "异步编程"})
    assert response.status_code == 200
    assert "第一篇笔记" in response.text


def test_search_short_chinese_word(auth_client):
    response = auth_client.get("/search", params={"q": "测试"})
    assert response.status_code == 200
    assert "命中" in response.text


def test_search_api(auth_client):
    response = auth_client.get("/api/search", params={"q": "异步"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] >= 1
    item = payload["items"][0]
    assert item["url"].startswith("/notes/")
    assert "<mark>" in item["title_html"] or "<mark>" in item["snippet_html"]


def test_search_api_requires_login(client):
    fresh = client.__class__(client.app)
    assert fresh.get("/api/search", params={"q": "x"}).status_code == 401


# ---------------------------------------------------------------------------
# Preview / 自动保存 / 上传
# ---------------------------------------------------------------------------
def test_preview_api(auth_client, csrf):
    response = auth_client.post(
        "/api/preview",
        json={"content": "## 标题\n\n**粗体** 与 `代码`", "title": ""},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "<strong>粗体</strong>" in payload["html"]
    assert payload["toc"][0]["text"] == "标题"
    assert payload["word_count"] > 0


def test_preview_escapes_html(auth_client, csrf):
    response = auth_client.post(
        "/api/preview",
        json={"content": "<script>alert(1)</script>"},
        headers={"X-CSRF-Token": csrf},
    )
    assert "&lt;script&gt;" in response.json()["html"]


def test_autosave_api(auth_client, note_id):
    token = csrf_of(auth_client)
    response = auth_client.patch(
        f"/api/notes/{note_id}",
        json={"title": "第一篇笔记（改名）", "content": NOTE_BODY + "\n自动保存追加。\n"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["word_count"] > 0


def test_api_requires_csrf(auth_client, note_id):
    response = auth_client.patch(f"/api/notes/{note_id}", json={"content": "x"})
    assert response.status_code == 403


def test_upload_image(auth_client, csrf):
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )
    response = auth_client.post(
        "/api/upload",
        files={"file": ("shot.png", png, "image/png")},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["url"].startswith("/media/")
    assert payload["markdown"].startswith("![")
    # 图片能通过静态挂载访问到
    assert auth_client.get(payload["url"]).status_code == 200


def test_upload_rejects_bad_extension(auth_client, csrf):
    response = auth_client.post(
        "/api/upload",
        files={"file": ("evil.svg", b"<svg onload=alert(1)>", "image/svg+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert "不支持" in response.json()["error"]


def test_upload_rejects_fake_image_content(auth_client, csrf):
    """扩展名是 .png 但内容不是图片，应被拒绝（只查扩展名是不够的）。"""
    response = auth_client.post(
        "/api/upload",
        files={"file": ("fake.png", b"<html><script>alert(1)</script></html>", "image/png")},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert "不是图片" in response.json()["error"]


def test_upload_alt_text_is_sanitised(auth_client, csrf):
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )
    response = auth_client.post(
        "/api/upload",
        files={"file": ("bad](name).png", png, "image/png")},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    assert "](name)" not in response.json()["markdown"]
    assert response.json()["markdown"].count("](") == 1


# ---------------------------------------------------------------------------
# 发布到博客
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def published_slug(client, note_id) -> str:
    """会话级：把「第一篇笔记」公开到博客，返回它的博客地址（会话内共用）。"""
    login(client)
    csrf = csrf_of(client)
    response = client.post(
        f"/notes/{note_id}/flag",
        data={"_csrf": csrf, "flag": "public", "value": "1", "next": f"/notes/{note_id}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(f"/notes/{note_id}")
    match = re.search(r'href="(/blog/[^"]+)"', page.text)
    assert match, "公开后详情页应该出现博客链接"
    return match.group(1)


def test_blog_lists_published(auth_client, published_slug):
    response = auth_client.get("/blog")
    assert response.status_code == 200
    assert "第一篇笔记" in response.text


def test_blog_post_page(client, published_slug):
    response = client.get(published_slug)
    assert response.status_code == 200
    assert "第一篇笔记" in response.text
    assert 'property="og:type" content="article"' in response.text
    assert "canonical" in response.text


def test_blog_post_has_toc_and_prev_next(client, published_slug):
    page = client.get(published_slug)
    assert "目录" in page.text
    assert "post-nav" in page.text


def test_blog_tag_filter(client, published_slug):
    response = client.get("/blog", params={"tag": "测试"})
    assert response.status_code == 200


def test_blog_archive(client, published_slug):
    response = client.get("/blog/archive")
    assert response.status_code == 200
    assert "归档" in response.text


def test_feed_and_rss(client, published_slug):
    import xml.etree.ElementTree as ET

    atom = client.get("/feed.xml")
    assert atom.status_code == 200
    root = ET.fromstring(atom.content)
    assert root.tag.endswith("feed")
    assert len(root.findall("{http://www.w3.org/2005/Atom}entry")) >= 1

    rss = client.get("/rss.xml")
    assert rss.status_code == 200
    rss_root = ET.fromstring(rss.content)
    assert rss_root.tag == "rss"
    items = rss_root.findall("./channel/item")
    assert items and items[0].find("title").text


def test_sitemap_and_robots(client, published_slug):
    import xml.etree.ElementTree as ET

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    ET.fromstring(sitemap.content)
    assert published_slug in sitemap.text

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /notes" in robots.text
    assert "Sitemap:" in robots.text


def test_public_post_links_point_to_blog(auth_client, client, csrf):
    """博客文章页侧栏里的相关文章 / 反向链接必须是 /blog/ 地址，不能指向要登录的 /notes/。"""
    target = auth_client.post(
        "/notes",
        data={
            "_csrf": csrf,
            "title": "公开的目标",
            "content": "这是被引用的公开笔记。",
            "tags": "测试, 公开",
            "is_public": "1",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert target.status_code == 303
    referrer = auth_client.post(
        "/notes",
        data={
            "_csrf": csrf,
            "title": "私密的引用者",
            "content": "见 [[公开的目标]]",
            "tags": "测试, 公开",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert referrer.status_code == 303

    listing = client.get("/blog")
    assert "公开的目标" in listing.text
    link = None
    for candidate in re.findall(r'href="(/blog/[^"]+)"', listing.text):
        if "target" in candidate or "公开" in candidate:
            link = candidate
            break
    assert link, "博客列表里应该有「公开的目标」的链接"
    page = client.get(link)
    assert page.status_code == 200
    rail = page.text.split('<aside class="post-rail">')[-1]
    assert "/notes/" not in rail, "公开页面的侧栏不该出现 /notes/ 链接"
    # 私密引用者不能出现在公开页面上
    assert "私密的引用者" not in page.text


def test_private_note_not_reachable_by_guest(client, auth_client, csrf):
    """草稿绝不能被未登录访客看到。"""
    created = auth_client.post(
        "/notes",
        data={"_csrf": csrf, "title": "私密草稿", "content": "只有我能看", "action": "save"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    fresh = client.__class__(client.app)
    assert fresh.get("/blog").text.find("私密草稿") == -1
    # 未公开的 slug 直接访问应 404
    response = fresh.get("/blog/私密草稿")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 历史版本 / 回收站 / 导出
# ---------------------------------------------------------------------------
def test_versions_flow(auth_client, note_id, csrf):
    listing = auth_client.get(f"/notes/{note_id}/versions")
    assert listing.status_code == 200
    match = re.search(r"/notes/%d/versions/(\d+)" % note_id, listing.text)
    assert match, "应该至少有一个历史版本"
    version_id = match.group(1)

    detail = auth_client.get(f"/notes/{note_id}/versions/{version_id}")
    assert detail.status_code == 200
    assert "版本对比" in detail.text

    restored = auth_client.post(
        f"/notes/{note_id}/versions/{version_id}/restore",
        data={"_csrf": csrf, "next": f"/notes/{note_id}"},
        follow_redirects=False,
    )
    assert restored.status_code == 303


def test_trash_flow(auth_client, csrf):
    created = auth_client.post(
        "/notes",
        data={"_csrf": csrf, "title": "待删除的笔记", "content": "内容", "action": "save"},
        follow_redirects=False,
    )
    note_path = created.headers["location"].replace("/edit", "")
    note_id = int(re.search(r"/notes/(\d+)", note_path).group(1))

    deleted = auth_client.post(
        f"/notes/{note_id}/delete", data={"_csrf": csrf, "next": "/notes"}, follow_redirects=False
    )
    assert deleted.status_code == 303
    assert auth_client.get(f"/notes/{note_id}").status_code == 404
    assert "待删除的笔记" in auth_client.get("/trash").text

    restored = auth_client.post(
        f"/notes/{note_id}/restore", data={"_csrf": csrf, "next": "/trash"}, follow_redirects=False
    )
    assert restored.status_code == 303
    assert auth_client.get(f"/notes/{note_id}").status_code == 200

    auth_client.post(f"/notes/{note_id}/delete", data={"_csrf": csrf, "next": "/notes"})
    purged = auth_client.post(
        f"/notes/{note_id}/purge", data={"_csrf": csrf, "next": "/trash"}, follow_redirects=False
    )
    assert purged.status_code == 303


def test_export_zip(auth_client):
    response = auth_client.get("/export/zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    assert "index.md" in names
    assert "notes.json" in names
    assert any(name.startswith("notes/") and name.endswith(".md") for name in names)
    manifest = json.loads(archive.read("notes.json").decode("utf-8"))
    assert manifest["count"] >= 2
    assert manifest["notes"][0]["title"]


def test_single_note_export(auth_client, note_id):
    response = auth_client.get(f"/notes/{note_id}/export.md")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.text.startswith("---")
    assert "title:" in response.text


# ---------------------------------------------------------------------------
# 其它页面
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["/tags", "/templates", "/ask", "/stats", "/trash", "/search", "/notes/new", "/manual"],
)
def test_pages_render(auth_client, path):
    response = auth_client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}"


def test_manual_page_renders_the_guide(auth_client):
    """使用说明.md 会被渲染成网页（不用装 Markdown 阅读器也能看）。"""
    response = auth_client.get("/manual")
    assert response.status_code == 200
    assert "使用说明" in response.text
    # 说明里的章节标题应该被渲染出来，并且生成了目录
    assert "怎么打开" in response.text
    assert "Markdown 语法速查" in response.text
    assert 'class="toc"' in response.text
    assert "post-body prose" in response.text
    # 代码块应该被渲染成 <pre>，而不是显示原始的反引号
    assert "<pre>" in response.text or "codehilite" in response.text


def test_note_templates_crud(auth_client, csrf):
    created = auth_client.post(
        "/templates/save",
        data={"_csrf": csrf, "name": "测试模板", "description": "说明", "content": "## 内容"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    listing = auth_client.get("/templates")
    assert "测试模板" in listing.text


def test_save_note_from_template(auth_client, csrf):
    listing = auth_client.get("/templates")
    match = re.search(r"/notes/new\?template=(\d+)", listing.text)
    assert match
    page = auth_client.get(f"/notes/new?template={match.group(1)}")
    assert page.status_code == 200


def test_404_page(auth_client):
    response = auth_client.get("/notes/999999")
    assert response.status_code == 404
    assert "不存在" in response.text


def test_startup_banner_is_gbk_safe(monkeypatch):
    """回归：启动横幅曾经用了 ⚠，在中文 Windows 的 GBK 控制台上抛
    UnicodeEncodeError，直接把服务启动搞崩（真的踩过）。"""

    class StrictGbkStream:
        """模拟真实的 GBK 控制台：编不出来的字符就抛 UnicodeEncodeError。"""

        encoding = "gbk"

        def __init__(self) -> None:
            self.data = ""

        def write(self, text: str) -> int:
            text.encode(self.encoding)  # 编不出来会抛，和真实控制台一样
            self.data += text
            return len(text)

        def flush(self) -> None:
            pass

    stream = StrictGbkStream()
    monkeypatch.setattr("sys.stdout", stream)

    from app.main import _print_banner

    _print_banner()  # 不允许抛异常
    assert "已启动" in stream.data
    assert "笔记后台" in stream.data
    stream.data.encode("gbk")  # 最终写出去的每一段都必须是 GBK 能编码的


HUGE = "99999999999999999999"
INT64_MAX = "9223372036854775807"


@pytest.mark.parametrize(
    "path",
    [
        f"/notes?page={HUGE}",
        f"/notes?page={INT64_MAX}",
        f"/blog?page={HUGE}",
        f"/blog?month=x&page={HUGE}",
        f"/search?q=&page={HUGE}",
        f"/tags?tag=x&page={HUGE}",
        f"/trash?page={HUGE}",
        f"/notes/{HUGE}",
        f"/notes/{HUGE}/edit",
        f"/notes/1/versions/{HUGE}",
        f"/templates?edit={HUGE}",
        f"/notes/{INT64_MAX}",
        "/notes?page=-1",
        "/notes?page=0",
        "/notes?page=abc",
        "/notes?page=1.5",
    ],
)
def test_huge_or_bad_integers_never_500(auth_client, path):
    """回归：超大整数以前会当成 SQLite 的 OFFSET/ID 绑参，抛 OverflowError → 500。
    现在应该在路由层就被拒（422/404），绝不能是 500。"""
    response = auth_client.get(path)
    assert response.status_code != 500, f"{path} 仍然 500"
    assert response.status_code in (200, 303, 404, 422), f"{path} 返回了 {response.status_code}"


def test_huge_integers_in_post_never_500(auth_client, csrf):
    flagged = auth_client.post(
        f"/notes/{HUGE}/flag", data={"_csrf": csrf, "flag": "star", "value": "1"}
    )
    assert flagged.status_code == 422
    patched = auth_client.patch(f"/api/notes/{HUGE}", json={"content": "x"},
                                headers={"X-CSRF-Token": csrf})
    assert patched.status_code == 422


@pytest.mark.parametrize("bad_id", [HUGE, INT64_MAX + "0", "-1", "0", "abc", "1.5"])
def test_template_save_with_bad_id_never_500(auth_client, csrf, bad_id):
    """回归：/templates/save 以前直接 int(表单值)，20 位纯数字会抛
    OverflowError（Python int 转 SQLite INTEGER 溢出）→ 500。
    现在非法编号一律 303 回列表页，且不能顺手建出一个新模板。
    （template_id 为空才是「新建」，所以空串 / 纯空白不在非法之列。）"""
    before = len(re.findall(r"template=\d+", auth_client.get("/templates").text))
    response = auth_client.post(
        "/templates/save",
        data={"_csrf": csrf, "template_id": bad_id, "name": "不该被创建", "content": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"template_id={bad_id!r} -> {response.status_code}"
    assert "/templates" in response.headers["location"]
    listing = auth_client.get("/templates")
    assert "不该被创建" not in listing.text, f"template_id={bad_id!r} 竟然建了新模板"
    assert len(re.findall(r"template=\d+", listing.text)) == before


def test_ask_without_ai(auth_client, csrf):
    response = auth_client.post("/ask", data={"_csrf": csrf, "question": "异步编程"})
    assert response.status_code == 200
    assert "AI 服务未配置" in response.text
    assert "第一篇笔记" in response.text


def test_ai_endpoint_without_config(auth_client, csrf):
    response = auth_client.post(
        "/api/ai/summarize",
        json={"title": "x", "content": "一些内容"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 503
    assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# 设置页（AI 配置）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def fake_ai_server():
    """起一个假的 OpenAI 兼容服务，验证「设置页保存 → AI 真的能用」整条链路。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace")
            try:
                payload = json.loads(body or "{}")
                text = " ".join(
                    str(message.get("content", "")) for message in payload.get("messages", [])
                )
            except ValueError:
                text = body
            if "标签" in text:
                reply = '["测试标签","AI"]'
            elif "摘要" in text:
                reply = "这是测试生成的摘要。"
            else:
                reply = "可用"
            data = json.dumps({"choices": [{"message": {"content": reply}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            data = json.dumps(
                {"object": "list", "data": [{"id": "test-model"}, {"id": "other-model"}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # 别往测试输出里刷日志
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()


@pytest.fixture()
def ai_configured(auth_client, csrf, fake_ai_server):
    """临时通过「设置页」配置 AI，测试结束自动清掉，避免影响其它用例。"""
    saved = auth_client.post(
        "/settings/ai",
        data={
            "_csrf": csrf,
            "base_url": fake_ai_server,
            "api_key": "sk-test-1234567890abcd",
            "model": "test-model",
            "timeout": "20",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    yield fake_ai_server
    auth_client.post("/settings/ai", data={"_csrf": csrf, "clear_all": "1"}, follow_redirects=False)


def test_settings_page_renders(auth_client):
    response = auth_client.get("/settings")
    assert response.status_code == 200
    assert "AI 服务" in response.text
    assert "常见服务商怎么填" in response.text
    assert "未启用" in response.text or "已启用" in response.text


def test_settings_page_needs_login(client):
    fresh = client.__class__(client.app)
    assert fresh.get("/settings", follow_redirects=False).status_code == 303


def test_settings_save_enables_ai_and_masks_key(auth_client, csrf, ai_configured):
    from app.services import ai as ai_service

    assert ai_service.is_enabled() is True
    page = auth_client.get("/settings")
    assert "已启用" in page.text
    # 密钥不能回显到页面上
    assert "sk-test-1234567890abcd" not in page.text
    assert "已保存：" in page.text


def test_ai_endpoints_work_with_configured_ai(auth_client, csrf, ai_configured):
    summary = auth_client.post(
        "/api/ai/summarize",
        json={"title": "测试", "content": "随便写点内容"},
        headers={"X-CSRF-Token": csrf},
    )
    assert summary.status_code == 200, summary.text
    assert summary.json()["summary"] == "这是测试生成的摘要。"

    tags = auth_client.post(
        "/api/ai/tags",
        json={"title": "测试", "content": "随便写点内容"},
        headers={"X-CSRF-Token": csrf},
    )
    assert tags.status_code == 200
    assert tags.json()["tags"] == ["测试标签", "AI"]


def test_ask_page_uses_configured_ai(auth_client, csrf, ai_configured):
    # 先写一篇含独特关键词的笔记，保证检索能命中
    auth_client.post(
        "/notes",
        data={
            "_csrf": csrf,
            "title": "问答测试笔记",
            "content": "这里有一段很独特的内容：紫色独角兽在跳舞。",
            "action": "save",
        },
        follow_redirects=False,
    )
    response = auth_client.post("/ask", data={"_csrf": csrf, "question": "紫色独角兽"})
    assert response.status_code == 200
    assert "AI 回答" in response.text
    assert "可用" in response.text  # 假服务返回的内容


def test_settings_test_connection_reports_failure(auth_client, csrf):
    """测试连接失败时要给出提示，而不是 500。"""
    response = auth_client.post(
        "/settings/ai/test",
        data={
            "_csrf": csrf,
            "base_url": "http://127.0.0.1:1/v1",
            "model": "nope",
            "timeout": "5",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = auth_client.get(response.headers["location"])
    assert "连接失败" in page.text


def test_settings_test_connection_success(auth_client, csrf, fake_ai_server):
    response = auth_client.post(
        "/settings/ai/test",
        data={
            "_csrf": csrf,
            "base_url": fake_ai_server,
            "model": "test-model",
            "timeout": "20",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = auth_client.get(response.headers["location"])
    assert "连接成功" in page.text


def test_settings_clear_all_disables_ai(auth_client, csrf, fake_ai_server):
    from app.services import ai as ai_service

    auth_client.post(
        "/settings/ai",
        data={"_csrf": csrf, "base_url": fake_ai_server, "model": "test-model", "timeout": "20"},
        follow_redirects=False,
    )
    assert ai_service.is_enabled() is True

    cleared = auth_client.post(
        "/settings/ai", data={"_csrf": csrf, "clear_all": "1"}, follow_redirects=False
    )
    assert cleared.status_code == 303
    assert ai_service.is_enabled() is False
    assert "已清除" in auth_client.get(cleared.headers["location"]).text


def test_settings_rejects_csrf(auth_client):
    response = auth_client.post(
        "/settings/ai", data={"base_url": "http://x", "model": "y"}, follow_redirects=False
    )
    assert response.status_code == 403


def test_ai_settings_persist_across_restart(auth_client, csrf, fake_ai_server):
    """页面保存的配置写进了数据库：重新 bootstrap（模拟重启）后还在。"""
    from app import db as db_mod
    from app.services import ai as ai_service

    auth_client.post(
        "/settings/ai",
        data={
            "_csrf": csrf,
            "base_url": fake_ai_server,
            "model": "persisted-model",
            "timeout": "30",
        },
        follow_redirects=False,
    )
    with db_mod.db() as conn:
        ai_service.bootstrap(conn)
    assert ai_service.current()["model"] == "persisted-model"
    assert ai_service.describe()["sources"]["model"] == "db"
    auth_client.post("/settings/ai", data={"_csrf": csrf, "clear_all": "1"}, follow_redirects=False)


def test_editor_page(auth_client, note_id):
    response = auth_client.get(f"/notes/{note_id}/edit")
    assert response.status_code == 200
    assert 'id="editor-form"' in response.text
    assert 'data-note-id="%d"' % note_id in response.text
    assert 'data-preview-url="/api/preview"' in response.text
    assert 'id="md-toolbar"' in response.text


def test_dark_mode_and_nonce(client):
    response = client.get("/blog")
    assert 'data-theme="light"' in response.text
    assert 'id="theme-toggle"' in response.text
    assert 'nonce="' in response.text


def test_logout(auth_client, csrf):
    """退出后 /notes 应该跳登录页（放在最后，避免影响其它测试）。"""
    response = auth_client.post("/logout", data={"_csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert auth_client.get("/notes", follow_redirects=False).status_code == 303
