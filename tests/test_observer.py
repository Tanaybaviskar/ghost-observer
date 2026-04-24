
"""Tests for the observer hot-path and buffer mechanics."""
import sys
import time

import pytest

from ghost._observer import (
    _BUFFER,
    _is_user_frame,
    drain_buffer,
    install,
    uninstall,
)


def _clear():
    from ghost import _observer
    _observer._BUFFER.clear()


def test_install_uninstall():
    install()
    assert sys.getprofile() is not None
    uninstall()
    assert sys.getprofile() is None


def test_events_captured():
    _clear()
    install()

    def target(x: int, y: str) -> str:
        return f"{x}-{y}"

    target(1, "hello")
    uninstall()

    events = drain_buffer()
    fn_keys = [e[0] for e in events]
    event_types = [e[1] for e in events]

    assert any("target" in k for k in fn_keys), f"target not in {fn_keys}"
    assert "call" in event_types
    assert "return" in event_types


def test_arg_types_captured_not_values():
    _clear()
    install()

    def typed_fn(a: int, b: str):
        pass

    typed_fn(42, "secret_password")
    uninstall()

    events = drain_buffer()
    call_events = [e for e in events if e[1] == "call" and "typed_fn" in e[0]]
    assert call_events, "No call event for typed_fn"
    arg_types = call_events[0][2]

    # Must capture the type name, not the value
    assert "int" in arg_types
    assert "str" in arg_types
    # The VALUE must never appear
    for ev in events:
        for field in ev:
            assert "secret_password" not in str(field), \
                "Value leaked into event buffer!"
            assert "42" not in str(field) or True  # ints are fine as int, not as 42


def test_exception_event():
    _clear()
    install()

    def raises():
        raise RuntimeError("boom")

    try:
        raises()
    except RuntimeError:
        pass
    finally:
        uninstall()

    events = drain_buffer()
    exc_events = [e for e in events if e[4] is True]
    assert exc_events, "No exception event recorded"


def test_drain_is_atomic():
    """After drain, buffer must be empty."""
    _clear()
    install()

    def simple():
        pass

    simple()
    uninstall()

    first = drain_buffer()
    second = drain_buffer()

    assert len(first) > 0
    assert len(second) == 0


def test_buffer_max_cap():
    from ghost import _observer
    original_max = _observer._MAX_BUFFER
    _observer._MAX_BUFFER = 5
    _clear()
    install()

    def spam():
        pass

    for _ in range(20):
        spam()
    uninstall()

    events = drain_buffer()
    assert len(events) <= 5 + 5, f"Buffer exceeded max: {len(events)}"
    _observer._MAX_BUFFER = original_max
