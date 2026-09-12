#!/usr/bin/env python
"""启动脚本。

    python run.py                       # 启动（默认 http://127.0.0.1:8000）
    python run.py --port 9000 --open    # 换端口并自动打开浏览器
    python run.py --reload              # 开发模式，改代码自动重启
    python run.py --hash-password 密码   # 生成可用在 .env 里的密码哈希
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _wire_banner_logging() -> None:
    """把启动横幅也写进日志文件。

    main.py 不归本任务改，它的 ``_print_banner()`` 走 ``print``；这里在运行时
    包一层 ``_safe_print``，保留原控制台输出，同时把每行以 file_only 记录写进日志。
    """
    from app import main as main_module

    original_safe_print = main_module._safe_print

    def _safe_print(text: str) -> None:
        original_safe_print(text)
        logger = logging.getLogger("inknote")
        for line in str(text).splitlines():
            if line.strip():
                logger.info("%s", line, extra={"inknote_file_only": True})

    main_module._safe_print = _safe_print


def main() -> int:
    from app.config import settings

    parser = argparse.ArgumentParser(description="墨痕 InkNote · 个人笔记与博客")
    parser.add_argument("--host", default=settings.host, help="监听地址")
    parser.add_argument("--port", type=int, default=settings.port, help="端口")
    parser.add_argument("--reload", action="store_true", help="开发模式：文件变化自动重启")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--hash-password", metavar="PASSWORD", help="生成 pbkdf2 密码哈希后退出")
    args = parser.parse_args()

    if args.hash_password:
        from app.security import hash_password

        print(hash_password(args.hash_password))
        print("\n把上面这行填进 .env 的 INKNOTE_PASSWORD_HASH，然后删掉 INKNOTE_PASSWORD。")
        return 0

    try:
        import uvicorn
    except ImportError:
        print("缺少依赖，请先执行：pip install -r requirements.txt", file=sys.stderr)
        return 1

    # 让启动横幅显示的是真实的监听地址，而不是 .env 里的默认值
    settings.host = args.host
    settings.port = args.port

    # 启动日志落盘（写不了也不抛异常），并把 main.py 的启动横幅桥接进日志
    from app import logging_setup

    log_path = logging_setup.setup_logging()
    logging.getLogger("inknote").info("日志文件：%s", log_path)
    _wire_banner_logging()

    if args.open:
        url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    os.chdir(ROOT)
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="debug" if settings.debug else "info",
        log_config=None,  # 不要用 uvicorn 自己的 handler 覆盖掉落盘 handler
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
