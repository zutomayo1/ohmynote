"""样式表结构守卫：不许出现「选择器后面少了 { }」的半截规则。

血泪教训（2026-09-18）：f07d36d「历史任务展开页面不再晃 + 管理标签下拉不再被裁」
从几处选择器列表里删类名时，把花括号一起删了，留下这样一段：

    .agent-runs, .agent-history, .editor__templates,
     /* tag-admin 不参与：... */

    .agent-runs::details-content,
    .agent-history::details-content, .editor__templates::details-content
      display: block; block-size: 0; overflow-y: clip;
      transition: block-size .26s ...;
    }

CSS 解析器碰到没闭合的 prelude 会一路吞到下一个 `{`，于是整段规则被**静默丢弃**：
「笔记助手」页的展开动画（agent-runs / agent-history）和紧跟其后的月亮 hover 动画
一起失效，浏览器控制台一句话都不说，代码里也很难看出来（`grep` 甚至还能 grep 到
`.agent-runs::details-content` 字样，只是它不在 cssRules 里）。

这里用一个极简的 CSS 结构扫描把它钉住：不校验声明内容，只保证
「选择器/at-rule 后面一定有 `{ }`、括号一定配对」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

CSS_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "css"

# 本项目的样式表只用容器型 at-rule（里面装的是规则，不是声明）：
# @media / @supports / @keyframes。若将来引入 @font-face / @page 这类
# 「里面是声明」的 at-rule，需要把它的名字加进这里。
DECL_AT_RULES: frozenset[str] = frozenset({"font-face", "page", "property", "counter-style", "viewport"})

# 参与「展开/折叠高度过渡」的块：这些选择器必须真的以规则形式存在，
# 否则动画会静默失效（括号写坏了、类名被删了都算）。加新折叠块时往这里补一行。
FOLD_HOOKS: tuple[str, ...] = (
    "agent-runs",
    "agent-history",
    "editor__templates",
    "ask-recent-block",
    "template-list-fold",
    "graph-params",
)


def scan_structure(src: str) -> list[str]:
    """返回结构问题的描述；空列表 = 括号与规则边界都正常。"""
    problems: list[str] = []
    stack: list[str] = []  # 'at'（容器 at-rule）/ 'decl'（规则体）
    pending = ""  # 只在「顶层 / 容器 at-rule 里」累积选择器文本
    pending_line = 1
    i, line, n = 0, 1, len(src)

    def at_boundary() -> bool:
        return not stack or stack[-1] == "at"

    while i < n:
        ch = src[i]
        if ch == "\n":
            line += 1
            if at_boundary():
                pending += ch
            i += 1
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end == -1:
                problems.append(f"第 {line} 行：注释 /* 没有闭合")
                break
            line += src.count("\n", i, end)
            i = end + 2
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and src[j] != ch:
                if src[j] == "\\":
                    j += 1
                if j < n and src[j] == "\n":
                    line += 1
                j += 1
            if j >= n:
                problems.append(f"第 {line} 行：字符串没有闭合")
                break
            i = j + 1
            continue
        if ch == "{":
            if at_boundary():
                head = pending.strip()
                if not head:
                    problems.append(f"第 {line} 行：`{{` 前面没有选择器")
                name = head.lstrip("@").split(" ", 1)[0].split("(", 1)[0].lower()
                stack.append("decl" if head.lstrip().startswith("@") and name in DECL_AT_RULES else
                             ("at" if head.startswith("@") else "decl"))
            else:
                stack.append("decl")
            pending = ""
            i += 1
            continue
        if ch == "}":
            if not stack:
                problems.append(f"第 {line} 行：多出来的 `}}`")
            else:
                kind = stack.pop()
                if kind == "at" and pending.strip():
                    problems.append(
                        f"第 {pending_line} 行：这段选择器后面缺 `{{ }}`（直接遇到了 `}}`）——"
                        "CSS 会静默丢弃整段规则"
                    )
            pending = ""
            i += 1
            continue
        if ch == ";":
            if at_boundary() and pending.strip() and not pending.lstrip().startswith("@"):
                problems.append(
                    f"第 {pending_line} 行：选择器后面缺 `{{`（`;` 落在了规则外）——"
                    "CSS 会静默丢弃整段规则"
                )
            pending = ""
            i += 1
            continue
        if at_boundary():
            if not pending.strip():
                pending_line = line
            pending += ch
        i += 1

    if stack:
        problems.append("文件末尾还有 `{` 没闭合")
    if pending.strip():
        problems.append(f"第 {pending_line} 行：文件末尾有一段没有 `{{ }}` 的选择器")
    return problems


STYLE_SHEETS = sorted(CSS_DIR.glob("*.css"))


@pytest.mark.parametrize("path", STYLE_SHEETS, ids=[p.name for p in STYLE_SHEETS])
def test_css_rules_are_well_formed(path: Path):
    """每张样式表都不许有半截规则（选择器后面少 `{ }`）。"""
    problems = scan_structure(path.read_text(encoding="utf-8"))
    assert not problems, f"{path.name} 结构有问题：\n" + "\n".join(f"  · {p}" for p in problems)


def test_fold_animation_hooks_present():
    """折叠动画的挂钩必须在：每个折叠块的 `::details-content` 过渡与 `[open]` 收尾都写成规则。"""
    css = (CSS_DIR / "style.css").read_text(encoding="utf-8")
    missing = []
    for cls in FOLD_HOOKS:
        for needle in (f".{cls}::details-content", f".{cls}[open]::details-content"):
            if needle not in css:
                missing.append(needle)
    assert not missing, "折叠动画的挂钩丢了：\n" + "\n".join(f"  · {m}" for m in missing)
    assert css.count("interpolate-size: allow-keywords") >= 3, (
        "interpolate-size: allow-keywords 掉了——block-size 到 auto 就没法过渡，动画会变成瞬开"
    )
    assert "block-size: auto" in css


def test_scan_structure_catches_broken_rule():
    """扫描器自检：拿 2026-09-18 那次写坏的样子，必须报出来。"""
    broken = (
        ".agent-runs, .agent-history, .editor__templates,\n"
        " /* tag-admin 不参与：块内有选择标签的下拉弹层，clip 会裁掉 */\n"
        "\n"
        ".agent-runs::details-content,\n"
        ".agent-history::details-content, .editor__templates::details-content\n"
        "  display: block; block-size: 0; overflow-y: clip;\n"
        "  transition: block-size .26s ease;\n"
        "}\n"
        "\n"
        ".theme-toggle:hover svg { animation: spin .5s; }\n"
    )
    assert scan_structure(broken), "写坏的规则没被识别出来"
    # 正确写法（同一段）应当干净
    fixed = (
        ".agent-runs, .agent-history, .editor__templates { interpolate-size: allow-keywords; }\n"
        "\n"
        ".agent-runs::details-content,\n"
        ".agent-history::details-content, .editor__templates::details-content {\n"
        "  display: block; block-size: 0; overflow-y: clip;\n"
        "  transition: block-size .26s ease;\n"
        "}\n"
        "\n"
        ".agent-runs[open]::details-content { block-size: auto; }\n"
        "\n"
        "@media (prefers-reduced-motion: reduce) {\n"
        "  .agent-runs::details-content { transition: none; }\n"
        "}\n"
        "\n"
        "@keyframes spin { to { transform: rotate(360deg); } }\n"
    )
    assert scan_structure(fixed) == []
