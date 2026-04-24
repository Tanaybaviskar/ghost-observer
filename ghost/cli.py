"""
ghost/cli.py

Entry point for the `ghost` command-line tool.

Step 1: installs the observer hook, runs the target script via runpy,
then prints collected events to stdout.  The stdout printer will be
replaced by the SQLite flush in Step 2.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import click

from ghost._observer import install, uninstall, drain_buffer


# ---------------------------------------------------------------------------
# Formatting helpers (Step 1 only — will move to _aggregator in Step 3)
# ---------------------------------------------------------------------------

_EVENT_SYMBOLS = {
    "call":        "→",
    "return":      "←",
    "exception":   "✗",
    "c_call":      "→c",
    "c_return":    "←c",
    "c_exception": "✗c",
}

_MAX_EVENTS_TO_PRINT = 200  # guard against flooding the terminal


def _fmt_event(ev: tuple) -> str:
    fn_key, event, arg_types, ret_or_exc, is_exc, ts_ns, caller_key = ev
    symbol = _EVENT_SYMBOLS.get(event, "?")
    # fn_key = "module:qualname:lineno" — show just qualname:lineno
    parts = fn_key.split(":", 2)
    short_key = f"{parts[1]}:{parts[2]}" if len(parts) == 3 else fn_key

    detail = ""
    if event == "call" and arg_types:
        detail = f"  args=({', '.join(arg_types)})"
    elif event == "return" and ret_or_exc:
        detail = f"  → {ret_or_exc}"
    elif event in ("exception", "c_exception") and ret_or_exc:
        detail = f"  raised {ret_or_exc}"

    caller_hint = f"  ← {caller_key.split(':')[1]}" if caller_key else ""
    return f"  {symbol:3}  {short_key}{detail}{caller_hint}"


def _print_summary(events: list[tuple]) -> None:
    """Print collected events to stdout (Step 1 implementation)."""
    click.echo(f"\n{'─' * 60}")
    click.echo(f"  Ghost  —  {len(events)} events captured")
    click.echo(f"{'─' * 60}")

    if not events:
        click.echo("  (no user-code events — check that your script calls functions)")
        return

    shown = events[:_MAX_EVENTS_TO_PRINT]
    for ev in shown:
        click.echo(_fmt_event(ev))

    if len(events) > _MAX_EVENTS_TO_PRINT:
        skipped = len(events) - _MAX_EVENTS_TO_PRINT
        click.echo(f"\n  … {skipped} more events (increase _MAX_EVENTS_TO_PRINT to see all)")

    # Quick frequency summary
    click.echo(f"\n{'─' * 60}")
    from collections import Counter
    key_counts: Counter[str] = Counter()
    for ev in events:
        fn_key, event, *_ = ev
        if event == "call":
            parts = fn_key.split(":", 2)
            short = f"{parts[1]}:{parts[2]}" if len(parts) == 3 else fn_key
            key_counts[short] += 1

    click.echo("  call frequency (top 15):")
    for key, count in key_counts.most_common(15):
        bar = "█" * min(count, 40)
        click.echo(f"  {count:>6}  {bar}  {key}")
    click.echo(f"{'─' * 60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """Ghost — runtime behaviour observer for Python apps."""


@cli.command(name="run", context_settings={"ignore_unknown_options": True})
@click.argument("script", type=click.Path(exists=True))
@click.argument("script_args", nargs=-1, type=click.UNPROCESSED)
def run_cmd(script: str, script_args: tuple[str, ...]) -> None:
    """Run SCRIPT under the Ghost observer.

    All arguments after SCRIPT are forwarded to the script unchanged.

      ghost run app.py --port 8080

    """
    script_path = Path(script).resolve()

    # Inject script directory at the front of sys.path so imports work as if
    # you ran `python app.py` directly.
    script_dir = str(script_path.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    # Forward argv so the target script sees sys.argv[0] = script name
    sys.argv = [str(script_path)] + list(script_args)

    # Install hook *before* runpy so we capture the very first frame
    install()

    try:
        runpy.run_path(str(script_path), run_name="__main__")
    except SystemExit:
        # Normal exit — don't re-raise, fall through to summary
        pass
    except Exception as exc:
        click.echo(f"\n[ghost] script raised {type(exc).__name__}: {exc}", err=True)
    finally:
        uninstall()

    events = drain_buffer()
    _print_summary(events)