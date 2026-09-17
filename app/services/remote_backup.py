"""远端备份：把本地最新快照 PUT 到 WebDAV 目录，做一份「机器没了也还在」的副本。

为什么选 WebDAV：Nextcloud / 坚果云 / 群晖 / 甚至路由器的文件服务都支持它，
而且 PUT / PROPFIND / MKCOL / DELETE 四个动作用标准库 urllib 就能做，
不需要引入对象存储 SDK —— 与本项目「零依赖」的克制一致。

三条设计原则：

1. **默认上传脱敏副本**。远端是别人的机器/云盘，默认副本里不含 AI 密钥、登录口令
   哈希与 WebDAV 口令本身（脱敏键清单见 ``db_backup.SANITIZED_META_KEYS``）。
   想连密钥一起备份的，把「上传完整备份」打开，页面上会明确警告。
2. **失败绝不静默**。每次尝试都把「时间 + 结果 + 人类可读的原因」写进 meta，
   备份页直接显示；本地备份也绝不受远端失败影响。
3. **只删自己传上去的文件**。清理远端旧副本时按文件名严格匹配
   ``inknote-<YYYYMMDD-HHMMSS>-<原因>[-sanitized].db``，别的文件一律不碰。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from .. import repo
from ..utils import now
from . import db_backup

logger = logging.getLogger("inknote.remote_backup")

# 全部配置存在 meta 表里，键前缀 backup.remote.（与 ai.* 同一套惯例）
PREFIX = "backup.remote."
KEYS = ("url", "user", "password", "interval_hours", "keep", "full", "last")

# 上传 / 列目录 / 删文件的超时（秒）。上传给得宽一点，库可能几十 MB。
UPLOAD_TIMEOUT = 120
QUICK_TIMEOUT = 20
DEFAULT_INTERVAL_HOURS = 24
DEFAULT_KEEP = 7

# 远端文件名必须长这样才算「我们传的」，清理时只动这些
OUR_NAME = re.compile(r"^inknote-\d{8}-\d{6}-[a-z0-9-]+\.db$")

PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:getcontentlength/><d:getlastmodified/>"
    b"</d:prop></d:propfind>"
)

_LOCK = threading.RLock()


class RemoteError(RuntimeError):
    """远端备份的可预期失败（网络 / 认证 / 目录），消息直接给用户看。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _load(conn: sqlite3.Connection) -> dict[str, str]:
    """取出 ``backup.remote.*`` 的全部配置（键去掉前缀）。"""
    return repo.get_meta_map(conn, PREFIX)


def _store(conn: sqlite3.Connection, values: dict) -> None:
    repo.save_meta_map(conn, values, PREFIX)


