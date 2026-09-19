# -*- coding: utf-8 -*-
from __future__ import annotations
"""agent 的循环层：JSON 协议循环、取消、格式重试、流式事件。"""
import json
import logging
import sqlite3
import threading
import time
from typing import Any
from uuid import uuid4

from ... import repo
from . import ai
from .history import _last_run_recap, _notes_list, _record_run, MAX_HISTORY_CHARS, MAX_HISTORY_TURNS
from .prompt import (SYSTEM_PROMPT, UNTRUSTED_CLOSE, UNTRUSTED_OPEN, _today_label,
                     fence_untrusted)
from .safety import _clear_pending_op, _get_pending_op, _save_pending_op, confirm_card, needs_confirm
from .tools import OBSERVE_LIMIT, _as_int, _describe_tools, _make_tools

logger = logging.getLogger("inknote.agent")

MAX_STEPS = 20               # 单次任务最多几步（原来 6 → 16 → 20：整理类任务常在十几步）

MAX_REPEAT_STEPS = 3         # 连续这么多步都在重复调用就收场，别把预算烧光

MAX_FORMAT_RETRIES = 2       # 模型没按 JSON 协议回：带反馈重试这么多次后才降级

MAX_ACTIONS_PER_TURN = 4     # 单轮最多并做几个工具（协议 v2：actions[] 数组）

CHAT_RETRIES = 2             # 模型调用失败自动重试次数（总共尝试 N 次）

PLAN_MAX_CHARS = 4000

_CANCEL_EVENTS: dict[str, threading.Event] = {}

_CANCEL_LOCK = threading.Lock()

def new_run_id() -> str:
    return uuid4().hex[:12]

def _register_run(run_id: str) -> threading.Event:
    with _CANCEL_LOCK:
        event = _CANCEL_EVENTS.get(run_id)
        if event is None:
            event = threading.Event()
            _CANCEL_EVENTS[run_id] = event
        # 顺手清理已结束的残留（防御性，正常都会在 finally 里摘掉）
        if len(_CANCEL_EVENTS) > 64:
            for key in [k for k, v in _CANCEL_EVENTS.items() if k != run_id and v.is_set()]:
                _CANCEL_EVENTS.pop(key, None)
        return event

