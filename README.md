# ghost-observer

**Copilot reads source. Ghost reads runtime.**

Ghost is a zero-config Python runtime observer. Point it at any script and it tells you things no static tool can: which argument types are *actually* passed, which functions are truly never called, which functions silently raise exceptions, and which ones are latency outliers.

```
pip install ghost-observer
ghost run app.py
ghost report
```

---

## How it works

```
ghost run app.py
    │
    ├─ sys.setprofile hook        ← captures every call/return, ~50ns overhead
    ├─ sys.settrace hook          ← exception detection only
    │       │
    │       ▼  appends tuple per event (types only, never values)
    │   in-memory buffer
    │       │
    │       ▼  background thread, every 1–5s (adaptive)
    ├─ SQLite  ~/.ghost/sessions/<id>.db
    │       │
    │       ▼
    └─ Aggregator → FunctionProfile per function
            │
            ▼
    ghost report / explain / anomalies / diff / watch
```

**Privacy guarantee:** Ghost captures `type(value).__qualname__`, never the value itself. No secrets, PII, or user data ever enters the buffer.

**Performance:** The hot-path hook appends one tuple per event using `time.perf_counter_ns()`. No I/O, no function calls, no computation. Overhead is ~50–100ns per event.

---

## Install

```bash
pip install ghost-observer

# With Gemini AI explanations
pip install ghost-observer[gemini]
```

---

## Quick start

```bash
# Observe any script
ghost run app.py

# With arguments forwarded to your script
ghost run app.py --port 8080 --debug

# View the profile
ghost report
ghost report --sort latency
ghost report --sort exceptions
```

---

## All commands

### `ghost run`

Runs a script under the Ghost observer and saves the session to `~/.ghost/sessions/`.

```bash
ghost run app.py
ghost run app.py --port 8080 --workers 4
```

All arguments after the script name are forwarded unchanged.

---

### `ghost report`

Shows an aggregated profile table for a session.

```bash
ghost report                      # most recent session
ghost report --sort latency       # slowest functions first
ghost report --sort exceptions    # highest exception rate first
ghost report --top 30             # show more rows
ghost report <session-id>         # specific session
```

**Columns:**

| Column | Meaning |
|---|---|
| `FUNCTION` | `qualname:line_number` |
| `CALLS` | Total call count |
| `EXC%` | % of calls that raised an exception |
| `MEAN LAT` | Average time from call to return |
| `DOM. ARG SIG` | Most common argument types seen at runtime |

---

### `ghost explain`

Deep-dives on a single function. Substring-matches qualnames.

```bash
ghost explain process_order
ghost explain DataProcessor.process
ghost explain add --backend template    # force template, skip AI
```

With Gemini:

```bash
# Windows PowerShell
$env:GEMINI_API_KEY = "your-key"

# macOS/Linux
export GEMINI_API_KEY=your-key

ghost explain process_order             # auto-detects key, uses Gemini
```

Output includes: call count, exception rate, latency (mean/min/max), argument type distribution with bar chart, return type distribution, top callers, top callees.

---

### `ghost anomalies`

Runs all four anomaly detectors and reports findings sorted by severity.

```bash
ghost anomalies
ghost anomalies --exc-threshold 0.10    # raise bar to 10%
ghost anomalies --latency-z 3.0         # stricter latency outlier threshold
ghost anomalies --script app.py         # enable never-called detection
```

**Four detectors:**

| Detector | What it finds |
|---|---|
| `type_mismatch` | Observed arg/return types conflict with PEP-484 annotations |
| `never_called` | Functions defined in source but never called (requires `--script`) |
| `high_exc_rate` | Exception rate exceeds threshold (default 5%) |
| `latency_outlier` | Mean latency is >2.5σ above the module average |

---

### `ghost diff`

Compares two sessions and surfaces meaningful changes.

```bash
ghost sessions                          # find session IDs
ghost diff <session-id-1> <session-id-2>
```

Reports: added/removed functions, call count changes, exception rate changes, latency regressions, new argument type signatures.

---

### `ghost watch`

Live-updating terminal report. Refreshes every N seconds by re-reading the SQLite DB.

```bash
ghost watch                             # watch most recent session
ghost watch --interval 1                # refresh every 1s
ghost watch --sort latency
ghost watch --top 30
ghost watch <session-id>                # watch a specific session
```

Works on active sessions (while your app is running) and completed ones. Press `Ctrl+C` to exit.

**Typical server workflow:**

```bash
# Terminal 1
ghost run server.py

# Terminal 2 — hit some endpoints, then watch
curl http://localhost:8080/api/orders
ghost watch --sort latency
```

---

### `ghost sessions`

Lists all recorded sessions, newest first.

```bash
ghost sessions
```

Output: session ID, script name, event count, start time.

---

### `ghost export`

Exports session profiles as JSON or CSV for use in other tools.

```bash
ghost export                            # JSON to stdout
ghost export --format csv -o profile.csv
ghost export <session-id> --format json -o session.json
```

JSON output schema:

```json
{
  "session_id": "...",
  "profiles": [
    {
      "fn_key": "myapp:process_order:42",
      "call_count": 1500,
      "exception_count": 12,
      "exception_rate": 0.008,
      "mean_latency_ns": 2400000,
      "min_latency_ns": 800000,
      "max_latency_ns": 15000000,
      "arg_type_dist": {"(Order, User)": 1488, "(Order, NoneType)": 12},
      "ret_type_dist": {"bool": 1500}
    }
  ]
}
```

---

### `ghost clean`

Deletes old sessions to free disk space.

```bash
ghost clean                             # delete sessions older than 7 days
ghost clean --older-than 30
ghost clean --older-than 1 --dry-run    # preview without deleting
```

---

## LLM backends

| Backend | Activation |
|---|---|
| Template | Always available (default) |
| Gemini | Set `GEMINI_API_KEY` environment variable |

The Gemini backend injects all runtime facts (call counts, types, latency, callers) into the prompt as grounded data. The model cannot hallucinate beyond what Ghost actually observed.

---

## Sessions

Sessions are stored in `~/.ghost/sessions/<session-id>.db` as SQLite databases. Each DB contains:

- `sessions` — metadata (script path, pid, start/end time, total events)
- `events` — raw event tuples (fn_key, event type, arg types, return type, is_exception, timestamp_ns, caller_key)
- `profiles` — aggregated per-function statistics

You can query them directly:

```bash
sqlite3 ~/.ghost/sessions/<id>.db "SELECT fn_key, call_count FROM profiles ORDER BY call_count DESC LIMIT 10"
```

Override the storage location:

```bash
# Windows PowerShell
$env:GHOST_DATA_DIR = "D:\ghost-data"

# macOS/Linux
export GHOST_DATA_DIR=/var/ghost
```

---

## Competitive angle

| Tool | What it sees |
|---|---|
| Pylance / mypy | Source code types (annotations only) |
| cProfile | Call counts and timing (no types) |
| GitHub Copilot | Source code |
| **Ghost** | Runtime behaviour — actual types, real exception rates, measured latency, live call graph |

Type mismatches from legacy callers, traffic-weighted refactor safety, ground-truth dead code detection, and latency outliers are all **invisible to static tools by definition**.

---

## Development

```bash
git clone https://github.com/yourusername/ghost-observer
cd ghost-observer
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install -e ".[dev]"
pytest tests/ -v
```

---

## License

MIT