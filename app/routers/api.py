"""前端用到的 JSON 接口：实时预览、自动保存、图片上传、快捷搜索、AI。

写操作统一走 CSRF 校验（X-CSRF-Token 头或 _csrf 字段），未登录返回 401 JSON。
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from .. import markdown_render, repo, search as search_mod
from ..config import settings
from ..deps import LimitParam, NoteId, csrf_protect, db_conn, require_login_api
from ..services import ai, media
from ..utils import human_size, now, parse_dt

router = APIRouter(
    prefix="/api",
    tags=["api"],
    dependencies=[Depends(require_login_api), Depends(csrf_protect)],
)

logger = logging.getLogger("inknote.api")


def _json(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


# 常见图片格式的文件头（magic bytes）：仅靠扩展名不够，这里再验一次内容
IMAGE_SIGNATURES: tuple[bytes, ...] = (
    b"\x89PNG\r\n\x1a\n",          # png
    b"\xff\xd8\xff",               # jpeg
    b"GIF87a", b"GIF89a",          # gif
    b"BM",                         # bmp
)


def looks_like_image(raw: bytes) -> bool:
    """校验文件内容是否像图片：PNG/JPEG/GIF/BMP 看文件头，WebP/AVIF 看 RIFF/ftyp 结构。"""
    if any(raw.startswith(signature) for signature in IMAGE_SIGNATURES):
        return True
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return True
    if len(raw) >= 12 and raw[4:8] == b"ftyp" and raw[8:12] in (b"avif", b"avis", b"heic", b"mif1"):
        return True
    return False


def safe_alt(name: str) -> str:
    """图片 alt 文本里不能出现会破坏 Markdown 语法的字符。"""
    cleaned = re.sub(r"[\[\]()\\`]", "", name or "").strip()
    return cleaned or "image"


async def read_json(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        # 前端偶发空 body / 非 JSON：按空 payload 处理不 500，但要留痕
        logger.warning("API：请求体 JSON 解析失败（path=%s）", request.url.path, exc_info=True)
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# 实时预览
# ---------------------------------------------------------------------------
@router.post("/preview")
async def preview(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    payload = await read_json(request)
    content = str(payload.get("content") or "")
    title = str(payload.get("title") or "").strip() or None
    result = markdown_render.render(content, title=title, resolver=repo.make_resolver(conn))
    return _json(
        {
            "html": result.html,
            "toc": result.toc,
            "word_count": result.word_count,
            "reading_minutes": result.reading_minutes,
            "excerpt": result.excerpt,
            "empty": not content.strip(),
        }
    )


# ---------------------------------------------------------------------------
# 自动保存
# ---------------------------------------------------------------------------
@router.patch("/notes/{note_id}")
async def autosave(
    note_id: NoteId,
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
):
    payload = await read_json(request)
    existing = repo.get_note(conn, note_id, include_deleted=True)
    if existing is None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    if existing["deleted_at"]:
        raise HTTPException(status_code=409, detail="这篇笔记在回收站里，无法自动保存")

    title = payload.get("title")
    content = payload.get("content")
    if title is None and content is None:
        return _json({"ok": True, "id": note_id, "skipped": True})

    note = repo.update_note(
        conn,
        note_id,
        title=None if title is None else str(title),
        content=None if content is None else str(content),
        reason="autosave",
    )
    if note is None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    stamp = parse_dt(note["updated_at"])
    return _json(
        {
            "ok": True,
            "id": note_id,
            "saved_at": stamp.strftime("%H:%M:%S") if stamp else "",
            "updated_at": note["updated_at"],
            "word_count": note["word_count"],
            "reading_minutes": note["reading_minutes"],
            "excerpt": note["summary"],
        }
    )


# ---------------------------------------------------------------------------
# 图片上传
# ---------------------------------------------------------------------------
@router.post("/upload")
async def upload(
    files: list[UploadFile] = File(default=[]),
    file: list[UploadFile] = File(default=[]),
):
    incoming = [item for item in [*file, *files] if item and item.filename]
    if not incoming:
        return _json({"ok": False, "error": "没有收到文件"}, 400)

    results = []
    for item in incoming:
        results.append(await _save_image(item))
    first = results[0]
    if not first.get("ok"):
        return _json(first, 400)
    return _json(
        {
            "ok": True,
            "url": first["url"],
            "markdown": first["markdown"],
            "name": first["name"],
            "size": first["size"],
            "size_text": human_size(first["size"]),
            # 新增字段：本次上传是否命中了已有内容（结构向后兼容）
            "deduped": bool(first.get("deduped")),
            "items": results,
        }
    )


async def _save_image(item: UploadFile) -> dict:
    original = Path(item.filename or "image").name
    suffix = Path(original).suffix.lower()
    if suffix not in settings.allowed_image_ext:
        return {
            "ok": False,
            "error": f"不支持的图片格式 {suffix or '（无扩展名）'}，"
            f"仅接受 {'、'.join(sorted(settings.allowed_image_ext))}",
            "name": original,
        }

    raw = await item.read()
    if not raw:
        return {"ok": False, "error": "文件是空的", "name": original}
    if len(raw) > settings.max_upload_bytes:
        return {
            "ok": False,
            "error": f"图片太大（{human_size(len(raw))}），上限 {human_size(settings.max_upload_bytes)}",
            "name": original,
        }
    if not looks_like_image(raw):
        return {
            "ok": False,
            "error": "这个文件的内容看起来不是图片（扩展名对不上），已拒绝。",
            "name": original,
        }

    digest = hashlib.sha256(raw).hexdigest()
    # 内容去重：uploads 里已有相同 sha256 就直接复用，不写新文件
    existing = media.find_by_sha256(digest, upload_dir=settings.upload_dir)
    if existing is not None:
        url = existing["url"]
        return {
            "ok": True,
            "url": url,
            "markdown": f"![{safe_alt(Path(original).stem)}]({url})",
            "name": existing["name"],
            "original": original,
            "size": len(raw),
            "deduped": True,
        }

    stamp = now()
    relative = Path(f"{stamp.year:04d}") / f"{stamp.month:02d}"
    target_dir = settings.upload_dir / relative
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{digest[:16]}{suffix}"
    target = target_dir / filename
    if not target.exists():
        target.write_bytes(raw)
    media.fingerprint(target)  # 新文件顺手缓存指纹，下次扫描不用重算

    url = f"/media/{relative.as_posix()}/{filename}"
    return {
        "ok": True,
        "url": url,
        "markdown": f"![{safe_alt(Path(original).stem)}]({url})",
        "name": filename,
        "original": original,
        "size": len(raw),
        "deduped": False,
    }


# ---------------------------------------------------------------------------
# 快捷搜索（命令面板）
# ---------------------------------------------------------------------------
@router.get("/search")
def quick_search(
    q: str = "",
    limit: LimitParam = 20,
    conn: sqlite3.Connection = Depends(db_conn),
):
    query = q.strip()
    if not query:
        return _json({"query": "", "count": 0, "items": []})
    limit = max(1, min(int(limit or 20), 50))
    notes = repo.search_notes(conn, query, limit=limit)
    tokens = search_mod.tokenize(query)
    items = [
        {
            "id": note["id"],
            "title": note["title"],
            "title_html": search_mod.highlight(note["title"], tokens),
            "url": note["url"],
            "snippet": note["snippet"],
            "snippet_html": search_mod.highlight(note["snippet"], tokens),
            "updated_at": note["updated_at"],
            "status": note["status"],
            "is_public": note["is_public"],
        }
        for note in notes
    ]
    return _json(
        {
            "query": query,
            "count": len(items),
            "items": items,
            "engine": "fts5-trigram" if (search_mod.FTS_ENABLED and search_mod.FTS_TOKENIZER == "trigram") else "like",
        }
    )


