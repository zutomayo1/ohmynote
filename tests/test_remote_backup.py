"""远端备份（WebDAV）：用真服务器跑真上传。

为什么自己搭一个假 WebDAV：远端备份的价值全在「真的把文件传出去了、真的能清理、
真的没把密钥传出去」—— 打桩（mock）只能证明我调了某个函数，证明不了这些。
假服务器只用标准库 http.server，支持 PUT / PROPFIND / MKCOL / DELETE + Basic 认证，
和真网盘的行为一致（含 401、409、目录不存在）。
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import re
import sqlite3
import threading
import urllib.parse
from pathlib import Path

import pytest

from app import db as db_mod
from app.services import db_backup, remote_backup


# ---------------------------------------------------------------------------
# 假 WebDAV 服务器
# ---------------------------------------------------------------------------


class _FakeWebDAV(http.server.BaseHTTPRequestHandler):
    root: Path
    user: str = ""
    password: str = ""
    uploads: list
    deletes: list
    mkcols: list
    missing_parent: int = 409      # 父目录不存在时回什么（坚果云实测回 404）

    def log_message(self, *args):  # 静音
        pass

    # ---- 工具 ----
    def _auth_ok(self) -> bool:
        if not self.user and not self.password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:  # noqa: BLE001
            return False
        return raw == f"{self.user}:{self.password}"

    def _deny(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="dav"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _target(self) -> Path:
        rel = urllib.parse.unquote(self.path.split("?", 1)[0]).strip("/")
        return self.root / rel if rel else self.root

    def _respond(self, status: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    # ---- 方法 ----
    def do_PUT(self):  # noqa: N802
        if not self._auth_ok():
            return self._deny()
        payload = self._body()
        target = self._target()
        if not target.parent.is_dir():
            return self._respond(type(self).missing_parent)   # 409 = Apache，404 = 坚果云
        target.write_bytes(payload)
        type(self).uploads.append(target.name)
        self._respond(201)

    def do_MKCOL(self):  # noqa: N802
        if not self._auth_ok():
            return self._deny()
        target = self._target()
        if target.is_dir():
            return self._respond(405)          # 已存在
        if not target.parent.is_dir():
            return self._respond(409)
        target.mkdir()
        type(self).mkcols.append(target.name)
        self._respond(201)

    def do_DELETE(self):  # noqa: N802
        if not self._auth_ok():
            return self._deny()
        target = self._target()
        if not target.is_file():
            return self._respond(404)
        target.unlink()
        type(self).deletes.append(target.name)
        self._respond(204)

    def do_PROPFIND(self):  # noqa: N802
        if not self._auth_ok():
            return self._deny()
        self._body()
        target = self._target()
        if not target.is_dir():
            return self._respond(404)
        prefix = self.path.rstrip("/")
        entries = [prefix + "/"]
        for child in sorted(target.iterdir()):
            entries.append(f"{prefix}/{urllib.parse.quote(child.name)}")
        parts = ['<?xml version="1.0" encoding="utf-8"?>',
                 '<d:multistatus xmlns:d="DAV:">']
        for href in entries:
            parts.append(
                f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
                f"<d:getcontentlength>0</d:getcontentlength>"
                f"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            )
        parts.append("</d:multistatus>")
        self._respond(207, "".join(parts).encode("utf-8"), "application/xml")

    def do_GET(self):  # noqa: N802
        if not self._auth_ok():
            return self._deny()
        target = self._target()
        if not target.is_file():
            return self._respond(404)
        self._respond(200, target.read_bytes(), "application/octet-stream")


def _start_dav(root: Path, **attrs):
    root.mkdir(parents=True, exist_ok=True)
    handler = type(
        "Handler", (_FakeWebDAV,),
        {"root": root, "user": "alice", "password": "s3cret",
         "uploads": [], "deletes": [], "mkcols": [], **attrs},
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, {
        "url": f"http://127.0.0.1:{server.server_address[1]}/",
        "dir": root,
        "handler": handler,
    }


def _stop_dav(server):
    server.shutdown()
    server.server_close()


@pytest.fixture()
def dav(tmp_path):
    """假 WebDAV（Apache 风格：父目录缺失回 409）。用户名 alice / 密码 s3cret。"""
    server, info = _start_dav(tmp_path / "dav")
    try:
        yield info
    finally:
        _stop_dav(server)


@pytest.fixture()
def dav404(tmp_path):
    """假 WebDAV（**坚果云风格：父目录缺失回 404**）—— 实测踩到的口径差异。"""
    server, info = _start_dav(tmp_path / "dav404", missing_parent=404)
    try:
        yield info
    finally:
        _stop_dav(server)


def _cfg(dav, **over) -> dict:
    cfg = {
        "url": dav["url"], "user": "alice", "password": "s3cret",
        "interval_hours": 24, "keep": 7, "full": False,
    }
    cfg.update(over)
    return cfg


def _save(conn, dav, **over) -> dict:
    return remote_backup.save(conn, _cfg(dav, **over))


def _remote_names(dav) -> set[str]:
    return {p.name for p in dav["dir"].iterdir()}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def test_save_and_load_config(auth_client, dav):
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        saved = _save(conn, dav, interval_hours=6, keep=3)
        assert saved["url"].endswith("/")
        cfg = remote_backup.config(conn)
        assert (cfg["user"], cfg["password"], cfg["interval_hours"], cfg["keep"]) == ("alice", "s3cret", 6, 3)
        assert remote_backup.is_configured(conn=conn)

        # 密码留空 = 保持原值（不能因为「没改密码」就把口令清了）
        remote_backup.save(conn, {"url": dav["url"], "user": "alice", "password": "",
                                  "interval_hours": "6", "keep": "3"})
        assert remote_backup.config(conn)["password"] == "s3cret"

        remote_backup.reset(conn)
        assert not remote_backup.is_configured(conn=conn)
        assert remote_backup.config(conn)["password"] == ""


def test_validate_rejects_bad_input():
    with pytest.raises(remote_backup.RemoteError):
        remote_backup.validate({"url": "ftp://x/y", "interval_hours": "24", "keep": "7"})
    with pytest.raises(remote_backup.RemoteError):
        remote_backup.validate({"url": "https://x/y", "interval_hours": "9999", "keep": "7"})
    with pytest.raises(remote_backup.RemoteError):
        remote_backup.validate({"url": "https://x/y", "interval_hours": "24", "keep": "0"})
    # 合法值原样通过，并统一补上结尾斜杠
    clean = remote_backup.validate({"url": "https://x/y", "interval_hours": "", "keep": ""})
    assert clean["url"] == "https://x/y/" and clean["interval_hours"] == 24 and clean["keep"] == 7


def test_mask_never_reveals_password():
    assert remote_backup.mask("") == ""
    assert remote_backup.mask("short") == "•" * 5
    long = remote_backup.mask("s3cret-abcdefgh")
    assert "s3cret" not in long and "abcdefgh" not in long


# ---------------------------------------------------------------------------
# 试连
# ---------------------------------------------------------------------------


def _probe_steady(cfg: dict, tries: int = 3) -> dict:
    """并行负载下本机回环偶发连不上（实测 -n8 约 1/10）：只对传输层失败重试。

    这条测的是「401 要给人话」这类语义，不是「本机套接字永远稳定」——所以只在
    消息是「连不上远端 / 网络错误」时重试；401 / 404 是服务器的确定答复，直接返回。
    真回归（probe 永远连不上）三次都失败，照样红，不会被这里掩盖。
    """
    result: dict = {}
    for _ in range(tries):
        result = remote_backup.probe(cfg)
        msg = result.get("message", "")
        if "连不上远端" not in msg and "网络错误" not in msg:
            return result
    return result


def test_probe_ok_and_auth_failure(auth_client, dav):
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        good = _probe_steady(_cfg(dav))
        assert good["ok"], good["message"]

        bad = _probe_steady(_cfg(dav, password="wrong"))
        assert not bad["ok"]
        assert "密码" in bad["message"], bad["message"]

        # 地址不可达：要给出人话，而不是把 traceback 抛出来
        dead = remote_backup.probe({"url": "http://127.0.0.1:9/", "user": "", "password": ""})
        assert not dead["ok"] and dead["message"]


def test_probe_is_read_only(auth_client, dav):
    with db_mod.db() as conn:
        remote_backup.probe(_cfg(dav))
    assert dav["handler"].uploads == [] and dav["handler"].deletes == []


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------


@pytest.fixture()
def secret_meta():
    """塞几个假密钥来验证「远端副本默认脱敏」。

    **必须还原** ``account.password_hash`` —— 这个会话里所有测试共用一个库，直接盖掉
    会让后面所有用例登录失败（第一版就这么把 login 打红了）。
    """
    from app import repo

    with db_mod.db() as conn:
        original = repo.get_meta_map(conn).get("account.password_hash", "")
        repo.save_meta_map(conn, {"api_key": "sk-SECRET-123", "base_url": "https://api.example.com/v1"}, "ai.")
        repo.save_meta_map(conn, {"password_hash": "pbkdf2$SECRET$hash"}, "account.")
        conn.commit()
    try:
        yield
    finally:
        with db_mod.db() as conn:
            repo.save_meta_map(conn, {"password_hash": original}, "account.")
            conn.commit()


def _meta_of(db_file) -> dict:
    """读一份快照/副本里的 meta（测试里用来检查脱敏结果）。"""
    conn = sqlite3.connect(str(db_file))
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def test_upload_lands_and_is_sanitized(auth_client, dav, secret_meta):
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        # 先造一份「确定含密钥」的最新快照 —— 否则下面的断言可能是空的（密钥压根不在快照里）
        snap = db_backup.create_snapshot(conn, reason="manual")
        source_meta = _meta_of(snap["path"])
        assert source_meta.get("ai.api_key") == "sk-SECRET-123", "测试前提不成立：快照里没有密钥"

        _save(conn, dav)
        result = remote_backup.run_upload(conn, trigger="manual")
        assert result["ok"], result["message"]
        assert result["name"] == snap["name"], "上传的应该是本地最新那份快照"
        assert result["name"] in _remote_names(dav), "文件没传到假 WebDAV 上"
        assert result["sanitized"] is True

        rows = _meta_of(dav["dir"] / result["name"])
        assert rows.get("ai.api_key", "") == "", "AI 密钥被传出去了！"
        assert rows.get("ai.base_url", "") == "https://api.example.com/v1", "不该动无关配置"
        assert rows.get("account.password_hash", "") == "", "登录口令哈希被传出去了！"
        assert rows.get("backup.remote.password", "") == "", "WebDAV 口令被传出去了！"


def test_upload_full_mode_keeps_secrets(auth_client, dav, secret_meta):
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        db_backup.create_snapshot(conn, reason="manual")
        _save(conn, dav, full=True)
        result = remote_backup.run_upload(conn, trigger="manual")
        assert result["ok"] and result["sanitized"] is False
        assert _meta_of(dav["dir"] / result["name"]).get("ai.api_key") == "sk-SECRET-123", \
            "完整备份模式应保留密钥"


def test_upload_creates_missing_directory(auth_client, dav):
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        _save(conn, dav, url=dav["url"] + "sub/dir/")
        result = remote_backup.run_upload(conn, trigger="manual")
        assert result["ok"], result["message"]
        assert (dav["dir"] / "sub" / "dir" / result["name"]).is_file()


def test_upload_self_heals_when_server_returns_404(auth_client, dav404):
    """坚果云对「目录不存在」回 404（不是 409）—— 也必须自动建目录后重试。

    这是真实故障：用户填了 …/dav/inknote/，库里/网盘上都没有这个目录，
    之前只认 409 → 直接报「地址不存在（404）」，看起来像密码或地址错了。
    """
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        _save(conn, dav404, url=dav404["url"] + "inknote/")
        result = remote_backup.run_upload(conn, trigger="manual")
        assert result["ok"], result["message"]
        assert (dav404["dir"] / "inknote" / result["name"]).is_file(), "目录没建出来或文件没传上"
        assert "inknote" in dav404["handler"].mkcols


def test_upload_creates_nested_levels(auth_client, dav):
    """多级目录一次建出来（a/b/c）：用户往往直接填深层路径。"""
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        _save(conn, dav, url=dav["url"] + "a/b/c/")
        result = remote_backup.run_upload(conn, trigger="manual")
        assert result["ok"], result["message"]
        assert (dav["dir"] / "a" / "b" / "c" / result["name"]).is_file()
        listing = sorted(str(p.relative_to(dav["dir"])) for p in dav["dir"].rglob("*"))
        walked = dav["dir"]
        for level in ("a", "b", "c"):
            walked = walked / level          # 逐级往下走（写成 root/<level> 是错的）
            assert walked.is_dir(), f"{level} 没建出来；实际目录：{listing}"


def test_chinese_directory_is_percent_encoded(auth_client, dav):
    """中文目录名要转成 %XX：坚果云的根目录就叫「我的坚果云」，且官方明说不支持中文。"""
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        saved = remote_backup.save(conn, _cfg(dav, url=dav["url"] + "我的坚果云/inknote/"))
        assert "%E6%88%91%E7%9A%84%E5%9D%9A%E6%9E%9C%E4%BA%91" in saved["url"], saved["url"]
        result = remote_backup.run_upload(conn, trigger="manual")
        assert result["ok"], result["message"]
        landed = dav["dir"] / "我的坚果云" / "inknote" / result["name"]
        assert landed.is_file(), "中文目录里的文件没落地"


def test_probe_tells_you_which_side_is_missing(auth_client, dav):
    """404 的诊断要能区分「目标目录没建」和「父目录都不存在」。"""
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        # 父目录存在、目标不存在 → 提示可以自动创建
        missing = remote_backup.probe(_cfg(dav, url=dav["url"] + "missing/"))
        assert not missing["ok"]
        assert "父目录是存在的" in missing["message"] and "自动创建" in missing["message"]
        # 父目录也不存在 → 明确说地址写错了
        deep = remote_backup.probe(_cfg(dav, url=dav["url"] + "nope/deep/"))
        assert not deep["ok"]
        assert "父目录也不存在" in deep["message"]


def test_jianguoyun_hint_only_for_jianguoyun():
    """坚果云的目录结构提示只对坚果云地址给，别污染别的服务端。"""
    assert remote_backup._host_hint({"url": "https://dav.jianguoyun.com/dav/x/"})
    assert remote_backup._host_hint({"url": "http://192.168.1.5/dav/"}) == ""
    assert remote_backup._host_hint({"url": "https://cloud.example.com/remote.php/dav/"}) == ""


def test_prune_only_removes_our_own_backups(auth_client, dav):
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        _save(conn, dav, keep=2)
        # 预置 3 份「旧的我们的备份」+ 2 个不该动的文件
        for stamp in ("20200101-010101", "20200102-010101", "20200103-010101"):
            (dav["dir"] / f"inknote-{stamp}-auto.db").write_bytes(b"old")
        (dav["dir"] / "我的重要文件.db").write_bytes(b"mine")
        (dav["dir"] / "readme.txt").write_bytes(b"hi")

        result = remote_backup.run_upload(conn, trigger="manual")
        assert result["ok"], result["message"]
        ours = sorted(n for n in _remote_names(dav) if remote_backup.OUR_NAME.match(n))
        assert len(ours) == 2, f"应只保留 2 份，实际 {ours}"
        assert result["name"] in ours, "刚上传的那份不能被自己清理掉"
        assert (dav["dir"] / "我的重要文件.db").exists(), "不该删别人的文件"
        assert (dav["dir"] / "readme.txt").exists()


def test_failure_is_recorded_not_raised(auth_client):
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        _save(conn, dav={"url": "http://127.0.0.1:9/"})
        before = len(db_backup.list_snapshots(limit=100))
        result = remote_backup.run_upload(conn, trigger="manual")
        assert result["ok"] is False and result["message"]
        status = remote_backup.status(conn)
        assert status and status["ok"] is False and status["message"] == result["message"]
        # 远端失败不该影响本地备份
        assert len(db_backup.list_snapshots(limit=100)) >= before


def test_maybe_remote_backup_respects_interval(auth_client, dav):
    with db_mod.db() as conn:
        remote_backup.reset(conn)
        # 没配 → 什么都不做
        assert remote_backup.maybe_remote_backup(conn) is None
        # 间隔 0 → 只手动
        _save(conn, dav, interval_hours=0)
        assert remote_backup.maybe_remote_backup(conn) is None
        assert dav["handler"].uploads == []
        # 间隔 24 → 第一次上传，紧接着再调就该跳过（幂等）
        _save(conn, dav, interval_hours=24)
        first = remote_backup.maybe_remote_backup(conn)
        assert first and first["ok"] and dav["handler"].uploads
        assert remote_backup.maybe_remote_backup(conn) is None
        assert len(dav["handler"].uploads) == 1


# ---------------------------------------------------------------------------
# 路由与页面
# ---------------------------------------------------------------------------


def test_remote_routes_need_csrf(auth_client, dav):
    response = auth_client.post("/backup/remote/save", data={"url": dav["url"]}, follow_redirects=False)
    assert response.status_code == 403


def test_remote_save_test_run_routes(auth_client, csrf, dav):
    saved = auth_client.post(
        "/backup/remote/save",
        data={"_csrf": csrf, "url": dav["url"], "user": "alice", "password": "s3cret",
              "interval_hours": "12", "keep": "5"},
        follow_redirects=False,
    )
    assert saved.status_code == 303

    with db_mod.db() as conn:
        cfg = remote_backup.config(conn)
        assert cfg["url"] == dav["url"] and cfg["keep"] == 5
        assert cfg["interval_hours"] == 12

    tested = auth_client.post("/backup/remote/test", data={"_csrf": csrf, "url": dav["url"],
                                                           "user": "alice", "password": "s3cret",
                                                           "interval_hours": "12", "keep": "5"},
                              follow_redirects=False)
    assert tested.status_code == 303
    assert "连接正常" in urllib.parse.unquote(tested.headers["location"])

    ran = auth_client.post("/backup/remote/run", data={"_csrf": csrf}, follow_redirects=False)
    assert ran.status_code == 303
    assert dav["handler"].uploads, "「立即上传」没真的传"
    assert "已上传" in urllib.parse.unquote(ran.headers["location"])

    cleared = auth_client.post("/backup/remote/reset", data={"_csrf": csrf}, follow_redirects=False)
    assert cleared.status_code == 303
    with db_mod.db() as conn:
        assert not remote_backup.is_configured(conn=conn)


def test_remote_test_returns_json_for_fetch(auth_client, csrf, dav):
    """「测试连接」在页面里走 fetch（回 JSON、不整页刷新）；无 JS 时仍回 303。"""
    form = {"_csrf": csrf, "url": dav["url"], "user": "alice", "password": "s3cret",
            "interval_hours": "24", "keep": "7"}

    fetched = auth_client.post("/backup/remote/test", data=form,
                               headers={"X-Requested-With": "fetch"})
    assert fetched.status_code == 200
    payload = fetched.json()
    assert payload["ok"] is True and "连接正常" in payload["message"]

    plain = auth_client.post("/backup/remote/test", data=form, follow_redirects=False)
    assert plain.status_code == 303, "没有 fetch 头时应保持表单回退"


def test_test_button_is_ajax_not_formaction(auth_client, dav):
    """按钮必须是 type=button + data-remote-test；改回 formaction 会整页刷新，把刚填的口令冲掉。"""
    page = auth_client.get("/backup").text
    assert "data-remote-test" in page
    assert 'id="remote-status"' in page
    assert 'formaction="/backup/remote/test"' not in page
    assert re.search(r'<button[^>]*type="button"[^>]*data-remote-test', page), "按钮应是 type=button"


def test_remote_test_returns_json_for_fetch(auth_client, csrf, dav):
    """「测试连接」在页面里走 fetch（回 JSON、不整页刷新）；无 JS 时仍回 303。"""
    form = {"_csrf": csrf, "url": dav["url"], "user": "alice", "password": "s3cret",
            "interval_hours": "24", "keep": "7"}

    fetched = auth_client.post("/backup/remote/test", data=form,
                               headers={"X-Requested-With": "fetch"})
    assert fetched.status_code == 200
    payload = fetched.json()
    assert payload["ok"] is True and "连接正常" in payload["message"]

    plain = auth_client.post("/backup/remote/test", data=form, follow_redirects=False)
    assert plain.status_code == 303, "没有 fetch 头时应保持表单回退"


def test_test_button_is_ajax_not_formaction(auth_client, dav):
    """按钮必须是 type=button + data-remote-test；改回 formaction 会整页刷新，把刚填的口令冲掉。"""
    page = auth_client.get("/backup").text
    assert "data-remote-test" in page
    assert 'id="remote-status"' in page
    assert 'formaction="/backup/remote/test"' not in page
    assert re.search(r'<button[^>]*type="button"[^>]*data-remote-test', page), "按钮应是 type=button"


def test_backup_page_preserves_form_values_without_echoing_password(auth_client, csrf, dav):
    secret = "s3cret-abcdefgh"
    auth_client.post(
        "/backup/remote/save",
        data={"_csrf": csrf, "url": dav["url"] + "inknote/", "user": "alice", "password": secret,
              "interval_hours": "12", "keep": "5"},
        follow_redirects=False,
    )
    page = auth_client.get("/backup").text
    assert secret not in page, "页面上回显了完整口令！"
    assert remote_backup.mask(secret) in page, "应该只显示掩码"
    assert dav["url"] + "inknote/" in page
    assert 'formaction="/backup/remote/run"' in page
    assert 'data-confirm' in page


def test_invalid_save_flashes_instead_of_writing(auth_client, csrf, dav):
    with db_mod.db() as conn:
        remote_backup.reset(conn)
    response = auth_client.post(
        "/backup/remote/save",
        data={"_csrf": csrf, "url": "not-a-url", "interval_hours": "24", "keep": "7"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "保存失败" in urllib.parse.unquote(response.headers["location"])
    with db_mod.db() as conn:
        assert remote_backup.config(conn)["url"] == ""


def test_sanitized_keys_cover_remote_credentials():
    """脱敏清单必须包含远端口令，否则「脱敏副本」会把网盘口令送出去。"""
    assert "backup.remote.password" in db_backup.SANITIZED_META_KEYS
    assert "backup.remote.user" in db_backup.SANITIZED_META_KEYS


def test_export_sanitized_copy_does_not_touch_source(tmp_path, secret_meta):
    with db_mod.db() as conn:
        created = db_backup.create_snapshot(conn, reason="manual")
    source = Path(created["path"])
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    dest = tmp_path / "copy.db"
    db_backup.export_sanitized_copy(source, dest)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before, "脱敏不该动源文件"
    assert dest.exists()
    assert _meta_of(dest).get("ai.api_key", "") == ""
