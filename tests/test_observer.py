"""Tests for the observer hot-path and buffer mechanics."""
import importlib.util
import os
import sys
import tempfile
import textwrap

import pytest

from ghost._observer import drain_buffer, install, uninstall
from ghost import _observer


# ---------------------------------------------------------------------------
# Helper: load a snippet from a real temp file so _is_user_frame passes
# ---------------------------------------------------------------------------

def _load_tempmod(src: str, name: str = "ghost_test_tmp"):
    """Write src to a real .py file and import it as a module."""
    tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w",
                                     encoding="utf-8")
    tmp.write(textwrap.dedent(src))
    tmp.close()
    spec = importlib.util.spec_from_file_location(name, tmp.name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, tmp.name


def _clear():
    _observer._BUFFER.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_install_uninstall():
    install()
    assert sys.getprofile() is not None
    uninstall()
    assert sys.getprofile() is None


def test_events_captured():
    mod, path = _load_tempmod("""
        def target(x, y):
            return str(x) + y
    """)
    _clear()
    install()
    mod.target(1, "hello")
    uninstall()

    events = drain_buffer()
    fn_keys = [e[0] for e in events]
    event_types = [e[1] for e in events]

    assert any("target" in k for k in fn_keys), f"target not in {fn_keys}"
    assert "call" in event_types
    assert "return" in event_types
    os.unlink(path)


def test_arg_types_captured_not_values():
    mod, path = _load_tempmod("""
        def typed_fn(a, b):
            pass
    """)
    _clear()
    install()
    mod.typed_fn(42, "secret_password")
    uninstall()

    events = drain_buffer()
    call_events = [e for e in events if e[1] == "call" and "typed_fn" in e[0]]
    assert call_events, "No call event for typed_fn"
    arg_types = call_events[0][2]

    assert "int" in arg_types
    assert "str" in arg_types
    # The actual value must never appear anywhere in the buffer
    all_text = str(events)
    assert "secret_password" not in all_text, "Value leaked into event buffer!"
    os.unlink(path)


def test_exception_event():
    mod, path = _load_tempmod("""
        def raises():
            raise RuntimeError("boom")
    """)
    _clear()
    install()
    try:
        mod.raises()
    except RuntimeError:
        pass
    finally:
        uninstall()

    events = drain_buffer()
    # sys.setprofile signals exceptions as "return" events with arg=None,
    # which our hook marks as is_exception=True (index 4).
    exc_events = [e for e in events if e[4] is True]
    assert exc_events, (
        f"No exception event recorded.\n"
        f"Events: {events}\n"
        f"Note: exceptions appear as return events with is_exception=True"
    )
    os.unlink(path)


def test_drain_is_atomic():
    mod, path = _load_tempmod("""
        def simple():
            pass
    """)
    _clear()
    install()
    mod.simple()
    uninstall()

    first = drain_buffer()
    second = drain_buffer()

    assert len(first) > 0
    assert len(second) == 0
    os.unlink(path)


def test_buffer_max_cap():
    mod, path = _load_tempmod("""
        def spam():
            pass
    """)
    original_max = _observer._MAX_BUFFER
    _observer._MAX_BUFFER = 5
    _clear()
    install()
    for _ in range(50):
        mod.spam()
    uninstall()

    events = drain_buffer()
    # Allow a few extra for the install/uninstall frames themselves
    assert len(events) <= 10, f"Buffer exceeded max: {len(events)}"
    _observer._MAX_BUFFER = original_max
    os.unlink(path)