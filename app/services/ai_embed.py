"""向量检索：给「问笔记」做语义召回（embedding + 余弦相似度）。

关键词检索只能命中字面相同的词；本模块把每篇笔记切块、调用服务商的
`POST {base}/embeddings`（OpenAI 兼容格式）拿到向量，存进自建的
`note_embeddings` 表。提问时把问题也向量化，和全部块算余弦相似度，
同一篇笔记只保留得分最高的块，返回和 `repo.retrieve_notes` 同构的结果。

对外接口（其它模块只应该用这些）：
    ensure(conn)                                  惰性建表，可重复调用
    available(conn=None) -> bool                  配了 embed_model 且库里有向量
    retrieve(conn, question, *, limit=6)          语义召回；不可用/出错一律返回 None
    status(conn) -> dict                          设置页要的索引状态
    rebuild(conn, progress=None, *, force=False)  同步重建（可由后台线程调用）
     maybe_auto_index(conn, *, interval_minutes=30, force=False, progress=None)
                                                   按需增量重建（启动 / 定时器调用）
     auto_index_state(conn) -> dict                自动索引状态（设置页 / 调试）
     pending_count(conn) -> int                    还有几篇笔记没跟上索引
    clear(conn) -> int                            清空索引，返回删除条数

设计取舍：
- 只用标准库；向量用 ``array('f')`` 打包成 BLOB，余弦相似度用纯 Python 点积。
- 查询复杂度 O(总块数 × 向量维度)：几千篇、每篇几块的点积在毫秒~百毫秒级。
- 所有外部失败都被吞掉：retrieve 返回 None，让调用方回退关键词检索。
"""

from __future__ import annotations

import array
import json
import logging
import math
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request

from ..utils import now, now_iso, parse_dt
from . import ai

logger = logging.getLogger("inknote.ai_embed")

TABLE = "note_embeddings"
META_PREFIX = "ai_embed."

# 自动增量索引自己的 meta 前缀：带前缀独立存放，不和手动重建的 last_built_at 撞
AUTO_META_PREFIX = "ai_embed.auto."
AUTO_MAX_ERRORS = 5      # 结果里最多回传前几条错误

BATCH_SIZE = 16          # 每次请求最多送 16 条文本
MAX_CHUNKS = 20          # 每篇最多保留 20 块，超出截断
CHUNK_OVERLAP = 100      # 相邻块重叠约 100 字
CHUNK_MAX = 800          # 单块硬上限
CHUNK_MIN = 600          # 目标下限（段落自然结束时可能略小）
_ATOM_MAX = CHUNK_MAX - CHUNK_OVERLAP - 2  # 切段上限，保证「重叠 + 段」不超 800

# 后台重建的运行状态。加锁，因为重建线程和请求线程都会读写。
_STATE_LOCK = threading.Lock()
_STATE: dict = {"running": False, "progress": None, "error": "", "last_built_at": ""}


# ---------------------------------------------------------------------------
# 建表 / 表结构
# ---------------------------------------------------------------------------
def ensure(conn: sqlite3.Connection) -> None:
    """惰性建表：不改 db.py，第一次用到时自己 CREATE TABLE IF NOT EXISTS。"""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        " note_id     INTEGER NOT NULL,"
        " chunk_index INTEGER NOT NULL,"
        " chunk       TEXT    NOT NULL DEFAULT '',"
        " vector      BLOB    NOT NULL,"
        " model       TEXT    NOT NULL DEFAULT '',"
        " updated_at  TEXT    NOT NULL DEFAULT '',"
        " PRIMARY KEY (note_id, chunk_index)"
        ")"
    )


# ---------------------------------------------------------------------------
# 配置 / 可用性
# ---------------------------------------------------------------------------
def _configured_model() -> str:
    """只认显式配置的 embed_model。

    ai.models_for("embed") 在 embed_model 为空时会回退到通用聊天模型，
    但聊天模型通常不支持 /embeddings，所以这里不把回退值当作「已配置向量模型」。
    """
    try:
        return str(ai.current().get("embed_model") or "").strip()
    except Exception:
        # 读不到 AI 配置时按「未配置向量模型」处理，调用方会回退关键词检索；留痕方便排查
        logger.debug("读取 embed_model 配置失败，按未配置处理", exc_info=True)
        return ""


def available(conn: sqlite3.Connection | None = None) -> bool:
    """配了 embed_model 且库里已经有向量；不传 conn 时自己开个短连接查。"""
    if not _configured_model():
        return False
    if conn is not None:
        return _has_vectors(conn)
    try:
        from .. import db as db_mod

        with db_mod.db() as own_conn:
            return _has_vectors(own_conn)
    except Exception:
        # 自己开短连接查失败（比如数据目录不可写）：按「没有向量」处理，不能影响页面
        logger.debug("检查向量索引可用性失败，按不可用处理", exc_info=True)
        return False


