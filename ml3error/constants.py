"""Internal tuning constants. Not a public configuration surface.

Monkey-patching is unsupported but possible if done before init().
"""

import re

# Background worker queue capacity. Drop-newest on overflow.
QUEUE_SIZE = 500

# Timeouts. HTTP_TIMEOUT is aspirational — the actual ceiling is set by
# the `notifiers` library (~5s connect + 20s read) since it doesn't
# expose timeouts. CRASH_TIMEOUT *is* enforced via thread+join since
# the process is dying anyway and a leaked thread is acceptable.
HTTP_TIMEOUT = 5.0
CRASH_TIMEOUT = 3.0

# Telegram caps messages at 4096 chars; this leaves headroom.
MAX_MESSAGE_CHARS = 3500

# SQLite busy-timeout used only on the crash-path connection.
SQLITE_BUSY_TIMEOUT_MS = 500

STATE_PRUNE_DAYS = 30

# 5 minutes: long enough that the cost is nothing, short enough that a
# missed heartbeat-time fires within an invisible window to humans.
HEARTBEAT_POLL_SECONDS = 300

SAFE_REPR_MAX_CHARS = 200
ATEXIT_DRAIN_SECONDS = 5.0

REDACTED = "***REDACTED***"

# Best-effort PII/secret scrubbing. Applied to traceback text, exception
# args repr, and each local's safe_repr. Matches get replaced with REDACTED.
# Patterns are intentionally simple — a few false positives beat missing
# the obvious cases.
SCRUB_PATTERNS: list[re.Pattern[str]] = [
    # Email addresses: local-part@domain.tld.
    re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    # "Bearer <token>" in Authorization-header-style strings. Case-insensitive
    # because HTTP headers are often lowercased in logs.
    re.compile(r"(?i)bearer\s+[a-zA-Z0-9._\-+/=]+"),
    # Provider-style API keys: a short prefix (sk-, pk-, api_, token_, …)
    # followed by 16+ chars. Catches Stripe sk_live_…, OpenAI sk-…,
    # and most "API key" conventions. \b anchors avoid mid-word matches.
    re.compile(r"\b(?:sk|pk|api|key|token|secret)[-_][a-zA-Z0-9_\-]{16,}\b"),
    # 32+ hex chars: SHA-256/MD5 digests, some session tokens, signed hashes.
    # Short hex (<32) is left alone to avoid redacting every small hex literal.
    re.compile(r"\b[a-fA-F0-9]{32,}\b"),
    # Base64-ish blobs of 40+ chars with optional = padding. Catches JWT
    # segments and long encoded secrets. Deliberately excludes `/` from
    # the character class — including it matches filesystem paths like
    # "Users/kuchin/Work/…" since those are 40+ chars of letters/digits/
    # slashes. Trade-off: standard-alphabet base64 containing many
    # slashes won't match, but URL-safe-ish tokens (dominant in practice)
    # still do.
    re.compile(r"\b[A-Za-z0-9+]{40,}={0,2}\b"),
    # Credit-card-like runs: 13-19 digits, optionally grouped by spaces or
    # dashes. Not a Luhn check — false positives (long order numbers) are fine.
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    # US Social Security numbers in the canonical 123-45-6789 form. The bare
    # 9-digit form would cause too many false positives, so we only catch
    # the hyphenated presentation.
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Assignment patterns like password=hunter2, api-key: abc, token = …
    # Captures the value (\S+) up to the next whitespace.
    re.compile(r"(?i)(password|passwd|pwd|api[_-]?key|secret|token)\s*[=:]\s*\S+"),
]
