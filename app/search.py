"""全文搜索。

策略（针对中文友好的取舍，2026-09-16 起为「中文二元索引」方案）：
- 索引建 FTS5 表（``tokenize='unicode61'``），但**存的是预分词文本**：
  CJK 切成「单字 + 相邻二元组」，英文/数字按词保留 —— 单字查询、两字查询、
  三字以上的连续子串都能精确命中 token，长短词全部走索引，不再退回 LIKE。
  （此前用 trigram：中文 ≥3 字才走索引，1~2 字的高频短词只能全表 LIKE。）
- 查询支持轻量语法（大小写均可）：
  ``tag:读书`` / ``#读书`` 按标签、``title:周报`` 仅标题、``cat:技术`` 按分类、
  ``is:starred|pinned|public|draft``、``after:2026-01`` / ``before:2026-09``
  按「最近更新」时间、``"精确短语"`` 整体匹配、``-词`` 排除；
  其余词照旧按全文关键词处理（多词 AND）。
- 万一运行环境的 SQLite 没编 FTS5，整个模块自动全程用 LIKE（结构化语法仍可用）。

两种走法的对外结果结构一致：[(sqlite3.Row, score, snippet), ...]。
"""

from __future__ import annotations

import calendar
import html
import logging
import re
import sqlite3

FTS_ENABLED = False
FTS_TOKENIZER = ""
FTS_TABLE = "notes_fts"

logger = logging.getLogger("inknote.search")

_TOKEN_RE = re.compile(r"\S+")
_PHRASE_RE = re.compile(r'"([^"]+)"|(\S+)')
_PREFIX_RE = re.compile(r"^(tag|title|cat|is|after|before):(.+)$", re.IGNORECASE)
_IS_VALUES = ("starred", "pinned", "public", "draft")
_WORD_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+#._-]*")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}")


# ---------------------------------------------------------------------------
# 索引文本预处理（中文二元分词的核心）
# ---------------------------------------------------------------------------
def _index_text(text: str) -> str:
    """把文本切成空格分隔的索引 token：

    - CJK：单字 + 相邻二元组（「部署手记」→ 部 署 手 记 部署 署手 手记）——
      单字查询命中单字 token、两字查询命中二元组 token、三字以上等于
      「相邻二元组全部 AND 命中」＝ 连续子串语义；
    - 英文/数字：按词保留（小写）。
    unicode61 分词器按空白切 token，所以这里用空格拼接即可。
    """
    pieces: list[str] = []
    text = text or ""
    cursor = 0

    def add_cjk_runs(segment: str) -> None:
        for run in _CJK_RUN_RE.findall(segment):
            chars = list(run)
            pieces.extend(chars)
            pieces.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))

    for match in _WORD_RE.finditer(text):
        add_cjk_runs(text[cursor : match.start()])
        pieces.append(match.group(0).lower())
        cursor = match.end()
    add_cjk_runs(text[cursor:])
    return " ".join(pieces)


def _match_token(token: str) -> str | None:
    """单个查询词 → FTS MATCH 表达式（长中文会展开成多个二元组 AND）。"""
    clean = token.strip().strip("\"'").lower()
    if not clean:
        return None
    if _CJK_RUN_RE.fullmatch(clean):
        chars = list(clean)
        if len(chars) == 1:
            return '"' + chars[0] + '"'
        return " AND ".join(
            '"' + chars[i] + chars[i + 1] + '"' for i in range(len(chars) - 1)
        )
    return '"' + clean.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# 建表 / 维护索引
