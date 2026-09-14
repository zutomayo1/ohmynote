"""应用入口：create_app() / app。

启动时会：初始化数据库、写入默认笔记模板、清理过期的回收站内容、打印访问信息。
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from . import db as db_mod
from . import logging_setup
from . import repo
from .config import STATIC_DIR, settings
from .deps import SAFE_METHODS, RedirectException, current_session, db_conn
from .services import ai
from .services import account, db_backup, site_settings
from .services import tidy as tidy_service
from .services import ai_embed as ai_embed_service
from .routers import (ai_admin, ai_embed, api, ask, auth, backup, blog, media, meta,
                      notes, pages, ai_inline)
from .templating import render

logger = logging.getLogger("inknote")

DESCRIPTION = "个人笔记 + 博客：Markdown 书写、双链、全文搜索、发布为博客。"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.resolve_secret()
    # 日志落盘（run.py 里已经调过一次，这里是幂等的第二道保险：
    # 直接用 uvicorn 起、或别的启动方式，也能写进 data/logs/）
    logging_setup.setup_logging()
    db_mod.init_db()
    with db_mod.db() as conn:
        repo.seed_templates(conn)
        purged = repo.purge_expired_trash(conn)
        # 读一次设置页里保存的 AI 配置（页面保存的优先于 .env）
        ai.bootstrap(conn)
        site_settings.bootstrap(conn)  # 站点信息：设置页保存的值覆盖 .env
        account.bootstrap(conn)  # 登录密码：设置页改过的优先于 .env
        try:
            snapshot = db_backup.maybe_auto_backup(conn, interval_hours=24, keep=7)
        except Exception:  # 备份失败绝不能拦着服务启动
            snapshot = None
            logger.warning("自动备份失败，服务继续启动", exc_info=True)
        if snapshot:
            logger.info("已自动备份数据库：%s（%s 字节）", snapshot.get("name"), snapshot.get("size"))
    if purged:
        logger.info("已清理 %s 篇超过 %s 天的回收站笔记", purged, settings.trash_days)
    _print_banner()
    _start_workers()
    try:
        yield
    finally:
        _stop_workers()


def _workers_enabled() -> bool:
    """后台线程总开关：设 INKNOTE_WORKERS=0 可关掉（测试里会关，保证行为确定）。"""
    return os.environ.get("INKNOTE_WORKERS", "").strip().lower() not in {"0", "false", "no", "off"}


_WORKER_STOPS: list[threading.Event] = []


def _spawn_worker(name: str, interval: float, work, *, run_immediately: bool) -> None:
    """起一个按 interval 秒轮询的后台守护线程。

    - ``run_immediately=True``：线程一起来就先干一次（索引要尽快追上启动前改过的笔记）；
    - ``run_immediately=False``：先等满一个 interval（备份在 lifespan 里刚做过一次）。
    - 停止用 ``threading.Event`` 而不是 ``sleep``：lifespan 在测试里会被反复触发，
      不回收就会每起一次 TestClient 漏两个永不退出的线程。
    """
    stop = threading.Event()
    _WORKER_STOPS.append(stop)

    def loop() -> None:
        if not run_immediately and stop.wait(interval):
            return
        while True:
            try:
                work()
            except Exception:  # 线程里的异常只能记下来，绝不能让线程死掉
                logger.warning("%s 出错", name, exc_info=True)
            if stop.wait(interval):
                return

    threading.Thread(target=loop, name=name, daemon=True).start()


def _stop_workers() -> None:
    """通知所有后台线程退出（shutdown 时调用，幂等）。"""
    while _WORKER_STOPS:
        _WORKER_STOPS.pop().set()


def _tick_index() -> None:
    """跑一次增量索引（没配向量模型 / 没到间隔它自己会立刻返回 None）。"""
    with db_mod.db() as conn:
        result = ai_embed_service.maybe_auto_index(conn, interval_minutes=30)
    if result:
        logger.info("向量索引已自动跟进：%s", result)


def _tick_backup() -> None:
    """看一眼该不该自动备份（是否该做由 db_backup 内部按时间判断，幂等）。"""
    with db_mod.db() as conn:
        snapshot = db_backup.maybe_auto_backup(conn, interval_hours=24, keep=7)
    if snapshot:
        logger.info("已自动备份数据库：%s", snapshot.get("name"))


def _tick_tidy() -> None:
    """看一眼该不该跑夜间整理（是否该做由 tidy.maybe_tidy 内部按时间 / 开关判断，幂等）。"""
    with db_mod.db() as conn:
        result = tidy_service.maybe_tidy(conn, interval_hours=24)
    if result:
        logger.info("夜间整理完成：%s", result)


def _start_workers() -> None:
    """启动后台守护线程：向量索引每 30 分钟跟进一次，备份 / 夜间整理每小时检查一次。

    放在后台线程里而不是 lifespan 里同步跑：两者都可能发外部请求，
    不能让服务启动等它们（没配 AI 时它们自己会立刻返回）。
    """
    if not _workers_enabled():
        logger.info("INKNOTE_WORKERS=0，后台索引 / 备份线程已关闭")
        return
    _spawn_worker("inknote-index", 1800, _tick_index, run_immediately=True)
    _spawn_worker("inknote-backup", 3600, _tick_backup, run_immediately=False)
    _spawn_worker("inknote-tidy", 3600, _tick_tidy, run_immediately=False)


def _safe_print(text: str) -> None:
    """在 Windows 的 GBK 控制台上也能安全输出（否则启动横幅一崩，服务就起不来）。

    先尝试把 stdout 切到 UTF-8；真切不了（比如被重定向到不支持的对象）时，
    再用当前编码加 errors='replace' 兜一次，绝不抛异常。
    """
    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        print(text, flush=True)
        return
    except UnicodeEncodeError:
        pass
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        print(text.encode(encoding, "replace").decode(encoding, "replace"), flush=True)
    except Exception:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def _print_banner() -> None:
    from . import search as search_mod

    lines = [
        "",
        "  " + "─" * 52,
        f"  {settings.site_title} 已启动",
        f"  笔记后台：http://{settings.host}:{settings.port}/notes",
        f"  公开博客：http://{settings.host}:{settings.port}/blog",
        f"  数据目录：{settings.data_dir}",
    ]
    if search_mod.FTS_ENABLED and search_mod.FTS_TOKENIZER == "trigram":
        lines.append("  全文搜索：FTS5（trigram 分词，中文子串可命中）")
    elif search_mod.FTS_ENABLED:
        lines.append(
            f"  全文搜索：FTS5（{search_mod.FTS_TOKENIZER} 分词不适合中文，查询实际走 LIKE 兜底）"
        )
    else:
        lines.append("  全文搜索：当前 SQLite 没有 FTS5，查询全部走 LIKE 兜底")
    if settings.is_default_password:
        # 这里刻意只用 GBK 也认识的字，避免控制台编码把启动搞崩
        lines.append("  [!] 正在使用默认密码 inknote，请在 .env 里改掉 INKNOTE_PASSWORD")
    if not ai.is_enabled():
        lines.append("  - AI 功能未启用（可在网站的「设置」页里配置，或写进 .env）")
    lines.append("  " + "─" * 52)
    _safe_print("\n".join(lines))


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.site_title} · InkNote",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        # 接口文档默认关闭（settings.docs_enabled，INKNOTE_DOCS=1 时开启）：
        # 私人笔记应用不该向匿名访客暴露 API 结构，见使用说明「安全」一节。
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 中间件 ----------------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        """① 跨站写请求拦截（Origin/Referer 校验）② 生成 CSP nonce ③ 安全响应头"""
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            host = request.headers.get("host") or ""
            allowed = True
            if origin:
                allowed = urlparse(origin).netloc == host
            elif referer:
                allowed = urlparse(referer).netloc == host
            if not allowed:
                return PlainTextResponse("跨站请求已被拒绝", status_code=403)

        nonce = secrets.token_urlsafe(16)
        request.state.nonce = nonce
        response: Response = await call_next(request)

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        csp = (
            "default-src 'self'; "
            "img-src 'self' data: https:; "
            "style-src 'self' 'unsafe-inline'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "form-action 'self'; "
            "base-uri 'self'; "
            "frame-ancestors 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", csp)
        return response

    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

    # ---------------- 静态资源 ----------------
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.mount("/media", StaticFiles(directory=str(settings.upload_dir)), name="media")

    # ---------------- 异常处理 ----------------
    @app.exception_handler(RedirectException)
    async def _redirect_handler(request: Request, exc: RedirectException):
        del request
        return RedirectResponse(exc.url, status_code=exc.status_code)

    def _wants_json(request: Request) -> bool:
        path = request.url.path
        if path.startswith("/api/"):
            return True
        accept = request.headers.get("accept") or ""
        return "application/json" in accept and "text/html" not in accept

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "error": str(exc.detail), "status": exc.status_code},
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None),
            )
        return render(
            request,
            "errors/error.html",
            status_code=exc.status_code,
            code=exc.status_code,
            message=str(exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "error": "请求参数不合法", "detail": exc.errors()}, status_code=422
            )
        return render(
            request,
            "errors/error.html",
            status_code=422,
            code=422,
            message="请求参数不合法，请检查表单内容。",
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("未处理的异常：%s %s", request.method, request.url.path)
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": "服务器内部错误"}, status_code=500)
        return render(
            request,
            "errors/error.html",
            status_code=500,
            code=500,
            message="服务器内部错误，请查看终端日志。",
        )

    # ---------------- 路由 ----------------
    app.include_router(auth.router)
    app.include_router(notes.router)
    app.include_router(pages.router)
    app.include_router(ask.router)
    app.include_router(ai_admin.settings_router)
    app.include_router(ai_admin.ai_api_router)
    app.include_router(ai_embed.router)
    app.include_router(ai_inline.router)
    app.include_router(backup.router)
    app.include_router(media.router)
    app.include_router(api.router)
    app.include_router(meta.router)
    app.include_router(blog.router)

    @app.get("/", include_in_schema=False)
    def home(
        request: Request,
        conn=Depends(db_conn),
        q: str = "",
        tag: str = "",
        month: str = "",
        page: int = 1,
    ):
        """登录后进笔记列表，未登录（访客）看到博客首页。"""
        if current_session(request):
            return RedirectResponse("/notes", status_code=303)
        return blog.blog_index(request, conn, q=q, tag=tag, month=month, page=page)

    return app


app = create_app()
