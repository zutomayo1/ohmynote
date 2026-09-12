"""编辑器内联 AI：接着写 + 选中文本改写（润色 / 精简 / 扩写 / 翻译 / 更正式）。

这一层只做两件事：拼中文提示词、调用 ``ai.chat``。配置、超时、错误翻译、
脱敏、用量统计、local_only 全部交给 ``app/services/ai.py``，不自己发 HTTP。

任务名用 ``"inline"``：``ai.chat`` 对未知 task 会回退到通用模型（不会报错），
同时用量统计里能把「内联 AI」和摘要 / 打标签 / 问答分开看。
"""

from __future__ import annotations

import sqlite3

from . import ai

TASK = "inline"

# 单次送给模型的正文上限，和 ai.py 的 MAX_CONTEXT_CHARS 保持一致
MAX_CONTEXT_CHARS = 6000

CONTINUE_SYSTEM = (
    "你是一个中文笔记写作助手。用户给你一篇还没写完的笔记，请你接着往下写。要求："
    "1) 只输出续写的正文，不要解释、不要重复已经写过的内容、不要加标题，"
    "也不要写「好的」「以下是续写」这类开场白；"
    "2) 延续原文的语言、人称、语气和 Markdown 结构；"
    "3) 通常写 1-3 段，直接从下一句写起，让内容能与原文自然衔接。"
)

REWRITE_SYSTEM = (
    "你是一个专业的中文文字编辑，负责按用户的要求改写文本。要求："
    "1) 只输出改写后的文本，不要解释、不要加引号、不要写「以下是改写后的内容」这类话；"
    "2) 保持原有的 Markdown 结构（标题、列表、引用、加粗、代码块等）和段落顺序；"
    "3) 不要添加原文没有的事实，也不要输出与改写无关的内容。"
)

# mode -> （按钮 / 预览面板上的中文名, 给模型的具体要求）
REWRITE_MODES: dict[str, dict[str, str]] = {
    "polish": {
        "label": "润色",
        "detail": "把文字润色得更通顺自然：修正错别字、标点、搭配和语病，但不要改变原意，也不要增删事实。",
    },
    "concise": {
        "label": "精简",
        "detail": "把文字精简：删掉啰嗦、重复、可有可无的词句，保留全部关键信息，不要丢要点。",
    },
    "expand": {
        "label": "扩写",
        "detail": "把文字扩写：在保留原意的前提下补充细节、例子或解释，让内容更充实，不要编造事实。",
    },
    "translate": {
        "label": "翻译",
        "detail": "把中文翻译成英文（英文译文）：要自然、地道、符合英语习惯，保留 Markdown 结构，只输出英文。",
    },
    "formal": {
        "label": "更正式",
        "detail": "把文字改得更正式、书面化：去掉口语、网络用语、语气词和多余标点，保持原意。",
    },
}


def _trim(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…（内容过长已截断）"


def continue_text(
    title: str,
    content: str,
    *,
    instruction: str = "",
    conn: sqlite3.Connection | None = None,
) -> str:
    """接着笔记正文往下写，返回可以直接接在原文后面的续写片段。"""
    content = (content or "").strip()
    if not content:
        raise ai.AIError("正文还是空的，先写点内容吧")

    lines = [f"笔记标题：{(title or '').strip() or '（无标题）'}", "", "已经写好的正文：", _trim(content)]
    instruction = (instruction or "").strip()
    if instruction:
        lines += ["", f"额外要求：{instruction}"]
    lines += ["", "请接着上面正文继续写。"]
    prompt = "\n".join(lines)

    result = ai.chat(
        [
            {"role": "system", "content": CONTINUE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        max_tokens=600,
        task=TASK,
        conn=conn,
    )
    return (result or "").strip()


def rewrite(text: str, mode: str, *, conn: sqlite3.Connection | None = None) -> str:
    """按 mode 改写文本；mode 见 REWRITE_MODES。"""
    text = (text or "").strip()
    if not text:
        raise ai.AIError("没有可改写的文字")

    spec = REWRITE_MODES.get(str(mode or "").strip())
    if spec is None:
        raise ai.AIError(f"不支持的改写方式：{mode}")

    prompt = f"请对下面的文本做「{spec['label']}」。{spec['detail']}\n\n【待改写文本】\n{_trim(text)}"
    result = ai.chat(
        [
            {"role": "system", "content": REWRITE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=1200,
        task=TASK,
        conn=conn,
    )
    return (result or "").strip()
