"""图片管理：扫描 / 引用统计 / 安全删除 / 页面与接口。

覆盖接口文档 3.3 节要求的全部行为。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote_plus

import pytest
from conftest import csrf_of  # noqa: F401  (保证 conftest 可用)

from app import db as db_mod, repo
from app.config import settings
from app.services import media

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)
NAMES = ("sha-used-one.png", "sha-used-two.png", "sha-orphan.png")
USED_ONE = "2026/09/sha-used-one.png"
USED_TWO = "2026/09/sha-used-two.png"
ORPHAN = "2026/09/sha-orphan.png"


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------
@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    """预置 3 张图（2 张会被引用、1 张孤立），并把 uploads 指到临时目录。"""
    root = tmp_path / "uploads"
    month = root / "2026" / "09"
    month.mkdir(parents=True)
    for name in NAMES:
        (month / name).write_bytes(PNG)
    monkeypatch.setattr(settings, "upload_dir", root)
    return root


@pytest.fixture()
def conn(tmp_path):
    """服务层用独立临时库，避免污染共享的测试库。"""
    path = tmp_path / "media.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


def _table(root: Path) -> Path:
    return root / "2026" / "09"


def _create_note(title: str, content: str) -> int:
    with db_mod.db() as conn:
        return int(repo.create_note(conn, title=title, content=content)["id"])


def _purge_note(note_id: int) -> None:
    with db_mod.db() as conn:
        repo.purge(conn, note_id)


# ---------------------------------------------------------------------------
# 1. 扫描
# ---------------------------------------------------------------------------
def test_scan_lists_preset_images(upload_dir):
    items = media.scan(upload_dir)
    assert {item["rel"] for item in items} == {USED_ONE, USED_TWO, ORPHAN}
    for item in items:
        assert item["url"] == f"/media/{item['rel']}"
        assert item["size"] == len(PNG)
        assert item["name"].endswith(".png")
    mtimes = [item["mtime"] for item in items]
    assert mtimes == sorted(mtimes, reverse=True)


def test_scan_missing_dir_is_empty(tmp_path):
    assert media.scan(tmp_path / "nope") == []


def test_extract_media_urls_handles_variants():
    content = (
        "![a](/media/2026/09/a.png)\n"
        '<img src="/media/2026/09/b.png?v=2">\n'
        "[/media/2026/09/a.png](/media/2026/09/a.png)\n"
        "百分之 %2f 编码：/media/2026/09/%E4%B8%AD.png\n"
        "无关链接 /static/x.png 与 https://example.com/media/nope.png\n"
    )
    urls = media.extract_media_urls(content)
    assert "/media/2026/09/a.png" in urls
    assert "/media/2026/09/b.png" in urls
    assert "/media/2026/09/中.png" in urls
    assert all(url.startswith("/media/") for url in urls)


# ---------------------------------------------------------------------------
# 2/5. 引用统计
# ---------------------------------------------------------------------------
def test_library_counts_references_and_orphans(upload_dir, conn):
    note = repo.create_note(
        conn,
        title="引用两张图的笔记",
        content=f"![一](/media/{USED_ONE})\n\n![二](/media/{USED_TWO})\n",
    )
    data = media.library(conn, upload_dir=upload_dir)

    assert data["total"] == 3
    assert data["orphan_count"] == 1
    assert data["total_size"] == 3 * len(PNG)
    assert data["orphan_size"] == len(PNG)

    by_rel = {item["rel"]: item for item in data["items"]}
    assert by_rel[USED_ONE]["orphan"] is False
    assert by_rel[USED_TWO]["orphan"] is False
    assert by_rel[ORPHAN]["orphan"] is True
    assert by_rel[ORPHAN]["used_by"] == []
    assert [entry["id"] for entry in by_rel[USED_ONE]["used_by"]] == [note["id"]]
    assert by_rel[USED_ONE]["used_by"][0]["url"] == f"/notes/{note['id']}"
    assert by_rel[USED_ONE]["used_by"][0]["title"] == "引用两张图的笔记"


def test_usage_detects_media_links_in_note_body(upload_dir, conn):
    note = repo.create_note(
        conn,
        title="正文带图",
        content='看这张图 <img src="/media/2026/09/sha-used-two.png"> 结尾。',
    )
    refs = media.usage(conn)
    ids = [entry["id"] for entry in refs.get(f"/media/{USED_TWO}", [])]
    assert note["id"] in ids


def test_usage_ignores_non_media_links(upload_dir, conn):
    repo.create_note(
        conn,
        title="只有站内链接",
        content="[a](/static/x.png) 和 https://example.com/media/x.png",
    )
    assert media.usage(conn) == {}


# ---------------------------------------------------------------------------
# 3. 删除孤立图片（服务层）
# ---------------------------------------------------------------------------
def test_delete_orphans_only_removes_orphans(upload_dir, conn):
    body = f"![x](/media/{USED_ONE})\n![y](/media/{USED_TWO})"
    note = repo.create_note(conn, title="引用两张", content=body)
    removed = media.delete_orphans(conn, upload_dir=upload_dir)

    assert removed == 1
    assert not (_table(upload_dir) / "sha-orphan.png").exists()
    assert (_table(upload_dir) / "sha-used-one.png").exists()
    assert (_table(upload_dir) / "sha-used-two.png").exists()
    # 笔记正文没被动过
    fresh = repo.get_note(conn, note["id"])
    assert fresh is not None
    assert fresh["content"] == body


def test_delete_removes_real_file(upload_dir):
    assert media.delete(ORPHAN, upload_dir=upload_dir) is True
    assert not (_table(upload_dir) / "sha-orphan.png").exists()
    assert media.delete(ORPHAN, upload_dir=upload_dir) is False


# ---------------------------------------------------------------------------
# 4. 目录穿越：一律不许删到 uploads 外面的文件
# ---------------------------------------------------------------------------
def test_delete_blocks_traversal_and_absolute(upload_dir, tmp_path):
    (tmp_path / "data").mkdir()
    fake_db = tmp_path / "data" / "inknote.db"
    fake_db.write_bytes(b"SQLite format 3\x00")
    sentinel = tmp_path / "secret.env"
    sentinel.write_text("TOPSECRET=1", encoding="utf-8")

    bad_inputs = [
        "../secret.env",
        "../../.env",
        "../data/inknote.db",
        "..%2f..%2f.env",
        str(fake_db),  # 绝对路径
    ]
    for bad in bad_inputs:
        assert media.delete(bad, upload_dir=upload_dir) is False, bad

    assert sentinel.is_file()
    assert fake_db.is_file()


def test_delete_refuses_directories(upload_dir):
    assert media.delete("2026", upload_dir=upload_dir) is False
    assert media.delete("2026/09", upload_dir=upload_dir) is False
    assert (_table(upload_dir) / "sha-used-one.png").exists()


# ---------------------------------------------------------------------------
# HTTP：页面
# ---------------------------------------------------------------------------
def test_images_page_renders_grid_and_stats(upload_dir, auth_client):
    note_id = _create_note("引用第一张", f"![x](/media/{USED_ONE})")
    try:
        response = auth_client.get("/images")
        assert response.status_code == 200
        html = response.text
        assert html.count('<article class="media-card') == 3
        assert "media-grid" in html
        assert "sha-used-one.png" in html
        assert "sha-orphan.png" in html
        assert "孤立" in html
        assert f'href="/notes/{note_id}"' in html
        assert 'href="/images?orphan=1"' in html
        assert 'action="/images/delete-orphans"' in html
        assert "data-confirm" in html
    finally:
        _purge_note(note_id)


def test_images_page_orphan_filter(upload_dir, auth_client):
    note_id = _create_note(
        "引用两张",
        f"![x](/media/{USED_ONE})\n![y](/media/{USED_TWO})",
    )
    try:
        response = auth_client.get("/images?orphan=1")
        assert response.status_code == 200
        html = response.text
        assert html.count('<article class="media-card') == 1
        assert "sha-orphan.png" in html
        assert "sha-used-one.png" not in html
        assert "sha-used-two.png" not in html
        assert 'href="/images">' in html  # 「显示全部图片」
    finally:
        _purge_note(note_id)


def test_images_empty_state(upload_dir, auth_client):
    for name in NAMES:
        media.delete(f"2026/09/{name}", upload_dir=upload_dir)
    html = auth_client.get("/images").text
    assert "还没有图片" in html
    assert "拖拽" in html


def test_images_requires_login(client):
    fresh = client.__class__(client.app)
    response = fresh.get("/images", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# HTTP：删除接口
# ---------------------------------------------------------------------------
def test_delete_referenced_image_rejected_then_force(upload_dir, auth_client, csrf):
    target = _table(upload_dir) / "sha-used-one.png"
    note_id = _create_note("还在引用它", f"![x](/media/{USED_ONE})")
    try:
        refused = auth_client.post(
            "/images/delete",
            data={"_csrf": csrf, "rel": USED_ONE},
            follow_redirects=False,
        )
        assert refused.status_code == 303
        assert target.is_file()
        assert "还在引用它" in unquote_plus(refused.headers["location"])

        forced = auth_client.post(
            "/images/delete",
            data={"_csrf": csrf, "rel": USED_ONE, "force": "1"},
            follow_redirects=False,
        )
        assert forced.status_code == 303
        assert not target.exists()
    finally:
        _purge_note(note_id)


def test_delete_orphan_via_route_keeps_note_body(upload_dir, auth_client, csrf):
    keep = _table(upload_dir) / "sha-used-one.png"
    body = f"![x](/media/{USED_ONE})"
    note_id = _create_note("引用一张", body)
    try:
        response = auth_client.post(
            "/images/delete",
            data={"_csrf": csrf, "rel": ORPHAN},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert not (_table(upload_dir) / "sha-orphan.png").exists()
        assert keep.is_file()
        with db_mod.db() as conn:
            fresh = repo.get_note(conn, note_id)
            assert fresh is not None
            assert fresh["content"] == body
    finally:
        _purge_note(note_id)


def test_delete_orphans_via_route(upload_dir, auth_client, csrf):
    note_id = _create_note("引用两张", f"![a](/media/{USED_ONE})\n![b](/media/{USED_TWO})")
    try:
        response = auth_client.post(
            "/images/delete-orphans",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "已清理 1 张" in unquote_plus(response.headers["location"])
        assert not (_table(upload_dir) / "sha-orphan.png").exists()
        assert (_table(upload_dir) / "sha-used-one.png").is_file()
        assert (_table(upload_dir) / "sha-used-two.png").is_file()
    finally:
        _purge_note(note_id)


def test_delete_route_blocks_traversal(upload_dir, auth_client, csrf, tmp_path):
    (tmp_path / "data").mkdir()
    fake_db = tmp_path / "data" / "inknote.db"
    fake_db.write_bytes(b"SQLite format 3\x00")
    sentinel = tmp_path / "secret.env"
    sentinel.write_text("TOPSECRET=1", encoding="utf-8")

    for rel in ("../secret.env", "../../.env", "../data/inknote.db", "..%2f..%2fsecret.env", str(fake_db)):
        response = auth_client.post(
            "/images/delete",
            data={"_csrf": csrf, "rel": rel},
            follow_redirects=False,
        )
        assert response.status_code == 303, rel

    assert sentinel.is_file()
    assert fake_db.is_file()


def test_delete_requires_csrf(upload_dir, auth_client):
    response = auth_client.post(
        "/images/delete",
        data={"rel": ORPHAN},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert (_table(upload_dir) / "sha-orphan.png").is_file()


def test_delete_orphans_requires_csrf(upload_dir, auth_client):
    response = auth_client.post("/images/delete-orphans", data={}, follow_redirects=False)
    assert response.status_code == 403
    assert (_table(upload_dir) / "sha-orphan.png").is_file()
