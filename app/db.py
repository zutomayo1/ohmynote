"""SQLite 连接与表结构。

设计取舍：
- 个人应用，不需要连接池；每次请求开一个短连接（SQLite 开销极小），
  天然避免多线程共用连接的问题。
- 开启 WAL，读写不互相阻塞；busy_timeout 处理偶发并发。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT    NOT NULL DEFAULT '',
    slug             TEXT    NOT NULL DEFAULT '',
    content          TEXT    NOT NULL DEFAULT '',
    summary          TEXT    NOT NULL DEFAULT '',
    category         TEXT    NOT NULL DEFAULT '',
    meta_description TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'draft',   -- draft 草稿 / saved 已保存
    is_public        INTEGER NOT NULL DEFAULT 0,
    is_pinned        INTEGER NOT NULL DEFAULT 0,
    is_starred       INTEGER NOT NULL DEFAULT 0,
    word_count       INTEGER NOT NULL DEFAULT 0,
    reading_minutes  INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    published_at     TEXT,
    deleted_at       TEXT                                -- 非空即在回收站
);

CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes (deleted_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_public  ON notes (is_public, deleted_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_trash   ON notes (deleted_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_slug ON notes (slug) WHERE slug <> '';

CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS note_tags (
    note_id INTEGER NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags (id)   ON DELETE CASCADE,
    PRIMARY KEY (note_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_note_tags_tag ON note_tags (tag_id);

CREATE TABLE IF NOT EXISTS note_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id    INTEGER NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
    title      TEXT    NOT NULL DEFAULT '',
    content    TEXT    NOT NULL DEFAULT '',
    tags       TEXT    NOT NULL DEFAULT '',
    summary    TEXT    NOT NULL DEFAULT '',
    reason     TEXT    NOT NULL DEFAULT 'manual',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_versions_note ON note_versions (note_id, created_at DESC);

CREATE TABLE IF NOT EXISTS note_links (
    source_id    INTEGER NOT NULL REFERENCES notes (id) ON DELETE CASCADE,
    target_id    INTEGER,
    target_title TEXT    NOT NULL,
    PRIMARY KEY (source_id, target_title)
);
CREATE INDEX IF NOT EXISTS idx_links_target ON note_links (target_id);

CREATE TABLE IF NOT EXISTS templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    content     TEXT    NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 100,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS blog_stats (
    slug  TEXT    PRIMARY KEY,
    likes INTEGER NOT NULL DEFAULT 0,
    reads INTEGER NOT NULL DEFAULT 0
);
"""

# 新版本若给已有表加字段，写在这里即可（启动时自动 ALTER TABLE）
MIGRATIONS: dict[str, dict[str, str]] = {
    # sort_order：置顶笔记之间的手工顺序（列表页拖拽写入；非置顶一律 0）
    "notes": {
        "is_archived": "INTEGER NOT NULL DEFAULT 0",
        "sort_order": "INTEGER NOT NULL DEFAULT 0",
    },
    "note_versions": {},
    "templates": {},
}


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else settings.db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 8000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def db(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """with db() as conn: ... 正常结束提交，出错回滚。"""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: str | Path | None = None) -> None:
    """建表 + 迁移 + 初始化全文索引。可重复执行。"""
    from . import search  # 延迟导入：search 依赖 db

    with db(path) as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        search.ensure_schema(conn)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        if not columns:
            continue
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_meta_map(conn: sqlite3.Connection, prefix: str = "") -> dict[str, str]:
    """取出以 prefix 开头的所有配置项，键会去掉前缀（如 ai.model）。"""
    if prefix:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ? ESCAPE '\\'",
            (prefix.replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchall()
    else:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    return {row["key"][len(prefix) :]: row["value"] for row in rows}


def save_meta_map(conn: sqlite3.Connection, values: dict[str, str], prefix: str = "") -> None:
    for key, value in values.items():
        set_meta(conn, f"{prefix}{key}", str(value))


def delete_meta(conn: sqlite3.Connection, keys) -> None:
    for key in keys:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
