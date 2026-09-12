"""pytest 公共装置：每个测试会话用一个临时数据目录，绝不碰真实笔记。"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在导入 app 之前设好环境变量（config 在导入时读取）
_DATA_DIR = Path(tempfile.mkdtemp(prefix="inknote-test-"))
os.environ["INKNOTE_DATA_DIR"] = str(_DATA_DIR)
os.environ["INKNOTE_PASSWORD"] = "test-password"
os.environ["INKNOTE_SECRET"] = "unit-test-secret-key-0123456789abcdef"
os.environ["INKNOTE_BASE_URL"] = "http://testserver"
os.environ["INKNOTE_TITLE"] = "测试站"
os.environ.pop("INKNOTE_PASSWORD_HASH", None)
os.environ["INKNOTE_AI_BASE_URL"] = ""
os.environ["INKNOTE_AI_MODEL"] = ""
# 关掉后台索引 / 备份线程：测试里会反复触发 lifespan，让线程跑起来会让
# 「索引状态」「自动备份」这类断言变成不确定的（想测线程本身的用例自己开回来）。
os.environ["INKNOTE_WORKERS"] = "0"
atexit.register(lambda: shutil.rmtree(_DATA_DIR, ignore_errors=True))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASSWORD = "test-password"


@pytest.fixture(scope="session")
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def csrf_of(client: TestClient, path: str = "/notes") -> str:
    """从页面里取出 CSRF token。"""
    response = client.get(path)
    match = re.search(r'name="csrf-token" content="([^"]*)"', response.text)
    if not match:
        raise AssertionError(f"{path} 页面里没有 csrf-token meta 标签")
    return match.group(1)


def login(client: TestClient) -> TestClient:
    """在传入的客户端上登录一次，返回同一个客户端（幂等：重复调用只刷新 cookie）。"""
    response = client.post(
        "/login", data={"password": PASSWORD, "next": "/notes"}, follow_redirects=False
    )
    assert response.status_code in (200, 303), response.text
    return client


@pytest.fixture()
def auth_client(client: TestClient) -> TestClient:
    """已登录的客户端（函数作用域）。

    以前是 session 作用域：别的用例改了密码 / 新建 TestClient 之后共享会话就可能失效，
    后面的用例全被踢到登录页 —— 表现为「单独跑全绿、全量跑全红」，非常难查
    （标签管理 / 标签输入两组用例就栽在这）。现在每个用例用之前重新登录一次，
    client 本身仍是共享的，所以同一个用例里的多次请求照旧共用 cookie。
    """
    return login(client)


@pytest.fixture()
def csrf(auth_client: TestClient) -> str:
    """当前会话的 CSRF token（与 auth_client 同为函数作用域，保证匹配）。"""
    return csrf_of(auth_client)