def _as_int(raw, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def config(conn: sqlite3.Connection) -> dict:
    """当前远端配置（``password`` 是明文，只在服务端流转，页面不回显）。"""
    data = _load(conn)
    return {
        "url": (data.get("url") or "").strip(),
        "user": (data.get("user") or "").strip(),
        "password": data.get("password") or "",
        "interval_hours": _as_int(data.get("interval_hours"), DEFAULT_INTERVAL_HOURS),
        "keep": _as_int(data.get("keep"), DEFAULT_KEEP),
        "full": (data.get("full") or "").strip() == "1",
    }


def is_configured(cfg: dict | None = None, conn: sqlite3.Connection | None = None) -> bool:
    data = cfg if cfg is not None else config(conn)
    return bool(data.get("url"))


def mask(secret: str) -> str:
    """和 AI 密钥一样：只露首尾，页面上不回显完整口令。"""
    text = (secret or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "•" * len(text)
    return f"{text[:3]}…{text[-2:]}"


def validate(values: dict) -> dict:
    """校验并整理表单值；非法值抛 ``RemoteError``（路由层直接 flash 给用户）。"""
    url = str(values.get("url") or "").strip()
    if url:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise RemoteError("地址要以 http:// 或 https:// 开头（WebDAV 目录地址）")
        if not parsed.netloc:
            raise RemoteError("地址里缺少主机名")
        if not url.endswith("/"):
            url += "/"          # 统一当成「目录」，拼文件名时才不会吃掉最后一段
        url = _encode_path(url)   # 中文目录名 → %XX（坚果云「我的坚果云」这类）

    # 注意用 `is None` 判空而不是 `or`：0 是合法值（= 只手动上传），
    # 写 `values.get(...) or ""` 会把 0 当成没填、静默回落到 24 小时（被测试抓过）。
    def _raw(name: str) -> str:
        value = values.get(name)
        return "" if value is None else str(value).strip()

    interval_raw = _raw("interval_hours")
    interval = DEFAULT_INTERVAL_HOURS if interval_raw == "" else _as_int(interval_raw, -1)
    if interval < 0 or interval > 720:
        raise RemoteError("间隔请填 0~720 小时（0 = 只手动上传）")

    keep_raw = _raw("keep")
    keep = DEFAULT_KEEP if keep_raw == "" else _as_int(keep_raw, -1)
    if keep < 1 or keep > 100:
        raise RemoteError("远端保留份数请填 1~100")

    return {
        "url": url,
        "user": str(values.get("user") or "").strip(),
        "password": str(values.get("password") or ""),
        "interval_hours": interval,
        "keep": keep,
        "full": bool(values.get("full")),
    }


def save(conn: sqlite3.Connection, values: dict) -> dict:
    """保存配置。密码留空 = 保持原值（与 AI 密钥一致，避免误清空）。"""
    clean = validate(values)
    if not clean["password"]:
        clean["password"] = _load(conn).get("password") or ""
    _store(conn, {
        "url": clean["url"],
        "user": clean["user"],
        "password": clean["password"],
        "interval_hours": str(clean["interval_hours"]),
        "keep": str(clean["keep"]),
        "full": "1" if clean["full"] else "0",
    })
    conn.commit()
    return clean


def reset(conn: sqlite3.Connection) -> None:
    """清空远端配置与状态（含口令）。"""
    repo.delete_meta(conn, [f"{PREFIX}{name}" for name in KEYS])
    conn.commit()


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def _write_status(conn: sqlite3.Connection, payload: dict) -> None:
    _store(conn, {"last": json.dumps(payload, ensure_ascii=False)})
    conn.commit()


def status(conn: sqlite3.Connection) -> dict | None:
    raw = (_load(conn).get("last") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _last_at(conn: sqlite3.Connection) -> datetime | None:
    data = status(conn) or {}
    stamp = str(data.get("at") or "")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# WebDAV 客户端（标准库 urllib）
# ---------------------------------------------------------------------------


def _auth_header(cfg: dict) -> dict:
    user = cfg.get("user") or ""
    password = cfg.get("password") or ""
    if not user and not password:
        return {}
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": "Basic " + token}


def _open(request: urllib.request.Request, cfg: dict, timeout: int):
    """本机 / 局域网地址（NAS、路由器上的 WebDAV）不走系统代理 —— 代理连内网只会 502。

    与 AI 服务同一套判断（``ai.is_local_address``），避免两处逻辑漂移。
    """
    from . import ai

    if ai.is_local_address(cfg.get("url") or ""):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def _request(method: str, url: str, cfg: dict, *, data=None, headers=None, timeout=QUICK_TIMEOUT) -> dict:
    """发一个 WebDAV 请求；HTTP 层错误也返回（交给调用方给友好提示），只有网络不通才抛。"""
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in {**_auth_header(cfg), **(headers or {})}.items():
        request.add_header(key, value)
    try:
        with _open(request, cfg, timeout) as response:
            return {
                "status": int(getattr(response, "status", response.getcode())),
                "body": response.read(),
            }
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:  # noqa: BLE001  读不到就算了，状态码已经够用
            body = b""
        return {"status": int(exc.code), "body": body, "reason": str(exc.reason)}
    except urllib.error.URLError as exc:
        raise RemoteError(f"连不上远端：{exc.reason}") from exc
    except OSError as exc:
        raise RemoteError(f"网络错误：{exc}") from exc


def _explain(status_code: int, what: str) -> str:
    if status_code in (401, 403):
        return f"{what}被拒绝：用户名或密码不对（{status_code}）"
    if status_code == 404:
        return f"{what}失败：地址不存在（{status_code}），确认 URL 指向的是已存在的目录"
    if status_code == 409:
        return f"{what}失败：目标目录不存在（409）"
    if status_code == 507:
        return f"{what}失败：远端空间不足（507）"
    if status_code >= 500:
        return f"{what}失败：远端服务出错（{status_code}）"
    return f"{what}失败（HTTP {status_code}）"


def _encode_path(url: str) -> str:
    """把 URL 路径里的非 ASCII（中文目录名）转成 ``%XX``。

    urllib 发不出含中文的请求行，而坚果云的主目录就叫「我的坚果云」——用户直接粘
    中文地址必须能work。已经写好的 ``%XX``（坚果云官方示例就是让人手填）保持原样。
    """
    parsed = urllib.parse.urlsplit(url)
    if not parsed.path:
        return url
    encoded = urllib.parse.quote(parsed.path, safe="/%:@!$&'()*+,;=~-._")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, encoded, parsed.query, parsed.fragment))


def _host_hint(cfg: dict) -> str:
    """坚果云特有的地址结构提示（只在确实是坚果云时给，别给别的服务端添乱）。"""
    host = (urllib.parse.urlsplit(cfg.get("url") or "").hostname or "").lower()
    if "jianguoyun" not in host:
        return ""
    return ("。坚果云的 WebDAV 根目录列的是你的同步文件夹（通常是「我的坚果云」），"
            "建议把地址填成 …/dav/我的坚果云/inknote/，或在客户端里先建好目录")


def _file_url(cfg: dict, name: str) -> str:
    return urllib.parse.urljoin(cfg["url"], urllib.parse.quote(name))


def _mkcol(cfg: dict, target: str) -> bool:
    """建一级目录；已存在返回 True（不同服务端用 405 / 重定向表示已存在）。"""
    response = _request("MKCOL", target, cfg)
    return response["status"] in (200, 201, 204, 301, 302, 307, 308, 405)


def _ensure_collection(cfg: dict) -> list[str]:
    """逐级把目标目录建出来（从最外层往里），返回新建成功的路径。

    用户往往只填了地址、没在网盘里先建目录；而「目录不存在」这个错误各服务端口径
    不一 —— 坚果云回 **404**，Apache/Nextcloud 回 **409**。两种情况都要能自愈，
    否则就成了「明明密码对、却一直报地址不存在」（实测踩到过）。
    """
    parsed = urllib.parse.urlsplit(cfg["url"])
    segments = [seg for seg in parsed.path.split("/") if seg]
    created: list[str] = []
    for index in range(1, len(segments) + 1):
        prefix = "/" + "/".join(segments[:index]) + "/"
        target = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, prefix, "", ""))
        response = _request("MKCOL", target, cfg)
        if response["status"] in (200, 201, 204):
            created.append(prefix)
        elif response["status"] in (301, 302, 307, 308, 405):
            continue                      # 已存在
        else:
            raise RemoteError(
                _explain(response["status"], f"创建目录 {prefix}") + _host_hint(cfg)
            )
    return created