# ---------------------------------------------------------------------------
def ensure_schema(conn: sqlite3.Connection) -> None:
    global FTS_ENABLED, FTS_TOKENIZER
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (FTS_TABLE,),
    ).fetchone()
    ddl = (row["sql"] or "").lower() if row is not None else ""

    if row is not None and "body" in ddl:
        # 已经是新格式（二元分词索引）
        FTS_ENABLED = "fts5" in ddl
        FTS_TOKENIZER = "bigram" if FTS_ENABLED else ""
        if not FTS_ENABLED:
            logger.debug("搜索：已有 %s 但不是 FTS5 表，全程回退 LIKE", FTS_TABLE)
        return

    if row is not None:
        # 旧格式（trigram 原文 / unicode61 原文）：查询语义变了，必须全量重建。
        # rebuild 是全表扫描但只在升级时发生一次；索引是纯衍生物，重建无风险。
        logger.info("搜索：索引格式升级（→ 中文二元分词），全量重建中…")
        conn.execute(f"DROP TABLE {FTS_TABLE}")

    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5("
            f"note_id UNINDEXED, body, tokenize='unicode61')"
        )
    except sqlite3.OperationalError:
        # SQLite 没编 FTS5：正常环境降级，用 debug 避免刷屏
        logger.debug("搜索：FTS5 建表失败，全程回退 LIKE", exc_info=True)
        FTS_ENABLED = False
        FTS_TOKENIZER = ""
        return

    FTS_ENABLED = True
    FTS_TOKENIZER = "bigram"
    rebuild(conn)


def sync_note(conn: sqlite3.Connection, note_id: int, title: str, content: str, tags_text: str) -> None:
    """写入 / 刷新一篇笔记的索引。"""
    if not FTS_ENABLED:
        return
    body = _index_text(f"{title or ''}\n{tags_text or ''}\n{content or ''}")
    try:
        conn.execute(f"DELETE FROM {FTS_TABLE} WHERE note_id = ?", (note_id,))
        conn.execute(
            f"INSERT INTO {FTS_TABLE} (note_id, body) VALUES (?, ?)",
            (note_id, body),
        )
    except sqlite3.OperationalError:
        # 索引写失败不影响笔记保存，但索引会与正文不一致，必须留痕
        logger.warning("搜索：FTS5 索引写入失败（note_id=%s）", note_id, exc_info=True)


def remove_note(conn: sqlite3.Connection, note_id: int) -> None:
    if not FTS_ENABLED:
        return
    try:
        conn.execute(f"DELETE FROM {FTS_TABLE} WHERE note_id = ?", (note_id,))
    except sqlite3.OperationalError:
        logger.warning("搜索：FTS5 索引删除失败（note_id=%s）", note_id, exc_info=True)


def rebuild(conn: sqlite3.Connection) -> int:
    """按 notes 表全量重建索引，返回索引条数（维护入口用）。"""
    if not FTS_ENABLED:
        return 0
    conn.execute(f"DELETE FROM {FTS_TABLE}")
    rows = conn.execute(
        "SELECT id, title, content FROM notes WHERE deleted_at IS NULL AND locked = 0"
    ).fetchall()
    count = 0
    for row in rows:
        tags = [
            r["name"]
            for r in conn.execute(
                "SELECT t.name FROM note_tags nt JOIN tags t ON t.id = nt.tag_id WHERE nt.note_id = ?",
                (row["id"],),
            )
        ]
        sync_note(conn, row["id"], row["title"], row["content"], " ".join(tags))
        count += 1
    return count


