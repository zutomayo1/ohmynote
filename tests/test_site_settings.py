"""站点信息设置 + 回收站角标的测试。

覆盖：
1. 保存站点名后 ``settings.site_title`` 立刻变，首页/笔记页 <title> 跟着变；
2. 重新 bootstrap（模拟重启）后仍生效，reset 回到 .env；
3. 非法值（per_page / trash_days / site_title）被拒、原配置不变、页面回显；
4. 未登录 POST 被挡（303 跳登录）、缺 CSRF 被挡（403）；
5. 回收站角标数字 == 回收站篇数，为 0 时不渲染；
6. 公开博客页（未登录）仍能正常渲染。
"""

from __future__ import annotations

import re

import pytest

from app import db as db_mod
from app import repo
from app.config import settings
from app.services import site_settings


@pytest.fixture(autouse=True)
def restore_site_settings():
    """站点配置写的是进程级 settings 单例：每个用例跑完都清掉页面配置。"""
    yield
    with db_mod.db() as conn:
        site_settings.reset(conn)


def save_site(client, csrf: str, **fields):
    data = {"_csrf": csrf}
    data.update(fields)
    return client.post("/settings/site", data=data, follow_redirects=False)


def page_title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.S)
    assert match, "页面里没有 <title>"
    return match.group(1)


# ---------------------------------------------------------------------------
# 1. 保存后立刻生效，<title> 跟着变
# ---------------------------------------------------------------------------
def test_save_site_title_takes_effect_in_titles(auth_client, csrf):
    saved = save_site(
        auth_client,
        csrf,
        site_title="并行测试站",
        site_subtitle="副标题也换了",
        site_description="新的站点描述",
        author="测试作者",
        base_url="http://example.com",
        per_page="12",
        trash_days="30",
    )
    assert saved.status_code == 303, saved.text

    # 直接写回 settings 对象，立刻生效（模板/路由都在读它）
    assert settings.site_title == "并行测试站"
    assert settings.site_subtitle == "副标题也换了"
    assert settings.per_page == 12 and isinstance(settings.per_page, int)
    assert settings.trash_days == 30

    for path in ("/notes", "/blog"):
        page = auth_client.get(path)
        assert page.status_code == 200
        assert "并行测试站" in page_title(page.text)

    # /settings 里能看到新的「站点信息」表单 + 当前值
    settings_page = auth_client.get("/settings")
    assert settings_page.status_code == 200
    assert "站点信息" in settings_page.text
    assert 'name="site_title"' in settings_page.text
    assert "并行测试站" in settings_page.text
    assert "每页" in settings_page.text and "回收站" in settings_page.text


# ---------------------------------------------------------------------------
# 2. 重新 bootstrap 后仍在；reset 后回到 .env
# ---------------------------------------------------------------------------
def test_bootstrap_persists_and_reset_falls_back_to_env(auth_client, csrf):
    save_site(
        auth_client,
        csrf,
        site_title="重启后还在",
        site_subtitle="",
        site_description="",
        author="",
        base_url="http://example.com",
        per_page="24",
        trash_days="7",
    )
    assert settings.site_title == "重启后还在"

    # 模拟重启：先把运行期配置还原成 .env，再让 bootstrap 从数据库读回来
    site_settings.bootstrap(None)
    assert settings.site_title == "测试站"  # conftest 里的 INKNOTE_TITLE

    with db_mod.db() as conn:
        site_settings.bootstrap(conn)
    assert settings.site_title == "重启后还在"
    assert settings.per_page == 24
    assert settings.trash_days == 7

    with db_mod.db() as conn:
        site_settings.reset(conn)
    assert settings.site_title == "测试站"
    assert settings.site_title == site_settings.current()["site_title"]
    assert settings.per_page == 12
    assert settings.trash_days == 30
    assert site_settings.describe()["sources"]["site_title"] == "env"


# ---------------------------------------------------------------------------
# 3. 非法值被拒、原配置不变、页面回显
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("per_page", "0", "5~200"),
        ("per_page", "201", "5~200"),
        ("per_page", "abc", "必须是整数"),
        ("trash_days", "-1", "0~3650"),
        ("trash_days", "3651", "0~3650"),
        ("site_title", "", "不能为空"),
        ("site_title", "长" * 61, "最多 60"),
    ],
)
def test_invalid_values_are_rejected(auth_client, csrf, field, value, expected):
    with db_mod.db() as conn:
        site_settings.bootstrap(conn)
    before = site_settings.current()

    payload = {
        "site_title": "不该被保存",
        "site_subtitle": "s",
        "site_description": "d",
        "author": "a",
        "base_url": "http://example.com",
        "per_page": "12",
        "trash_days": "30",
    }
    payload[field] = value

    resp = save_site(auth_client, csrf, **payload)
    assert resp.status_code == 400, resp.text
    # 原配置一点没变（页面配置 + 运行期 settings）
    assert site_settings.current() == before
    assert settings.site_title == before["site_title"]
    assert settings.per_page == before["per_page"]
    assert settings.trash_days == before["trash_days"]
    # flash 报错 + 非法输入回显
    assert "flash--error" in resp.text
    assert expected in resp.text
    if value:
        assert value in resp.text


