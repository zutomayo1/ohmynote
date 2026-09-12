"""备份与恢复：导出入口 + 导入（zip / json / md）+ 数据库快照页面。

对外接口：

    GET  /backup                  页面：导出入口 + 导入表单 + 数据库备份列表
    POST /backup/import           上传文件并合并回库，303 回 /backup（flash 摘要）
    POST /backup/create           立即生成一份数据库快照（reason=manual）
    POST /backup/create-sanitized 生成脱敏快照（清空 AI 密钥 / 密码哈希，可安全分享）
    GET  /backup/download/{name}  下载某份快照（FileResponse）
    POST /backup/restore/{name}   回滚（先自动保护现场，再写回当前连接）
    POST /backup/delete/{name}    删除某份快照
    GET  /export/json             JSON 全量导出（zip 之外的结构化入口）

合并逻辑在 ``app/services/importer.py``；快照逻辑在 ``app/services/db_backup.py``。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response

from .. import repo
from ..config import settings
from ..deps import csrf_protect, db_conn, require_login
from ..services import db_backup, importer
from ..templating import render
from ..utils import as_bool, human_size, now, now_iso, url_with_query

router = APIRouter(dependencies=[Depends(require_login), Depends(csrf_protect)])

# 进程内记住「最近一次导入结果」，供 /backup 页面显示。
# 个人单进程应用够用；重启后清空（页面会显示「还没有导入记录」）。
_LAST_RESULT: dict | None = None
_LAST_LOCK = threading.Lock()

# 开发期让页面自带样式：主 agent 把补丁合并进 style.css 后这段可删。
_CSS_PATCH = Path(__file__).resolve().parents[2] / ".scratch" / "css-patch-backup.css"
_CSS_PATCH2 = Path(__file__).resolve().parents[2] / ".scratch" / "css-patch-backup2.css"
_CSS_PATCH3 = Path(__file__).resolve().parents[2] / ".scratch" / "css-patch-backup3.css"


def _load_css(*paths: Path) -> str:
    chunks: list[str] = []
    for path in paths:
        try:
            if path.is_file():
                chunks.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(chunks)


def _remember(result: dict) -> None:
    global _LAST_RESULT
    with _LAST_LOCK:
        _LAST_RESULT = result


def _last_result() -> dict | None:
    with _LAST_LOCK:
        return _LAST_RESULT


def _summary(result: dict) -> str:
    prefix = "预览：" if result.get("dry_run") else ""
    text = (
        f"{prefix}新建 {int(result.get('created', 0))} 篇 · "
        f"更新 {int(result.get('updated', 0))} 篇 · "
        f"跳过 {int(result.get('skipped', 0))} 篇"
    )
    errors = result.get("errors") or []
    if errors:
        text += f" · {len(errors)} 条错误"
    return text


def _source_for(filename: str) -> str:
    suffix = Path((filename or "").lower()).suffix
    if suffix == ".zip":
        return "zip"
    if suffix == ".json":
        return "json"
    return "markdown"


@router.get("/backup")
def backup_page(request: Request):
    return render(
        request,
        "backup.html",
        result=_last_result(),
        backup_css=_load_css(_CSS_PATCH, _CSS_PATCH2, _CSS_PATCH3),
        snapshots=db_backup.list_snapshots(),
        backups_dir=str(db_backup.backup_dir()),
        reason_labels=db_backup.REASON_LABELS,
    )


# ---------------------------------------------------------------------------
# 数据库快照：立即备份 / 下载 / 回滚 / 删除
# ---------------------------------------------------------------------------
def _resolve_backup(name: str) -> Path:
    """把页面传来的 name 解析成 backups/ 里的真实文件；非法一律 404。"""
    if not db_backup.is_safe_snapshot_name(name):
        raise HTTPException(status_code=404, detail="备份不存在或文件名不合法")
    path = db_backup.backup_dir() / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="备份不存在")
    return path


@router.post("/backup/create")
def backup_create(conn: sqlite3.Connection = Depends(db_conn)):
    result = db_backup.create_snapshot(conn, reason="manual")
    msg = f"已生成备份 {result['name']}（{human_size(result['size'])}）"
    if result.get("removed"):
        msg += f"，并清理了 {len(result['removed'])} 份过旧的自动备份"
    return RedirectResponse(url_with_query("/backup", msg=msg), status_code=303)


@router.post("/backup/create-sanitized")
def backup_create_sanitized(conn: sqlite3.Connection = Depends(db_conn)):
    """生成脱敏快照：清空快照里的 AI 密钥与密码哈希，适合发给别人。"""
    result = db_backup.create_snapshot(conn, reason="manual", sanitized=True)
    msg = (
        f"已生成脱敏备份 {result['name']}（{human_size(result['size'])}；"
        "已清空 AI 密钥与密码哈希，可安全分享）"
    )
    if result.get("removed"):
        msg += f"，并清理了 {len(result['removed'])} 份过旧的自动备份"
    return RedirectResponse(url_with_query("/backup", msg=msg), status_code=303)


@router.get("/backup/download/{name}")
def backup_download(name: str):
    path = _resolve_backup(name)
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.post("/backup/restore/{name}")
def backup_restore(name: str, conn: sqlite3.Connection = Depends(db_conn)):
    try:
        result = db_backup.restore_snapshot(conn, name)
    except ValueError:
        raise HTTPException(status_code=404, detail="备份不存在或文件名不合法")
    except FileNotFoundError:
        return RedirectResponse(
            url_with_query("/backup", msg=f"备份不存在：{name}", kind="warn"),
            status_code=303,
        )
    msg = f"已回滚到 {result['restored']}；回滚前的数据已另存为 {result['safety']}"
    return RedirectResponse(url_with_query("/backup", msg=msg), status_code=303)


@router.post("/backup/delete/{name}")
def backup_delete(name: str):
    try:
        removed = db_backup.delete_snapshot(name)
    except ValueError:
        raise HTTPException(status_code=404, detail="备份不存在或文件名不合法")
    if removed:
        return RedirectResponse(url_with_query("/backup", msg=f"已删除备份 {name}"), status_code=303)
    return RedirectResponse(
        url_with_query("/backup", msg=f"备份不存在：{name}", kind="warn"), status_code=303
    )


@router.post("/backup/import")
async def backup_import(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    file: UploadFile = File(...),
    dry_run: str = Form(""),
):
    del request
    preview = as_bool(dry_run)
    filename = file.filename or "上传的文件"
    raw = await file.read()

    if len(raw) > settings.max_upload_bytes:
        result = importer.result_with_error(
            filename,
            f"文件太大（{len(raw)} 字节），上限 {settings.max_upload_bytes} 字节",
            source=_source_for(filename),
            dry_run=preview,
        )
    else:
        result = importer.sniff_and_import(conn, filename, raw, dry_run=preview)

    _remember(result)
    return RedirectResponse(
        url_with_query(
            "/backup",
            msg=_summary(result),
            kind="warn" if result.get("errors") else "ok",
        ),
        status_code=303,
    )


# ---------------------------------------------------------------------------
# JSON 全量导出（现有导出路由只有 /export/zip，这个补上结构化入口）
# ---------------------------------------------------------------------------
@router.get("/export/json")
def export_json(conn: sqlite3.Connection = Depends(db_conn)):
    notes = repo.all_notes(conn, sort="created")
    payload = {
        "app": "inknote",
        "format": "inknote-json-v1",
        "count": len(notes),
        "exported_at": now_iso(),
        "exported_from": settings.site_title,
        "base_url": settings.base_url,
        "notes": [
            {
                "title": note.get("title") or "",
                "slug": note.get("slug") or "",
                "content": note.get("content") or "",
                "tags": note.get("tags") or [],
                "category": note.get("category") or "",
                "summary": note.get("summary") or "",
                "status": note.get("status") or "draft",
                "is_public": bool(note.get("is_public")),
                "created_at": note.get("created_at"),
                "updated_at": note.get("updated_at"),
            }
            for note in notes
        ],
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    filename = f"inknote-notes-{now().strftime('%Y%m%d-%H%M')}.json"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