# ---------------------------------------------------------------------------
# 查询语法
# ---------------------------------------------------------------------------
def parse_query(query: str) -> dict:
    """把用户查询解析成结构化条件 + 普通词（详见模块 docstring 的语法表）。

    返回 dict：terms / phrases / excluded / tags / titles / categories /
    is（{"starred": True, ...}）/ after / before。引号短语整体保留、不解析前缀。
    """
    parsed: dict = {
        "terms": [],
        "phrases": [],
        "excluded": [],
        "tags": [],
        "titles": [],
        "categories": [],
        "is": {},
        "after": None,
        "before": None,
    }
    for match in _PHRASE_RE.finditer(query or ""):
        phrase = match.group(1)
        piece = phrase if phrase is not None else (match.group(2) or "")
        if not piece:
            continue
        if phrase is not None:
            parsed["phrases"].append(phrase.strip())
            continue
        low = piece.lower()
        if low.startswith("#") and len(piece) > 1:
            parsed["tags"].append(piece[1:])
            continue
        if piece.startswith("-") and len(piece) > 1:
            parsed["excluded"].append(piece[1:])
            continue
        prefix = _PREFIX_RE.match(piece)
        if prefix:
            key, value = prefix.group(1).lower(), prefix.group(2).strip()
            if key == "tag" and value:
                parsed["tags"].append(value)
            elif key == "title" and value:
                parsed["titles"].append(value)
            elif key == "cat" and value:
                parsed["categories"].append(value)
            elif key == "is" and value in _IS_VALUES:
                parsed["is"][value] = True
            elif key in ("after", "before") and value:
                parsed[key] = value
            else:
                parsed["terms"].append(piece)   # 未知前缀 / 坏值：当普通词
            continue
        parsed["terms"].append(piece)

    # 去重保序 + 限量：普通词最多 6 个（与旧行为一致）
    deduped: list[str] = []
    seen: set[str] = set()
    for token in parsed["terms"]:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(token)
        if len(deduped) >= 6:
            break
    parsed["terms"] = deduped
    parsed["phrases"] = parsed["phrases"][:3]
    parsed["excluded"] = parsed["excluded"][:4]
    for key in ("tags", "titles", "categories"):
        parsed[key] = parsed[key][:4]
    return parsed


def _normalize_date(value: str, *, start: bool) -> str | None:
    """``2026`` / ``2026-01`` / ``2026-01-05`` → 完整时间戳。

    start=True 取区间起点（年/月自动落到 1 月 1 日 / 当月 1 日）；
    start=False 取终点（只给年/月时落到该月最后一天的 23:59:59）。
    解析不了返回 None（调用方忽略该条件）。
    """
    parts = re.findall(r"\d+", value or "")[:3]
    if not parts:
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    year = numbers[0]
    month = numbers[1] if len(numbers) > 1 else 1
    day = numbers[2] if len(numbers) > 2 else 1
    if not 1 <= month <= 12 or not 1 <= day <= 31 or not 1 <= year <= 9999:
        return None
    if start:
        return f"{year:04d}-{month:02d}-{day:02d} 00:00:00"
    if len(numbers) < 3:
        day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{day:02d} 23:59:59"


def _structured_where(parsed: dict) -> tuple[list[str], list]:
    """把结构化条件翻译成 SQL（列都带 ``n.`` 前缀，FTS/LIKE 两条路共用）。"""
    from .utils import escape_like

    where: list[str] = []
    params: list = []
    for tag in parsed["tags"]:
        where.append(
            "EXISTS (SELECT 1 FROM note_tags nt JOIN tags t ON t.id = nt.tag_id "
            "WHERE nt.note_id = n.id AND t.name = ? COLLATE NOCASE)"
        )
        params.append(tag)
    for value in parsed["titles"]:
        where.append("n.title LIKE ? ESCAPE '\\'")
        params.append("%" + escape_like(value) + "%")
    for value in parsed["categories"]:
        where.append("n.category = ? COLLATE NOCASE")
        params.append(value)
    for value in parsed["excluded"]:
        pattern = "%" + escape_like(value) + "%"
        where.append("n.title NOT LIKE ? ESCAPE '\\' AND n.content NOT LIKE ? ESCAPE '\\'")
        params.extend([pattern, pattern])
    if parsed["is"].get("starred"):
        where.append("n.is_starred = 1")
    if parsed["is"].get("pinned"):
        where.append("n.is_pinned = 1")
    if parsed["is"].get("public"):
        where.append("n.is_public = 1")
    if parsed["is"].get("draft"):
        where.append("n.status = 'draft'")
    if parsed.get("after"):
        start = _normalize_date(parsed["after"], start=True)
        if start:
            where.append("n.updated_at >= ?")
            params.append(start)
    if parsed.get("before"):
        end = _normalize_date(parsed["before"], start=False)
        if end:
            where.append("n.updated_at <= ?")
            params.append(end)
    return where, params


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def tokenize(query: str) -> list[str]:
    """把查询串切成关键词（去重、保序、最多 6 个）。"""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(query or ""):
        token = raw.strip().strip("\"'")
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
        if len(tokens) >= 6:
            break
    return tokens


