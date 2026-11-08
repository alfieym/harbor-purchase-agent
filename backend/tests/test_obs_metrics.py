"""S13 metrics registry: counters, gauges and rolling-window histograms."""

from harbor.observability.metrics import MetricRegistry, summarize


def test_counters_gauges_and_histograms() -> None:
    registry = MetricRegistry()
    registry.incr("turns", 2)
    registry.incr("turns")
    registry.gauge("sessions", 3)
    for value in (10, 20, 30, 40):
        registry.observe("latency_ms", value)

    snapshot = registry.snapshot()

    assert snapshot["counters"]["turns"] == 3
    assert snapshot["gauges"]["sessions"] == 3
    assert snapshot["histograms"]["latency_ms"] == {
        "count": 4,
        "min": 10,
        "max": 40,
        "mean": 25.0,
        "p50": 20,
        "p95": 40,
        "p99": 40,
    }


def test_histogram_window_keeps_the_latest_observations() -> None:
    registry = MetricRegistry(window=3)
    for value in range(1, 6):
        registry.observe("latency_ms", value)

    summary = registry.snapshot()["histograms"]["latency_ms"]

    assert summary["count"] == 3
    assert summary["min"] == 3
    assert summary["max"] == 5
    assert summary["p50"] == 4


def test_summarize_uses_nearest_rank() -> None:
    assert summarize([1]) == {
        "count": 1,
        "min": 1,
        "max": 1,
        "mean": 1.0,
        "p50": 1,
        "p95": 1,
        "p99": 1,
    }
    assert summarize([1, 2, 3, 4, 5])["p95"] == 5


def test_snapshot_is_a_copy() -> None:
    registry = MetricRegistry()
    registry.incr("turns")
    snapshot = registry.snapshot()
    snapshot["counters"]["turns"] = 99

    assert registry.snapshot()["counters"]["turns"] == 1
