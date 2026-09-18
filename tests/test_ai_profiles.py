"""AI 服务商预设：多套「地址+密钥+主模型」存档与一键切换。

安全红线：密钥只在服务端流转——页面只露尾号；脱敏备份必须整键清掉 ai.profiles。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.services import ai


@pytest.fixture()
def conn(client):
    from app import db

    with db.db() as conn:
        yield conn


@pytest.fixture(autouse=True)
def clean_profiles(conn):
    """测试会话共用一个库：先把别的用例留下的预设清掉，别让断言互相污染。"""
    from app import repo

    repo.delete_meta(conn, ["ai.profiles"])
    conn.commit()
    yield
    repo.delete_meta(conn, ["ai.profiles"])
    conn.commit()


def _configure(conn, base_url="https://api.siliconflow.cn/v1", key="sk-test-1234567890abcd",
               model="deepseek-ai/DeepSeek-V4-Flash"):
    values = {
        "base_url": base_url, "api_key": key, "model": model,
        "model_summary": "old-provider-model", "timeout": "60", "embed_model": "BAAI/bge-m3",
    }
    ai.save(conn, values)
    return values


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------
def test_save_profile_requires_name(conn):
    _configure(conn)
    assert ai.save_profile(conn, "  ")["ok"] is False
    assert ai.list_profiles(conn) == []


def test_save_profile_requires_complete_config(conn):
    ai.save(conn, {"base_url": "https://x.example/v1", "model": "", "api_key": "k"})
    result = ai.save_profile(conn, "半套配置")
    assert result["ok"] is False and "不完整" in result["error"]
    assert ai.list_profiles(conn) == []


def test_save_profile_snapshots_and_overwrites_by_name(conn):
    _configure(conn)
    first = ai.save_profile(conn, "硅基流动")
    assert first["ok"] is True and first["count"] == 1

    # 当前配置改了再同名覆盖：不产生第二条
    ai.save(conn, {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                   "api_key": "sk-second-key-9876"})
    second = ai.save_profile(conn, "硅基流动")
    assert second["count"] == 1
    profiles = ai.list_profiles(conn)
    assert profiles[0]["base_url"] == "https://api.deepseek.com/v1"
    assert profiles[0]["key_tail"] == "9876"


def test_list_profiles_masks_key_to_tail(conn):
    _configure(conn)
    ai.save_profile(conn, "带密钥的预设")
    profiles = ai.list_profiles(conn)
    assert profiles[0]["key_tail"] == "abcd"
    assert "sk-test-1234567890" not in json.dumps(profiles, ensure_ascii=False)


def test_apply_profile_switches_and_clears_task_overrides(conn):
    _configure(conn)
    ai.save_profile(conn, "硅基流动")
    # 切到另一家（模拟换服务商）
    ai.save(conn, {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                   "api_key": "sk-deepseek-key-4321"})
    result = ai.apply_profile(conn, "硅基流动")
    assert result["ok"] is True

    cfg = ai.current()
    assert cfg["base_url"] == "https://api.siliconflow.cn/v1"
    assert cfg["api_key"] == "sk-test-1234567890abcd"
    assert cfg["model"] == "deepseek-ai/DeepSeek-V4-Flash"
    # 分任务模型覆盖被清空（上一家的模型名带着走只会 404）
    assert cfg["model_summary"] == "" and cfg["model_tags"] == "" and cfg["model_answer"] == ""
    # 其它设置保持不动（timeout 在 _normalise 里转 int）
    assert cfg["timeout"] == 60
    assert cfg["embed_model"] == "BAAI/bge-m3"


def test_apply_profile_keeps_active_config_when_override_absent(conn):
    """配置里没存覆盖时（DEFAULTS 为空串），切换后仍是空、不报错。"""
    ai.save(conn, {"base_url": "https://a.example/v1", "model": "m1", "api_key": "k1",
                   "timeout": "45"})
    ai.save_profile(conn, "A")
    ai.save(conn, {"base_url": "https://b.example/v1", "model": "m2", "api_key": "k2"})
    assert ai.apply_profile(conn, "A")["ok"] is True
    assert ai.current()["base_url"] == "https://a.example/v1"


def test_apply_unknown_profile_is_error(conn):
    _configure(conn)
    result = ai.apply_profile(conn, "根本不存在")
    assert result["ok"] is False and "不存在" in result["error"]
    assert ai.current()["base_url"] == "https://api.siliconflow.cn/v1"


def test_delete_profile(conn):
    _configure(conn)
    ai.save_profile(conn, "要删的")
    assert ai.delete_profile(conn, "要删的") is True
    assert ai.list_profiles(conn) == []
    assert ai.delete_profile(conn, "要删的") is False   # 再删一次：False，不报错


def test_list_profiles_marks_only_exact_match_active(conn):
    """同一家的两把密钥：只有「地址+模型+密钥」全等的那份算使用中（只比 base_url 会全标上）。"""
    ai.save(conn, {"base_url": "https://api.siliconflow.cn/v1", "model": "m1",
                   "api_key": "sk-aaaa1111"})
    ai.save_profile(conn, "同一家 A")
    ai.save(conn, {"base_url": "https://api.siliconflow.cn/v1", "model": "m1",
                   "api_key": "sk-bbbb2222"})
    ai.save_profile(conn, "同一家 B")

    profiles = ai.list_profiles(conn)
    assert [p["name"] for p in profiles if p["active"]] == ["同一家 B"]


def test_profiles_have_no_limit(conn):
    """用户要求无上限：存 30 份也照单全收。"""
    _configure(conn)
    for i in range(30):
        assert ai.save_profile(conn, f"预设{i}")["ok"] is True
    assert len(ai.list_profiles(conn)) == 30


# ---------------------------------------------------------------------------
# 脱敏备份：ai.profiles 必须整键清空（里面全是密钥）
# ---------------------------------------------------------------------------
def test_sanitized_backup_clears_profiles(conn, tmp_path):
    _configure(conn)
    ai.save_profile(conn, "带密钥的预设")
    conn.commit()
    assert conn.execute("SELECT value FROM meta WHERE key='ai.profiles'").fetchone()[0] != ""

    from app.services import db_backup

    # 源路径从连接上拿（PRAGMA database_list），不猜文件名；
    # 库是 WAL 模式，已提交的数据可能还在 -wal 文件里，先 checkpoint 落盘再复制
    source = conn.execute("PRAGMA database_list").fetchall()
    source_path = next(row[2] for row in source if row[2])
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    target = tmp_path / "sanitized.db"
    db_backup.export_sanitized_copy(__import__("pathlib").Path(source_path), target)

    check = sqlite3.connect(str(target))
    try:
        value = check.execute("SELECT value FROM meta WHERE key='ai.profiles'").fetchone()[0]
        assert value == ""
        assert check.execute("SELECT value FROM meta WHERE key='ai.api_key'").fetchone()[0] == ""
    finally:
        check.close()


# ---------------------------------------------------------------------------
# HTTP 层：设置页表单（无 JS 也能用）
# ---------------------------------------------------------------------------
def _csrf(client) -> str:
    import re

    page = client.get("/settings")
    match = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
    return match.group(1)


def test_profile_endpoints_need_auth(client):
    """HTML 路由未登录是 303 去登录页。

    注意：client 是 session 级 fixture，可能被其它用例的 auth_client 登录过——
    必须造一个全新实例（干净 cookie）才能测「未登录」；已登录但缺 CSRF 是 403。
    """
    fresh = client.__class__(client.app)
    assert fresh.post("/settings/ai/profiles/save", json={"name": "x"},
                      follow_redirects=False).status_code == 303
    assert fresh.post("/settings/ai/profiles/apply", json={"name": "x"},
                      follow_redirects=False).status_code == 303
    assert fresh.post("/settings/ai/profiles/delete", json={"name": "x"},
                      follow_redirects=False).status_code == 303


def test_profile_flow_over_http(auth_client, conn):
    _configure(conn)
    conn.commit()   # HTTP 层是另一个连接，先提交
    headers = {"X-CSRF-Token": _csrf(auth_client)}

    # 保存 → 303 回设置页
    res = auth_client.post("/settings/ai/profiles/save", data={"name": "HTTP 预设"},
                           headers=headers, follow_redirects=False)
    assert res.status_code == 303

    # 设置页能看到预设（密钥只露尾号）
    page = auth_client.get("/settings")
    assert "HTTP 预设" in page.text
    assert "abcd" in page.text
    assert "sk-test-1234567890" not in page.text, "完整密钥绝不能出现在页面里"

    # 换配置后一键切回
    ai.save(conn, {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                   "api_key": "sk-other-key-1111"})
    conn.commit()
    res = auth_client.post("/settings/ai/profiles/apply", data={"name": "HTTP 预设"},
                           headers=headers, follow_redirects=False)
    assert res.status_code == 303
    assert ai.current()["base_url"] == "https://api.siliconflow.cn/v1"

    # 删除
    res = auth_client.post("/settings/ai/profiles/delete", data={"name": "HTTP 预设"},
                           headers=headers, follow_redirects=False)
    assert res.status_code == 303
    assert ai.list_profiles(conn) == []


def test_settings_page_shows_empty_hint_without_profiles(auth_client):
    page = auth_client.get("/settings")
    assert "我的预设" in page.text
    assert "还没有预设" in page.text


def test_settings_page_has_no_nested_forms(auth_client):
    """HTML 不允许嵌套 form：浏览器会丢弃内层 form 标签，按钮就会提交到外层地址。

    上一版「我的预设」被放在 AI 配置表单内部 → 「存当前配置 / 启用 / 删除」实际
    提交的都是「保存 AI 配置」，预设永远存不进去（用户实测「我的预设一直为空」）。
    这里用 HTML 解析器守住结构，别再用浏览器行为差异踩同一个坑。
    """
    from html.parser import HTMLParser

    class FormNesting(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.forms = 0
            self.nested = 0

        def handle_starttag(self, tag, attrs):
            if tag != "form":
                return
            self.forms += 1
            if self.depth > 0:
                self.nested += 1
            self.depth += 1

        def handle_endtag(self, tag):
            if tag == "form" and self.depth > 0:
                self.depth -= 1

    page = auth_client.get("/settings")
    parser = FormNesting()
    parser.feed(page.text)
    assert parser.forms >= 4, "设置页应该有：AI 配置 / 预设若干 / 提示词 等多个表单"
    assert parser.nested == 0, "设置页出现了嵌套 form——浏览器会丢弃内层，按钮会提交错地址"

    # 预设相关表单必须在外层 AI 配置表单之外
    assert 'class="ai-profile-save"' in page.text
    start = page.text.index('id="ai-settings-form"')
    end = page.text.index("</form>", start)
    outer_block = page.text[start:end]
    assert "profiles/save" not in outer_block
    assert "profiles/apply" not in outer_block
    assert "profiles/delete" not in outer_block