def highlight_tokens(query: str) -> list[str]:
    """结果页高亮用的词：解析后的普通词 + 短语 + 标签 + 标题词（不高亮语法本身）。"""
    parsed = parse_query(query)
    out: list[str] = []
    seen: set[str] = set()
    for token in parsed["terms"] + parsed["phrases"] + parsed["tags"] + parsed["titles"]:
        key = token.lower()
        if key in seen or not token.strip():
            continue
        seen.add(key)
        out.append(token)
        if len(out) >= 8:
            break
    return out


# 问句里常见但没有信息量的虚词
STOPWORDS = {
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个", "上", "也",
    "很", "到", "说", "要", "去", "会", "着", "没有", "看", "好", "自己", "这", "那", "什么",
    "怎么", "如何", "为什么", "哪些", "哪个", "可以", "需要", "以及", "关于", "总结", "一下",
    "请", "帮我", "告诉", "吗", "呢", "吧", "the", "a", "an", "is", "are", "to", "of", "and",
    "how", "what", "why", "which", "do", "does", "i", "you", "me", "my",
}
_PUNCT_RE = re.compile(r"[\s，。！？、；：「」『』（）【】《》,.!?;:\"'`~…—\-_/\\|+=*&^%$#@\[\]{}<>]+")


def question_terms(question: str, *, limit: int = 48) -> list[str]:
    """把一句自然语言问题拆成可用于检索的字组（中文按 2/3/4 元组，英文按词）。"""
    text = (question or "").lower()
    terms: list[str] = []

    # 英文单词与数字
    for word in _LATIN_RE.findall(text):
        if len(word) >= 2 and word not in STOPWORDS:
            terms.append(word)

    # 中文：滑窗取 2~4 字，过滤纯虚词
    chunks = _CJK_RUN_RE.findall(text)
    for chunk in chunks:
        for size in (4, 3, 2):
            for index in range(0, max(0, len(chunk) - size + 1)):
                piece = chunk[index : index + size]
                if piece in STOPWORDS or piece in terms:
                    continue
                if all(char in "的地得了着和与及之" for char in piece):
                    continue
                terms.append(piece)
                if len(terms) >= limit:
                    return terms[:limit]
    return terms[:limit]


_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9+#._-]{1,30}")


def retrieve(conn: sqlite3.Connection, question: str, *, limit: int = 6, public_only: bool = False) -> list[tuple[sqlite3.Row, float, str]]:
    """问答用的粗排检索：问题里的字组命中越多分越高。

    中文没有分词也照样能用（2/3/4 元组覆盖），笔记量在几千篇以内速度没问题。
    """
    terms = question_terms(question)
    if not terms:
        return []

    conditions = ["deleted_at IS NULL AND locked = 0"]
    if public_only:
        conditions.append("is_public = 1")
    rows = conn.execute(
        f"SELECT * FROM notes WHERE {' AND '.join(conditions)}"
    ).fetchall()

    scored: list[tuple[sqlite3.Row, float, str]] = []
    for row in rows:
        title = (row["title"] or "").lower()
        content = (row["content"] or "").lower()
        summary = (row["summary"] or "").lower()
        if not content and not title:
            continue
        score = 0.0
        matched = 0
        for term in terms:
            if term in title:
                score += 6.0
                matched += 1
                continue
            if term in summary:
                score += 1.5
                matched += 1
            hits = content.count(term)
            if hits:
                score += 1.0 + min(hits, 3) * 0.4
                matched += 1
        if not matched:
            continue
        scored.append((row, round(score, 2), make_snippet(row["content"] or "", terms)))

    scored.sort(key=lambda item: (item[1], item[0]["updated_at"] or ""), reverse=True)
    return scored[:limit]


