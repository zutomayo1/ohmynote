"""预取 / 预渲染的安全网：鼠标划过链接不该产生任何副作用。

背景：base.html 里加了 speculation rules（悬停即把目标页准备好）。但 `/notes/today`
是**唯一有写操作的 GET** —— 笔记不存在时就地新建一篇。预取一旦落到它头上，鼠标
划过「今日笔记」就会凭空多出一篇空笔记。这里锁住三道防线：

1. 客户端规则：白名单式预渲染 + 显式排除 /notes/today 与下载类路径，且 JSON 合法
2. 高风险链接带 data-no-prerender 属性（规则之外的第二重）
3. 服务端闸门：带 Sec-Purpose 预取头的请求一律不写库
"""

import json
import re

from app import db as db_mod
from app.services import note_templates

SPEC_RE = re.compile(r'<script type="speculationrules"[^>]*>(.*?)</script>', re.S)


def _rules(client) -> dict:
    html = client.get("/notes").text
    found = SPEC_RE.findall(html)
    assert found, "页面里没有 speculation rules（预渲染规则丢了）"
    return json.loads(found[0])


def test_speculation_rules_are_valid_json(auth_client):
    rules = _rules(auth_client)
    assert "prerender" in rules and "prefetch" in rules
    for entry in rules["prerender"]:
        assert entry["eagerness"] in ("moderate", "conservative", "eager", "immediate")
        assert entry["where"]


def test_rules_exclude_side_effectful_and_download_routes(auth_client):
    """预渲染白名单里必须带排除项：写库的 GET、下载包、外链标记。"""
    rules = _rules(auth_client)
    blob = json.dumps(rules["prerender"])
    for must_exclude in ("/notes/today", "/notes/*/export.*", "/export/*", "/backup/download/*"):
        assert must_exclude in blob, f"预渲染规则没有排除 {must_exclude}"
    assert "[data-no-prerender]" in blob
    # 只读页面必须真的在名单里，否则等于没开预渲染
    for read_only in ("/notes/*", "/blog/*", "/settings", "/stats", "/graph"):
        assert read_only in blob, f"预渲染规则漏了只读页面 {read_only}"


def test_risky_links_are_marked_no_prerender(auth_client):
    html = auth_client.get("/notes").text
    assert re.search(r'<a[^>]+href="/notes/today"[^>]*data-no-prerender', html), \
        "「今日笔记」链接应带 data-no-prerender"
    backup = auth_client.get("/backup").text
    for path in ("/export/zip", "/export/json"):
        assert re.search(r'<a[^>]+href="%s"[^>]*data-no-prerender' % re.escape(path), backup), \
            f"下载链接 {path} 应带 data-no-prerender"


def _today_note_count() -> int:
    title = __import__("datetime").date.today().strftime("%Y-%m-%d")
    with db_mod.db() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM notes WHERE title = ?", (title,)).fetchone()["c"]


def test_prefetch_request_does_not_create_today_note(auth_client):
    """核心断言：带预取头的 GET /notes/today 不能建笔记（用增量，别假设库里干净）。"""
    before = _today_note_count()
    cases = ({"Sec-Purpose": "prefetch"}, {"Sec-Purpose": "prefetch;prerender"},
             {"Sec-Purpose": "Prefetch"}, {"Purpose": "prefetch"}, {"X-Purpose": "preview"})
    for headers in cases:
        response = auth_client.get("/notes/today", headers=headers, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/notes", f"预取 {headers} 时应绕开，不进编辑器"
    assert _today_note_count() == before, "预取请求把每日笔记建出来了！"


def test_normal_request_still_creates_today_note(auth_client):
    """正常点击不受影响：照旧建/复用一篇并进编辑器。

    这个会话里所有测试共用一个库，所以建完自己清掉，别把「今日笔记」留给下一个
    用例（曾因此把 test_note_templates 的模板断言打红）。
    """
    from app.repo import trash as trash_repo

    before = _today_note_count()
    response = auth_client.get("/notes/today", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/notes/"), "正常点击被预取闸门拦住了"
    if before == 0:
        assert _today_note_count() == before + 1
        note_id = int(location.split("/")[2])
        try:
            again = auth_client.get("/notes/today", follow_redirects=False)
            assert again.headers["location"] == location, "已存在时应复用同一篇"
        finally:
            with db_mod.db() as conn:
                trash_repo.purge(conn, note_id)
                conn.commit()
            assert _today_note_count() == before


def test_prefetch_helper_recognises_headers():
    """闸门函数本身的行为（不依赖路由）。"""
    from app.routers.notes.views import is_speculative_prefetch

    class _Req:
        def __init__(self, headers):
            self.headers = headers

    assert is_speculative_prefetch(_Req({"sec-purpose": "prefetch"})) is True
    assert is_speculative_prefetch(_Req({"sec-purpose": "prefetch;prerender"})) is True
    assert is_speculative_prefetch(_Req({"purpose": "prefetch"})) is True
    assert is_speculative_prefetch(_Req({"x-purpose": "preview"})) is True   # 早年 Chrome 用它
    assert is_speculative_prefetch(_Req({})) is False
    assert is_speculative_prefetch(_Req({"sec-fetch-mode": "navigate"})) is False


def test_scripts_defer_work_until_prerender_activation():
    """预渲染页面会跑 JS：一次性引导与 SW 注册必须等「激活」后再做。"""
    from pathlib import Path

    onboarding = Path("app/static/js/onboarding.js").read_text(encoding="utf-8")
    pwa = Path("app/static/js/pwa.js").read_text(encoding="utf-8")
    assert "document.prerendering" in onboarding
    assert "prerenderingchange" in onboarding
    assert "whenActive(start)" in onboarding, "引导启动要包在 whenActive 里"
    assert "document.prerendering" in pwa and "prerenderingchange" in pwa


def test_scroll_listeners_are_passive():
    """滚动/改尺寸监听一律被动 + 合帧：不能挡住滚动线程，也不能每事件读布局。"""
    from pathlib import Path

    for name, pattern in (
        ("app/static/js/chart-tip.js", "scheduleReposition"),
        ("app/static/js/editor.js", "schedulePill"),
        ("app/static/js/onboarding.js", "scheduleLayout"),
    ):
        source = Path(name).read_text(encoding="utf-8")
        assert pattern in source, f"{name} 缺 rAF 合帧（{pattern}）"
        assert "{ passive: true" in source or "{ passive: true, capture: true }" in source, \
            f"{name} 的滚动监听没有 passive"
    # 不许再出现「strip 掉 passive 的老写法」
    chart_tip = Path("app/static/js/chart-tip.js").read_text(encoding="utf-8")
    assert "addEventListener('scroll', reposition, true)" not in chart_tip
    editor = Path("app/static/js/editor.js").read_text(encoding="utf-8")
    assert "addEventListener('scroll', hidePill, true)" not in editor


def test_note_templates_daily_name_unchanged():
    """顺手确认闸门没有动到「每日笔记」模板语义。"""
    assert note_templates.DAILY_NAME
