"""命令面板（Ctrl+K）升级的静态守卫测试。

面板逻辑全在 app.js 里（无独立模块），所以在服务端只做可静态断言的约定检查：
命令清单完整、拼音匹配键齐全、没有引入外部依赖、样式类有定义。
行为层由 .scratch/palette_e2e.py 用真实浏览器覆盖。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "static" / "js" / "app.js"
STYLE = ROOT / "app" / "static" / "css" / "style.css"


def _js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_palette_commands_cover_key_destinations():
    """首批命令要覆盖常用落点（含新增的 /graph）。"""
    src = _js()
    assert "PALETTE_COMMANDS" in src
    for title in ("新建笔记", "关系图谱", "写作统计", "模板", "回收站", "打开设置", "切换深色模式"):
        assert f"'{title}'" in src or f'"{title}"' in src, f"命令面板缺少「{title}」"


def test_palette_commands_have_pinyin_keys():
    """拼音首字母要能命中：关系图谱 = gxtp（用户在面板里会直接敲缩写）。"""
    src = _js()
    for abbr in ("gxtp", "xjbj", "jrtb", "hszh"):
        assert f"abbr: '{abbr}'" in src, f"缺少拼音首字母 {abbr}"
    assert "isSubsequence" in src, "应保留子序列模糊匹配实现"


def test_palette_has_no_external_dependency():
    """零依赖底线：不得引 CDN / 第三方库。"""
    src = _js()
    assert "cdn." not in src and "unpkg" not in src and "jsdelivr" not in src
    assert not re.search(r"import\s+.*from\s+['\"]http", src)


def test_palette_new_classes_are_styled():
    """新造的类名（分组标题 / 空状态）必须有样式，否则面板会显示成裸文本。"""
    css = STYLE.read_text(encoding="utf-8")
    src = _js()
    for cls in ("palette__group-title", "palette__empty"):
        assert f"'{cls}'" in src, f"app.js 里应该用到 {cls}"
        assert f".{cls}" in css, f"style.css 缺少 .{cls} 样式"


def test_palette_records_recent_notes_locally():
    """「最近打开」用 localStorage 记忆，键名沿用 inknote 前缀惯例。"""
    src = _js()
    assert "RECENT_KEY" in src
    assert re.search(r"RECENT_KEY\s*=\s*'inknote", src), "最近笔记的存储键应以 inknote 开头"