def _use_fts() -> bool:
    """二元索引对任何长度的词都能精确命中，所以只要有索引就走 FTS。"""
    return FTS_ENABLED and FTS_TOKENIZER == "bigram"


def _conditions(*, public_only: bool, include_deleted: bool) -> list[str]:
    where: list[str] = []
    if not include_deleted:
        where.append("n.deleted_at IS NULL AND n.locked = 0")
    if public_only:
        where.append("n.is_public = 1")
    return where


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    public_only: bool = False,
    include_deleted: bool = False,
    limit: int = 50,
    pool: int = 400,
) -> list[tuple[sqlite3.Row, float, str]]:
    """返回 (笔记行, 相关度分数, 摘要片段) 列表，已按相关度排序。"""
    parsed = parse_query(query)
    tokens = parsed["terms"]
    phrases = parsed["phrases"]
    structured = bool(
        parsed["tags"]
        or parsed["titles"]
        or parsed["categories"]
        or parsed["excluded"]
        or parsed["is"]
        or parsed.get("after")
        or parsed.get("before")
    )
    if not tokens and not phrases and not structured:
        return []

    rows: list[sqlite3.Row] | None = None
    if (tokens or phrases) and _use_fts():
        rows = _search_fts(conn, tokens, phrases, parsed,
                           public_only=public_only, pool=pool)
    if rows is None:
        # 纯结构化条件（没有关键词）或 FTS 不可用 → LIKE 路径
        rows = _search_like(conn, tokens, phrases, parsed,
                            public_only=public_only,
                            include_deleted=include_deleted, pool=pool)

    ranked_terms = tokens + phrases
    return _rank(rows, ranked_terms, limit, _load_tags(conn, [row["id"] for row in rows]))


def _load_tags(conn: sqlite3.Connection, note_ids: list[int]) -> dict[int, str]:
    """取一批笔记的标签文本，用于打分。"""
    ids = [int(i) for i in note_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT nt.note_id AS note_id, t.name AS name FROM note_tags nt "
        f"JOIN tags t ON t.id = nt.tag_id WHERE nt.note_id IN ({placeholders})",
        ids,
    ).fetchall()
    grouped: dict[int, list[str]] = {}
    for row in rows:
        grouped.setdefault(int(row["note_id"]), []).append(row["name"])
    return {note_id: " ".join(names) for note_id, names in grouped.items()}


def _search_fts(
    conn: sqlite3.Connection,
    tokens: list[str],
    phrases: list[str],
    parsed: dict,
    *,
    public_only: bool,
    pool: int,
) -> list[sqlite3.Row] | None:
    match_parts = [_match_token(token) for token in tokens]
    match_parts += [_match_token(phrase) for phrase in phrases]
    match_parts = [part for part in match_parts if part]
    if not match_parts:
        return None
    match = " AND ".join(match_parts)

    where = [f"{FTS_TABLE} MATCH ?", *_conditions(public_only=public_only, include_deleted=False)]
    extra_where, extra_params = _structured_where(parsed)
    where += extra_where
    params = [match, *extra_params]
    sql = (
        f"SELECT n.*, bm25({FTS_TABLE}) AS fts_rank "
        f"FROM {FTS_TABLE} f JOIN notes n ON n.id = f.note_id "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY fts_rank LIMIT {int(pool)}"
    )
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # 索引损坏或 MATCH 语法问题：本次退回 LIKE，属可预期降级，用 debug 避免刷屏
        logger.debug("搜索：FTS5 查询失败，本次退回 LIKE（tokens=%r）", tokens, exc_info=True)
        return None  # 索引损坏或语法问题，交给 LIKE 兜底


