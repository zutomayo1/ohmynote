"""style.css 的规则级守卫：抓「肉眼看不出来、audit_css 也管不到」的样式问题。

`scripts/audit_css.py` 只管「模板里的 class 有没有样式」，管不了
**同一条规则被定义两次、后写的静默覆盖前面的** —— 导航下划线就栽在这上面：

  .nav-link.is-active::after { left: 12px; right: 12px; ... }   ← 旧规则，优先级 (0,2,1)
  .nav-link::after           { left: 50%; transform: translateX(-50%); }  ← 新规则，优先级更低
  .nav-link.is-active::after { width: 18px; }                    ← 新规则，只改宽度

结果 `left` 一直是 12px，又被新规则的 translateX(-50%) 往左拖走 9px，
选中的那个导航项下划线稳定地偏在文字左边。改配色时又加了一条 hover 规则，
于是每次改样式都能看见它，但谁也说不清"这算不算设计"。

所以这里守两条：
1) 伪元素选择器不许重复定义（同选择器的两段声明 = 迟早互相打架）；
2) 导航下划线的「居中契约」：靠 left:50% + translateX(-50%) 居中，
   不允许再出现给 .nav-link 系伪元素写死 left 的规则。
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "app/static/css/style.css").read_text(encoding="utf-8")

# 确实需要写两处的重复（每一条都要有正当理由，合并掉之后记得把条目删掉）
ALLOWED_DUPLICATES = {
    "*::before": "全局 reset 的 box-sizing 与「减弱动效」各一处，作用完全不同",
    "*::after": "同上",
    ".timeline__item:hover::before": "主规则是 hover 放大点亮；reduced-motion 媒体查询里 同选择器把 transform 归零——是对动效的关闭而非叠加，必须分处",
    ".nav-more__menu::before": "宽屏当 hover 桥接用、窄屏要把它 display:none 关掉",
    # 下面这条不是设计，是历史遗留：待办对勾被重画过一次，旧规则的
    # display/opacity 还在参与计算（新规则只覆盖了尺寸与 transform）。
    # 视觉上目前是对的，但同类「一半来自旧规则」正是导航下划线出问题的方式。
    # 要清理得先看一眼待办清单的对勾，所以先留在这里、只记录不改。
    ".task-list-indicator::after": "待核查：两段画的是不同的对勾，叠加后恰好正确",
    # 第二处是 prefers-reduced-motion: reduce 里的 animation: none ——
    # 故意的减弱动效覆盖（与 .select-combo__chevron 同一模式）
    ".ask-bubble.is-typing .ask-bubble__role::after": "减弱动效覆盖：reduce 下关掉打字动画",
}


def iter_rules(css: str):
    """逐条产出 (选择器, 声明体)。@media / @supports 的壳会剥掉、递归进内部。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)   # 注释里可能有花括号
    index, length = 0, len(css)
    buffer = ""
    while index < length:
        char = css[index]
        if char == "{":
            selector = buffer.strip()
            depth, cursor = 1, index + 1
            while cursor < length and depth:
                if css[cursor] == "{":
                    depth += 1
                elif css[cursor] == "}":
                    depth -= 1
                cursor += 1
            body = css[index + 1: cursor - 1]
            if selector.lstrip().startswith("@"):
                yield from iter_rules(body)
            else:
                yield selector, body
            buffer = ""
            index = cursor
        elif char == "}":
            buffer = ""
            index += 1
        else:
            buffer += char
            index += 1


def _pseudo_selectors():
    """[{选择器: 出现次数}]，只统计带 ::before / ::after 的。"""
    counts: dict[str, int] = {}
    for selector, _ in iter_rules(CSS):
        for part in selector.split(","):
            part = " ".join(part.split())
            if "::before" in part or "::after" in part:
                counts[part] = counts.get(part, 0) + 1
    return counts


def test_no_pseudo_element_selector_is_defined_twice():
    counts = _pseudo_selectors()
    duplicated = sorted(name for name, times in counts.items() if times > 1)
    unexpected = [name for name in duplicated if name not in ALLOWED_DUPLICATES]
    assert not unexpected, (
        "这些伪元素选择器被定义了多次（后写的会静默覆盖前面的，很容易写出"
        f"「一半属性来自旧规则、一半来自新规则」的怪样子）：{unexpected}\n"
        "确实需要分两处写的，加进 ALLOWED_DUPLICATES 并写清理由。"
    )


def test_allowed_duplicates_are_still_duplicated():
    """白名单不能烂在那儿：某条重复被合并掉之后，条目也要一起删。"""
    counts = _pseudo_selectors()
    stale = sorted(name for name in ALLOWED_DUPLICATES if counts.get(name, 0) < 2)
    assert not stale, f"这些已经不重复了，从 ALLOWED_DUPLICATES 里删掉：{stale}"


