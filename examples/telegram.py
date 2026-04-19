"""End-to-end check against real Telegram.

Copy .env.example to .env, fill in ML3ERROR_TRANSPORT / ML3ERROR_TRANSPORT_*,
then run:
    uv run --env-file .env python examples/telegram.py

Expect two messages in Telegram: a ping, then a ValueError report.
"""
from pathlib import Path

import ml3error

# All credentials come from ML3ERROR_* env vars loaded via python-dotenv.
# project_root is pinned to the repo root (not the examples/ dir) so the
# SQLite state lands where `uv run ml3error` expects it by default.
ml3error.init(
    project="ml3error-smoke",
    project_root=Path(__file__).resolve().parent.parent,
    heartbeat=False,
)

ml3error.ping()

try:
    raise ValueError("hello from ml3error")
except ValueError as e:
    ml3error.report(e)

ml3error.close()  # flush the background worker before the process exits
