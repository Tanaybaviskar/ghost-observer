"""
ghost/_fn_key.py

Stable, cheap string key for any CPython frame or code object.

Key format:  "<module>:<qualname>:<firstlineno>"

The firstlineno component disambiguates closures and nested classes that
share a qualname in the same module.  Called on every profile event so it
must do no I/O and no Python-level function calls.
"""
from __future__ import annotations

import types


def frame_key(frame: types.FrameType) -> str:
    """Return a stable key string for *frame*.  Hot-path safe."""
    code = frame.f_code
    module: str = frame.f_globals.get("__name__") or "<unknown>"
    return f"{module}:{code.co_qualname}:{code.co_firstlineno}"


def code_key(code: types.CodeType, module: str) -> str:
    """Same key built from a code object + explicit module name."""
    return f"{module}:{code.co_qualname}:{code.co_firstlineno}"


def parse_key(key: str) -> tuple[str, str, int]:
    """Split a fn_key back into (module, qualname, firstlineno)."""
    parts = key.split(":", 2)
    if len(parts) != 3:
        return ("<unknown>", key, 0)
    try:
        return parts[0], parts[1], int(parts[2])
    except ValueError:
        return parts[0], parts[1], 0