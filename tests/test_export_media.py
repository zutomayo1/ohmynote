"""导出带图：/export/zip 带 media/ 图片、导入还原、幂等与路径安全。

照着 tests/test_import.py 的写法：用独立临时库 + monkeypatch settings.upload_dir，
不碰共享的数据目录。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from app import db as db_mod, repo
from app.config import settings
from app.routers import pages
from app.services import importer

# 两张内容不同的 PNG 字节（不做图片内容校验，只验证字节一致）
IMG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 64
IMG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 96
IMAGES = {
    "2026/09/roundtrip-a.png": IMG_A,
    "2026/09/roundtrip-b.png": IMG_B,
}


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------
def _make_export(tmp_path, monkeypatch, images: dict[str, bytes] | None = None) -> bytes:
    """造一个带图笔记的源库 + uploads，调用 /export/zip 处理函数返回 zip 字节。"""
    images = IMAGES if images is None else images
    uploads = tmp_path / "src-uploads"
    for rel, data in images.items():
        path = uploads / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    db_path = tmp_path / "source.db"
    db_mod.init_db(db_path)
    with db_mod.db(db_path) as conn:
        content = "\n\n".join(f"![{Path(rel).name}](/media/{rel})" for rel in images)
        repo.create_note(conn, title="带图笔记", content=content)
        monkeypatch.setattr(settings, "upload_dir", uploads)
        return pages.export_zip(conn=conn).body


def _fresh_conn(tmp_path, name: str = "target.db"):
    db_path = tmp_path / name
    db_mod.init_db(db_path)
    return db_mod.db(db_path)


# ---------------------------------------------------------------------------
# 1. 往返（最重要）
# ---------------------------------------------------------------------------
def test_zip_roundtrip_carries_media(tmp_path, monkeypatch):
    payload = _make_export(tmp_path, monkeypatch)

    archive = zipfile.ZipFile(io.BytesIO(payload))
    names = set(archive.namelist())
    for rel, data in IMAGES.items():
        assert f"media/{rel}" in names
        assert archive.read(f"media/{rel}") == data
    assert "BACKUP-README.txt" in names

    manifest = json.loads(archive.read("notes.json").decode("utf-8"))
    assert manifest["media"] == sorted(IMAGES)

    # 导进新库 + 新 uploads
    target_uploads = tmp_path / "target-uploads"
    monkeypatch.setattr(settings, "upload_dir", target_uploads)
    with _fresh_conn(tmp_path) as conn:
        result = importer.sniff_and_import(conn, "backup.zip", payload)

    assert result["errors"] == []
    assert result["media_added"] == 2
    assert result["media_skipped"] == 0
    assert result["media_total"] == 2
    for rel, data in IMAGES.items():
        assert (target_uploads / rel).read_bytes() == data


# ---------------------------------------------------------------------------
# 2. 幂等
# ---------------------------------------------------------------------------
def test_zip_import_media_is_idempotent(tmp_path, monkeypatch):
    payload = _make_export(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "target-uploads")

    with _fresh_conn(tmp_path) as conn:
        first = importer.sniff_and_import(conn, "backup.zip", payload)
        second = importer.sniff_and_import(conn, "backup.zip", payload)

    assert first["media_added"] == 2 and first["media_skipped"] == 0
    assert second["media_added"] == 0 and second["media_skipped"] == 2
    assert second["errors"] == []


# ---------------------------------------------------------------------------
# 3. 路径穿越 / 绝对路径 / 盘符
# ---------------------------------------------------------------------------
def test_import_rejects_path_traversal(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(settings, "upload_dir", uploads)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "notes/evil.md",
            "---\ntitle: 恶意包\n---\n\n正文",
        )
        archive.writestr(
            "notes.json",
            json.dumps({"notes": [{"title": "恶意包", "file": "notes/evil.md"}]}),
        )
        archive.writestr("media/../../.env", b"SECRET=1")
        archive.writestr("media/C:/x.png", b"not-an-image")
        archive.writestr("media//abs.png", b"not-an-image")
        link = zipfile.ZipInfo("media/link.png")
        link.external_attr = (0o120777 << 16)  # 声明成 Unix 符号链接
        archive.writestr(link, "../../outside.png")

    with _fresh_conn(tmp_path, "evil.db") as conn:
        result = importer.sniff_and_import(conn, "evil.zip", buffer.getvalue())

    assert result["media_added"] == 0
    assert result["errors"], "越界路径应该进 errors"
    # 不能写到 uploads 外面
    assert not (uploads / "../../.env").resolve().exists()
    # uploads 里也不该凭空多出文件
    assert not uploads.exists() or not any(item.is_file() for item in uploads.rglob("*"))


# ---------------------------------------------------------------------------
# 4. 同名不同内容不覆盖
# ---------------------------------------------------------------------------
def test_import_keeps_conflicting_media(tmp_path, monkeypatch):
    payload = _make_export(tmp_path, monkeypatch, {"2026/09/dup.png": b"NEW-CONTENT"})
    uploads = tmp_path / "target-uploads"
    existing = uploads / "2026" / "09" / "dup.png"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"OLD-CONTENT")
    monkeypatch.setattr(settings, "upload_dir", uploads)

    with _fresh_conn(tmp_path, "dup.db") as conn:
        result = importer.sniff_and_import(conn, "dup.zip", payload)

    assert existing.read_bytes() == b"OLD-CONTENT"
    assert (existing.parent / "dup-1.png").read_bytes() == b"NEW-CONTENT"
    assert result["media_added"] == 1
    assert result["media_skipped"] == 0
    assert result["media_renamed"] == 1


# ---------------------------------------------------------------------------
# 5. notes.json 的 media 键 + 老 JSON 兼容
# ---------------------------------------------------------------------------
def test_notes_json_has_media_and_old_json_still_imports(tmp_path, monkeypatch):
    payload = _make_export(tmp_path, monkeypatch)
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(payload)).read("notes.json").decode("utf-8"))
    assert "media" in manifest and isinstance(manifest["media"], list)

    old_json = json.dumps(
        {"notes": [{"title": "老结构", "content": "没有 media 字段"}]},
        ensure_ascii=False,
    ).encode("utf-8")
    with _fresh_conn(tmp_path, "old.db") as conn:
        result = importer.sniff_and_import(conn, "old.json", old_json)

    assert result["errors"] == []
    assert result["created"] == 1
    assert result["media_added"] == 0
    assert result["media_skipped"] == 0


def test_import_json_recognises_media_field(tmp_path):
    """JSON 只有文件名清单、没有图片数据：认出来但不写文件。"""
    payload = json.dumps(
        {"notes": [{"title": "带清单", "content": "x"}], "media": ["a.png", "b/c.jpg"]},
        ensure_ascii=False,
    ).encode("utf-8")
    with _fresh_conn(tmp_path, "json-media.db") as conn:
        result = importer.sniff_and_import(conn, "export.json", payload)

    assert result["media_total"] == 2
    assert result["media_added"] == 0
    assert result["media_skipped"] == 0
