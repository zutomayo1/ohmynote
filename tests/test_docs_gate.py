"""接口文档（/docs、/redoc、/openapi.json）默认关闭，INKNOTE_DOCS=1 时开启。"""

from fastapi.testclient import TestClient

from app import config
from app.main import create_app

DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


def test_docs_disabled_by_default():
    client = TestClient(create_app())
    for path in DOCS_PATHS:
        response = client.get(path)
        assert response.status_code == 404, f"{path} 应该 404（默认关闭）"


def test_docs_enabled_via_env(monkeypatch):
    monkeypatch.setattr(config.settings, "docs_enabled", True)
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    # /redoc 是 HTML 壳，重定向到 redoc 静态资源也行，能响应即可
    assert client.get("/redoc").status_code in (200, 307)
