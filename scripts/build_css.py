#!/usr/bin/env python
"""把 app/static/css/src/*.css 按文件名顺序拼成 app/static/css/style.css。

    python scripts/build_css.py            # 生成 style.css
    python scripts/build_css.py --check    # 只校验「产物是不是最新的」（CI 与 scripts/check.py 会跑）

为什么不改成多个 <link>：单文件交付意味着首屏只有 1 个 CSS 请求、缓存键只有 1 个
（asset_v 是所有静态文件 mtime 的 md5）。仓库里本来就有同类做法——highlight.css
也是脚本（scripts/build_highlight_css.py）生成的。想要多文件交付时，改 base.html
加几个 <link> 即可，源文件这一层不用动。

**拼接顺序 = 层叠顺序**：源文件按文件名排序拼接，顺序和拆分前的老 style.css
完全一致；除了每个源文件前面注入的一段「这一节来自哪个文件」的横幅注释，
产出的样式声明逐字节不变。所以这个重构对外观是零影响（可用
`python -c "from scripts.build_css import strip_banners"` 自行核对）。

改样式请改 app/static/css/src/ 下的文件；改了 src 没重新生成时，
tests/test_css_syntax.py 与 scripts/check.py 都会红。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "app/static/css/src"
TARGET = ROOT / "app/static/css/style.css"

# 生成物头部的告警（strip_banners 靠这个前缀识别并整段剥掉）
PREAMBLE_MARK = "/*! ⚠️ 生成物"
PREAMBLE = """\
/*! ⚠️ 生成物 —— 由 scripts/build_css.py 从 app/static/css/src/*.css 拼成，别直接改这个文件。
 *
 *  改样式：编辑 src/ 下对应主题的文件（文件名前缀就是拼接顺序），然后
 *          python scripts/build_css.py
 *  校验：  python scripts/build_css.py --check   （scripts/check.py 与 CI 都会跑）
 *  顺序即层叠：后面的文件可以覆盖前面同优先级的规则。新增样式写进对应主题文件
 *  （或末尾的补丁文件），别插在中间——那会改变层叠结果。
 *  每个分段前的那段横幅注释也是本脚本注入的，删掉不影响样式。
 */
"""

BANNER_MARK = "/* ───── src/"
BANNER_RULE = "─" * 62


def order() -> list[str]:
    """源文件顺序 = 文件名排序（前缀 00-/10-/…/76- 即是拼接顺序）。"""
    return [p.name for p in sorted(SRC_DIR.glob("*.css"))]


def _topics(part: str) -> list[str]:
    """列出这一段里的顶层分区标题，用于横幅里的「本段装什么」。"""
    out = []
    for line in part.split("\n"):
        s = line.strip()
        if s.startswith("/* ===== ") and s.endswith(" ===== */"):
            out.append(s[len("/* ===== "):-len(" ===== */")])
    return out


def _banner(name: str, part: str) -> str:
    lines = [f"{BANNER_MARK}{name} {BANNER_RULE}"]
    topics = _topics(part)
    if topics:
        # 每行最多塞 3 个标题，别把横幅摊得太长
        row: list[str] = []
        for topic in topics:
            row.append(topic)
            if len(row) == 3:
                lines.append("   " + " · ".join(row))
                row = []
        if row:
            lines.append("   " + " · ".join(row))
    else:
        lines.append("   （本段没有顶层分区头：是文件开头的文档注释与总横幅）")
    lines.append("   " + "─" * 61 + " */")
    return "\n".join(lines) + "\n\n"


def build() -> str:
    """拼出 style.css 的完整内容（含注入的横幅）。"""
    chunks = [PREAMBLE, "\n"]
    for name in order():
        part = (SRC_DIR / name).read_text(encoding="utf-8")
        chunks.append(_banner(name, part))
        chunks.append(part)
    return "".join(chunks)


def strip_banners(text: str) -> str:
    """剥掉注入的横幅与头部告警 —— 剩下的应当等于 src/*.css 的简单拼接。

    横幅/告警各占「一整段注释 + 紧随的一个空行」，所以跳完注释还要吃掉那个空行，
    否则每剥一段都会多留一个空行（这条是 tests/test_css_syntax.py 的等价性断言）。
    """
    out: list[str] = []
    skipping = False
    eat_blank = False
    for line in text.split("\n"):
        if skipping:
            if line.rstrip().endswith("*/"):
                skipping = False
                eat_blank = True
            continue
        if eat_blank:
            eat_blank = False
            if not line.strip():
                continue
        if line.startswith(PREAMBLE_MARK) or line.startswith(BANNER_MARK):
            skipping = True
            continue
        out.append(line)
    return "\n".join(out)


def _read_target() -> str:
    return TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in args
    names = order()
    if not names:
        print(f"[!!] {SRC_DIR} 里没有 .css 源文件")
        return 1

    built = build()
    current = _read_target()

    if check:
        if built == current:
            print(f"[OK] style.css 与 src/ 一致（{len(names)} 个源文件，{built.count(chr(10)) + 1} 行）")
            return 0
        print("[!!] style.css 不是最新的 —— 跑 python scripts/build_css.py 重新生成")
        print(f"     src/ 源文件：{'、'.join(names)}")
        return 1

    if built == current:
        print(f"[OK] 已是最新，无需写入（{len(names)} 个源文件，{current.count(chr(10)) + 1} 行）")
        return 0

    TARGET.write_text(built, encoding="utf-8")
    print(f"[OK] 已生成 {TARGET.relative_to(ROOT)}：{len(names)} 个源文件 → {built.count(chr(10)) + 1} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
