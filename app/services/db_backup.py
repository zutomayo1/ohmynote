"""数据库自动备份：用 SQLite 在线备份 API 生成一致性快照，支持列表 / 回滚 / 删除。

为什么不用 ``shutil.copy``：
应用开着 WAL 时，「主库文件 + -wal」才是完整数据，直接复制 ``.db`` 可能拿到
「已提交内容还没 checkpoint 进主库」的不一致快照。``sqlite3.Connection.backup()``
会在源连接上做一致性读、写进目标连接，天然跨 WAL，也不会要求停写。

对外接口（主 agent 在 lifespan / 守护线程里接线用）::

    create_snapshot(conn, *, reason="manual", keep=7, sanitized=False) -> dict
    list_snapshots(*, limit=50) -> list[dict]
    maybe_auto_backup(conn, *, interval_hours=24, keep=7) -> dict | None
    restore_snapshot(conn, name) -> dict
    delete_snapshot(name) -> bool
    backup_dir() -> Path

备份文件放在 ``settings.data_dir / "backups"``，命名形如
``inknote-<YYYYMMDD-HHMMSS>-<reason>.db``，其中 reason ∈ manual / auto /
before-restore。备份目录是普通文件目录，直接把整个 ``data/backups/`` 拷走即可。

``sanitized=True`` 生成「脱敏备份」：在临时文件里做完快照后，用独立连接把
``ai.api_key`` / ``account.password_hash`` 两个 meta 值置为空串，再改名成
``inknote-<时间戳>-<原因>-sanitized.db``。**只动快照，绝不动当前库。**
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..config import settings
from ..utils import ISO_FMT, now, parse_dt

logger = logging.getLogger("inknote.db_backup")

# 合法的快照原因（用于列表里显示中文、以及文件名的解析）
REASONS = ("manual", "auto", "before-restore")
REASON_LABELS = {
    "manual": "手动",
    "auto": "自动",
    "before-restore": "回滚前",
    "unknown": "未知",
}

# 脱敏备份要清空的 meta 键（存在才清；其余键一个不动）。
SANITIZED_META_KEYS = ("ai.api_key", "account.password_hash",
                       "backup.remote.password", "backup.remote.user")

# 同一进程内串行化「生成 / 回滚 / 删除」，避免两个自动备份同时滚动删除。
_LOCK = threading.RLock()

_MAX_NAME_LENGTH = 200


# ---------------------------------------------------------------------------
# 路径与文件名安全
# ---------------------------------------------------------------------------
def backup_dir() -> Path:
    """备份目录：``data/backups/``（不存在则创建）。"""
    directory = Path(settings.data_dir) / "backups"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 建不出来时后续操作会自然报错；这里只是尽量保证目录存在。
        pass
    return directory


def _is_safe_name(name: str, directory: Path | None = None) -> bool:
    """判断是不是一个允许访问的备份文件名（防目录穿越）。

    只放行：非空、不长、不含 ``/`` 与 ``\\``、不含 ``..``、以 ``.db`` 结尾、
    ``Path(name).name == name``（即本身就是一个纯文件名）、解析后父目录仍然是
    ``backups/`` 的文件名。
    """
    text = str(name or "")
    if not text or len(text) > _MAX_NAME_LENGTH:
        return False
    if "/" in text or "\\" in text or ".." in text:
        return False
    if any(ord(ch) < 32 for ch in text):
        return False
    if not text.lower().endswith(".db"):
        return False
    if Path(text).name != text:  # 绝对路径 / 带目录成分都会在这里被挡
        return False
    root = directory or backup_dir()
    try:
        candidate = (root / text).resolve()
        resolved_root = root.resolve()
    except OSError:
        return False
    return candidate.parent == resolved_root


def is_safe_snapshot_name(name: str) -> bool:
    """公开的布尔版校验，页面 / 测试可以直接用。"""
    return _is_safe_name(name)


def _snapshot_path(name: str) -> Path:
    """校验并返回备份文件路径；不合法直接 ``ValueError``（调用方转 400/404）。"""
    if not _is_safe_name(name):
        raise ValueError(f"备份文件名不合法：{name!r}")
    return backup_dir() / str(name).strip()


def _is_sanitized_name(name: str) -> bool:
    """文件名是否带 ``-sanitized`` 标记（冲突序号是 ``-sanitized-2``）。"""
    stem = name[:-3] if name.lower().endswith(".db") else name
    return re.search(r"-sanitized(?:-\d+)?$", stem) is not None


def _reason_of(name: str) -> str:
    """从文件名里解析原因；解析不出返回 ``unknown``（不会猜成 manual）。"""
    stem = name[:-3] if name.lower().endswith(".db") else name
    if not stem.startswith("inknote-"):
        return "unknown"
    parts = stem[len("inknote-") :].split("-")
    if len(parts) < 3:
        return "unknown"
    tail = "-".join(parts[2:])
    # 脱敏备份在原因后面还挂着 ``-sanitized``（冲突时 ``-sanitized-2``），先剥掉
    tail = re.sub(r"-sanitized(?:-\d+)?$", "", tail)
    for reason in REASONS:
        if tail == reason or re.fullmatch(re.escape(reason) + r"-\d+", tail):
            return reason
    return "unknown"


def _normalize_reason(reason: str) -> str:
    text = str(reason or "").strip().lower()
    if text in REASONS:
        return text
    safe = re.sub(r"[^a-z0-9-]+", "-", text).strip("-")
    return safe[:40] or "manual"


def _unlink(path: Path) -> bool:
    """删除备份文件及其可能存在的 WAL 边车文件；返回是否真的删了。"""
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("删除备份失败：%s（%s）", path, exc)
        return False
    for suffix in ("-wal", "-shm", "-journal"):
        side = Path(str(path) + suffix)
        if side.exists():
            try:
                side.unlink()
            except OSError:
                pass
    return True


# ---------------------------------------------------------------------------
# 列表 / 生成
# ---------------------------------------------------------------------------
def list_snapshots(*, limit: int = 50) -> list[dict]:
    """按修改时间倒序列出备份：``{"name","size","mtime","reason","sanitized"}``。"""
    directory = backup_dir()
    if not directory.is_dir():
        return []
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        logger.warning("读取备份目录失败：%s（%s）", directory, exc)
        return []

    rows: list[tuple[float, dict]] = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            if not _is_safe_name(entry.name, directory):
                continue
            stat = entry.stat()
        except OSError:
            continue
        stamp = float(stat.st_mtime)
        rows.append(
            (
                stamp,
                {
                    "name": entry.name,
                    "size": int(stat.st_size),
                    "mtime": datetime.fromtimestamp(stamp).strftime(ISO_FMT),
                    "reason": _reason_of(entry.name),
                    "sanitized": _is_sanitized_name(entry.name),
                },
            )
        )

    rows.sort(key=lambda item: (item[0], item[1]["name"]), reverse=True)
    result = [item for _, item in rows]
    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = 50
    if count >= 1:
        result = result[:count]
    return result


def _prune_auto(keep: int, directory: Path | None = None) -> list[str]:
    """滚动删除「超出 keep 份」的自动备份；手动 / 回滚前 / 脱敏的一律不动。"""
    try:
        keep_count = int(keep)
    except (TypeError, ValueError):
        keep_count = 7
    if keep_count < 1:
        keep_count = 1

    autos = [
        item
        for item in list_snapshots(limit=1000)
        if item["reason"] == "auto" and not item.get("sanitized")
    ]
    removed: list[str] = []
    for item in autos[keep_count:]:
        try:
            path = _snapshot_path(item["name"])
        except ValueError:
            continue
        if _unlink(path):
            removed.append(item["name"])
    if removed:
        logger.info("滚动清理自动备份 %s 份：%s", len(removed), removed)
    return removed


def _connection_db_path(conn: sqlite3.Connection) -> str | None:
    """问出这条连接的主库文件路径（``:memory:`` 返回 None）。

    用 ``PRAGMA database_list`` 而不是 ``settings.db_path``：连接可能指向别的库文件。
    """
    try:
        for row in conn.execute("PRAGMA database_list"):
            # row = (seq, name, file)
            if str(row[1]) == "main":
                value = str(row[2] or "")
                return value or None
    except sqlite3.Error:
        return None
    return None


def _sanitize_snapshot_file(path: Path) -> None:
    """用独立连接打开快照文件，把敏感 meta 键置为空串（只动这个文件）。"""
    conn = sqlite3.connect(str(path), timeout=15.0)
    try:
        with conn:
            placeholders = ",".join("?" for _ in SANITIZED_META_KEYS)
            conn.execute(
                f"UPDATE meta SET value = '' WHERE key IN ({placeholders})",
                SANITIZED_META_KEYS,
            )
    finally:
        conn.close()


def export_sanitized_copy(source: Path, dest: Path) -> None:
    """把 ``source`` 快照复制到 ``dest`` 并抹掉敏感 meta 键（复用同一份键清单）。

    远端备份默认走这条：上传出去的是「不含 AI 密钥 / 登录口令哈希 / WebDAV 口令」
    的副本。注意它是**文件级复制 + 就地把敏感键置空**，只动 dest，绝不碰源文件。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    _sanitize_snapshot_file(dest)


