"""A2：设置页改登录密码的测试（只用 TestClient，真实 /login 路由）。

覆盖：
1. 改密码成功 → 旧密码登录失败、新密码登录成功
2. 旧密码错 → 拒绝、库里口令不变、连续错会触发节流
3. 两次新密码不一致 / 太短（7 位）→ 拒绝
4. describe() 不含哈希片段；reset() 后回到 .env 口令
5. 未登录 POST 被挡（303 跳登录）、已登录缺 CSRF 被挡（403）
"""

from __future__ import annotations

import json

import pytest

from app import db as db_mod
from app import repo
from app.deps import login_throttle
from app.services import account

from conftest import PASSWORD

NEW_PASSWORD = "brand-new-secret-42"


@pytest.fixture(autouse=True)
def clean_account(client):
    """每个用例前后都把口令恢复成 .env、清掉节流计数，避免污染其它测试。"""
    login_throttle.reset()
    account.password_throttle.reset()
    with db_mod.db() as conn:
        account.reset(conn)
    yield
    with db_mod.db() as conn:
        account.reset(conn)
    account.password_throttle.reset()
    login_throttle.reset()


def change_password(client, csrf, old, new, confirm=None, **extra):
    data = {
        "_csrf": csrf,
        "old_password": old,
        "new_password": new,
        "confirm_password": new if confirm is None else confirm,
    }
    data.update(extra)
    return client.post("/settings/password", data=data, follow_redirects=False)


def fresh_login(client, password):
    """用一个全新的 TestClient 走真实 /login（不污染会话 cookie）。"""
    fresh = client.__class__(client.app)
    return fresh.post(
        "/login", data={"password": password, "next": "/notes"}, follow_redirects=False
    )


# ---------------------------------------------------------------------------
# 1. 改密码成功：旧密码登录失败、新密码登录成功
# ---------------------------------------------------------------------------
def test_change_password_then_login_with_new(auth_client, csrf):
    changed = change_password(auth_client, csrf, PASSWORD, NEW_PASSWORD)
    assert changed.status_code == 303, changed.text

    # 旧密码不能再登录；新密码可以
    assert fresh_login(auth_client, PASSWORD).status_code == 401
    assert fresh_login(auth_client, NEW_PASSWORD).status_code == 303

    # 设置页 summary 提示「页面已改」
    page = auth_client.get("/settings")
    assert page.status_code == 200
    assert "账号" in page.text
    assert "密码：页面已改" in page.text
    # 会话 cookie 未动：改完密码后原客户端仍在线
    assert auth_client.get("/notes").status_code == 200


# ---------------------------------------------------------------------------
# 2. 旧密码错：拒绝 + 口令不变；连续错触发节流
# ---------------------------------------------------------------------------
def test_wrong_old_password_rejected_and_unchanged(auth_client, csrf):
    rejected = change_password(auth_client, csrf, "not-the-old-password", NEW_PASSWORD)
    assert rejected.status_code == 400
    assert "旧密码不正确" in rejected.text

    with db_mod.db() as conn:
        assert repo.get_meta_map(conn, account.META_PREFIX) == {}
        assert account.verify(conn, PASSWORD) is True
        assert account.verify(conn, NEW_PASSWORD) is False

    # 环境口令仍能登录，新密码不能
    assert fresh_login(auth_client, PASSWORD).status_code == 303
    assert fresh_login(auth_client, NEW_PASSWORD).status_code == 401


def test_wrong_old_password_is_throttled(auth_client, csrf):
    limit = account.password_throttle.limit
    for _ in range(limit):
        resp = change_password(auth_client, csrf, "wrong-wrong-wrong", NEW_PASSWORD)
        assert resp.status_code == 400

    locked = change_password(auth_client, csrf, PASSWORD, NEW_PASSWORD)
    assert locked.status_code == 429
    assert "旧密码错误次数过多" in locked.text


# ---------------------------------------------------------------------------
# 3. 新密码太短 / 两次不一致 / 与旧密码相同 → 拒绝
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "new,confirm,expected",
    [
        ("short7.", "short7.", "至少 8 位"),
        ("good-new-secret", "different-secret", "不一致"),
        (PASSWORD, PASSWORD, "不能和旧密码相同"),
    ],
)
def test_invalid_new_password_rejected(auth_client, csrf, new, confirm, expected):
    resp = change_password(auth_client, csrf, PASSWORD, new, confirm)
    assert resp.status_code == 400, resp.text
    assert expected in resp.text

    with db_mod.db() as conn:
        assert repo.get_meta_map(conn, account.META_PREFIX) == {}
        assert account.verify(conn, PASSWORD) is True


# ---------------------------------------------------------------------------
# 4. describe() 不泄露哈希；reset() 回到 .env
# ---------------------------------------------------------------------------
def test_describe_has_no_hash_and_reset_falls_back(auth_client, csrf):
    assert change_password(auth_client, csrf, PASSWORD, NEW_PASSWORD).status_code == 303

    with db_mod.db() as conn:
        stored = repo.get_meta_map(conn, account.META_PREFIX)["password_hash"]
    info = account.describe()
    blob = json.dumps(info, ensure_ascii=False, default=str)
    assert stored not in blob
    for part in stored.split("$"):
        if len(part) >= 6:
            assert part not in blob
    assert info["source"] == "db"
    assert info["managed"] is True
    assert info["has_hash"] is True

    with db_mod.db() as conn:
        account.reset(conn)
    assert account.describe()["source"] == "env"
    assert account.describe()["managed"] is False

    with db_mod.db() as conn:
        assert account.verify(conn, PASSWORD) is True
        assert account.verify(conn, NEW_PASSWORD) is False

    # 回到 .env 口令后，真实登录也恢复
    assert fresh_login(auth_client, PASSWORD).status_code == 303
    assert fresh_login(auth_client, NEW_PASSWORD).status_code == 401


# ---------------------------------------------------------------------------
# 5. 未登录 / 缺 CSRF 被挡
# ---------------------------------------------------------------------------
def test_password_post_requires_login_and_csrf(auth_client):
    from fastapi.testclient import TestClient

    from app.main import app

    payload = {
        "old_password": PASSWORD,
        "new_password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
    }

    with TestClient(app) as anon:
        blocked = anon.post("/settings/password", data=payload, follow_redirects=False)
    assert blocked.status_code == 303
    assert "/login" in blocked.headers.get("location", "")

    no_csrf = auth_client.post("/settings/password", data=payload, follow_redirects=False)
    assert no_csrf.status_code == 403
