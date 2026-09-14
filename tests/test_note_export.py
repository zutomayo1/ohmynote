"""单文件精美导出：服务构建 / HTTP 端点 / 权限。"""

from conftest import csrf_of


def _create_note(auth_client, csrf: str, title: str, content: str) -> int:
    response = auth_client.post(
        "/notes",
        data={"_csrf": csrf, "title": title, "action": "save", "content": content},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].split("/")[-2])


def test_build_export_html_contains_title_and_body():
    from app.services import note_export

    body = note_export.build_export_html(
        {"title": "导出测试"}, "<h2>小节</h2><p>正文内容</p>"
    )
    assert "<title>导出测试</title>" in body
    assert "<h2>小节</h2>" in body
    assert "正文内容" in body
    assert "prefers-color-scheme: dark" in body
    assert "墨痕 InkNote" in body
    assert body.startswith("<!doctype html>")


def test_export_endpoint_roundtrip(auth_client, csrf):
    note_id = _create_note(auth_client, csrf, "导出笔记", "# 标题\n\n**加粗**内容")

    response = auth_client.get(f"/notes/{note_id}/export.html")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    body = response.text
    assert "<title>导出笔记</title>" in body
    assert "加粗" in body
    # 自包含：内嵌样式，不引用站点资源
    assert "<style>" in body
    assert "/static/" not in body


def test_export_endpoint_404(auth_client, csrf):
    assert auth_client.get("/notes/99999/export.html").status_code == 404


def test_export_requires_auth():
    """未登录会被 require_login 重定向到登录页（拿到的是登录页而非笔记内容）。

    注意：conftest 的 auth_client 会把登录 cookie 写进共享的 client 实例，
    所以这里必须用全新的未登录 TestClient（项目惯例 client.__class__(client.app)）。
    """
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as fresh:
        response = fresh.get("/notes/1/export.html", follow_redirects=False)
    assert response.status_code in (301, 302, 303)
    assert "/login" in response.headers["location"]
    # 即便跟随重定向，内容也是登录页而不是笔记
    followed = fresh.get("/notes/1/export.html")
    assert "<title>导出" not in followed.text


def test_export_chinese_filename_encoded(auth_client, csrf):
    note_id = _create_note(auth_client, csrf, "中文名笔记", "x")
    response = auth_client.get(f"/notes/{note_id}/export.html")
    header = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in header
    # 中文不应以原样出现（需 URL 编码）
    assert "中文名" not in header.split("filename=")[0]
