"""自检脚本 `scripts/check.py` 的测试（保证它自己别坏、别在 GBK 控制台上崩）。"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check.py"

# 这些宽字符在 Windows GBK 控制台上会 UnicodeEncodeError，脚本里不许出现
FORBIDDEN = ["✅", "⚠️", "❌", "🚀", "🎉", "❓"]


def test_script_exists_and_compiles():
    assert SCRIPT.is_file(), "scripts/check.py 应该存在"
    import ast

    ast.parse(SCRIPT.read_text(encoding="utf-8-sig"), str(SCRIPT))


def test_no_wide_emoji_in_source():
    """之前 scripts/audit_css.py 就因为打印 ✅ 在 GBK 控制台上崩过。"""
    source = SCRIPT.read_text(encoding="utf-8")
    found = [char for char in FORBIDDEN if char in source]
    assert not found, f"自检脚本里不该出现这些字符（GBK 控制台会崩）：{found}"


def test_help_works():
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert process.returncode == 0, process.stderr
    assert "--quick" in process.stdout
    assert "--port" in process.stdout


def test_import_has_no_side_effects():
    """import 不该跑主流程（不能因为被 import 就跑一遍全量测试）。"""
    code = (
        "import sys, pathlib;"
        f"sys.path.insert(0, r'{ROOT}');"
        "import scripts.check as c;"
        "print('IMPORTED');"
        "print(hasattr(c, 'main'))"
    )
    process = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert "IMPORTED" in process.stdout, process.stderr
    assert "True" in process.stdout
    assert "自检" not in process.stdout.split("IMPORTED")[0], "import 时不该执行主流程"


@pytest.mark.slow
def test_quick_mode_runs_green():
    """真跑一次 --quick：语法 + 样式 + 少量测试 + 冒烟，都必须过。

    例外：并行开发时新模板 class 的样式可能还没合并进 style.css，
    这时「样式」那一步会（正确地）失败 —— 只有这一步允许红，其它都得绿。
    """
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--quick"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    output = process.stdout + process.stderr
    for expected in ("语法", "样式", "测试", "冒烟", "环境"):
        assert expected in output, f"自检输出里应该有「{expected}」这一步：{output[-1500:]}"

    # 汇总区里每个失败项：只允许「样式」失败（它负责产物最新 + class 是否都有样式）
    failures = [
        line for line in output.splitlines()
        if line.startswith("[!!]") and line.strip()
    ]
    unexpected = [line for line in failures if "样式" not in line]
    assert not unexpected, f"不该有别的失败项：{unexpected}\n{output[-2000:]}"
    if not failures:
        assert process.returncode == 0, output[-2000:]
    else:
        assert "结果：" in output
