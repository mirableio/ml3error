"""End-to-end check with the Redis state backend (transport still Telegram).

Requires a local Redis at localhost:6379 and the `redis` extra installed
(uv sync --extra redis). Run with:
    uv run --env-file .env python examples/redis.py

Expect in Telegram: one ping + one RuntimeError report (second RuntimeError
is suppressed by cooldown).
Expect in Redis: ml3error:fp:* and ml3error:meta hashes.
"""
from pathlib import Path

import ml3error

# Transport comes from ML3ERROR_TRANSPORT / ML3ERROR_TRANSPORT_* in .env.
# state= overrides ML3ERROR_STATE for this one script so the SQLite default
# path isn't used, regardless of what's in .env.
ml3error.init(
    state="redis://localhost:6379/0",
    project="ml3error-redis-smoke",
    project_root=Path(__file__).resolve().parent.parent,
    heartbeat=False,
)

ml3error.ping()

try:
    raise RuntimeError("redis backend test")
except RuntimeError as e:
    ml3error.report(e)

try:
    raise RuntimeError("redis backend test")  # same fingerprint → suppress
except RuntimeError as e:
    ml3error.report(e)

ml3error.close()
print("done")
