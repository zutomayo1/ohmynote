"""Markdown -> HTML 渲染管线。

一次渲染同时产出：HTML、目录（TOC）、纯文本、字数、阅读时长、摘要、双链信息。
安全策略：**先转义原始 HTML 再交给 Markdown**，因此不支持内联 HTML，
<script> 之类的标签只会以纯文本出现；链接与图片地址再做一次协议白名单过滤。
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from typing import Callable

import markdown as markdown_lib

from .utils import CJK_RE

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
EXTENSIONS = [
    "fenced_code",
    "codehilite",
    "tables",
    "toc",
    "sane_lists",
    "attr_list",
    "def_list",
    "footnotes",
    "smarty",
    # pymdown-extensions 提供 GitHub 风味语法：任务清单、删除线、高亮
    "pymdownx.tasklist",
    "pymdownx.tilde",
    "pymdownx.mark",
]

EXTENSION_CONFIGS = {
    "codehilite": {
        "guess_lang": False,
        "css_class": "codehilite",
        "linenums": False,
        "noclasses": False,
        "pygments_style": "default",
    },
    "toc": {"toc_depth": "1-4", "permalink": False, "anchorlink": False},
    "footnotes": {"UNIQUE_IDS": True},
    "smarty": {"smart_dashes": True, "smart_quotes": True, "smart_ellipses": True},
    "pymdownx.tasklist": {"custom_checkbox": True, "clickable_checkbox": False},
    "pymdownx.tilde": {"subscript": False},
}

# 中文按字、英文按词的阅读速度
CJK_PER_MINUTE = 400
LATIN_PER_MINUTE = 220

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_TAG_START_RE = re.compile(r"<(?=[A-Za-z/!?])")
_WIKI_RE = re.compile(r"\[\[([^\[\]]{1,200})\]\]")
_WIKI_TOKEN_RE = re.compile(r"\{\{wl:(\d+)\}\}")
_ATTR_URL_RE = re.compile(r"(?P<attr>href|src)\s*=\s*\"(?P<url>[^\"]*)\"", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\s(?P<prefix>[^>]*?)href=\"(?P<url>https?://[^\"]+)\"(?P<suffix>[^>]*)>", re.IGNORECASE)
_IMG_RE = re.compile(r"<img\s", re.IGNORECASE)
_CJK_ONLY = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+")
_BAD_SCHEMES = ("javascript:", "vbscript:", "data:text/html", "file:")


@dataclass
class WikiRef:
    """正文里的一处 [[标题]] 引用（title 是目标，alias 是显示文字）。"""

    title: str
    alias: str
    note_id: int | None = None
    url: str = ""
    exists: bool = False


@dataclass
class Rendered:
    html: str = ""
    toc: list[dict] = field(default_factory=list)
    plain: str = ""
    excerpt: str = ""
    word_count: int = 0
    reading_minutes: int = 0
    wikilinks: list[WikiRef] = field(default_factory=list)
    # 页面里是否出现公式 / mermaid 图，模板据此决定要不要加载渲染脚本
    has_math: bool = False
    has_mermaid: bool = False


_MERMAID_TOKEN_RE = re.compile(r"\{\{mm:(\d+)\}\}")


# ---------------------------------------------------------------------------
# 代码块保护：只在「非代码」片段上做替换
# ---------------------------------------------------------------------------
# 任务清单标记：列表项开头的 `- [ ]` / `1. [x]`（代码块内不算）
_TASK_MARK_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\])", re.M)


def _mark_task_checkboxes(html: str) -> str:
    """任务清单复选框：去掉 disabled、按出现顺序编号（data-task-index，0 起）。

    disabled 的表单控件会吞掉所有鼠标事件，点击根本不派发，所以这里
    必须放开、由前端 JS 接管点击（详情页回写、其它页面 preventDefault）。
    JS 挂掉时复选框可以被原生点动但不会保存（刷新还原），无害降级。
    """
    counter = [0]

    def repl(m: re.Match) -> str:
        counter[0] += 1
        return f'<input type="checkbox" data-task-index="{counter[0] - 1}"'

    # 勾选与未勾选的都要接管（checked 的写法是 disabled checked/>）
    return re.sub(r'<input type="checkbox" disabled(?=[ /])', repl, html)


def extract_tasks(content: str) -> list[dict]:
    """抽取正文里的任务清单项（代码块外），index 与 toggle_task_item 完全同口径。

    返回 [{"index": 0 起的序号, "done": bool, "text": 任务文字}]。
    """
    state = {"index": -1}
    items: list[dict] = []

    def process(chunk: str) -> str:
        for line in chunk.split("\n"):
            match = _TASK_MARK_RE.match(line)
            if not match:
                continue
            state["index"] += 1
            items.append(
                {
                    "index": state["index"],
                    "done": match.group(2) in ("x", "X"),
                    "text": line[match.end():].strip(),
                }
            )
        return chunk

    map_outside_code(content, process)
    return items


def toggle_task_item(content: str, index: int) -> tuple[str, bool] | None:
    """把正文里第 index 个（0 起）任务标记切换勾选态。

    只处理代码块外的标记（与渲染器计数口径一致）；越界返回 None。
    返回 (新正文, 切换后的勾选态)。
    """
    state = {"seen": -1, "checked": None}

    def process(chunk: str) -> str:
        def repl(m: re.Match) -> str:
            state["seen"] += 1
            if state["seen"] != index:
                return m.group(0)
            mark = m.group(2)
            new_mark = " " if mark in ("x", "X") else "x"
            state["checked"] = new_mark == "x"
            return m.group(1) + new_mark + m.group(3)

        return _TASK_MARK_RE.sub(repl, chunk)

    new_content = map_outside_code(content, process)
    if state["checked"] is None:
        return None
    return new_content, state["checked"]


def map_outside_code(text: str, fn: Callable[[str], str]) -> str:
    """对文本里 **不在** 围栏代码块与行内代码中的片段调用 fn。"""
    if not text:
        return ""
    lines = text.split("\n")
    output: list[str] = []
    fence: str | None = None
    for line in lines:
        match = _FENCE_RE.match(line)
        if fence is None and match:
            fence = match.group(1)[0] * 3
            output.append(line)
            continue
        if fence is not None:
            output.append(line)
            if match and match.group(1)[0] * 3 == fence and not match.group(2).strip():
                fence = None
            continue
        output.append(_map_inline_segments(line, fn))
    return "\n".join(output)


def _map_inline_segments(line: str, fn: Callable[[str], str]) -> str:
    """按 CommonMark 规则识别行内代码，只在「真正配对」的代码段上跳过处理。

    只有长度完全相同的反引号段才能闭合（` 与 ` 配对、`` 与 `` 配对）。
    没有闭合的反引号只是普通字符 —— 它后面的内容必须照常转义，
    否则一行里塞一个反引号就能让裸 `<script>` 逃过转义（这是真实修过的漏洞）。
    """
    if "`" not in line:
        return fn(line)

    parts: list[str] = []
    position = 0
    length = len(line)
    while position < length:
        start = line.find("`", position)
        if start < 0:
            parts.append(fn(line[position:]))
            break
        end = start
        while end < length and line[end] == "`":
            end += 1
        run = end - start
        close = _find_backtick_run(line, end, run)
        if close < 0:
            # 没有等长的闭合段：从这里到行尾都按普通文本处理
            parts.append(fn(line[position:]))
            break
        parts.append(fn(line[position:start]))     # 代码段之前
        parts.append(line[start : close + run])     # 代码段原样保留，交给 Markdown 自己转义
        position = close + run
    return "".join(parts)


def _find_backtick_run(text: str, start: int, size: int) -> int:
    """找出下一个长度恰好为 size 的反引号段的起始下标，找不到返回 -1。"""
    index = start
    length = len(text)
    while index < length:
        found = text.find("`", index)
        if found < 0:
            return -1
        end = found
        while end < length and text[end] == "`":
            end += 1
        if end - found == size:
            return found
        index = end
    return -1


def escape_raw_html(text: str) -> str:
    """把所有看起来像标签的 `<` 转义掉，借此禁用 Markdown 的内联 HTML。

    只处理 `<` 后面紧跟字母 / `/` / `!` / `?` 的情况，所以 `1 < 2`、
    引用块 `> 引用` 这些写法不受影响。
    """
    return _TAG_START_RE.sub("&lt;", text or "")


# `#标签`、`##标题` 这类「井号后面没有空格」的写法，按 CommonMark 当普通文本处理，
# 否则一整行 `#标签` 会被当成 H1，正文里的 #标签 就废了。
_ATX_NO_SPACE_RE = re.compile(r"^(#{1,6})(?=[^\s#])")


def normalise_headings(text: str) -> str:
    """与 CommonMark 对齐：`# 标题` 才是标题，`#标题` 是普通文字。"""
    return _ATX_NO_SPACE_RE.sub(r"\\\1", text or "")


# ---------------------------------------------------------------------------
# 纯文本 / 统计
# ---------------------------------------------------------------------------
_PLAIN_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"```.*?```", re.S), " "),          # 围栏代码
    (re.compile(r"~~~.*?~~~", re.S), " "),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),  # 图片 -> alt
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),   # 链接 -> 文字
    (re.compile(r"\[\[([^\[\]|]+)\|([^\[\]]+)\]\]"), r"\2"),  # [[目标|别名]]
    (re.compile(r"\[\[([^\[\]]+)\]\]"), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),
    (re.compile(r"^\s{0,3}([-*+]|\d+[.)])\s+", re.M), ""),
    (re.compile(r"^\s{0,3}\[[ xX]\]\s*", re.M), ""),
    (re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$", re.M), " "),
    (re.compile(r"^\s{0,3}\[\^[^\]]+\]:.*$", re.M), " "),
    (re.compile(r"`+([^`]*)`+"), r"\1"),
    (re.compile(r"(\*\*\*|___)(.+?)\1", re.S), r"\2"),
    (re.compile(r"(\*\*|__)(.+?)\1", re.S), r"\2"),
    (re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.S), r"\1"),
    (re.compile(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", re.S), r"\1"),
    (re.compile(r"~~(.+?)~~", re.S), r"\1"),
    (re.compile(r"==(.+?)==", re.S), r"\1"),
    (re.compile(r"<[^>]{1,200}>"), " "),
    (re.compile(r"^\s*\|.*\|\s*$", re.M), " "),
]


def strip_markdown(text: str) -> str:
    """把 Markdown 还原成用于摘要 / 搜索索引的纯文本。"""
    plain = text or ""
    for pattern, replacement in _PLAIN_RULES:
        plain = pattern.sub(replacement, plain)
    plain = plain.replace("|", " ")
    plain = re.sub(r"[ \t\u00a0]+", " ", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    return plain.strip()


@dataclass
class Stats:
    cjk: int = 0
    latin: int = 0

    @property
    def words(self) -> int:
        return self.cjk + self.latin

    @property
    def minutes(self) -> int:
        if self.words <= 0:
            return 0
        return max(1, math.ceil(self.cjk / CJK_PER_MINUTE + self.latin / LATIN_PER_MINUTE))


_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*")


def text_stats(source: str) -> Stats:
    """字数：中日韩按字计，拉丁文按词计（先剥掉 Markdown 标记）。"""
    plain = strip_markdown(source)
    cjk = len(CJK_RE.findall(plain))
    latin = len(_LATIN_WORD_RE.findall(plain))
    return Stats(cjk=cjk, latin=latin)


def count_words(source: str) -> int:
    return text_stats(source).words


def reading_minutes(source: str) -> int:
    return text_stats(source).minutes


def make_excerpt(source: str, length: int = 110) -> str:
    """自动摘要：取纯文本开头，尽量在句读处收尾。"""
    plain = re.sub(r"\s+", " ", strip_markdown(source or "")).strip()
    if len(plain) <= length:
        return plain
    head = plain[:length]
    for mark in ("。", "！", "？", "；", ".", "!", "?", ";", "，", ","):
        position = head.rfind(mark)
        if position >= length * 0.6:
            return head[: position + 1].rstrip()
    return head.rstrip() + "…"


# ---------------------------------------------------------------------------
# mermaid 图：```mermaid 围栏先抽出来，渲染完再放回成安全 div
# ---------------------------------------------------------------------------
_MERMAID_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*mermaid\s*$")


def _extract_mermaid_blocks(source: str) -> tuple[str, list[str]]:
    """把 ```` ```mermaid ```` 围栏抽出来换成 ``{{mm:N}}`` 占位符。

    必须在 escape_raw_html 之前做：占位符不含 ``<``，后面的转义碰不到它；
    抽出来的代码不经过 Markdown，原样留给前端 Mermaid 渲染。
    """
    if "mermaid" not in (source or ""):
        return source or "", []
    lines = source.split("\n")
    out: list[str] = []
    blocks: list[str] = []
    fence: str | None = None
    current: list[str] = []
    for line in lines:
        match = _FENCE_RE.match(line)
        if fence is None and match and _MERMAID_FENCE_RE.match(line):
            fence = match.group(1)[0] * 3
            current = []
            out.append(f"{{{{mm:{len(blocks)}}}}}")
            blocks.append("")
            continue
        if fence is not None:
            if match and match.group(1)[0] * 3 == fence and not match.group(2).strip():
                blocks[-1] = "\n".join(current)
                fence = None
                continue
            current.append(line)
            continue
        out.append(line)
    return "\n".join(out), blocks


def _restore_mermaid(rendered: str, blocks: list[str]) -> str:
    """把占位符换回 ``<div class="mermaid">``（代码已转义；空块直接丢弃）。"""

    def swap(match: re.Match[str]) -> str:
        index = int(match.group(1) or match.group(2))
        code = blocks[index] if 0 <= index < len(blocks) else ""
        if not code.strip():
            return ""
        return f'<div class="mermaid">{html.escape(code)}</div>'

    # 先吃掉独占一段的 <p>{{mm:N}}</p>（常见情形），再兜底裸占位符
    rendered = re.sub(r"<p>\s*\{\{mm:(\d+)\}\}\s*</p>", swap, rendered)
    return _MERMAID_TOKEN_RE.sub(swap, rendered)


# ---------------------------------------------------------------------------
# 双链
# ---------------------------------------------------------------------------
def extract_wikilinks(source: str) -> list[WikiRef]:
    """抽取正文里的 [[标题]] / [[标题|别名]]（代码块内不解析）。"""
    refs: list[WikiRef] = []

    def collect(chunk: str) -> str:
        for match in _WIKI_RE.finditer(chunk):
            raw = match.group(1)
            title, _, alias = raw.partition("|")
            title = title.strip()
            if not title:
                continue
            refs.append(WikiRef(title=title, alias=(alias.strip() or title)))
        return chunk

    map_outside_code(source or "", collect)
    return refs


def _replace_wikilinks(
    source: str,
    resolver: Callable[[str], WikiRef | None] | None,
    url_builder: Callable[[WikiRef], str],
) -> tuple[str, list[WikiRef]]:
    """把 [[标题]] 换成占位符（返回替换后的文本 + 引用列表），避免被 Markdown 语法吃掉。"""
    collected: list[WikiRef] = []

    def replace(chunk: str) -> str:
        def on_match(match: re.Match[str]) -> str:
            raw = match.group(1)
            title, _, alias = raw.partition("|")
            title = title.strip()
            if not title:
                return match.group(0)
            ref = WikiRef(title=title, alias=(alias.strip() or title))
            if resolver is not None:
                try:
                    resolved = resolver(title)
                except Exception:  # 解析失败不影响渲染
                    resolved = None
                if resolved is not None:
                    ref.note_id = resolved.note_id
                    ref.exists = resolved.exists
                    ref.url = resolved.url or url_builder(ref)
            if not ref.url and ref.exists and ref.note_id:
                ref.url = url_builder(ref)
            index = len(collected)
            collected.append(ref)
            return f"{{{{wl:{index}}}}}"

        return _WIKI_RE.sub(on_match, chunk)

    replaced = map_outside_code(source, replace)
    return replaced, collected


def render_wikilink(ref: WikiRef, *, url_builder: Callable[[WikiRef], str]) -> str:
    label = html.escape(ref.alias or ref.title)
    if ref.exists:
        url = html.escape(ref.url or url_builder(ref))
        return f'<a class="wikilink" href="{url}">{label}</a>'
    return f'<span class="wikilink wikilink--missing" title="这篇笔记还没有创建">{label}</span>'


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _strip_leading_title(source: str, title: str | None) -> str:
    """正文第一行若是与笔记标题重复的 `# 标题`，就删掉，避免页面出现两个 H1。"""
    if not title or not source:
        return source
    lines = source.split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^#\s+(.*)$", stripped)
        if match and match.group(1).strip().casefold() == title.strip().casefold():
            return "\n".join(lines[:index] + lines[index + 1 :]).lstrip("\n")
        return source
    return source


