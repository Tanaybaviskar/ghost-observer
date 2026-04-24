"""Tests for the Aggregator."""
import pytest

from ghost._aggregator import Aggregator


def _make_event(fn_key, event, arg_types=(), ret=None, is_exc=False, ts=1000, caller=None):
    return (fn_key, event, arg_types, ret, is_exc, ts, caller)


def test_call_count():
    agg = Aggregator()
    events = [
        _make_event("mod:fn:1", "call", ("int",), ts=100),
        _make_event("mod:fn:1", "return", ret="int", ts=200),
        _make_event("mod:fn:1", "call", ("str",), ts=300),
        _make_event("mod:fn:1", "return", ret="str", ts=400),
    ]
    agg.ingest(events)
    p = agg.get("mod:fn:1")
    assert p is not None
    assert p.call_count == 2


def test_exception_rate():
    agg = Aggregator()
    events = [
        _make_event("mod:fn:1", "call", ts=100),
        _make_event("mod:fn:1", "exception", is_exc=True, ret="ValueError", ts=150),
        _make_event("mod:fn:1", "call", ts=200),
        _make_event("mod:fn:1", "return", ret="int", ts=250),
    ]
    agg.ingest(events)
    p = agg.get("mod:fn:1")
    assert p.exception_count == 1
    assert p.exception_rate == pytest.approx(0.5)


def test_latency_tracking():
    agg = Aggregator()
    events = [
        _make_event("mod:fn:1", "call", ts=1_000_000, caller="mod:caller:1"),
        _make_event("mod:fn:1", "return", ret="int", ts=6_000_000),
    ]
    agg.ingest(events)
    p = agg.get("mod:fn:1")
    assert p.mean_latency_ns == pytest.approx(5_000_000)
    assert p.min_latency_ns == 5_000_000
    assert p.max_latency_ns == 5_000_000


def test_caller_callee_graph():
    agg = Aggregator()
    events = [
        _make_event("mod:caller:1", "call", ts=100),
        _make_event("mod:callee:5", "call", ts=110, caller="mod:caller:1"),
        _make_event("mod:callee:5", "return", ret="int", ts=120),
        _make_event("mod:caller:1", "return", ret="int", ts=130),
    ]
    agg.ingest(events)
    callee_p = agg.get("mod:callee:5")
    assert "mod:caller:1" in callee_p.callers
    caller_p = agg.get("mod:caller:1")
    assert "mod:callee:5" in caller_p.callees


def test_arg_type_distribution():
    agg = Aggregator()
    events = [
        _make_event("mod:fn:1", "call", ("int", "str"), ts=100),
        _make_event("mod:fn:1", "return", ret="bool", ts=110),
        _make_event("mod:fn:1", "call", ("float", "str"), ts=200),
        _make_event("mod:fn:1", "return", ret="bool", ts=210),
        _make_event("mod:fn:1", "call", ("int", "str"), ts=300),
        _make_event("mod:fn:1", "return", ret="bool", ts=310),
    ]
    agg.ingest(events)
    p = agg.get("mod:fn:1")
    assert p.arg_type_dist["(int, str)"] == 2
    assert p.arg_type_dist["(float, str)"] == 1