def test_nav_underline_is_centered_by_transform():
    """导航下划线＝居中 18px 药丸：left:50% + translateX(-50%)。

    一旦有人再给 .nav-link 相关的 ::after 写死 left（比如 left: 12px），
    优先级更高的那条会赢，横条就会被 translateX(-50%) 拖到文字左边去。
    """
    underline_rules = [
        (selector, body)
        for selector, body in iter_rules(CSS)
        if ".nav-link" in selector and "::after" in selector
    ]
    assert underline_rules, "应该有 .nav-link::after 的规则"

    base = [body for selector, body in underline_rules if selector.strip() == ".nav-link::after"]
    assert base, "缺 .nav-link::after 基础规则（居中与动效都靠它）"
    assert "left: 50%" in base[0].replace("left:50%", "left: 50%"), base[0]
    assert "translateX(-50%)" in base[0].replace(" ", ""), base[0]

    fixed_left = [
        (selector, line.strip())
        for selector, body in underline_rules
        for line in body.splitlines()
        if re.match(r"\s*left:\s*(?!50%)\S", line)
    ]
    assert not fixed_left, (
        f"这些规则给导航下划线写死了 left，会把居中拖偏（应删掉，只留 left:50%）：{fixed_left}"
    )


def test_active_nav_underline_keeps_its_width_on_hover():
    """悬停的短横只给未选中的链接 —— 选中的那条被 hover 压回 12px 会当场缩一下。"""
    hover_rules = [
        " ".join(selector.split())
        for selector, _ in iter_rules(CSS)
        if "::after" in selector and ":hover" in selector and "nav-link" in selector
    ]
    assert hover_rules, "应该有导航下划线的 hover 规则"
    for selector in hover_rules:
        assert ":not(.is-active)" in selector, (
            f"hover 规则要排除选中的链接，否则选中项的下划线会在鼠标移上去时缩短：{selector}"
        )


def test_entry_animations_do_not_use_forwards_fill():
    """入场动画不能用带 forwards 的填充（both / forwards）。

    forwards 会把末帧的 transform 永远留在元素上，而任何 transform
    （哪怕 translateY(0)）都会创建层叠上下文 —— 页头里的「更多」菜单
    z-index 被困住，整个页头会被后写的吸顶目录盖住（2026-09-14 用户截图实锤）。
    """
    import re

    for match in re.finditer(r"animation:([\w-]+)\s+[^;]*;\n?", CSS):
        shorthand = match.group(0).strip()
        if match.group(1) == "ink-rise":
            assert "both" not in shorthand and "forwards" not in shorthand, (
                f"ink-rise 不能用 forwards/both 填充（会残留 transform、困住菜单 z-index）：{shorthand}"
            )


def test_stats_page_is_reachable_from_nav():
    """/stats 曾经全站没有任何入口（用户因此一直没见过热力图）。"""
    from pathlib import Path

    base = (Path(__file__).resolve().parent.parent / "app/templates/base.html").read_text(
        encoding="utf-8"
    )
    assert 'href="/stats"' in base, "顶栏导航里必须有 /stats 入口"


def test_css_custom_properties_are_defined_before_use():
    """style.css 里 var(--x) 引用的自定义属性必须真有定义（或有兜底值）。

    教训：多 agent 并行写补丁时，补丁里用了 --text/--border/--muted/--hover
    这类「别处的通用令牌」，本项目根本没有——有硬编码兜底的会在暗色主题下变成
    浅色块，没兜底的（--ok）会让整条声明失效。守卫在合并前就拦住。
    """
    import re

    css = CSS
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
    # 由模板内联 / JS setProperty 注入的覆盖项，允许无 var() 兜底
    injected = {"--i", "--dot", "--split", "--cols", "--arrow-x"}
    problems = []
    for match in re.finditer(r"var\(\s*(--[a-z0-9-]+)\s*(,[^)]*)?\)", css):
        name, fallback = match.group(1), match.group(2)
        if name in defined or name in injected:
            continue
        if fallback and fallback.strip(" ,"):
            continue          # 有兜底值：允许（但要确认兜底不是硬编码浅色）
        line = css[:match.start()].count("\n") + 1
        problems.append(f"第 {line} 行 var({name}) 无定义也无兜底")
    assert not problems, "未定义且无兜底的 CSS 变量：" + "；".join(problems[:8])


def test_css_fallbacks_do_not_hardcode_light_colors():
    """**未定义**的令牌，其兜底值不得是硬编码浅色（暗色主题下会露馅）。

    注意只查「令牌本身没定义」的情况——令牌有定义时兜底永远不生效，
    #fff 这类写法无害（项目里大量存在）。
    """
    import re

    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", CSS))
    injected = {"--i", "--dot", "--split", "--cols", "--arrow-x"}
    offenders = []
    for match in re.finditer(r"var\(\s*(--[a-z0-9-]+)\s*,\s*([^)]+)\)", CSS):
        name, fallback = match.group(1), match.group(2).strip()
        if name in defined or name in injected:
            continue
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", fallback):
            continue
        rgb = [int(fallback.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
        if sum(rgb) / 3 > 200:
            offenders.append(f"{name} → {fallback}")
    assert not offenders, "未定义令牌的兜底是硬编码浅色（暗色主题会露馅）：" + "、".join(offenders[:8])
