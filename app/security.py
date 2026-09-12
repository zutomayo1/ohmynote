"""密码哈希、签名会话 Cookie、登录限流。

只依赖标准库，不引入 passlib / itsdangerous：单用户个人应用够用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any

PBKDF2_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------
def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """返回 `pbkdf2_sha256$迭代次数$盐$哈希`，可直接写进 INKNOTE_PASSWORD_HASH。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验明文密码是否匹配存储的哈希。任何格式错误都返回 False。

    ``stored`` 既可以是 ``.env`` 里的 INKNOTE_PASSWORD_HASH，也可以是
    设置页写进数据库 meta 的哈希（格式完全相同），调用方不用区分。
    """
    if not stored or not password:
        return False
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != _ALGO:
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected.hex(), digest_hex)


def verify_plain(password: str, stored: str) -> bool:
    """用 hmac.compare_digest 校验明文口令（.env 的 INKNOTE_PASSWORD 回退路径）。"""
    if not password or not stored:
        return False
    try:
        return hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8"))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 会话 Cookie：base64url(json).hmac_sha256
# ---------------------------------------------------------------------------
def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def make_session(secret: str, *, max_age: int, user: str = "owner") -> tuple[str, str]:
    """生成 (会话 token, csrf token)。csrf token 同时写进 cookie 与页面表单。"""
    csrf = secrets.token_urlsafe(24)
    payload = {"sub": user, "csrf": csrf, "iat": int(time.time()), "exp": int(time.time()) + max_age}
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}", csrf


def read_session(secret: str, token: str | None) -> dict[str, Any] | None:
    """校验并解出会话数据；签名不对、过期、格式错误都返回 None。"""
    if not token or "." not in token:
        return None
    body, _, signature = token.rpartition(".")
    if not body or not signature:
        return None
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload


# ---------------------------------------------------------------------------
# 登录限流（进程内存，单用户场景足够）
# ---------------------------------------------------------------------------
class LoginThrottle:
    """滑动窗口限流：window 秒内失败超过 limit 次就锁一段时间。"""

    def __init__(self, limit: int = 8, window: int = 600, lockout: int = 300) -> None:
        self.limit = limit
        self.window = window
        self.lockout = lockout
        self._failures: list[float] = []
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        self._failures = [t for t in self._failures if t >= cutoff]

    def _remaining_locked(self) -> int:
        """调用前必须已持有锁。返回还需等待的秒数，0 表示未被锁定。"""
        return int(self._blocked_until - time.time()) + 1 if self._blocked_until > time.time() else 0

    def blocked_for(self) -> int:
        """返回还需等待的秒数，0 表示未被锁定。"""
        with self._lock:
            return self._remaining_locked()

    def register_failure(self) -> int:
        """记录一次失败；若已触发锁定则返回剩余秒数。"""
        with self._lock:
            now = time.time()
            self._prune(now)
            self._failures.append(now)
            if len(self._failures) >= self.limit and self._blocked_until <= now:
                self._blocked_until = now + self.lockout
                self._failures.clear()
            return self._remaining_locked()

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._blocked_until = 0.0
