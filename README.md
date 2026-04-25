# ghost-observer

**Copilot reads source. Ghost reads runtime.**

`ghost` is a Python package that watches any Python app run and tells engineers things about their code that no static tool can see:

- Which argument types are *actually* passed (vs. what hints say)
- Which functions are never called in production
- Which functions have surprising exception rates
- Which functions are latency outliers vs. their module peers
- How the call graph actually looks at runtime

## Install

```bash
pip install ghost-observer
```

## Quick start

```bash
# Observe a script
ghost run app.py

# View profile for the last session (options: --top 20, --sort latency/calls/exc)
ghost report
ghost report --sort latency --top 10

# Explain a specific function (auto-detects Gemini if GEMINI_API_KEY is set)
ghost explain process_order
ghost explain process_order --backend gemini

# Run all anomaly detectors
# Configurable with: --exc-threshold 0.05, --latency-z 2.5, --script <name>
ghost anomalies

# Compare two sessions
ghost diff <session1> <session2>

# List sessions
ghost sessions

# Clean up old session files (default: 7 days)
ghost clean
ghost clean --older-than 1
ghost clean --dry-run  # Preview what would be deleted without deleting

# Export session profile data (options: --format json/csv)
ghost export
ghost export --format csv -o profile.csv
ghost export 1777123710-6ff2ebb1 --format json
```

## LLM backends

| Backend  | Activation                     |
|----------|-------------------------------|
| Template | Always available (default)    |
| Gemini   | Set `GEMINI_API_KEY` env var  |

## Architecture

```
ghost run app.py
    │
    ├─ sys.setprofile hook (_observer.py)
    │       │  appends (fn_key, event, arg_types, ret_type,
    │       │           is_exc, timestamp_ns, caller_key)
    │       ▼
    │   in-memory buffer (plain list, atomic swap)
    │       │
    │       ▼  every 1–5s (adaptive)
    ├─ FlushThread (_storage.py)
    │       │  writes rows to SQLite (~/.ghost/sessions/<id>.db)
    │       │  feeds Aggregator
    │       ▼
    └─ Aggregator (_aggregator.py)
            │  builds FunctionProfile per fn_key
            ▼
        ghost report / ghost explain / ghost anomalies / ghost diff
```

## Privacy

Ghost captures `type(value).__qualname__`, **never** the value itself.  No user data, secrets, or PII ever enters the buffer.

## Performance

The hot-path hook appends one tuple per event with no I/O, no function calls, and no computation beyond a `type()` call.  Overhead is ~50–100ns per event on modern hardware.