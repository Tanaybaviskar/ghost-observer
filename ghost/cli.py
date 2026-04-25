"""
ghost/cli.py

Entry point for all `ghost` CLI commands:

  ghost run <script> [args...]   — observe a Python script
  ghost report [session]         — show aggregated profile for a session
  ghost explain <fn_pattern>     — explain a specific function
  ghost anomalies [session]      — run all anomaly detectors
  ghost diff <session1> <session2> — compare two sessions
  ghost sessions                 — list recorded sessions
"""
from __future__ import annotations

import os
import runpy
import sys
import time
import uuid
from pathlib import Path

import click

from ghost._aggregator import Aggregator
from ghost._fn_key import parse_key
from ghost._observer import install, uninstall
from ghost._storage import (
    FlushThread,
    close_session,
    insert_session,
    list_sessions,
    open_db,
    session_db_path,
)
from ghost.anomalies import run_all as run_anomalies
from ghost.diff import diff_sessions, format_diff
from ghost.explainer.registry import get_explainer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_session(session_id: str | None) -> str:
    """Return session_id, defaulting to the most recent session."""
    if session_id:
        return session_id
    sessions = list_sessions()
    if not sessions:
        raise click.ClickException(
            "No sessions found. Run `ghost run <script>` first."
        )
    return sessions[0]


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


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option()
def cli() -> None:
    """Ghost — runtime behaviour observer for Python apps.

    Copilot reads source.  Ghost reads runtime.
    """


# ---------------------------------------------------------------------------
# ghost run
# ---------------------------------------------------------------------------

@cli.command(name="run", context_settings={"ignore_unknown_options": True})
@click.argument("script", type=click.Path(exists=True))
@click.argument("script_args", nargs=-1, type=click.UNPROCESSED)
@click.option("--session-id", default=None, hidden=True,
              help="Override the auto-generated session ID (for testing).")
