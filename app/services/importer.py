"""导入 / 恢复：把导出产物合并回数据库。

与 ``app/services/export.py`` 严格对应：

* ``build_zip`` 产出的压缩包里有 ``notes/*.md``（带 YAML front matter）和 ``notes.json``
  清单；导入时以 ``.md`` 的 front matter 为准，``notes.json`` 只做缺失字段的补充，
  这样即使有人只改了 Markdown 也能读进来。
* 也接受裸 JSON 数组、``{"notes": [...]}`` 对象，以及单篇 Markdown。

合并规则（幂等）：
1. 有 slug 按 slug 找；没有 slug 按标题（不区分大小写）找。
2. 找到 → 更新；没找到 → 新建。
3. 绝不删除 / 清空用户已有的、备份里没有的笔记。
4. 出错只记进 ``errors`` 并继续处理下一条，绝不整体失败。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from .. import repo
from ..config import settings
from ..utils import as_bool, parse_tags

logger = logging.getLogger("inknote.importer")

__all__ = ["sniff_and_import", "import_zip", "import_json", "import_markdown"]

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_CONTENT_KEYS = ("content", "body", "markdown", "text")
_STATUSES = ("draft", "saved")


# ---------------------------------------------------------------------------
# 统一返回结构
# ---------------------------------------------------------------------------
def _empty_result(source: str, dry_run: bool) -> dict[str, Any]:
    return {
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "notes": 0,
        "errors": [],
        "source": source if source in ("zip", "json", "markdown") else "markdown",
        "dry_run": bool(dry_run),
        # 图片计数（向后兼容：老调用方只读它认识的键）
        "media_total": 0,
        "media_added": 0,
        "media_skipped": 0,
        "media_renamed": 0,
    }


def result_with_error(filename: str, message: str, *, source: str = "markdown", dry_run: bool = False) -> dict[str, Any]:
    """构造一个只有错误的结果（给路由处理空文件 / 超大文件用）。"""
    result = _empty_result(source, dry_run)
    result["errors"].append(f"{filename or '上传的文件'}：{message}")
    return result


def _dry(kw: dict[str, Any]) -> bool:
    return bool(kw.get("dry_run", False))


# ---------------------------------------------------------------------------
# 入口分派
# ---------------------------------------------------------------------------
def sniff_and_import(
    conn: sqlite3.Connection,
    filename: str,
    data: bytes,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """按扩展名 / 内容分派到 zip / json / markdown 导入。"""
    name = (filename or "").strip()
    raw = data or b""
    suffix = Path(name.lower()).suffix
    if suffix == ".zip":
        source = "zip"
    elif suffix == ".json":
        source = "json"
    elif suffix in (".md", ".markdown", ".txt"):
        source = "markdown"
    else:
        source = _sniff_source(raw)

    if not raw:
        result = _empty_result(source, dry_run)
        result["errors"].append(f"{name or '上传的文件'}：文件是空的，没有可导入的内容")
        return result

    try:
        if source == "zip":
            return import_zip(conn, raw, dry_run=dry_run)
        if source == "json":
            return import_json(conn, raw, dry_run=dry_run)
        return _import_markdown_bytes(conn, raw, filename=name, dry_run=dry_run)
    except Exception as exc:  # 解析层兜底：坏文件不能把整个页面带成 500
        # 错误同时返回给用户；服务端也留一条，方便统计是哪些文件/哪类异常导致导入失败
        logger.debug(
            "导入失败（file=%s, source=%s, dry_run=%s）：%s",
            name or "-",
            source,
            dry_run,
            exc,
            exc_info=True,
        )
        result = _empty_result(source, dry_run)
        result["errors"].append(
            f"{name or '上传的文件'}：导入失败（{type(exc).__name__}: {exc}）"
        )
        return result


def _sniff_source(data: bytes) -> str:
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 不是 UTF-8 就先按 JSON 试，后面的解码/解析会给出具体错误；留痕说明判型依据
        logger.debug("内容不是有效 UTF-8（前 8 字节=%r），按 JSON 尝试解析", bytes(data[:8]))
        return "json"
    return "json" if text.lstrip()[:1] in ("{", "[") else "markdown"


def _import_markdown_bytes(
    conn: sqlite3.Connection, data: bytes, *, filename: str, dry_run: bool
) -> dict[str, Any]:
    result = _empty_result("markdown", dry_run)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 非 UTF-8 的 Markdown 直接拒绝并说明原因（已在 errors 里告诉用户）；服务端留痕
        logger.debug(
            "Markdown 文件不是有效 UTF-8（file=%s, bytes=%s）",
            filename or "-",
            len(data or b""),
        )
        result["errors"].append(
            f"{filename or '上传的文件'}：不是有效的 UTF-8 文本，无法解析（请另存为 UTF-8 后重试）"
        )
        return result
    return import_markdown(conn, text, filename=filename, dry_run=dry_run)


# ---------------------------------------------------------------------------
# zip 内的 media/ 图片：还原到 settings.upload_dir
# ---------------------------------------------------------------------------
_MEDIA_ENTRY_PREFIX = "media/"
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


def _media_rel(name: str) -> str:
    """去掉 zip 成员名的 ``media/`` 前缀；不是 media 条目返回空串。"""
    raw = (name or "").replace("\\", "/").strip()
    if raw.startswith("./"):
        raw = raw[2:]
    if not raw.lower().startswith(_MEDIA_ENTRY_PREFIX):
        return ""
    return raw[len(_MEDIA_ENTRY_PREFIX):]


def _safe_media_target(rel: str, root: Path) -> Path | None:
    """把相对路径解析成 uploads 根内的安全路径；越界 / 符号链接一律返回 None。

    顺序：先过滤 ``..``、绝对路径、盘符，再 ``resolve()`` 确认仍在 uploads 根内，
    并逐级检查符号链接——zip 里的路径不能借软链溜出上传目录。
    """
    raw = (rel or "").replace("\\", "/").strip()
    if not raw or "\x00" in raw or raw.startswith("/") or _DRIVE_PREFIX_RE.match(raw):
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    try:
        root_resolved = root.resolve()
    except OSError:
        return None
    target = root_resolved.joinpath(*parts)
    current = root_resolved
    try:
        for part in parts:
            current = current / part
            if current.is_symlink():
                return None
    except OSError:
        return None
    try:
        resolved = target.resolve()
    except OSError:
        return None
    if resolved == root_resolved or not _is_within(resolved, root_resolved):
        return None
    return target


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    """zip 条目是否声明为符号链接（Unix 模式 0120000）。"""
    mode = (info.external_attr >> 16) & 0xFFFF
    return (mode & 0o170000) == 0o120000


def _free_media_name(path: Path) -> Path:
    """同名但内容冲突时找 ``-1`` / ``-2`` 新名字；绝不覆盖已有文件。"""
    index = 1
    while index < 10000:
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1
    return path.with_name(f"{path.stem}-{os.getpid()}{path.suffix}")


def _sha256(handle: Any) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 256), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _same_archive_content(archive: zipfile.ZipFile, info: zipfile.ZipInfo, path: Path) -> bool:
    """流式比较 zip 条目与磁盘文件内容（大图不整读进内存）。"""
    try:
        if path.stat().st_size != info.file_size:
            return False
        with archive.open(info, "r") as source:
            incoming = _sha256(source)
        with open(path, "rb") as existing:
            current = _sha256(existing)
    except (OSError, zipfile.BadZipFile):
        return False
    return incoming == current


def _import_media_entries(
    archive: zipfile.ZipFile,
    result: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    """把 zip 里 ``media/`` 前缀的条目流式还原到 ``settings.upload_dir``。"""
    root = settings.upload_dir
    for info in archive.infolist():
        name = info.filename
        if name.endswith("/"):
            continue
        rel = _media_rel(name)
        if not rel:
            continue
        result["media_total"] += 1
        if _zip_is_symlink(info):
            result["errors"].append(f"{name}：符号链接，已拒绝")
            continue
        target = _safe_media_target(rel, root)
        if target is None:
            result["errors"].append(f"{name}：路径不安全，已拒绝")
            continue
        renamed = False
        try:
            if target.exists():
                if target.is_file() and _same_archive_content(archive, info, target):
                    result["media_skipped"] += 1
                    continue
                # 同名但内容不同（或目标不是普通文件）：改名保留两边，绝不覆盖
                target = _free_media_name(target)
                renamed = True
            if dry_run:
                result["media_added"] += 1
                if renamed:
                    result["media_renamed"] += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, open(target, "wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 256)
            result["media_added"] += 1
            if renamed:
                result["media_renamed"] += 1
        except (OSError, zipfile.BadZipFile) as exc:
            # 单张图片失败只记错误并继续，不能拖垮整包导入
            logger.debug("zip 内图片导入失败（file=%s）：%s", name, exc, exc_info=True)
            result["errors"].append(f"{name}：图片写入失败（{type(exc).__name__}: {exc}）")


# ---------------------------------------------------------------------------
# zip
# ---------------------------------------------------------------------------
def import_zip(conn: sqlite3.Connection, data: bytes, **kw: Any) -> dict[str, Any]:
    """导入导出压缩包：优先读 notes/*.md，用 notes.json 补缺字段。"""
    dry_run = _dry(kw)
    result = _empty_result("zip", dry_run)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        # 用户上传的不是 zip：errors 里说明了，服务端也留一条（bytes=%s）
        logger.debug(
            "压缩包无法打开（%s）：%s（bytes=%s）",
            type(exc).__name__,
            exc,
            len(data or b""),
        )
        result["errors"].append(f"压缩包无法打开：{exc or '不是有效的 zip 文件'}")
        return result

    try:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        manifest = _read_manifest(archive, names, result)
        by_file: dict[str, dict[str, Any]] = {}
        for entry in manifest:
            if isinstance(entry, dict):
                path = _as_text(entry.get("file")).lstrip("./")
                if path:
                    by_file[path] = entry

        # 图片先还原（即使 zip 里没有 Markdown，也允许只恢复图片内容）
        _import_media_entries(archive, result, dry_run=dry_run)

        candidates = [
            name
            for name in names
            if name.lower().endswith((".md", ".markdown"))
            and Path(name).name.lower() != "index.md"
        ]
        if not candidates:
            result["errors"].append("zip 里没有找到可导入的 Markdown 笔记（期望 notes/*.md）")
            return result

        for name in candidates:
            result["notes"] += 1
            entry = by_file.get(name) or by_file.get(name.lstrip("./"))
            try:
                text = _read_text(archive, name)
                meta, body = _parse_front_matter(text)
                record = _note_from_markdown(meta, body, Path(name).name)
                if entry:
                    record = _merge_manifest(record, entry)
                _merge_one(conn, record, result, dry_run=dry_run, label=name)
            except Exception as exc:
                # 单篇失败只跳过这一篇（符合「绝不整体失败」的设计），但必须留痕
                logger.debug("zip 内单篇导入失败（file=%s）：%s", name, exc, exc_info=True)
                result["skipped"] += 1
                result["errors"].append(f"{name}：{type(exc).__name__}: {exc}")
    finally:
        try:
            archive.close()
        except Exception:
            # 关闭失败没法补救（文件句柄由 GC 兜底），记 debug 即可
            logger.debug("关闭 zip 文件失败", exc_info=True)
    return result


def _read_manifest(
    archive: zipfile.ZipFile, names: list[str], result: dict[str, Any]
) -> list[Any]:
    if "notes.json" not in names:
        return []
    try:
        payload = json.loads(_read_text(archive, "notes.json"))
    except (ValueError, OSError) as exc:
        # notes.json 只用来补缺字段，坏了就当没有清单继续导入 md；errors 里也告诉用户
        logger.debug("zip 内 notes.json 解析失败：%s", exc)
        result["errors"].append(f"notes.json 解析失败：{exc}")
        return []
    if isinstance(payload, dict):
        notes = payload.get("notes")
        if notes is None:
            return [payload] if _looks_like_note(payload) else []
        return notes if isinstance(notes, list) else []
    return payload if isinstance(payload, list) else []


def _read_text(archive: zipfile.ZipFile, name: str) -> str:
    raw = archive.read(name)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 非 UTF-8（常见于 GBK 导出的旧文件）：用替换字符尽量读进来，比整篇丢掉好；留痕
        logger.debug("zip 内 %s 不是 UTF-8，改用 errors=replace 解码", name)
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# json
# ---------------------------------------------------------------------------
def import_json(conn: sqlite3.Connection, data: bytes, **kw: Any) -> dict[str, Any]:
    """兼容 ``{"notes": [...]}``、``notes.json`` 清单和裸数组。"""
    dry_run = _dry(kw)
    result = _empty_result("json", dry_run)
    try:
        text = data.decode("utf-8-sig") if isinstance(data, (bytes, bytearray)) else str(data)
    except UnicodeDecodeError:
        logger.debug(
            "JSON 文件不是有效 UTF-8（bytes=%s）",
            len(data or b"") if isinstance(data, (bytes, bytearray)) else "?",
        )
        result["errors"].append("JSON 文件不是有效的 UTF-8 文本")
        return result
    if not text.strip():
        result["errors"].append("JSON 文件是空的")
        return result
    try:
        payload = json.loads(text)
    except ValueError as exc:
        # 用户可见的解析错误已经在 errors 里；服务端留一条便于发现是导入功能的问题还是文件的问题
        logger.debug("JSON 解析失败：%s", exc)
        result["errors"].append(f"JSON 解析失败：{exc}")
        return result

    if isinstance(payload, dict):
        # JSON 里只有文件名清单、没有图片数据：认出来即可，不当成可写文件。
        media = payload.get("media")
        if isinstance(media, list):
            result["media_total"] = len(media)
        if isinstance(payload.get("notes"), list):
            items = payload["notes"]
        elif _looks_like_note(payload):
            items = [payload]
        else:
            result["errors"].append("JSON 里没有 notes 数组，也看不出是一篇笔记")
            return result
    elif isinstance(payload, list):
        items = payload
    else:
        result["errors"].append("JSON 顶层必须是数组，或包含 notes 数组的对象")
        return result

    for index, item in enumerate(items, start=1):
        result["notes"] += 1
        if not isinstance(item, dict):
            result["skipped"] += 1
            result["errors"].append(f"第 {index} 条：不是对象，已跳过")
            continue
        try:
            _merge_one(conn, _note_from_json(item), result, dry_run=dry_run, label=f"第 {index} 条")
        except Exception as exc:
            # 单条失败只跳过这一条（符合「绝不整体失败」的设计），留痕记录第几条
            logger.debug("JSON 第 %s 条导入失败：%s", index, exc, exc_info=True)
            result["skipped"] += 1
            result["errors"].append(f"第 {index} 条：{type(exc).__name__}: {exc}")
    return result


def _looks_like_note(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(set(payload) & {"title", "content", "body", "markdown", "text", "slug", "tags"})


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------
def import_markdown(
    conn: sqlite3.Connection,
    text: str,
    *,
    filename: str,
    **kw: Any,
) -> dict[str, Any]:
    """导入单篇 Markdown；标题依次取 front matter → 正文 ``# 标题`` → 文件名。"""
    dry_run = _dry(kw)
    result = _empty_result("markdown", dry_run)
    if not (text or "").strip():
        result["errors"].append(f"{filename or '上传的文件'}：文件是空的，没有可导入的内容")
        return result
    try:
        meta, body = _parse_front_matter(text or "")
        record = _note_from_markdown(meta, body, filename)
    except Exception as exc:
        # 解析失败只拒绝这一篇，errors 里已经告诉用户；服务端留痕便于定位是哪类文件
        logger.debug("Markdown 解析失败（file=%s）：%s", filename or "-", exc, exc_info=True)
        result["errors"].append(f"{filename or 'Markdown'}：解析失败（{type(exc).__name__}: {exc}）")
        return result
    result["notes"] = 1
    _merge_one(conn, record, result, dry_run=dry_run, label=filename or "Markdown")
    return result


def _note_from_markdown(meta: dict[str, Any], body: str, filename: str) -> dict[str, Any]:
    title = _as_text(meta.get("title")) or _first_heading(body)
    if not title:
        title = Path(str(filename or "")).stem.strip()
    is_public = _as_bool(meta.get("public", meta.get("is_public", meta.get("published"))))
    status = _as_text(meta.get("status")).lower()
    if status not in _STATUSES:
        status = "saved" if is_public else "draft"
    return {
        "title": title,
        "content": (body or "").rstrip(),
        "tags": _as_tag_list(meta.get("tags")),
        "category": _as_text(meta.get("category")),
        "summary": _as_text(meta.get("summary")),
        "status": status,
        "is_public": is_public,
        "slug": _as_text(meta.get("slug")),
    }


def _first_heading(text: str) -> str:
    in_fence = False
    for raw in (text or "").splitlines():
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _H1_RE.match(raw)
        if match:
            return match.group(1).strip()
    return ""


# ---------------------------------------------------------------------------
# front matter / 字段解析
# ---------------------------------------------------------------------------
def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for index in range(1, min(len(lines), 4000)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, text
    meta = _parse_meta_lines(lines[1:end])
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def _parse_meta_lines(lines: list[str]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    current: str | None = None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1] in (" ", "\t"):
            # YAML 块列表：tags:\n  - a\n  - b
            if current and raw.strip().startswith("-"):
                if not isinstance(meta.get(current), list):
                    meta[current] = []
                meta[current].append(_decode_scalar(raw.strip()[1:].strip()))
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip().lower()
        if not key:
            continue
        value = value.strip()
        if value == "":
            meta[key] = []
            current = key
        else:
            meta[key] = _decode_scalar(value)
            current = None
    return meta


def _decode_scalar(value: str) -> Any:
    value = (value or "").strip()
    if value == "":
        return ""
    if value[0] in "[{":
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            # front matter 的类 JSON 值不合法：当普通字符串用（YAML 允许不严格的写法），留痕
            logger.debug("front matter 值 %r 不是合法 JSON，按原字符串处理", value[:80])
            return value
    if value[0] in "\"'":
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            # 引号里不是合法 JSON 字符串：手工去掉首尾引号，尽量保留内容
            logger.debug("front matter 引号值 %r 无法按 JSON 解析，去掉首尾引号保留原文", value[:80])
            return value.strip("\"'")
    low = value.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    return value


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return "、".join(_as_text(item) for item in value if item is not None).strip()
    return str(value).strip()


def _as_bool(value: Any) -> bool:
    """导入包里的真值：先用统一口径，再认导入场景专有的「public / 公开」写法。"""
    if as_bool(value):
        return True
    if value is None or isinstance(value, bool):
        return False
    return str(value).strip().lower() in {"public", "公开", "已公开"}


def _as_tag_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return []
    if isinstance(value, str):
        return parse_tags(value)
    if isinstance(value, (list, tuple, set)):
        return parse_tags([_as_text(item) for item in value])
    return parse_tags([_as_text(value)])


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------
def _note_from_json(item: dict[str, Any]) -> dict[str, Any]:
    # content 缺失（如 notes.json 清单）用 None 表示「不要动现有正文」；
    # 显式给了空字符串才是「清空正文」。
    content: str | None = None
    for key in _CONTENT_KEYS:
        if item.get(key) is not None:
            content = str(item.get(key))
            break
    is_public = _as_bool(item.get("is_public", item.get("public", item.get("published"))))
    status = _as_text(item.get("status")).lower()
    if status not in _STATUSES:
        status = "saved" if is_public else "draft"
    return {
        "title": _as_text(item.get("title")),
        "content": content,
        "tags": _as_tag_list(item.get("tags")),
        "category": _as_text(item.get("category")),
        "summary": _as_text(item.get("summary")),
        "status": status,
        "is_public": is_public,
        "slug": _as_text(item.get("slug")),
    }


def _merge_manifest(record: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Markdown 里缺的字段，用 notes.json 清单补齐（不覆盖已有值）。"""
    if not _as_text(record.get("title")) and _as_text(entry.get("title")):
        record["title"] = _as_text(entry.get("title"))
    if not _as_text(record.get("slug")) and _as_text(entry.get("slug")):
        record["slug"] = _as_text(entry.get("slug"))
    if not record.get("tags") and entry.get("tags"):
        record["tags"] = _as_tag_list(entry.get("tags"))
    if not _as_text(record.get("status")) and _as_text(entry.get("status")):
        record["status"] = _as_text(entry.get("status"))
    if not _as_bool(record.get("is_public")) and _as_bool(entry.get("is_public")):
        record["is_public"] = True
    if not str(record.get("content") or "").strip() and _as_text(entry.get("content")):
        record["content"] = str(entry.get("content"))
    return record


def _find_existing(
    conn: sqlite3.Connection, *, slug: str, title: str
) -> dict[str, Any] | None:
    if slug:
        return repo.get_note_by_slug(conn, slug, public_only=False)
    if title:
        ref = repo.resolve_title(conn, title, public_only=False)
        if ref is not None and ref.note_id:
            return repo.get_note(conn, int(ref.note_id))
    return None


def _merge_one(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    result: dict[str, Any],
    *,
    dry_run: bool,
    label: str,
) -> None:
    title = _as_text(record.get("title"))
    raw_content = record.get("content")
    has_content = raw_content is not None
    content = str(raw_content or "")
    slug = _as_text(record.get("slug"))
    tags = _as_tag_list(record.get("tags"))
    category = _as_text(record.get("category"))
    summary = _as_text(record.get("summary"))
    is_public = _as_bool(record.get("is_public"))
    status = _as_text(record.get("status")).lower()
    if status not in _STATUSES:
        status = "saved" if is_public else "draft"

    if not title and not content.strip() and not slug:
        result["skipped"] += 1
        result["errors"].append(f"{label}：没有标题也没有正文，已跳过")
        return

    existing = _find_existing(conn, slug=slug, title=title)
    if existing is not None:
        if not dry_run:
            repo.update_note(
                conn,
                int(existing["id"]),
                title=title or existing["title"],
                content=(content if has_content else None),
                tags=tags,
                category=category,
                summary=summary,
                status=status,
                is_public=is_public,
                slug=(slug or None),
                reason="import",
            )
        result["updated"] += 1
    else:
        if not dry_run:
            repo.create_note(
                conn,
                title=title,
                content=content,
                tags=tags,
                category=category,
                summary=summary,
                status=status,
                is_public=is_public,
                slug=slug,
            )
        result["created"] += 1
