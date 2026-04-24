"""
ghost/anomalies.py

Four anomaly detectors that operate on a completed Aggregator.

1. TypeMismatch     — observed arg/return types conflict with PEP-484 hints
2. NeverCalled      — function exists in source but call_count == 0
3. HighExcRate      — exception_rate > threshold (default 5%)
4. LatencyOutlier   — function's mean latency is > N std-devs above module avg

Each detector returns a list of Anomaly dataclass instances.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ghost._aggregator import Aggregator, FunctionProfile
from ghost._fn_key import parse_key


@dataclass
class Anomaly:
    kind: str            # "type_mismatch" | "never_called" | "high_exc_rate" | "latency_outlier"
    fn_key: str
    severity: str        # "low" | "medium" | "high"
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        module, qualname, lineno = parse_key(self.fn_key)
        loc = f"{module}.{qualname} (line {lineno})"
        return f"[{self.severity.upper():6}] {self.kind:20}  {loc}\n           {self.message}"


# ---------------------------------------------------------------------------
# 1. Type-hint mismatch
# ---------------------------------------------------------------------------

def _get_hints(fn_key: str) -> dict[str, type] | None:
    """Try to resolve PEP-484 hints for fn_key.  Returns None if unavailable."""
    module_name, qualname, _ = parse_key(fn_key)
    try:
        mod = sys.modules.get(module_name)
        if mod is None:
            return None
        # Navigate dotted qualname (handles nested classes and closures)
        obj: Any = mod
        for part in qualname.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return None
        hints = {}
        try:
            hints = {
                k: v for k, v in (getattr(obj, "__annotations__", {}) or {}).items()
                if isinstance(v, type)
            }
        except Exception:
            pass
        return hints or None
    except Exception:
        return None


def detect_type_mismatches(agg: Aggregator) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    for fn_key, profile in agg.profiles().items():
        if profile.call_count == 0:
            continue
        hints = _get_hints(fn_key)
        if not hints:
            continue

        module, qualname, lineno = parse_key(fn_key)
        # Build positional param name list from hints (exclude 'return', 'self', 'cls')
        param_names = [
            k for k in hints if k not in ("return", "self", "cls")
        ]

        # Check arg type distribution
        for sig, count in profile.arg_type_dist.items():
            # sig looks like "(int, str)"
            observed = [t.strip() for t in sig.strip("()").split(",") if t.strip()]
            for i, obs_type_name in enumerate(observed):
                if i >= len(param_names):
                    break
                param = param_names[i]
                expected_type = hints.get(param)
                if expected_type is None:
                    continue
                if obs_type_name != expected_type.__qualname__:
                    rate = count / profile.call_count
                    severity = "high" if rate > 0.5 else "medium" if rate > 0.1 else "low"
                    anomalies.append(Anomaly(
                        kind="type_mismatch",
                        fn_key=fn_key,
                        severity=severity,
                        message=(
                            f"param '{param}' annotated as {expected_type.__qualname__} "
                            f"but observed {obs_type_name!r} in {count}/{profile.call_count} calls "
                            f"({rate:.0%})"
                        ),
                        detail={
                            "param": param,
                            "expected": expected_type.__qualname__,
                            "observed": obs_type_name,
                            "count": count,
                            "rate": rate,
                        },
                    ))

        # Check return type
        ret_hint = hints.get("return")
        if ret_hint and profile.ret_type_dist:
            for obs_ret, count in profile.ret_type_dist.items():
                if obs_ret != ret_hint.__qualname__:
                    rate = count / profile.call_count
                    if rate > 0.05:
                        anomalies.append(Anomaly(
                            kind="type_mismatch",
                            fn_key=fn_key,
                            severity="medium" if rate > 0.3 else "low",
                            message=(
                                f"return annotated as {ret_hint.__qualname__} "
                                f"but observed {obs_ret!r} in {count}/{profile.call_count} calls "
                                f"({rate:.0%})"
                            ),
                            detail={
                                "param": "return",
                                "expected": ret_hint.__qualname__,
                                "observed": obs_ret,
                                "count": count,
                                "rate": rate,
                            },
                        ))

    return anomalies


# ---------------------------------------------------------------------------
# 2. Never-called functions
# ---------------------------------------------------------------------------

def _collect_defined_functions(script_path: str) -> list[str]:
    """Parse a script with AST and return qualnames of all defined functions."""
    try:
        src = Path(script_path).read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception:
        return []
    names: list[str] = []
    _walk_ast_fns(tree, [], names)
    return names


def _walk_ast_fns(node: ast.AST, stack: list[str], out: list[str]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.append(child.name)
            out.append(".".join(stack))
            _walk_ast_fns(child, stack, out)
            stack.pop()
        elif isinstance(child, ast.ClassDef):
            stack.append(child.name)
            _walk_ast_fns(child, stack, out)
            stack.pop()
        else:
            _walk_ast_fns(child, stack, out)


def detect_never_called(agg: Aggregator, script_path: str) -> list[Anomaly]:
    defined = set(_collect_defined_functions(script_path))
    called_qualnames: set[str] = set()
    for fn_key in agg.profiles():
        _, qualname, _ = parse_key(fn_key)
        called_qualnames.add(qualname)

    anomalies: list[Anomaly] = []
    for qualname in defined:
        if qualname not in called_qualnames:
            anomalies.append(Anomaly(
                kind="never_called",
                fn_key=f"<script>:{qualname}:0",
                severity="low",
                message=f"'{qualname}' is defined but was never called during this session",
                detail={"qualname": qualname},
            ))
    return anomalies


# ---------------------------------------------------------------------------
# 3. High exception rate
# ---------------------------------------------------------------------------

def detect_high_exc_rate(
    agg: Aggregator, threshold: float = 0.05
) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    for fn_key, profile in agg.profiles().items():
        if profile.call_count < 3:          # ignore low-sample functions
            continue
        rate = profile.exception_rate
        if rate > threshold:
            severity = "high" if rate > 0.5 else "medium" if rate > 0.2 else "low"
            anomalies.append(Anomaly(
                kind="high_exc_rate",
                fn_key=fn_key,
                severity=severity,
                message=(
                    f"exception rate {rate:.1%} "
                    f"({profile.exception_count}/{profile.call_count} calls) "
                    f"exceeds threshold {threshold:.0%}"
                ),
                detail={
                    "rate": rate,
                    "exception_count": profile.exception_count,
                    "call_count": profile.call_count,
                    "threshold": threshold,
                },
            ))
    return anomalies


# ---------------------------------------------------------------------------
# 4. Latency outlier
# ---------------------------------------------------------------------------

def detect_latency_outliers(
    agg: Aggregator, z_threshold: float = 2.5
) -> list[Anomaly]:
    """Flag functions whose mean latency is > z_threshold std-devs above module mean."""
    # Group by module
    by_module: dict[str, list[tuple[str, float]]] = {}
    for fn_key, profile in agg.profiles().items():
        mean = profile.mean_latency_ns
        if mean is None or profile.call_count < 2:
            continue
        module, _, _ = parse_key(fn_key)
        by_module.setdefault(module, []).append((fn_key, mean))

    anomalies: list[Anomaly] = []
    for module, entries in by_module.items():
        if len(entries) < 3:
            continue
        latencies = [lat for _, lat in entries]
        mu = sum(latencies) / len(latencies)
        variance = sum((x - mu) ** 2 for x in latencies) / len(latencies)
        sigma = math.sqrt(variance)
        if sigma == 0:
            continue
        for fn_key, mean_lat in entries:
            z = (mean_lat - mu) / sigma
            if z > z_threshold:
                severity = "high" if z > 4.0 else "medium"
                anomalies.append(Anomaly(
                    kind="latency_outlier",
                    fn_key=fn_key,
                    severity=severity,
                    message=(
                        f"mean latency {mean_lat/1e6:.2f}ms is {z:.1f}σ above "
                        f"module average {mu/1e6:.2f}ms"
                    ),
                    detail={
                        "mean_latency_ns": mean_lat,
                        "module_mean_ns": mu,
                        "module_sigma_ns": sigma,
                        "z_score": z,
                    },
                ))
    return anomalies


# ---------------------------------------------------------------------------
# Unified runner
# ---------------------------------------------------------------------------

def run_all(
    agg: Aggregator,
    script_path: str = "",
    exc_threshold: float = 0.05,
    latency_z: float = 2.5,
) -> list[Anomaly]:
    results: list[Anomaly] = []
    results.extend(detect_type_mismatches(agg))
    if script_path:
        results.extend(detect_never_called(agg, script_path))
    results.extend(detect_high_exc_rate(agg, threshold=exc_threshold))
    results.extend(detect_latency_outliers(agg, z_threshold=latency_z))
    # Sort: high → medium → low, then by fn_key
    sev_order = {"high": 0, "medium": 1, "low": 2}
    results.sort(key=lambda a: (sev_order.get(a.severity, 9), a.fn_key))
    return results