"""日志落盘设置。

把 ``inknote.*`` 的日志同时写到 ``<data_dir>/logs/inknote.log``，按天滚动；
控制台输出继续保留，并让 ``uvicorn`` 的日志也走同一批 handler。

设计原则：
- 所有异常都降级为「只打控制台」，绝不让日志问题拦住服务启动；
- 重复调用不叠加 handler（幂等），只更新级别 / 保留天数等配置。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from .config import settings

LOGGER_NAME = "inknote"
UVICORN_LOGGER_NAME = "uvicorn"
LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "inknote.log"

DEFAULT_LEVEL = logging.INFO
DEFAULT_DAYS = 14

# 打在 handler 上的私有标记，用来识别「本模块加的 handler」，避免重复安装
_MARKER = "_inknote_handler"
_KIND = "_inknote_kind"
_FILE_ONLY = "inknote_file_only"

_FORMAT = "%(asctime)s %(levelname)-5s %(name)s  %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _ConsoleFilter(logging.Filter):
    """启动横幅已经由 ``print`` 打过一次，标记 file_only 的记录不再重复打控制台。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, _FILE_ONLY, False)


def _mark(handler: logging.Handler, kind: str) -> None:
    setattr(handler, _MARKER, True)
    setattr(handler, _KIND, kind)


def _find_handler(logger: logging.Logger, kind: str) -> logging.Handler | None:
    for handler in logger.handlers:
        if getattr(handler, _MARKER, False) and getattr(handler, _KIND, None) == kind:
            return handler
    return None


def _resolve_level(level: str | int | None) -> tuple[int, bool]:
    """返回 (级别, 是否用了默认值兜底)。"""
    if isinstance(level, int):
        return level, False
    raw = os.environ.get("INKNOTE_LOG_LEVEL") if level is None else level
    if raw is None or not str(raw).strip():
        return DEFAULT_LEVEL, False
    text = str(raw).strip()
    if text.lstrip("+-").isdigit():
        try:
            return int(text), False
        except ValueError:
            pass
    resolved = logging.getLevelName(text.upper())
    if isinstance(resolved, int):
        return resolved, False
    return DEFAULT_LEVEL, True


def _resolve_days(days: int | None) -> tuple[int, bool]:
    """返回 (保留天数, 是否用了默认值兜底)。非法 / 小于 1 都回退到 14。"""
    raw = os.environ.get("INKNOTE_LOG_DAYS") if days is None else days
    if raw is None or not str(raw).strip():
        return DEFAULT_DAYS, False
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_DAYS, True
    if value < 1:
        return DEFAULT_DAYS, True
    return value, False


def _make_console_handler() -> logging.StreamHandler:
    """造一个尽量兼容 Windows GBK 控制台的 StreamHandler。"""
    handler = logging.StreamHandler()
    try:
        stream = getattr(handler, "stream", None)
        # 能切 UTF-8 就切，出问题也不影响日志继续走默认编码
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    handler.addFilter(_ConsoleFilter())
    _mark(handler, "console")
    return handler


def _make_file_handler(path: Path, days: int) -> logging.handlers.TimedRotatingFileHandler:
    handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(path),
        when="midnight",
        backupCount=days,
        encoding="utf-8",
    )
    _mark(handler, "file")
    return handler


def _detach(handler: logging.Handler) -> None:
    for name in (LOGGER_NAME, UVICORN_LOGGER_NAME):
        logger = logging.getLogger(name)
        if handler in logger.handlers:
            logger.removeHandler(handler)
    try:
        handler.close()
    except Exception:
        pass


def setup_logging(*, level: str | int | None = None, log_dir: Path | str | None = None,
                  days: int | None = None) -> Path:
    """安装 inknote / uvicorn 的日志 handler，返回日志文件路径。

    - ``level``：日志级别，默认读 ``INKNOTE_LOG_LEVEL``，再默认 ``INFO``；
    - ``log_dir``：日志目录，默认 ``settings.data_dir / "logs"``；
    - ``days``：文件保留天数，默认读 ``INKNOTE_LOG_DAYS``，再默认 14。

    目录建不了或文件打不开时退回「只打控制台」，只记一条 warning，不抛异常。
    """
    logger = logging.getLogger(LOGGER_NAME)
    uvicorn_logger = logging.getLogger(UVICORN_LOGGER_NAME)
    logger.propagate = False
    uvicorn_logger.propagate = False

    base_dir = Path(log_dir) if log_dir is not None else Path(settings.data_dir) / LOG_DIR_NAME
    log_path = base_dir / LOG_FILE_NAME
    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    # 先装控制台：后面的 warning 才有地方可去，也保证 uvicorn / 横幅不会消失
    console_handler = _find_handler(logger, "console")
    if console_handler is None:
        console_handler = _make_console_handler()
        logger.addHandler(console_handler)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.NOTSET)

    level_value, level_bad = _resolve_level(level)
    logger.setLevel(level_value)
    uvicorn_logger.setLevel(level_value)
    if level_bad:
        logger.warning("INKNOTE_LOG_LEVEL=%r 不是合法日志级别，改用默认值 INFO", os.environ.get("INKNOTE_LOG_LEVEL"))

    days_value, days_bad = _resolve_days(days)
    if days_bad:
        logger.warning("INKNOTE_LOG_DAYS=%r 不是正整数，改用默认值 %d", os.environ.get("INKNOTE_LOG_DAYS"), DEFAULT_DAYS)

    file_handler = _find_handler(logger, "file")
    # 换了目录就拆掉旧文件 handler，避免继续往旧路径写
    if file_handler is not None and Path(file_handler.baseFilename).resolve() != log_path.resolve():
        _detach(file_handler)
        file_handler = None

    if file_handler is None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = _make_file_handler(log_path, days_value)
        except OSError:
            # 写不了文件可以接受，服务不能因此起不来
            file_handler = None
            logger.warning("日志文件不可写，退回只输出控制台（path=%s）", log_path, exc_info=True)
        except Exception:
            # 文件系统之外的问题也一并兜住，保持「绝不因日志崩溃」的承诺
            file_handler = None
            logger.warning("初始化日志文件失败，退回只输出控制台（path=%s）", log_path, exc_info=True)
        else:
            logger.addHandler(file_handler)

    if file_handler is not None:
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.NOTSET)
        try:
            file_handler.backupCount = days_value
        except Exception:
            pass

    # uvicorn 与 inknote 共用同一批 handler，访问日志才会同时落盘
    for handler in (console_handler, file_handler):
        if handler is not None and handler not in uvicorn_logger.handlers:
            uvicorn_logger.addHandler(handler)

    return log_path
