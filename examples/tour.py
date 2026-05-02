"""A tour of every message format ml3error produces.

Sends, in order:
    1. ping (synthetic verification message)
    2. fresh exception — no suppressed count, no regression
    3. (silently bumped suppressed count for the same fp)
    4. cooldown-expired send for the same fp — shows "suppressed N similar errors"
    5. mark resolved → re-fire → REOPENED banner
    6. bare log.error (no traceback)
    7. log.exception (with traceback)
    8. heartbeat digest (with active errors → 🚨 in subject)
    9. heartbeat digest (everything resolved → ✅ in subject)

Uses a tiny cooldown (a few seconds) so the cooldown-expired message
arrives during the same run. Total runtime ≈ 12 s. Run from repo root:

    uv run --env-file .env python examples/tour.py
"""
import logging
import time
from pathlib import Path

import ml3error

ROOT = Path(__file__).resolve().parent.parent

# ~3.6s cooldown so we can demonstrate the "suppressed N" message
# without making the user wait 24 hours.
ml3error.init(
    project="ml3error-tour",
    project_root=ROOT,
    cooldown_hours=0.001,
    heartbeat=False,
    hooks=[],
)


def _raise():
    """Always raise the same ValueError → one fingerprint across all calls."""
    raise ValueError("recurring failure for the tour")


# ---- 1. ping ---------------------------------------------------------
print("[1/9] ping")
ml3error.ping()
time.sleep(1)


# ---- 2. fresh exception ---------------------------------------------
print("[2/9] fresh exception (no suppressed, no regression)")
try:
    _raise()
except ValueError as e:
    ml3error.report(e)
time.sleep(1)


# ---- 3. silent suppressions inside cooldown -------------------------
print("[3/9] 4 same-fp reports — should be silently suppressed")
for _ in range(4):
    try:
        _raise()
    except ValueError as e:
        ml3error.report(e)


# ---- 4. cooldown-expired send shows the suppressed count ------------
print("[4/9] waiting for cooldown to expire…")
time.sleep(4)
print("       firing same fp again — expect 'suppressed 4 similar errors'")
try:
    _raise()
except ValueError as e:
    ml3error.report(e)
time.sleep(1.5)


# ---- 5. regression: resolve, then fire again ------------------------
print("[5/9] marking resolved + firing → REOPENED banner")
items = ml3error._reporter.store.list_fingerprints()
target = next(it for it in items if it["exc_type"] == "ValueError")
ml3error._reporter.store.set_resolved(target["fp"], True)
time.sleep(4)  # past cooldown again
try:
    _raise()
except ValueError as e:
    ml3error.report(e)
time.sleep(1.5)


# ---- 6. bare log.error (no exc_info, no traceback) ------------------
print("[6/9] bare log.error — message only, no traceback")
logger = logging.getLogger("ml3error.tour")
logger.handlers.clear()
logger.addHandler(ml3error.LoggingHandler())
logger.setLevel(logging.ERROR)
logger.error("database connection dropped — bare log call")
time.sleep(1)


# ---- 7. log.exception (with exc_info, with traceback) ---------------
print("[7/9] log.exception — with traceback")
try:
    raise RuntimeError("logged via log.exception")
except RuntimeError:
    logger.exception("background task failed")
time.sleep(1)

ml3error.close()


# ---- 8 & 9. heartbeat digest previews -------------------------------
import threading
from ml3error.config import build
from ml3error.heartbeat import Heartbeat
from ml3error.store import open_store
from ml3error.transport import Transport

# Re-open everything just for the heartbeat sends (close() above tore
# the live reporter down).
cfg = build(project="ml3error-tour", project_root=ROOT, cooldown_hours=0.001,
            heartbeat=False, hooks=[])
store, crash = open_store(cfg.state)
crash.close()
try:
    transport = Transport(cfg.transport, cfg.transport_config)
    hb = Heartbeat(cfg.project, cfg.heartbeat_time, store, transport, threading.Lock())

    print("[8/9] heartbeat digest with active errors → expect 🚨")
    hb.fire(update_state=False)
    time.sleep(1.5)

    # Mark every fingerprint resolved so the next digest has zero active.
    print("[9/9] marking all fingerprints resolved + heartbeat → expect ✅")
    for fp in store.list_fingerprints():
        store.set_resolved(fp["fp"], True)
    hb.fire(update_state=False)
finally:
    store.close()

print("\nDone. Check Telegram — you should have 7 messages + 2 heartbeats.")
