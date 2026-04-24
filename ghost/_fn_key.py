"""
ghost/_fn_key.py

Produces a stable string key for any CPython frame.

Design constraints:
- Must be cheap: called on every profile event.
- Must be deterministic: same function always produces the same key.
- Must be unique across the entire process, including reloaded modules.

Key format:  "<module>:<qualname>:<firstlineno>"

The firstlineno component disambiguates functions with the same qualname in
the same module (e.g. closures, nested classes rebuilt at runtime).
"""

from __future__ import annotations

import types


def frame_key(frame: types.FrameType) -> str:
    """Return a stable key string for *frame*.

    This is called in the hot path, so it must do no I/O and no attribute
    lookups beyond what CPython caches on the frame / code object.
    """
    code = frame.f_code
    # f_globals["__name__"] is set by the import system and cached in the
    # frame's globals dict — one dict lookup, no function call.
    module: str = frame.f_globals.get("__name__") or "<unknown>"
    return f"{module}:{code.co_qualname}:{code.co_firstlineno}"


def code_key(code: types.CodeType, module: str) -> str:
    """Same key but from a code object + explicit module name.

    Used during aggregation when we don't have a live frame.
    """
    return f"{module}:{code.co_qualname}:{code.co_firstlineno}"