def _search_like(
    conn: sqlite3.Connection,
    tokens: list[str],
    phrases: list[str],
    parsed: dict,
    *,
    public_only: bool,
    include_deleted: bool,
    pool: int,
) -> list[sqlite3.Row]:
    from .utils import escape_like

    where = _conditions(public_only=public_only, include_deleted=include_deleted)
    params: list = []
    for token in tokens:
        pattern = f"%{escape_like(token)}%"
        where.append(
            "(n.title LIKE ? ESCAPE '\\' OR n.content LIKE ? ESCAPE '\\' OR EXISTS ("
            "SELECT 1 FROM note_tags nt JOIN tags t ON t.id = nt.tag_id "
            "WHERE nt.note_id = n.id AND t.name LIKE ? ESCAPE '\\'))"
        )
        params.extend([pattern, pattern, pattern])
    for phrase in phrases:
        pattern = f"%{escape_like(phrase)}%"
        where.append("(n.title LIKE ? ESCAPE '\\' OR n.content LIKE ? ESCAPE '\\')")
        params.extend([pattern, pattern])
    extra_where, extra_params = _structured_where(parsed)
    where += extra_where
    params += extra_params
    sql = f"SELECT n.* FROM notes n WHERE {' AND '.join(where)}"
    sql += f" ORDER BY n.updated_at DESC, n.id DESC LIMIT {int(pool)}"
    return conn.execute(sql, params).fetchall()


def _rank(
    rows: list[sqlite3.Row], tokens: list[str], limit: int, tags_map: dict[int, str] | None = None
) -> list[tuple[sqlite3.Row, float, str]]:
    """在 Python 里统一打分：标题 > 标签 > 正文；并生成摘要片段。"""
    tags_map = tags_map or {}
    scored: list[tuple[sqlite3.Row, float, str]] = []
    for row in rows:
        keys = {key: row[key] for key in row.keys()}
        title = (keys.get("title") or "").lower()
        content = (keys.get("content") or "")
        lowered = content.lower()
        tags_text = tags_map.get(int(keys["id"]), "").lower()

        score = 0.0
        for token in tokens:
            needle = token.lower()
            if needle in title:
                score += 8.0
            if needle in tags_text:
                score += 4.0
            hits = lowered.count(needle)
            if hits:
                score += 1.0 + min(hits, 5) * 0.4
        # 置顶 / 星标轻微加权，让重要笔记更靠前
        if keys.get("is_pinned"):
            score += 1.5
        if keys.get("is_starred"):
            score += 0.5
        snippet = make_snippet(content, tokens)
        scored.append((row, round(score, 2), snippet))

    scored.sort(key=lambda item: (item[1], item[0]["updated_at"] or ""), reverse=True)
    return scored[: max(1, limit)]


# ---------------------------------------------------------------------------
# 片段与高亮
# ---------------------------------------------------------------------------
def make_snippet(content: str, tokens: list[str], *, before: int = 40, after: int = 120) -> str:
    """从正文里截取包含关键词的一段纯文本，用于搜索结果展示。"""
    from .markdown_render import strip_markdown

    plain = re.sub(r"\s+", " ", strip_markdown(content or "")).strip()
    if not plain:
        return ""
    lowered = plain.lower()
    position = -1
    for token in tokens:
        found = lowered.find(token.lower())
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        return plain[: before + after].strip()
    start = max(0, position - before)
    end = min(len(plain), position + after)
    piece = plain[start:end].strip()
    return ("…" if start > 0 else "") + piece + ("…" if end < len(plain) else "")


def highlight(text: str, tokens: list[str]) -> str:
    """转义文本并把命中关键词包进 <mark>，结果可安全地用 |safe 输出。"""
    escaped = html.escape(text or "")
    cleaned = [token for token in tokens if token.strip()]
    if not cleaned or not escaped:
        return escaped
    patterns = sorted({re.escape(html.escape(token)) for token in cleaned}, key=len, reverse=True)
    try:
        regex = re.compile("|".join(patterns), re.IGNORECASE)
    except re.error:
        # patterns 都过了 re.escape，正常不会失败；真失败说明输入异常，值得 warning
        logger.warning("搜索：高亮正则编译失败，退回纯转义文本", exc_info=True)
        return escaped
    return regex.sub(lambda match: f"<mark>{match.group(0)}</mark>", escaped)