def create_snapshot(
    conn: sqlite3.Connection,
    *,
    reason: str = "manual",
    keep: int = 7,
    sanitized: bool = False,
) -> dict:
    """用 ``conn.backup(dest)`` 生成一份一致性快照，返回快照信息。

    ``sanitized=True`` 时生成「脱敏备份」：先在临时文件里正常备份，再用独立
    连接把 ``meta`` 里的 ``ai.api_key`` / ``account.password_hash`` 置为空串，
    最后原子改名成 ``inknote-<时间戳>-<原因>-sanitized.db``。**只动快照文件，
    不动当前库。**

    返回值：``{"name","path","size","created_at","kept","removed","sanitized"}``。
    ``removed`` 是本次滚动清理掉的自动备份名列表；``kept`` 是清理后剩余的
    自动备份份数。手动 / before-restore / 脱敏备份不会被滚动删除。
    """
    reason = _normalize_reason(reason)
    sanitized = bool(sanitized)
    directory = backup_dir()

    with _LOCK:
        stamp = now().strftime("%Y%m%d-%H%M%S")
        base = f"inknote-{stamp}-{reason}"
        if sanitized:
            base += "-sanitized"
        path = directory / f"{base}.db"
        index = 2
        while path.exists():
            path = directory / f"{base}-{index}.db"
            index += 1

        # 先写临时文件（后缀不是 .db，list_snapshots 不会把半成品列出去），
        # 脱敏 / 校验都通过后再原子改名成正式文件；下载时永远只会拿到成品。
        temp_path = path.with_name(path.name + ".part")
        _unlink(temp_path)

        # 先把调用方可能挂着的写事务落盘：否则快照看不到本轮改动，
        # 而且同一个连接既持有写锁又去当 backup 源会自己等自己（实测会永久挂住）。
        try:
            conn.commit()
        except sqlite3.Error:
            pass

        # 用一根独立的只读连接当源：WAL 下读者不会被写事务挡住，
        # 拿到的也是一致的、已提交的快照。
        # 注意：源路径必须从 conn 自己问（测试里 conn 可能指向别的库文件，
        # 用 settings.db_path 会备份到错的库 —— 踩过）。
        db_file = _connection_db_path(conn)
        source: sqlite3.Connection | None = None
        if db_file:
            try:
                source = sqlite3.connect(
                    f"file:{Path(db_file).as_posix()}?mode=ro", uri=True, timeout=15.0
                )
            except sqlite3.Error:
                source = None
        backup_source = source or conn

        dest = sqlite3.connect(str(temp_path), timeout=15.0)
        try:
            with dest:
                backup_source.backup(dest)
        except Exception:
            # 失败时别留下半成品
            dest.close()
            if source is not None:
                source.close()
            _unlink(temp_path)
            raise
        dest.close()
        if source is not None:
            source.close()

        if sanitized:
            try:
                _sanitize_snapshot_file(temp_path)
            except Exception:
                _unlink(temp_path)
                raise

        try:
            temp_path.replace(path)
        except OSError:
            _unlink(temp_path)
            raise

        size = int(path.stat().st_size)
        removed = _prune_auto(keep)
        kept = sum(
            1
            for item in list_snapshots(limit=1000)
            if item["reason"] == "auto" and not item.get("sanitized")
        )
        return {
            "name": path.name,
            "path": str(path),
            "size": size,
            "created_at": now().strftime(ISO_FMT),
            "kept": kept,
            "removed": removed,
            "sanitized": sanitized,
        }


