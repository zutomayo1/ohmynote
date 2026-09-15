"""flag 无刷新 JSON 端点的测试。

端点：POST /notes/{note_id}/flag.json
- 沿用路由级 require_login + csrf_protect 依赖
- note_id 由 NoteId 限幅（1..MAX_SQLITE_INT），越界直接 4xx 而非 500
- flag 必须在 FLAG_FIELDS 内，否则 400
- 返回 {ok, note_id, flag, field, value, is_on} 便于前端就地更新

约定（见 conftest.py）：
- client 是 session 作用域、与所有用例共享 cookie/DB，所以「需登录」用例用全新 TestClient，
  不依赖共享 client 的登录态；断言只针对本次创建的那一篇笔记（禁止全局聚合计数）。
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient


def _create_note(client, csrf_token: str, **extra) -> int:
    """用带 csrf 的表单新建一篇笔记，返回其 id。"""
    payload = {
        "_csrf": csrf_token,
        "title": "flag-api-测试笔记",
        "content": "用于 flag 接口测试",
        "action": "view",
        **extra,
    }
    resp = client.post("/notes", data=payload, follow_redirects=False)
    assert resp.status_code in (200, 303), resp.text
    loc = resp.headers.get("location", "")
    match = re.search(r"/notes/(\d+)", loc)
    assert match, f"创建后没有跳转到笔记页：{loc!r}"
    return int(match.group(1))


def test_flag_json_requires_login():
    """未登录：路由级 require_login 会重定向到登录页（303），绝不是 500。

    新客户端没有任何会话 cookie；关闭重定向跟随，直接断言被拦在 303（落到 /login）。
    """
    from app.main import app

    anon = TestClient(app, follow_redirects=False)  # 全新客户端，无任何会话 cookie
    resp = anon.post("/notes/1/flag.json", data={"flag": "star", "value": "1"})
    assert resp.status_code == 303, resp.status_code
    assert "/login" in (resp.headers.get("location") or "")


def test_flag_json_requires_csrf(auth_client, csrf):
    """已登录但不带 CSRF：csrf_protect 必须 403。"""
    nid = _create_note(auth_client, csrf)
    # 不传 _csrf，也不带 X-CSRF-Token 头
    resp = auth_client.post(f"/notes/{nid}/flag.json", data={"flag": "star", "value": "1"})
    assert resp.status_code == 403, resp.text


def test_flag_json_toggle_and_reset(auth_client, csrf):
    """置位 → 复位，响应体字段正确；未知 flag 返回 400 而非 500。"""
    nid = _create_note(auth_client, csrf)
    url = f"/notes/{nid}/flag.json"
    headers = {"X-CSRF-Token": csrf}

    r1 = auth_client.post(url, data={"flag": "star", "value": "1"}, headers=headers)
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["ok"] is True
    assert body["flag"] == "star"
    assert body["field"] == "is_starred"
    assert body["value"] is True
    assert body["is_on"] is True

    r2 = auth_client.post(url, data={"flag": "star", "value": "0"}, headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["is_on"] is False

    # 未知 flag → 400，不是 500
    r3 = auth_client.post(url, data={"flag": "bogus", "value": "1"}, headers=headers)
    assert r3.status_code == 400, r3.text


def test_flag_json_bad_id_returns_4xx(auth_client, csrf):
    """非法 id：越界 → 4xx（FastAPI 路径参数校验），不存在的合法 id → 404，都不能 500。"""
    headers = {"X-CSRF-Token": csrf}
    # 超过 MAX_SQLITE_INT（2**63-1）的整数 → NoteId 校验失败
    big = 2 ** 63
    r_big = auth_client.post(
        f"/notes/{big}/flag.json", data={"flag": "star", "value": "1"}, headers=headers
    )
    assert r_big.status_code // 100 == 4, f"期望 4xx，实际 {r_big.status_code}: {r_big.text}"
    # 范围内但不存在 → 404
    r_missing = auth_client.post(
        "/notes/999999/flag.json", data={"flag": "star", "value": "1"}, headers=headers
    )
    assert r_missing.status_code == 404, r_missing.text
