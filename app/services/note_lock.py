"""锁定笔记：单篇密码保护。

语义（也是使用说明里写的那套）：

- 给某一篇设密码 → 该篇标记 ``locked=1``，**正文要输入密码才显示**（阅读页与
  编辑器都挡）；密码只存 pbkdf2 派生值，库里看不到明文。
- 解锁是**按浏览器会话**的：输对一次，这个浏览器本次会话内（含重新打开标签页）
  都能看；换个浏览器、清了 cookie、或点了「重新锁定」就又要输。
- 锁定会**自动取消公开**（否则"锁了却还挂在博客上"更危险）；因此博客列表、
  RSS、sitemap、标签页、公开搜索这些原本就靠 ``is_public = 1`` 过滤的地方，
  无需逐处再判断。
- **锁定笔记不参与全文搜索、图谱与 AI**：设锁时把它从 FTS 索引里撤掉，
  解锁移除密码时再补回；相关 SQL 也统一带 ``locked = 0``。
- 导出（JSON / ZIP）仍然包含锁定笔记的正文 —— 备份的意义就是"不丢东西"，
  锁防的是随手翻看，不是防自己。导出页面会提示其中有多少篇是锁定的。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from typing import Any

from .. import repo
from ..config import settings
from ..utils import now_iso

UNLOCK_COOKIE = "inknote_unlock"
UNLOCK_MAX_AGE = 60 * 60 * 12          # 解锁状态保留 12 小时（与登录会话同量级）
_PBKDF2_ROUNDS = 120_000
_KIND = "note-unlock"


# ---------------------------------------------------------------------------
# 密码派生与校验
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """pbkdf2-hmac-sha256，返回 ``pbkdf2$轮数$盐$摘要``（自带盐，可平滑升级轮数）。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return "pbkdf2${}${}${}".format(
        _PBKDF2_ROUNDS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def verify_password(password: str, stored: str | None) -> bool:
    """校验密码；格式不对/为空一律 False（用 compare_digest 防时序侧信道）。"""
    if not password or not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2":
        return False
    try:
        rounds = int(parts[1])
        salt = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        expected = base64.urlsafe_b64decode(parts[3] + "=" * (-len(parts[3]) % 4))
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(digest, expected)


# ---------------------------------------------------------------------------
# 解锁状态：单独一枚签名 cookie（不动登录会话的格式）
# ---------------------------------------------------------------------------

def _sign(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = hmac.new(settings.secret_key.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _read(token: str | None) -> list[int]:
    if not token or "." not in token:
        return []
    body, _, signature = token.rpartition(".")
    if not body or not signature:
        return []
    expected = hmac.new(settings.secret_key.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return []
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict) or data.get("k") != _KIND:
        return []
    if int(data.get("exp", 0)) < time.time():
        return []
    ids = data.get("notes") or []
    return [int(i) for i in ids if isinstance(i, (int, str)) and str(i).isdigit()]


def unlocked_ids(request) -> list[int]:
    """当前浏览器会话已解锁的笔记 id 列表。"""
    return _read(request.cookies.get(UNLOCK_COOKIE))


def cookie_value(ids: list[int]) -> str:
    return _sign({"k": _KIND, "notes": sorted({int(i) for i in ids}), "exp": int(time.time()) + UNLOCK_MAX_AGE})


def is_open(request, note: dict[str, Any]) -> bool:
    """这篇笔记此刻能否看到正文（未锁定，或本会话已解锁）。"""
    if not note or not int(note.get("locked") or 0):
        return True
    return int(note.get("id") or 0) in unlocked_ids(request)


def add_unlocked(request, note_id: int) -> list[int]:
    ids = unlocked_ids(request)
    if note_id not in ids:
        ids.append(note_id)
    return ids


def drop_unlocked(request, note_id: int) -> list[int]:
    return [i for i in unlocked_ids(request) if i != note_id]


# ---------------------------------------------------------------------------
# 设锁 / 解绑（含搜索索引同步）
# ---------------------------------------------------------------------------

def set_lock(conn: sqlite3.Connection, note_id: int, password: str) -> dict[str, Any] | None:
    """给笔记设密码，并自动取消公开、从搜索索引里撤掉。"""
    from .. import search

    conn.execute(
        "UPDATE notes SET locked = 1, lock_hash = ?, is_public = 0, updated_at = ? WHERE id = ?",
        (hash_password(password), now_iso(), note_id),
    )
    conn.commit()
    search.remove_note(conn, note_id)
    return repo.get_note(conn, note_id)


def clear_lock(conn: sqlite3.Connection, note_id: int) -> dict[str, Any] | None:
    """解除锁定（需要先输对密码），并把正文补回搜索索引。"""
    from .. import search

    conn.execute(
        "UPDATE notes SET locked = 0, lock_hash = '', updated_at = ? WHERE id = ?",
        (now_iso(), note_id),
    )
    conn.commit()
    note = repo.get_note(conn, note_id)
    if note:
        # 与 repo/notes.py::sync_derived 的写法保持一致（标签拼成一个串交给 FTS）
        search.sync_note(conn, note_id, note.get("title") or "", note.get("content") or "",
                         " ".join(note.get("tags") or []))
    return note


def locked_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM notes WHERE locked = 1 AND deleted_at IS NULL").fetchone()
    return int(row["c"]) if row else 0