def run_cmd(script: str, script_args: tuple[str, ...],
            session_id: str | None) -> None:
    """Run SCRIPT under the Ghost observer and record the session.

    All arguments after SCRIPT are forwarded unchanged:

      ghost run app.py --port 8080

    """
    script_path = str(Path(script).resolve())
    sid = session_id or f"{int(time.time())}-{uuid.uuid4().hex[:8]}"

    # Insert script dir at front of sys.path (mirrors `python script.py`)
    script_dir = str(Path(script_path).parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    sys.argv = [script_path] + list(script_args)

    conn = open_db(sid)
    insert_session(conn, sid, script_path, os.getpid())

    agg = Aggregator()
    flusher = FlushThread(conn, sid, aggregator=agg)
    flusher.start()

    click.echo(f"[ghost] session {sid}")
    click.echo(f"[ghost] observing {script_path}")

    install()
    try:
        runpy.run_path(script_path, run_name="__main__")
    except SystemExit:
        pass
    except Exception as exc:
        click.echo(f"\n[ghost] script raised {type(exc).__name__}: {exc}", err=True)
    finally:
        uninstall()

    total = flusher.stop()
    agg.flush_to_db(conn, sid)
    close_session(conn, sid, total)
    conn.close()

    click.echo(f"[ghost] {total:,} events  ·  session saved → {session_db_path(sid)}")


# ---------------------------------------------------------------------------
# ghost sessions
# ---------------------------------------------------------------------------

@cli.command(name="sessions")
def sessions_cmd() -> None:
    """List all recorded Ghost sessions (newest first)."""
    import sqlite3 as _sq3
    sessions = list_sessions()
    if not sessions:
        click.echo("No sessions found.")
        return
    click.echo(f"\n{'#':>3}  {'SESSION ID':32}  {'SCRIPT':30}  {'EVENTS':>7}  {'DATE'}")
    click.echo("─" * 90)
    for i, sid in enumerate(sessions, 1):
        path = session_db_path(sid)
        try:
            c = _sq3.connect(str(path))
            row = c.execute(
                "SELECT script_path, total_events, started_at FROM sessions WHERE session_id=?",
                (sid,)
            ).fetchone()
            c.close()
            if row:
                import os as _os, datetime as _dt
                script = _os.path.basename(row[0])
                events = row[1] or 0
                started = _dt.datetime.fromtimestamp(row[2] / 1000).strftime("%m-%d %H:%M")
                click.echo(f"{i:>3}  {sid:32}  {script:30}  {events:>7}  {started}")
            else:
                click.echo(f"{i:>3}  {sid:32}  (no metadata)")
        except Exception:
            click.echo(f"{i:>3}  {sid:32}  (unreadable)")
    click.echo()


# ---------------------------------------------------------------------------
# ghost report
# ---------------------------------------------------------------------------

@cli.command(name="report")
@click.argument("session", required=False, default=None)
@click.option("--top", default=20, show_default=True,
              help="Number of functions to show.")
@click.option("--sort", default="calls",
              type=click.Choice(["calls", "latency", "exceptions"]),
              help="Sort key.")
def report_cmd(session: str | None, top: int, sort: str) -> None:
    """Show aggregated profile for a session.

    SESSION defaults to the most recent session.
    """
    sid = _resolve_session(session)
    conn = open_db(sid, read_only=True)
    agg = Aggregator.rebuild_from_db(conn, sid)
    conn.close()

    profiles = list(agg.profiles().values())
    if not profiles:
        click.echo("No profile data found for this session.")
        return

    # Filter out ghost's own internals and top-level module frames
    profiles = [
        p for p in profiles
        if not p.fn_key.startswith("ghost.")
        and not p.fn_key.startswith("ghost._")
        and "<module>" not in p.fn_key
    ]

    if sort == "calls":
        profiles.sort(key=lambda p: -p.call_count)
    elif sort == "latency":
        profiles.sort(key=lambda p: -(p.mean_latency_ns or 0))
    elif sort == "exceptions":
        profiles.sort(key=lambda p: -p.exception_rate)

    profiles = profiles[:top]

    click.echo(f"\nGhost report  —  session {sid[:20]}")
    click.echo("─" * 80)
    click.echo(
        f"{'FUNCTION':45} {'CALLS':>7} {'EXC%':>6} {'MEAN LAT':>10} {'DOM. ARG SIG'}"
    )
    click.echo("─" * 80)

    for p in profiles:
        _, qualname, lineno = parse_key(p.fn_key)
        name_col = f"{qualname}:{lineno}"[:45]
        exc_pct = f"{p.exception_rate:.0%}" if p.call_count > 0 else "—"
        mean_lat = _ns_to_human(p.mean_latency_ns)
        dom_sig = p.dominant_arg_sig()[:30]
        click.echo(
            f"{name_col:45} {p.call_count:>7,} {exc_pct:>6} {mean_lat:>10}  {dom_sig}"
        )

    click.echo("─" * 80)
    total_user = len([p for p in agg.profiles().values()
                      if not p.fn_key.startswith("ghost.")
                      and not p.fn_key.startswith("ghost._")
                      and "<module>" not in p.fn_key])
    click.echo(f"  Showing {len(profiles)} of {total_user} user functions.\n")


# ---------------------------------------------------------------------------
# ghost explain
# ---------------------------------------------------------------------------

@cli.command(name="explain")
@click.argument("fn_pattern")
@click.argument("session", required=False, default=None)
@click.option("--backend", default=None,
              type=click.Choice(["template", "gemini"]),
              help="Force a specific explainer backend.")
def explain_cmd(fn_pattern: str, session: str | None, backend: str | None) -> None:
    """Explain a function's runtime behaviour.

    FN_PATTERN is matched against qualnames (substring match).
    SESSION defaults to the most recent session.

    Examples:

      ghost explain process_order

      ghost explain DataProcessor.process

      ghost explain add --backend template
    """
    sid = _resolve_session(session)
    conn = open_db(sid)
    agg = Aggregator.rebuild_from_db(conn, sid)
    conn.close()

    # Find matching profiles
    pattern = fn_pattern.lower()
    matches = [
        p for p in agg.profiles().values()
        if pattern in p.fn_key.lower()
    ]

    if not matches:
        raise click.ClickException(
            f"No function matching {fn_pattern!r} found in session {sid[:20]}.\n"
            f"Try `ghost report` to see available functions."
        )

    if len(matches) > 1:
        click.echo(f"Multiple matches for {fn_pattern!r}:")
        for m in matches[:10]:
            _, qualname, lineno = parse_key(m.fn_key)
            click.echo(f"  {qualname}:{lineno}  ({m.call_count:,} calls)")
        click.echo("\nBe more specific or use the full qualname.")
        return

    explainer = get_explainer(backend)
    click.echo(f"\n[ghost explain]  backend={explainer.name}\n")
    click.echo(explainer.explain(matches[0]))
    click.echo()


# ---------------------------------------------------------------------------
# ghost anomalies
# ---------------------------------------------------------------------------

@cli.command(name="anomalies")
@click.argument("session", required=False, default=None)
@click.option("--exc-threshold", default=0.05, show_default=True,
              help="Exception rate threshold (0–1).")
@click.option("--latency-z", default=2.5, show_default=True,
              help="Z-score threshold for latency outlier detection.")
@click.option("--script", default=None,
              help="Path to original script for never-called detection.")
def anomalies_cmd(session: str | None, exc_threshold: float,
                  latency_z: float, script: str | None) -> None:
    """Run all anomaly detectors against a session.

    SESSION defaults to the most recent session.
    """
    sid = _resolve_session(session)
    conn = open_db(sid, read_only=True)
    agg = Aggregator.rebuild_from_db(conn, sid)

    # Try to find the script path from session metadata if not provided
    if script is None:
        row = conn.execute(
            "SELECT script_path FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        if row:
            script = row[0]

    conn.close()

    click.echo(f"\nGhost anomalies  —  session {sid[:20]}")
    click.echo("─" * 60)

    results = run_anomalies(
        agg,
        script_path=script or "",
        exc_threshold=exc_threshold,
        latency_z=latency_z,
    )

    if not results:
        click.echo("  No anomalies detected.")
    else:
        counts = {"high": 0, "medium": 0, "low": 0}
        for a in results:
            counts[a.severity] = counts.get(a.severity, 0) + 1
            click.echo(str(a))
            click.echo()
        click.echo("─" * 60)
        click.echo(
            f"  {len(results)} anomalies  ·  "
            f"{counts['high']} high  {counts['medium']} medium  {counts['low']} low\n"
        )


# ---------------------------------------------------------------------------
# ghost diff
# ---------------------------------------------------------------------------

@cli.command(name="diff")
@click.argument("session1")
@click.argument("session2")
def diff_cmd(session1: str, session2: str) -> None:
    """Compare two sessions side by side.

    SESSION1 and SESSION2 are session IDs (from `ghost sessions`).
    """
    for sid in (session1, session2):
        path = session_db_path(sid)
        if not path.exists():
            raise click.ClickException(f"Session not found: {sid}")

    conn1 = open_db(session1)
    conn2 = open_db(session2)
    agg1 = Aggregator.rebuild_from_db(conn1, session1)
    agg2 = Aggregator.rebuild_from_db(conn2, session2)
    conn1.close()
    conn2.close()

    entries = diff_sessions(agg1, agg2)
    click.echo(format_diff(entries, session1, session2))