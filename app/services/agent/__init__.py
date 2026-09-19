# -*- coding: utf-8 -*-
"""墨痕的笔记 Agent（2026-09-18 拆包）：core 循环 / tools 工具层 / safety 确认层 /
history 执行历史 / prompt 提示词。公共 API 与拆分前的单文件版本完全一致。"""
from ... import repo
from .. import ai  # noqa: F401  （保持 agent.ai / agent.repo 属性可用）

from .core import (MAX_ACTIONS_PER_TURN, MAX_FORMAT_RETRIES, MAX_REPEAT_STEPS, MAX_STEPS,
                   OBSERVE_LIMIT,
                   PLAN_MAX_CHARS, _chat_with_retry, _clean_history, _extract_json,
                   _partial_answer, _planned_actions, _register_run, _release_run,
                   iter_agent_events, new_run_id, request_cancel, run_agent)
from .history import (MAX_HISTORY_CHARS, MAX_HISTORY_TURNS, MAX_RUNS, _last_run_recap,
                      _notes_list, _record_run, clear_runs, delete_run, ensure_run_ids,
                      list_runs)
from .prompt import SYSTEM_PROMPT, _today_label  # noqa: F401
from .safety import (_ALL_CONFIRMABLE, _WRITE_TOOLS, _clear_pending_op,
                     _get_pending_op, _save_pending_op, _take_pending_op,
                     cancel_pending, execute_pending)
from .tools import (ToolSpec, _as_int, _as_tags, _describe_tools, _heading_lines,
                    _locate_section, _make_tools, _note_brief, _opt_int, _TOOL_SPECS)

__all__ = [
    "run_agent", "iter_agent_events", "request_cancel", "new_run_id",
    "execute_pending", "cancel_pending", "list_runs", "clear_runs", "delete_run",
    "ensure_run_ids", "SYSTEM_PROMPT", "ToolSpec", "repo", "ai",
    "MAX_STEPS", "MAX_REPEAT_STEPS", "MAX_FORMAT_RETRIES", "MAX_ACTIONS_PER_TURN",
    "PLAN_MAX_CHARS", "OBSERVE_LIMIT",
    "MAX_HISTORY_TURNS", "MAX_HISTORY_CHARS", "MAX_RUNS", "_TOOL_SPECS",
    "_make_tools", "_describe_tools", "_extract_json", "_clean_history", "_today_label",
    "_record_run", "_notes_list", "_last_run_recap", "_locate_section", "_heading_lines",
    "_as_int", "_as_tags", "_note_brief", "_opt_int", "_planned_actions", "_chat_with_retry",
    "_partial_answer", "_register_run", "_release_run",
    "_save_pending_op", "_get_pending_op", "_take_pending_op", "_clear_pending_op",
    "_WRITE_TOOLS", "_ALL_CONFIRMABLE",
]
