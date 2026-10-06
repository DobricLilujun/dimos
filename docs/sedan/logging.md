# Logging system

The web console records **all of its execution output — including errors — on
the backend and stores it on the local machine**, so it can be reviewed after
the run. Nothing is streamed to the browser for this purpose: the browser
**Backend console** only shows the most recent ~200 lines in memory, while the
logs written here persist to disk and outlive the session.

This page is for the **standalone** console (`python -m dimos.web.console`).
The **embedded** console (`dimos run ...-persistent-console`) uses the normal
`dimos run` per-run logs (the CLI's own log directory and exception handler),
not the directory described here.

## What is saved

Two files are written to the console's log directory:

| File | Contents | Format |
|---|---|---|
| **`main.jsonl`** | The console's own logs: startup, lifecycle (start / stop / timeout), settings changes, and **every error / uncaught exception**. | Structured JSON, one record per line (systematic, rule-based output). |
| **`stack.log`** | The captured output of the **robot stack the console started** — its stdout / stderr, including its own errors. | Plain text, appended in real time. Secrets are masked. |

`main.jsonl` is the record you read for diagnosis; `stack.log` is the raw
console output of the child stack so you can see what it printed, even after it
scrolls past the in-memory **Backend console**.

### `main.jsonl` record shape

Each line is a JSON object with, at minimum:

| Field | Meaning |
|---|---|
| `timestamp` | ISO-8601 time of the record |
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `logger` | source location of the log call, as a path relative to the project (e.g. `dimos/web/console/settings.py`) |
| `event` | the human-readable message |
| `func_name`, `lineno` | callsite of the log call |
| `exception_type`, `exception_message`, `traceback_lines` | present only on an error / uncaught exception (full traceback) |

Additional context is appended as `key=value` pairs, e.g. `log_dir=…`,
`debug=true`, `pid=…`, `map_mode=…`.

### `stack.log`

The captured robot-stack output is appended as the console reads it, so it
survives beyond the 200-line in-memory **Backend console**. Secrets are
**redacted** before anything is written: `OPENAI_API_KEY` and
`UNITREE_AES_128_KEY` appear as `[REDACTED]`, never in the clear.

## Where it is stored

The console creates a **dedicated, timestamped log directory** and prints its
path at startup:

```
<log-dir>/web-console/<timestamp>/
├── main.jsonl          # structured console logs (rotating)
├── main.jsonl.1 … .20  # rotated backups (oldest kept)
└── stack.log           # captured robot-stack output (redacted)
```

**`<log-dir>`** resolves to:

| When | Value |
|---|---|
| Project is a git checkout | `<project-root>/logs` |
| Installed package | `~/.local/state/dimos/logs` (the state dir) |
| `DIMOS_LOG_DIR` is set | `DIMOS_LOG_DIR` (base, overrides the above) |
| The base is not writable | a temp fallback: `<tempdir>/dimos/logs` |

So a typical run lands at, e.g.:

```
/path/to/dimos/logs/web-console/20261006-171318/
```

Each run gets its **own** timestamped directory, so runs do not overwrite each
other.

## Naming rules

| Item | Rule |
|---|---|
| **Run directory** | `web-console/<timestamp>`, where `<timestamp>` = `YYYYmmdd-HHMMSS` (local time), e.g. `20261006-171318` |
| **Structured log** | `main.jsonl` — one JSON object per line |
| **Stack output** | `stack.log` — the captured stack stdout / stderr |
| **Rotation** | `main.jsonl` rotates at **10 MiB**, keeping up to **20** backups (`main.jsonl.1` … `main.jsonl.20`), oldest kept |
| **Stack log** | `stack.log` is append-only for the run (no rotation); it closes on shutdown |

## Debug level

| How | Level |
|---|---|
| `python -m dimos.web.console --debug` | `DEBUG` for the console **and** its child robot stack (the stack inherits it from the environment) |
| No flag | `INFO` |
| `DIMOS_LOG_LEVEL=…` in the environment | that level (e.g. `DIMOS_LOG_LEVEL=DEBUG`), respected even without `--debug` |

The level is applied to loggers that were already created at import time, so it
takes effect for the whole process, not only loggers created later.

## Error capture

- **Uncaught exceptions** in the console process are caught and written with a
  **full traceback** to `main.jsonl` (and shown on the console), instead of
  dying silently.
- A **graceful-stop timeout** (the stack did not stop in time) is logged as an
  `ERROR`.
- The robot stack's own output — including its errors — is captured in
  `stack.log`.

## Viewing the logs

The log directory path is printed at startup. To inspect it:

```bash
# structured console logs (pretty-print with jq)
cat <log-dir>/main.jsonl | jq .

# only errors, most recent first
jq 'select(.level=="ERROR" or .level=="CRITICAL")' <log-dir>/main.jsonl

# the captured robot-stack output
tail -f <log-dir>/stack.log
```

The console also exposes a `/api/log` endpoint that returns the **log directory
path** and whether debug mode is on — this is metadata for locating the local
files; it does **not** stream the log contents into the browser.

> The logs are local files on the machine that ran the console. Read them with
> your normal file / log tools; they are not uploaded anywhere.
