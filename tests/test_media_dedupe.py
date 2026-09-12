"""第四轮 G：图片按内容去重（上传去重 / 重复检测 / 清理 / 页面）。

只走进程内 TestClient 与临时 uploads 目录，不启动服务、不起浏览器。
"""

from __future__ import annotations

import hashlib
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
# 与 PNG 内容不同，但文件头仍是 PNG，可绕过 looks_like_image 的格式校验
PNG2 = PNG + b"\x00\x01"
PNG_SHA = hashlib.sha256(PNG).hexdigest()


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------
@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    """把 uploads 指到干净的临时目录。"""
    root = tmp_path / "uploads"
    (root / "2026" / "09").mkdir(parents=True)
    monkeypatch.setattr(settings, "upload_dir", root)
    return root


@pytest.fixture()
def conn(tmp_path):
    """服务层用独立临时库，避免污染共享的测试库。"""
    path = tmp_path / "dedupe.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


def _seed(root: Path, rel: str, data: bytes = PNG) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _image_files(root: Path) -> list[Path]:
    """uploads 里的真实图片（不含 .sha256 边车）。"""
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(media.FINGERPRINT_SUFFIX)
    ]


def _upload(auth_client, csrf: str, name: str, data: bytes = PNG):
    return auth_client.post(
        "/api/upload",
        files={"file": (name, data, "image/png")},
        headers={"X-CSRF-Token": csrf},
    )


def _create_note(title: str, content: str) -> int:
    """给 HTTP 页面用的笔记必须写进全局测试库（路由依赖它）。"""
    with db_mod.db() as connection:
        return int(repo.create_note(connection, title=title, content=content)["id"])


def _purge_note(note_id: int) -> None:
    with db_mod.db() as connection:
        repo.purge(connection, note_id)


# ---------------------------------------------------------------------------
# 1/2. 上传：同内容只留一份，不同内容都留
# ---------------------------------------------------------------------------
def test_upload_same_content_dedupes_to_one_file(upload_dir, auth_client, csrf):
    first = _upload(auth_client, csrf, "shot.png")
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["deduped"] is False
    assert payload["url"].startswith("/media/")

    # 故意换个扩展名再传同一份内容：老逻辑会写成两个文件，去重后必须复用第一个
    second = _upload(auth_client, csrf, "shot-copy.jpg")
    assert second.status_code == 200, second.text
    deduped = second.json()
    assert deduped["deduped"] is True
    assert deduped["url"] == payload["url"]
    assert deduped["items"][0]["deduped"] is True
    assert deduped["markdown"].endswith(f"]({payload['url']})")

    files = _image_files(upload_dir)
    assert len(files) == 1
    assert files[0].read_bytes() == PNG


def test_upload_different_content_keeps_both(upload_dir, auth_client, csrf):
    first = _upload(auth_client, csrf, "a.png")
    second = _upload(auth_client, csrf, "b.png", PNG2)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["deduped"] is False
    assert first.json()["url"] != second.json()["url"]
    files = _image_files(upload_dir)
    assert len(files) == 2
    assert {path.read_bytes() for path in files} == {PNG, PNG2}


# ---------------------------------------------------------------------------
# 3. duplicates()：按 sha256 分组 + used_by 正确
# ---------------------------------------------------------------------------
def test_duplicates_groups_by_sha_and_reports_used_by(upload_dir, conn):
    rels = ["2026/09/dup-a.png", "2026/09/dup-b.png", "2026/09/dup-c.png"]
    for rel in rels:
        _seed(upload_dir, rel, PNG)
    note = repo.create_note(conn, title="引用其中一份", content=f"![x](/media/{rels[1]})")

    groups = media.duplicates(conn, upload_dir=upload_dir)
    assert len(groups) == 1
    group = groups[0]
    assert group["sha256"] == PNG_SHA
    assert group["count"] == 3
    assert group["duplicated_bytes"] == 2 * len(PNG)

    by_rel = {entry["rel"]: entry for entry in group["files"]}
    assert set(by_rel) == set(rels)
    for entry in group["files"]:
        assert entry["url"] == f"/media/{entry['rel']}"
        assert entry["size"] == len(PNG)
    assert [entry["id"] for entry in by_rel[rels[1]]["used_by"]] == [note["id"]]
    assert by_rel[rels[0]]["used_by"] == []
    assert by_rel[rels[2]]["used_by"] == []


def test_duplicates_ignores_different_content(upload_dir, conn):
    _seed(upload_dir, "2026/09/x.png", PNG)
    _seed(upload_dir, "2026/09/y.png", PNG2)
    assert media.duplicates(conn, upload_dir=upload_dir) == []


