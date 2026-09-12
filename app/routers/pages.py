"""标签页、搜索结果页、笔记模板、问笔记、数据导出。"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .. import repo, search as search_mod
from ..config import settings
from ..deps import EditParam, MAX_SQLITE_INT, PageParam, TemplateId, csrf_protect, db_conn, require_login
from ..services import ai, ai_search, media
from ..services import export as export_service
from ..templating import render
from ..utils import now, total_pages, url_with_query

router = APIRouter(dependencies=[Depends(require_login), Depends(csrf_protect)])


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------
@router.get("/tags")
def tags_page(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    tag: str = "",
    q: str = "",
    sort: str = "",
    page: PageParam = 1,
):
    tags = repo.list_tags(conn, q=q, sort=sort)
    all_tags = repo.list_tags(conn, limit=500, sort=sort)
    per_page = settings.per_page
    groups: list[dict] = []
    notes: list[dict] = []
    total = 0

    if tag:
        notes, total = repo.list_notes(conn, tag=tag, page=page, per_page=per_page)
    else:
        for item in tags[:12]:
            group_notes, _count = repo.list_notes(conn, tag=item["name"], per_page=5)
            groups.append({"tag": item, "notes": group_notes})

    return render(
        request,
        "tags.html",
        tags=tags,
        groups=groups,
        all_tags=all_tags,
        tag=tag,
        q=q,
        sort=sort,
        notes=notes,
        total=total,
        page=max(1, page),
        pages=total_pages(total, per_page),
    )
@router.post("/tags/rename")
def tag_rename(
    conn: sqlite3.Connection = Depends(db_conn),
    old: str = Form(""),
    new: str = Form(""),
):
    old_name = (old or "").strip().lstrip("#").strip()
    try:
        result = repo.rename_tag(conn, old, new)
    except ValueError as exc:
        # 空名 / 超长 / 不存在：303 + 错误 flash，绝不让非法输入变成 500。
        return RedirectResponse(
            url_with_query("/tags", msg=str(exc), kind="error"), status_code=303
        )
    if result["merged"]:
        msg = f"标签「{old_name}」已合并到「{result['name']}」，影响 {result['notes']} 篇笔记"
    else:
        msg = f"标签已重命名为「{result['name']}」，影响 {result['notes']} 篇笔记"
    return RedirectResponse(
        url_with_query("/tags", tag=result["name"], msg=msg), status_code=303
    )


@router.post("/tags/delete")
def tag_delete(
    conn: sqlite3.Connection = Depends(db_conn),
    name: str = Form(""),
):
    clean = (name or "").strip().lstrip("#").strip()
    result = repo.delete_tag(conn, name)
    if not result["deleted"]:
        return RedirectResponse(
            url_with_query("/tags", msg=f"标签「{clean or name}」不存在", kind="error"),
            status_code=303,
        )
    return RedirectResponse(
        url_with_query(
            "/tags",
            msg=f"已删除标签 {clean or name}（{result['notes']} 篇笔记受影响）",
        ),
        status_code=303,
    )


@router.post("/tags/cleanup")
def tag_cleanup(conn: sqlite3.Connection = Depends(db_conn)):
    removed = repo.purge_unused_tags(conn)
    msg = f"已清理 {removed} 个未使用标签" if removed else "没有未使用的标签"
    return RedirectResponse(url_with_query("/tags", msg=msg), status_code=303)





# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
# 语义模式一次取多少条候选：ai_embed.retrieve 每次都要全库算余弦，
# 翻页不重复调用，所以放大到够翻几页，再在内存里切片分页。
SEMANTIC_CANDIDATES = 100


@router.get("/search")
def search_page(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    q: str = "",
    page: PageParam = 1,
    mode: str = "",
    fav: str = "",
    tag: str = "",
    category: str = "",
    status: str = "",
    sort: str = "",
):
    per_page = settings.per_page
    requested_semantic = (mode or "").strip().lower() == "semantic"
    engine = "keyword"
    fallback_reason = ""
    semantic_needs_setup = False

    if q.strip() and requested_semantic:
        # 先试语义；拿到 None（没配模型 / 没索引 / 调用失败）就回退关键词。
        semantic = ai_search.semantic_search(
            conn, q, limit=min(200, max(SEMANTIC_CANDIDATES, per_page * 5))
        )
        if semantic is not None:
            results = semantic["items"]
            engine = "semantic"
        else:
            results = repo.search_notes(conn, q, limit=300)
            diagnosis = ai_search.diagnose(conn)
            fallback_reason = diagnosis["reason"]
            semantic_needs_setup = diagnosis["needs_setup"]
    else:
        results = repo.search_notes(conn, q, limit=300) if q.strip() else []

    # 检索负责「找出来」，筛选/排序负责「再缩小」—— 口径与列表页共用同一套实现。
    fav = (fav or "").strip()
    tag = (tag or "").strip()
    category = (category or "").strip()
    status = (status or "").strip()
    sort = (sort or "").strip()
    has_filter = bool(fav or tag or category or status)
    if results and has_filter:
        results = repo.filter_notes(results, tag=tag, category=category, status=status, fav=fav)
    if results and sort:
        # 默认不动顺序：关键词/语义的相关度排序比任何时间排序都有用
        results = repo.sort_notes(results, sort=sort)

    total = len(results)
    start = (max(1, page) - 1) * per_page
    page_items = results[start : start + per_page]
    return render(
        request,
        "search.html",
        q=q,
        notes=page_items,
        total=total,
        page=max(1, page),
        pages=total_pages(total, per_page),
        tokens=search_mod.tokenize(q),
        mode="semantic" if requested_semantic else "keyword",
        engine=engine,
        backend="FTS5 trigram"
        if search_mod.FTS_ENABLED and search_mod.FTS_TOKENIZER == "trigram"
        else "LIKE",
        fallback_reason=fallback_reason,
        semantic_needs_setup=semantic_needs_setup,
        fav=fav,
        tag=tag,
        category=category,
        status=status,
        sort=sort,
        has_filter=has_filter,
        all_tags=repo.list_tags(conn, limit=60),
        all_categories=repo.list_categories(conn),
    )


# ---------------------------------------------------------------------------
# 笔记模板
# ---------------------------------------------------------------------------
@router.get("/templates")
def templates_page(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    edit: EditParam = 0,
):
    editing = repo.get_template(conn, edit) if edit else None
    return render(
        request,
        "templates.html",
        templates=repo.list_templates(conn),
        editing=editing,
    )


def _parse_template_id(raw: str) -> tuple[bool, int | None]:
    """解析表单里的模板编号，返回 ``(是否合法, id 或 None 表示新建)``。

    不能直接 ``int()``：纯数字也可能是 20 位这种超出 SQLite INTEGER 的值，
    绑到 SQL 上会抛 ``OverflowError: Python int too large to convert to SQLite INTEGER``
    （之前就是这个把 ``POST /templates/save`` 打成 500）。所以用 MAX_SQLITE_INT 卡上限。
    """
    text = (raw or "").strip()
    if not text:
        return True, None
    if not text.isdigit():
        return False, None
    value = int(text)
    if not 1 <= value <= MAX_SQLITE_INT:
        return False, None
    return True, value


@router.post("/templates/save")
def template_save(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    template_id: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    content: str = Form(""),
):
    del request
    ok, tid = _parse_template_id(template_id)
    if not ok:
        # 非法编号：给一句人话就回列表页，绝不拿它去查库。
        return RedirectResponse(
            url_with_query("/templates", msg="模板编号不合法，请刷新页面后重试", kind="error"),
            status_code=303,
        )
    repo.save_template(conn, template_id=tid, name=name, description=description, content=content)
    return RedirectResponse(
        url_with_query("/templates", msg="模板已保存" if tid else "模板已创建"), status_code=303
    )


@router.post("/templates/{template_id}/delete")
def template_delete(
    request: Request,
    template_id: TemplateId,
    conn: sqlite3.Connection = Depends(db_conn),
):
    del request
    repo.delete_template(conn, template_id)
    return RedirectResponse(url_with_query("/templates", msg="模板已删除"), status_code=303)


# ---------------------------------------------------------------------------
# 导出：在 export.build_zip 基础上补 media/ 图片
# ---------------------------------------------------------------------------
_MEDIA_URL_PREFIX = "/media/"
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _is_within(child: Path, root: Path) -> bool:
    """child 是否真的在 root 目录里面（兼容 Windows 大小写差异）。"""
    try:
        if child.is_relative_to(root):
            return True
    except (ValueError, OSError):
        pass
    left = [os.path.normcase(part) for part in child.parts]
    right = [os.path.normcase(part) for part in root.parts]
    return len(left) > len(right) and left[: len(right)] == right


def _safe_upload_path(rel: str, root: Path) -> Path | None:
    """把 /media/ 后面的相对路径解析成 uploads 根内的文件；越界返回 None。"""
    raw = (rel or "").replace("\\", "/").strip()
    if not raw or "\x00" in raw or raw.startswith("/") or _DRIVE_PREFIX_RE.match(raw):
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    try:
        root_resolved = root.resolve()
        resolved = root_resolved.joinpath(*parts).resolve()
    except OSError:
        return None
    if resolved == root_resolved or not _is_within(resolved, root_resolved):
        return None
    return resolved


def collect_referenced_media(conn: sqlite3.Connection) -> list[tuple[str, Path]]:
    """收集笔记正文引用、且真实存在的 uploads 图片，返回 [(相对路径, 文件路径), ...]。"""
    try:
        refs = media.usage(conn)
    except Exception:  # 导出不能因为引用统计失败就整体失败
        return []
    found: dict[str, Path] = {}
    for url in refs:
        text = str(url)
        if not text.startswith(_MEDIA_URL_PREFIX):
            continue
        rel = text[len(_MEDIA_URL_PREFIX):]
        target = _safe_upload_path(rel, settings.upload_dir)
        if target is None:
            continue
        try:
            if target.is_file():
                found[rel.replace("\\", "/")] = target
        except OSError:
            continue
    return sorted(found.items())


def _backup_readme(media_count: int) -> str:
    return (
        "InkNote 备份说明\n"
        "================\n\n"
        f"media/ 目录里是笔记正文引用到的 {media_count} 张图片。\n"
        "导入时它们会按相对路径还原到 data/uploads/ 下：\n"
        "- 已存在且内容相同的图片会跳过；\n"
        "- 同名但内容不同的会改名（如 x-1.png）保留，绝不覆盖你已有的图片。\n"
    )


def build_zip_with_media(conn: sqlite3.Connection) -> bytes:
    """导出笔记 zip，并在 media/ 目录里带上笔记引用的图片。"""
    base = export_service.build_zip(conn)
    media_items = collect_referenced_media(conn)
    media_names = [rel for rel, _path in media_items]
    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(io.BytesIO(base), "r") as source, zipfile.ZipFile(
            buffer, "w", zipfile.ZIP_DEFLATED
        ) as target:
            manifest: dict[str, Any] = {}
            try:
                loaded = json.loads(source.read("notes.json").decode("utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
            except (KeyError, ValueError, UnicodeDecodeError):
                manifest = {}
            for info in source.infolist():
                if info.filename == "notes.json":
                    continue
                target.writestr(info, source.read(info.filename))
            manifest["media"] = media_names
            target.writestr("notes.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for rel, path in media_items:
                try:
                    # 用 zf.write 流式写入，大图不会整读进内存
                    target.write(path, f"media/{rel}")
                except OSError:
                    # 单张图读不到就跳过，剩下的笔记和图片照常导出
                    continue
            target.writestr("BACKUP-README.txt", _backup_readme(len(media_names)))
    except zipfile.BadZipFile:
        return base
    return buffer.getvalue()


@router.get("/export/zip")
def export_zip(conn: sqlite3.Connection = Depends(db_conn)):
    payload = build_zip_with_media(conn)
    filename = f"inknote-notes-{now().strftime('%Y%m%d-%H%M')}.zip"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # 说明这包备份里除了笔记还带了 media/ 图片
            "X-InkNote-Export-Contains": "notes,media",
        },
    )


# ---------------------------------------------------------------------------
# 使用说明（把项目根目录的 使用说明.md 渲染成网页）
# ---------------------------------------------------------------------------
MANUAL_FILE = "使用说明.md"


@router.get("/manual")
def manual_page(request: Request):
    """直接渲染 使用说明.md，这样不用装 Markdown 阅读器也能看说明。"""
    from ..config import BASE_DIR
    from ..markdown_render import render as render_markdown

    path = BASE_DIR / MANUAL_FILE
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"没找到 {MANUAL_FILE}，它应该放在项目根目录。")
    rendered = render_markdown(path.read_text(encoding="utf-8"), title=None)
    return render(
        request,
        "manual.html",
        html=rendered.html,
        toc=rendered.toc,
        word_count=rendered.word_count,
        reading_minutes=rendered.reading_minutes,
    )


@router.get("/stats")
def stats_page(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    """统计小面板的独立页面（首页只放精简版）。"""
    stats = repo.dashboard_stats(conn)
    tags = repo.list_tags(conn, limit=30)
    months = repo.archive_months(conn, public_only=False)
    recent, _total = repo.list_notes(conn, per_page=8, sort="created")
    return render(
        request,
        "stats.html",
        stats=stats,
        tags=tags,
        months=months,
        recent=recent,
    )
