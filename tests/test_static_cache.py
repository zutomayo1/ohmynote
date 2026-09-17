"""静态资源缓存头（Cache-Control）。

背景：Starlette 的 StaticFiles 只发 ETag / Last-Modified、**不发 Cache-Control**，
于是每次翻页浏览器都要为每个资源（style.css + 8 个 js）发一次条件请求等 304，
弱网下每个来回都是实打实的等待。两处 URL 都是内容寻址的，可以长缓存：

- `/static/...?v=<asset_v>`：asset_v = 所有静态文件 mtime 的 md5（进程启动时算）
- `/media/<年>/<月>/<内容哈希><.ext>`：文件名即上传内容 sha256 前 16 位

覆盖：纯函数分支、真实请求的响应头、以及「动态页面不加缓存头」。
"""

from __future__ import annotations

from app.config import settings
from app.main import static_cache_control


# ---------------------------------------------------------------------------
# 1. 纯函数：哪些路径、发什么头
# ---------------------------------------------------------------------------

def test_versioned_static_is_immutable():
    assert static_cache_control("/static/css/style.css", "v=1a58943e") == \
        "public, max-age=31536000, immutable"
    assert static_cache_control("/static/js/app.js", "v=abc") == \
        "public, max-age=31536000, immutable"


def test_unversioned_static_falls_back_to_short_cache():
    assert static_cache_control("/static/css/style.css", "") == "public, max-age=3600"
    assert static_cache_control("/static/favicon.svg", "") == "public, max-age=3600"


def test_media_with_content_hash_name_is_immutable():
    assert static_cache_control("/media/2026/09/0123456789abcdef.png", "") == \
        "public, max-age=31536000, immutable"
    assert static_cache_control("/media/2026/09/0123456789abcdefabcdef01.webp", "") == \
        "public, max-age=3600", "超过 16 位就不算内容哈希命名"


def test_media_without_content_hash_name_falls_back():
    for name in ("photo.png", "IMG_2024.JPG", "0123456789abcde.png", "0123456789ABCDEF.png"):
        assert static_cache_control(f"/media/2026/09/{name}", "") == "public, max-age=3600", name


def test_dynamic_paths_get_no_cache_header():
    for path in ("/blog", "/notes", "/settings", "/api/notes", "/media", "/static"):
        assert static_cache_control(path, "") is None, path


# ---------------------------------------------------------------------------
# 2. 端到端：真实请求带上了头，且动态页面没被顺手加上
# ---------------------------------------------------------------------------

def test_response_headers_end_to_end(client):
    versioned = client.get("/static/css/style.css?v=deadbeef")
    assert versioned.status_code == 200
    assert "immutable" in versioned.headers.get("cache-control", "")
    assert "etag" in versioned.headers, "ETag 不该被顶掉"

    plain = client.get("/static/css/style.css")
    assert plain.headers.get("cache-control") == "public, max-age=3600"

    html = client.get("/blog")
    assert html.status_code == 200
    assert "cache-control" not in html.headers, "动态页面不该被加上缓存头"


def test_uploaded_media_is_immutable_end_to_end(client):
    directory = settings.upload_dir / "2026" / "09"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "fedcba9876543210.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    try:
        response = client.get("/media/2026/09/fedcba9876543210.png")
        assert response.status_code == 200
        assert "immutable" in response.headers.get("cache-control", "")
    finally:
        target.unlink(missing_ok=True)