# ---------------------------------------------------------------------------
# 4/5. cleanup_duplicates()：删未被引用的多余副本，被引用的一份都不动
# ---------------------------------------------------------------------------
def test_cleanup_duplicates_keeps_referenced_and_frees_bytes(upload_dir, conn):
    rels = ["2026/09/dup-a.png", "2026/09/dup-b.png", "2026/09/dup-c.png"]
    for rel in rels:
        _seed(upload_dir, rel, PNG)
    repo.create_note(conn, title="引用第三份", content=f"![x](/media/{rels[2]})")

    result = media.cleanup_duplicates(conn, upload_dir=upload_dir)
    assert result == {"removed": 2, "freed": 2 * len(PNG)}
    assert (upload_dir / rels[2]).is_file()
    assert not (upload_dir / rels[0]).exists()
    assert not (upload_dir / rels[1]).exists()


def test_cleanup_duplicates_never_deletes_referenced_copies(upload_dir, conn):
    rels = ["2026/09/dup-a.png", "2026/09/dup-b.png"]
    for rel in rels:
        _seed(upload_dir, rel, PNG)
    repo.create_note(conn, title="笔记 A", content=f"![x](/media/{rels[0]})")
    repo.create_note(conn, title="笔记 B", content=f"![x](/media/{rels[1]})")

    result = media.cleanup_duplicates(conn, upload_dir=upload_dir)
    assert result == {"removed": 0, "freed": 0}
    assert all((upload_dir / rel).is_file() for rel in rels)


# ---------------------------------------------------------------------------
# 6. 老图片没有指纹：首次扫描懒计算，仍能识别为重复
# ---------------------------------------------------------------------------
def test_legacy_images_without_fingerprint_are_lazily_deduped(upload_dir, conn):
    rels = ["2026/09/old-a.png", "2026/09/old-b.png"]
    for rel in rels:
        _seed(upload_dir, rel, PNG)
        assert not (upload_dir / f"{rel}{media.FINGERPRINT_SUFFIX}").exists()

    groups = media.duplicates(conn, upload_dir=upload_dir)
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert groups[0]["sha256"] == PNG_SHA

    # 懒计算生效：边车指纹被补上了
    for rel in rels:
        sidecar = upload_dir / f"{rel}{media.FINGERPRINT_SUFFIX}"
        assert sidecar.is_file()
        assert sidecar.read_text(encoding="utf-8").strip() == PNG_SHA


# ---------------------------------------------------------------------------
# 页面与清理路由
# ---------------------------------------------------------------------------
def test_images_page_shows_duplicate_hint_and_cleanup_button(upload_dir, auth_client):
    rels = ["2026/09/dup-a.png", "2026/09/dup-b.png", "2026/09/dup-c.png"]
    for rel in rels:
        _seed(upload_dir, rel, PNG)
    note_id = _create_note("引用一份", f"![x](/media/{rels[0]})")
    try:
        html = auth_client.get("/images").text
        assert "内容重复" in html
        assert "与 2 张图内容相同" in html
        assert "重复" in html and "组，可省" in html
        assert 'action="/images/cleanup-duplicates"' in html
        assert "清理多余副本" in html
        assert "data-confirm" in html
        # 被引用的那份要提示「不会自动清理」
        assert "不会自动清理" in html
    finally:
        _purge_note(note_id)


def test_cleanup_route_removes_only_unreferenced(upload_dir, auth_client, csrf):
    rels = ["2026/09/dup-a.png", "2026/09/dup-b.png", "2026/09/dup-c.png"]
    for rel in rels:
        _seed(upload_dir, rel, PNG)
    note_id = _create_note("引用第三份", f"![x](/media/{rels[2]})")
    try:
        response = auth_client.post(
            "/images/cleanup-duplicates",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        location = unquote_plus(response.headers["location"])
        assert "已清理 2 份多余副本" in location
        assert (upload_dir / rels[2]).is_file()
        assert not (upload_dir / rels[0]).exists()
        assert not (upload_dir / rels[1]).exists()
    finally:
        _purge_note(note_id)


def test_cleanup_route_keeps_fully_referenced_group(upload_dir, auth_client, csrf):
    rels = ["2026/09/dup-a.png", "2026/09/dup-b.png"]
    for rel in rels:
        _seed(upload_dir, rel, PNG)
    first = _create_note("笔记 A", f"![x](/media/{rels[0]})")
    second = _create_note("笔记 B", f"![x](/media/{rels[1]})")
    try:
        html = auth_client.get("/images").text
        assert 'action="/images/cleanup-duplicates"' not in html

        response = auth_client.post(
            "/images/cleanup-duplicates",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "没有可自动清理的重复副本" in unquote_plus(response.headers["location"])
        assert all((upload_dir / rel).is_file() for rel in rels)
    finally:
        _purge_note(first)
        _purge_note(second)


def test_cleanup_route_requires_csrf(upload_dir, auth_client):
    rel = "2026/09/dup-a.png"
    _seed(upload_dir, rel, PNG)
    response = auth_client.post("/images/cleanup-duplicates", data={}, follow_redirects=False)
    assert response.status_code == 403
    assert (upload_dir / rel).is_file()
