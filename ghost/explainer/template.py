
"""
ghost/explainer/template.py

Pure f-string template explainer — no external dependencies, no network calls.
Always available as the fallback backend.
"""
from __future__ import annotations

from ghost._aggregator import FunctionProfile
from ghost._fn_key import parse_key
from ghost.explainer.base import BaseExplainer


def _ns_to_human(ns: float | None) -> str:
    if ns is None:
        return "n/a"
    if ns < 1_000:
        return f"{ns:.0f}ns"
    if ns < 1_000_000:
        return f"{ns/1_000:.1f}µs"
    if ns < 1_000_000_000:
        return f"{ns/1_000_000:.2f}ms"
    return f"{ns/1_000_000_000:.3f}s"


class TemplateExplainer(BaseExplainer):
    @property
    def name(self) -> str:
        return "template"

    def explain(self, profile: FunctionProfile) -> str:
        module, qualname, lineno = parse_key(profile.fn_key)
        lines: list[str] = [
            f"Function: {qualname}  (module: {module}, first defined at line {lineno})",
            "─" * 60,
        ]

        lines.append(f"  Calls observed   : {profile.call_count:,}")
        lines.append(f"  Exceptions raised: {profile.exception_count:,}  "
                     f"({profile.exception_rate:.1%} rate)")

        lines.append(f"  Latency (mean)   : {_ns_to_human(profile.mean_latency_ns)}")
        lines.append(f"  Latency (min/max): "
                     f"{_ns_to_human(profile.min_latency_ns)} / "
                     f"{_ns_to_human(profile.max_latency_ns)}")

        if profile.arg_type_dist:
            lines.append("\n  Argument type distribution (positional):")
            for sig, count in sorted(profile.arg_type_dist.items(),
                                     key=lambda x: -x[1]):
                pct = count / profile.call_count * 100
                bar = "█" * min(int(pct / 5), 20)
                lines.append(f"    {count:>6}  {bar:20}  {pct:5.1f}%  {sig}")

        if profile.ret_type_dist:
            lines.append("\n  Return type distribution:")
            for rtype, count in sorted(profile.ret_type_dist.items(),
                                       key=lambda x: -x[1]):
                pct = count / profile.call_count * 100
                lines.append(f"    {count:>6}  {pct:5.1f}%  {rtype}")

        if profile.callers:
            lines.append("\n  Top callers:")
            for caller, count in sorted(profile.callers.items(),
                                        key=lambda x: -x[1])[:5]:
                _, caller_qualname, _ = parse_key(caller)
                lines.append(f"    {count:>6}×  {caller_qualname}")

        if profile.callees:
            lines.append("\n  Functions called from here (top 5):")
            for callee, count in sorted(profile.callees.items(),
                                        key=lambda x: -x[1])[:5]:
                _, callee_qualname, _ = parse_key(callee)
                lines.append(f"    {count:>6}×  {callee_qualname}")

        return "\n".join(lines)