def _has_vectors(conn: sqlite3.Connection) -> bool:
    try:
        ensure(conn)
        row = conn.execute(f"SELECT 1 FROM {TABLE} LIMIT 1").fetchone()
        return row is not None
    except Exception:
        # 表还没建 / 库只读等：按「没有向量」处理，让调用方回退关键词检索
        logger.debug("查询向量表失败（table=%s），按无向量处理", TABLE, exc_info=True)
        return False


def status(conn: sqlite3.Connection) -> dict:
    """设置页 / 路由要的索引状态；结构固定，任何异常都变成 error 字段。"""
    running, progress = _running_progress()
    payload = {
        "configured": bool(_configured_model()),
        "model": _configured_model(),
        "total": 0,
        "indexed": 0,
        "running": running,
        "progress": progress,
        "error": "",
        "last_built_at": "",
    }
    try:
        ensure(conn)
        payload["total"] = _count_live_notes(conn)
        payload["indexed"] = _count_indexed_notes(conn)
        payload["last_built_at"] = _load_last_built(conn)
        with _STATE_LOCK:
            if _STATE["error"]:
                payload["error"] = str(_STATE["error"])
            if _STATE["last_built_at"]:
                payload["last_built_at"] = str(_STATE["last_built_at"])
    except Exception as exc:  # 状态查询失败也不该让页面 500
        payload["error"] = str(exc) or "读取向量索引状态失败"
    return payload


def is_running() -> bool:
    with _STATE_LOCK:
        return bool(_STATE["running"])


def _running_progress() -> tuple[bool, dict | None]:
    with _STATE_LOCK:
        running = bool(_STATE["running"])
        progress = dict(_STATE["progress"]) if _STATE["progress"] else None
    return running, progress


def _report(progress, done: int, total: int) -> None:
    """更新模块级进度，并回调外部 progress(done, total)。"""
    done = max(0, int(done))
    total = max(0, int(total))
    with _STATE_LOCK:
        if _STATE["running"]:
            _STATE["progress"] = {"done": done, "total": total}
    if progress is not None:
        try:
            progress(done, total)
        except Exception:  # 进度回调坏了不能拖垮重建
            logger.debug("向量重建进度回调抛异常，已忽略（done=%s, total=%s）", done, total, exc_info=True)


# ---------------------------------------------------------------------------
# 统计 / 元信息
# ---------------------------------------------------------------------------
def _count_live_notes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM notes WHERE deleted_at IS NULL").fetchone()
    return int(row["c"] or 0) if row else 0


