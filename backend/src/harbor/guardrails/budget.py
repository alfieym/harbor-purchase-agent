"""Daily, global token budget (spec §5.5).

Two-phase accounting: ``acquire`` reserves before the call, ``settle`` corrects
the reservation to the provider's reported usage, ``release`` gives it back
when the call failed without usage. Storage is SQLite with ``BEGIN IMMEDIATE``
so two threads cannot oversell the same day.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator

from harbor.guardrails.events import log_guard_event

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_tokens (
    day  TEXT PRIMARY KEY,
    used INTEGER NOT NULL DEFAULT 0
)
"""


class BudgetTier(StrEnum):
    MAIN = "main"
    LITE = "lite"
    MINIMAL = "minimal"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class Admission:
    granted: bool
    reserved: int
    tier: BudgetTier
    remaining_ratio: float


def _tier_for(ratio: float) -> BudgetTier:
    # Spec §5.5: >50% main, (20%, 50%] lite, (5%, 20%] minimal, <=5% fallback.
    if ratio > 0.5:
        return BudgetTier.MAIN
    if ratio > 0.2:
        return BudgetTier.LITE
    if ratio > 0.05:
        return BudgetTier.MINIMAL
    return BudgetTier.FALLBACK


class SqliteTokenBudget:
    """One row per local day, shared by every buyer in the process."""

    def __init__(
        self,
        path: Path,
        daily_cap: int,
        *,
        chars_per_token: float = 2.0,
        safety_factor: float = 1.2,
        clock: Callable[[], date] = date.today,
    ) -> None:
        self._path = Path(path)
        self.daily_cap = daily_cap
        self._chars_per_token = chars_per_token
        self._safety_factor = safety_factor
        self._clock = clock
        self._degraded = False
        self._init_schema()

    # ---------------------------------------------------------------- storage

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(SCHEMA)
        except sqlite3.Error:
            self._degrade()

    def _degrade(self) -> None:
        if self._degraded:
            return
        self._degraded = True
        # Budget is throttling, not a safety boundary: log once, then get out
        # of the way and let the conversation continue (S11-17).
        log_guard_event("budget.unavailable", reason="storage_error")

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _day(self) -> str:
        return self._clock().isoformat()

    # ------------------------------------------------------------- accounting

    def used_today(self) -> int:
        if self._degraded:
            return 0
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT used FROM daily_tokens WHERE day = ?", (self._day(),)
                ).fetchone()
        except sqlite3.Error:
            self._degrade()
            return 0
        return int(row[0]) if row else 0

    def remaining_ratio(self) -> float:
        return max(0.0, (self.daily_cap - self.used_today()) / self.daily_cap)

    def tier(self) -> BudgetTier:
        if self._degraded:
            return BudgetTier.MAIN
        return _tier_for(self.remaining_ratio())

    def turn_allowed(self) -> bool:
        return self.tier() is not BudgetTier.FALLBACK

    def estimate(self, messages: Sequence[Any]) -> int:
        chars = sum(len(_message_text(message)) for message in messages)
        estimate = math.ceil(chars / self._chars_per_token * self._safety_factor)
        return max(1, estimate)

    def acquire(self, estimated_tokens: int) -> Admission:
        if self._degraded:
            return Admission(True, 0, BudgetTier.MAIN, 1.0)
        day = self._day()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO daily_tokens(day, used) VALUES (?, 0)",
                        (day,),
                    )
                    used = int(
                        conn.execute(
                            "SELECT used FROM daily_tokens WHERE day = ?", (day,)
                        ).fetchone()[0]
                    )
                    ratio = max(0.0, (self.daily_cap - used) / self.daily_cap)
                    if estimated_tokens + used > self.daily_cap or ratio <= 0.05:
                        self._rollback(conn)
                        return Admission(False, 0, _tier_for(ratio), ratio)
                    conn.execute(
                        "UPDATE daily_tokens SET used = used + ? WHERE day = ?",
                        (estimated_tokens, day),
                    )
                    conn.execute("COMMIT")
                except sqlite3.Error:
                    self._rollback(conn)
                    raise
        except sqlite3.Error:
            self._degrade()
            return Admission(True, 0, BudgetTier.MAIN, 1.0)
        new_used = used + estimated_tokens
        new_ratio = max(0.0, (self.daily_cap - new_used) / self.daily_cap)
        return Admission(True, estimated_tokens, _tier_for(new_ratio), new_ratio)

    def settle(self, reserved: int, actual: int) -> None:
        if self._degraded:
            return
        self._apply_delta(actual - reserved)

    def release(self, reserved: int) -> None:
        if self._degraded:
            return
        self._apply_delta(-reserved)

    def _apply_delta(self, delta: int) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE daily_tokens SET used = MAX(0, used + ?) WHERE day = ?",
                    (delta, self._day()),
                )
        except sqlite3.Error:
            self._degrade()


def _message_text(message: Any) -> str:
    """Best-effort text of one message, without importing AgentScope here."""
    getter = getattr(message, "get_text_content", None)
    if callable(getter):
        try:
            return getter() or ""
        except Exception:  # noqa: BLE001 - a malformed message must not break accounting
            return ""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            text = getattr(block, "text", None)
            parts.append(text if isinstance(text, str) else str(block))
        return "".join(parts)
    return str(content)
