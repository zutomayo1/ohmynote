"""图片库服务：扫描 uploads、统计笔记引用、内容去重、安全删除。

对外接口（冻结，见 docs/new-features-interfaces.md 3.3）：

    scan(upload_dir)            -> 列出 uploads 里的图片（含 sha256 指纹）
    usage(conn)                 -> {url: [引用它的笔记...]}
    library(conn, upload_dir=)  -> 页面要的全部数据（含重复统计）
    delete(rel, upload_dir=)    -> 安全删除单张
    delete_orphans(conn)        -> 清掉所有没被引用的图片
    duplicates(conn)            -> 按 sha256 分组的重复图片
    cleanup_duplicates(conn)    -> 只删每组里没被引用的多余副本

内容指纹方案：文件旁写 ``<name>.sha256`` 边车文件（纯文本 64 位十六进制）。
理由：不依赖数据库（db.py 不归本组改、还要兼容老库/回滚）、随文件走、老图片
首次扫描时懒计算回填；边车文件在 scan() 里被过滤，不会混进图片列表。

安全原则：任何删除都必须落在 uploads 根目录**里面**（Path.resolve() 后判断，
不走 ../、绝对路径、符号链接）。这里只做「文件系统 + 引用统计」，
「被引用的默认拒绝删除」由路由层把关。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..config import settings

MEDIA_PREFIX = "/media/"
_ISO_FMT = "%Y-%m-%d %H:%M:%S"
# 图片库每页 24 张（路由与测试共用，避免两边写死不一致）。
PER_PAGE = 24
# 内容指纹边车后缀：<image>.sha256
FINGERPRINT_SUFFIX = ".sha256"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# 正文里的 /media/ 链接：到空白、引号、右括号、反引号等为止。
# 前面的负向后顾避免把 https://example.com/media/x.png 这种外链当成站内引用。
_MEDIA_RE = re.compile(r"(?<![A-Za-z0-9./_\-])/media/[^\s\"'<>)\]}`]+")


# ---------------------------------------------------------------------------
# 正文解析
# ---------------------------------------------------------------------------
def _clean_url(raw: str) -> str:
    """把匹配到的片段整理成规范 URL（去掉查询串/锚点/百分号编码/尾部标点）。"""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    path = unquote(parsed.path)
    if not path.startswith(MEDIA_PREFIX):
        return ""
    # 尾部的句读通常属于正文，不属于 URL
    return path.rstrip(".,;:!?、，。；：！？")


def extract_media_urls(content: str | None) -> set[str]:
    """从一段 Markdown/HTML 正文里抽出所有 /media/... 图片链接。"""
    if not content:
        return set()
    found: set[str] = set()
    for match in _MEDIA_RE.finditer(str(content)):
        url = _clean_url(match.group(0))
        if url:
            found.add(url)
    return found


# ---------------------------------------------------------------------------
# 内容指纹（sha256）
# ---------------------------------------------------------------------------
def _sidecar_path(image: Path) -> Path:
    return image.with_name(image.name + FINGERPRINT_SUFFIX)


def _hash_file(path: Path) -> str:
    """分块计算文件 sha256；读不了返回空串（不抛异常）。"""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _cached_fingerprint(image: Path) -> str:
    """读边车缓存；没有、过期或内容不合法都返回空串。"""
    sidecar = _sidecar_path(image)
    try:
        if not sidecar.is_file():
            return ""
        # 图片比边车新 → 缓存可能过期，宁可重算
        if sidecar.stat().st_mtime < image.stat().st_mtime:
            return ""
        cached = sidecar.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""
    return cached if _HEX64_RE.fullmatch(cached) else ""


def fingerprint(image: Path | str) -> str:
    """返回图片的 sha256（64 位十六进制），并尽量回填 / 复用 <name>.sha256 边车。

    老图片没有边车时在这里「懒计算」（scan() 时自动补齐）。任何读写失败都
    不抛异常：最坏情况返回空串，页面照常可用。
    """
    path = Path(image)
    cached = _cached_fingerprint(path)
    if cached:
        return cached
    digest = _hash_file(path)
    if not digest:
        return ""
    try:
        _sidecar_path(path).write_text(digest + "\n", encoding="utf-8")
    except OSError:
        pass  # 写不了缓存不影响本次使用，只是下次还要再算一遍
    return digest


def find_by_sha256(digest: str, *, upload_dir: Path | str | None = None) -> dict | None:
    """在上传目录里找内容 sha256 相同的文件；找不到返回 None。"""
    wanted = str(digest or "").strip().lower()
    if not _HEX64_RE.fullmatch(wanted):
        return None
    for item in scan(upload_dir):
        if item.get("sha256") == wanted:
            return item
    return None


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
def _root(upload_dir: Path | str | None) -> Path:
    return Path(upload_dir) if upload_dir else settings.upload_dir


def scan(upload_dir: Path | str | None = None) -> list[dict]:
    """递归列出 uploads 下的所有文件，按修改时间倒序。"""
    root = _root(upload_dir)
    try:
        if not root.is_dir():
            return []
    except OSError:
        return []

    items: list[dict] = []
    try:
        candidates = list(root.rglob("*"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            if not path.is_file():
                continue
            # 边车指纹不是图片，不能出现在图库/统计里
            if path.name.endswith(FINGERPRINT_SUFFIX):
                continue
            stat = path.stat()
            rel = path.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        mtime = float(stat.st_mtime)
        items.append(
            {
                "name": path.name,
                "rel": rel,
                "url": f"{MEDIA_PREFIX}{rel}",
                "size": int(stat.st_size),
                "mtime": mtime,
                "uploaded": datetime.fromtimestamp(mtime).strftime(_ISO_FMT),
                "sha256": fingerprint(path),
            }
        )
    # 同一秒上传的文件用 rel 兜底，保证顺序稳定
    items.sort(key=lambda item: (item["mtime"], item["rel"]), reverse=True)
    return items


# ---------------------------------------------------------------------------
# 引用统计
# ---------------------------------------------------------------------------
def usage(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """扫全部笔记正文里的 /media/ 链接，返回 {url: [{id,title,url}, ...]}。

    回收站里的笔记也算引用（图片删了就没法恢复了），所以这里不筛 deleted_at。
    """
    refs: dict[str, list[dict]] = {}
    try:
        rows = conn.execute("SELECT id, title, content FROM notes ORDER BY id").fetchall()
    except sqlite3.Error:
        return refs
    for row in rows:
        urls = extract_media_urls(row["content"])
        if not urls:
            continue
        note_id = int(row["id"])
        entry = {
            "id": note_id,
            "title": (row["title"] or "").strip() or "无标题笔记",
            "url": f"/notes/{note_id}",
        }
        for url in urls:
            refs.setdefault(url, []).append(entry)
    return refs


# ---------------------------------------------------------------------------
# 内容去重
# ---------------------------------------------------------------------------
def _duplicate_groups(
    items: list[dict], refs: dict[str, list[dict]]
) -> list[dict]:
    """把 scan() 的结果按 sha256 分组，只返回至少 2 份的组。

    组内文件按 (mtime, rel) 升序，方便 cleanup 时「都没有引用就留最早那份」。
    """
    buckets: dict[str, list[dict]] = {}
    for item in items:
        digest = str(item.get("sha256") or "").strip().lower()
        if not digest:
            continue
        buckets.setdefault(digest, []).append(item)

    groups: list[dict] = []
    for digest, bucket in buckets.items():
        if len(bucket) < 2:
            continue
        ordered = sorted(bucket, key=lambda item: (item["mtime"], item["rel"]))
        files = [
            {
                "rel": item["rel"],
                "url": item["url"],
                "size": int(item["size"]),
                "used_by": list(refs.get(item["url"], [])),
            }
            for item in ordered
        ]
        groups.append(
            {
                "sha256": digest,
                "files": files,
                "count": len(files),
                # 同内容大小必然相同：多出来的 (count-1) 份就是理论上可省的字节
                "duplicated_bytes": (len(files) - 1) * files[0]["size"],
            }
        )
    groups.sort(key=lambda group: (group["count"], group["duplicated_bytes"]), reverse=True)
    return groups


def duplicates(
    conn: sqlite3.Connection, *, upload_dir: Path | str | None = None
) -> list[dict]:
    """按 sha256 分组返回重复图片。

    每组：``{"sha256", "files": [{"rel","url","size","used_by"}], "count",
    "duplicated_bytes"}``；``duplicated_bytes`` 指删到只剩一份能省下的字节。
    """
    return _duplicate_groups(scan(upload_dir), usage(conn))


def _cleanup_targets(group: dict) -> list[dict]:
    """一组重复里该删哪些：每组至少留一份，其余只删「没被任何笔记引用」的。

    保留优先级：被引用的那份 > 最早的那份。被引用的副本永远不自动删。
    """
    files = group["files"]
    keep = next((idx for idx, entry in enumerate(files) if entry["used_by"]), 0)
    return [
        entry
        for idx, entry in enumerate(files)
        if idx != keep and not entry["used_by"]
    ]


def cleanup_duplicates(
    conn: sqlite3.Connection, *, upload_dir: Path | str | None = None
) -> dict:
    """清理重复图片里没被引用的多余副本，返回 ``{"removed": n, "freed": bytes}``。"""
    groups = _duplicate_groups(scan(upload_dir), usage(conn))
    removed = 0
    freed = 0
    for group in groups:
        for entry in _cleanup_targets(group):
            if delete(entry["rel"], upload_dir=upload_dir):
                removed += 1
                freed += int(entry["size"])
    return {"removed": removed, "freed": freed}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def library(conn: sqlite3.Connection, *, upload_dir: Path | str | None = None) -> dict:
    """给 /images 页面用的完整数据（含引用、孤立与重复统计）。"""
    items = scan(upload_dir)
    refs = usage(conn)
    groups = _duplicate_groups(items, refs)

    # 每个文件的「同伴数」：同 sha256 组里除自己之外还有几份
    peers: dict[str, int] = {}
    for group in groups:
        for entry in group["files"]:
            peers[entry["url"]] = group["count"] - 1

    removable_count = 0
    removable_size = 0
    for group in groups:
        for entry in _cleanup_targets(group):
            removable_count += 1
            removable_size += int(entry["size"])

    total_size = 0
    orphan_size = 0
    orphan_count = 0
    for item in items:
        used_by = refs.get(item["url"], [])
        item["used_by"] = used_by
        item["orphan"] = not used_by
        item["duplicate_count"] = peers.get(item["url"], 0)
        total_size += item["size"]
        if item["orphan"]:
            orphan_count += 1
            orphan_size += item["size"]
    return {
        "items": items,
        "total": len(items),
        "orphan_count": orphan_count,
        "total_size": total_size,
        "orphan_size": orphan_size,
        "duplicate_groups": len(groups),
        "duplicate_count": sum(group["count"] for group in groups),
        # 理论可省（把每组删到只剩一份）
        "duplicate_bytes": sum(group["duplicated_bytes"] for group in groups),
        # 实际可省：cleanup_duplicates 真正会删的字节
        "duplicate_removable": removable_count,
        "duplicate_removable_bytes": removable_size,
    }


# ---------------------------------------------------------------------------
# 分页
# ---------------------------------------------------------------------------
def paginate(
    items: list[dict],
    *,
    page: int = 1,
    per_page: int = PER_PAGE,
) -> tuple[list[dict], int, int]:
    """把已排好序的列表切成当前页，返回 (当前页条目, 合法页码, 总页数)。

    页码越界（<1 或超出总页数）会自动收敛到合法范围，这样 URL 里的
    `?page=999` 不会渲染出一个假的「还没有图片」空状态。空列表固定为
    `(空, 1, 1)`，让空状态逻辑简单。
    """
    per_page = max(1, int(per_page or PER_PAGE))
    total = len(items)
    pages = max(1, math.ceil(total / per_page)) if total else 1
    page = max(1, min(int(page or 1), pages))
    start = (page - 1) * per_page
    return items[start : start + per_page], page, pages


# ---------------------------------------------------------------------------
# 安全删除
# ---------------------------------------------------------------------------
def _is_within(child: Path, root: Path) -> bool:
    """child 是否真的在 root 目录里面（含 Windows 大小写差异的兜底）。"""
    try:
        if child.is_relative_to(root):
            return True
    except (ValueError, OSError):
        pass
    left = [os.path.normcase(part) for part in child.parts]
    right = [os.path.normcase(part) for part in root.parts]
    return len(left) > len(right) and left[: len(right)] == right


def _resolve_target(path_or_rel: str, root: Path) -> Path | None:
    """把用户输入解析成 uploads 里的真实文件路径；越界一律返回 None。"""
    raw = (path_or_rel or "").strip()
    if not raw or "\x00" in raw:
        return None
    if raw.startswith(MEDIA_PREFIX):
        raw = raw[len(MEDIA_PREFIX) :]
    try:
        root_resolved = root.resolve()
    except OSError:
        return None
    candidate = Path(raw)
    target = candidate if candidate.is_absolute() else root_resolved / raw
    try:
        resolved = target.resolve()
    except OSError:
        return None
    if resolved == root_resolved or not _is_within(resolved, root_resolved):
        return None
    return resolved


def delete(path_or_rel: str, *, upload_dir: Path | str | None = None) -> bool:
    """删除 uploads 里的一张图片；越界、目录、不存在都返回 False。"""
    target = _resolve_target(path_or_rel, _root(upload_dir))
    if target is None:
        return False
    try:
        if not target.is_file():  # 目录 / 特殊文件一律不动
            return False
        target.unlink()
    except OSError:
        return False
    # 顺手清掉边车指纹，别在上传目录里留孤儿小文件
    try:
        _sidecar_path(target).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def delete_orphans(conn: sqlite3.Connection, *, upload_dir: Path | str | None = None) -> int:
    """删掉所有没被引用的图片，返回成功删除的数量。"""
    data = library(conn, upload_dir=upload_dir)
    removed = 0
    for item in data["items"]:
        if item["orphan"] and delete(item["rel"], upload_dir=upload_dir):
            removed += 1
    return removed
