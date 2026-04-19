# ml3error — Specification

*This document is authoritative. It supersedes `PROJECT.md` (the initial sketch) where they differ.*

A tiny Python library that notifies you when your program hits an error.
Built for small hobby projects where pulling in Sentry or a full-blown
observability stack is overkill.

You add a few lines to your script, and from then on any uncaught exception
gets sent to you over email, Telegram, or Slack. Repeated errors are
grouped so your phone doesn't buzz a thousand times for the same crash.

## What it does in one paragraph

When your program crashes, `ml3error` looks at the error, figures out a
"fingerprint" for it, checks a small local database to see if it has
already told you about this error recently, and if not, sends you a
message. If it has, it just quietly increments a counter. Once a day it
sends a short summary of everything it suppressed. That's the whole thing.

## Target Python version

Python **3.10 or newer**.

## Installation

The library will be published on PyPI as `ml3error`.

```bash
uv add ml3error
```

If you want to use Redis for state instead of SQLite:

```bash
uv add "ml3error[redis]"
```

The only runtime dependency is [`notifiers`](https://pypi.org/project/notifiers/),
which is what actually delivers the email/Telegram/Slack message.

## How you use it

The whole API is four functions (`init`, `report`, `ping`, `close`)
plus one class (`LoggingHandler`, for projects that log errors instead
of raising them).

```python
import ml3error

ml3error.init(
    transport="telegram",
    transport_config={"token": "...", "chat_id": "..."},
    state="sqlite:///~/.ml3error.db",
)

# From here on, any uncaught exception is sent to you automatically.
# You can also report caught exceptions manually:

try:
    risky_thing()
except Exception as e:
    ml3error.report(e)
```

`close()` flushes anything still in the send queue and shuts the worker
down. It is also registered with `atexit` so you don't have to call it
yourself in normal cases.

`ping()` sends a synthetic `"ml3error ping from <project>"`
notification through the configured transport, synchronously and
without touching the queue, cooldown, or scrubbing. Use it once after
`init()` to confirm your transport credentials actually work. If the
send fails, `ping()` raises — no silent "looks fine, nothing arrived"
surprises later. It is deliberately **not** called from `init()`,
because restarting a process should not spam your Telegram chat.

```python
ml3error.init(...)
ml3error.ping()   # run once during setup, remove or comment out after
```

### API details

- **`init()` is idempotent.** Calling it a second time with the same
  arguments is a no-op; calling it with different arguments raises
  `RuntimeError` rather than silently reconfiguring a live worker
  thread. To reconfigure, call `close()` first.
- **`report(exc)` traceback source.** If `exc.__traceback__` is set
  (which it is for any exception that has been raised, even if it was
  later caught), the library uses that. If it is `None` (you
  constructed the exception manually and never raised it), the
  library falls back to `sys.exc_info()` when called from inside an
  `except` block, and otherwise reports the exception with no
  traceback frames — fingerprint uses `(type, "<unknown>", "<unknown>")`.
- **`report(exc)` is thread-safe.** You can call it from any thread,
  including inside an asyncio task.
- **Per-call locals override.** `report(exc, locals=...)` accepts an
  optional bool that overrides the init-level `with_locals` default
  for this one call. Use `report(exc, locals=False)` at call sites you
  know handle sensitive values (auth flows, payment handlers, anything
  with user-supplied secrets) to send just the traceback without frame
  locals, even if locals capture is enabled globally. Omit the argument
  (or pass `None`) to use the init-level default. The kwarg shadows
  Python's `locals` builtin inside the function signature, which is
  deliberate — the builtin isn't referenced inside `report()`, and the
  shorter name reads better at the call site where this argument is
  actually used.
- **After `close()`**, further calls to `report()` and `ping()` raise
  `RuntimeError`. This is deliberate: silent no-ops hide bugs. Call
  `init()` again if you want to resume.

### `logging.Handler` integration

Many Python projects log errors instead of raising them
(`log.exception(...)`, `log.error(..., exc_info=True)`). For that path,
the library ships a `logging.Handler`:

```python
import logging
import ml3error

ml3error.init(...)

logging.getLogger().addHandler(ml3error.LoggingHandler())
```

Behavior:

- Records with `exc_info` are forwarded to `ml3error.report(exc)` with
  the exception object, so fingerprinting, payload assembly, and
  traceback text are identical to a direct `report()` call.
- Records **without** `exc_info` (plain `log.error("db offline")`)
  are also reported. The fingerprint is `(level, rel_path, func_name)`
  computed from the `LogRecord`'s `pathname` / `funcName`, so different
  call sites and different severity levels dedup independently. The
  outgoing message uses the level name (e.g. `ERROR`) as its 'type' and
  the record's formatted message as its body.
- The handler activates at any level it's configured to accept
  (default `logging.ERROR`); lower-severity records are filtered out
  by the standard `logging` level check before `emit()` runs.
- Both paths go through the same pipeline as `report()`: scrub,
  fingerprint, cooldown check, queue.
- Exceptions raised *inside* the handler are swallowed (standard
  `logging.Handler.handleError` behavior) so logging itself never
  breaks because the notifier is misbehaving.

## Configuration

You can configure the library two ways, and you can mix them:

1. Pass arguments to `init(...)`.
2. Set environment variables with the prefix `ML3ERROR_`.

If both are set, `init(...)` arguments win.

**List-valued options in env vars** are comma-separated, with whitespace
around commas stripped. An empty string means an empty list. Example:
`ML3ERROR_HOOKS="excepthook, threading"`.

### Internal tuning constants

A few knobs that nobody should need to tune in practice live as
module-level constants inside the library, not as public config:

| Constant             | Value  | Purpose                                                       |
|----------------------|--------|---------------------------------------------------------------|
| `QUEUE_SIZE`         | `500`  | Background worker queue capacity.                             |
| `HTTP_TIMEOUT`       | `5.0`  | Seconds to wait for a transport call in the worker.           |
| `CRASH_TIMEOUT`      | `3.0`  | Seconds to wait when sending from a dying process.            |
| `MAX_MESSAGE_CHARS`  | `3500` | Hard cap on assembled message body (Telegram-safe).           |
| `SCRUB_PATTERNS`     | (see "Scrubbing") | Default regex list applied to all messages.        |

These are intentionally not exposed in `init(...)` or via env vars to
keep the public surface small. The values work for the hobby-scale
targets this library is built for. If you really need to change one,
monkey-patch the attribute on the `ml3error` module **before** calling
`init()`. That escape hatch is unsupported — we may change these
constants or their names between versions.

| Option                 | Env var                        | Default               | Meaning                                                              |
|------------------------|--------------------------------|-----------------------|----------------------------------------------------------------------|
| `transport`            | `ML3ERROR_TRANSPORT`           | — (required)          | `"email"`, `"telegram"`, or `"slack"`.                               |
| `transport_config`     | `ML3ERROR_TRANSPORT_*`         | — (required)          | The token/chat-id/webhook dict that `notifiers` expects.             |
| `state`                | `ML3ERROR_STATE`               | `sqlite:///<project_root>/.ml3error.db` | `sqlite:///path/to/file.db` or `redis://host:port/db`.   |
| `project`              | `ML3ERROR_PROJECT`             | basename of `project_root` | Short name for this project. Shown in every message.            |
| `project_root`         | `ML3ERROR_PROJECT_ROOT`        | dir of `__main__.__file__` | Folder your code lives in. Used to make file paths relative.    |
| `cooldown_hours`       | `ML3ERROR_COOLDOWN_HOURS`      | `24`                  | How long to stay silent after sending a given error once.            |
| `with_locals`       | `ML3ERROR_WITH_LOCALS`      | `True`                | Default for whether to include frame locals in messages. Can be overridden per call via `report(exc, locals=False)`. |
| `heartbeat`            | `ML3ERROR_HEARTBEAT`           | `True`                | Whether to send a daily summary message.                             |
| `heartbeat_time`       | `ML3ERROR_HEARTBEAT_TIME`      | `"09:00"`             | Local-time `"HH:MM"` the daily heartbeat fires at.                   |
| `hooks`        | `ML3ERROR_HOOKS`       | `["excepthook", "threading"]` | Which Python error hooks to install. Valid: `excepthook`, `threading`, `asyncio`. |

For state you pick either SQLite (easy, no infrastructure) or Redis
(nice if you already run one), not both. If you don't set `state`, the
library uses a SQLite file at `<project_root>/.ml3error.db` — one file
per project, sitting next to the code it reports on.

## Grouping errors (fingerprinting)

Every error is reduced to a short identifier called a **fingerprint**.
Two errors with the same fingerprint count as "the same error" for
cooldown purposes.

The fingerprint is built from three things:

1. The **exception class name**, e.g. `ValueError`.
2. The **file path relative to `project_root`**, e.g. `app/loaders/config.py`.
3. The **function name** where the error happened, e.g. `load_config`.

We use the path relative to `project_root` so the same error in dev
and in production counts as one error, but we still distinguish between
two different `utils.py` files inside the same project. If the file
lives outside `project_root` (for example inside `site-packages`), we
fall back to its basename.

`project_root` defaults to the directory of the entry-point script
(`sys.modules['__main__'].__file__`). That is the right answer whether
you run `python myscript.py` or `python -m myapp`, and it does not
depend on what directory the user happened to launch from.

`init()` raises and asks you to pass `project_root` explicitly in three
cases where the default would be wrong or unsafe:

1. `__main__.__file__` is not defined (REPL, Jupyter notebook, embedded
   interpreter).
2. The inferred directory sits inside `site-packages/` (console-script
   entry points, pipx-installed CLIs).
3. The inferred directory is a `bin/` directory (Gunicorn, Uvicorn,
   and similar launchers where `__main__.__file__` points at
   `.../venv/bin/gunicorn`).

We deliberately do not fall back to `os.getcwd()` and do not try to
auto-walk upward looking for a `pyproject.toml` or `.git` marker —
both are silent-wrong-answer generators. If you use a launcher, set
`project_root=Path(__file__).parent` in your entry module. One line,
zero magic.

We use the innermost frame (the one that actually raised), not the
outermost.

This is deliberately simple. It is not bulletproof — moving a function
to a different file will create a new fingerprint — but that is
acceptable for a hobby-grade tool.

## Cooldown and suppression

When a fingerprint fires:

- **First time, or first time in the last `cooldown_hours`:** send a
  message. If and only if the send succeeds, update `last_notified`
  to the current time.
- **Inside the cooldown window:** don't send anything, just increment
  a counter.
- **Next fire after the window ends:** send a message, and include the
  count of suppressed occurrences in the body, e.g.
  "*suppressed 47 similar errors in the last 24h*".

Critically, `last_notified` is advanced **only on a confirmed successful
send** — never when a send fails or when we merely enqueue a payload.
If the transport is down, every subsequent occurrence of the same
fingerprint will retry the send (bumping `transport_failures`) rather
than being silently suppressed for `cooldown_hours`.

## Scrubbing sensitive data

Before anything goes out, the library tries to remove obvious personal
or secret values. It does this by running a list of regular expressions
over three strings:

1. The formatted traceback text.
2. The `repr()` of the exception's arguments.
3. Each local variable (as repr) in each frame of the traceback.

Matches are replaced with `***REDACTED***`.

The pattern list lives as a module constant `ml3error.SCRUB_PATTERNS`
and catches:

- Email addresses.
- Bearer tokens and common API-key shapes.
- Long hex and base64-looking blobs.
- Credit-card-like sequences.
- US SSN-like sequences.
- `password=...` and `api_key=...` assignments in query strings or logs.

This is best-effort. It will miss things. Treat it as a safety net, not
a guarantee. If you need to add a pattern or drop a default, monkey-patch
`ml3error.SCRUB_PATTERNS` before calling `init()`. This is deliberately
not a public config knob — a lean library is not the right place to
take user-supplied regex, since Python's `re` has no timeout and a
pathological pattern (nested quantifiers) would hang the crash path
with no safe way to abort.

### Safe `repr` for locals

Taking `repr()` of arbitrary locals is dangerous during exception
handling: a custom `__repr__` can execute user code, recurse forever,
block on I/O, or raise. The library never calls `repr()` directly on a
frame local. Instead it uses a small `safe_repr(value)` helper that:

1. Wraps the call in `try/except Exception` and on failure returns
   `"<unreprable FooType>"`.
2. Truncates the resulting string to **200 characters** with a trailing
   `…` marker.

If a local's safe repr raises, that one local is replaced by the
fallback string; the rest of the frame's locals are unaffected.

**What `safe_repr` deliberately does not do:** it does not enforce a
wall-clock timeout on the `repr()` call itself. A custom `__repr__`
that blocks on I/O, sleeps, or hangs indefinitely will hang the
reporting path, because the only cheap ways to enforce a timeout are
`signal.SIGALRM` (main-thread-only, globally intrusive) and
thread-with-join (threads in Python don't stop when their `join`
timeout expires). Neither fits a lean library. If you have locals
whose `__repr__` can block, set `with_locals=False` in `init()`;
tracebacks will then include only file/function/line, no local
variable values.

## How messages are sent

There are two paths.

### Background path (normal case)

For errors reported via `ml3error.report(exc)`, or uncaught errors in
threads, or asyncio-loop errors:

- Scrubbing, fingerprinting, and the **cooldown check all happen on
  the caller's thread, before anything is enqueued.** Errors inside
  their cooldown window only bump `suppressed_count` in state; they
  never reach the queue. This keeps the queue free of noise and makes
  the overflow policy meaningful.
- In addition to the cooldown check, the caller thread also checks a
  small in-memory `set[str]` of **fingerprints currently queued or
  being sent**. If the fingerprint is already in-flight, the occurrence
  is treated as suppressed (bump `suppressed_count`, don't enqueue).
  The worker removes the fingerprint from this set after the send
  attempt completes, success or failure. This closes the race where
  several occurrences of the same fingerprint could all pass the
  cooldown check before any of them actually updates `last_notified`.
  The set lives only in memory and is guarded by the same lock as the
  store. It does not dedupe across two processes sharing one Redis
  store — that is an accepted limitation.
- A single daemon worker thread is spawned the first time it is needed.
- A `queue.Queue(maxsize=QUEUE_SIZE)` feeds it. Each entry is a
  distinct, cooldown-passing error payload ready to send.
- If the queue is full, the **newest** item is dropped (the incoming
  one is rejected) and a counter is incremented. The rationale is that
  a full queue means the transport is slow or broken, and the earliest
  item is almost always the root cause of whatever cascade came after
  it — that is the one worth keeping.
- Each send gets up to `HTTP_TIMEOUT` seconds (5s).
- One attempt per error. If it fails, a counter is incremented and
  reported in the next heartbeat. No retries, no backoff.
- `atexit` drains the queue with a bounded timeout.

### Message size

Telegram caps messages at 4096 characters; email and Slack are much
larger but still finite. To keep every transport happy with one rule,
the library assembles the full message (project tag + fingerprint +
traceback + locals + suppressed-count line) and then, if the result
is longer than `MAX_MESSAGE_CHARS` (3500), truncates the tail and
appends `…[truncated]`. No per-transport chunking, no clever priority
reordering — a single hard cap applied uniformly. Oversized payloads
should never turn a real crash into a transport failure.

### Synchronous path (dying process)

For truly uncaught exceptions caught by `sys.excepthook`, the worker
thread is no longer reliable — the process is about to exit. So the
library does it all inline:

- scrub → fingerprint → check cooldown → (if due) send → on success,
  update `last_notified`.
- The send uses `CRASH_TIMEOUT` (3s) instead of 5s.
- The cooldown read and the `last_notified` write both go through a
  dedicated SQLite connection (see "State storage" above) with no
  Python-level lock, so the crash path cannot be blocked by the
  background worker or heartbeat thread. If the state update itself
  cannot complete within SQLite's 500 ms busy timeout, it is skipped
  — accepting one possible duplicate alert on the next run rather
  than failing to deliver the current one.

## State storage

`ml3error` keeps a small amount of state between runs so it can tell
"I already told you about this" from "this is new".

### SQLite (default)

Tables:

- `fingerprints(fp TEXT PRIMARY KEY, first_seen INTEGER, last_notified INTEGER, suppressed_count INTEGER)`
- `meta(key TEXT PRIMARY KEY, value TEXT)` — holds `last_heartbeat`
  and three running counters: `dropped`, `transport_failures`,
  `suppressed`. The counters accumulate since the last successful
  heartbeat send and are reset to zero after a heartbeat fires.

Behavior:

- WAL mode is enabled on open.
- On startup, rows in `fingerprints` whose most recent activity is
  older than 30 days are deleted — specifically
  `COALESCE(last_notified, first_seen) < now - 30d`. This keeps
  long-lived fingerprints that are still firing, and only drops ones
  that have genuinely gone quiet.
- The SQLite connection is opened lazily, on first use.
- The store is hit from three threads (the caller reporting an error,
  the background worker recording send outcomes, and the heartbeat
  thread) so the connection is opened with `check_same_thread=False`
  and every read or write is guarded by a single module-level
  `threading.Lock`. No per-thread connections, no connection pool —
  the lock is fine for the traffic this library will ever see.
- **The crash path (`sys.excepthook`) is an exception to this.** It
  uses its own dedicated SQLite connection, opened lazily, with
  `busy_timeout=500` ms and **no** Python-level lock. The crash path
  only ever does a one-shot `SELECT last_notified` followed (on
  successful send) by `UPDATE last_notified`, and WAL mode already
  serializes those safely at the file level. The motivation is that
  a dying process has a 3-second budget to scrub, fingerprint, send,
  and record, and it should never spend that budget blocked on a
  Python lock held by the background worker or heartbeat thread. If
  SQLite itself is busy beyond 500 ms, the crash-path state update is
  **skipped silently** — best-effort. A duplicate send on the next
  run is a better failure mode than no send at all. For Redis this
  situation does not arise: redis-py uses its own socket timeout and
  there is no cross-thread Python lock to contend on.

### Redis (optional)

Same logical shape, encoded as Redis keys:

- `ml3error:fp:<fp>` hash with `first_seen`, `last_notified`, `suppressed_count`.
- `ml3error:meta` hash with `last_heartbeat`, `dropped`,
  `transport_failures`, `suppressed`.

A 30-day TTL on the `fp:*` keys handles pruning, **refreshed on every
write** so active fingerprints don't silently expire.

## Daily heartbeat

If `heartbeat=True` (the default), the library sends a once-a-day
summary message at the local time given by `heartbeat_time` (default
`"09:00"`):

> *[my-bot] ml3error heartbeat — since last heartbeat: 12 errors suppressed, 0 transport failures, 0 messages dropped from queue.*

The leading `[my-bot]` is the `project` config value, so if you run
several projects that all notify into the same Telegram chat you can
tell them apart.

How it's scheduled:

- A single daemon timer thread is started during `init()`.
- The thread runs a simple poll loop: sleep 5 minutes, wake up, check
  the wall clock.
- On each wake, it fires the heartbeat if **both** conditions hold:
  1. the current local time is at or past today's `heartbeat_time`, and
  2. `last_heartbeat` in state is before today's `heartbeat_time`.
- After firing, `last_heartbeat` is advanced to today's scheduled slot
  **whether the send succeeded or failed**. This is deliberate: a dead
  transport should not turn the heartbeat into a 5-minute retry loop.
- The three counters (`dropped`, `transport_failures`, `suppressed`)
  reset to zero **only on a successful send**. On a failed send, the
  counters keep accumulating and `transport_failures` is bumped by one
  for the failed heartbeat itself. They all roll into the next
  successful heartbeat, which is therefore a summary "since last
  *successful* heartbeat".
- Because it's a daemon thread, it does not keep the process alive.

The poll loop is deliberately dumb instead of computing "sleep for
N seconds until 9am". `time.sleep` across a laptop suspend or a VM
pause is unreliable — it can wake up hours late, or fire immediately
on resume — so we just re-check the clock every 5 minutes and let the
two conditions above do the right thing. The heartbeat may fire up to
5 minutes after `heartbeat_time`, which is fine.

The project name is also prepended to every regular error message, not
just heartbeats, so you always know which project sent it.

**Known limitation:** short-lived processes (a cron script that runs
for 5 seconds) will almost never send a heartbeat, because they exit
before the timer fires. That's fine — this tool is aimed at long-running
hobby projects (bots, web apps, home-server scripts). If your process
is a one-shot, set `heartbeat=False`.

## Hooks into Python's error machinery

`init(hooks=[...])` decides which hooks are installed. Valid
values are:

- `"excepthook"` — `sys.excepthook`, catches uncaught exceptions on the
  main thread. Default on.
- `"threading"` — `threading.excepthook`, catches uncaught exceptions
  in other threads. Default on.
- `"asyncio"` — calls `loop.set_exception_handler(...)` on the currently
  running event loop. Default off. Only usable if `init()` itself runs
  from inside a coroutine or after the loop is running.

For the common case where `init()` runs at import time (before the event
loop exists), call **`ml3error.install_asyncio_hook()`** from inside your
async entry point instead:

```python
import ml3error

ml3error.init(transport="telegram", transport_config={...})

async def main():
    ml3error.install_asyncio_hook()  # requires init() to have run
    # ... your app ...
```

The handler it installs is identical to what `hooks=["asyncio"]` would set
up, and is removed by `close()` along with the other hooks.

Pass `hooks=[]` to disable all of them and drive everything
through manual `ml3error.report(exc)`.

In all cases, whatever hook was installed previously is chained: we call
our handler, then the previous one.

## What is explicitly not in scope

- **Structured context** (user id, request id, tags, extra fields). Keep
  it simple; if you want this, use Sentry.
- **Breadcrumbs** (history of events leading up to the error).
- **Sampling / rate-limiting** beyond the per-fingerprint cooldown.
- **Framework auto-instrumentation** (no Flask/FastAPI/Django middleware).
- **Retries or backoff** on transport failures. One attempt. Count the
  failure. Move on.
- **Remote configuration.**

## Project layout

```
ml3error/            # package (flat layout, no src/ dir)
  __init__.py        # init(), report(), ping(), close(), LoggingHandler
  constants.py       # QUEUE_SIZE, HTTP_TIMEOUT, CRASH_TIMEOUT,
                     #   MAX_MESSAGE_CHARS, SCRUB_PATTERNS
  config.py          # dataclass + env-var loader
  fingerprint.py
  scrub.py
  store/
    base.py          # Store protocol
    sqlite.py        # default backend
    redis.py         # optional backend
  worker.py          # background thread + bounded queue
  transport.py       # thin notifiers wrapper
  hooks.py           # sys/threading/asyncio installers
  logging_handler.py # logging.Handler integration
  heartbeat.py       # daily summary thread
tests/
  ...
pyproject.toml
README.md
SPEC.md              # this file
```

## Development setup

```bash
uv init --lib ml3error
uv add notifiers
uv add --optional redis redis
uv add --dev pytest
```

`pyproject.toml` pins `requires-python = ">=3.10"` and declares the
`redis` extra.