def _sanitize_urls(rendered: str) -> str:
    def fix(match: re.Match[str]) -> str:
        attr = match.group("attr")
        url = html.unescape(match.group("url")).strip()
        lowered = url.lower()
        if lowered.startswith(_BAD_SCHEMES):
            return f'{attr}="#"'
        if lowered.startswith("data:") and not (attr.lower() == "src" and lowered.startswith("data:image/")):
            return f'{attr}="#"'
        return match.group(0)

    return _ATTR_URL_RE.sub(fix, rendered)


def _externalize_links(rendered: str) -> str:
    def fix(match: re.Match[str]) -> str:
        whole = match.group(0)
        if "target=" in whole.lower():
            return whole
        return (
            f'<a {match.group("prefix")}href="{match.group("url")}"'
            f' target="_blank" rel="noopener noreferrer"{match.group("suffix")}>'
        )

    return _ANCHOR_RE.sub(fix, rendered)


def _lazy_images(rendered: str) -> str:
    return _IMG_RE.sub('<img loading="lazy" decoding="async" ', rendered)


# 兜底防线：即便上面某处漏了转义，也不允许这些标签/事件属性出现在正文里。
# 注意别把 <input>（Markdown 任务清单要用）和 <label> 放进来。
_DANGEROUS_TAG_RE = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed|applet|style|form|link|meta|base|svg|math|template)\b",
    re.IGNORECASE,
)
_ON_ATTR_RE = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]{1,2000}>")


