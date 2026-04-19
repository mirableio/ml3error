Lightweight error notifier

- In-process Python library
- Background worker: on-demand thread (ThreadPoolExecutor, max_workers=1), 
  bounded queue (500), drop-newest on overflow, 5s HTTP timeout
- Synchronous path for uncaught exceptions (sys.excepthook) with 3s timeout
- Fingerprint: (exception_type, filename, function)
- PII scrubbing: regex-based redaction of locals before send
- Cooldown: notify on first occurrence, suppress for N hours (default 24), 
  next occurrence after expiry reports suppressed count
- State: SQLite default (WAL mode, auto-prune >30 days on startup), 
  Redis optional via config
- Transport: notifiers lib; channels email/Telegram/Slack via config
- Daily heartbeat with summary (errors suppressed, errors dropped from queue,
  transport failures)
- No SDK dependency, no external infrastructure beyond the transport endpoint
