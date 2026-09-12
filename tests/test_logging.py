"""日志落盘：文件真的写进去、幂等、级别、不可写降级、非法天数、滚动配置。"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest

from app.logging_setup import setup_logging


@pytest.fixture(autouse=True)
def isolate_logging(monkeypatch):
    """每个用例前后隔离全局 logger，避免 handler 叠加 / 级别互相污染。"""
    monkeypatch.delenv("INKNOTE_LOG_LEVEL", raising=False)
    monkeypatch.delenv("INKNOTE_LOG_DAYS", raising=False)

    loggers = [logging.getLogger("inknote"), logging.getLogger("uvicorn")]
    before = [(logger, list(logger.handlers), logger.level, logger.propagate) for logger in loggers]

    yield

    created: set[logging.Handler] = set()
    for logger in loggers:
        for handler in list(logger.handlers):
            if getattr(handler, "_inknote_handler", False):
                created.add(handler)
                logger.removeHandler(handler)
    for handler in created:
        try:
            handler.close()
        except Exception:
            pass
    for logger, handlers, level, propagate in before:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _handler_of_kind(kind: str) -> logging.Handler | None:
    for handler in logging.getLogger("inknote").handlers:
        if getattr(handler, "_inknote_kind", None) == kind:
            return handler
    return None


def _file_handler() -> logging.handlers.TimedRotatingFileHandler | None:
    handler = _handler_of_kind("file")
    assert handler is None or isinstance(handler, logging.handlers.TimedRotatingFileHandler)
    return handler


def _flush_handlers() -> None:
    for name in ("inknote", "uvicorn"):
        for handler in logging.getLogger(name).handlers:
            try:
                handler.flush()
            except Exception:
                pass


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 文件创建 + 真的能读到写进去的日志
# ---------------------------------------------------------------------------
def test_file_created_and_message_written(tmp_path):
    log_path = setup_logging(log_dir=tmp_path)

    assert log_path == tmp_path / "inknote.log"
    assert log_path.is_file()

    logging.getLogger("inknote.test").info("问笔记：流式回答保存失败（conversation=1）")
    _flush_handlers()

    text = _read(log_path)
    assert "问笔记：流式回答保存失败（conversation=1）" in text
    assert "inknote.test" in text


# ---------------------------------------------------------------------------
# 2. 幂等：连续调 3 次 handler 不增长
# ---------------------------------------------------------------------------
def test_setup_logging_is_idempotent(tmp_path):
    setup_logging(log_dir=tmp_path)
    logger = logging.getLogger("inknote")
    before_total = len(logger.handlers)
    before_file = sum(getattr(h, "_inknote_kind", None) == "file" for h in logger.handlers)

    for _ in range(3):
        setup_logging(log_dir=tmp_path)

    assert len(logger.handlers) == before_total
    assert sum(getattr(h, "_inknote_kind", None) == "file" for h in logger.handlers) == before_file == 1


# ---------------------------------------------------------------------------
# 3. 级别：DEBUG 能进文件，默认 INFO 时 debug 不进
# ---------------------------------------------------------------------------
def test_debug_level_writes_debug(tmp_path):
    setup_logging(level="DEBUG", log_dir=tmp_path)

    logging.getLogger("inknote.test").debug("调试信息")
    _flush_handlers()

    assert "调试信息" in _read(tmp_path / "inknote.log")


def test_default_info_suppresses_debug(tmp_path):
    setup_logging(log_dir=tmp_path)

    logging.getLogger("inknote.test").debug("不应出现的调试信息")
    logging.getLogger("inknote.test").info("应该出现的常规信息")
    _flush_handlers()

    text = _read(tmp_path / "inknote.log")
    assert "应该出现的常规信息" in text
    assert "不应出现的调试信息" not in text


# ---------------------------------------------------------------------------
# 4. 目录不可写：不抛异常，控制台 handler 仍然在
# ---------------------------------------------------------------------------
def test_unwritable_dir_falls_back_to_console(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"

    def _deny_mkdir(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "mkdir", _deny_mkdir)
    log_path = setup_logging(log_dir=blocked)

    assert log_path == blocked / "inknote.log"
    assert _handler_of_kind("console") is not None
    assert _file_handler() is None


# ---------------------------------------------------------------------------
# 5. 非法 INKNOTE_LOG_DAYS 回退 14
# ---------------------------------------------------------------------------
def test_invalid_log_days_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("INKNOTE_LOG_DAYS", "abc")

    setup_logging(log_dir=tmp_path)

    handler = _file_handler()
    assert handler is not None
    assert handler.backupCount == 14


# ---------------------------------------------------------------------------
# 6. 滚动配置正确
# ---------------------------------------------------------------------------
def test_rotation_config(tmp_path):
    setup_logging(log_dir=tmp_path, days=5)

    handler = _file_handler()
    assert handler is not None
    assert handler.when.upper() == "MIDNIGHT"
    assert handler.backupCount == 5
    assert (handler.encoding or "").lower() == "utf-8"


# ---------------------------------------------------------------------------
# 附加：目录来自 settings.data_dir，且 uvicorn 也共享同一批 handler
# ---------------------------------------------------------------------------
def test_default_log_dir_uses_settings_data_dir():
    from app.config import settings

    log_path = setup_logging()

    assert log_path == Path(settings.data_dir) / "logs" / "inknote.log"
    assert log_path.is_file()


def test_uvicorn_shares_inknote_handlers(tmp_path):
    setup_logging(log_dir=tmp_path)

    inknote_handlers = set(logging.getLogger("inknote").handlers)
    uvicorn_handlers = set(logging.getLogger("uvicorn").handlers)
    assert inknote_handlers
    assert inknote_handlers.issubset(uvicorn_handlers)
