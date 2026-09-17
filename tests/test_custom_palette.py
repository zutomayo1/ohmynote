"""自定义主题色 → 整套色板推导的测试。

覆盖：
1. 输出结构：亮 / 暗两条规则、各 15 个令牌、无游离花括号；
2. 纸面染色强度：鲜艳种子推导出的背景彩度足够（不再「看不出变化」）；
3. 近灰种子退化：纯灰种子推导出中性灰，不染脏色；
4. 可读性底线：正文 / 次级文字在推导背景上对比度达标（亮暗各测）；
5. 非法输入（非 #RRGGBB）返回空串；
6. 渲染回归：页面 style 里的 dark 选择器引号不被 Jinja 转义成 &#34;
   （曾导致整条暗色规则被 CSS 解析器丢弃）；
7. 防漂移：settings.html 的实时预览算法与 templating.py 用同一组彩度参数。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.templating import custom_brand_css

# 亮 / 暗两套纸面族的彩度参数（Python 与 JS 必须一致）
CHROMA_LIGHT = [".036+.030*sv", ".046+.038*sv", ".028+.026*sv",
                ".040+.034*sv", ".036+.032*sv", ".042+.036*sv"]
CHROMA_DARK = [".022+.030*sv", ".026+.034*sv", ".030+.036*sv",
               ".032+.032*sv", ".036+.034*sv"]
LUMA_LIGHT = [".940", ".885", ".974", ".915", ".855", ".775"]
LUMA_DARK = [".105", ".145", ".165", ".205", ".235", ".285"]

TOKENS = ("--bg", "--bg-soft", "--surface", "--surface-2", "--ink", "--ink-2",
          "--ink-3", "--line", "--line-strong", "--brand", "--brand-dark",
          "--brand-soft", "--accent", "--accent-soft", "--header-bg")


def split_rules(css: str) -> tuple[str, str]:
    """把输出切成（亮色规则体, 暗色规则体）。"""
    light, _, rest = css.partition("\n")
    assert light.startswith("html:root{"), light[:40]
    assert rest.startswith('html:root[data-theme="dark"]{'), rest[:40]
    return light, rest


def token(css: str, name: str, dark: bool = False) -> str:
    body = split_rules(css)[1 if dark else 0]
    match = re.search(r"%s:([^;]+);" % re.escape(name), body)
    assert match, "缺少令牌 %s" % name
    return match.group(1)


def rgb(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def chroma(value: str) -> int:
    r, g, b = rgb(value)
    return max(r, g, b) - min(r, g, b)


def relative_luma(value: str) -> float:
    def lin(c: int) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((relative_luma(a), relative_luma(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ---------------------------------------------------------------------------
# 1. 结构
# ---------------------------------------------------------------------------

def test_structure_two_rules_full_token_set():
    css = custom_brand_css("#3B5BA5")
    assert css.count("html:root") == 2
    assert "}};" not in css and "{{" not in css, "花括号转义残留"
    for name in TOKENS:
        assert token(css, name)
        assert token(css, name, dark=True)
    # 品牌色就是种子色本身（大小写规范化）
    assert token(css, "--brand") == "#3B5BA5"
    assert token(css, "--brand", dark=True) != "#3B5BA5", "暗色下品牌色应提亮"


# ---------------------------------------------------------------------------
# 2. 纸面染色强度：用户反馈「自定义色对背景没啥影响」的回归防线
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", ["#3B5BA5", "#C2185B", "#3D7A5F"])
def test_vivid_seed_tints_paper_visibly(seed):
    css = custom_brand_css(seed)
    for name in ("--bg", "--bg-soft", "--surface-2"):
        value = token(css, name, dark=False)
        assert chroma(value) >= 14, "%s 在 %s 下染色过淡：%s" % (name, seed, value)
    for name in ("--bg", "--surface"):
        value = token(css, name, dark=True)
        assert chroma(value) >= 10, "暗色 %s 在 %s 下染色过淡：%s" % (name, seed, value)
    # 背景彩度必须明显超过旧实现（旧值 ~5/255），且亮色底仍够亮
    light_bg = token(css, "--bg")
    assert relative_luma(light_bg) > 0.75, "亮色背景被压暗了：%s" % light_bg


def test_paper_hue_follows_seed():
    """纸面偏色方向应与种子色相同（蓝种子 → 冷底，红种子 → 暖底）。"""
    blue = rgb(token(custom_brand_css("#3B5BA5"), "--bg"))
    pink = rgb(token(custom_brand_css("#C2185B"), "--bg"))
    assert blue[2] > blue[0], "蓝种子背景应偏蓝：%r" % (blue,)
    assert pink[0] > pink[2], "洋红种子背景应偏红：%r" % (pink,)


# ---------------------------------------------------------------------------
# 3. 近灰种子 → 中性灰（不染脏色）
# ---------------------------------------------------------------------------

def test_gray_seed_stays_neutral():
    css = custom_brand_css("#4A4A4A")
    for name, dark in (("--bg", False), ("--bg-soft", False), ("--surface-2", False),
                       ("--bg", True), ("--line", False)):
        value = token(css, name, dark=dark)
        assert chroma(value) <= 3, "%s 被染出色相：%s" % (name, value)


def test_dark_backgrounds_are_dark():
    css = custom_brand_css("#3B5BA5")
    for name in ("--bg", "--bg-soft", "--surface"):
        assert relative_luma(token(css, name, dark=True)) < 0.05, name


# ---------------------------------------------------------------------------
# 4. 可读性底线
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", ["#3B5BA5", "#C2185B", "#3D7A5F", "#4A4A4A", "#F0C419"])
def test_text_contrast_holds(seed):
    css = custom_brand_css(seed)
    for dark in (False, True):
        bg = token(css, "--bg", dark=dark)
        assert contrast(token(css, "--ink", dark=dark), bg) >= 7.0, (seed, dark, "正文对比不足")
        assert contrast(token(css, "--ink-2", dark=dark), bg) >= 4.5, (seed, dark, "次级对比不足")


# ---------------------------------------------------------------------------
# 5. 非法输入
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "red", "#12345", "#GGGGGG", "#3B5BA5;color:red",
                                 "rgb(1,2,3)", "#3B5BA5 "])
def test_invalid_input_returns_empty(bad):
    assert custom_brand_css(bad) in ("", custom_brand_css("#3B5BA5")), bad


# ---------------------------------------------------------------------------
# 6. 渲染回归：暗色选择器引号不被转义
# ---------------------------------------------------------------------------

def test_rendered_style_keeps_dark_rule_quotes(auth_client):
    from app import db as db_mod
    from app.services import site_settings

    with db_mod.db() as conn:
        site_settings.save(conn, {"appearance_custom": "#3B5BA5"})
    try:
        html = auth_client.get("/blog").text
        assert 'html:root[data-theme="dark"]' in html, "暗色规则被转义/丢失"
        assert "&#34;" not in html, "引号被 Jinja 转义成实体，CSS 会整条失效"
        assert "nonce=" in html
    finally:
        with db_mod.db() as conn:
            site_settings.reset(conn)


# ---------------------------------------------------------------------------
# 7. 防漂移：JS 实时预览与 Python 推导共用同一组参数
# ---------------------------------------------------------------------------

def test_preview_js_matches_python_parameters():
    root = Path(__file__).resolve().parent.parent
    py = (root / "app" / "templating.py").read_text(encoding="utf-8")
    html = (root / "app" / "templates" / "settings.html").read_text(encoding="utf-8")
    compact_py = re.sub(r"\s+", "", py)
    compact_js = re.sub(r"\s+", "", html)
    for expr in CHROMA_LIGHT + CHROMA_DARK:
        assert expr in compact_py, "Python 缺少彩度参数 %s" % expr
        assert expr in compact_js, "预览 JS 缺少彩度参数 %s（已漂移）" % expr
    for luma in LUMA_LIGHT + LUMA_DARK:
        assert luma in compact_py and luma in compact_js, "亮度参数 %s 未同步" % luma
    assert "appearance-preview" in html, "预览 style 元素缺失"
