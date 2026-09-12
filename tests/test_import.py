"""导入 / 恢复：往返、幂等、单篇 Markdown、notes.json、裸 JSON、坏文件、HTTP 入口。"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import db as db_mod, repo
from app.services import export as export_service, importer


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "import.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


def _seed(conn):
    """造几篇覆盖公开/草稿、标签、分类、url 的笔记。"""
    first = repo.create_note(
        conn,
        title="往返一",
        content="第一段。\n\n## 小节\n\n正文里有 #python 和 #异步 两个标签。",
        tags="python, 异步",
        category="技术",
        is_public=True,
        status="saved",
    )
    second = repo.create_note(
        conn,
        title="往返二",
        content="第二段结尾。",
        tags="生活",
        category="生活",
        status="draft",
    )
    return [first, second]


def _zip_bytes(tmp_path, seed=True):
    source = tmp_path / "source.db"
    db_mod.init_db(source)
    with db_mod.db(source) as src:
        originals = _seed(src) if seed else []
        payload = export_service.build_zip(src)
    return payload, originals


# ---------------------------------------------------------------------------
# 往返 + 幂等（最重要）
# ---------------------------------------------------------------------------
def test_zip_roundtrip_preserves_fields(conn, tmp_path):
    payload, originals = _zip_bytes(tmp_path)
    result = importer.sniff_and_import(conn, "backup.zip", payload)

    assert result["source"] == "zip"
    assert result["errors"] == []
    assert result["created"] == len(originals)
    assert result["updated"] == 0

    imported = {note["title"]: note for note in repo.all_notes(conn)}
    assert set(imported) == {note["title"] for note in originals}
    for original in originals:
        got = imported[original["title"]]
        assert got["content"] == original["content"]
        assert set(got["tags"]) == set(original["tags"])
        assert got["is_public"] is original["is_public"]
        assert got["category"] == original["category"]
        assert got["status"] == original["status"]


def test_zip_import_twice_does_not_double(conn, tmp_path):
    payload, originals = _zip_bytes(tmp_path)
    importer.sniff_and_import(conn, "backup.zip", payload)
    second = importer.sniff_and_import(conn, "backup.zip", payload)

    assert second["created"] == 0
    assert second["updated"] == len(originals)
    assert second["errors"] == []
    assert len(repo.all_notes(conn)) == len(originals)


def test_zip_import_does_not_delete_existing_notes(conn, tmp_path):
    payload, originals = _zip_bytes(tmp_path)
    keep = repo.create_note(conn, title="备份里没有的笔记", content="要留住")
    importer.sniff_and_import(conn, "backup.zip", payload)

    assert repo.get_note(conn, keep["id"]) is not None
    assert len(repo.all_notes(conn)) == len(originals) + 1


# ---------------------------------------------------------------------------
# 单篇 Markdown
# ---------------------------------------------------------------------------
def test_markdown_front_matter_wins(conn):
    text = (
        "---\n"
        'title: "单篇标题"\n'
        "slug: my-single\n"
        'tags: ["甲", "乙"]\n'
        'category: "测试"\n'
        "public: true\n"
        "status: saved\n"
        "---\n\n"
        "# 正文里的标题\n\n正文内容。\n"
    )
    result = importer.sniff_and_import(conn, "single.md", text.encode("utf-8"))

    assert result["source"] == "markdown"
    assert result["created"] == 1 and result["errors"] == []
    note = repo.get_note_by_slug(conn, "my-single", public_only=False)
    assert note is not None
    assert note["title"] == "单篇标题"
    assert set(note["tags"]) == {"甲", "乙"}
    assert note["category"] == "测试"
    assert note["is_public"] is True
    assert note["content"] == "# 正文里的标题\n\n正文内容。"


def test_markdown_title_falls_back_to_heading_then_filename(conn):
    importer.sniff_and_import(conn, "one.md", "# 来自标题\n\n正文".encode("utf-8"))
    importer.sniff_and_import(conn, "two-file.md", "没有一级标题的正文".encode("utf-8"))

    titles = {note["title"] for note in repo.all_notes(conn)}
    assert "来自标题" in titles
    assert "two-file" in titles


def test_markdown_bare_yaml_block_list(conn):
    text = "---\ntitle: 块列表标签\ntags:\n  - alpha\n  - beta\n---\n\n正文"
    result = importer.import_markdown(conn, text, filename="block.md")
    assert result["created"] == 1
    note = repo.all_notes(conn)[0]
    assert set(note["tags"]) == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# notes.json（zip 里的清单）/ 裸 JSON
# ---------------------------------------------------------------------------
def test_import_notes_json_manifest(conn, tmp_path):
    payload, originals = _zip_bytes(tmp_path)
    manifest = zipfile.ZipFile(io.BytesIO(payload)).read("notes.json")

    result = importer.sniff_and_import(conn, "notes.json", manifest)
    assert result["source"] == "json"
    assert result["errors"] == []
    assert result["created"] == len(originals)
    assert len(repo.all_notes(conn)) == len(originals)

    again = importer.sniff_and_import(conn, "notes.json", manifest)
    assert again["created"] == 0
    assert again["updated"] == len(originals)
    assert len(repo.all_notes(conn)) == len(originals)


def test_manifest_import_keeps_existing_content(conn):
    note = repo.create_note(conn, title="保留正文", content="原始正文", slug="keep-body")
    manifest = json.dumps(
        {"notes": [{"title": "保留正文", "slug": "keep-body", "tags": ["t"], "is_public": True}]},
        ensure_ascii=False,
    ).encode("utf-8")

    result = importer.sniff_and_import(conn, "notes.json", manifest)
    assert result["updated"] == 1
    fresh = repo.get_note(conn, note["id"])
    assert fresh["content"] == "原始正文"
    assert fresh["is_public"] is True


def test_import_bare_json_array(conn):
    payload = json.dumps(
        [
            {"title": "裸一", "content": "a", "tags": ["x"], "is_public": True},
            {"title": "裸二", "content": "b"},
        ],
        ensure_ascii=False,
    ).encode("utf-8")

    first = importer.sniff_and_import(conn, "notes.json", payload)
    assert first["created"] == 2 and first["errors"] == []

    second = importer.sniff_and_import(conn, "notes.json", payload)
    assert second["created"] == 0 and second["updated"] == 2
    assert len(repo.all_notes(conn)) == 2


def test_import_single_json_object(conn):
    payload = json.dumps({"title": "单对象", "content": "x"}, ensure_ascii=False).encode("utf-8")
    result = importer.sniff_and_import(conn, "note.json", payload)
    assert result["created"] == 1


def test_match_by_title_when_slug_missing(conn):
    first = importer.import_markdown(conn, "# 同名\n\n第一版", filename="a.md")
    assert first["created"] == 1

    second = importer.import_markdown(conn, "# 同名\n\n第二版", filename="b.md")
    assert second["created"] == 0 and second["updated"] == 1
    assert len(repo.all_notes(conn)) == 1


def test_dry_run_does_not_write(conn):
    result = importer.import_markdown(
        conn, "---\ntitle: 预览\n---\n\nx", filename="preview.md", dry_run=True
    )
    assert result["created"] == 1
    assert result["dry_run"] is True
    assert repo.all_notes(conn) == []


# ---------------------------------------------------------------------------
# 坏文件：不能 500，必须进 errors
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "filename,data",
    [
        ("empty.zip", b""),
        ("empty.md", b""),
        ("empty.json", b""),
        ("garbled.md", b"\xff\xfe\x00\x01not-utf8"),
        ("broken.json", b"{not json"),
        ("notzip.zip", b"this is definitely not a zip"),
        ("mystery.bin", b"\x80\x81\x82\x83"),
    ],
)
def test_bad_files_report_errors_without_raising(conn, filename, data):
    result = importer.sniff_and_import(conn, filename, data)
    assert result["errors"], f"{filename} 应该在 errors 里说明问题"
    assert result["created"] == 0
    assert repo.all_notes(conn) == []


def test_zip_without_notes_reports_error(conn):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.txt", "no notes here")
        archive.writestr("notes.json", json.dumps({"notes": []}))
    result = importer.sniff_and_import(conn, "empty-notes.zip", buffer.getvalue())
    assert result["errors"]
    assert result["created"] == 0


# ---------------------------------------------------------------------------
# HTTP：登录 / CSRF / 页面 / 上传
# ---------------------------------------------------------------------------
def test_backup_requires_login():
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/backup", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_backup_page_has_form_and_export_links(auth_client):
    page = auth_client.get("/backup")
    assert page.status_code == 200
    html = page.text
    assert 'action="/backup/import"' in html
    assert 'enctype="multipart/form-data"' in html
    assert 'name="file"' in html
    assert "/export/zip" in html
    assert "/export/json" in html
    assert "backup-drop" in html
    assert "绝不删除" in html
    assert "不会翻倍" in html


def test_backup_post_without_csrf_is_rejected(auth_client):
    files = {"file": ("x.md", b"# t\n\nbody", "text/markdown")}
    response = auth_client.post("/backup/import", files=files, follow_redirects=False)
    assert response.status_code == 403


def test_backup_post_dry_run_redirects_and_shows_result(auth_client, csrf):
    text = "---\ntitle: HTTP预览\n---\n\n内容".encode("utf-8")
    response = auth_client.post(
        "/backup/import",
        data={"_csrf": csrf, "dry_run": "1"},
        files={"file": ("dry.md", text, "text/markdown")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/backup")
    from urllib.parse import unquote_plus

    assert "新建 1 篇" in unquote_plus(response.headers["location"])

    page = auth_client.get("/backup")
    assert "最近一次导入" in page.text
    assert "预览模式" in page.text


def test_backup_post_real_import_then_cleanup(auth_client, csrf):
    title = "HTTP真实导入-唯一标题"
    text = f"---\ntitle: {title}\ntags: [\"http\"]\n---\n\n真实正文".encode("utf-8")
    response = auth_client.post(
        "/backup/import",
        data={"_csrf": csrf},
        files={"file": ("real.md", text, "text/markdown")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with db_mod.db() as outer:
        found = repo.find_notes_by_title(outer, title)
        assert found
        for note in found:
            repo.purge(outer, note["id"])
    with db_mod.db() as outer:
        assert repo.find_notes_by_title(outer, title) == []


def test_json_export_route(auth_client):
    response = auth_client.get("/export/json")
    assert response.status_code == 200
    payload = response.json()
    assert payload["app"] == "inknote"
    assert isinstance(payload["notes"], list)
    assert payload["count"] == len(payload["notes"])


def test_json_export_roundtrips_into_fresh_db(tmp_path, auth_client, csrf):
    title = "JSON导出往返-唯一标题"
    auth_client.post(
        "/notes",
        data={"_csrf": csrf, "title": title, "content": "导出正文", "action": "save"},
        follow_redirects=False,
    )
    try:
        exported = auth_client.get("/export/json").content
        target = tmp_path / "fresh.db"
        db_mod.init_db(target)
        with db_mod.db(target) as fresh:
            result = importer.sniff_and_import(fresh, "export.json", exported)
            assert result["created"] >= 1
            assert result["errors"] == []
    finally:
        with db_mod.db() as outer:
            for note in repo.find_notes_by_title(outer, title):
                repo.purge(outer, note["id"])
