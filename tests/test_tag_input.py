"""B 组：编辑器标签输入增强的回归测试。

前端交互没法在 pytest 里真跑，这里覆盖能测的部分：
1) 服务端渲染的「常用标签」胶囊区块 / 自动补全面板容器 / 提示元素；
2) 原输入框 name / id、工具栏、AI 按钮没被弄坏；
3) 用 node 子进程复测与后端 app/utils.py 对齐的纯函数 parseTags
   （切分 / 去 # / 大小写去重 / 最多 12 个）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import PASSWORD, csrf_of

ROOT = Path(__file__).resolve().parents[1]
EDITOR_JS = ROOT / "app" / "static" / "js" / "editor.js"

COMMON_TAG = "标签增强甲"
SECOND_TAG = "标签增强乙"

# 在 node 里搭一个最小的全局环境加载 editor.js，然后测挂在 window.InkNote.tagUtils 上的纯函数。
NODE_HARNESS = r"""
'use strict';
global.window = global;
global.document = {
  readyState: 'complete',
  addEventListener: function () {},
  removeEventListener: function () {},
  body: null,
  getElementById: function () { return null; },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  createElement: function () { return {}; }
};

require(process.env.EDITOR_JS);

var utils = global.window.InkNote && global.window.InkNote.tagUtils;
if (!utils) { console.error('window.InkNote.tagUtils 不存在'); process.exit(1); }

function eq(label, actual, expected) {
  var a = JSON.stringify(actual);
  var b = JSON.stringify(expected);
  if (a !== b) { console.error('FAIL ' + label + ': ' + a + ' !== ' + b); process.exit(1); }
}

eq('parseTags basic', utils.parseTags('python, Python、#异步'), ['python', '异步']);
eq('parseTags spaces', utils.parseTags(' a ,b  c、d'), ['a', 'b', 'c', 'd']);
eq('parseTags separators', utils.parseTags('x;y|z\tw\nv'), ['x', 'y', 'z', 'w', 'v']);
eq('parseTags blank', utils.parseTags('   '), []);
eq('parseTags hash ci', utils.parseTags('#Tag #tag'), ['Tag']);
eq('parseTags cap', utils.parseTags('A,B,C,D,E,F,G,H,I,J,K,L,M,N'),
   ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L']);
eq('MAX_TAGS', utils.MAX_TAGS, 12);
eq('normalizeTag', utils.normalizeTag('  #Py  thon  '), 'Py thon');
console.log('tagUtils ok');
"""


@pytest.fixture(scope="module")
def editor_client(client):
    """独立登录的客户端。

    共享的 auth_client 会被别的测试（改密码 / 新 TestClient 生命周期）弄丢会话，
    全量跑时这里不能依赖它；自己开一个 TestClient 走真实 /login。
    """
    fresh = client.__class__(client.app)
    response = fresh.post(
        "/login", data={"password": PASSWORD, "next": "/notes"}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    return fresh


@pytest.fixture(scope="module")
def tagged_notes(editor_client):
    """造两篇带标签的笔记，保证「常用标签」区块有服务端数据。"""
    csrf_token = csrf_of(editor_client)
    created = []
    for index, tags in enumerate((f"{COMMON_TAG}，{SECOND_TAG}", COMMON_TAG), start=1):
        response = editor_client.post(
            "/notes",
            data={
                "_csrf": csrf_token,
                "title": f"标签输入测试 {index}",
                "content": "标签输入增强测试正文。",
                "tags": tags,
                "action": "save",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        match = re.search(r"/notes/(\d+)", response.headers["location"])
        assert match, response.headers["location"]
        created.append(int(match.group(1)))
    return created


def _editor_html(editor_client, note_id: int) -> str:
    response = editor_client.get(f"/notes/{note_id}/edit")
    assert response.status_code == 200, response.text
    return response.text


def test_common_tags_block_shows_known_tags(editor_client, tagged_notes):
    html = _editor_html(editor_client, tagged_notes[0])
    match = re.search(r'<div class="tag-common"[^>]*id="tag-common".*?</div>', html, re.S)
    assert match, "没有找到服务端渲染的常用标签区块 #tag-common"
    block = match.group(0)
    chips = re.findall(r'data-tag="([^"]+)"', block)
    assert chips, "常用标签区块里一个胶囊都没有"
    assert 1 <= len(chips) <= 12, f"常用标签数量应在 1~12 之间，实际 {len(chips)}"
    # COMMON_TAG 被两篇笔记使用（次数 >= 2），按使用次数排序必然在常用里
    assert COMMON_TAG in chips, f"{COMMON_TAG} 应出现在常用标签区块，实际 {chips}"
    assert 'class="tag-chip' in block


def test_tags_input_and_ai_controls_intact(editor_client, tagged_notes):
    html = _editor_html(editor_client, tagged_notes[0])
    # 输入框本体必须保留（编辑器 JS / AI 推荐标签都依赖 tags-input + name=tags）
    assert 'id="tags-input"' in html
    assert 'name="tags"' in html
    assert re.search(r'<input[^>]*id="tags-input"[^>]*name="tags"', html), "tags-input 的 name 丢了"
    # 增强容器 / 面板 / 提示元素
    assert 'id="note-tags"' in html
    assert 'id="tag-suggest"' in html
    assert 'id="tag-input-hint"' in html
    assert 'id="tag-input-count"' in html
    # 工具栏与 AI 按钮没被弄坏
    assert 'id="md-toolbar"' in html
    assert 'id="ai-tags"' in html
    assert 'data-ai-tags-url="/api/ai/tags"' in html
    assert 'id="ai-summary"' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_parse_tags_pure_function_with_node(tmp_path):
    harness = tmp_path / "tag_utils_check.js"
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    env = dict(os.environ)
    env["EDITOR_JS"] = str(EDITOR_JS)
    proc = subprocess.run(
        [shutil.which("node"), str(harness)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, f"node 测试失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "tagUtils ok" in proc.stdout
