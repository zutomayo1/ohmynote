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
    POST /backup/remote/save      保存远端备份（WebDAV）配置
    POST /backup/remote/test      按表单值试连一次（只读，不写任何东西；
                                  fetch 请求回 JSON，表单请求回 303）
    POST /backup/remote/run       立即上传一份到远端
    POST /backup/remote/reset     清空远端配置（含口令）

合并逻辑在 ``app/services/importer.py``；快照逻辑在 ``app/services/db_backup.py``。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .. import repo
from ..config import settings
from ..deps import csrf_protect, db_conn, require_login
from ..services import db_backup, importer, remote_backup
from ..templating import render
from ..utils import as_bool, human_size, now, now_iso, url_with_query

router = APIRouter(dependencies=[Depends(require_login), Depends(csrf_protect)])

# 进程内记住「最近一次导入结果」，供 /backup 页面显示。
# 个人单进程应用够用；重启后清空（页面会显示「还没有导入记录」）。
_LAST_RESULT: dict | None = None
_LAST_LOCK = threading.Lock()

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
def backup_page(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    cfg = remote_backup.config(conn)
    return render(
        request,
        "backup.html",
        result=_last_result(),
        snapshots=db_backup.list_snapshots(),
        backups_dir=str(db_backup.backup_dir()),
        reason_labels=db_backup.REASON_LABELS,
        remote={
            "url": cfg["url"],
            "user": cfg["user"],
            "interval_hours": cfg["interval_hours"],
            "keep": cfg["keep"],
            "full": cfg["full"],
            # 口令绝不回显：只给一个「已保存」的掩码
            "has_password": bool(cfg["password"]),
            "password_mask": remote_backup.mask(cfg["password"]),
        },
        remote_status=remote_backup.status(conn),
    )


# ---------------------------------------------------------------------------
# 远端备份（WebDAV）：配置 / 试连 / 立即上传 / 清空
# ---------------------------------------------------------------------------


def _back(msg: str, kind: str = "ok") -> RedirectResponse:
    return RedirectResponse(url_with_query("/backup", msg=msg, kind=kind), status_code=303)


@router.post("/backup/remote/save")
def backup_remote_save(
    url: str = Form(""),
    user: str = Form(""),
    password: str = Form(""),
    interval_hours: str = Form("24"),
    keep: str = Form("7"),
    full: str | None = Form(None),
    conn: sqlite3.Connection = Depends(db_conn),
):
    try:
        cfg = remote_backup.save(conn, {
            "url": url, "user": user, "password": password,
            "interval_hours": interval_hours, "keep": keep, "full": as_bool(full),
        })
    except remote_backup.RemoteError as exc:
        return _back(f"保存失败：{exc}", kind="warn")

    if not cfg["url"]:
        return _back("已清空远端地址（不再自动上传）")
    if cfg["interval_hours"] <= 0:
        return _back("已保存；间隔为 0 —— 只在点「立即上传」时才传")
    return _back(f"已保存；每 {cfg['interval_hours']} 小时自动上传一份，远端保留 {cfg['keep']} 份")


@router.post("/backup/remote/test")
def backup_remote_test(
    request: Request,
    url: str = Form(""),
    user: str = Form(""),
    password: str = Form(""),
    interval_hours: str = Form("24"),
    keep: str = Form("7"),
    full: str | None = Form(None),
    conn: sqlite3.Connection = Depends(db_conn),
):
    """按页面上的值试连（口令留空就用已保存的那个），只读不写。"""
    try:
        cfg = remote_backup.validate({
            "url": url, "user": user, "password": password,
            "interval_hours": interval_hours, "keep": keep, "full": as_bool(full),
        })
    except remote_backup.RemoteError as exc:
        return _back(f"配置有误：{exc}", kind="warn")
    if not cfg["password"]:
        cfg["password"] = remote_backup.config(conn)["password"]
    result = remote_backup.probe(cfg)

    # 页面里的「测试连接」走 fetch：**不能整页刷新**，否则刚填的地址/口令全没了
    # （用户得重新输一遍密码，等于惩罚他先测试再保存）。无 JS 时仍走 303 回退。
    if (request.headers.get("x-requested-with") or "").lower() == "fetch":
        return JSONResponse({"ok": bool(result["ok"]), "message": result["message"]})
    return _back(result["message"], kind="ok" if result["ok"] else "warn")


@router.post("/backup/remote/run")
def backup_remote_run(conn: sqlite3.Connection = Depends(db_conn)):
    result = remote_backup.run_upload(conn, trigger="manual")
    return _back(result["message"], kind="ok" if result["ok"] else "warn")


@router.post("/backup/remote/reset")
def backup_remote_reset(conn: sqlite3.Connection = Depends(db_conn)):
    remote_backup.reset(conn)
    return _back("已清空远端备份配置（口令一并删除）")


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
    # 允许一次选多个文件（multiple）：字段名仍是 file，单个文件的老调用方不受影响
    files: list[UploadFile] = File(..., alias="file"),
    dry_run: str = Form(""),
):
    del request
    preview = as_bool(dry_run)

    merged: dict | None = None
    for upload in files:
        filename = upload.filename or "上传的文件"
        raw = await upload.read()
        if len(raw) > settings.max_upload_bytes:
            result = importer.result_with_error(
                filename,
                f"文件太大（{len(raw)} 字节），上限 {settings.max_upload_bytes} 字节",
                source=_source_for(filename),
                dry_run=preview,
            )
        else:
            result = importer.sniff_and_import(conn, filename, raw, dry_run=preview)
        if merged is None:
            merged = result
            continue
        for key in ("created", "updated", "skipped", "notes", "media_total",
                    "media_added", "media_skipped", "media_renamed"):
            merged[key] = int(merged.get(key, 0)) + int(result.get(key, 0))
        merged["errors"] = list(merged.get("errors") or []) + list(result.get("errors") or [])
    result = merged or importer.result_with_error("", "没有收到文件", dry_run=preview)

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