def _put(cfg: dict, path: Path, name: str) -> None:
    payload = path.read_bytes()
    response = _request(
        "PUT", _file_url(cfg, name), cfg, data=payload,
        headers={"Content-Type": "application/octet-stream"},
        timeout=UPLOAD_TIMEOUT,
    )
    if response["status"] in (200, 201, 204):
        return
    # 目录不存在：404（坚果云）或 409（Apache/Nextcloud）都先建出来再重试一次
    if response["status"] in (404, 409):
        _ensure_collection(cfg)
        retry = _request(
            "PUT", _file_url(cfg, name), cfg, data=payload,
            headers={"Content-Type": "application/octet-stream"},
            timeout=UPLOAD_TIMEOUT,
        )
        if retry["status"] in (200, 201, 204):
            return
        raise RemoteError(_explain(retry["status"], "上传") + _host_hint(cfg))
    raise RemoteError(_explain(response["status"], "上传") + _host_hint(cfg))


def list_remote(cfg: dict) -> list[dict]:
    """列目录（Depth: 1），返回 ``[{"name": ...}]``。

    用正则扒 href 而不是 XML 解析：不同服务端的命名空间前缀五花八门（D: / d: / 无前缀），
    正则只认 ``<...href ...>文本</...href>``，反而更稳；解析不了也不会抛。
    """
    response = _request(
        "PROPFIND", cfg["url"], cfg, data=PROPFIND_BODY,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
    )
    if response["status"] not in (200, 207):
        raise RemoteError(_explain(response["status"], "读取远端目录"))
    text = response["body"].decode("utf-8", "replace")
    names: list[str] = []
    for raw in re.findall(r"<[^>]*href[^>]*>([^<]+)</[^>]*href>", text, re.I):
        name = urllib.parse.unquote(raw.strip().rstrip("/").rsplit("/", 1)[-1])
        if name:
            names.append(name)
    return [{"name": name} for name in names]


