"""
ghost/_observer.py

The sys.setprofile hook and the in-memory event buffer.

HOT-PATH RULE (enforced by code review, not the interpreter):
    _profile_hook() must only:
        - read frame attributes that are already cached (f_code, f_globals,
          f_locals snapshot for arg types, f_back for caller key)
        - compute type(...).__qualname__ — a C-level slot read, no Python call
        - call _BUFFER.append(tuple) — a single C-level list mutation

    It must NEVER:
        - call any Python-level function
        - do any I/O (print, open, logging)
        - allocate objects beyond the one tuple per event
        - access f_locals for anything other than the argument names listed
          in co_varnames[:co_argcount]

Privacy guarantee: we capture type(value).__qualname__, never value itself.
"""

from __future__ import annotations

import sys
import time
import types
from typing import Any

from ghost._fn_key import frame_key

# ---------------------------------------------------------------------------
# Filter helpers — computed once at observer-install time, not in the hook
# ---------------------------------------------------------------------------

import sysconfig as _sysconfig
import site as _site

_STDLIB_PREFIX: tuple[str, ...] = tuple(
    p for p in (
        _sysconfig.get_path("stdlib"),
        _sysconfig.get_path("platstdlib"),
    )
    if p
)

_SITE_PREFIXES: tuple[str, ...] = tuple(
    p for p in getattr(_site, "getsitepackages", lambda: [])()
    + [getattr(_site, "getusersitepackages", lambda: "")()]
    if p
)

_SKIP_PREFIXES: tuple[str, ...] = _STDLIB_PREFIX + _SITE_PREFIXES


def _is_user_frame(frame: types.FrameType) -> bool:
    """Return True iff this frame belongs to user code.

    Checks the source file path, not the module name, so it works for
    __main__ scripts, installed packages alike.
    """
    filename: str = frame.f_code.co_filename
    # Built-in frames have no real filename
    if filename.startswith("<"):
        return False
    for prefix in _SKIP_PREFIXES:
        if filename.startswith(prefix):
            return False
    return True


# ---------------------------------------------------------------------------
# Event buffer
# ---------------------------------------------------------------------------

# Each element is a raw tuple; schema documented below.
# Swapped atomically in the background thread (Step 2).
_BUFFER: list[tuple] = []

# Event tuple schema (index → meaning):
#   0  fn_key        str   — "<module>:<qualname>:<firstlineno>"
#   1  event         str   — "call" | "return" | "c_call" | "c_return" | "c_exception" | "exception"
#   2  arg_types     tuple[str, ...]  — type qualnames of positional args (calls only)
#   3  return_type   str | None       — type qualname of return value (returns only)
#   4  is_exception  bool             — True on exception events
#   5  timestamp_ns  int              — time.monotonic_ns()
#   6  caller_key    str | None       — fn_key of the calling frame (one level up)

_EMPTY_TYPES: tuple[()] = ()  # singleton to avoid allocating an empty tuple each call


# ---------------------------------------------------------------------------
# The hook — must stay lean
# ---------------------------------------------------------------------------

def _profile_hook(frame: types.FrameType, event: str, arg: Any) -> None:
    """sys.setprofile callback.

    Called by CPython for every function call and return in the interpreter.
    This function is the innermost bottleneck of Ghost; every nanosecond
    counts.  Do not add any logic here without benchmarking first.
    """
    # Fast-path filter: skip non-user frames immediately
    if not _is_user_frame(frame):
        return

    ts = time.monotonic_ns()
    key = frame_key(frame)

    # Caller key — f_back is already set by CPython before the hook fires
    caller = frame.f_back
    caller_key: str | None = frame_key(caller) if (caller and _is_user_frame(caller)) else None

    if event == "call":
        # Capture arg types from positional arguments only.
        # We read f_locals (a snapshot dict CPython materialises on demand)
        # only for the names listed in co_varnames[:co_argcount].
        # We never read the *values*, only type(value).__qualname__.
        code = frame.f_code
        n = code.co_argcount
        if n:
            local_snap = frame.f_locals  # one dict snapshot
            arg_types: tuple[str, ...] = tuple(
                type(local_snap[name]).__qualname__
                for name in code.co_varnames[:n]
                if name in local_snap
            )
        else:
            arg_types = _EMPTY_TYPES

        _BUFFER.append((key, "call", arg_types, None, False, ts, caller_key))

    elif event == "return":
        ret_type: str | None = type(arg).__qualname__ if arg is not None else "NoneType"
        _BUFFER.append((key, "return", _EMPTY_TYPES, ret_type, False, ts, caller_key))

    elif event == "exception":
        # arg is (exc_type, exc_value, traceback)
        exc_type_name: str = arg[0].__qualname__ if arg and arg[0] else "UnknownException"
        _BUFFER.append((key, "exception", _EMPTY_TYPES, exc_type_name, True, ts, caller_key))

    # c_call / c_return / c_exception: C extension frames.
    # arg is the C function object itself.  We record the name but cannot
    # inspect arguments (they live on the C stack, not in f_locals).
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
    """Atomically swap out the current buffer and return its contents.

    Safe to call from a background thread: list assignment is atomic in
    CPython (the GIL protects the swap).  Events written between the
    read of _BUFFER and the assignment are lost — acceptable for a
    profiler; correctness is statistical, not transactional.
    """
    global _BUFFER
    old, _BUFFER = _BUFFER, []
    return old