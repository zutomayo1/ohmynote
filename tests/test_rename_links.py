"""改标题联动更新 [[链接]]：repo.link_refs_to / rename_link_refs + 路由。

约定：路由用例断言数据前先 db_conn.commit()（db_conn 的事务对请求连接不可见）。
"""

from __future__ import annotations

from app import repo
from tests.test_agent import db_conn  # noqa: F401  fixture 随模块导入

PREFIX = "改名E2E-"


def test_link_refs_to_finds_refs_with_alias(db_conn):
    target = repo.create_note(db_conn, title=PREFIX + "目标", content="正文")
    src = repo.create_note(
        db_conn,
        title=PREFIX + "来源",
        content=f"看 [[{PREFIX}目标]] 和 [[{PREFIX}目标|别名]]。",
    )
    db_conn.commit()
    refs = repo.link_refs_to(db_conn, target["id"], PREFIX + "目标")
    assert [(n["id"], hits) for n, hits in refs] == [(src["id"], 2)]


def test_link_refs_to_ignores_case_and_code_blocks(db_conn):
    target = repo.create_note(db_conn, title=PREFIX + "Case 目标", content="x")
    repo.create_note(
        db_conn,
        title=PREFIX + "Case 来源",
        content=f"[[{PREFIX}CASE 目标]]\n\n```\n[[{PREFIX}case 目标]]\n```",
    )
    db_conn.commit()
    refs = repo.link_refs_to(db_conn, target["id"], PREFIX + "case 目标")
    # 大小写不敏感命中正文里的那一处；代码块里的不算
    assert refs and refs[0][1] == 1


def test_rename_link_refs_rewrites_and_keeps_alias(db_conn):
    target = repo.create_note(db_conn, title=PREFIX + "旧标题", content="x")
    src = repo.create_note(
        db_conn,
        title=PREFIX + "引用者",
        content=f"链接 [[{PREFIX}旧标题]] 与 [[{PREFIX}旧标题|我的叫法]]。",
    )
    db_conn.commit()
    # 真实流程：先把 target 改名，再更新引用者的链接（否则改写后反而悬空）
    repo.update_note(db_conn, target["id"], title=PREFIX + "新标题", reason="manual")
    db_conn.commit()

    updated, refs_count, failed = repo.rename_link_refs(
        db_conn, target["id"], PREFIX + "旧标题", PREFIX + "新标题"
    )
    assert (updated, refs_count, failed) == (1, 2, 0)

    content = repo.get_note(db_conn, src["id"])["content"]
    assert f"[[{PREFIX}新标题]]" in content
    assert f"[[{PREFIX}新标题|我的叫法]]" in content
    assert "旧标题" not in content

    # 改写走 update_note → note_links 会重建，反向链接仍然指向目标
    _links = [dict(r) for r in db_conn.execute("SELECT * FROM note_links")]
    _notes = [dict(r) for r in db_conn.execute("SELECT id, title FROM notes")]
    assert any(n["id"] == src["id"] for n in repo.backlinks(db_conn, target["id"])), (
        _links,
        _notes,
    )


def test_rename_link_refs_same_title_is_noop(db_conn):
    target = repo.create_note(db_conn, title=PREFIX + "同名", content="x")
    assert repo.rename_link_refs(
        db_conn, target["id"], PREFIX + "同名", PREFIX + "同名"
    ) == (0, 0, 0)
    assert repo.rename_link_refs(db_conn, target["id"], "", PREFIX + "新") == (0, 0, 0)


def test_rename_route_flow(auth_client, csrf, db_conn):
    target = repo.create_note(db_conn, title=PREFIX + "路由目标", content="x")
    src = repo.create_note(
        db_conn, title=PREFIX + "路由引用", content=f"见 [[{PREFIX}路由目标]]。"
    )
    db_conn.commit()
    headers = {"X-CSRF-Token": csrf}

    # 改名保存 → 重定向带 rename_from
    resp = auth_client.post(
        f"/notes/{target['id']}",
        data={"title": PREFIX + "路由新名", "content": "x", "action": "save"},
        headers=headers,
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.status_code
    assert "rename_from=" in resp.headers["location"]

    # 编辑页显示提示条
    page = auth_client.get(
        f"/notes/{target['id']}/edit?rename_from={PREFIX}路由目标", headers=headers
    )
    assert "引用着旧标题" in page.text and "一并更新" in page.text

    # 一键更新
    resp2 = auth_client.post(
        f"/notes/{target['id']}/rename-links",
        data={"old_title": PREFIX + "路由目标"},
        headers=headers,
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    content = repo.get_note(db_conn, src["id"])["content"]
    assert f"[[{PREFIX}路由新名]]" in content, content


def test_rename_route_rejects_duplicate_title(auth_client, csrf, db_conn):
    target = repo.create_note(db_conn, title=PREFIX + "改名者", content="x")
    repo.create_note(db_conn, title=PREFIX + "撞名笔记", content="x")
    db_conn.commit()
    headers = {"X-CSRF-Token": csrf}
    # 先把目标改成与另一篇同名，再请求更新链接 → 应拒绝
    resp = auth_client.post(
        f"/notes/{target['id']}",
        data={"title": PREFIX + "撞名笔记", "content": "x", "action": "save"},
        headers=headers,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    resp2 = auth_client.post(
        f"/notes/{target['id']}/rename-links",
        data={"old_title": PREFIX + "改名者"},
        headers=headers,
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    from urllib.parse import unquote

    assert "先给它改个别的名字" in unquote(resp2.headers["location"])


def test_detail_page_shows_rename_banner_and_back_redirect(auth_client, csrf, db_conn):
    """回归：保存默认落到详情页，但提示条只渲染在编辑页——用户什么都看不到。

    详情页带 ?rename_from= 时也要出提示条；从详情页提交后应回到详情页。
    """
    target = repo.create_note(db_conn, title=PREFIX + "详情目标", content="x")
    src = repo.create_note(
        db_conn, title=PREFIX + "详情引用", content=f"见 [[{PREFIX}详情目标]]。"
    )
    db_conn.commit()
    headers = {"X-CSRF-Token": csrf}

    # 改名 → 落到详情页（带 rename_from）
    resp = auth_client.post(
        f"/notes/{target['id']}",
        data={"title": PREFIX + "详情新名", "content": "x", "action": "view"},
        headers=headers,
        follow_redirects=False,
    )
    assert resp.status_code == 303

    detail = auth_client.get(
        f"/notes/{target['id']}?rename_from={PREFIX}详情目标", headers=headers
    )
    assert "引用着旧标题" in detail.text and "一并更新" in detail.text

    # 从详情页提交（带 back）→ 回详情页而不是编辑页
    resp2 = auth_client.post(
        f"/notes/{target['id']}/rename-links",
        data={"old_title": PREFIX + "详情目标", "back": f"/notes/{target['id']}"},
        headers=headers,
        follow_redirects=False,
    )
    assert resp2.status_code == 303
    assert resp2.headers["location"].startswith(f"/notes/{target['id']}?")
    assert f"[[{PREFIX}详情新名]]" in repo.get_note(db_conn, src["id"])["content"]

    # back 只接受站内路径：// 与外链一律回落到编辑页
    resp3 = auth_client.post(
        f"/notes/{target['id']}/rename-links",
        data={"old_title": PREFIX + "详情新名", "back": "https://evil.example"},
        headers=headers,
        follow_redirects=False,
    )
    assert resp3.headers["location"].startswith(f"/notes/{target['id']}/edit")