def _delete(cfg: dict, name: str) -> bool:
    response = _request("DELETE", _file_url(cfg, name), cfg)
    return response["status"] in (200, 202, 204, 404)


def prune_remote(cfg: dict, entries: list[dict], *, keep: int, protect: str = "") -> list[str]:
    """只删「我们自己传上去的」旧副本，保留最新 ``keep`` 份。

    文件名带时间戳，按名字排序即按时间排序（不依赖服务端返回的修改时间）。
    ``protect`` 里的名字（一般是本次刚传的）永不删除。其它文件一律不碰。
    """
    ours = sorted(e["name"] for e in entries if OUR_NAME.match(e["name"]))
    if protect and protect not in ours:
        ours.append(protect)
    ours = sorted(set(ours))
    limit = max(1, int(keep))
    if len(ours) <= limit:
        return []
    removed: list[str] = []
    for name in ours[: len(ours) - limit]:
        if name == protect:
            continue
        if _delete(cfg, name):
            removed.append(name)
    return removed


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------


def _temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="inknote-remote-"))


def _prepare_payload(conn: sqlite3.Connection, cfg: dict) -> tuple[Path, str, Path | None]:
    """挑出要上传的文件：本地最新快照；没有就先造一份自动备份。

    返回 ``(文件路径, 远端文件名, 需要善后的临时目录或 None)``。脱敏模式下会复制到
    临时目录再抹掉敏感键 —— **不往 data/backups/ 里塞临时文件**，本地保留策略不受影响。
    """
    snapshots = db_backup.list_snapshots(limit=1)
    if snapshots:
        name = snapshots[0]["name"]
        source = db_backup.backup_dir() / name
    else:
        created = db_backup.create_snapshot(conn, reason="auto")
        name, source = created["name"], Path(created["path"])

    if cfg.get("full"):
        return source, name, None

    directory = _temp_dir()
    target = directory / name
    db_backup.export_sanitized_copy(source, target)
    return target, name, directory


