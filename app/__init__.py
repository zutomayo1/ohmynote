"""墨痕 InkNote —— 个人笔记 + 博客。

导入 `create_app` 即可拿到 FastAPI 应用；`app` 是默认实例（uvicorn app.main:app）。
"""

from .main import app, create_app  # noqa: F401

__all__ = ["app", "create_app"]
__version__ = "1.0.0"
