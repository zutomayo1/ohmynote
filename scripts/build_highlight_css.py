"""生成代码高亮用的 CSS（Pygments token 配色）。

    python scripts/build_highlight_css.py

产物：app/static/css/highlight.css
亮色用 friendly（暖调），暗色用 one-dark。

**只导出 token 颜色**：容器背景 / 圆角 / 内边距由 style.css 的 .codehilite 负责。
Pygments 自带输出的 `#f0f0f0` 冷灰底和 `#282C34` 冷蓝黑底会破坏暖调纸感，这里主动剔除。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pygments.formatters import HtmlFormatter  # noqa: E402

LIGHT_STYLE = "friendly"
DARK_STYLE = "one-dark"
OUTPUT = ROOT / "app" / "static" / "css" / "highlight.css"

HEADER = """/* 代码高亮配色 —— 由 scripts/build_highlight_css.py 生成，请勿手工修改。
 * 亮色：Pygments {light}   暗色：Pygments {dark}
 * 这里只有 token 的颜色；容器（背景/边框/圆角/内边距）在 style.css 的 .codehilite 里，
 * 所以代码块永远跟着站点主题走，不会冒出冷灰色底。
 */

"""

# Pygments 会顺手输出这些全局/容器规则，与站点的暖调设计冲突，剔除
DROP_PATTERNS = (
    re.compile(r"^pre\s*\{"),
    re.compile(r"^\.codehilite\s*\{\s*background"),
    re.compile(r'^\[data-theme="dark"\]\s*\.codehilite\s*\{'),
    re.compile(r"^\.codehilite\s+\.hll\s*\{"),
    re.compile(r'^\[data-theme="dark"\]\s*\.codehilite\s+\.hll\s*\{'),
)

FOOTER = """
/* 行内代码与整行高亮：跟着主题走 */
.prose :not(pre) > code {
  background: var(--surface-2);
  color: var(--brand-dark);
  padding: .12em .38em;
  border-radius: 4px;
}
.codehilite .hll, .prose pre .hll { background: var(--brand-soft); display: block; }
"""


def _strip(css_text: str) -> str:
    kept = [line for line in css_text.splitlines() if not any(p.match(line) for p in DROP_PATTERNS)]
    return "\n".join(kept).strip("\n")


def build() -> str:
    light = _strip(HtmlFormatter(style=LIGHT_STYLE).get_style_defs(".codehilite"))
    dark = _strip(HtmlFormatter(style=DARK_STYLE).get_style_defs('[data-theme="dark"] .codehilite'))
    return (
        HEADER.format(light=LIGHT_STYLE, dark=DARK_STYLE)
        + light
        + "\n\n/* ---------- 深色模式 ---------- */\n"
        + dark
        + "\n"
        + FOOTER
    )


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    css = build()
    OUTPUT.write_text(css, encoding="utf-8")
    print(f"已写入 {OUTPUT}（{len(css.splitlines())} 行，花括号 {css.count('{')}/{css.count('}')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
