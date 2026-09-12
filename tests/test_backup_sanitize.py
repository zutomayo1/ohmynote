"""D 组 · 备份脱敏：普通备份保留密钥、脱敏备份清空密钥、当前库不受影响、可回滚、页面入口。

只用 TestClient（进程内），不起服务。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import unquote_plus

import pytest
from fastapi.testclient import TestClient

from app import db as db_mod, repo
from app.services import db_backup

API_KEY = "sk-test-secret-0123456789"
PASSWORD_HASH = "pbkdf2_sha256$200000$c2FsdA$deadbeef"


@pytest.fixture(autouse=True)
def isolate_backups():
    """与 test_db_backup 一致：开始前清历史自动备份，结束后删掉本用例新建的备份。"""
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
    path = tmp_path / "sanitize-test.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


def _seed(connection: sqlite3.Connection) -> None:
    """写入明文密钥 / 密码哈希，外加几个必须原样保留的普通 meta 键。"""
    db_mod.set_meta(connection, "ai.api_key", API_KEY)
    db_mod.set_meta(connection, "account.password_hash", PASSWORD_HASH)
    db_mod.set_meta(connection, "ai.model", "deepseek-chat")
    db_mod.set_meta(connection, "site.site_title", "脱敏测试站")
    connection.commit()


def _meta_value(path: str | Path, key: str) -> str | None:
    check = sqlite3.connect(str(path))
    try:
        row = check.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    finally:
        check.close()
    return None if row is None else row[0]


def _meta_keys(path: str | Path) -> set[str]:
    check = sqlite3.connect(str(path))
    try:
        return {row[0] for row in check.execute("SELECT key FROM meta")}
    finally:
        check.close()


# ---------------------------------------------------------------------------
# 1. 普通备份：密钥原样保留（别把正常备份也脱敏了）
# ---------------------------------------------------------------------------
def test_normal_backup_keeps_secrets(conn):
    _seed(conn)

    snap = db_backup.create_snapshot(conn, reason="manual")

    assert snap["sanitized"] is False
    assert "-sanitized" not in snap["name"]
    assert _meta_value(snap["path"], "ai.api_key") == API_KEY
    assert _meta_value(snap["path"], "account.password_hash") == PASSWORD_HASH


# ---------------------------------------------------------------------------
# 2. 脱敏备份：两个敏感键变空串，其它 meta 键一个不少
# ---------------------------------------------------------------------------
def test_sanitized_backup_blanks_secrets_but_keeps_other_meta(conn):
    _seed(conn)
    before_keys = {row[0] for row in conn.execute("SELECT key FROM meta")}

    snap = db_backup.create_snapshot(conn, reason="manual", sanitized=True)

    assert snap["sanitized"] is True
    assert _meta_value(snap["path"], "ai.api_key") == ""
    assert _meta_value(snap["path"], "account.password_hash") == ""
    assert _meta_value(snap["path"], "ai.model") == "deepseek-chat"
    assert _meta_value(snap["path"], "site.site_title") == "脱敏测试站"
    assert _meta_keys(snap["path"]) == before_keys
    # 临时文件不能残留
    assert not list(db_backup.backup_dir().glob("*.part"))


def test_sanitized_backup_tolerates_missing_password_hash(conn):
    """密码来自 .env、meta 里没有 account.password_hash 时也不能报错。"""
    db_mod.set_meta(conn, "ai.api_key", API_KEY)
    conn.commit()

    snap = db_backup.create_snapshot(conn, reason="manual", sanitized=True)

    assert _meta_value(snap["path"], "ai.api_key") == ""
    assert _meta_value(snap["path"], "account.password_hash") is None


# ---------------------------------------------------------------------------
# 3. 当前库绝不能被改动（最容易犯的错）
# ---------------------------------------------------------------------------
def test_current_db_is_not_touched(conn, tmp_path):
    _seed(conn)

    db_backup.create_snapshot(conn, reason="manual", sanitized=True)

    # 同一条连接
    assert db_mod.get_meta(conn, "ai.api_key") == API_KEY
    assert db_mod.get_meta(conn, "account.password_hash") == PASSWORD_HASH
    # 另开连接直接读库文件，排除「只是没提交 / 只在会话里看着对」的假象
    assert _meta_value(tmp_path / "sanitize-test.db", "ai.api_key") == API_KEY
    assert _meta_value(tmp_path / "sanitize-test.db", "account.password_hash") == PASSWORD_HASH


# ---------------------------------------------------------------------------
# 4. 文件名 / list_snapshots 的 sanitized 标记
# ---------------------------------------------------------------------------
def test_sanitized_name_and_list_flag(conn):
    _seed(conn)

    snap = db_backup.create_snapshot(conn, reason="manual", sanitized=True)
    normal = db_backup.create_snapshot(conn, reason="manual")

    assert snap["name"].endswith("-manual-sanitized.db")
    assert normal["name"].endswith("-manual.db")
    assert not normal["name"].endswith("-sanitized.db")

    items = {item["name"]: item for item in db_backup.list_snapshots(limit=1000)}
    assert items[snap["name"]]["sanitized"] is True
    assert items[snap["name"]]["reason"] == "manual"
    assert items[normal["name"]]["sanitized"] is False


def test_sanitized_auto_backup_survives_rolling_prune(conn):
    """脱敏备份是「手动 / 分享」性质，即使 reason=auto 也不能被滚动删掉。"""
    safe = db_backup.create_snapshot(conn, reason="auto", keep=1, sanitized=True)
    for _ in range(3):
        db_backup.create_snapshot(conn, reason="auto", keep=1)

    names = {item["name"] for item in db_backup.list_snapshots(limit=1000)}
    assert safe["name"] in names
    assert any(
        item["reason"] == "auto" and not item["sanitized"] for item in db_backup.list_snapshots(limit=1000)
    )


# ---------------------------------------------------------------------------
# 5. 脱敏备份能回滚：笔记完好，密钥为空（AI 未配置，其它功能不受影响）
# ---------------------------------------------------------------------------
def test_sanitized_backup_can_restore(conn):
    note = repo.create_note(conn, title="脱敏回滚-原标题", content="正文还在")
    conn.commit()
    _seed(conn)
    snap = db_backup.create_snapshot(conn, reason="manual", sanitized=True)

    repo.update_note(conn, note["id"], title="改过的标题", content="改过的正文")
    db_mod.set_meta(conn, "ai.api_key", "sk-rotated-key")
    conn.commit()

    result = db_backup.restore_snapshot(conn, snap["name"])

    assert result["restored"] == snap["name"]
    restored = repo.get_note(conn, note["id"])
    assert restored is not None
    assert restored["title"] == "脱敏回滚-原标题"
    assert restored["content"] == "正文还在"
    assert db_mod.get_meta(conn, "ai.api_key") == ""
    assert db_mod.get_meta(conn, "account.password_hash") == ""


# ---------------------------------------------------------------------------
# 6. 页面：风险提示 + 脱敏按钮 + 已脱敏徽章；未登录 / 缺 CSRF 被挡
# ---------------------------------------------------------------------------
def test_backup_page_has_risk_notice_and_sanitized_button(auth_client):
    page = auth_client.get("/backup")
    assert page.status_code == 200
    html = page.text

    assert "/backup/create-sanitized" in html
    assert "脱敏备份" in html
    assert "备份文件里含 AI 密钥等敏感信息" in html
    assert "别随手发人" in html
    assert "『脱敏备份』" in html
    # 不写「加密」：我们没有实现加密，别给用户错误安全感
    assert "加密" not in html
    # 原有的备份 / 导入入口都还在
    assert 'action="/backup/create"' in html
    assert 'action="/backup/import"' in html


def test_backup_page_marks_sanitized_row(auth_client, conn):
    _seed(conn)
    snap = db_backup.create_snapshot(conn, reason="manual", sanitized=True)

    page = auth_client.get("/backup")

    assert page.status_code == 200
    assert snap["name"] in page.text
    assert "已脱敏" in page.text


def test_sanitized_route_requires_login_and_csrf(auth_client):
    from app.main import app

    with TestClient(app) as anon:
        response = anon.post(
            "/backup/create-sanitized", data={"_csrf": "x"}, follow_redirects=False
        )
    assert response.status_code == 303
    assert "/login" in response.headers["location"]

    response = auth_client.post("/backup/create-sanitized", data={}, follow_redirects=False)
    assert response.status_code == 403


def test_sanitized_backup_via_http(auth_client, csrf):
    before = {item["name"] for item in db_backup.list_snapshots(limit=1000)}

    response = auth_client.post(
        "/backup/create-sanitized", data={"_csrf": csrf}, follow_redirects=False
    )

    assert response.status_code == 303
    location = unquote_plus(response.headers["location"])
    assert "脱敏备份" in location
    created = [item for item in db_backup.list_snapshots(limit=1000) if item["name"] not in before]
    assert len(created) == 1
    assert created[0]["sanitized"] is True


def test_download_sanitized_filename_keeps_suffix(auth_client, conn):
    _seed(conn)
    snap = db_backup.create_snapshot(conn, reason="manual", sanitized=True)

    response = auth_client.get(f"/backup/download/{snap['name']}")

    assert response.status_code == 200
    assert "-sanitized" in response.headers.get("content-disposition", "")
