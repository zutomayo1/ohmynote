"""全局配置。

所有配置都来自环境变量（或项目根目录的 .env 文件），这样部署时不用改代码。
读取顺序：系统环境变量 > .env 文件 > 代码里的默认值。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .utils import as_bool

logger = logging.getLogger("inknote.config")

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


def load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，支持 # 注释与成对引号，不覆盖已有环境变量。"""
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        # 读不了 .env 还有环境变量/默认值兜底，但必须留痕，否则线上「配置没生效」无从查起
        logger.warning("读取 .env 失败（path=%s），改用环境变量 / 代码默认值", path, exc_info=True)
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    return default if value is None else value.strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        # 环境变量写错了不能让进程起不来：退回默认值，但把原始值记下来方便排错
        logger.warning("环境变量 %s=%r 不是整数，改用默认值 %s", key, raw, default)
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    # 口径与表单一致，统一在 app.utils.as_bool（顺带容忍首尾空白）
    return as_bool(_env(key, "1" if default else "0"))


def _env_path(key: str, default: Path) -> Path:
    value = _env(key)
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


class Settings:
    """运行期配置对象，`settings` 是模块级单例。"""

    def __init__(self) -> None:
        # --- 站点信息 ---
        self.site_title = _env("INKNOTE_TITLE", "墨痕") or "墨痕"
        self.site_subtitle = _env("INKNOTE_SUBTITLE", "一个人的笔记与写作")
        self.site_description = _env("INKNOTE_DESCRIPTION", "这里是我的个人笔记与博客。")
        self.author = _env("INKNOTE_AUTHOR", "我")
        self.base_url = _env("INKNOTE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        self.per_page = max(4, min(_env_int("INKNOTE_PER_PAGE", 12), 100))

        # --- 存储 ---
        self.data_dir = _env_path("INKNOTE_DATA_DIR", BASE_DIR / "data")
        self.upload_dir = self.data_dir / "uploads"
        self.db_path = self.data_dir / "inknote.db"
        self.max_upload_bytes = max(64 * 1024, _env_int("INKNOTE_MAX_UPLOAD_BYTES", 8 * 1024 * 1024))
        self.allowed_image_ext = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}

        # --- 认证 ---
        self.password = _env("INKNOTE_PASSWORD", "inknote")
        self.password_hash = _env("INKNOTE_PASSWORD_HASH")
        self.secret_key = _env("INKNOTE_SECRET")
        self.session_cookie = "inknote_session"
        self.session_max_age = _env_int("INKNOTE_SESSION_DAYS", 30) * 86400

        # --- 行为 ---
        self.version_keep = max(3, _env_int("INKNOTE_VERSION_KEEP", 20))
        self.trash_days = max(1, _env_int("INKNOTE_TRASH_DAYS", 30))
        self.host = _env("INKNOTE_HOST", "127.0.0.1")
        self.port = _env_int("INKNOTE_PORT", 8000)
        self.debug = _env_bool("INKNOTE_DEBUG", False)

        # --- AI（可选） ---
        # 这些是「配置文件」这一路的配置；设置页保存的值会覆盖它们（见 services/ai.py）
        self.ai_base_url = _env("INKNOTE_AI_BASE_URL").rstrip("/")
        self.ai_api_key = _env("INKNOTE_AI_API_KEY")
        self.ai_model = _env("INKNOTE_AI_MODEL")
        self.ai_embed_model = _env("INKNOTE_AI_EMBED_MODEL")
        self.ai_timeout = _env_int("INKNOTE_AI_TIMEOUT", 45)

        self._ensure_dirs()

    # ------------------------------------------------------------------
    def _ensure_dirs(self) -> None:
        for path in (self.data_dir, self.upload_dir):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                # 建不了目录也不能让 import 阶段直接崩，但后续读写一定会失败，必须留痕
                logger.warning("创建数据目录失败（path=%s），相关功能可能不可用", path, exc_info=True)

    # ------------------------------------------------------------------
    @property
    def ai_enabled(self) -> bool:
        """三个关键项都配好才算开启 AI 功能。"""
        return bool(self.ai_base_url and self.ai_model)

    @property
    def is_default_password(self) -> bool:
        return not self.password_hash and self.password == "inknote"

    def resolve_secret(self, storage: Path | None = None) -> str:
        """签名密钥：优先环境变量，其次 data/.secret，最后内存随机值。

        持久化的好处是重启后已登录的会话不会失效。
        """
        if self.secret_key:
            return self.secret_key
        path = storage or (self.data_dir / ".secret")
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if len(value) >= 32:
                    self.secret_key = value
                    return value
        except OSError:
            # 读失败就重新生成密钥（老会话会失效），但必须留痕说明原因
            logger.warning("读取密钥文件失败（path=%s），本次改用新的随机密钥", path, exc_info=True)
        value = os.urandom(32).hex()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        except OSError:
            # 写不了只是重启后会话失效，本次仍可用；留痕方便排查「每次重启都要重登」
            logger.warning("持久化密钥失败（path=%s），密钥只在本次进程内有效", path, exc_info=True)
        self.secret_key = value
        return value


settings = Settings()