# ---------------------------------------------------------------------------
# 4. 未登录 / 缺 CSRF 被挡
# ---------------------------------------------------------------------------
def test_site_post_requires_login_and_csrf(auth_client):
    from fastapi.testclient import TestClient

    from app.main import app

    payload = {"site_title": "匿名", "per_page": "12", "trash_days": "30"}

    # 未登录：303 跳登录页
    with TestClient(app) as anon:
        blocked = anon.post("/settings/site", data=payload, follow_redirects=False)
    assert blocked.status_code == 303
    assert "/login" in blocked.headers.get("location", "")

    # 已登录但没有 CSRF：403
    no_csrf = auth_client.post("/settings/site", data=payload, follow_redirects=False)
    assert no_csrf.status_code == 403


# ---------------------------------------------------------------------------
# 5. 回收站角标
# ---------------------------------------------------------------------------
def test_trash_badge_matches_count_and_is_hidden_when_empty(auth_client):
    with db_mod.db() as conn:
        repo.empty_trash(conn)  # 从「回收站为空」的确定状态开始

    clean = auth_client.get("/notes")
    assert clean.status_code == 200
    assert 'class="nav-badge"' not in clean.text

    with db_mod.db() as conn:
        note = repo.create_note(conn, title="角标测试笔记", content="正文", status="saved")
        repo.soft_delete(conn, note["id"])
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM notes WHERE deleted_at IS NOT NULL"
        ).fetchone()["c"]
    assert count == 1

    page = auth_client.get("/notes")
    assert page.status_code == 200
    assert f'<span class="nav-badge">{count}</span>' in page.text

    with db_mod.db() as conn:
        repo.purge(conn, note["id"])
    empty = auth_client.get("/notes")
    assert 'class="nav-badge"' not in empty.text


# ---------------------------------------------------------------------------
# 6. 未登录的公开页面不受 base_context 改动影响
# ---------------------------------------------------------------------------
def test_public_pages_render_for_anonymous():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anon:
        for path in ("/blog", "/blog/archive", "/about", "/login"):
            resp = anon.get(path)
            assert resp.status_code == 200, f"{path} 渲染失败"
        # 访客导航里没有回收站入口，自然也没有角标
        assert 'class="nav-badge"' not in anon.get("/blog").text


# ---------------------------------------------------------------------------
# 7. 只恢复外观（reset_appearance）：外观回默认，站点信息保持不动
# ---------------------------------------------------------------------------
def test_reset_appearance_keeps_site_info(auth_client, csrf):
    save_site(
        auth_client,
        csrf,
        site_title="别被清掉",
        site_subtitle="",
        site_description="",
        author="",
        base_url="",
        per_page="24",
        trash_days="7",
        appearance_palette="ocean",
        appearance_custom="#3B5BA5",
        appearance_mode="dark",
        appearance_radius="lg",
    )
    assert settings.appearance_custom == "#3B5BA5"
    assert settings.appearance_radius == "lg"

    response = auth_client.post(
        "/settings/site", data={"_csrf": csrf, "reset_appearance": "1"}, follow_redirects=False
    )
    assert response.status_code == 303

    # 外观整组回到 .env / 默认
    assert settings.appearance_custom == ""
    assert settings.appearance_radius == "md"
    assert settings.appearance_mode == "auto"
    # 站点信息一项都没被动
    assert settings.site_title == "别被清掉"
    assert settings.per_page == 24
    assert settings.trash_days == 7
    # 重新 bootstrap（模拟重启）后依旧如此
    with db_mod.db() as conn:
        site_settings.bootstrap(conn)
    assert settings.site_title == "别被清掉"
    assert settings.appearance_custom == ""


def test_appearance_reset_button_rendered(auth_client):
    page = auth_client.get("/settings")
    assert 'name="reset_appearance"' in page.text
    assert "只恢复外观默认值" in page.text


def test_reset_rejects_unknown_field():
    """未知字段宁可报错，也不静默清掉一堆配置。"""
    with db_mod.db() as conn:
        with pytest.raises(site_settings.SiteSettingsError):
            site_settings.reset(conn, ("appearance_custom", "site_titel"))
