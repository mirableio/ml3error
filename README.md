# ml3error

A tiny error notifier for hobby Python projects. When your program crashes, it sends you a message on Telegram, Slack, or email — and won't buzz your phone again for the same error until tomorrow.

Built for personal bots, home-server scripts, and small apps where pulling in Sentry is overkill. Single runtime dependency ([notifiers](https://pypi.org/project/notifiers/)), SQLite for state (Redis optional), no external services beyond your chosen transport.

## Install

```bash
uv add ml3error
```

## Use it

**Recommended:** keep credentials out of code. Drop a `.env` next to your script:

```bash
ML3ERROR_TRANSPORT=telegram
ML3ERROR_TRANSPORT_TOKEN=1234567890:ABCdef-your-bot-token
ML3ERROR_TRANSPORT_CHAT_ID=123456789
```

Then in your code:

```python
import ml3error
ml3error.init()
```

That's it. `.env` is loaded automatically (via `python-dotenv`). Shell-exported vars still take precedence, so `uv run --env-file .env python your_script.py` also works. See [`.env.example`](.env.example) for the full set of recognized names.

From now on, any uncaught exception gets scrubbed for PII, fingerprinted, and sent to you. Repeated errors with the same fingerprint are grouped and suppressed for 24h by default. A once-a-day heartbeat tells you everything is still running.

You can also report caught exceptions manually:

```python
try:
    risky_thing()
except Exception as e:
    ml3error.report(e)
```

### Programmatic config (alternative)

If you'd rather not use env vars, every option takes an `init()` kwarg:

```python
ml3error.init(
    transport="telegram",
    transport_config={"token": "...", "chat_id": "..."},
    cooldown_hours=12,
)
```

Kwargs win over env vars when both are set.

### Verify once

Call `ml3error.ping()` after `init()` the first time to confirm your credentials work — it sends a synthetic message and raises if the transport refuses.

### Review UI

`ml3error` ships with a minimal web UI to review the errors you've been notified about and mark them resolved:

```bash
uv run ml3error
```

Opens `http://127.0.0.1:6025` in your browser. State comes from `.env` / env vars, or pass `--state sqlite:///path.db` explicitly.

### Preview the daily digest

The library sends a once-a-day heartbeat listing your most active errors, their counts, and how recently they fired. To see what that looks like on demand (e.g., after wiring up a new transport):

```bash
uv run ml3error heartbeat
```

Sends the same message the scheduled thread would send, using your configured transport. This is a pure preview — it does **not** advance `last_heartbeat` or reset counters, so the scheduled daily firing still happens normally.

## What it doesn't do

- No structured context, breadcrumbs, or sampling — use Sentry if you want those.
- No framework auto-instrumentation.
- No retries on transport failures (one attempt, counted in the next heartbeat).

## More

Full design and behavior details live in [SPEC.md](SPEC.md). Runnable end-to-end examples are in [`examples/`](examples/).