def _neutralise_dangerous_tags(rendered: str) -> str:
    """把危险的裸标签转成纯文本、去掉事件属性；作用域只限真正的标签，不碰已转义的文本。"""

    def fix(match: re.Match[str]) -> str:
        tag = match.group(0)
        if _DANGEROUS_TAG_RE.match(tag):
            return "&lt;" + tag[1:]
        return _ON_ATTR_RE.sub("", tag)

    return _TAG_RE.sub(fix, rendered)


def _wrap_tables(rendered: str) -> str:
    rendered = re.sub(r"<table(\s[^>]*)?>", lambda m: '<div class="table-wrap"><table' + (m.group(1) or "") + ">", rendered)
    return rendered.replace("</table>", "</table></div>")


def flatten_toc(tokens: list[dict] | None) -> list[dict]:
    """把 toc_tokens 树压平成 [{level, id, text}]。"""
    flat: list[dict] = []

    def walk(items: list[dict] | None) -> None:
        for item in items or []:
            flat.append(
                {
                    "level": int(item.get("level", 2)),
                    "id": item.get("id", ""),
                    "text": item.get("name", ""),
                }
            )
            walk(item.get("children"))

    walk(tokens)
    return flat


def render(
    source: str,
    *,
    title: str | None = None,
    resolver: Callable[[str], WikiRef | None] | None = None,
    url_builder: Callable[[WikiRef], str] | None = None,
) -> Rendered:
    """渲染一篇 Markdown 笔记。"""
    url_builder = url_builder or (lambda ref: f"/notes/{ref.note_id}" if ref.note_id else "#")
    body = _strip_leading_title(source or "", title)

    # 0) mermaid 围栏先抽走（占位符不吃转义、不吃 Markdown 语法）
    body, mermaid_blocks = _extract_mermaid_blocks(body)
    has_mermaid = any(block.strip() for block in mermaid_blocks)

    # 0.5) 探测公式：$$…$$、\[…\]、\(…\)（代码块里的不算）
    _math_hits: list[bool] = []

    def _probe_math(chunk: str) -> str:
        if "$$" in chunk or "\\[" in chunk or "\\(" in chunk:
            _math_hits.append(True)
        return chunk

    map_outside_code(body, _probe_math)
    has_math = bool(_math_hits)

    # 1) 禁用裸 HTML + 把 `#标签` 这类写法从「标题」里救出来
    body = map_outside_code(body, lambda chunk: normalise_headings(escape_raw_html(chunk)))
    # 2) 双链 -> 占位符（避免被 Markdown 的链接语法吃掉）
    body, refs = _replace_wikilinks(body, resolver, url_builder)

    parser = markdown_lib.Markdown(extensions=EXTENSIONS, extension_configs=EXTENSION_CONFIGS)
    rendered = parser.convert(body)
    toc = flatten_toc(getattr(parser, "toc_tokens", None))

    # 3) 占位符 -> 真正的链接
    def swap(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if 0 <= index < len(refs):
            return render_wikilink(refs[index], url_builder=url_builder)
        return ""

    rendered = _WIKI_TOKEN_RE.sub(swap, rendered)

    # 4) 收尾处理
    rendered = _neutralise_dangerous_tags(rendered)
    if has_mermaid:
        # 放在危险标签清洗之后：我们自己构造的 div 是唯一允许出现的原始节点
        rendered = _restore_mermaid(rendered, mermaid_blocks)
    rendered = _sanitize_urls(rendered)
    rendered = _mark_task_checkboxes(rendered)
    rendered = _externalize_links(rendered)
    rendered = _lazy_images(rendered)
    rendered = _wrap_tables(rendered)

    body_without_title = _strip_leading_title(source or "", title)
    plain = strip_markdown(body_without_title)
    stats = text_stats(body_without_title)

    return Rendered(
        html=rendered,
        toc=toc,
        plain=plain,
        excerpt=make_excerpt(source or ""),
        word_count=stats.words,
        reading_minutes=stats.minutes,
        wikilinks=refs,
        has_math=has_math,
        has_mermaid=has_mermaid,
    )


def preview_payload(source: str, *, title: str | None = None) -> dict:
    """给编辑器实时预览用的精简结果。"""
    result = render(source, title=title)
    return {
        "html": result.html,
        "toc": result.toc,
        "word_count": result.word_count,
        "reading_minutes": result.reading_minutes,
        "excerpt": result.excerpt,
        "empty": not (source or "").strip(),
    }
