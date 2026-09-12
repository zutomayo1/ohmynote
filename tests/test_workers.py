"""后台线程的接线与生命周期。

回归重点：`app/main.py` 里那两个守护线程以前**只定义了函数、从来没有被调用**，
所以「每 30 分钟自动跟进向量索引」「每小时自动备份」这两句话是假的、且没有任何用例拦得住。
这里盯死三件事：lifespan 真的启动了它们、shutdown 真的回收了它们、单次出错不会让线程死掉。
"""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from app import main


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _live_worker_names() -> set[str]:
    return {t.name for t in threading.enumerate() if t.name.startswith("inknote-")}


def _drain_workers() -> None:
    main._stop_workers()
    _wait_until(lambda: not _live_worker_names(), timeout=3.0)


# ---------------------------------------------------------------------------
# lifespan 接线
# ---------------------------------------------------------------------------
def test_lifespan_starts_workers_and_stops_them_on_exit(monkeypatch):
    """进 lifespan 起线程、出 lifespan 停线程（不漏线程，也不留空转的）。"""
    monkeypatch.setenv("INKNOTE_WORKERS", "1")
    calls: list[str] = []
    # 把两次 tick 换掉：只验证「被调度到了」，不去真连 embedding / 真做备份。
    monkeypatch.setattr(main, "_tick_index", lambda: calls.append("index"))
    monkeypatch.setattr(main, "_tick_backup", lambda: calls.append("backup"))

    _drain_workers()
    try:
        with TestClient(main.app) as client:
            assert client.get("/health").status_code == 200
            assert _live_worker_names() >= {"inknote-index", "inknote-backup"}
            # 索引线程起来就先跑一次；备份线程要先等满一小时，不该立刻跑。
            assert _wait_until(lambda: "index" in calls), "索引线程没有立刻跑第一次"
            assert "backup" not in calls, "备份线程不该在启动时立刻跑"
        # 退出 lifespan 后两个线程都要被通知停掉
        assert _wait_until(lambda: not _live_worker_names()), f"线程没退出：{_live_worker_names()}"
    finally:
        _drain_workers()


def test_workers_can_be_switched_off(monkeypatch):
    """INKNOTE_WORKERS=0 时一个线程都不该起（测试默认就靠这个保持确定性）。"""
    monkeypatch.setenv("INKNOTE_WORKERS", "0")
    _drain_workers()
    main._start_workers()
    assert not _live_worker_names()
    assert main._workers_enabled() is False
    main._start_workers()  # 幂等，再来一次也不该有动静
    assert not _live_worker_names()


def test_workers_are_off_by_default_under_pytest():
    """conftest 里设了 INKNOTE_WORKERS=0，测试环境默认必须是关的。"""
    assert main._workers_enabled() is False


# ---------------------------------------------------------------------------
# 线程健壮性
# ---------------------------------------------------------------------------
def test_worker_survives_a_failing_tick():
    """单次 tick 抛异常不能把线程弄死（否则索引会永久停更且无人察觉）。"""
    _drain_workers()
    hits: list[int] = []

    def boom() -> None:
        hits.append(1)
        raise RuntimeError("故意炸一次")

    main._spawn_worker("inknote-unit-boom", 0.01, boom, run_immediately=True)
    try:
        assert _wait_until(lambda: len(hits) >= 3), "抛异常后线程没有继续跑"
    finally:
        _drain_workers()


def test_stop_workers_is_idempotent():
    """_stop_workers 可以被反复调用（shutdown 可能走多条路径）。"""
    main._stop_workers()
    main._stop_workers()
    assert main._WORKER_STOPS == []