def _count_indexed_notes(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        f"SELECT COUNT(DISTINCT e.note_id) AS c FROM {TABLE} e "
        "JOIN notes n ON n.id = e.note_id WHERE n.deleted_at IS NULL"
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def _load_last_built(conn: sqlite3.Connection) -> str:
    from .. import repo

    return str(repo.get_meta_map(conn, META_PREFIX).get("last_built_at") or "")


def _save_last_built(conn: sqlite3.Connection, value: str) -> None:
    from .. import repo

    repo.save_meta_map(conn, {"last_built_at": value}, META_PREFIX)


def _purge_stale(conn: sqlite3.Connection) -> int:
    """删掉 note_id 已不在 notes（或已进回收站）里的残留向量。"""
    cursor = conn.execute(
        f"DELETE FROM {TABLE} WHERE note_id NOT IN "
        "(SELECT id FROM notes WHERE deleted_at IS NULL)"
    )
    return int(cursor.rowcount or 0)


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------
def _note_text(title: str, content: str) -> str:
    """把标题也放进向量，正文里已经有同名标题时不重复。"""
    content = (content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    head = (title or "").strip()
    if head and head not in content[:200]:
        return f"# {head}\n\n{content}".strip()
    return content


def _split_segments(text: str) -> list[str]:
    """按空行切段；Markdown 标题单独成段，让标题语义更容易命中。"""
    text = re.sub(r"[ \t]+\n", "\n", text)
    segments: list[str] = []
    for block in re.split(r"\n\s*\n+", text):
        block = block.strip()
        if not block:
            continue
        # 在标题行前面切开，标题和它下面的正文各成一段
        for piece in re.split(r"(?m)(?=^#{1,6}\s)", block):
            piece = piece.strip()
            if piece:
                segments.append(piece)
    return segments


def _slice_long(segment: str, size: int, overlap: int) -> list[str]:
    """超长段落切片；优先在句末/换行处断开，切片之间保留 overlap。"""
    if len(segment) <= size:
        return [segment]
    pieces: list[str] = []
    start = 0
    length = len(segment)
    while start < length:
        end = min(start + size, length)
        if end < length:
            window = segment[start:end]
            best = 0
            for mark in "。！？；.!?;，,\n":
                position = window.rfind(mark)
                if position > best:
                    best = position
            if best >= size // 2:
                end = start + best + 1
        piece = segment[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= length:
            break
        start = max(0, end - overlap)
    return pieces


def chunk_note(title: str, content: str) -> list[str]:
    """把一篇笔记切成 600~800 字、相邻重叠约 100 字的块，最多 20 块。"""
    text = _note_text(title, content)
    if not text:
        return []
    atoms: list[str] = []
    for segment in _split_segments(text):
        atoms.extend(_slice_long(segment, _ATOM_MAX, CHUNK_OVERLAP))

    chunks: list[str] = []
    current = ""
    truncated = False
    index = 0
    while index < len(atoms):
        atom = atoms[index].strip()
        if not atom:
            index += 1
            continue
        if current and len(current) + 1 + len(atom) > CHUNK_MAX:
            room = CHUNK_MAX - len(current) - 1
            if 0 < room < len(atom) and len(current) < CHUNK_MIN:
                # 当前块太短时，从下一段借一段补齐到 800，避免出现 400 多字的碎块
                current = f"{current}\n{atom[:room]}"
                atoms[index] = atom[room:]
                chunks.append(current.strip())
                if len(chunks) >= MAX_CHUNKS:
                    truncated = True
                    break
                current = current[-CHUNK_OVERLAP:].strip()
                continue
            chunks.append(current.strip())
            if len(chunks) >= MAX_CHUNKS:
                truncated = True
                break
            # 新块开头带上一块的尾部，形成约 100 字的上下文重叠
            current = current[-CHUNK_OVERLAP:].strip()
        current = f"{current}\n{atom}" if current else atom
        index += 1

    # 结尾只有上一块的「重叠尾巴」时不要再单独成块
    if current.strip() and (not chunks or len(current) > CHUNK_OVERLAP):
        if len(chunks) < MAX_CHUNKS:
            chunks.append(current.strip())
        else:
            truncated = True

    if truncated:
        logger.warning("笔记「%s」内容过长，向量索引只保留前 %s 块", title or "无标题", MAX_CHUNKS)
    return chunks[:MAX_CHUNKS]


# ---------------------------------------------------------------------------
# 向量：打包 / 归一化 / 相似度
# ---------------------------------------------------------------------------
def _pack_vector(vector) -> bytes:
    if not vector:
        return b""
    return array.array("f", [float(x) for x in vector]).tobytes()


def _unpack_vector(blob) -> list[float]:
    if not blob:
        # 空 BLOB 多半是历史坏数据；跳过该块但不能中断整轮检索
        logger.debug("向量 BLOB 为空，跳过该块（table=%s）", TABLE)
        return []
    values = array.array("f")
    try:
        values.frombytes(bytes(blob))
    except (TypeError, ValueError):
        # BLOB 不是 float32 数组：跳过该块，留痕提示 note_embeddings.vector 可能损坏
        logger.debug("向量 BLOB 无法解包，跳过该块（table=%s）", TABLE, exc_info=True)
        return []
    return [float(x) for x in values]


def _normalise(vector) -> list[float]:
    """把向量归一化成单位长度，之后点积就等于余弦相似度。"""
    numbers = [float(x) for x in vector]
    norm = math.sqrt(sum(x * x for x in numbers))
    if norm <= 0:
        return numbers
    return [x / norm for x in numbers]


def _dot(left: list[float], right: list[float]) -> float:
    # 归一化后的点积 = 余弦相似度。查询复杂度主项：总块数 × 向量维度；
    # zip 比按下标索引略快，几千块纯 Python 大约百毫秒级。
    return float(sum(x * y for x, y in zip(left, right)))


# ---------------------------------------------------------------------------
# 调用 embedding 接口
# ---------------------------------------------------------------------------
def _headers(conf: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if conf.get("api_key"):
        headers["Authorization"] = f"Bearer {conf['api_key']}"
    return headers


def _embed_once(texts: list[str], conf: dict, model: str) -> list[list[float]]:
    """请求一批（<=16 条）文本的向量；失败抛 ai.AIError。"""
    payload = {"model": model, "input": list(texts)}
    request = urllib.request.Request(
        ai.endpoint(conf["base_url"], "embeddings"),
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(conf),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=conf.get("timeout") or 45) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # pragma: no cover - 读错误体失败无所谓
            logger.debug("读取向量服务错误响应体失败（HTTP %s）", exc.code, exc_info=True)
        raise ai.AIError(ai.humanize_error(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise ai.AIError(f"连不上这个地址：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ai.AIError("向量服务响应超时") from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ai.AIError("向量服务返回的不是合法 JSON") from exc

    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise ai.AIError("向量服务返回格式不对（缺少 data 列表）")

    parsed: list[tuple[int, list[float]]] = []
    for position, item in enumerate(items):
        if isinstance(item, dict):
            index = item.get("index")
            vector = item.get("embedding")
        else:
            index = position
            vector = item
        if not isinstance(vector, (list, tuple)) or not vector:
            raise ai.AIError("向量服务返回的 embedding 不是数字列表")
        try:
            numbers = [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise ai.AIError("向量服务返回的 embedding 里有非数字") from exc
        parsed.append((index if isinstance(index, int) else position, numbers))

    if len(parsed) != len(texts):
        raise ai.AIError(f"向量服务返回了 {len(parsed)} 条，期望 {len(texts)} 条")

    # 服务商不保证顺序：有 index 就按 index 排，没有就按返回顺序
    if all(isinstance(index, int) for index, _vector in parsed):
        parsed.sort(key=lambda pair: pair[0])
    return [vector for _index, vector in parsed]


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """把任意数量的文本按 16 条一批送出去，返回顺序和输入一致。"""
    if not texts:
        return []
    conf = ai.credentials()
    model = _configured_model()
    base_url = str(conf.get("base_url") or "").strip()
    if not base_url:
        raise ai.AIError("尚未配置 AI 服务地址（base_url）")
    if not model:
        raise ai.AIError("尚未配置向量模型（embed_model）")

    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        vectors.extend(_embed_once(texts[start : start + BATCH_SIZE], conf, model))
    return vectors


# ---------------------------------------------------------------------------
# 语义检索
# ---------------------------------------------------------------------------
def retrieve(conn: sqlite3.Connection, question: str, *, limit: int = 6) -> list[dict] | None:
    """语义召回；任何不可用/失败都返回 None，让调用方回退关键词检索。"""
    try:
        question = (question or "").strip()
        if not question or not _configured_model():
            # 高频正常路径：没配向量模型 / 空问题直接回退关键词，不刷 warning
            logger.debug("向量检索跳过：问题为空或未配置 embed_model，回退关键词检索")
            return None
        ensure(conn)
        rows = conn.execute(f"SELECT note_id, chunk, vector FROM {TABLE}").fetchall()
        if not rows:
            # 高频正常路径：索引库为空（还没重建过），回退关键词
            logger.debug("向量库为空（table=%s），回退关键词检索", TABLE)
            return None

        query = _normalise(_embed_texts([question])[0])

        scored: list[tuple[float, int, str]] = []
        for row in rows:
            vector = _unpack_vector(row["vector"])
            if not vector:
                continue
            scored.append((_dot(query, vector), int(row["note_id"]), str(row["chunk"] or "")))
        if not scored:
            logger.debug("向量库里没有可用向量（全部解包失败），回退关键词检索")
            return None
        scored.sort(key=lambda item: item[0], reverse=True)

        top_k = max(1, int(limit)) * 4
        best: dict[int, tuple[float, str]] = {}
        for score, note_id, chunk in scored[:top_k]:
            if note_id not in best or score > best[note_id][0]:
                best[note_id] = (score, chunk)
        ordered = sorted(best.items(), key=lambda item: item[1][0], reverse=True)
        if not ordered:
            logger.debug("向量检索没有命中任何笔记，回退关键词检索")
            return None

        from .. import repo
        from .. import search as search_mod

        note_ids = [note_id for note_id, _best in ordered]
        placeholders = ",".join("?" for _ in note_ids)
        note_rows = conn.execute(
            f"SELECT * FROM notes WHERE id IN ({placeholders}) AND deleted_at IS NULL", note_ids
        ).fetchall()
        notes = {note["id"]: note for note in repo.hydrate(conn, note_rows)}
        terms = search_mod.question_terms(question)

        results: list[dict] = []
        for note_id, (score, chunk) in ordered:
            note = notes.get(note_id)
            if not note:
                continue
            note = dict(note)
            note["score"] = round(float(score), 4)
            # 语义命中时问题里的词往往不在正文里；用命中块做片段更贴题
            note["snippet"] = search_mod.make_snippet(chunk or note.get("content") or "", terms)
            results.append(note)
        return results or None
    except Exception:
        logger.debug("向量检索失败，回退关键词检索（limit=%s）", limit, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 相关笔记：只读向量辅助（不重建索引，也不动 retrieve 的现有行为）
# ---------------------------------------------------------------------------
def embedding_for_note(conn: sqlite3.Connection, note_id: int) -> list[float] | None:
    """取这篇笔记的向量：多块按维度取平均；没有 / 读取失败返回 None。

    只读辅助，复用本模块已存好的向量，不重新调用 embedding 服务。
    存储阶段写的是归一化向量，这里按原样平均，交给调用方再做归一化。
    """
    try:
        note_id = int(note_id)
        ensure(conn)
        rows = conn.execute(
            f"SELECT vector FROM {TABLE} WHERE note_id = ? ORDER BY chunk_index",
            (note_id,),
        ).fetchall()
        if not rows:
            logger.debug("笔记 #%s 还没有向量", note_id)
            return None
        vectors = [_unpack_vector(row["vector"]) for row in rows]
        vectors = [vector for vector in vectors if vector]
        if not vectors:
            logger.debug("笔记 #%s 的向量 BLOB 全部不可用", note_id)
            return None
        width = len(vectors[0])
        # 换过向量模型时维度可能不一致：只用和第一条同维度的块，避免 zip 截断算出假余弦
        usable = [vector for vector in vectors if len(vector) == width]
        if not usable:
            logger.debug("笔记 #%s 的向量维度不一致，视为没有向量", note_id)
            return None
        average = [0.0] * width
        for vector in usable:
            for index, value in enumerate(vector):
                average[index] += value
        count = float(len(usable))
        return [value / count for value in average]
    except Exception:
        # 只读辅助：任何异常都当作「没有向量」，调用方会回退关键词相关笔记
        logger.debug("读取笔记 #%s 的向量失败，按无向量处理", note_id, exc_info=True)
        return None


def similar_notes(conn: sqlite3.Connection, note_id: int, *, limit: int = 5) -> list[dict] | None:
    """按向量余弦找和 note_id 相似的笔记（排除自己 / 回收站），按相似度倒序。

    只用库里已经存好的向量，读取阶段不再调用外部 embedding 服务，因此即使
    向量服务临时挂了，已建索引的笔记依旧能出推荐。不可用 / 没有任何命中
    一律返回 None，让调用方回退 repo 的关键词逻辑。
    """
    try:
        if not _configured_model():
            logger.debug("相似笔记跳过：未配置 embed_model，回退关键词")
            return None
        try:
            note_id = int(note_id)
        except (TypeError, ValueError):
            return None
        limit = max(1, int(limit))
        ensure(conn)

        target = embedding_for_note(conn, note_id)
        if not target:
            logger.debug("相似笔记跳过：笔记 #%s 没有可用向量，回退关键词", note_id)
            return None
        query = _normalise(target)

        # 直接在 SQL 里排除自己和回收站，避免回收站里的高分残留把正常结果挤掉
        rows = conn.execute(
            f"SELECT e.note_id AS note_id, e.chunk AS chunk, e.vector AS vector "
            f"FROM {TABLE} e JOIN notes n ON n.id = e.note_id "
            "WHERE e.note_id <> ? AND n.deleted_at IS NULL",
            (note_id,),
        ).fetchall()
        if not rows:
            logger.debug("向量库里没有其它笔记，回退关键词检索")
            return None

        scored: list[tuple[float, int, str]] = []
        for row in rows:
            vector = _unpack_vector(row["vector"])
            if not vector or len(vector) != len(query):
                # 维度不一致（换过向量模型）：跳过，避免 zip 截断后算出假相似度
                continue
            scored.append((_dot(query, _normalise(vector)), int(row["note_id"]), str(row["chunk"] or "")))
        if not scored:
            logger.debug("向量库里没有可用向量，回退关键词检索")
            return None
        scored.sort(key=lambda item: item[0], reverse=True)

        # 和 retrieve 一致：同一篇笔记只保留得分最高的块
        top_k = limit * 4
        best: dict[int, tuple[float, str]] = {}
        for score, candidate_id, chunk in scored[:top_k]:
            if candidate_id not in best or score > best[candidate_id][0]:
                best[candidate_id] = (score, chunk)
        ordered = sorted(best.items(), key=lambda item: item[1][0], reverse=True)
        if not ordered:
            return None

        from .. import repo
        from .. import search as search_mod

        note_ids = [candidate_id for candidate_id, _best in ordered]
        placeholders = ",".join("?" for _ in note_ids)
        note_rows = conn.execute(
            f"SELECT * FROM notes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            note_ids,
        ).fetchall()
        notes = {note["id"]: note for note in repo.hydrate(conn, note_rows)}

        results: list[dict] = []
        for candidate_id, (score, chunk) in ordered:
            note = notes.get(candidate_id)
            if not note:
                continue
            item = dict(note)
            item["score"] = round(float(score), 4)
            item["snippet"] = search_mod.make_snippet(chunk or note.get("content") or "", [])
            results.append(item)
        return results or None
    except Exception:
        # 真出异常了不能静默：调用方虽然会回退关键词，但留 warning 方便排查
        logger.warning("相似笔记计算失败，回退关键词（note_id=%s）", note_id, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 重建 / 清理
# ---------------------------------------------------------------------------
def rebuild(
    conn: sqlite3.Connection,
    progress=None,
    *,
    force: bool = False,
) -> dict:
    """同步重建向量索引。

    - 默认只重建 updated_at / model 变化过的笔记，没变的跳过；
    - force=True 时不看缓存，全部重算（设置页的「强制重建」）；
    - 顺手删掉 note_id 已不存在的残留记录；
    - 返回 {"ok", "total", "indexed", "built", "skipped", "removed", "error", "last_built_at"}。
    """
    model = _configured_model()
    with _STATE_LOCK:
        if _STATE["running"]:
            return {
                "ok": False,
                "total": 0,
                "indexed": 0,
                "built": 0,
                "skipped": 0,
                "removed": 0,
                "error": "正在重建中",
                "last_built_at": str(_STATE.get("last_built_at") or ""),
            }
        _STATE["running"] = True
        _STATE["progress"] = None
        _STATE["error"] = ""

    total = 0
    indexed = 0
    built = 0
    skipped = 0
    removed = 0
    try:
        ensure(conn)
        conf = ai.credentials()
        if not str(conf.get("base_url") or "").strip():
            raise ai.AIError("尚未配置 AI 服务地址（base_url）")
        if not model:
            raise ai.AIError("尚未配置向量模型（embed_model）")

        notes = conn.execute(
            "SELECT id, title, content, updated_at FROM notes "
            "WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
        total = len(notes)
        _report(progress, 0, total)

        existing = {
            int(row["note_id"]): (
                str(row["updated_at"] or ""),
                int(row["chunks"] or 0),
                str(row["model"] or ""),
            )
            for row in conn.execute(
                f"SELECT note_id, MAX(updated_at) AS updated_at, COUNT(*) AS chunks, "
                f"MAX(model) AS model FROM {TABLE} GROUP BY note_id"
            )
        }
        removed += _purge_stale(conn)

        for position, note in enumerate(notes, 1):
            note_id = int(note["id"])
            stamp = str(note["updated_at"] or "")
            cached = existing.get(note_id)
            if not force and cached and cached[0] == stamp and cached[1] > 0 and cached[2] == model:
                skipped += 1
                _report(progress, position, total)
                continue

            chunks = chunk_note(note["title"], note["content"])
            conn.execute(f"DELETE FROM {TABLE} WHERE note_id = ?", (note_id,))
            if chunks:
                vectors = [_normalise(vector) for vector in _embed_texts(chunks)]
                conn.executemany(
                    f"INSERT OR REPLACE INTO {TABLE} "
                    "(note_id, chunk_index, chunk, vector, model, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (note_id, index, chunk, _pack_vector(vectors[index]), model, stamp)
                        for index, chunk in enumerate(chunks)
                    ],
                )
                built += 1
            _report(progress, position, total)

        last_built = now_iso()
        _save_last_built(conn, last_built)
        indexed = _count_indexed_notes(conn)
        with _STATE_LOCK:
            _STATE["last_built_at"] = last_built
        return {
            "ok": True,
            "total": total,
            "indexed": indexed,
            "built": built,
            "skipped": skipped,
            "removed": removed,
            "error": "",
            "last_built_at": last_built,
        }
    except Exception as exc:
        message = str(exc) or "向量索引重建失败"
        # 结果里虽然有 error 字段，但会丢掉 traceback；重建是用户主动触发、不频繁，值得 warning
        logger.warning(
            "向量索引重建失败（已建 %s/%s，built=%s）：%s", indexed, total, built, exc, exc_info=True
        )
        with _STATE_LOCK:
            _STATE["error"] = message
        return {
            "ok": False,
            "total": total,
            "indexed": indexed,
            "built": built,
            "skipped": skipped,
            "removed": removed,
            "error": message,
            "last_built_at": str(_STATE.get("last_built_at") or ""),
        }
    finally:
        with _STATE_LOCK:
            _STATE["running"] = False
            _STATE["progress"] = None


def clear(conn: sqlite3.Connection) -> int:
    """清空向量索引，返回删除的块数。"""
    try:
        ensure(conn)
        cursor = conn.execute(f"DELETE FROM {TABLE}")
        return int(cursor.rowcount or 0)
    except Exception:
        # 清空失败不能让设置页 500；但这是用户主动操作，值得 warning 而不是 debug
        logger.warning("清空向量索引失败（table=%s）", TABLE, exc_info=True)
        return 0

# ---------------------------------------------------------------------------
# 自动增量索引（主 agent 在启动时 + 每 30 分钟的后台线程里调用）
# ---------------------------------------------------------------------------
def _load_auto_meta(conn: sqlite3.Connection) -> dict:
    """读自动索引自己的 meta（前缀独立，不和手动重建的键冲突）。"""
    try:
        from .. import repo

        return repo.get_meta_map(conn, AUTO_META_PREFIX)
    except Exception:
        logger.debug("读取自动索引 meta 失败", exc_info=True)
        return {}


def _save_auto_result(conn: sqlite3.Connection, result: dict, last_run_at: str) -> None:
    """成功跑完一轮才写；失败不调用，下一轮还会重试。"""
    from .. import repo

    summary = {key: result.get(key) for key in ("indexed", "total", "seconds", "pruned")}
    repo.save_meta_map(
        conn,
        {
            "last_run_at": str(last_run_at),
            "last_result": json.dumps(summary, ensure_ascii=False),
        },
        AUTO_META_PREFIX,
    )


def _seconds_since(stamp: str) -> float | None:
    """距 stamp 过了多少秒；解析不了返回 None（当作没跑过）。"""
    dt = parse_dt(stamp)
    if dt is None:
        return None
    return (now() - dt).total_seconds()


def _stored_index(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """一次读出现有索引：{note_id: [块文本]} 和 {note_id: (updated_at, model)}。"""
    chunks: dict[int, list[str]] = {}
    meta: dict[int, tuple[str, str]] = {}
    rows = conn.execute(
        f"SELECT note_id, chunk, updated_at, model FROM {TABLE} ORDER BY note_id, chunk_index"
    ).fetchall()
    for row in rows:
        note_id = int(row["note_id"])
        chunks.setdefault(note_id, []).append(str(row["chunk"] or ""))
        meta[note_id] = (str(row["updated_at"] or ""), str(row["model"] or ""))
    return chunks, meta


def maybe_auto_index(
    conn: sqlite3.Connection,
    *,
    interval_minutes: float = 30,
    force: bool = False,
    progress=None,
) -> dict | None:
    """按需做一轮增量索引，供启动时 / 后台定时器调用。

    - 没配 embed_model：返回 None，不发请求、不写 meta（不留痕迹）；
    - 距上次自动索引不足 interval_minutes：返回 None；
    - 单篇失败只记进 ``errors``，继续跑后面的；只要有一篇失败就不写时间戳，下一轮重试；
    - 成功返回 ``{"indexed", "total", "seconds", "pruned"}``（有失败时多带 ``errors``）。
    """
    model = _configured_model()
    if not model:
        # 高频正常路径：没配向量模型，静默跳过
        return None

    if not force:
        # 间隔判断失败（meta 里时间格式坏了 / interval 不是数字）就当作该跑，绝不往外抛
        try:
            last_run_at = str(_load_auto_meta(conn).get("last_run_at") or "")
            if last_run_at:
                elapsed = _seconds_since(last_run_at)
                window = max(0.0, float(interval_minutes)) * 60.0
                if elapsed is not None and 0 <= elapsed < window:
                    logger.debug("自动索引跳过：距上次 %.1f 秒，不足 %s 分钟", elapsed, interval_minutes)
                    return None
        except Exception:
            logger.debug("自动索引：间隔判断失败，按需要重跑处理", exc_info=True)

    try:
        ensure(conn)
    except Exception:
        logger.warning("自动索引：建表失败，跳过本轮", exc_info=True)
        return None

    started = time.monotonic()
    with _STATE_LOCK:
        if _STATE["running"]:
            logger.debug("自动索引跳过：已有重建在运行")
            return None
        _STATE["running"] = True
        _STATE["progress"] = None

    total = 0
    built = 0
    pruned = 0
    errors: list[str] = []
    try:
        conf = ai.credentials()
        if not str(conf.get("base_url") or "").strip():
            raise ai.AIError("尚未配置 AI 服务地址（base_url）")

        notes = conn.execute(
            "SELECT id, title, content, updated_at FROM notes "
            "WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
        total = len(notes)
        _report(progress, 0, total)

        pruned = _purge_stale(conn)
        stored_chunks, stored_meta = _stored_index(conn)

        for position, note in enumerate(notes, 1):
            note_id = int(note["id"])
            stamp = str(note["updated_at"] or "")
            info = stored_meta.get(note_id)
            try:
                chunks = chunk_note(note["title"], note["content"])
            except Exception as exc:  # 切块是纯本地逻辑，兜底不中断整轮
                errors.append(f"笔记 #{note_id} 切块失败：{exc}")
                _report(progress, position, total)
                continue

            if not chunks:
                # 标题必非空，正常走不到；真遇到也只是清掉残留，不算失败
                if info is not None:
                    conn.execute(f"DELETE FROM {TABLE} WHERE note_id = ?", (note_id,))
                _report(progress, position, total)
                continue

            # 除了 updated_at / model，再比一次块文本：同一秒内改的正文也能被认出来
            unchanged = (
                not force
                and info is not None
                and info[0] == stamp
                and info[1] == model
                and stored_chunks.get(note_id, []) == chunks
            )
            if unchanged:
                _report(progress, position, total)
                continue

            try:
                # 先算向量再删旧行：单篇失败时旧索引还在，不会被清空
                vectors = [_normalise(vector) for vector in _embed_texts(chunks)]
                conn.execute(f"DELETE FROM {TABLE} WHERE note_id = ?", (note_id,))
                conn.executemany(
                    f"INSERT OR REPLACE INTO {TABLE} "
                    "(note_id, chunk_index, chunk, vector, model, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (note_id, index, chunk, _pack_vector(vectors[index]), model, stamp)
                        for index, chunk in enumerate(chunks)
                    ],
                )
                built += 1
            except Exception as exc:
                errors.append(f"笔记 #{note_id} 索引失败：{exc}")
                logger.warning("自动索引：笔记 #%s 索引失败，跳过继续", note_id, exc_info=True)
            _report(progress, position, total)

        result = {
            "indexed": built,
            "total": total,
            "seconds": round(time.monotonic() - started, 2),
            "pruned": pruned,
        }
        if errors:
            # 有单篇失败：收敛错误、不写时间戳，下一轮整体重试
            result["errors"] = errors[:AUTO_MAX_ERRORS]
            logger.warning("自动索引：%s 篇失败（成功 %s/%s），不记时间戳", len(errors), built, total)
            return result

        _save_auto_result(conn, result, now_iso())
        logger.info("自动索引完成：indexed=%s total=%s pruned=%s", built, total, pruned)
        return result
    except Exception:
        # 地址没配、库挂了等整轮级失败：log 后返回 None，不写时间戳
        logger.warning("自动索引整轮失败，本轮不写时间戳", exc_info=True)
        return None
    finally:
        with _STATE_LOCK:
            _STATE["running"] = False
            _STATE["progress"] = None


def pending_count(conn: sqlite3.Connection) -> int:
    """还有几篇 live 笔记的 updated_at / model 和索引里存的对不上（含从未索引）。"""
    try:
        ensure(conn)
        model = _configured_model()
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM notes n LEFT JOIN ("
            f"SELECT note_id, MAX(updated_at) AS updated_at, MAX(model) AS model "
            f"FROM {TABLE} GROUP BY note_id) e ON e.note_id = n.id "
            "WHERE n.deleted_at IS NULL AND ("
            " e.note_id IS NULL OR e.updated_at IS NULL OR e.updated_at <> n.updated_at"
            " OR e.model IS NULL OR e.model <> ?)",
            (model,),
        ).fetchone()
        return int(row["c"] or 0) if row else 0
    except Exception:
        logger.debug("统计待索引笔记失败", exc_info=True)
        return 0


def auto_index_state(conn: sqlite3.Connection) -> dict:
    """给设置页 / 调试用：上次自动索引时间、上次结果、是否还有待索引的笔记。"""
    state = {
        "last_run_at": "",
        "last_result": None,
        "pending_count": 0,
        "has_pending": False,
        "error": "",
    }
    try:
        meta = _load_auto_meta(conn)
        state["last_run_at"] = str(meta.get("last_run_at") or "")
        raw = str(meta.get("last_result") or "")
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                logger.debug("自动索引结果 JSON 解析失败（value=%r）", raw, exc_info=True)
                parsed = None
            if isinstance(parsed, dict):
                state["last_result"] = parsed
        state["pending_count"] = pending_count(conn)
        state["has_pending"] = state["pending_count"] > 0
    except Exception as exc:
        state["error"] = str(exc) or "读取自动索引状态失败"
    return state