def run_upload(conn: sqlite3.Connection, *, trigger: str = "manual") -> dict:
    """生成/挑选一份快照并上传，记录状态后返回结果（不抛异常，失败也返回 ok=False）。"""
    cfg = config(conn)
    if not cfg["url"]:
        return {"ok": False, "message": "还没配置 WebDAV 地址", "at": now().isoformat(timespec="seconds")}

    started = time.perf_counter()
    temp: Path | None = None
    try:
        with _LOCK:
            path, name, temp = _prepare_payload(conn, cfg)
            _put(cfg, path, name)
            removed = prune_remote(cfg, list_remote(cfg), keep=cfg["keep"], protect=name)
        payload = {
            "ok": True,
            "at": now().isoformat(timespec="seconds"),
            "message": f"已上传 {name}（{path.stat().st_size // 1024} KB）",
            "name": name,
            "size": int(path.stat().st_size),
            "sanitized": not cfg.get("full"),
            "removed": removed,
            "trigger": trigger,
            "seconds": round(time.perf_counter() - started, 2),
        }
    except RemoteError as exc:
        payload = {
            "ok": False,
            "at": now().isoformat(timespec="seconds"),
            "message": str(exc),
            "trigger": trigger,
            "seconds": round(time.perf_counter() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001  任何意外都要落状态，不能静默
        logger.warning("远端备份出错", exc_info=True)
        payload = {
            "ok": False,
            "at": now().isoformat(timespec="seconds"),
            "message": f"意外错误：{exc}",
            "trigger": trigger,
            "seconds": round(time.perf_counter() - started, 2),
        }
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)

    _write_status(conn, payload)
    logger.info("远端备份（%s）：%s", trigger, payload["message"])
    return payload


def probe(cfg: dict) -> dict:
    """按给定配置列一次目录；**只读**，不写任何东西。

    可以传页面上刚填、还没保存的配置 —— 「测试连接」的意义就在这儿。
    """
    if not (cfg or {}).get("url"):
        return {"ok": False, "message": "还没填 WebDAV 地址"}
    try:
        entries = list_remote(cfg)
    except RemoteError as exc:
        return {"ok": False, "message": str(exc) + _parent_hint(cfg)}
    ours = [e["name"] for e in entries if OUR_NAME.match(e["name"])]
    return {
        "ok": True,
        "message": f"连接正常，目录里有 {len(entries)} 个条目（其中 {len(ours)} 份是我们的备份）",
    }


def _parent_hint(cfg: dict) -> str:
    """目标目录不存在时，看一眼父目录在不在 —— 直接告诉用户是「没建」还是「写错了」。"""
    parsed = urllib.parse.urlsplit(cfg.get("url") or "")
    parent = parsed.path.rstrip("/").rsplit("/", 1)[0] + "/"
    # 注意：父目录就是根目录（`/missing/` → `/`）时**不能跳过** —— 根目录的
    # PROPFIND 恰恰是最能说明问题的：根在、目标不在 = 目录没建。
    if parent == parsed.path or parent == "":
        return ""
    parent_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parent, "", ""))
    response = _request("PROPFIND", parent_url, cfg, data=PROPFIND_BODY,
                        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"})
    status = response["status"]
    if status in (200, 207):
        return "。父目录是存在的 —— 目标目录还没建：点「立即上传」我们会自动创建，或在网盘里手动新建"
    if status == 404:
        return "。父目录也不存在，地址多半写错了" + _host_hint(cfg)
    return ""


def test_connection(conn: sqlite3.Connection) -> dict:
    """用已保存的配置测试连接。"""
    return probe(config(conn))


def maybe_remote_backup(conn: sqlite3.Connection) -> dict | None:
    """定时线程调它：没配 / 关掉 / 没到间隔都返回 None（幂等）。

    按「上次尝试时间」节流：失败也会退避一个间隔，不会一直打远端。
    """
    cfg = config(conn)
    if not cfg["url"] or cfg["interval_hours"] <= 0:
        return None
    last = _last_at(conn)
    if last is not None and now() - last < timedelta(hours=cfg["interval_hours"]):
        return None
    return run_upload(conn, trigger="auto")
