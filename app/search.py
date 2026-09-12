"""全文搜索。

策略（针对中文友好的取舍）：
- 首选 SQLite FTS5 的 **trigram** 分词器：它能对中文做子串匹配，速度也快。
- trigram 要求查询词至少 3 个字符，所以 **短查询（少于 3 字）自动退回 LIKE**，
  保证「笔记」「搜索」这类高频双字词也能搜到。
- 万一运行环境的 SQLite 没编 FTS5（或没有 trigram），整个模块自动全程用 LIKE。
两种走法的对外结果结构一致：[(sqlite3.Row, score, snippet), ...]。
"""

from __future__ import annotations

import html
import logging
import re
import sqlite3

FTS_ENABLED = False
FTS_TOKENIZER = ""
FTS_TABLE = "notes_fts"

logger = logging.getLogger("inknote.search")

_TOKEN_RE = re.compile(r"\S+")


# ---------------------------------------------------------------------------
# 建表 / 维护索引
# ---------------------------------------------------------------------------
def ensure_schema(conn: sqlite3.Connection) -> None:
    global FTS_ENABLED, FTS_TOKENIZER
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (FTS_TABLE,),
    ).fetchone()
    if row is not None:
        ddl = (row["sql"] or "").lower()
        FTS_ENABLED = "fts5" in ddl
        FTS_TOKENIZER = "trigram" if "trigram" in ddl else ("unicode61" if FTS_ENABLED else "")
        if not FTS_ENABLED:
            logger.debug("搜索：已有 %s 但不是 FTS5 表，全程回退 LIKE", FTS_TABLE)
        return

    for tokenizer in ("trigram", "unicode61"):
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5("
                f"note_id UNINDEXED, title, content, tags, tokenize='{tokenizer}')"
            )
        except sqlite3.OperationalError:
            # SQLite 没编 FTS5 或没有该分词器：属正常环境降级，用 debug 避免刷屏
            logger.debug("搜索：FTS5 建表失败（tokenizer=%s），尝试下一个", tokenizer, exc_info=True)
            continue
        FTS_ENABLED = True
        FTS_TOKENIZER = tokenizer
        return
    FTS_ENABLED = False
    FTS_TOKENIZER = ""
    logger.debug("搜索：当前 SQLite 不支持 FTS5/trigram，全程回退 LIKE")


def sync_note(conn: sqlite3.Connection, note_id: int, title: str, content: str, tags_text: str) -> None:
    """写入 / 刷新一篇笔记的索引。"""
    if not FTS_ENABLED:
        return
    try:
        conn.execute(f"DELETE FROM {FTS_TABLE} WHERE note_id = ?", (note_id,))
        conn.execute(
            f"INSERT INTO {FTS_TABLE} (note_id, title, content, tags) VALUES (?, ?, ?, ?)",
            (note_id, title or "", content or "", tags_text or ""),
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
        "SELECT id, title, content FROM notes WHERE deleted_at IS NULL"
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
# 查询
# ---------------------------------------------------------------------------
def tokenize(query: str) -> list[str]:
    """把查询串切成关键词（去重、保序、最多 6 个）。"""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(query or ""):
        token = raw.strip().strip('"\'')
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
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}")


def retrieve(conn: sqlite3.Connection, question: str, *, limit: int = 6, public_only: bool = False) -> list[tuple[sqlite3.Row, float, str]]:
    """问答用的粗排检索：问题里的字组命中越多分越高。

    中文没有分词也照样能用（2/3/4 元组覆盖），笔记量在几千篇以内速度没问题。
    """
    terms = question_terms(question)
    if not terms:
        return []

    conditions = ["deleted_at IS NULL"]
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


def _use_fts(tokens: list[str]) -> bool:
    if not FTS_ENABLED or FTS_TOKENIZER != "trigram":
        return False
    return bool(tokens) and all(len(token) >= 3 for token in tokens)


def _conditions(*, public_only: bool, include_deleted: bool) -> list[str]:
    where: list[str] = []
    if not include_deleted:
        where.append("n.deleted_at IS NULL")
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
    tokens = tokenize(query)
    if not tokens:
        return []

    rows: list[sqlite3.Row] | None = None
    if _use_fts(tokens):
        rows = _search_fts(conn, tokens, public_only=public_only, pool=pool)
    elif FTS_ENABLED and FTS_TOKENIZER == "trigram":
        # trigram 要求查询词 >=3 字符，短词退回 LIKE 是设计行为
        logger.debug("搜索：查询词不足 3 字符，退回 LIKE（query=%r）", query)
    if rows is None:
        rows = _search_like(
            conn, tokens, public_only=public_only, include_deleted=include_deleted, pool=pool
        )

    return _rank(rows, tokens, limit, _load_tags(conn, [row["id"] for row in rows]))


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
    conn: sqlite3.Connection, tokens: list[str], *, public_only: bool, pool: int
) -> list[sqlite3.Row] | None:
    match = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    where = [f"{FTS_TABLE} MATCH ?", *_conditions(public_only=public_only, include_deleted=False)]
    sql = (
        f"SELECT n.*, bm25({FTS_TABLE}, 6.0, 1.0, 3.0) AS fts_rank "
        f"FROM {FTS_TABLE} f JOIN notes n ON n.id = f.note_id "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY fts_rank LIMIT {int(pool)}"
    )
    try:
        return conn.execute(sql, [match]).fetchall()
    except sqlite3.OperationalError:
        # 索引损坏或 MATCH 语法问题：本次退回 LIKE，属可预期降级，用 debug 避免刷屏
        logger.debug("搜索：FTS5 查询失败，本次退回 LIKE（tokens=%s）", tokens, exc_info=True)
        return None  # 索引损坏或语法问题，交给 LIKE 兜底


def _search_like(
    conn: sqlite3.Connection,
    tokens: list[str],
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
