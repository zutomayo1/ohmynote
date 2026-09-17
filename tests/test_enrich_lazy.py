"""公式 / mermaid 的加载策略守卫。

背景与结论（慢链路 2 Mbps / 100 ms RTT 实测，见提交信息）：

  | 配置                  | DCL（页面 JS 初始化） | 图表出现 | mermaid |
  | 改前：<script src> 进关键路径 | 4749ms          | 4973ms   | 823KB   |
  | 改后：load 之后按需拉取       | 280ms           | 4310ms   | 823KB   |

`defer` 脚本按文档顺序执行，而 mermaid 压缩后还有 823KB，于是后面所有 defer
脚本（app.js / blog.js / 阅读进度…）都要等它下完，DOMContentLoaded 被推到
4.7 秒 —— 主题、色板、分类树这些交互全跟着哑掉。改成按需加载后两全其美：
DCL 快 17 倍，图反而更早出现（不再和关键资源抢带宽）。

KaTeX 保持 eager：公式就是正文文字，晚渲染会让读者先看到 $$…$$ 源码。

所以这里守三件事：
1. 有 mermaid 的页面**不得**再出现 eager 的 mermaid <script src>；
2. 地址仍要交给页面（enrich.js 靠 window.__inknoteMermaidSrc 拉取）；
3. 公式页面的 KaTeX 仍是 eager，别被顺手一起懒加载了。
"""

from __future__ import annotations

from app import repo
from tests.test_agent import db_conn  # noqa: F401  fixture 随模块导入

PREFIX = "enrich守卫-"
MERMAID_BODY = "```mermaid\ngraph TD\n  A --> B\n```\n\n正文。\n"
MATH_BODY = "行内 $E=mc^2$ 与独立公式：\n\n$$\n\\int_0^1 x dx\n$$\n"


def _public_note(db_conn, title: str, content: str) -> dict:
    note = repo.create_note(db_conn, title=title, content=content, is_public=True, status="saved")
    db_conn.commit()
    return note


def test_mermaid_is_not_loaded_on_critical_path(client, db_conn):
    note = _public_note(db_conn, PREFIX + "图", MERMAID_BODY)
    html = client.get(f"/blog/{note['slug']}").text

    assert '<script src="/static/vendor/mermaid/mermaid.min.js' not in html, \
        "mermaid 又回到关键路径了：823KB 会把 DCL 推后数秒（实测 280ms → 4749ms）"
    assert "window.__inknoteMermaidSrc" in html, "地址要交给 enrich.js 才能按需拉"
    assert "/static/vendor/mermaid/mermaid.min.js?v=" in html
    assert "enrich.js" in html


def test_math_still_loads_eagerly(client, db_conn):
    note = _public_note(db_conn, PREFIX + "公式", MATH_BODY)
    html = client.get(f"/blog/{note['slug']}").text

    assert '<script src="/static/vendor/katex/katex.min.js' in html, \
        "KaTeX 必须保持 eager：晚渲染会让读者先看到 $$…$$ 源码"
    assert '<link rel="stylesheet" href="/static/vendor/katex/katex.min.css' in html


def test_plain_note_loads_neither(client, db_conn):
    note = _public_note(db_conn, PREFIX + "普通", "只有正文。\n" * 5)
    html = client.get(f"/blog/{note['slug']}").text

    assert "mermaid" not in html
    assert "katex" not in html
    assert "enrich.js" not in html, "既没公式也没图，就不该引 enrich.js"
