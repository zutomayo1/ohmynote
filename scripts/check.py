#!/usr/bin/env python
"""墨痕 InkNote · 一键自检

改完代码跑这一条就够了：

    .venv\\Scripts\\python.exe scripts\\check.py            # 全部检查
    .venv\\Scripts\\python.exe scripts\\check.py --quick    # 跳过慢的（只跑快速用例）
    .venv\\Scripts\\python.exe scripts\\check.py --port 8000  # 顺手探一下已经跑起来的服务

依次检查：语法 → 样式审计 → 测试 → 应用冒烟（临时数据目录，不碰你的笔记）→ 数据库完整性 → 环境摘要。
任何一步失败都不中断，最后统一报告，退出码 0/1 反映成败。
"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，输出宽字符会 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 老环境没有 reconfigure
    pass

OK = "[OK]"
BAD = "[!!]"
SKIP = "[--]"

QUICK_TESTS = ["tests/test_utils.py", "tests/test_markdown.py", "tests/test_forms_csrf.py"]


class Result:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = True
        self.skipped = False
        self.detail = ""
        self.seconds = 0.0


def _run(cmd: list[str], *, timeout: int = 900) -> tuple[int, str]:
    process = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return process.returncode, (process.stdout or "") + (process.stderr or "")


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------
def check_syntax() -> Result:
    result = Result("Python 语法")
    bad: list[str] = []
    count = 0
    for root in ("app", "tests", "scripts"):
        for path in sorted((ROOT / root).rglob("*.py")):
            count += 1
            try:
                source = path.read_text(encoding="utf-8-sig")  # 兼容带 BOM 的文件
                ast.parse(source, str(path))
            except SyntaxError as exc:
                bad.append(f"{path.relative_to(ROOT)}:{exc.lineno} {exc.msg}")
            except OSError as exc:
                bad.append(f"{path.relative_to(ROOT)} 读不了: {exc}")
    result.ok = not bad
    result.detail = f"{count} 个文件" + ("，全部通过" if not bad else "；有问题：" + "; ".join(bad[:5]))
    return result


def check_css() -> Result:
    result = Result("样式审计")
    code, output = _run([sys.executable, "scripts/audit_css.py"], timeout=180)
    result.ok = code == 0
    # 只挑真正的结论行，别把 DeprecationWarning 当成结果
    useful = [
        line.strip()
        for line in output.splitlines()
        if line.strip()
        and not line.lstrip().startswith(("from ", "import ", "Traceback"))
        and "Warning" not in line
    ]
    result.detail = (useful[-1] if useful else "没有输出")[:180]
    return result


def check_tests(quick: bool) -> Result:
    result = Result("测试" + ("（快速）" if quick else ""))
    targets = QUICK_TESTS if quick else ["tests"]
    code, output = _run([sys.executable, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider"], timeout=1800)
    result.ok = code == 0
    summary = [line for line in output.splitlines() if "passed" in line or "failed" in line or "error" in line.lower()]
    failures = [line for line in output.splitlines() if line.startswith(("FAILED", "ERROR"))]
    result.detail = (summary[-1] if summary else "没有摘要")[:120]
    if failures:
        result.detail += " | " + "; ".join(failures[:3])
    return result


def check_smoke() -> Result:
    """用临时数据目录起一次应用（TestClient，不占端口），把关键页面走一遍。"""
    result = Result("应用冒烟")
    data_dir = Path(tempfile.mkdtemp(prefix="inknote-check-"))
    old_env = {key: os.environ.get(key) for key in
               ("INKNOTE_DATA_DIR", "INKNOTE_PASSWORD", "INKNOTE_SECRET", "INKNOTE_TITLE", "INKNOTE_BASE_URL")}
    # 必须在导入 app 之前设好环境（config 是导入时读的）
    os.environ.update(
        {
            "INKNOTE_DATA_DIR": str(data_dir),
            "INKNOTE_PASSWORD": "check-script-password",
            "INKNOTE_SECRET": "check-script-secret-0123456789abcdef",
            "INKNOTE_TITLE": "自检站",
            "INKNOTE_BASE_URL": "http://testserver",
        }
    )
    os.environ.pop("INKNOTE_PASSWORD_HASH", None)
    import contextlib
    import io as _io

    try:
        from fastapi.testclient import TestClient

        from app.main import app

        problems: list[str] = []
        # 应用启动时会打印自己的横幅，自检输出里不需要它
        with contextlib.redirect_stdout(_io.StringIO()), TestClient(app) as client:
            health = client.get("/health")
            if health.status_code != 200 or not health.json().get("ok"):
                problems.append(f"/health -> {health.status_code}")
            for path in ("/login", "/blog"):
                code = client.get(path).status_code
                if code != 200:
                    problems.append(f"{path} -> {code}")
            notes_anon = client.get("/notes", follow_redirects=False)
            if notes_anon.status_code != 303:
                problems.append(f"未登录 /notes 应 303，实际 {notes_anon.status_code}")
            client.post("/login", data={"password": "check-script-password", "next": "/notes"}, follow_redirects=False)
            for path in ("/notes", "/settings", "/backup", "/images", "/trash", "/tags"):
                code = client.get(path).status_code
                if code != 200:
                    problems.append(f"{path} -> {code}")
        result.ok = not problems
        result.detail = "关键页面全部正常" if not problems else "; ".join(problems[:5])
    except Exception as exc:  # noqa: BLE001 - 自检脚本本身不能因为一个模块坏了就整体崩
        result.ok = False
        result.detail = f"{type(exc).__name__}: {exc}"[:180]
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(data_dir, ignore_errors=True)
    return result


def check_database() -> Result:
    result = Result("数据库完整性")
    db_path = ROOT / "data" / "inknote.db"
    if not db_path.is_file():
        result.ok = True
        result.detail = "跳过（还没有 data/inknote.db）"
        return result
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchone()[0]
            notes = 0
            try:
                notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            except sqlite3.Error:
                pass
            ok_integrity = bool(integrity) and str(integrity[0]).lower() == "ok"
            result.ok = ok_integrity
            result.detail = (
                f"integrity_check={integrity[0] if integrity else '?'} · {tables} 张表 · {notes} 篇笔记"
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result.ok = False
        result.detail = f"打不开或损坏: {exc}"
    return result


def check_environment() -> Result:
    result = Result("环境摘要")
    try:
        import sqlite3 as _sqlite

        from app.config import settings

        def dep_version(name: str) -> str:
            try:
                from importlib.metadata import version

                return version(name)
            except Exception:  # noqa: BLE001
                return "?"

        conn = _sqlite.connect(":memory:")
        try:
            fts5 = False
            trigram = False
            try:
                conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
                fts5 = trigram = True
            except _sqlite.Error:
                try:
                    conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
                    fts5 = True
                except _sqlite.Error:
                    pass
        finally:
            conn.close()

        data_size = 0
        if settings.data_dir.exists():
            for path in settings.data_dir.rglob("*"):
                if path.is_file():
                    data_size += path.stat().st_size

        result.detail = (
            f"Python {sys.version.split()[0]} · SQLite {_sqlite.sqlite_version}"
            f" · FTS5={'有' if fts5 else '无'}/trigram={'有' if trigram else '无'}"
            f" · fastapi={dep_version('fastapi')} starlette={dep_version('starlette')}"
            f" jinja2={dep_version('jinja2')} markdown={dep_version('markdown')}"
            f" · data/ {data_size / 1024 / 1024:.1f} MB"
        )
    except Exception as exc:  # noqa: BLE001
        result.ok = False
        result.detail = f"{type(exc).__name__}: {exc}"[:180]
    return result


def check_live_server(port: int) -> Result:
    result = Result(f"在线服务 :{port}")
    try:
        import httpx

        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
        payload = response.json()
        result.ok = response.status_code == 200 and bool(payload.get("ok"))
        result.detail = f"/health -> {response.status_code} · 站点 {payload.get('site')} · {payload.get('notes')} 篇"
    except Exception as exc:  # noqa: BLE001 - 没起服务是正常情况
        result.ok = True
        result.skipped = True
        result.detail = f"跳过（{type(exc).__name__}，服务可能没在跑）"
    return result


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="墨痕 InkNote 一键自检")
    parser.add_argument("--quick", action="store_true", help="跳过慢的：只跑快速测试")
    parser.add_argument("--port", type=int, default=0, help="顺便探测这个端口上已启动的服务")
    args = parser.parse_args()

    steps: list[tuple[str, object]] = [
        ("语法", lambda: check_syntax()),
        ("样式", lambda: check_css()),
        ("测试", lambda: check_tests(args.quick)),
        ("冒烟", lambda: check_smoke()),
        ("数据库", lambda: check_database()),
        ("环境", lambda: check_environment()),
    ]
    if args.port:
        steps.append((f"在线 :{args.port}", lambda: check_live_server(args.port)))

    print("=" * 68)
    print(f"墨痕 InkNote 自检{'（快速模式）' if args.quick else ''}")
    print("=" * 68)

    results: list[Result] = []
    for label, fn in steps:
        started = time.perf_counter()
        print(f"→ {label} ... ", end="", flush=True)
        try:
            result: Result = fn()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - 每一步都不许把整体搞崩
            result = Result(label)
            result.ok = False
            result.detail = f"{type(exc).__name__}: {exc}"[:180]
        result.seconds = time.perf_counter() - started
        results.append(result)
        mark = SKIP if result.skipped else (OK if result.ok else BAD)
        print(f"{mark} ({result.seconds:.1f}s)")

    print("-" * 68)
    for result in results:
        mark = SKIP if result.skipped else (OK if result.ok else BAD)
        print(f"{mark} {result.name:<12} {result.seconds:>5.1f}s  {result.detail}")
    failed = [item for item in results if not item.ok]
    print("-" * 68)
    total = sum(item.seconds for item in results)
    if failed:
        print(f"结果：{BAD} {len(failed)} 项失败 / 共 {len(results)} 项，耗时 {total:.1f}s")
        for item in failed:
            print(f"      - {item.name}: {item.detail}")
    else:
        print(f"结果：{OK} 全部通过（{len(results)} 项，耗时 {total:.1f}s）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
