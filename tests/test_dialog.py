"""自绘确认对话框：接线与结构守卫。

行为验证在 e2e（.scratch/e2e_dialog.py：Esc / 遮罩 / Tab 圈定 / 焦点 / 暗色适配 /
确认后动作生效）；这里锁住「接线别被改回去」这件事：

1. base.html 必须引入 dialog.js，且排在 app.js **之前**
2. data-confirm 的处理必须走自绘对话框，不能退回 window.confirm（原生弹窗会
   整页冻结、不跟主题、按钮只能写「确定/取消」）
3. 重新提交必须带上 submitter（requestSubmit）—— 站点设置那张表单里多个动作
   靠提交按钮的 name/value 与 formaction 区分，丢了 submitter 会执行错动作
4. 编辑器「未保存改动」同样走自绘
5. 关键危险操作都配了人话按钮文案
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import STATIC_DIR

APP_JS = Path("app/static/js/app.js").read_text(encoding="utf-8")
EDITOR_JS = Path("app/static/js/editor.js").read_text(encoding="utf-8")
DIALOG_JS = Path("app/static/js/dialog.js").read_text(encoding="utf-8")
BASE_HTML = Path("app/templates/base.html").read_text(encoding="utf-8")


def test_dialog_script_is_loaded_before_app_js():
    assert "js/dialog.js" in BASE_HTML, "base.html 没引入 dialog.js"
    dialog_at = BASE_HTML.index("js/dialog.js")
    app_at = BASE_HTML.index("js/app.js")
    assert dialog_at < app_at, "dialog.js 要排在 app.js 之前（defer 按文档顺序执行）"
    assert (STATIC_DIR / "js" / "dialog.js").is_file()


def test_dialog_module_has_expected_contract():
    for token in ("InkNote.dialog", "showModal", "addEventListener('close'", "data-dialog-ok",
                  "data-dialog-cancel", "window.confirm"):
        assert token in DIALOG_JS, f"dialog.js 缺少 {token}"
    # 老浏览器要退回原生，而不是「没有确认就直接执行」
    assert "showModal === 'function'" in DIALOG_JS or "showModal" in DIALOG_JS


def test_app_js_uses_dialog_and_resubmits_with_submitter():
    pattern = re.compile(r"function initConfirm\(\).*?\n  \}", re.S)
    match = pattern.search(APP_JS)
    assert match, "找不到 initConfirm"
    body = match.group(0)
    assert "InkNote.dialog" in body, "data-confirm 没有走自绘对话框"
    assert "requestSubmit" in APP_JS, "重新提交必须用 requestSubmit，否则 submitter 丢了"
    # 兜底可以是原生（老浏览器），但主路径不能是它
    confirm_lines = [line for line in body.splitlines() if "window.confirm" in line]
    assert len(confirm_lines) <= 1, "主路径不该再用 window.confirm"


def test_editor_unsaved_prompt_uses_dialog():
    assert "InkNote.dialog" in EDITOR_JS
    assert "仍要离开" in EDITOR_JS and "留在这里" in EDITOR_JS
    assert "window.confirm(form.dataset.unsavedText" not in EDITOR_JS, "编辑器还在用原生确认"


def test_dangerous_actions_have_human_button_words():
    """危险动作的按钮要写人话（这是换自绘的主要收益）。"""
    wanted = {
        "app/templates/templates.html": "删除模板",
        "app/templates/trash.html": "清空回收站",
        "app/templates/tags.html": "删除标签",
        "app/templates/images.html": "清理图片",
        "app/templates/backup.html": "回滚到这份备份",
    }
    for path, word in wanted.items():
        html = Path(path).read_text(encoding="utf-8")
        assert f'data-confirm-ok="{word}"' in html, f"{path} 缺 data-confirm-ok={word}"


def test_data_confirm_still_declarative_everywhere():
    """声明式入口不能退化：所有 data-confirm 都还带文案，且带按钮词。"""
    files = list(Path("app/templates").rglob("*.html"))
    confirms = 0
    for path in files:
        html = path.read_text(encoding="utf-8")
        confirms += len(re.findall(r'data-confirm="[^"]+"', html))
    assert confirms >= 18, f"data-confirm 数量异常：{confirms}"
    for path in files:
        html = path.read_text(encoding="utf-8")
        for match in re.finditer(r'data-confirm="([^"]*)"', html):
            assert match.group(1).strip(), f"{path} 有空文案的 data-confirm"