def _release_run(run_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(run_id, None)

def request_cancel(run_id: str) -> bool:
    """请求取消一个正在跑的任务。返回是否找到了还在跑的 run。"""
    with _CANCEL_LOCK:
        event = _CANCEL_EVENTS.get(str(run_id or ""))
    if event is None:
        return False
    event.set()
    return True

def _clean_history(history: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """把前端送来的会话历史收敛成安全形状：只留 user/assistant、限条数与长度。"""
    cleaned: list[dict[str, str]] = []
    if not isinstance(history, list):
        return cleaned
    for item in history[-MAX_HISTORY_TURNS * 2:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            cleaned.append({"role": role, "content": content[:MAX_HISTORY_CHARS]})
    return cleaned

def _chat_with_retry(conn: sqlite3.Connection, messages: list[dict[str, str]]):
    """调一次模型，失败自动重试。返回 (回复文本, 错误)；后者为 None 表示成功。"""
    last_error: ai.AIError | None = None
    for attempt in range(CHAT_RETRIES):
        try:
            return ai.chat(messages, task="agent", conn=conn, temperature=0.0, max_tokens=2000), None
        except ai.AIError as exc:
            last_error = exc
            if attempt + 1 < CHAT_RETRIES:
                time.sleep(1.0)
    return None, last_error

def _partial_answer(steps: list[dict[str, Any]], error: Exception) -> str:
    """模型挂了但已经做了一些事：把进度如实交付，并给出可操作的补救建议。

    以前这种情况直接返回空回答 —— 用户白等几十秒，连做过什么都看不到。
    """
    lines = [f"没能跑完：{str(error)[:200]}"]
    done = [str(step.get("summary") or "") for step in steps if step.get("summary")]
    if done:
        lines.append("已经完成的步骤：")
        lines.extend(f"- {item}" for item in done[-8:])
        lines.append("上面这些改动都已经生效了。")
    lines.append("可以在设置页把「AI 超时」调大（默认 45 秒），或者把任务拆小一点再试。")
    return "\n".join(lines)

def _extract_json(raw: str) -> dict | None:
    """从模型回复里抠出 JSON 对象；容忍代码块包裹和前后废话。"""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text, flags=re.S).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None

def _planned_actions(data: dict) -> list[tuple[str, dict]]:
    """协议 v2：单轮可带 actions 数组并做多个独立工具；兼容旧的单数 action。

    最多 MAX_ACTIONS_PER_TURN 个；final 不在这里处理。坏形状返回空列表。
    """
    planned: list[tuple[str, dict]] = []
    raw_list = data.get("actions")
    if isinstance(raw_list, list):
        for item in raw_list[:MAX_ACTIONS_PER_TURN]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("action") or "").strip()
            if not name or name == "final":
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            planned.append((name, params))
    if not planned:
        name = str(data.get("action") or "").strip()
        if name and name != "final":
            params = data.get("params") if isinstance(data.get("params"), dict) else {}
            planned.append((name, params))
    return planned[:MAX_ACTIONS_PER_TURN]

def _summarize_step(name: str, result: dict) -> str:
    if result.get("error"):
        return f"{name} 失败：{result['error']}"
    if name == "search_notes" or name == "list_recent":
        titles = "、".join(str(note.get("title")) for note in result.get("notes", [])[:5])
        return f"{name} 命中 {result.get('count', 0)} 篇：{titles}"
    if name == "create_note":
        return f"已创建笔记 #{result.get('note_id')}"
    if name == "read_note":
        return f"已读取《{result.get('title')}》"
    if name == "restore_version":
        return f"已把《{result.get('title')}》恢复到指定版本"
    if name == "merge_notes":
        return f"已合并 {len(result.get('merged_notes') or [])} 篇进《{result.get('target_title')}》"
    if name == "semantic_search":
        return f"语义检索命中 {result.get('count', 0)} 篇"
    if name == "replace_in_note":
        return f"已在《{result.get('title')}》里替换 {result.get('replaced', 0)} 处"
    if name == "prepend_note":
        return f"已在《{result.get('title')}》开头插入内容"
    if name == "rewrite_section":
        return f"已重写《{result.get('title')}》的「{result.get('section')}」小节"
    if name == "bulk_set_category":
        return f"已把 {result.get('updated', 0)} 篇的分类设为「{result.get('category') or '（空）'}」"
    if name == "find_similar":
        return f"找到 {result.get('count', 0)} 篇相似笔记"
    if name == "get_note_history":
        return f"查到 {result.get('count', 0)} 个历史版本"
    if name == "list_backlinks":
        return f"查到 {result.get('count', 0)} 篇反向链接"
    if result.get("updated"):
        return f"已更新笔记 #{result.get('note_id')}"
    return f"{name} 完成"

def iter_agent_events(
    conn: sqlite3.Connection,
    task: str,
    *,
    max_steps: int = MAX_STEPS,
    read_only: bool = False,
    dry_run: bool = False,
    history: list[dict[str, str]] | None = None,
    run_id: str | None = None,
    confirmed_plan: str | None = None,
):
    """agent 循环的流式版本：每完成一步就 yield 一个事件 dict，而不是干等。

    事件类型（SSE 里每个 `data: ` 行的内容）：
      {"type": "step", "tool": ..., "params": {...}, "summary": ..., "run_id": ...}
      {"type": "final", "ok": bool, "answer": ..., "steps": [...], "error": ...,
       "run_id": ..., "duration_ms": ..., "cancelled": bool}
      {"type": "error", "error": ...}（未配置 AI 等前置失败，没有 final）

    `run_agent` 就是它的消费者：吃掉 step/final/error 后拼回原来的 dict。

    取消：request_cancel(run_id) 置位后，循环在**下一个步骤边界**安全停下
    （正在进行的模型调用不会被掐断）；run_id 由调用方传入或自动生成。
    """
    if not ai.is_enabled():
        yield {"type": "error", "error": "尚未配置 AI 服务"}
        return
    task = (task or "").strip()
    if not task:
        yield {"type": "error", "error": "任务不能为空"}
        return

    run_id = run_id or new_run_id()
    cancel_event = _register_run(run_id)
    started = time.time()
    tools = _make_tools(conn)
    system = SYSTEM_PROMPT.format(tools=_describe_tools(tools), today=_today_label(),
                                  max_actions=MAX_ACTIONS_PER_TURN,
                                  untrusted_open=UNTRUSTED_OPEN,
                                  untrusted_close=UNTRUSTED_CLOSE)
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    recap = _last_run_recap(conn)
    if recap:
        # 补上「上一轮动过哪篇笔记」——history 里只有最终回答，note_id 常常已经丢了，
        # 于是用户说「继续」时模型只能反问（真实踩过：0 步就交白卷）
        messages.append({"role": "system", "content": recap})
    plan_text = str(confirmed_plan or "").strip()[:PLAN_MAX_CHARS]
    if plan_text:
        # 两段式任务流的后半段：用户在计划里审阅过这份计划，这里严格照做
        messages.append({"role": "system", "content":
            "用户已在计划中审阅并确认了以下计划，请严格按计划执行：可以微调参数，"
            "不要扩大范围、不要添加计划之外的大动作。\n" + plan_text})
    messages.extend(_clean_history(history))
    messages.append({"role": "user", "content": task})

    steps: list[dict[str, Any]] = []
    involved: dict[int, str] = {}   # 本次任务动过/读过的笔记 id -> 标题（给前端做链接）
    seen_calls: set[str] = set()    # 已成功执行过的「工具+参数」签名，用来拦住重复空转
    repeat_streak = 0               # 连续多少步是重复调用
    format_retries = 0              # 已经做过几次「格式失控带反馈重试」

    def _finish(ok: bool, answer: str, error: str = "") -> dict[str, Any]:
        answer = answer or ""
        _record_run(conn, task, ok=ok, answer=answer, steps=steps, error=error,
                    read_only=read_only, dry_run=dry_run, involved=involved,
                    duration_ms=_elapsed_ms(), cancelled=cancel_event.is_set())
        return {"type": "final", "ok": ok, "answer": answer, "steps": steps,
                "error": error, "notes": _notes_list(involved), "run_id": run_id,
                "duration_ms": _elapsed_ms(), "cancelled": cancel_event.is_set()}

    def _elapsed_ms() -> int:
        return int((time.time() - started) * 1000)

    def _push_observe(payload: dict, limit_chars: int) -> None:
        """把工具结果喂回模型：整体围上「不可信」围栏（prompt injection 防线的执行点），
        再按工具自己的上限截断，别把长正文一刀切掉。

        围栏在唯一出口处生效：无论哪个工具（含将来新增的），笔记衍生的内容都以
        数据身份进入模型；伪造的结束标记在 fence_untrusted 里已被中性化。
        """
        text = fence_untrusted(json.dumps(payload, ensure_ascii=False))
        if len(text) > limit_chars:
            text = text[:limit_chars] + "…（结果过长已截断）"
        messages.append({"role": "user", "content": "工具结果：" + text})

    def _collect(note_id: Any, title: Any) -> None:
        if not isinstance(note_id, int):
            return
        if not title:
            try:
                note = repo.get_note(conn, note_id)
                title = str((note or {}).get("title") or "")
            except Exception:
                title = ""
        involved[note_id] = title or f"笔记 #{note_id}"

    def _execute_one(action: str, params: dict) -> tuple[dict, str, dict | None]:
        """执行单个动作，返回 (观察结果, 步骤摘要, 确认信息或 None)。

        只改外层的 seen_calls / repeat_streak；步数、事件与收尾由外层管。
        """
        nonlocal repeat_streak
        signature = action + ":" + json.dumps(params, ensure_ascii=False, sort_keys=True)
        spec = tools.get(action)
        confirm_info: dict | None = None

        if signature in seen_calls and spec is not None:
            # 同一个工具 + 完全一样的参数又调一次：结果不会变，别把步数烧在这儿
            repeat_streak += 1
            observation = {
                "error": "这一步刚才已经执行过（工具和参数完全相同），再执行结果也不会变。"
                         "请换个做法，或者直接用 final 给出答案。"
            }
            summary = f"跳过重复的 {action} 调用"
        elif dry_run:
            # 计划：所有工具都只「说要做什么」，不真正执行。模型会拿到固定的规划提示，
            # 想清楚全部步骤后用 final 输出「将要做的事」清单 —— 适合先审后放。
            repeat_streak = 0
            observation = {
                "dry_run": True,
                "note": "计划模式：本工具没有被真正调用。请继续规划后续步骤；独立的读操作"
                        "请合并进 actions 一轮做完，减少轮次。全部想清楚后用 final 输出"
                        "「将要做的事」清单（不要声称已执行）。",
            }
            summary = f"（计划）将执行 {action}"
            seen_calls.add(signature)
        elif read_only and spec.writes:
            repeat_streak = 0
            observation = {"error": "只读模式：本次任务不执行写操作，如需修改请关闭只读模式后重试"}
            summary = f"已拦截写操作 {action}（只读模式）"
            seen_calls.add(signature)
        elif needs_confirm(spec, params, conn):
            # 危险/大影响操作：不执行，生成确认卡片等用户点「确认执行」（机制层兜底）
            repeat_streak = 0
            ctx_key = spec.subject_key
            ctx_note = None
            if params.get(ctx_key) is not None:
                ctx_note = repo.get_note(conn, _as_int(params.get(ctx_key)))
                if ctx_note is None:
                    return {"error": "笔记不存在"}, f"{action} 失败：笔记不存在", None
            existing = _get_pending_op(conn)
            if (existing and existing.get("action") == action
                    and existing.get("params") == params):
                pending = existing
                repeat_streak += 1   # 反复生成同一张确认卡也按空转算
            else:
                pending = _save_pending_op(conn, action, params, ctx_note or {})
            label, consequence = confirm_card(spec, params)
            subject = ""
            if ctx_note is not None:
                subject = str(ctx_note.get("title") or subject)
                summary = f"等待用户确认：{label}《{subject}》"
            else:
                summary = f"等待用户确认：{label}"
            observation = {
                "confirm_required": True,
                "confirm_id": pending["id"],
                "note": "确认卡片已生成，本步没有真正执行。用户在页面上点「确认执行」才会生效。",
            }
            confirm_info = {"id": pending["id"], "note_id": params.get(ctx_key),
                            "title": subject, "label": label, "consequence": consequence}
        elif spec is None:
            repeat_streak = 0
            observation = {"error": f"未知工具 {action!r}，可用工具：{', '.join(tools)}"}
            summary = f"未知工具 {action}"
        else:
            repeat_streak = 0
            try:
                observation = spec.run(params)
            except (TypeError, ValueError, OverflowError) as exc:
                observation = {"error": f"参数不合法：{exc}"}
            except Exception as exc:  # 工具内部出错也不能让整个 agent 崩
                logger.warning("agent 工具 %s 执行失败", action, exc_info=True)
                observation = {"error": f"工具执行失败：{exc}"}
            summary = _summarize_step(action, observation)
            # 只有真的执行过才算「做过」；报错的调用允许换个参数重试
            if not (isinstance(observation, dict) and observation.get("error")):
                seen_calls.add(signature)
        return observation, summary, confirm_info

    try:
        for _step_no in range(max_steps):
            if cancel_event.is_set():
                done = f"已完成 {len(steps)} 步，" if steps else ""
                yield _finish(True, f"任务已按你的要求取消。{done}已完成的操作都生效了。")
                return

            raw, last_error = _chat_with_retry(conn, messages)
            if last_error is not None:
                # 模型调用重试后仍挂：**不丢掉已完成的步骤**，把进度和补救建议一起交付
                yield _finish(False, _partial_answer(steps, last_error), error=str(last_error))
                return

            data = _extract_json(raw)
            if data is None:
                # 模型没按格式回：带反馈让它重试几次；仍不行就把原话当最终回答收场
                if format_retries < MAX_FORMAT_RETRIES:
                    format_retries += 1
                    messages.append({"role": "assistant", "content": (raw or "").strip()[:2000]})
                    messages.append({"role": "user", "content":
                        "你上一条回复不符合约定格式。只输出一个 JSON 对象："
                        '调工具用 {"action": "工具名", "params": {...}}'
                        '（多个独立工具用 {"actions": [...]}），'
                        '完成用 {"action": "final", "answer": "..."}。'
                        "不要输出其他文字或代码块。"})
                    continue
                yield _finish(True, (raw or "").strip())
                return

            action = str(data.get("action") or "").strip()
            if action == "final":
                yield _finish(True, str(data.get("answer") or "").strip())
                return

            planned = _planned_actions(data)
            if not planned:
                if format_retries < MAX_FORMAT_RETRIES:
                    format_retries += 1
                    messages.append({"role": "assistant",
                                     "content": json.dumps(data, ensure_ascii=False)})
                    messages.append({"role": "user", "content":
                        "这一步没有可执行的工具调用（action 不是已知工具）。"
                        "请重新输出一个 JSON 对象。"})
                    continue
                yield _finish(True, "我没有看懂这一步的指令格式，先停下来了。已完成的操作都生效了。")
                return

            # ---- 执行本轮动作（协议 v2：单轮可并做多个独立工具） ----
            format_retries = 0          # 能正常出招了就重置格式重试计数
            round_observations: list[dict[str, Any]] = []
            observe_limits: list[int] = []
            stop_round = False
            last_confirm: dict | None = None
            for sub_action, sub_params in planned:
                if len(steps) >= max_steps:
                    break
                if cancel_event.is_set():
                    stop_round = True
                    break
                step_started = time.perf_counter()
                observation, summary, confirm_info = _execute_one(sub_action, sub_params)
                step_ms = int((time.perf_counter() - step_started) * 1000)
                if isinstance(observation, dict):
                    _collect(observation.get("note_id"), observation.get("title"))
                    _collect(observation.get("id"), observation.get("title"))
                    for item in observation.get("notes") or []:
                        if isinstance(item, dict):
                            _collect(item.get("id"), item.get("title"))
                    if observation.get("merged"):
                        _collect(observation.get("target_id"), observation.get("target_title"))
                step = {"tool": sub_action, "summary": summary, "params": sub_params,
                        "duration_ms": step_ms}
                steps.append(step)
                # 先把这一步推给前端，再准备下一轮——这就是「流式」的核心
                event: dict[str, Any] = {"type": "step", "tool": sub_action, "summary": summary,
                                         "params": sub_params, "duration_ms": step_ms,
                                         "notes": _notes_list(involved),
                                         "run_id": run_id}
                if confirm_info:
                    event["confirm"] = confirm_info
                yield event
                round_observations.append({"action": sub_action, "result": observation})
                observe_limits.append(
                    int(getattr(tools.get(sub_action), "observe_limit", None) or OBSERVE_LIMIT))
                if confirm_info:
                    # 等用户确认期间别继续执行本轮剩余动作，避免在未确认状态下叠加操作
                    last_confirm = confirm_info
                    stop_round = True
                    break

            if not round_observations:
                continue   # 本轮没动任何工具（步数耗尽/取消），交给外层判断收尾

            messages.append({"role": "assistant", "content": json.dumps(data, ensure_ascii=False)})
            _push_observe({"results": round_observations},
                          max(observe_limits) if observe_limits else OBSERVE_LIMIT)

            if cancel_event.is_set():
                done = f"已完成 {len(steps)} 步，" if steps else ""
                yield _finish(True, f"任务已按你的要求取消。{done}已完成的操作都生效了。")
                return

            if repeat_streak >= MAX_REPEAT_STEPS:
                answer = (
                    f"我卡在重复操作上了（同一个调用连着做了 {repeat_streak} 次，结果不会变），先停下来。"
                    "已经完成的操作都生效了。你可以把任务说得更具体一点，或者告诉我下一步做什么。"
                )
                yield _finish(True, answer)
                return

            if stop_round and last_confirm:
                # 生成了确认卡片：本轮到此为止，等用户在页面上确认（不再烧模型调用）
                yield _finish(True, "已生成确认卡片，等你在页面上点「确认执行」。确认前我不会继续操作。")
                return

        # 步数耗尽还没收尾：安全停下，绝不无限循环
        answer = "步骤太多，我先停下来了。已经完成的操作都生效了，你可以继续给我补充指令。"
        yield _finish(True, answer)
    finally:
        _release_run(run_id)

def run_agent(
    conn: sqlite3.Connection,
    task: str,
    *,
    max_steps: int = MAX_STEPS,
    read_only: bool = False,
    dry_run: bool = False,
    history: list[dict[str, str]] | None = None,
    run_id: str | None = None,
    confirmed_plan: str | None = None,
) -> dict[str, Any]:
    """跑一次 agent 循环。等价于消费 `iter_agent_events` 并拼回 {ok, answer, steps, error}。

    保留旧签名与返回值结构，原有测试无需改动即可全绿。
    """
    final: dict[str, Any] | None = None
    for event in iter_agent_events(conn, task, max_steps=max_steps, read_only=read_only,
                                   dry_run=dry_run, history=history, run_id=run_id,
                                   confirmed_plan=confirmed_plan):
        if event["type"] == "error":
            return {"ok": False, "answer": "", "steps": [], "error": event["error"]}
        if event["type"] == "final":
            final = event
            break
        # step 事件：步骤已包含在最终 final 的 steps 里，这里忽略即可
    if final is None:
        return {"ok": False, "answer": "", "steps": [], "error": "agent 没有产生结果",
                "cancelled": False, "duration_ms": 0}
    return {
        "ok": bool(final.get("ok")),
        "answer": final.get("answer") or "",
        "steps": final.get("steps") or [],
        "error": final.get("error") or "",
        "cancelled": bool(final.get("cancelled")),
        "duration_ms": int(final.get("duration_ms") or 0),
    }
