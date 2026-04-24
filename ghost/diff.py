"""
ghost/diff.py

Compare two Ghost sessions and surface meaningful changes.

`ghost diff <session1> <session2>` shows:
  - Functions added or removed between sessions
  - Call-count changes (regressions / improvements)
  - Exception-rate changes
  - Latency changes
  - Type-distribution shifts (new types appeared or disappeared)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ghost._aggregator import Aggregator, FunctionProfile
from ghost._fn_key import parse_key


@dataclass
class DiffEntry:
    fn_key: str
    kind: str          # "added" | "removed" | "changed"
    changes: list[str]

    def __str__(self) -> str:
        _, qualname, lineno = parse_key(self.fn_key)
        header = f"  {self.kind.upper():8}  {qualname} (line {lineno})"
        body = "\n".join(f"             {c}" for c in self.changes)
        return f"{header}\n{body}" if body else header


def diff_sessions(agg1: Aggregator, agg2: Aggregator) -> list[DiffEntry]:
    """Compare two aggregators and return a list of DiffEntry objects."""
    p1 = agg1.profiles()
    p2 = agg2.profiles()

    keys1 = set(p1)
    keys2 = set(p2)

    entries: list[DiffEntry] = []

    # Functions only in session 1 (removed)
    for key in sorted(keys1 - keys2):
        entries.append(DiffEntry(fn_key=key, kind="removed",
                                 changes=[f"called {p1[key].call_count}× in session 1"]))

    # Functions only in session 2 (added)
    for key in sorted(keys2 - keys1):
        entries.append(DiffEntry(fn_key=key, kind="added",
                                 changes=[f"called {p2[key].call_count}× in session 2"]))

    # Functions in both — diff their profiles
    for key in sorted(keys1 & keys2):
        a, b = p1[key], p2[key]
        changes: list[str] = []

        # Call count
        if a.call_count != b.call_count:
            delta = b.call_count - a.call_count
            sign = "+" if delta > 0 else ""
            pct = (delta / a.call_count * 100) if a.call_count else float("inf")
            changes.append(
                f"call count: {a.call_count} → {b.call_count}  ({sign}{delta}, {sign}{pct:.0f}%)"
            )

        # Exception rate
        er1, er2 = a.exception_rate, b.exception_rate
        if abs(er1 - er2) > 0.02:
            arrow = "↑" if er2 > er1 else "↓"
            changes.append(
                f"exception rate: {er1:.1%} → {er2:.1%}  {arrow}"
            )

        # Latency
        m1, m2 = a.mean_latency_ns, b.mean_latency_ns
        if m1 is not None and m2 is not None:
            ratio = m2 / m1 if m1 else float("inf")
            if ratio > 1.2 or ratio < 0.8:
                arrow = "↑ slower" if ratio > 1 else "↓ faster"
                changes.append(
                    f"mean latency: {m1/1e6:.2f}ms → {m2/1e6:.2f}ms  ({ratio:.2f}×) {arrow}"
                )

        # New arg types that appeared in session 2
        new_sigs = set(b.arg_type_dist) - set(a.arg_type_dist)
        for sig in sorted(new_sigs):
            changes.append(f"new arg signature observed: {sig}")

        # Arg types that disappeared
        gone_sigs = set(a.arg_type_dist) - set(b.arg_type_dist)
        for sig in sorted(gone_sigs):
            changes.append(f"arg signature no longer seen: {sig}")

        if changes:
            entries.append(DiffEntry(fn_key=key, kind="changed", changes=changes))

    return entries


def format_diff(entries: list[DiffEntry], session1: str, session2: str) -> str:
    lines = [
        f"Ghost diff  {session1[:12]}  →  {session2[:12]}",
        "─" * 60,
    ]
    if not entries:
        lines.append("  No meaningful differences detected between sessions.")
        return "\n".join(lines)

    added   = [e for e in entries if e.kind == "added"]
    removed = [e for e in entries if e.kind == "removed"]
    changed = [e for e in entries if e.kind == "changed"]

    for section, items in (("added", added), ("removed", removed), ("changed", changed)):
        if items:
            lines.append(f"\n  ── {section} ({len(items)}) ──")
            for entry in items:
                lines.append(str(entry))

    lines.append(f"\n{'─' * 60}")
    lines.append(
        f"  {len(added)} added  ·  {len(removed)} removed  ·  {len(changed)} changed"
    )
    return "\n".join(lines)