import threading
from datetime import date
from pathlib import Path

from harbor.guardrails.budget import BudgetTier, SqliteTokenBudget


def _budget(tmp_path: Path, cap: int = 1000) -> SqliteTokenBudget:
    return SqliteTokenBudget(tmp_path / "b.sqlite3", cap)


def test_tier_boundaries(tmp_path) -> None:
    b = _budget(tmp_path)
    assert b.tier() is BudgetTier.MAIN
    b.acquire(700); b.settle(700, 700)
    assert b.tier() is BudgetTier.LITE          # 30%
    b.acquire(200); b.settle(200, 200)
    assert b.tier() is BudgetTier.MINIMAL       # 10%
    b.acquire(60); b.settle(60, 60)
    assert b.tier() is BudgetTier.FALLBACK      # 4%
    assert b.turn_allowed() is False


def test_acquire_refuses_past_cap(tmp_path) -> None:
    b = _budget(tmp_path)
    assert b.acquire(900).granted is True
    assert b.acquire(200).granted is False


def test_settle_and_release(tmp_path) -> None:
    b = _budget(tmp_path)
    a = b.acquire(300)
    b.settle(a.reserved, 120)
    assert b.used_today() == 120
    b2 = b.acquire(100)
    b.release(b2.reserved)
    assert b.used_today() == 120


def test_concurrent_acquire_does_not_oversell(tmp_path) -> None:
    # 两个真线程同时抢同一份额度：BEGIN IMMEDIATE 必须让其中一个看到另一个的结果。
    b = _budget(tmp_path, cap=1000)
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def grab() -> None:
        barrier.wait()
        granted = b.acquire(600).granted
        with lock:
            results.append(granted)

    threads = [threading.Thread(target=grab) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]


def test_day_rollover_resets(tmp_path) -> None:
    today = [date(2026, 11, 6)]
    b = SqliteTokenBudget(tmp_path / "b.sqlite3", 1000, clock=lambda: today[0])
    b.acquire(900); b.settle(900, 900)
    assert b.used_today() == 900
    today[0] = date(2026, 11, 7)
    assert b.used_today() == 0
    assert b.tier() is BudgetTier.MAIN


def test_unwritable_db_fails_open(tmp_path, caplog) -> None:
    # 目录不存在 → sqlite 打不开。S11-17：预算只是节流，不能挡住正常对话。
    b = SqliteTokenBudget(tmp_path / "missing" / "b.sqlite3", 1000)
    assert b.acquire(10).granted is True
    assert b.used_today() == 0
    assert b.tier() is BudgetTier.MAIN
    assert b.turn_allowed() is True
