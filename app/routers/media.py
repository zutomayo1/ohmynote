"""图片管理路由：/images 页面 + 删除接口。

注意：`/media/...` 是静态文件挂载点（图片本身），所以这个页面用 `/images`，
不要注册到 `/media` 上。

对外接口（冻结，见 docs/new-features-interfaces.md 3.3）：

    GET  /images                 图片库：缩略图网格 + 尺寸/日期/被哪篇笔记引用 + 孤立图片
    POST /images/delete          按 rel 删一张（被引用的要 force=1 才删），303 回 /images
    POST /images/delete-orphans  一次清掉所有没被引用的图，303 回 /images
    POST /images/cleanup-duplicates  清理重复内容里未被引用的多余副本，303 回 /images
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..deps import PageParam, csrf_protect, db_conn, require_login
from ..services import media
from ..templating import render
from ..utils import as_bool, human_size, url_with_query

router = APIRouter(dependencies=[Depends(require_login), Depends(csrf_protect)])

# 开发期自包含样式：把 .scratch/css-patch-media3.css 内联进 /images。
# 主 agent 用 .scratch/merge4.py 合并进 style.css 后，这段可以删。
_CSS_PATCH = Path(__file__).resolve().parents[2] / ".scratch" / "css-patch-media3.css"


def _load_css(path: Path) -> str:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return ""


def _truthy(value: object) -> bool:
    """唯一口径在 app.utils.as_bool；这里只是给本模块留个短名字。"""
    return as_bool(value)


def _flash(msg: str, kind: str = "ok") -> RedirectResponse:
    return RedirectResponse(url_with_query("/images", msg=msg, kind=kind), status_code=303)


def _normalize_rel(value: str) -> str:
    """把 /media/ 前缀与反斜杠归一化；「..」原样保留，交给服务层拒绝。"""
    raw = (value or "").strip().replace("\\", "/")
    if raw.startswith(media.MEDIA_PREFIX):
        raw = raw[len(media.MEDIA_PREFIX):]
    elif raw.startswith("media/"):
        raw = raw[len("media/"):]
    return raw


@router.get("/images")
def images_page(
    request: Request,
    orphan: int = 0,
    page: PageParam = 1,
    conn: sqlite3.Connection = Depends(db_conn),
):
    data = media.library(conn)
    only_orphans = bool(orphan)
    all_items = data["items"]
    if only_orphans:
        all_items = [item for item in all_items if item["orphan"]]
    # 排序在 scan() 里已经固定为「上传时间倒序」，这里只切片，不再排序。
    items, page, pages = media.paginate(all_items, page=page)
    return render(
        request,
        "images.html",
        items=items,
        stats=data,
        only_orphans=only_orphans,
        page=page,
        pages=pages,
        filtered_total=len(all_items),
        media_css=_load_css(_CSS_PATCH),
    )


@router.post("/images/delete")
def images_delete(
    request: Request,
    rel: str = Form(""),
    force: str = Form(""),
    conn: sqlite3.Connection = Depends(db_conn),
):
    del request
    data = media.library(conn)
    wanted = _normalize_rel(rel)
    item = next((entry for entry in data["items"] if entry["rel"] == wanted), None)
    if item is None:
        return _flash("要删除的图片不存在，或路径不在上传目录里。", "warn")

    if item["used_by"] and not _truthy(force):
        shown = item["used_by"][:5]
        names = "、".join(f"《{entry['title']}》" for entry in shown)
        more = "" if len(item["used_by"]) <= 5 else f" 等 {len(item['used_by'])} 篇"
        return _flash(f"图片正被{names}{more}引用，已拒绝删除；确认要删请用强制删除。", "warn")

    if not media.delete(item["rel"]):
        return _flash("删除失败：文件不存在或路径不合法。", "warn")
    action = "已强制删除" if item["used_by"] else "已删除"
    return _flash(f"{action} {item['name']}，释放 {human_size(item['size'])}。", "ok")


@router.post("/images/delete-orphans")
def images_delete_orphans(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
):
    del request
    before = media.library(conn)
    removed = media.delete_orphans(conn)
    if not removed:
        return _flash("没有可清理的孤立图片。", "ok")
    after = media.library(conn)
    freed = human_size(max(0, before["total_size"] - after["total_size"]))
    return _flash(f"已清理 {removed} 张孤立图片，释放 {freed}。", "ok")


@router.post("/images/cleanup-duplicates")
def images_cleanup_duplicates(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
):
    """清理同一内容里没被任何笔记引用的多余副本；被引用的副本永远保留。"""
    del request
    result = media.cleanup_duplicates(conn)
    removed = int(result.get("removed", 0))
    freed = int(result.get("freed", 0))
    if not removed:
        return _flash("没有可自动清理的重复副本（被笔记引用的重复只会提示，不会自动删）。", "warn")
    return _flash(f"已清理 {removed} 份多余副本，释放 {human_size(freed)}。", "ok")
