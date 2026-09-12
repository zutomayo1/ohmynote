"""账号口令：设置页改登录密码 + 登录校验。

密码来源优先级（和 ``app/services/site_settings.py`` 的机制完全一致）：

    设置页保存（meta 表，前缀 ``account.``）  →  ``.env`` 的 ``INKNOTE_PASSWORD_HASH``
    →  ``.env`` 的 ``INKNOTE_PASSWORD``（明文）

说明：
- 哈希继续用 ``app/security.py`` 的 pbkdf2_sha256 + 随机 salt，不自己发明格式。
- 明文回退用 ``hmac.compare_digest`` 比较。
- 会话 cookie 是 HMAC 签名的、跟口令无关：改密码不会让已登录设备下线。

对外接口：
    bootstrap(conn)            启动时读 meta；conn 可为 None（只取 .env 基线）
    save_password(conn, new)   校验并写入 meta（哈希），立即生效
    verify(conn, password)     按优先级校验口令，任何异常都返回 False
    describe()                 给设置页的状态：来源 / 文案；**绝不返回哈希**
    reset(conn)                清掉页面改的密码，回到 .env
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from ..config import settings
from ..security import LoginThrottle, hash_password, verify_password, verify_plain

META_PREFIX = "account."
PASSWORD_KEY = "password_hash"
MIN_PASSWORD_LENGTH = 8

# 旧密码错误节流：10 分钟内错 5 次锁 5 分钟（复用登录那套 LoginThrottle 思路）
password_throttle = LoginThrottle(limit=5, window=600, lockout=300)

# 运行期状态：只存「来源 / 是否哈希」这类安全信息，不存任何哈希值
_state: dict[str, Any] = {}


class AccountError(ValueError):
    """页面提交的新密码不合法（message 直接展示给用户）。"""


def _env_credential() -> tuple[str, str, str]:
    """``.env`` / 默认口令基线：返回 ``(kind, value, source)``。

    kind 为 ``hash``（走 pbkdf2 校验）或 ``plain``（走 compare_digest）。
    """
    if settings.password_hash:
        return "hash", settings.password_hash, "env-hash"
    if os.environ.get("INKNOTE_PASSWORD"):
        return "plain", settings.password, "env"
    return "plain", settings.password, "default"


def _effective(conn: sqlite3.Connection | None) -> tuple[str, str, str]:
    """当前生效口令：数据库 meta → .env 哈希 → .env 明文。"""
    if conn is not None:
        try:
            from .. import repo

            stored = repo.get_meta_map(conn, META_PREFIX)
            db_hash = str(stored.get(PASSWORD_KEY) or "").strip()
            if db_hash:
                return "hash", db_hash, "db"
        except Exception:  # noqa: BLE001 - 库读不出来就回退 .env，绝不把登录搞崩
            pass
    return _env_credential()


def _env_keys() -> list[str]:
    keys: list[str] = []
    if settings.password_hash:
        keys.append("INKNOTE_PASSWORD_HASH")
    if os.environ.get("INKNOTE_PASSWORD"):
        keys.append("INKNOTE_PASSWORD")
    return keys


def _label(source: str) -> str:
    if source == "db":
        return "页面已改"
    if source == "env-hash":
        return "来自 .env（哈希）"
    if source == "env":
        return "来自 .env"
    return "默认密码（inknote）"


def _snapshot(source: str) -> dict[str, Any]:
    """把来源整理成模板能直接用的安全状态（不含哈希）。"""
    return {
        "source": source,
        "label": _label(source),
        "managed": source == "db",
        "has_hash": source in ("db", "env-hash"),
        "env_keys": _env_keys(),
    }


def bootstrap(conn: sqlite3.Connection | None = None) -> None:
    """启动时调用（主 agent 在 lifespan 里接）：读 meta 覆盖 .env 基线。"""
    _kind, _value, source = _effective(conn)
    _state.clear()
    _state.update(_snapshot(source))


def _ensure() -> None:
    """没 bootstrap 过（比如 main.py 还没接线）时，先用 .env 基线初始化。"""
    if not _state:
        bootstrap(None)


def describe() -> dict[str, Any]:
    """给设置页用：来源 / 文案 / 是否页面改过。**绝不返回哈希或任何片段**。"""
    _ensure()
    return dict(_state)


def is_db_managed(conn: sqlite3.Connection | None) -> bool:
    """当前是否有「页面改的密码」覆盖 .env（登录回退路径要据此决定能不能回退）。"""
    if conn is None:
        return False
    try:
        from .. import repo

        stored = repo.get_meta_map(conn, META_PREFIX)
        return bool(str(stored.get(PASSWORD_KEY) or "").strip())
    except Exception:  # noqa: BLE001 - 读不出来就当没有，交给 .env 回退
        return False


def _clean_new_password(new_password: str) -> str:
    if not isinstance(new_password, str) or len(new_password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"新密码至少 {MIN_PASSWORD_LENGTH} 位")
    return new_password


def save_password(conn: sqlite3.Connection, new_password: str) -> dict[str, Any]:
    """校验后把新密码的哈希写进 meta，并立即生效。非法值抛 AccountError。"""
    from .. import repo

    password = _clean_new_password(new_password)
    repo.save_meta_map(conn, {PASSWORD_KEY: hash_password(password)}, META_PREFIX)
    bootstrap(conn)
    return describe()


def verify(conn: sqlite3.Connection | None, password: str) -> bool:
    """按优先级校验口令；任何格式 / 库错误都返回 False（绝不抛异常）。"""
    if not password:
        return False
    try:
        kind, value, _source = _effective(conn)
        if kind == "hash":
            return verify_password(password, value)
        return verify_plain(password, value)
    except Exception:  # noqa: BLE001 - 登录路径必须稳，校验失败就是 False
        return False


def reset(conn: sqlite3.Connection) -> None:
    """清掉页面改的密码，回到 ``.env`` / 默认口令。"""
    from .. import repo

    repo.delete_meta(conn, [f"{META_PREFIX}{PASSWORD_KEY}"])
    bootstrap(conn)
