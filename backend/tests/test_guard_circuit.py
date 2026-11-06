from harbor.guardrails.circuit import CircuitBreaker, CircuitState


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_opens_after_threshold_and_cools_down() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(threshold=3, cooldown_seconds=60, clock=clock)
    for _ in range(3):
        cb.record_failure("m1")
    assert cb.state("m1") is CircuitState.OPEN
    assert cb.allow("m1") is False
    clock.now = 61
    assert cb.allow("m1") is True            # half_open 探测
    cb.record_success("m1")
    assert cb.state("m1") is CircuitState.CLOSED


def test_failed_probe_reopens() -> None:
    clock = FakeClock()
    cb = CircuitBreaker(threshold=1, cooldown_seconds=10, clock=clock)
    cb.record_failure("m1")
    clock.now = 11
    assert cb.allow("m1") is True
    cb.record_failure("m1")
    assert cb.state("m1") is CircuitState.OPEN
    assert cb.allow("m1") is False


def test_models_are_isolated() -> None:
    cb = CircuitBreaker(threshold=1, cooldown_seconds=10, clock=lambda: 0.0)
    cb.record_failure("m1")
    assert cb.allow("m2") is True
