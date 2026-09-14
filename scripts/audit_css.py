"""交叉审计：模板里用到的 class 是否都在 style.css 里定义了。

这是一个「契约检查」脚本——改模板或改 CSS 之后跑一下，防止样式漏配。

    .venv\\Scripts\\python.exe scripts/audit_css.py
    # 需要交互式查看结果时加 --verbose
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("INKNOTE_DATA_DIR", tempfile.mkdtemp(prefix="inknote-audit-"))
os.environ.setdefault("INKNOTE_SECRET", "audit-secret-key-0123456789")
os.environ.setdefault("INKNOTE_PASSWORD", "audit-password")

# JS 生成的、不在 HTML 源码里的状态类
JS_STATE_CLASSES = {
    "is-active", "is-open", "is-out", "is-dragover", "is-error", "is-long", "is-xlong",
    "is-hidden", "is-saving", "is-saved", "is-dirty", "is-on", "is-empty", "is-selected",
    "code-copy", "drag-over",
}
# 图标类由 _macros.html 的 icon() 生成，样式在 .icon 上
ICON_PREFIX = "icon--"

PAGES = [
    "/notes", "/notes/1", "/notes/2", "/notes/1/edit", "/notes/1/versions", "/notes/1/versions/1",
    "/notes/new", "/tags", "/tags?tag=审计", "/search?q=审计", "/templates", "/ask", "/stats",
    "/trash", "/blog", "/blog/archive", "/blog/tags", "/about", "/login", "/notes/9999",
    "/settings", "/todos",
]


def collect_classes(html: str) -> set[str]:
    found: set[str] = set()
    for chunk in re.findall(r'class="([^"]*)"', html):
        found |= {token for token in chunk.split() if token}
    return found


def main() -> int:
    # Windows 控制台默认是 GBK，先把输出切成 UTF-8，避免中文/符号触发 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    verbose = "--verbose" in sys.argv
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        client.post("/login", data={"password": "audit-password", "next": "/notes"}, follow_redirects=False)
        page = client.get("/notes")
        match = re.search(r'name="csrf-token" content="([^"]*)"', page.text)
        token = match.group(1) if match else ""
        seed = [
            {"title": "审计用笔记", "content": "## 标题\n\n- [x] 完成\n- [ ] 待办\n\n```python\nprint(1)\n```\n\n"
             "| a | b |\n| - | - |\n| 1 | 2 |\n\n[[不存在的笔记]]\n\n> 引用\n\n![图](/static/favicon.svg)\n",
             "tags": "审计", "action": "view"},
            {"title": "第二篇", "content": "正文 [[审计用笔记]]", "action": "save", "is_public": "1"},
        ]
        for payload in seed:
            client.post("/notes", data={"_csrf": token, **payload}, follow_redirects=False)
        client.post("/notes/1/flag", data={"_csrf": token, "flag": "public", "value": "1", "next": "/notes/1"})
        client.post("/templates/save", data={"_csrf": token, "name": "审计模板", "content": "c"}, follow_redirects=False)

        classes: set[str] = set()
        for path in PAGES:
            response = client.get(path)
            if "html" in response.headers.get("content-type", ""):
                classes |= collect_classes(response.text)

    css = (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
    defined |= set(re.findall(r"\.([a-zA-Z][\w-]*)", (ROOT / "app/static/css/highlight.css").read_text(encoding="utf-8")))

    missing = sorted(
        name
        for name in classes
        if name not in defined
        and name not in JS_STATE_CLASSES
        and not name.startswith(ICON_PREFIX)
        # Pygments 的 token 类（.k / .nf / .s2 …）
        and not re.fullmatch(r"[a-z]{1,3}\d?", name)
    )

    print(f"页面里出现 {len(classes)} 个 class，style.css / highlight.css 定义了 {len(defined)} 个")
    if missing:
        print(f"未覆盖 {len(missing)} 个：")
        for name in missing:
            print("   -", name)
        return 1
    print("OK：所有模板 class 都有对应样式")
    if verbose:
        print("已覆盖：", " ".join(sorted(classes)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
