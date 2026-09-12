"""数据库自动备份：快照完整性 / 列表 / 幂等 / 滚动保留 / 回滚 / 目录穿越 / HTTP。

只用 TestClient（进程内），不起服务。
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import unquote_plus

import pytest
from fastapi.testclient import TestClient

from app import db as db_mod, repo
from app.services import db_backup


@pytest.fixture(autouse=True)
def isolate_backups():
    """每个用例：开始前清掉历史自动备份，结束后删掉本用例新建的备份。

    data 目录是整个测试会话共享的，这样其它测试文件（如 test_import）不受影响，
    本文件里的「数量」断言也不会被别的用例带跑。
    """
    directory = db_backup.backup_dir()
    before = {path.name for path in directory.glob("*.db")}
    for item in db_backup.list_snapshots(limit=1000):
        if item["reason"] == "auto":
            db_backup.delete_snapshot(item["name"])
    yield
    for path in directory.glob("*.db"):
        if path.name not in before:
            try:
                path.unlink()
            except OSError:
                pass


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "backup-test.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


# ---------------------------------------------------------------------------
# 1. 快照本身是有效的 SQLite，且包含现有笔记
# ---------------------------------------------------------------------------
def test_snapshot_is_valid_sqlite_and_contains_notes(conn):
    repo.create_note(conn, title="备份里能看到的笔记", content="正文内容")
    conn.commit()

    result = db_backup.create_snapshot(conn, reason="manual")

    path = Path(result["path"])
    assert Path(result["name"]).name == result["name"]
    assert path.is_file()
    assert result["size"] == path.stat().st_size > 0

    check = sqlite3.connect(str(path))
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        titles = {row[0] for row in check.execute("SELECT title FROM notes")}
    finally:
        check.close()
    assert "备份里能看到的笔记" in titles


# ---------------------------------------------------------------------------
# 2. 列表按时间倒序、数量正确
# ---------------------------------------------------------------------------
def test_list_snapshots_newest_first(conn):
    before = len(db_backup.list_snapshots(limit=1000))
    made: list[str] = []
    for _ in range(3):
        made.append(db_backup.create_snapshot(conn, reason="manual")["name"])
        time.sleep(0.05)

    items = db_backup.list_snapshots(limit=1000)
    assert len(items) == before + 3
    assert set(made) <= {item["name"] for item in items}

    stamps = [item["mtime"] for item in items]
    assert stamps == sorted(stamps, reverse=True)
    assert items[0]["name"] == made[-1]  # 最后创建的排最前


# ---------------------------------------------------------------------------
# 3. maybe_auto_backup 幂等；超过间隔才会再生成
# ---------------------------------------------------------------------------
def test_maybe_auto_backup_is_idempotent(conn):
    first = db_backup.maybe_auto_backup(conn, interval_hours=24)
    assert first is not None
    assert first["name"] in {item["name"] for item in db_backup.list_snapshots(limit=1000)}

    assert db_backup.maybe_auto_backup(conn, interval_hours=24) is None
    assert db_backup.maybe_auto_backup(conn, interval_hours=1) is None
    # 非法间隔退回默认 24h，不能变成「每次都备份」
    assert db_backup.maybe_auto_backup(conn, interval_hours=0) is None


def test_maybe_auto_backup_creates_when_stale(conn):
    first = db_backup.create_snapshot(conn, reason="auto")
    path = db_backup.backup_dir() / first["name"]
    old = time.time() - 3 * 24 * 3600
    os.utime(path, (old, old))

    again = db_backup.maybe_auto_backup(conn, interval_hours=24)

    assert again is not None
    assert again["name"] != first["name"]
    assert "auto" in again["name"]


# ---------------------------------------------------------------------------
# 4. 滚动保留：keep=2 时自动只留 2 份，手动 / 回滚前不动
# ---------------------------------------------------------------------------
def test_rolling_keep_never_deletes_manual(conn):
    manual = db_backup.create_snapshot(conn, reason="manual", keep=2)

    last = None
    for _ in range(5):
        last = db_backup.create_snapshot(conn, reason="auto", keep=2)
        time.sleep(0.05)

    items = db_backup.list_snapshots(limit=1000)
    names = {item["name"] for item in items}
    auto_items = [item for item in items if item["reason"] == "auto"]
    assert len(auto_items) == 2
    assert last is not None and last["removed"], "第 5 份自动备份应当触发滚动清理"
    # 保留的是最近的两份：刚生成的那份一定还在（文件名会复用，所以不按名字下标断言）
    assert last["name"] in {item["name"] for item in auto_items}
    assert manual["name"] in names

    safety = db_backup.create_snapshot(conn, reason="before-restore", keep=2)
    items = db_backup.list_snapshots(limit=1000)
    names = {item["name"] for item in items}
    assert len([item for item in items if item["reason"] == "auto"]) == 2
    assert safety["name"] in names
    assert manual["name"] in names


# ---------------------------------------------------------------------------
# 5. 回滚真的生效，且回滚前的现场被另存
# ---------------------------------------------------------------------------
def test_restore_reverts_title_and_keeps_safety(conn):
    note = repo.create_note(conn, title="回滚前标题", content="原始正文")
    conn.commit()
    snap = db_backup.create_snapshot(conn, reason="manual")

    repo.update_note(conn, note["id"], title="改过的标题", content="改过的正文")
    conn.commit()
    assert repo.get_note(conn, note["id"])["title"] == "改过的标题"

    result = db_backup.restore_snapshot(conn, snap["name"])

    assert result["restored"] == snap["name"]
    assert result["safety"] != snap["name"]
    restored = repo.get_note(conn, note["id"])
    assert restored["title"] == "回滚前标题"
    assert restored["content"] == "原始正文"

    safety_path = db_backup.backup_dir() / result["safety"]
    assert safety_path.is_file()
    check = sqlite3.connect(str(safety_path))
    try:
        titles = {row[0] for row in check.execute("SELECT title FROM notes")}
    finally:
        check.close()
    assert "改过的标题" in titles  # 现场备份里是「回滚前」的新数据


def test_restore_missing_snapshot_raises(conn):
    with pytest.raises(FileNotFoundError):
        db_backup.restore_snapshot(conn, "inknote-29991231-000000-manual.db")


# ---------------------------------------------------------------------------
# 6. 目录穿越 / 非法文件名一律拒绝
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_name",
    [
        "../../.env",
        "..\\x.db",
        "C:\\Windows\\x.db",
        "/etc/passwd.db",
        "sub/dir.db",
        "..",
        "",
        "x.txt",
    ],
)
def test_unsafe_names_are_rejected(conn, bad_name):
    assert db_backup.is_safe_snapshot_name(bad_name) is False
    with pytest.raises(ValueError):
        db_backup.restore_snapshot(conn, bad_name)
    with pytest.raises(ValueError):
        db_backup.delete_snapshot(bad_name)


def test_traversal_does_not_touch_outside_file():
    sentinel = db_backup.backup_dir().parent / "sentinel-outside.db"
    sentinel.write_text("keep me", encoding="utf-8")
    try:
        with pytest.raises(ValueError):
            db_backup.delete_snapshot(f"../{sentinel.name}")
        assert sentinel.read_text(encoding="utf-8") == "keep me"
    finally:
        sentinel.unlink(missing_ok=True)


def test_http_traversal_is_rejected(auth_client, csrf):
    """HTTP 层再挡一遍：URL 编码的 / 、反斜杠、绝对路径都不能碰。"""
    gets = ["/backup/download/..%2F..%2F.env", "/backup/download/C:%5CWindows%5Cx.db"]
    for path in gets:
        response = auth_client.get(path, follow_redirects=False)
        assert response.status_code in (400, 404), (path, response.status_code)

    posts = ["/backup/delete/..%2F..%2F.env", "/backup/restore/..%2Fx.db"]
    for path in posts:
        response = auth_client.post(path, data={"_csrf": csrf}, follow_redirects=False)
        assert response.status_code in (400, 404), (path, response.status_code)


# ---------------------------------------------------------------------------
# 7. HTTP：登录 / CSRF / 页面 / 下载 / 删除 / 回滚
# ---------------------------------------------------------------------------
def test_backup_page_requires_login():
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/backup", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_backup_posts_without_csrf_are_rejected(auth_client):
    for path in ("/backup/create", "/backup/delete/x.db", "/backup/restore/x.db"):
        response = auth_client.post(path, data={}, follow_redirects=False)
        assert response.status_code == 403, path


def test_backup_page_shows_db_section_and_keeps_import_block(auth_client, conn):
    snap = db_backup.create_snapshot(conn, reason="manual")
    page = auth_client.get("/backup")
    assert page.status_code == 200
    html = page.text
    assert "/backup/create" in html
    assert snap["name"] in html
    assert "data/backups" in html
    assert "直接拷走" in html
    # 上一轮的导入区块必须原样保留
    assert 'action="/backup/import"' in html
    assert 'enctype="multipart/form-data"' in html
    assert "绝不删除" in html


def test_manual_backup_via_http(auth_client, csrf):
    before = len(db_backup.list_snapshots(limit=1000))
    response = auth_client.post("/backup/create", data={"_csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    items = db_backup.list_snapshots(limit=1000)
    assert len(items) == before + 1
    assert items[0]["reason"] == "manual"
    assert "已生成备份" in unquote_plus(response.headers["location"])


def test_download_snapshot(auth_client, conn):
    snap = db_backup.create_snapshot(conn, reason="manual")
    response = auth_client.get(f"/backup/download/{snap['name']}")
    assert response.status_code == 200
    assert response.content == Path(snap["path"]).read_bytes()
    assert snap["name"] in response.headers.get("content-disposition", "")


def test_delete_snapshot_via_http(auth_client, conn, csrf):
    snap = db_backup.create_snapshot(conn, reason="manual")
    response = auth_client.post(
        f"/backup/delete/{snap['name']}", data={"_csrf": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    assert not (db_backup.backup_dir() / snap["name"]).exists()
    assert db_backup.delete_snapshot(snap["name"]) is False  # 再删返回 False


def test_restore_snapshot_via_http(auth_client, csrf):
    with db_mod.db() as connection:
        note = repo.create_note(connection, title="HTTP回滚-旧标题", content="旧正文")
        note_id = note["id"]
    with db_mod.db() as connection:
        snap = db_backup.create_snapshot(connection, reason="manual")

    with db_mod.db() as connection:
        repo.update_note(connection, note_id, title="HTTP回滚-新标题")
        assert repo.get_note(connection, note_id)["title"] == "HTTP回滚-新标题"

    try:
        response = auth_client.post(
            f"/backup/restore/{snap['name']}", data={"_csrf": csrf}, follow_redirects=False
        )
        assert response.status_code == 303
        location = unquote_plus(response.headers["location"])
        assert "已回滚到" in location
        assert "另存为" in location

        with db_mod.db() as connection:
            assert repo.get_note(connection, note_id)["title"] == "HTTP回滚-旧标题"
    finally:
        with db_mod.db() as connection:
            repo.purge(connection, note_id)

# ---------------------------------------------------------------------------
# 回归：调用方连接还挂着未提交的写事务时，备份不能自己锁死自己
# ---------------------------------------------------------------------------
def test_create_snapshot_does_not_deadlock_with_open_transaction(tmp_path):
    """真实踩过的坑：应用启动时（lifespan 里刚写完 meta）调备份，
    `conn.backup(dest)` 会永久挂住 —— 表现是服务根本起不来。
    这里用线程 + join 超时，让「卡住」变成断言失败而不是把整个测试挂死。
    """
    import threading

    from app import db as db_mod
    from app.services import db_backup

    db_path = tmp_path / "deadlock.db"
    db_mod.init_db(db_path)

    outcome: dict = {}

    def worker() -> None:
        try:
            with db_mod.db(db_path) as conn:
                # 故意做一次「未提交」的写：模拟 lifespan 里的 bootstrap 之后
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('deadlock.probe', '1')"
                    " ON CONFLICT(key) DO UPDATE SET value='1'"
                )
                # 不 commit，直接备份
                outcome["snapshot"] = db_backup.create_snapshot(conn, reason="manual", keep=7)
        except Exception as exc:  # noqa: BLE001 - 记录下来给断言看
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=30)

    assert not thread.is_alive(), "create_snapshot 在调用方有未提交写事务时挂住了（又自锁了）"
    assert "error" not in outcome, outcome.get("error")
    snapshot = outcome.get("snapshot") or {}
    assert snapshot.get("name"), snapshot
    assert snapshot.get("size", 0) > 0

    # 快照必须是一份能打开的、完整的库
    import sqlite3

    conn = sqlite3.connect(snapshot["path"])
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0].lower() == "ok"
        assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] >= 0
    finally:
        conn.close()
