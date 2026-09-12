"""数据层：标签合并、版本快照、双链、回收站、统计。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import db as db_mod, repo
from app.services import export as export_service
from app.utils import now


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "repo.db"
    db_mod.init_db(path)
    with db_mod.db(path) as connection:
        yield connection


# ---------------------------------------------------------------------------
# 创建 / 更新
# ---------------------------------------------------------------------------
def test_create_generates_slug_and_summary(conn):
    note = repo.create_note(conn, title="Python 学习笔记", content="第一句话。第二句话。" * 3)
    assert note["slug"] == "python-学习笔记"
    assert note["summary"].startswith("第一句话")
    assert note["status"] == "draft"
    assert note["word_count"] > 0


def test_create_without_title_gets_placeholder(conn):
    note = repo.create_note(conn, title="   ", content="x")
    assert note["title"] == "无标题笔记"
    assert note["slug"] == "无标题笔记"


def test_duplicate_slug_gets_suffix(conn):
    first = repo.create_note(conn, title="同名标题", content="a")
    second = repo.create_note(conn, title="同名标题", content="b")
    assert first["slug"] == "同名标题"
    assert second["slug"] == "同名标题-2"


def test_custom_slug_is_sanitised(conn):
    note = repo.create_note(conn, title="标题", content="x", slug="my/post name?x=1")
    assert note["slug"] == "my-post-name-x-1"


def test_inline_hashtags_are_merged_with_explicit_tags(conn):
    note = repo.create_note(
        conn, title="标签测试", content="正文里写 #异步 #Python 两个标签", tags="python, 编程"
    )
    assert {tag.lower() for tag in note["tags"]} == {"python", "编程", "异步"}
    # 标签保持去重
    assert len(note["tags"]) == len(set(tag.lower() for tag in note["tags"]))


def test_code_block_hashtags_are_not_collected(conn):
    note = repo.create_note(conn, title="代码", content="```\n#不是标签\n```\n\n正文")
    assert note["tags"] == []


def test_auto_summary_follows_content_but_manual_survives(conn):
    note = repo.create_note(conn, title="摘要", content="原始内容。")
    assert note["summary"] == "原始内容。"

    updated = repo.update_note(conn, note["id"], content="完全不同的新内容。")
    assert updated["summary"] == "完全不同的新内容。"

    manual = repo.update_note(conn, note["id"], summary="我手写的摘要")
    assert manual["summary"] == "我手写的摘要"
    again = repo.update_note(conn, note["id"], content="又改了正文。")
    assert again["summary"] == "我手写的摘要"


def test_publish_sets_published_at_once(conn):
    note = repo.create_note(conn, title="发布", content="x")
    assert note["published_at"] is None
    published = repo.set_flags(conn, note["id"], is_public=True)
    assert published["published_at"]
    stamp = published["published_at"]
    repo.set_flags(conn, note["id"], is_public=False)
    again = repo.set_flags(conn, note["id"], is_public=True)
    assert again["published_at"] == stamp


def test_flag_toggle_does_not_touch_updated_at(conn):
    note = repo.create_note(conn, title="开关", content="x")
    repo.set_flags(conn, note["id"], is_starred=True)
    assert repo.get_note(conn, note["id"])["updated_at"] == note["updated_at"]


# ---------------------------------------------------------------------------
# 历史版本
# ---------------------------------------------------------------------------
def test_manual_snapshots_accumulate(conn):
    note = repo.create_note(conn, title="版本", content="v1")
    repo.update_note(conn, note["id"], content="v2", reason="manual")
    repo.update_note(conn, note["id"], content="v3", reason="manual")
    versions = repo.list_versions(conn, note["id"])
    assert len(versions) == 2
    assert versions[-1]["reason"] == "manual"


def test_autosave_snapshots_are_coalesced(conn):
    note = repo.create_note(conn, title="自动保存", content="v1")
    for index in range(5):
        repo.update_note(conn, note["id"], content=f"v{index + 2}", reason="autosave")
    assert len(repo.list_versions(conn, note["id"])) == 1


def test_versions_are_pruned_to_keep(conn, monkeypatch):
    monkeypatch.setattr(repo.settings, "version_keep", 3)
    note = repo.create_note(conn, title="剪枝", content="v0")
    for index in range(6):
        repo.update_note(conn, note["id"], content=f"v{index + 1}", reason="manual")
    assert len(repo.list_versions(conn, note["id"])) == 3


def test_restore_version_brings_content_back(conn):
    note = repo.create_note(conn, title="回滚", content="原始")
    repo.update_note(conn, note["id"], content="改坏了", reason="manual")
    oldest = repo.list_versions(conn, note["id"])[-1]
    restored = repo.restore_version(conn, note["id"], oldest["id"])
    assert restored["content"] == "原始"
    # 回滚前的内容被先存成了快照
    reasons = [item["reason"] for item in repo.list_versions(conn, note["id"])]
    assert "before-restore" in reasons


def test_snapshot_keeps_tags_text(conn):
    note = repo.create_note(conn, title="标签快照", content="x", tags="甲")
    repo.update_note(conn, note["id"], tags="乙", content="y", reason="manual")
    assert repo.list_versions(conn, note["id"])[-1]["tags"] == "甲"


# ---------------------------------------------------------------------------
# 双链
# ---------------------------------------------------------------------------
def test_wikilink_target_and_backlink(conn):
    target = repo.create_note(conn, title="目标笔记", content="内容")
    source = repo.create_note(conn, title="来源笔记", content="见 [[目标笔记]]")
    links = repo.outgoing_links(conn, source["id"])
    assert links[0]["exists"] is True
    assert links[0]["note_id"] == target["id"]
    assert [item["title"] for item in repo.backlinks(conn, target["id"])] == ["来源笔记"]


def test_dangling_link_is_attached_after_target_created(conn):
    source = repo.create_note(conn, title="先写的", content="提到 [[后来才有]]")
    assert repo.outgoing_links(conn, source["id"])[0]["exists"] is False
    repo.create_note(conn, title="后来才有", content="正文")
    assert repo.outgoing_links(conn, source["id"])[0]["exists"] is True


def test_rename_reattaches_dangling_links(conn):
    source = repo.create_note(conn, title="引用者", content="见 [[新名字]]")
    target = repo.create_note(conn, title="旧名字", content="x")
    assert repo.outgoing_links(conn, source["id"])[0]["exists"] is False
    repo.update_note(conn, target["id"], title="新名字")
    assert repo.outgoing_links(conn, source["id"])[0]["exists"] is True


def test_code_wikilink_is_not_indexed(conn):
    note = repo.create_note(conn, title="代码双链", content="`[[编辑器里的]]`")
    assert repo.outgoing_links(conn, note["id"]) == []


def test_backlinks_respect_public_only(conn):
    public_note = repo.create_note(conn, title="公开笔记", content="x", is_public=True)
    repo.create_note(conn, title="私密引用", content="见 [[公开笔记]]")
    repo.create_note(conn, title="公开引用", content="见 [[公开笔记]]", is_public=True)
    assert len(repo.backlinks(conn, public_note["id"])) == 2
    assert len(repo.backlinks(conn, public_note["id"], public_only=True)) == 1


def test_related_notes_by_shared_tags(conn):
    note = repo.create_note(conn, title="主笔记", content="内容", tags="python, 异步")
    repo.create_note(conn, title="相关", content="内容", tags="python, 异步, 网络")
    repo.create_note(conn, title="不相关", content="内容", tags="菜谱")
    related = repo.related_notes(conn, note, limit=5)
    assert [item["title"] for item in related] == ["相关"]
    assert "共同标签" in related[0]["reason"]


# ---------------------------------------------------------------------------
# 列表 / 筛选 / 回收站
# ---------------------------------------------------------------------------
def test_filters_and_sorting(conn):
    repo.create_note(conn, title="A", content="短内容", tags="x", category="技术")
    repo.create_note(conn, title="B", content="长" * 30, tags="y", category="读书", status="saved")
    repo.create_note(conn, title="C", content="中" * 10, tags="x", is_starred=True)

    assert repo.list_notes(conn, tag="x")[1] == 2
    assert repo.list_notes(conn, category="技术")[1] == 1
    assert repo.list_notes(conn, status="saved")[1] == 1
    assert repo.list_notes(conn, fav="starred")[1] == 1
    titles = [item["title"] for item in repo.list_notes(conn, sort="words")[0]]
    assert titles[0] == "B"
    assert repo.list_notes(conn, q="长", category="技术")[1] == 0
    assert repo.list_notes(conn, q="长", category="读书")[1] == 1


def test_month_filter(conn):
    note = repo.create_note(conn, title="本月", content="x")
    month = note["updated_at"][:7]
    assert repo.list_notes(conn, month=month)[1] == 1
    assert repo.list_notes(conn, month="1999-01")[1] == 0


def test_trash_flow(conn):
    note = repo.create_note(conn, title="垃圾", content="x")
    assert repo.soft_delete(conn, note["id"]) is True
    assert repo.get_note(conn, note["id"]) is None
    assert repo.get_note(conn, note["id"], include_deleted=True)["deleted"]
    assert repo.list_notes(conn, include_deleted=True)[1] == 1
    assert repo.list_notes(conn)[1] == 0
    repo.restore(conn, note["id"])
    assert repo.list_notes(conn)[1] == 1
    repo.soft_delete(conn, note["id"])
    assert repo.empty_trash(conn) == 1
    assert repo.get_note(conn, note["id"], include_deleted=True) is None


def test_expired_trash_is_purged(conn, monkeypatch):
    monkeypatch.setattr(repo.settings, "trash_days", 7)
    note = repo.create_note(conn, title="过期", content="x")
    repo.soft_delete(conn, note["id"])
    old = (now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE notes SET deleted_at = ? WHERE id = ?", (old, note["id"]))
    assert repo.purge_expired_trash(conn) == 1
    fresh = repo.create_note(conn, title="没过期", content="x")
    repo.soft_delete(conn, fresh["id"])
    assert repo.purge_expired_trash(conn) == 0


def test_trash_days_left(conn, monkeypatch):
    monkeypatch.setattr(repo.settings, "trash_days", 30)
    note = repo.create_note(conn, title="剩余天数", content="x")
    repo.soft_delete(conn, note["id"])
    assert repo.trash_days_left(repo.get_note(conn, note["id"], include_deleted=True)["deleted_at"]) == 30


def test_delete_cascades_tags_and_versions(conn):
    note = repo.create_note(conn, title="级联", content="v1", tags="孤标签")
    repo.update_note(conn, note["id"], content="v2", reason="manual")
    repo.purge(conn, note["id"])
    assert conn.execute("SELECT COUNT(*) AS c FROM note_tags").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM note_versions").fetchone()["c"] == 0
    assert repo.list_tags(conn) == []


# ---------------------------------------------------------------------------
# 标签 / 归档 / 统计
# ---------------------------------------------------------------------------
def test_tag_counts_and_public_filter(conn):
    repo.create_note(conn, title="P", content="x", tags="共同", is_public=True)
    repo.create_note(conn, title="Q", content="x", tags="共同")
    tags = {item["name"]: item["count"] for item in repo.list_tags(conn)}
    assert tags["共同"] == 2
    public_tags = {item["name"]: item["count"] for item in repo.list_tags(conn, public_only=True)}
    assert public_tags["共同"] == 1


def test_archive_months(conn):
    repo.create_note(conn, title="公开", content="x", is_public=True)
    repo.create_note(conn, title="草稿", content="x")
    assert len(repo.archive_months(conn)) == 1
    assert repo.archive_months(conn, public_only=False)[0]["count"] == 2
    month = repo.archive_months(conn)[0]
    assert month["label"].endswith("月")
    assert len(month["key"]) == 7


def test_dashboard_stats(conn):
    repo.create_note(conn, title="一篇", content="一二三四五", is_public=True)
    repo.create_note(conn, title="两篇", content="六七八九十")
    stats = repo.dashboard_stats(conn)
    assert stats["total"] == 2
    assert stats["drafts"] == 1
    assert stats["public_count"] == 1
    assert stats["words"] == 10
    assert stats["streak"] == 1
    assert stats["today_updated"] == 2

def test_streak_counts_consecutive_days(conn):
    note = repo.create_note(conn, title="连续", content="x")
    today = now().date()
    for index, day in enumerate([today, today - timedelta(days=1), today - timedelta(days=2)]):
        stamp = day.strftime("%Y-%m-%d 09:00:00")
        conn.execute(
            "INSERT INTO note_versions (note_id, title, content, tags, summary, reason, created_at)"
            " VALUES (?, '', '', '', '', 'manual', ?)",
            (note["id"], stamp),
        )
    assert repo.writing_streak(conn) == 3


def test_adjacent_notes(conn):
    first = repo.create_note(conn, title="第一篇", content="x")
    second = repo.create_note(conn, title="第二篇", content="x")
    previous, following = repo.adjacent_notes(conn, second)
    assert previous is None
    assert following["title"] == "第一篇"
    previous, following = repo.adjacent_notes(conn, first)
    assert previous["title"] == "第二篇"
    assert following is None


def test_adjacent_public_only(conn):
    """公开文章的前后导航不能泄露私密笔记。"""
    public_first = repo.create_note(conn, title="公开的", content="x", is_public=True)
    repo.create_note(conn, title="私密的", content="x")
    public_last = repo.create_note(conn, title="又一篇公开的", content="x", is_public=True)

    previous, following = repo.adjacent_notes(conn, public_first, public_only=True)
    assert previous is None or previous["title"] != "私密的"
    assert following is None

    previous, following = repo.adjacent_notes(conn, public_last, public_only=True)
    assert previous is None
    assert following["title"] == "公开的"


# ---------------------------------------------------------------------------
# 模板 / 导出
# ---------------------------------------------------------------------------
def test_templates_seed_and_crud(conn):
    repo.seed_templates(conn)
    repo.seed_templates(conn)  # 幂等
    names = [item["name"] for item in repo.list_templates(conn)]
    assert "读书笔记" in names
    assert len(names) == len(set(names))

    new_id = repo.save_template(conn, name="我的模板", content="## 内容")
    assert repo.get_template(conn, new_id)["content"] == "## 内容"
    repo.save_template(conn, template_id=new_id, name="改名", content="x")
    assert repo.get_template(conn, new_id)["name"] == "改名"
    repo.delete_template(conn, new_id)
    assert repo.get_template(conn, new_id) is None


def test_export_zip_contains_markdown_with_front_matter(conn):
    import io
    import json
    import zipfile

    repo.create_note(conn, title="导出测试", content="正文内容", tags="标签甲", is_public=True)
    payload = export_service.build_zip(conn)
    archive = zipfile.ZipFile(io.BytesIO(payload))
    names = archive.namelist()
    assert "index.md" in names and "notes.json" in names and "README.txt" in names
    md_name = [name for name in names if name.startswith("notes/")][0]
    body = archive.read(md_name).decode("utf-8")
    assert body.startswith("---")
    assert "title:" in body and "正文内容" in body
    manifest = json.loads(archive.read("notes.json").decode("utf-8"))
    assert manifest["count"] == 1
    assert manifest["notes"][0]["tags"] == ["标签甲"]


def test_export_is_not_truncated_by_pagination(conn):
    """回归：导出曾复用 list_notes(per_page=100000)，但后者把 per_page 截到 1000，
    导致笔记超过 1000 篇时静默丢失。"""
    import io
    import json
    import zipfile

    from app.utils import now_iso

    stamp = now_iso()
    conn.executemany(
        "INSERT INTO notes (title, slug, content, summary, status, created_at, updated_at)"
        " VALUES (?, ?, 'x', 'x', 'saved', ?, ?)",
        [(f"批量笔记 {index}", f"bulk-{index}", stamp, stamp) for index in range(1005)],
    )
    assert len(repo.all_notes(conn, sort="created")) == 1005

    archive = zipfile.ZipFile(io.BytesIO(export_service.build_zip(conn)))
    manifest = json.loads(archive.read("notes.json").decode("utf-8"))
    assert manifest["count"] == 1005
    assert len([name for name in archive.namelist() if name.startswith("notes/")]) == 1005


def test_export_filename_is_safe(conn):
    note = repo.create_note(conn, title='非法/字符:测试*?"<>|', content="x")
    name = export_service.note_filename(note)
    assert name.endswith(".md")
    assert not any(char in name for char in '/\\:*?"<>|')


def test_find_notes_by_title(conn):
    repo.create_note(conn, title="关键词笔记", content="x")
    assert len(repo.find_notes_by_title(conn, "关键词")) == 1
    assert repo.find_notes_by_title(conn, "不存在") == []
    # LIKE 通配符被转义，不会误匹配
    assert repo.find_notes_by_title(conn, "%") == []
