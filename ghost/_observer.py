"""
ghost/_observer.py

sys.setprofile + sys.settrace hooks and in-memory event buffer.

HOT-PATH RULE: _profile_hook() must only append one tuple per event.
No I/O, no Python function calls, no allocation beyond the tuple itself.

EXCEPTION DETECTION
-------------------
sys.setprofile does not fire "exception" events — only sys.settrace does.
Both `return None` and a raised exception produce a setprofile "return"
with arg=None, making them indistinguishable at the profile level alone.

Strategy: install a minimal settrace hook that records which code objects
are actively propagating an exception into a per-thread set (_raising).
The profile hook checks this set on every "return" event.

INSTALL-TIME FALSE POSITIVE FIX
---------------------------------
sys.settrace fires a trace event on the frame that CALLED sys.settrace
(i.e. the frame calling install()). If that frame happens to have an
active exception at that moment (e.g. inside a try/except in cli.py),
its code object gets added to _raising as a false positive.

Fix: we record the code object of the install() caller at install time
and permanently exclude it from _raising. Additionally we clear _raising
inside the profile hook on the very first event after install, ensuring
any transient false flags from the settrace activation are wiped before
any real data is recorded.

Privacy: we capture type(value).__qualname__, never value itself.
"""
from __future__ import annotations

import site as _site
import sys
import sysconfig as _sysconfig
import threading
import time
import types
from typing import Any

from ghost._fn_key import frame_key

# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

def _collect_skip_prefixes() -> tuple[str, ...]:
    import os
    prefixes: list[str] = []
    for key in ("stdlib", "platstdlib"):
        p = _sysconfig.get_path(key)
        if p:
            prefixes.append(p)
    try:
        prefixes.extend(_site.getsitepackages())
    except AttributeError:
        pass
    try:
        u = _site.getusersitepackages()
        if u:
            prefixes.append(u)
    except AttributeError:
        pass
    return tuple(os.path.normcase(p) for p in prefixes if p)


_SKIP_PREFIXES: tuple[str, ...] = _collect_skip_prefixes()
import os as _os


def _is_user_frame(frame: types.FrameType) -> bool:
    filename: str = frame.f_code.co_filename
    if filename.startswith("<"):
        return False
    norm = _os.path.normcase(filename)
    for prefix in _SKIP_PREFIXES:
        if norm.startswith(prefix):
            return False
    return True


# ---------------------------------------------------------------------------
# Exception tracking
# ---------------------------------------------------------------------------

_thread_local = threading.local()

# Code objects to permanently exclude from exception tracking.
# Populated by install() with the caller's code object to prevent
# the settrace-activation false positive.
_excluded_codes: set[types.CodeType] = set()


def _raising() -> set:
    s = getattr(_thread_local, "raising", None)
    if s is None:
        s = set()
        _thread_local.raising = s
    return s


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------

_BUFFER: list[tuple] = []
_MAX_BUFFER: int = 50_000
_EMPTY_TYPES: tuple[()] = ()

# One-shot flag: clear _raising on the first profile event after install,
# to wipe any false positives from settrace activation.
_thread_local_clear_on_next: threading.local = threading.local()


def _should_clear() -> bool:
    return getattr(_thread_local_clear_on_next, "pending", False)


def _set_clear_pending() -> None:
    _thread_local_clear_on_next.pending = True


def _clear_done() -> None:
    _thread_local_clear_on_next.pending = False


# Event tuple schema
# 0  fn_key        str
# 1  event         str   "call"|"return"|"c_call"|"c_return"|"c_exception"
# 2  arg_types     tuple[str, ...]
# 3  ret_or_exc    str | None
# 4  is_exception  bool
# 5  timestamp_ns  int
# 6  caller_key    str | None


# ---------------------------------------------------------------------------
# Trace hook — exception detection only
# ---------------------------------------------------------------------------

def _trace_hook(frame: types.FrameType, event: str, arg: Any) -> Any:
    if event == "exception" and _is_user_frame(frame):
        code = frame.f_code
        if code not in _excluded_codes:
            _raising().add(code)
    return _trace_hook


# ---------------------------------------------------------------------------
# Profile hook
# ---------------------------------------------------------------------------

def _profile_hook(frame: types.FrameType, event: str, arg: Any) -> None:
    if len(_BUFFER) >= _MAX_BUFFER:
        return
    if not _is_user_frame(frame):
        return

    # One-shot clear: wipe any false positives from settrace activation
    if _should_clear():
        _raising().clear()
        _clear_done()

    ts = time.monotonic_ns()
    key = frame_key(frame)

    caller = frame.f_back
    caller_key: str | None = (
        frame_key(caller) if (caller and _is_user_frame(caller)) else None
    )

    if event == "call":
        code = frame.f_code
        n = code.co_argcount
        if n:
            local_snap = frame.f_locals
            arg_types: tuple[str, ...] = tuple(
                type(local_snap[name]).__qualname__
                for name in code.co_varnames[:n]
                if name in local_snap
            )
        else:
            arg_types = _EMPTY_TYPES
        _BUFFER.append((key, "call", arg_types, None, False, ts, caller_key))

    elif event == "return":
        raising = _raising()
        code = frame.f_code
        pending_exc = code in raising
        if pending_exc:
            raising.discard(code)

        if pending_exc and arg is None:
            _BUFFER.append((key, "return", _EMPTY_TYPES, None, True, ts, caller_key))
        else:
            ret_type = type(arg).__qualname__ if arg is not None else "NoneType"
            _BUFFER.append((key, "return", _EMPTY_TYPES, ret_type, False, ts, caller_key))

    elif event in ("c_call", "c_return", "c_exception"):
        c_name = getattr(arg, "__qualname__", None) or getattr(arg, "__name__", "<c_fn>")
        _BUFFER.append((key, event, _EMPTY_TYPES, c_name, event == "c_exception", ts, caller_key))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install() -> None:
    """Attach Ghost hooks. Must be called from the frame you want to observe."""
    # Exclude the caller's code object from exception tracking — settrace
    # fires an exception event on the caller frame during activation.
    caller_frame = sys._getframe(1)
    _excluded_codes.add(caller_frame.f_code)

    sys.setprofile(_profile_hook)
    sys.settrace(_trace_hook)

    # Schedule a one-shot clear on the next profile event to wipe any
    # remaining false positives from settrace activation.
    _set_clear_pending()


def uninstall() -> None:
    """Remove Ghost hooks."""
    sys.settrace(None)
    sys.setprofile(None)
    _raising().clear()
    _excluded_codes.clear()


def drain_buffer() -> list[tuple]:
    """Atomically swap out the buffer. GIL-safe in CPython."""
    global _BUFFER
    old, _BUFFER = _BUFFER, []
    return old