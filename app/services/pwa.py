"""PWA：manifest、service worker 与站点主题色。

为什么 sw.js 由服务端动态生成：预缓存清单必须带当前 ``asset_v`` 指纹 ——
静态资源是内容寻址的（见 main.static_cache_control），URL 变了缓存才会更新；
而 SW 一旦注册就长期驻留浏览器，版本得和资源指纹对齐。生成式写在 Python 里
最直接：不需要构建步骤，也不引任何依赖。

图标是随仓库提交的 PNG（项目没有 Pillow，光栅化只在开发时用无头浏览器做一次，
见 .scratch/gen_icons.py）：运行时零依赖、离线可用。

隐私说明：SW 缓存的是「这台设备浏览过的页面」。私人笔记应用是一人一机，
所以默认缓存全部同源页面；共用电脑请清站点数据（使用说明里也写了）。
"""

from __future__ import annotations

import json
import re

from ..config import settings
from ..templating import ASSET_VERSION, custom_brand_css

# 六套内置色板 + 默认墨迹：品牌色 / 亮底 / 暗底（值取自 style.css 的色板块）
PALETTE_COLORS: dict[str, tuple[str, str, str]] = {
    "": ("#B5533C", "#FAF6EF", "#1F1B17"),
    "bamboo": ("#3D7A5F", "#F2F5F0", "#171E19"),
    "ocean": ("#3B5BA5", "#F0F3F7", "#161A21"),
    "plum": ("#8E4A7E", "#F6F2F6", "#1E1A1E"),
    "amber": ("#B8742E", "#F8F3E9", "#211B12"),
    "slate": ("#4E5D6C", "#F3F4F5", "#1A1C1F"),
}

# 安装时预缓存的核心资源（相对路径 + asset_v 指纹）；
# graph.js / editor.js 这些只在个别页面用的不进预缓存，交给运行时缓存按需补。
PRECACHE = (
    "/static/css/style.css",
    "/static/css/highlight.css",
    "/static/js/app.js",
    "/static/js/select.js",
    "/static/js/chart-tip.js",
    "/static/js/blog.js",
    "/static/js/reading-progress.js",
    "/static/js/link-suggest.js",
    "/static/js/onboarding.js",
    "/static/js/pwa.js",
    "/static/favicon.svg",
    "/static/offline.html",
)

OFFLINE_URL = "/static/offline.html"

_ICONS = (
    {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
    {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
    {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512",
     "type": "image/png", "purpose": "maskable"},
)

_BG_RE = re.compile(r"--bg:(#[0-9A-Fa-f]{6})")


def theme_colors() -> dict[str, str]:
    """当前站点的品牌色 / 亮底 / 暗底（给 manifest 与 <meta theme-color> 用）。

    自定义主题色优先：直接读它推导出来的背景色，手机状态栏才会和页面底色一致。

    先 ``ensure_ready()``：这个函数在渲染早期就被调用，可能早于 lifespan 的
    bootstrap —— 那时读 ``settings.appearance_custom`` 会直接 AttributeError。
    """
    from . import site_settings

    site_settings.ensure_ready()
    custom = (settings.appearance_custom or "").strip()
    if custom:
        css = custom_brand_css(custom)
        light, _, dark = css.partition("\n")
        light_hit, dark_hit = _BG_RE.search(light), _BG_RE.search(dark)
        return {
            "brand": custom.upper(),
            "light": light_hit.group(1) if light_hit else PALETTE_COLORS[""][1],
            "dark": dark_hit.group(1) if dark_hit else PALETTE_COLORS[""][2],
        }
    brand, light, dark = PALETTE_COLORS.get(
        (settings.appearance_palette or "").strip(), PALETTE_COLORS[""]
    )
    return {"brand": brand, "light": light, "dark": dark}


def manifest_payload() -> dict:
    """Web App Manifest（安装到桌面 / 手机主屏用）。"""
    colors = theme_colors()
    title = (settings.site_title or "墨痕").strip()
    subtitle = (settings.site_subtitle or "").strip()
    return {
        "name": f"{title} · {subtitle}" if subtitle else title,
        "short_name": title[:12],
        "description": (settings.site_description or "").strip(),
        "lang": "zh-CN",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": colors["light"],
        "theme_color": colors["brand"],
        "icons": [dict(icon) for icon in _ICONS],
        "shortcuts": [
            {"name": "新建笔记", "url": "/notes/new"},
            {"name": "搜索", "url": "/search"},
        ],
    }


_SW_TEMPLATE = """/* 墨痕 Service Worker —— 离线阅读
 * 由服务端在 /sw.js 动态生成（预缓存清单带当前资源指纹），不要手改生成结果。
 *
 * 策略：
 *   预缓存：样式 / 核心脚本 / 离线兜底页，安装时一次性拉齐
 *   /static/ 与 /media/：缓存优先（URL 内容寻址，与 HTTP 长缓存一致）
 *   页面导航：网络优先；离线时回落「这台设备读过的那个页面」，再退到离线页
 *   非 GET 与 /api/：直连网络，绝不缓存（离线时不能「假装保存成功」）
 *
 * 隐私：缓存的是浏览器里读过的页面，共用电脑请清站点数据。
 */
var VERSION = '__VERSION__';
var PRECACHE = __PRECACHE__;
var OFFLINE_URL = '__OFFLINE_URL__';

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(VERSION).then(function (cache) {
      return Promise.all(PRECACHE.map(function (url) {
        return cache.add(new Request(url, { cache: 'reload' })).catch(function () {});
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        if (key !== VERSION && key.indexOf('inknote-') === 0) { return caches.delete(key); }
        return null;
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

function cacheFirst(request) {
  return caches.open(VERSION).then(function (cache) {
    return cache.match(request).then(function (hit) {
      if (hit) { return hit; }
      return fetch(request).then(function (res) {
        if (res && res.ok && !res.redirected && res.type === 'basic') {
          cache.put(request, res.clone());
        }
        return res;
      });
    });
  });
}

function networkFirst(request) {
  return fetch(request).then(function (res) {
    if (res && res.ok && !res.redirected && res.type === 'basic') {
      var copy = res.clone();
      caches.open(VERSION).then(function (cache) { cache.put(request, copy); });
    }
    return res;
  }).catch(function () {
    return caches.open(VERSION).then(function (cache) {
      return cache.match(request).then(function (hit) {
        // 离线兜底页是带 ?v=指纹 预缓存的，这里必须 ignoreSearch，否则查不到
        // （曾导致 respondWith(undefined) → 导航直接 ERR_FAILED）
        return hit || caches.match(OFFLINE_URL, { ignoreSearch: true });
      });
    });
  });
}

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') { return; }
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) { return; }
  if (url.pathname === '/sw.js') { return; }
  if (url.pathname.indexOf('/api/') === 0) { return; }
  if (req.mode === 'navigate') { event.respondWith(networkFirst(req)); return; }
  if (url.pathname.indexOf('/static/') === 0 || url.pathname.indexOf('/media/') === 0) {
    event.respondWith(cacheFirst(req));
  }
});
"""


def service_worker_js() -> str:
    """生成 /sw.js 内容（占位符替换而非 str.format：JS 里全是花括号）。"""
    precache = [f"{path}?v={ASSET_VERSION}" for path in PRECACHE]
    return (
        _SW_TEMPLATE.replace("__VERSION__", f"inknote-{ASSET_VERSION}")
        .replace("__PRECACHE__", json.dumps(precache, ensure_ascii=False, indent=2))
        .replace("__OFFLINE_URL__", OFFLINE_URL)
    )
