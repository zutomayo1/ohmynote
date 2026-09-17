"""PWA：manifest、service worker 与离线兜底页。

覆盖：
1. manifest 字段齐全、可解析，图标文件真的存在且是正确尺寸的 PNG；
2. manifest 的 theme_color / background_color 跟随站点配色（色板 / 自定义色）；
3. /sw.js 带 no-cache、预缓存清单含指纹且路径都存在、语法能被 node 校验；
4. 离线兜底页完全自包含（断网时外部资源拿不到）；
5. 页面 head 里挂上了 manifest / apple-touch-icon / theme-color，脚本引了 pwa.js。
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess

import pytest

from app.config import STATIC_DIR
from app.services import pwa, site_settings
from app.templating import ASSET_VERSION


@pytest.fixture(autouse=True)
def _settings_ready(client):
    """站点配置（appearance_* 等）由 bootstrap 挂到 settings 单例上。

    真实启动流程在 lifespan 里会做一次；这里显式跑一遍，免得「直接调函数」
    的用例拿到没有这些属性的 settings（AttributeError 而不是断言失败，很难看）。
    """
    from app import db as db_mod

    with db_mod.db() as conn:
        site_settings.bootstrap(conn)
    yield


PALETTE_CASES = {
    "": "#B5533C",
    "bamboo": "#3D7A5F",
    "ocean": "#3B5BA5",
    "plum": "#8E4A7E",
    "amber": "#B8742E",
    "slate": "#4E5D6C",
}


# ---------------------------------------------------------------------------
# 1. manifest
# ---------------------------------------------------------------------------

def test_manifest_payload_fields():
    data = pwa.manifest_payload()
    assert data["start_url"] == "/"
    assert data["scope"] == "/"
    assert data["display"] == "standalone"
    assert data["lang"] == "zh-CN"
    assert data["name"] and data["short_name"]
    assert len(data["short_name"]) <= 12
    assert {icon["src"] for icon in data["icons"]} == {
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/icon-maskable-512.png",
    }
    assert any(icon.get("purpose") == "maskable" for icon in data["icons"]), "必须有一枚 maskable"
    assert [s["url"] for s in data["shortcuts"]] == ["/notes/new", "/search"]


def test_manifest_endpoint(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert "application/manifest+json" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-cache", "改了配色要立刻生效"
    data = json.loads(response.text)  # 必须是合法 JSON
    assert data["start_url"] == "/"


@pytest.mark.parametrize("palette,brand", list(PALETTE_CASES.items()))
def test_theme_color_follows_palette(palette, brand, client):
    """站点换配色后，手机状态栏颜色要跟着走。"""
    with _saved({"appearance_palette": palette, "appearance_custom": ""}):
        payload = pwa.manifest_payload()
        assert payload["theme_color"].upper() == brand
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", payload["background_color"])


def test_theme_color_follows_custom_color(client):
    with _saved({"appearance_palette": "ocean", "appearance_custom": "#C2185B"}):
        colors = pwa.theme_colors()
        assert colors["brand"] == "#C2185B", "自定义色优先于色板"
        assert colors["light"] != "#F0F3F7", "应当用自定义色推导出来的底色，而不是色板底色"
        assert colors["light"].upper() != colors["dark"].upper()


class _saved:
    """临时改站点配色，退出时恢复（站点配置是进程级单例）。"""

    def __init__(self, values: dict):
        self.values = values

    def __enter__(self):
        from app import db as db_mod

        with db_mod.db() as conn:
            site_settings.save(conn, self.values)
        return self

    def __exit__(self, *exc):
        from app import db as db_mod

        with db_mod.db() as conn:
            site_settings.reset(conn)
        return False


# ---------------------------------------------------------------------------
# 2. service worker
# ---------------------------------------------------------------------------

def test_service_worker_endpoint(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-cache", "SW 必须可被重新校验"
    body = response.text
    assert f"inknote-{ASSET_VERSION}" in body, "缓存名要带资源指纹，否则旧缓存清不掉"
    for path in pwa.PRECACHE:
        assert f"{path}?v={ASSET_VERSION}" in body, f"预缓存漏了 {path}"
    assert pwa.OFFLINE_URL in body
    # 策略标记：写请求直连、API 直连、静态 cache-first、导航 network-first
    for marker in ("cacheFirst", "networkFirst", "'/api/'", "req.mode === 'navigate'",
                   "req.method !== 'GET'"):
        assert marker in body, f"SW 里缺少策略标记 {marker}"


def test_precache_paths_all_exist():
    """预缓存清单写错路径会让安装静默少缓存一个文件，这里钉住。"""
    missing = [path for path in pwa.PRECACHE
               if not (STATIC_DIR / path.replace("/static/", "", 1)).is_file()]
    assert not missing, f"预缓存清单里有不存在的文件：{missing}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_service_worker_js_is_valid(tmp_path):
    """SW 是用占位符替换拼出来的字符串，语法错了会静默失去离线能力。"""
    script = tmp_path / "sw.js"
    script.write_text(pwa.service_worker_js(), encoding="utf-8")
    result = subprocess.run([shutil.which("node"), "--check", str(script)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# 3. 离线兜底页
# ---------------------------------------------------------------------------

def test_offline_page_is_self_contained(client):
    response = client.get(pwa.OFFLINE_URL)
    assert response.status_code == 200
    html = response.text
    assert "<style" in html, "离线页必须内联样式"
    assert not re.search(r'<link[^>]+rel="stylesheet"', html), "离线时外部 CSS 拿不到"
    assert not re.search(r'<script[^>]+src=', html), "离线时外部脚本拿不到"
    assert "离线" in html and 'href="/"' in html


# ---------------------------------------------------------------------------
# 4. 图标
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,size", [
    ("icon-192.png", 192), ("icon-512.png", 512),
    ("icon-maskable-512.png", 512), ("apple-touch-icon-180.png", 180),
])
def test_icons_are_png_of_expected_size(name, size):
    data = (STATIC_DIR / "icons" / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{name} 不是 PNG"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (size, size)


# ---------------------------------------------------------------------------
# 5. 页面接线
# ---------------------------------------------------------------------------

def test_base_html_wires_pwa(client):
    html = client.get("/blog").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert 'rel="apple-touch-icon"' in html
    assert html.count('name="theme-color"') == 2, "亮暗各一个 theme-color"
    assert 'media="(prefers-color-scheme: light)"' in html
    assert 'media="(prefers-color-scheme: dark)"' in html
    assert re.search(r'/static/js/pwa\.js\?v=[0-9a-f]+', html), "pwa.js 没挂上"
