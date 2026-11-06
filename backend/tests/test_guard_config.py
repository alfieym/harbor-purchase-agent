import logging

import pytest
from pydantic import ValidationError

from harbor.core.config import Settings
from harbor.guardrails.events import GuardMetrics, log_guard_event


def test_guard_defaults_are_safe() -> None:
    s = Settings(_env_file=None)
    assert s.guardrails_enabled is True
    assert s.guard_daily_token_budget == 500_000
    assert s.guard_max_retries == 1
    assert s.guard_request_timeout_seconds == 20.0


def test_request_budget_must_fit_inside_turn() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            # 关掉派发，否则 75s 的默认批预算会先一步触发另一条校验，
            # 这条测试就测不到 guard 的那条了。
            dispatch_enabled=False,
            turn_timeout_seconds=30.0,
            guard_request_timeout_seconds=20.0,
            guard_max_retries=2,
        )


def test_metrics_count_and_snapshot() -> None:
    m = GuardMetrics()
    m.incr("content.filtered")
    m.incr("content.filtered", count=2)
    assert m.snapshot() == {"content.filtered": 3}


def test_event_fields_are_whitelisted(caplog) -> None:
    caplog.set_level(logging.INFO)
    log_guard_event("model.fallback", model="lite-1", reason="budget_tier_lite", raw="秘密")
    record = caplog.records[-1]
    assert record.guard["raw"] is None  # 非白名单字段被丢弃
    assert record.guard["model"] == "lite-1"
