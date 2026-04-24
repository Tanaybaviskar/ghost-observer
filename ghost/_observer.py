"""
ghost/_observer.py

The sys.setprofile hook and in-memory event buffer.

HOT-PATH RULE: _profile_hook() must only:
  - read frame attributes already cached by CPython
  - compute type(x).__qualname__  (C-level slot read)
  - call _BUFFER.append(tuple)    (single C-level list mutation)

It must NEVER: call any Python function, do any I/O, allocate beyond
one tuple per event, or read f_locals beyond positional arg names.

Privacy guarantee: we capture type(value).__qualname__, never value itself.
"""
from __future__ import annotations

import site as _site
import sys
import sysconfig as _sysconfig
import time
import types
from typing import Any

from ghost._fn_key import frame_key

# ---------------------------------------------------------------------------
# Filter: pre-compute skip prefixes once at import time
# ---------------------------------------------------------------------------

def _collect_skip_prefixes() -> tuple[str, ...]:
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
    # Normalise separators so Windows paths match
    import os
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
# Buffer
# ---------------------------------------------------------------------------

_BUFFER: list[tuple] = []
_MAX_BUFFER: int = 50_000   # drop events when overflowing; prevents OOM on tight loops

# Singleton empty tuple — avoids allocating a new empty tuple per call-event
_EMPTY_TYPES: tuple[()] = ()

# Event tuple schema  (index → meaning)
# 0  fn_key        str               "<module>:<qualname>:<lineno>"
# 1  event         str               "call"|"return"|"exception"|"c_call"|"c_return"|"c_exception"
# 2  arg_types     tuple[str, ...]   type qualnames of positional args (calls only)
# 3  ret_or_exc    str | None        return type qualname, or exception type name
# 4  is_exception  bool
# 5  timestamp_ns  int               time.monotonic_ns()
# 6  caller_key    str | None        fn_key of the calling frame


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------

def _profile_hook(frame: types.FrameType, event: str, arg: Any) -> None:
    if len(_BUFFER) >= _MAX_BUFFER:
        return
    if not _is_user_frame(frame):
        return

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
        ret_type = type(arg).__qualname__ if arg is not None else "NoneType"
        _BUFFER.append((key, "return", _EMPTY_TYPES, ret_type, False, ts, caller_key))

    elif event == "exception":
        exc_name = arg[0].__qualname__ if (arg and arg[0]) else "UnknownException"
        _BUFFER.append((key, "exception", _EMPTY_TYPES, exc_name, True, ts, caller_key))

    elif event in ("c_call", "c_return", "c_exception"):
        c_name = getattr(arg, "__qualname__", None) or getattr(arg, "__name__", "<c_fn>")
        _BUFFER.append((key, event, _EMPTY_TYPES, c_name, event == "c_exception", ts, caller_key))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install() -> None:
    """Attach the Ghost profile hook to the current thread."""
    sys.setprofile(_profile_hook)


def uninstall() -> None:
    """Remove the Ghost profile hook."""
    sys.setprofile(None)


def drain_buffer() -> list[tuple]:
    """Atomically swap out the buffer and return its contents.

    GIL-safe: list assignment is atomic in CPython.  Safe to call from a
    background thread without a lock.  Events written between the read and
    the assignment are lost — acceptable for a statistical profiler.
    """
    global _BUFFER
    old, _BUFFER = _BUFFER, []
    return old