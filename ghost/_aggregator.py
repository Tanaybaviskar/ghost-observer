"""
ghost/_aggregator.py

Builds and maintains per-function FunctionProfile objects from raw event tuples.

The aggregator is called by the FlushThread after each SQLite write, so it
always reflects a superset of what is persisted.  It also provides
rebuild_from_db() so `ghost report` can reconstruct profiles from an old
session without re-running the app.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FunctionProfile:
    fn_key: str

    call_count: int = 0
    exception_count: int = 0

    # {canonical_arg_sig: count}  e.g. {"(int, str)": 42}
    arg_type_dist: dict[str, int] = field(default_factory=dict)
    # {return_type_qualname: count}
    ret_type_dist: dict[str, int] = field(default_factory=dict)
    # {caller_fn_key: count}
    callers: dict[str, int] = field(default_factory=dict)
    # {callee_fn_key: count}  — populated from callee's caller_key
    callees: dict[str, int] = field(default_factory=dict)

    # Latency: tracked between matching call/return pairs
    total_latency_ns: int = 0
    min_latency_ns: Optional[int] = None
    max_latency_ns: Optional[int] = None
    _in_flight: dict[str, int] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ #

    @property
    def exception_rate(self) -> float:
        if self.call_count == 0:
            return 0.0
        return self.exception_count / self.call_count

    @property
    def mean_latency_ns(self) -> Optional[float]:
        completed = self.call_count - len(self._in_flight)
        if completed <= 0:
            return None
        return self.total_latency_ns / completed

    def dominant_arg_sig(self) -> str:
        if not self.arg_type_dist:
            return "()"
        return max(self.arg_type_dist, key=self.arg_type_dist.__getitem__)

    def dominant_return_type(self) -> str:
        if not self.ret_type_dist:
            return "unknown"
        return max(self.ret_type_dist, key=self.ret_type_dist.__getitem__)

    def to_dict(self) -> dict:
        return {
            "fn_key": self.fn_key,
            "call_count": self.call_count,
            "exception_count": self.exception_count,
            "exception_rate": self.exception_rate,
            "arg_type_dist": self.arg_type_dist,
            "ret_type_dist": self.ret_type_dist,
            "callers": self.callers,
            "callees": self.callees,
            "total_latency_ns": self.total_latency_ns,
            "min_latency_ns": self.min_latency_ns,
            "max_latency_ns": self.max_latency_ns,
            "mean_latency_ns": self.mean_latency_ns,
        }


class Aggregator:
    """Thread-safe registry of FunctionProfile objects."""

    def __init__(self) -> None:
        self._profiles: dict[str, FunctionProfile] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest(self, events: list[tuple]) -> None:
        """Update profiles from a batch of raw event tuples."""
        with self._lock:
            for ev in events:
                fn_key, event, arg_types, ret_or_exc, is_exc, ts_ns, caller_key = ev

                # Skip c_* events — we only aggregate pure-Python frames
                if event.startswith("c_"):
                    continue

                p = self._get_or_create(fn_key)

                if event == "call":
                    p.call_count += 1
                    sig = f"({', '.join(arg_types)})"
                    p.arg_type_dist[sig] = p.arg_type_dist.get(sig, 0) + 1
                    if caller_key:
                        p.callers[caller_key] = p.callers.get(caller_key, 0) + 1
                        # Register this fn as a callee of the caller
                        caller_p = self._get_or_create(caller_key)
                        caller_p.callees[fn_key] = caller_p.callees.get(fn_key, 0) + 1
                    # Track in-flight for latency; key by caller_key+ts for recursion safety
                    flight_key = f"{caller_key}:{ts_ns}"
                    p._in_flight[flight_key] = ts_ns

                elif event == "return":
                    if is_exc:
                        # sys.setprofile signals exceptions as return-with-None-arg.
                        # Our hook marks these with is_exception=True.
                        p.exception_count += 1
                        p._in_flight.clear()  # abandon in-flight on exception
                    else:
                        if ret_or_exc:
                            p.ret_type_dist[ret_or_exc] = p.ret_type_dist.get(ret_or_exc, 0) + 1
                        # Close oldest in-flight entry (LIFO approximation)
                        if p._in_flight:
                            oldest_key = next(iter(p._in_flight))
                            call_ts = p._in_flight.pop(oldest_key)
                            latency = ts_ns - call_ts
                            if latency >= 0:
                                p.total_latency_ns += latency
                                if p.min_latency_ns is None or latency < p.min_latency_ns:
                                    p.min_latency_ns = latency
                                if p.max_latency_ns is None or latency > p.max_latency_ns:
                                    p.max_latency_ns = latency

                elif event == "exception":
                    # sys.settrace only — kept for safety
                    p.exception_count += 1
                    p._in_flight.clear()

    def _get_or_create(self, fn_key: str) -> FunctionProfile:
        p = self._profiles.get(fn_key)
        if p is None:
            p = FunctionProfile(fn_key=fn_key)
            self._profiles[fn_key] = p
        return p

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def profiles(self) -> dict[str, FunctionProfile]:
        with self._lock:
            return dict(self._profiles)

    def get(self, fn_key: str) -> FunctionProfile | None:
        with self._lock:
            return self._profiles.get(fn_key)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def flush_to_db(self, conn: sqlite3.Connection, session_id: str) -> None:
        """Upsert all current profiles into the profiles table."""
        with self._lock:
            rows = []
            for p in self._profiles.values():
                rows.append((
                    session_id,
                    p.fn_key,
                    p.call_count,
                    p.exception_count,
                    json.dumps(p.arg_type_dist),
                    json.dumps(p.ret_type_dist),
                    json.dumps(p.callers),
                    json.dumps(p.callees),
                    p.total_latency_ns,
                    p.min_latency_ns,
                    p.max_latency_ns,
                ))
            conn.executemany(
                """INSERT INTO profiles
                   (session_id, fn_key, call_count, exception_count,
                    arg_type_dist, ret_type_dist, callers, callees,
                    total_latency_ns, min_latency_ns, max_latency_ns)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id, fn_key) DO UPDATE SET
                     call_count=excluded.call_count,
                     exception_count=excluded.exception_count,
                     arg_type_dist=excluded.arg_type_dist,
                     ret_type_dist=excluded.ret_type_dist,
                     callers=excluded.callers,
                     callees=excluded.callees,
                     total_latency_ns=excluded.total_latency_ns,
                     min_latency_ns=excluded.min_latency_ns,
                     max_latency_ns=excluded.max_latency_ns
                """,
                rows,
            )
            conn.commit()

    @classmethod
    def rebuild_from_db(cls, conn: sqlite3.Connection,
                        session_id: str) -> "Aggregator":
        """Reconstruct an Aggregator from persisted profile rows."""
        agg = cls()
        cur = conn.execute(
            "SELECT fn_key, call_count, exception_count, arg_type_dist, "
            "ret_type_dist, callers, callees, total_latency_ns, "
            "min_latency_ns, max_latency_ns "
            "FROM profiles WHERE session_id=?",
            (session_id,),
        )
        with agg._lock:
            for row in cur.fetchall():
                (fn_key, call_count, exc_count, arg_dist_j, ret_dist_j,
                 callers_j, callees_j, total_lat, min_lat, max_lat) = row
                p = FunctionProfile(fn_key=fn_key)
                p.call_count = call_count
                p.exception_count = exc_count
                p.arg_type_dist = json.loads(arg_dist_j)
                p.ret_type_dist = json.loads(ret_dist_j)
                p.callers = json.loads(callers_j)
                p.callees = json.loads(callees_j)
                p.total_latency_ns = total_lat
                p.min_latency_ns = min_lat
                p.max_latency_ns = max_lat
                agg._profiles[fn_key] = p
        return agg