def maybe_auto_backup(
    conn: sqlite3.Connection, *, interval_hours: int = 24, keep: int = 7
) -> dict | None:
    """启动时 / 每小时调一次：距上次自动备份不足 ``interval_hours`` 就返回 None。

    幂等：反复调用只会在真正超过间隔（或从来没有自动备份）时生成一份。
    """
    try:
        hours = float(interval_hours)
    except (TypeError, ValueError):
        hours = 24.0
    if hours <= 0:
        hours = 24.0

    # 检查 + 生成放在同一把锁里：避免启动调用和每小时守护线程同时各生成一份。
    with _LOCK:
        last_auto = None
        for item in list_snapshots(limit=1000):
            if item["reason"] == "auto" and not item.get("sanitized"):
                last_auto = item
                break  # 列表已按时间倒序，第一条就是最新的自动备份

        if last_auto is not None:
            stamp = parse_dt(last_auto["mtime"])
            if stamp is not None and (now() - stamp).total_seconds() < hours * 3600:
                return None

        return create_snapshot(conn, reason="auto", keep=keep)


# ---------------------------------------------------------------------------
# 回滚 / 删除
# ---------------------------------------------------------------------------
def _assert_healthy(source: sqlite3.Connection, name: str) -> None:
    """回滚前先确认备份文件本身能打开且完整，避免把坏数据写回现网库。"""
    try:
        row = source.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"备份文件不是有效的 SQLite 数据库：{name}") from exc
    if not row or str(row[0]).strip().lower() != "ok":
        raise ValueError(f"备份文件未通过完整性检查，已拒绝回滚：{name}")


def restore_snapshot(conn: sqlite3.Connection, name: str) -> dict:
    """把备份写回**当前连接**：先自动保护现场，再 ``source.backup(conn)``。

    注意：这里绝不替换 ``.db`` 文件、也不删 WAL，只是让当前连接的数据变成
    备份里的样子，避免出现「主库已换、-wal 还是旧数据」的撕裂状态。
    返回 ``{"restored": 备份名, "safety": 回滚前现场备份名}``。
    """
    path = _snapshot_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"备份不存在：{name}")

    with _LOCK:
        source = sqlite3.connect(str(path), timeout=15.0)
        try:
            _assert_healthy(source, path.name)
            safety = create_snapshot(conn, reason="before-restore")["name"]
            source.backup(conn)
        finally:
            source.close()
        logger.info("已从备份 %s 回滚；回滚前数据另存为 %s", path.name, safety)
        return {"restored": path.name, "safety": safety}


def delete_snapshot(name: str) -> bool:
    """删除指定备份；文件名非法抛 ``ValueError``，文件不存在返回 False。"""
    path = _snapshot_path(name)
    with _LOCK:
        return _unlink(path)
