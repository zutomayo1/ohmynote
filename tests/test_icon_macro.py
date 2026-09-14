"""图标宏的守卫：模板里引用的图标必须真的存在。

`icon(name)` 是一长串 `{% elif %}`，名字写错**不会报错** ——
它会渲染出一个空 `<svg>`（占着位置、什么都不画）。
按钮上就表现为「图标位置凭空多出一段空白」，肉眼很难说清哪里不对。
整理动作区时就是这么发现 `icon('copy')` 一直不存在的（详情页与编辑器的复制按钮）。

顺带守住第二件事：宏里的名字要有注释块可读，所以这里只检查「用了但没定义」。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MACRO = (ROOT / "app/templates/_macros.html").read_text(encoding="utf-8")


def _defined() -> set[str]:
    return set(re.findall(r"name == '([a-z-]+)'", MACRO))


def _used() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for path in sorted((ROOT / "app/templates").rglob("*.html")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"icon\('([a-z-]+)'", source):
            used.setdefault(match.group(1), set()).add(path.name)
    return used


def test_every_referenced_icon_is_defined():
    defined = _defined()
    missing = {name: sorted(files) for name, files in _used().items() if name not in defined}
    assert not missing, (
        "这些图标名被模板引用、但 _macros.html 里没有定义"
        f"（会渲染成空白 svg，按钮上就是凭空多一段空白）：{missing}"
    )


def test_macro_defines_a_reasonable_set():
    """防止 elif 链被误删导致大面积图标一起变空白。"""
    defined = _defined()
    assert len(defined) >= 25, f"图标宏只剩 {len(defined)} 个，可能被改坏了"
    for must in ("edit", "trash", "search", "plus", "copy", "more", "download"):
        assert must in defined, f"缺基础图标 {must}"
