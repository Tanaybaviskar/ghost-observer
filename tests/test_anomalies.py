"""Tests for anomaly detectors."""
import pytest

from ghost._aggregator import Aggregator, FunctionProfile
from ghost.anomalies import (
    detect_high_exc_rate,
    detect_latency_outliers,
    detect_type_mismatches,
)


def _simple_agg(fn_key: str, **kwargs) -> Aggregator:
    agg = Aggregator()
    p = FunctionProfile(fn_key=fn_key, **kwargs)
    agg._profiles[fn_key] = p
    return agg


def test_high_exc_rate_detected():
    agg = _simple_agg(
        "mymod:bad_fn:10",
        call_count=10,
        exception_count=6,
    )
    results = detect_high_exc_rate(agg, threshold=0.05)
    assert len(results) == 1
    assert results[0].kind == "high_exc_rate"
    assert results[0].severity == "high"


def test_high_exc_rate_below_threshold():
    agg = _simple_agg(
        "mymod:ok_fn:10",
        call_count=100,
        exception_count=3,
    )
    results = detect_high_exc_rate(agg, threshold=0.05)
    assert len(results) == 0


def test_latency_outlier_detected():
    agg = Aggregator()
    # Create 5 fast functions and 1 slow one in same module
    for i in range(5):
        p = FunctionProfile(fn_key=f"mymod:fast_{i}:{i+1}")
        p.call_count = 10
        p.total_latency_ns = 10 * 1_000_000   # 1ms mean
        p.min_latency_ns = 900_000
        p.max_latency_ns = 1_100_000
        agg._profiles[p.fn_key] = p

    slow = FunctionProfile(fn_key="mymod:slow_fn:99")
    slow.call_count = 10
    slow.total_latency_ns = 10 * 500_000_000  # 500ms mean — very slow
    slow.min_latency_ns = 490_000_000
    slow.max_latency_ns = 510_000_000
    agg._profiles[slow.fn_key] = slow

    results = detect_latency_outliers(agg, z_threshold=2.5)
    assert any(r.fn_key == "mymod:slow_fn:99" for r in results)


def test_no_false_positive_low_sample():
    """Functions with call_count < 3 should not trigger exc rate detector."""
    agg = _simple_agg("mymod:rare:1", call_count=2, exception_count=2)
    results = detect_high_exc_rate(agg)
    assert len(results) == 0