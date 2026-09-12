"""AI 相关路由：设置页（配置 / 测试连接）与 /api/ai/* 接口。

单独成文件是为了让设置页、模型列表、用量统计、向量索引这些功能
有一个清晰的归属，不和笔记/博客的路由混在一起。
"""

from __future__ import annotations

import hmac
import sqlite3
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import repo
from ..deps import csrf_protect, db_conn, require_login, require_login_api
from ..services import account, ai, ai_usage, site_settings
from ..templating import render
from ..utils import now, url_with_query

# 设置页：表单提交，需要登录 + CSRF
settings_router = APIRouter(dependencies=[Depends(require_login), Depends(csrf_protect)])

# /api/ai/*：给前端 fetch 用，未登录返回 401 JSON
ai_api_router = APIRouter(
    prefix="/api",
    dependencies=[Depends(require_login_api), Depends(csrf_protect)],
)


def _json(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


async def read_json(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


router = settings_router  # 兼容旧写法

# 用量统计里 task 字段的中文名（summary/tags/answer 是 ai.chat 里的任务标识）
TASK_LABELS = {
    "summary": "摘要",
    "tags": "打标签",
    "answer": "问答",
    "embed": "向量",
}


def _usage_chart(by_day: list[dict], *, days: int = 30) -> list[dict]:
    """把「有记录的那几天」补成连续 30 天，顺带算好柱状图的高度百分比。

    纯 CSS 画柱子用：模板只读 percent 写进 style="height: N%"，
    empty 的那天给个 is-empty 类，视觉上留一条浅浅的基线。
    """
    rows = {str(item.get("day") or ""): item for item in by_day}
    peak = max([int(item.get("calls") or 0) for item in by_day] or [0])
    today = now().date()
    series: list[dict] = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        item = rows.get(day) or {}
        calls = int(item.get("calls") or 0)
        tokens = int(item.get("tokens") or 0)
        percent = int(round(calls * 100 / peak)) if peak else 0
        series.append(
            {
                "day": day,
                "label": day[5:],  # MM-DD
                "calls": calls,
                "tokens": tokens,
                "percent": max(percent, 6) if calls else 0,
                "empty": calls == 0,
            }
        )
    return series


# ---------------------------------------------------------------------------
# 设置页（目前主要是 AI 配置）
# ---------------------------------------------------------------------------
def _settings_context(request: Request, conn: sqlite3.Connection, **extra) -> HTMLResponse:
    """设置页需要的完整上下文（正常 GET 与表单校验失败回显共用）。"""
    usage = ai_usage.summary(conn)
    context: dict = {
        "ai": ai.describe(),
        "usage": usage,
        "usage_chart": _usage_chart(usage["by_day"]),
        "task_labels": TASK_LABELS,
        "prompts": ai.describe_prompts(),
        "site": site_settings.describe(),
        "account": account.describe(),
    }
    context.update(extra)
    return render(request, "settings.html", **context)


@settings_router.get("/settings")
def settings_page(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    """站点信息 + AI 配置 + 用量统计。页面保存的优先于 .env。"""
    return _settings_context(request, conn)


@settings_router.post("/settings/site")
def save_site_settings(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    site_title: str = Form(""),
    site_subtitle: str = Form(""),
    site_description: str = Form(""),
    author: str = Form(""),
    base_url: str = Form(""),
    per_page: str = Form("12"),
    trash_days: str = Form("30"),
    reset: str | None = Form(None),
):
    """保存站点信息：页面 > .env > 默认，非法值 flash 报错并回显、不写库。"""
    if reset:
        site_settings.reset(conn)
        return RedirectResponse(
            url_with_query("/settings", msg="已清除本页保存的站点信息，回到 .env / 默认值"),
            status_code=303,
        )

    values = {
        "site_title": site_title,
        "site_subtitle": site_subtitle,
        "site_description": site_description,
        "author": author,
        "base_url": base_url,
        "per_page": per_page,
        "trash_days": trash_days,
    }
    try:
        site_settings.save(conn, values)
    except site_settings.SiteSettingsError as exc:
        # 把用户填的（含非法值）原样回显在表单里，只 flash 报错，不落库
        return _settings_context(
            request,
            conn,
            site_form=values,
            msg=str(exc),
            msg_kind="error",
            status_code=400,
        )
    return RedirectResponse(
        url_with_query("/settings", msg="站点信息已保存并生效"), status_code=303
    )


@settings_router.post("/settings/password")
def save_account_password(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    old_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    reset_password: str | None = Form(None),
):
    """设置页改登录密码：旧密码校验 + 节流。

    成功只写 meta 的 account.password_hash，**不动会话 cookie**（cookie 是
    HMAC 签名的、跟口令无关），所以已登录的设备不会被踢下线。
    """
    if reset_password:
        account.reset(conn)
        return RedirectResponse(
            url_with_query("/settings", msg="已清除本页改的登录密码，回到 .env 里的口令"),
            status_code=303,
        )

    blocked = account.password_throttle.blocked_for()
    if blocked:
        return _settings_context(
            request,
            conn,
            account_open=True,
            msg=f"旧密码错误次数过多，请 {blocked} 秒后再试。",
            msg_kind="error",
            status_code=429,
        )

    if not old_password:
        return _settings_context(
            request, conn, account_open=True, msg="请输入旧密码", msg_kind="error", status_code=400
        )

    if not account.verify(conn, old_password):
        remaining = account.password_throttle.register_failure()
        message = "旧密码不正确。"
        if remaining:
            message = f"旧密码不正确，已暂时锁定，请 {remaining} 秒后再试。"
        return _settings_context(
            request, conn, account_open=True, msg=message, msg_kind="error", status_code=400
        )

    errors: list[str] = []
    if len(new_password) < account.MIN_PASSWORD_LENGTH:
        errors.append(f"新密码至少 {account.MIN_PASSWORD_LENGTH} 位")
    if not hmac.compare_digest(new_password.encode("utf-8"), confirm_password.encode("utf-8")):
        errors.append("两次输入的新密码不一致")
    if not errors and hmac.compare_digest(
        new_password.encode("utf-8"), old_password.encode("utf-8")
    ):
        errors.append("新密码不能和旧密码相同")
    if errors:
        return _settings_context(
            request, conn, account_open=True, msg="；".join(errors), msg_kind="error", status_code=400
        )

    try:
        account.save_password(conn, new_password)
    except account.AccountError as exc:
        return _settings_context(
            request, conn, account_open=True, msg=str(exc), msg_kind="error", status_code=400
        )
    account.password_throttle.reset()
    return RedirectResponse(
        url_with_query(
            "/settings",
            msg="密码已改，下次登录请用新密码；已登录的设备不会被踢下线（会话 cookie 与密码无关）",
        ),
        status_code=303,
    )


@settings_router.post("/settings/ai")
def save_ai_settings(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    base_url: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(""),
    model_summary: str = Form(""),
    model_tags: str = Form(""),
    model_answer: str = Form(""),
    embed_model: str = Form(""),
    timeout: str = Form("45"),
    local_only: str | None = Form(None),
    redact: str | None = Form(None),
    clear_key: str | None = Form(None),
    clear_all: str | None = Form(None),
):
    del request
    keys = [f"{ai.META_PREFIX}{name}" for name in ai.FIELDS]

    if clear_all:
        repo.delete_meta(conn, keys)
        ai.bootstrap(conn)
        return RedirectResponse(
            url_with_query("/settings", msg="已清除页面里保存的 AI 配置（回到 .env 或未配置状态）"),
            status_code=303,
        )

    # 密钥留空表示「不改」，勾了「清除密钥」就真的清掉
    if clear_key:
        saved_key = ""
    else:
        saved_key = api_key.strip() or (ai.current().get("api_key") or "")

    values = {
        "base_url": base_url.strip(),
        "model": model.strip(),
        "model_summary": model_summary.strip(),
        "model_tags": model_tags.strip(),
        "model_answer": model_answer.strip(),
        "embed_model": embed_model.strip(),
        "timeout": timeout.strip() or "45",
        "api_key": saved_key,
        # 复选框没勾时浏览器不发送字段，所以统一在这里转成 "1" / ""
        "local_only": "1" if local_only else "",
        "redact": "1" if redact else "",
    }

    # 「只允许本机模型」开启时，不允许把地址配到公网。
    # 借用 ai 模块自己那条判定规则（临时套上新配置检查，失败就还原）。
    if values["local_only"] and values["base_url"]:
        ai.configure({**ai.current(), **values})
        try:
            ai._assert_local_only(values["base_url"])
        except ai.AIError as exc:
            ai.bootstrap(conn)  # 还原运行期配置，别把坏配置留在内存里
            return RedirectResponse(
                url_with_query("/settings", msg=str(exc), kind="error"), status_code=303
            )

    repo.save_meta_map(conn, values, ai.META_PREFIX)
    ai.bootstrap(conn)
    if ai.is_enabled():
        return RedirectResponse(url_with_query("/settings", msg="AI 配置已保存并生效"), status_code=303)
    return RedirectResponse(
        url_with_query("/settings", msg="已保存，但还缺 Base URL 或模型名，AI 尚未启用", kind="warn"),
        status_code=303,
    )


@settings_router.post("/settings/ai/prompts")
def save_ai_prompts(
    request: Request,
    conn: sqlite3.Connection = Depends(db_conn),
    summary: str = Form(""),
    tags: str = Form(""),
    answer: str = Form(""),
    reset: str | None = Form(None),
):
    """保存 / 恢复三套提示词模板；非法模板 flash 报错并原样回显，不写库。"""
    if reset:
        ai.reset_prompts(conn)
        return RedirectResponse(
            url_with_query("/settings", msg="提示词已恢复默认"), status_code=303
        )

    values = {"summary": summary, "tags": tags, "answer": answer}
    try:
        ai.save_prompts(conn, values)
    except ai.AIError as exc:
        return _settings_context(
            request,
            conn,
            prompt_form=values,
            prompts_open=True,
            msg=str(exc),
            msg_kind="error",
            status_code=400,
        )
    return RedirectResponse(
        url_with_query("/settings", msg="提示词已保存并生效"), status_code=303
    )


@settings_router.post("/settings/ai/usage/reset")
def reset_ai_usage(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    """清空 AI 用量统计（设置页的「重置用量统计」按钮）。"""
    del request
    removed = ai_usage.reset(conn)
    return RedirectResponse(
        url_with_query("/settings", msg=f"已重置用量统计（清掉 {removed} 条记录）"), status_code=303
    )


@settings_router.post("/settings/ai/test")
def test_ai_settings(
    request: Request,
    base_url: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(""),
    timeout: str = Form("45"),
):
    """用表单里填的值试发一次请求，不保存配置。"""
    del request
    candidate = {
        "base_url": base_url.strip(),
        "model": model.strip(),
        "timeout": timeout.strip() or "45",
        "api_key": api_key.strip() or (ai.current().get("api_key") or ""),
    }
    if not (candidate["base_url"] and candidate["model"]):
        return RedirectResponse(
            url_with_query("/settings", msg="先把 Base URL 和模型名填上再测试", kind="warn"),
            status_code=303,
        )
    result = ai.probe(candidate)
    if result.get("ok"):
        return RedirectResponse(
            url_with_query("/settings", msg=f"连接成功！模型回复：{result.get('reply') or '（空）'}"),
            status_code=303,
        )
    failed = "；".join(
        f"{step['name']}：{step['detail']}"
        for step in result.get("steps", [])
        if step["status"] == "fail"
    )
    return RedirectResponse(
        url_with_query("/settings", msg=f"连接失败 —— {failed or '未知原因'}", kind="error"),
        status_code=303,
    )


# ---------------------------------------------------------------------------
# 给设置页前端用的 JSON 接口
# ---------------------------------------------------------------------------
@ai_api_router.post("/ai/probe")
async def ai_probe(request: Request):
    """分步体检：连不上 / 密钥错 / 模型不存在，分别报出来。"""
    payload = await read_json(request)
    candidate = {
        "base_url": str(payload.get("base_url") or "").strip(),
        "model": str(payload.get("model") or "").strip(),
        "timeout": str(payload.get("timeout") or "45"),
        "api_key": str(payload.get("api_key") or "").strip() or (ai.current().get("api_key") or ""),
        # 页面上还没保存的勾选状态也能生效（没传就沿用已保存的）
        "local_only": payload.get("local_only", ai.current().get("local_only")),
    }
    if not (candidate["base_url"] and candidate["model"]):
        return _json({"ok": False, "error": "先填 Base URL 和模型名", "steps": []}, 400)
    return _json(ai.probe(candidate))


@ai_api_router.get("/ai/models")
def ai_models(base_url: str = "", use_saved: int = 1):
    """拉取服务商支持的模型列表，给设置页的模型名做成下拉选择。"""
    conf = dict(ai.current()) if use_saved else {}
    base = base_url.strip() or str(conf.get("base_url") or "")
    if not base:
        return _json({"ok": False, "models": [], "error": "先填 Base URL"}, 400)
    try:
        models = ai.list_models(config={**ai.current(), "base_url": base})
    except ai.AIError as exc:
        return _json({"ok": False, "models": [], "error": str(exc)}, 400)
    return _json({"ok": True, "models": models})


# ---------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------
@ai_api_router.post("/ai/summarize")
async def ai_summarize(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    payload = await read_json(request)
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content") or "").strip()
    note_id = payload.get("id")
    if not content and isinstance(note_id, int):
        note = repo.get_note(conn, note_id)
        if note:
            content, title = note["content"], note["title"]
    if not content:
        return _json({"ok": False, "error": "正文还是空的，先写点内容吧"}, 400)
    if not ai.is_enabled():
        return _json({"ok": False, "error": "尚未配置 AI 服务（INKNOTE_AI_BASE_URL / INKNOTE_AI_MODEL）"}, 503)
    try:
        summary = ai.summarize(title, content, conn=conn)
    except ai.AIError as exc:
        return _json({"ok": False, "error": str(exc)}, 502)
    return _json({"ok": True, "summary": summary})


@ai_api_router.post("/ai/tags")
async def ai_tags(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
    payload = await read_json(request)
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content") or "").strip()
    if not content:
        return _json({"ok": False, "error": "正文还是空的，先写点内容吧"}, 400)
    if not ai.is_enabled():
        return _json({"ok": False, "error": "尚未配置 AI 服务（INKNOTE_AI_BASE_URL / INKNOTE_AI_MODEL）"}, 503)
    existing = [item["name"] for item in repo.list_tags(conn, limit=60)]
    try:
        tags = ai.suggest_tags(title, content, existing, conn=conn)
    except ai.AIError as exc:
        return _json({"ok": False, "error": str(exc)}, 502)
    return _json({"ok": True, "tags": tags})
