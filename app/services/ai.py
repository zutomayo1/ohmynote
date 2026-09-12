"""可选 AI 能力：自动摘要、推荐标签、基于笔记的问答（RAG）。

配置有两个来源，优先取「设置页」里保存的，其次取 .env：

    设置页保存（写进数据库 meta 表）  →  .env / 环境变量  →  未配置

对接任何 **OpenAI 兼容** 的服务：/chat/completions（对话）、/embeddings（向量）、
/models（列模型）。OpenAI、DeepSeek、通义、Moonshot、本地 Ollama / vLLM 都行。
未配置时 is_enabled() 为 False，界面只提示不报错。

对外接口（其它模块只应该用这些）：
    bootstrap/configure/current/describe/save      配置的读写
    is_enabled/models_for/credentials              取生效配置
    endpoint/api_root/list_models                  拼地址、列模型
    chat/summarize/suggest_tags/answer             实际调用
    prompt_template/describe_prompts/save_prompts/reset_prompts  提示词模板
    probe/humanize_error/redact_text               诊断、错误翻译、脱敏
    PROVIDERS                                      服务商预设（设置页的一键填充）
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from ..config import settings
from ..utils import as_bool

logger = logging.getLogger("inknote.ai")

MAX_CONTEXT_CHARS = 6000
MAX_TOTAL_CONTEXT_CHARS = 9000

META_PREFIX = "ai."
# 布尔型字段（保存时页面传 "1"/"" 也要能正确处理）
BOOL_FIELDS = ("local_only", "redact")
# 全部可配置字段
FIELDS = (
    "base_url",
    "api_key",
    "model",
    "model_summary",
    "model_tags",
    "model_answer",
    "embed_model",
    "timeout",
    "local_only",
    "redact",
)
DEFAULTS: dict = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "model_summary": "",
    "model_tags": "",
    "model_answer": "",
    "embed_model": "",
    "timeout": 45,
    "local_only": False,
    "redact": False,
}

# 任务 → 用哪个模型字段（留空则回退到 model）
TASK_MODEL_FIELD = {
    "summary": "model_summary",
    "tags": "model_tags",
    "answer": "model_answer",
    "embed": "embed_model",
}

# ---------------------------------------------------------------------------
# 提示词模板（存在 meta 的 ai.prompt.* 下，设置页可改）
# ---------------------------------------------------------------------------
PROMPT_TASKS = ("summary", "tags", "title", "category", "answer")
PROMPT_LABELS = {
    "summary": "摘要",
    "tags": "打标签",
    "title": "起标题",
    "category": "推荐分类",
    "answer": "问答",
}
PROMPT_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "summary": ("title", "content"),
    "tags": ("title", "content", "existing"),
    "title": ("title", "content"),
    "category": ("title", "content", "existing"),
    "answer": ("question", "contexts"),
}
PROMPT_MAX_CHARS = 4000
PROMPT_PREFIX = f"{META_PREFIX}prompt."
# 默认只内置「用户消息」这一段；system 指令见下面的 SYSTEM_* 常量。
DEFAULT_PROMPTS = {
    "summary": "笔记标题：{title}\n\n笔记正文：\n{content}",
    "tags": "笔记标题：{title}\n\n笔记正文：\n{content}{existing}",
    "title": "笔记标题：{title}\n\n笔记正文：\n{content}",
    "category": "笔记标题：{title}\n\n笔记正文：\n{content}{existing}",
    "answer": "【资料】\n\n{contexts}\n\n【问题】\n{question}\n\n请依据以上资料作答。",
}
# 运行期缓存：只放「库里有保存」的模板，空表示用内置默认
_prompts: dict[str, str] = {}


# 设置页「一键填充」用的预设
PROVIDERS: list[dict] = [
    {
        "key": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "embed_model": "",
        "key_url": "https://platform.deepseek.com/api_keys",
        "note": "国内直连、便宜，适合中文笔记",
    },
    {
        "key": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "embed_model": "text-embedding-3-small",
        "key_url": "https://platform.openai.com/api-keys",
        "note": "有 embedding，向量检索效果最好",
    },
    {
        "key": "dashscope",
        "name": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "embed_model": "text-embedding-v3",
        "key_url": "https://bailian.console.aliyun.com/",
        "note": "阿里云百炼，中文友好",
    },
    {
        "key": "ollama",
        "name": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "embed_model": "nomic-embed-text",
        "key_url": "",
        "note": "完全本地，不用密钥、不联网",
    },
]


class AIError(RuntimeError):
    """AI 调用失败（网络、鉴权、返回格式等），message 直接展示给用户。"""


class AIUnsupported(AIError):
    """服务商不提供这个接口（比如自建网关没有 /models）——不算配置错误。"""


# 运行期真正生效的配置：启动时由 bootstrap() 填好，设置页保存后再填一次
_config: dict = dict(DEFAULTS)
_sources: dict = {}  # 每个字段的来源：db（设置页）/ env / ""


def _env_values() -> dict:
    return {
        "base_url": settings.ai_base_url,
        "api_key": settings.ai_api_key,
        "model": settings.ai_model,
        "embed_model": settings.ai_embed_model,
        "timeout": settings.ai_timeout,
    }


def _as_bool(value) -> bool:
    """唯一口径在 app.utils.as_bool（配置里的 "1"/"true"/"on" 等写法统一走它）。"""
    return as_bool(value)


def _normalise(values: dict) -> dict:
    out = dict(DEFAULTS)
    for key in FIELDS:
        value = values.get(key)
        if value not in (None, ""):
            out[key] = value
    try:
        out["timeout"] = max(5, min(int(out["timeout"]), 600))
    except (TypeError, ValueError):
        out["timeout"] = DEFAULTS["timeout"]
    out["base_url"] = api_root(str(out["base_url"]))
    for key in ("api_key", "model", "model_summary", "model_tags", "model_answer", "embed_model"):
        out[key] = str(out[key]).strip()
    for key in BOOL_FIELDS:
        out[key] = _as_bool(out[key])
    return out


def bootstrap(conn: sqlite3.Connection | None = None) -> dict:
    """启动时调用：先取 .env，再用「设置页」保存的值覆盖。"""
    global _prompts
    global _config, _sources
    stored: dict = {}
    if conn is not None:
        from .. import repo

        stored = {
            key: value
            for key, value in repo.get_meta_map(conn, META_PREFIX).items()
            if value not in (None, "")
        }

    env = _env_values()
    merged = dict(env)
    sources: dict = {}
    for key in FIELDS:
        if stored.get(key) not in (None, ""):
            merged[key] = stored[key]
            sources[key] = "db"
        elif env.get(key) not in (None, "", 0, False):
            sources[key] = "env"
        else:
            sources[key] = ""
    _config = _normalise(merged)
    _prompts = {
        task: stored[f"prompt.{task}"]
        for task in PROMPT_TASKS
        if stored.get(f"prompt.{task}")
    }
    _sources = sources
    return current()


def save(conn: sqlite3.Connection, values: dict) -> dict:
    """把设置页提交的字段写进数据库，并立即生效。"""
    from .. import repo

    payload = {key: _config_value(values.get(key)) for key in FIELDS if key in values}
    repo.save_meta_map(conn, payload, META_PREFIX)
    return bootstrap(conn)


def _config_value(value) -> str:
    if isinstance(value, bool):
        return "1" if value else ""
    return "" if value is None else str(value).strip()


def configure(values: dict, sources: dict | None = None) -> dict:
    """直接设置运行期配置（「测试连接」用它试临时值，不落库）。"""
    global _config, _sources
    _config = _normalise(values)
    _sources = sources if sources is not None else dict(_sources)
    return current()


def current() -> dict:
    return dict(_config)


def is_enabled() -> bool:
    """只要 base_url 和 model 都有值就算启用（本地 Ollama 可能不需要 key）。"""
    return bool(_config.get("base_url") and _config.get("model"))


def describe() -> dict:
    """给设置页用：当前值、每个字段的来源、密钥掩码、.env 是否也配了。"""
    env = _env_values()
    key = str(_config.get("api_key") or "")
    env_keys = [
        f"INKNOTE_AI_{name.upper()}"
        for name, value in env.items()
        if value not in (None, "", 0)
    ]
    return {
        "base_url": _config.get("base_url") or "",
        "model": _config.get("model") or "",
        "model_summary": _config.get("model_summary") or "",
        "model_tags": _config.get("model_tags") or "",
        "model_answer": _config.get("model_answer") or "",
        "embed_model": _config.get("embed_model") or "",
        "timeout": _config.get("timeout") or DEFAULTS["timeout"],
        "local_only": bool(_config.get("local_only")),
        "redact": bool(_config.get("redact")),
        "has_key": bool(key),
        "key_mask": (f"{key[:3]}……{key[-4:]}" if len(key) > 12 else ("已设置" if key else "")),
        "sources": dict(_sources),
        "enabled": is_enabled(),
        "env_keys": env_keys,
        "providers": [dict(item) for item in PROVIDERS],
        "tasks": {
            "summary": models_for("summary"),
            "tags": models_for("tags"),
            "answer": models_for("answer"),
        },
    }


# ---------------------------------------------------------------------------
# 地址与模型
# ---------------------------------------------------------------------------
def api_root(base_url: str) -> str:
    """把用户填的地址统一成 API 根（去掉结尾斜杠和可能多填的 /chat/completions）。"""
    base = (base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/embeddings", "/models"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    return base


def endpoint(base_url: str, path: str) -> str:
    """拼出完整接口地址：endpoint(base, "chat/completions")。"""
    return f"{api_root(base_url)}/{path.lstrip('/')}"


def models_for(task: str) -> str:
    """某个任务实际用哪个模型：优先专用字段，否则回退到通用 model。"""
    field = TASK_MODEL_FIELD.get(task)
    if field and _config.get(field):
        return str(_config[field])
    return str(_config.get("model") or "")


def credentials() -> dict:
    """给其它模块（比如向量检索）取连接信息用，避免它们碰私有状态。"""
    return {
        "base_url": _config.get("base_url") or "",
        "api_key": _config.get("api_key") or "",
        "timeout": _config.get("timeout") or DEFAULTS["timeout"],
        "embed_model": models_for("embed"),
    }


def _missing_prompt_placeholders(task: str, template: str) -> list[str]:
    """返回模板里缺失的必需占位符（不带大括号）。"""
    return [
        name
        for name in PROMPT_PLACEHOLDERS.get(task, ())
        if f"{{{name}}}" not in (template or "")
    ]


def _stored_prompt(task: str) -> str | None:
    """库里存的、校验通过的模板；没有 / 非法都返回 None（调用方回退默认）。"""
    template = _prompts.get(task)
    if not template:
        return None
    if len(template) > PROMPT_MAX_CHARS:
        logger.warning("提示词模板 %s 超过 %d 字符，已忽略并回退默认", task, PROMPT_MAX_CHARS)
        return None
    missing = _missing_prompt_placeholders(task, template)
    if missing:
        logger.warning(
            "提示词模板 %s 缺少占位符 %s，已回退默认",
            task,
            "、".join(f"{{{name}}}" for name in missing),
        )
        return None
    return str(template)


def prompt_template(task: str) -> str:
    """当前生效的模板：库里有合法自定义就用它，否则用内置默认。"""
    if task not in PROMPT_TASKS:
        raise AIError(f"未知的提示词任务：{task}")
    return _stored_prompt(task) or DEFAULT_PROMPTS[task]


def describe_prompts() -> dict:
    """给设置页用：每套模板的当前值、是否自定义、需要的占位符。"""
    info: dict = {}
    for task in PROMPT_TASKS:
        info[task] = {
            "label": PROMPT_LABELS.get(task, task),
            "value": prompt_template(task),
            "default": DEFAULT_PROMPTS[task],
            "custom": _stored_prompt(task) is not None,
            "placeholders": list(PROMPT_PLACEHOLDERS.get(task, ())),
            "max_chars": PROMPT_MAX_CHARS,
        }
    return info


def save_prompts(conn: sqlite3.Connection, values: dict) -> dict:
    """保存非空的自定义模板；缺占位符 / 超长直接拒绝（抛 AIError），不写库。"""
    from .. import repo

    payload: dict[str, str] = {}
    for task, raw in (values or {}).items():
        if task not in PROMPT_TASKS:
            raise AIError(f"未知的提示词任务：{task}")
        text = "" if raw is None else str(raw)
        if not text.strip():
            continue  # 空 = 不修改，留给「恢复默认」处理
        if len(text) > PROMPT_MAX_CHARS:
            raise AIError(
                f"「{PROMPT_LABELS.get(task, task)}」模板太长（{len(text)} 字符），"
                f"最多 {PROMPT_MAX_CHARS} 字符"
            )
        missing = _missing_prompt_placeholders(task, text)
        if missing:
            raise AIError(
                f"「{PROMPT_LABELS.get(task, task)}」模板缺少占位符："
                + "、".join(f"{{{name}}}" for name in missing)
            )
        payload[task] = text

    if payload:
        repo.save_meta_map(conn, payload, PROMPT_PREFIX)
        _prompts.update(payload)
    return dict(_prompts)


def reset_prompts(conn: sqlite3.Connection) -> None:
    """清除所有自定义模板，回到内置默认。"""
    from .. import repo

    repo.delete_meta(conn, [f"{PROMPT_PREFIX}{task}" for task in PROMPT_TASKS])
    _prompts.clear()


def _format_prompt(task: str, **values) -> str:
    """按模板格式化；用户模板写坏（多余占位符 / 括号不配对）时回退默认模板。"""
    try:
        return prompt_template(task).format(**values)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        logger.warning("提示词模板 %s 格式化失败，已回退默认模板", task, exc_info=True)
        return DEFAULT_PROMPTS[task].format(**values)


def _headers(conf: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if conf.get("api_key"):
        headers["Authorization"] = f"Bearer {conf['api_key']}"
    return headers


def _assert_local_only(base_url: str, local_only: bool | None = None) -> None:
    """开了「只允许本机模型」时，挡住指向公网的地址，避免笔记被误发出去。

    local_only=None 表示用当前生效配置里的开关；传 True/False 可以临时覆盖
    （「测试连接」需要按页面上还没保存的勾选状态来判断）。
    """
    flag = bool(_config.get("local_only")) if local_only is None else bool(local_only)
    if not flag or not base_url:
        return
    host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    local = (
        host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
        or host.startswith("192.168.")
        or host.startswith("10.")
        or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host or "")
    )
    if not local:
        raise AIError(
            f"设置里勾了「只允许本机模型」，但当前地址是外部的（{host}）。"
            "要么改成 127.0.0.1 的本地模型，要么去设置页取消这个开关。"
        )


def assert_base_url_allowed(base_url: str, local_only: bool | None = None) -> None:
    """公开版本：给路由层做保存前校验用（比如设置页）。"""
    _assert_local_only(base_url, local_only)


def is_local_address(base_url: str) -> bool:
    """这个地址是不是本机/局域网（设置页用来提前提示）。"""
    host = (urllib.parse.urlparse(base_url or "").hostname or "").lower()
    if not host:
        return False
    return (
        host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
        or host.startswith("192.168.")
        or host.startswith("10.")
        or bool(re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host))
    )


def list_models(
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: int | None = None,
    *,
    config: dict | None = None,
) -> list[str]:
    """拉取服务商支持的模型列表（OpenAI 兼容的 GET /models）。"""
    conf = _normalise(config) if config else dict(_config)
    conf["base_url"] = api_root(base_url or conf.get("base_url") or "")
    if api_key is not None:
        conf["api_key"] = api_key.strip()
    if timeout is not None:
        conf["timeout"] = max(5, min(int(timeout), 600))
    if not conf["base_url"]:
        raise AIError("先填 Base URL 才能获取模型列表。")
    _assert_local_only(conf["base_url"], conf.get("local_only"))

    request = urllib.request.Request(
        endpoint(conf["base_url"], "models"), headers=_headers(conf), method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=conf["timeout"]) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # pragma: no cover
            pass
        if exc.code in (404, 405, 501):
            raise AIUnsupported(f"这个服务没有提供 /models 接口（HTTP {exc.code}），跳过列表检查") from exc
        raise AIError(humanize_error(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise AIError(f"连不上这个地址：{exc.reason}") from exc
    except TimeoutError as exc:
        raise AIError("获取模型列表超时") from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise AIError("这个地址没返回标准的模型列表（不是 OpenAI 兼容接口？）") from exc
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise AIError("这个地址没返回标准的模型列表")
    names: list[str] = []
    for item in items:
        name = item.get("id") if isinstance(item, dict) else item
        if name:
            names.append(str(name))
    return sorted(set(names))


def humanize_error(status: int, detail: str = "") -> str:
    """把 HTTP 状态码翻译成「人话 + 下一步该干什么」。"""
    hints = {
        400: "请求被拒绝：多半是模型名写错了，或者这个模型不支持这种调用方式",
        401: "密钥无效或没有权限：检查 API Key 是否抄错、是否已过期",
        402: "账户余额不足，需要充值",
        403: "这个密钥没有访问该模型的权限（有些模型要单独开通）",
        404: "地址或模型不存在：确认 Base URL 填到 /v1 为止、模型名拼写正确",
        413: "请求内容太长了，换短一点的笔记再试",
        422: "参数不被服务端接受，通常是模型名不对",
        429: "触发了频率限制或额度用尽：等一会儿再试，或换成更便宜的模型",
        500: "AI 服务端内部错误，稍后重试",
        502: "网关错误：通常是 Base URL 填错了",
        503: "服务暂时不可用，稍后重试",
        504: "网关超时：模型响应太慢",
    }
    hint = hints.get(status, f"AI 服务返回了 HTTP {status}")
    detail = (detail or "").strip().replace("\n", " ")
    if detail:
        return f"{hint}。（服务端原话：{detail[:220]}）"
    return hint


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
REDACT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"), "sk-***"),
    (re.compile(r"\b(?:ghp|gho|glpat|xox[baprs])-[A-Za-z0-9_\-]{10,}\b"), "***-token"),
    (re.compile(r"\b[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{20,}\b"), "***.jwt.***"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)\b\s*[:=]\s*\S+"
        ),
        r"\1=***",
    ),
    (re.compile(r"\b1[3-9]\d{9}\b"), "1**********"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "******************"),
    (re.compile(r"\b\d{16,19}\b"), "**** **** **** ****"),
]


def redact_text(text: str) -> tuple[str, int]:
    """把正文里像密钥/口令/手机号/身份证/卡号的片段打码。返回 (新文本, 命中数)。"""
    if not text:
        return text, 0
    hits = 0
    result = text
    for pattern, replacement in REDACT_RULES:
        result, count = pattern.subn(replacement, result)
        hits += count
    return result, hits


def _endpoint(base_url: str) -> str:
    """兼容旧调用：对话补全地址。"""
    return endpoint(base_url, "chat/completions")


def _chat_direct(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 700,
    config: dict | None = None,
    task: str = "answer",
    conn: sqlite3.Connection | None = None,
) -> str:
    """调用一次对话补全，返回助手文本。

    - config：只在「测试连接」时传临时配置，不落库
    - task：summary / tags / answer，决定用哪个模型（见 models_for）
    - conn：传了就把 token 用量记进数据库（设置页的用量统计）
    """
    conf = _normalise(config) if config else dict(_config)
    model = models_for(task) if config is None else (conf.get("model") or "")
    if not (conf.get("base_url") and model):
        raise AIError(
            "尚未配置 AI 服务：可以在「设置」页里填，或者设置环境变量 "
            "INKNOTE_AI_BASE_URL 与 INKNOTE_AI_MODEL。"
        )
    _assert_local_only(conf["base_url"], conf.get("local_only"))

    if _config.get("redact"):
        cleaned = []
        for message in messages:
            body, _hits = redact_text(str(message.get("content") or ""))
            cleaned.append({**message, "content": body})
        messages = cleaned

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    request = urllib.request.Request(
        endpoint(conf["base_url"], "chat/completions"),
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(conf),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=conf["timeout"]) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # pragma: no cover - 读错误体失败无所谓
            pass
        raise AIError(humanize_error(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise AIError(f"连不上这个地址：{exc.reason}") from exc
    except TimeoutError as exc:
        raise AIError(f"等待超过 {conf['timeout']} 秒还没响应，可能是模型太慢或网络问题") from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise AIError("AI 服务返回的不是合法 JSON") from exc

    if conn is not None:
        try:
            from . import ai_usage

            ai_usage.record(conn, model=model, task=task, usage=data.get("usage") or {})
        except Exception:  # 记用量失败不该影响正常使用
            pass

    choices = data.get("choices") or []
    if not choices:
        raise AIError("AI 服务没有返回任何结果")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # 兼容多段内容
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return (content or "").strip()


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 700,
    config: dict | None = None,
    task: str = "answer",
    conn: sqlite3.Connection | None = None,
) -> str:
    """调用一次对话补全，返回助手文本；主模型失败时自动回退到通用模型。

    签名和返回类型和以前完全一致。只有「没传临时 config」且「分任务模型 ≠
    通用模型」时才有回退：先用 task 专用模型，失败后用 model 再试一次，
    两次都失败抛第一次（主模型）的错误；用量只记成功那次。
    """
    if config is not None:
        # 「测试连接」等临时配置：只有一个模型，不回退
        return _chat_direct(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            config=config,
            task=task,
            conn=conn,
        )

    primary = models_for(task)
    general = str(_config.get("model") or "")
    if not (_config.get("base_url") and primary):
        raise AIError(
            "尚未配置 AI 服务：可以在「设置」页里填，或者设置环境变量 "
            "INKNOTE_AI_BASE_URL 与 INKNOTE_AI_MODEL。"
        )
    _assert_local_only(_config.get("base_url") or "", _config.get("local_only"))
    attempts = [primary]
    if general and general != primary:
        attempts.append(general)

    first_error: AIError | None = None
    for index, used_model in enumerate(attempts):
        attempt_conf = {**dict(_config), "model": used_model}
        try:
            return _chat_direct(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                config=attempt_conf,
                task=task,
                conn=conn,
            )
        except AIError as exc:
            if index == 0:
                first_error = exc
            if index + 1 < len(attempts):
                logger.warning(
                    "模型 %s 调用失败，回退到 %s：%s",
                    used_model,
                    attempts[index + 1],
                    exc,
                    exc_info=True,
                )
                continue
            if first_error is not None and first_error is not exc:
                raise first_error from exc
            raise
    # 正常情况下循环里一定 return 或 raise，这里只是让类型检查器安心
    raise AIError("尚未配置 AI 服务：可以在「设置」页里填，或者设置环境变量 INKNOTE_AI_BASE_URL 与 INKNOTE_AI_MODEL。")


def probe(config: dict) -> dict:
    """分四步体检，返回 {"ok": bool, "steps": [...], "reply": str}。

    设置页的「测试连接」用它，好处是失败时能说清是哪一步断的：
    连不上 / 密钥不对 / 模型不存在 / 能连但对话失败。
    """
    conf = _normalise(config)
    steps: list[dict] = []

    if not conf.get("base_url"):
        return {
            "ok": False,
            "steps": [{"name": "地址", "status": "fail", "detail": "还没有填 Base URL"}],
            "reply": "",
        }

    # 第 1 步：能不能连通 + 密钥是否有效（顺便拿到模型列表）
    models: list[str] = []
    try:
        models = list_models(config=conf)
        steps.append(
            {
                "name": "连通性与密钥",
                "status": "ok",
                "detail": f"成功访问 {endpoint(conf['base_url'], 'models')}，返回 {len(models)} 个模型",
            }
        )
    except AIUnsupported as exc:
        models = []
        steps.append({"name": "连通性与密钥", "status": "skip", "detail": str(exc)})
    except AIError as exc:
        steps.append({"name": "连通性与密钥", "status": "fail", "detail": str(exc)})
        return {"ok": False, "steps": steps, "reply": ""}

    # 第 2 步：模型名是否存在
    if models:
        if conf["model"] in models:
            steps.append({"name": "模型名", "status": "ok", "detail": f"{conf['model']} 在可用列表里"})
        else:
            near = [name for name in models if conf["model"].split(":")[0] in name][:5] or models[:5]
            steps.append(
                {
                    "name": "模型名",
                    "status": "fail",
                    "detail": f"服务商没有 {conf['model']} 这个模型。可用的有：" + "、".join(near),
                }
            )
            return {"ok": False, "steps": steps, "reply": ""}
    else:
        steps.append({"name": "模型名", "status": "skip", "detail": "跳过（拿不到模型列表）"})

    # 第 3 步：真的发一次对话
    try:
        reply = chat(
            [{"role": "user", "content": "请只回复两个字：可用"}],
            config=conf,
            temperature=0,
            max_tokens=16,
        )
    except AIError as exc:
        steps.append({"name": "对话", "status": "fail", "detail": str(exc)})
        return {"ok": False, "steps": steps, "reply": ""}

    steps.append(
        {
            "name": "对话",
            "status": "ok",
            "detail": "模型正常返回了内容，配置可用",
        }
    )
    return {"ok": True, "steps": steps, "reply": (reply or "").strip()[:40] or "（内容为空，但连接正常）"}


def _trim(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…（内容过长已截断）"


SYSTEM_SUMMARY = (
    "你是一个中文笔记助手。请为用户给出的笔记写一句话摘要，要求："
    "1) 不超过 60 个汉字；2) 直接概括这篇笔记讲了什么，不要用「这篇笔记」开头；"
    "3) 只输出摘要本身，不要引号、不要 Markdown、不要任何解释。"
)

SYSTEM_TAGS = (
    "你是一个中文笔记助手。请根据笔记内容推荐 3-5 个标签，要求："
    "1) 每个标签 2-6 个字，不要带 # 号；2) 优先复用用户已有标签；"
    "3) 只输出一个 JSON 数组，例如 [\"Python\",\"异步编程\"]，不要任何解释文字。"
)

SYSTEM_TITLE = (
    "你是一个中文笔记助手。请为用户给出的笔记起一个标题，要求："
    "1) 8-20 个字，概括主题、说人话，不要空洞的「关于…的思考」这种套话；"
    "2) 不要带书名号、引号、Markdown，也不要带 # 号；"
    "3) 只输出标题本身，不要任何解释。若已有标题已经足够好，就沿用或只做微调。"
)

SYSTEM_CATEGORY = (
    "你是一个中文笔记助手。请为笔记选一个分类，要求："
    "1) 只输出一个分类名，2-6 个字，不要带符号；"
    "2) 优先从用户已有分类里挑；确实都不合适才新拟一个；"
    "3) 只输出分类名本身，不要任何解释。"
)

SYSTEM_QA = (
    "你是一个只依据给定资料回答问题的中文助手。要求："
    "1) 只能使用【资料】里的内容，不允许编造；"
    "2) 如果资料不足以回答，就明确说「笔记里没有找到相关内容」并指出还需要什么信息；"
    "3) 引用资料时用 [1]、[2] 这样的编号标注来源；"
    "4) 用 Markdown 组织答案，条理清晰，中文作答。"
)


def summarize(title: str, content: str, *, conn: sqlite3.Connection | None = None) -> str:
    prompt = _format_prompt("summary", title=title or "（无标题）", content=_trim(content))
    result = chat(
        [{"role": "system", "content": SYSTEM_SUMMARY}, {"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=200,
        task="summary",
        conn=conn,
    )
    return result.strip().strip('"“”').split("\n")[0][:120]


def suggest_tags(
    title: str,
    content: str,
    existing: list[str] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    hint = ""
    if existing:
        hint = f"\n\n用户已有的标签（可优先复用）：{'、'.join(existing[:40])}"
    prompt = _format_prompt(
        "tags",
        title=title or "（无标题）",
        content=_trim(content),
        existing=hint,
    )
    result = chat(
        [{"role": "system", "content": SYSTEM_TAGS}, {"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=200,
        task="tags",
        conn=conn,
    )
    return _parse_tag_list(result)


def _clean_inline(raw: str, *, limit: int = 40) -> str:
    """把「一句话结果」洗干净：去代码围栏 / 引号 / 井号 / Markdown 标记，只留第一行。

    模型很爱回「标题：xxx」或者给标题套上引号，直接用会写进输入框里很难看。
    """
    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    text = text.split("\n")[0].strip()
    text = re.sub(r"^(标题|题目|分类|类别)\s*[:：]\s*", "", text)
    return text.strip().strip("\"'“”‘’《》#*`-— 　").strip()[:limit]


def suggest_title(
    title: str, content: str, *, conn: sqlite3.Connection | None = None
) -> str:
    """给一篇笔记起标题（也有标题时按「微调」处理）。"""
    prompt = _format_prompt("title", title=title or "（无标题）", content=_trim(content))
    result = chat(
        [{"role": "system", "content": SYSTEM_TITLE}, {"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=80,
        task="title",
        conn=conn,
    )
    return _clean_inline(result, limit=60)


def suggest_category(
    title: str,
    content: str,
    existing: list[str] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> str:
    """推荐一个分类；`existing` 是用户已有分类，会提示模型优先复用。"""
    hint = ""
    if existing:
        hint = f"\n\n用户已有的分类（优先从中挑）：{'、'.join(existing[:30])}"
    prompt = _format_prompt(
        "category", title=title or "（无标题）", content=_trim(content), existing=hint
    )
    result = chat(
        [{"role": "system", "content": SYSTEM_CATEGORY}, {"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=40,
        task="category",
        conn=conn,
    )
    return _clean_inline(result, limit=12)


def _parse_tag_list(raw: str) -> list[str]:
    """从模型输出里稳健地抠出标签数组。"""
    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    candidates: list[str] = []
    parsed_json_list = False
    match = re.search(r"\[(.*?)\]", text, re.S)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                candidates = [str(item) for item in parsed]
                # 关键：JSON 数组解析成功（哪怕是空数组）就别再退回「按标点切原文」，
                # 否则模型回 [] 时会切出一个名叫 "[]" 的垃圾标签（真踩过）。
                parsed_json_list = True
        except ValueError:
            candidates = re.split(r"[,，、\n]", match.group(1))
    if not candidates and not parsed_json_list:
        # 兜底：模型可能直接给「缓存、Redis」这种纯文本
        fallback = text.strip().strip("[]（）()")
        candidates = re.split(r"[,，、\n]", fallback)
    tags: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        name = str(item).strip().strip("'\"#-•[]（）()").strip()
        # 只剩标点/空白的不算标签
        if not name or not re.search(r"[\w\u4e00-\u9fff]", name):
            continue
        if len(name) > 20 or name.lower() in seen:
            continue
        seen.add(name.lower())
        tags.append(name)
        if len(tags) >= 5:
            break
    return tags


def build_qa_messages(question: str, contexts: list[dict], history: list[dict] | None = None) -> list[dict]:
    """拼出问答用的 messages（多轮时把历史插在中间）。给 ask 页面和流式输出共用。"""
    blocks: list[str] = []
    used = 0
    for index, item in enumerate(contexts or [], start=1):
        body = _trim(item.get("content", ""))
        if used + len(body) > MAX_TOTAL_CONTEXT_CHARS:
            body = body[: max(0, MAX_TOTAL_CONTEXT_CHARS - used)]
        if not body:
            break
        used += len(body)
        blocks.append(f"【资料 {index}】《{item.get('title') or '无标题'}》\n{body}")
    prompt = _format_prompt(
        "answer", contexts="\n\n".join(blocks), question=question.strip()
    )
    messages: list[dict] = [{"role": "system", "content": SYSTEM_QA}]
    for turn in (history or [])[-6:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        content = str(turn.get("content") or "").strip()
        if content:
            messages.append({"role": role, "content": content[:4000]})
    messages.append({"role": "user", "content": prompt})
    return messages


def answer(
    question: str,
    contexts: list[dict],
    *,
    history: list[dict] | None = None,
    conn: sqlite3.Connection | None = None,
    config: dict | None = None,
) -> str:
    """基于检索到的笔记回答问题。contexts: [{title, content, url}]"""
    if not contexts:
        return "没有在你的笔记里检索到相关内容，换个关键词试试？"
    return chat(
        build_qa_messages(question, contexts, history),
        temperature=0.2,
        max_tokens=1200,
        task="answer",
        conn=conn,
        config=config,
    )
