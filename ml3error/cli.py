"""Console script entry point. Invoked via `uv run ml3error [...]`.

Subcommands:
    web         Start the review UI. This is the default.
    heartbeat   Send a daily-digest message now, using the same code
                path as the scheduled background thread.

Config precedence: CLI args > environment > .env file in cwd > defaults.
The .env loading happens automatically in config.build().
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

from dotenv import load_dotenv

from .web import _default_state_url, serve


def _state_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("ML3ERROR_STATE")
    if env:
        return env
    return _default_state_url()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ml3error",
        description="Lightweight error notifier. Default command: web.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Web UI bind address (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=6025,
        help="Web UI port (default: 6025).",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="State URL. Overrides $ML3ERROR_STATE / .env "
        "(fallback: sqlite:///$CWD/.ml3error.db).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open a browser window on startup.",
    )
    # Single positional command. Optional so bare `ml3error` works.
    parser.add_argument(
        "command",
        nargs="?",
        default="web",
        choices=["web", "heartbeat"],
        help="'web' (default) starts the review UI; "
        "'heartbeat' sends a digest now.",
    )
    return parser


def _send_heartbeat() -> int:
    """Send a one-off heartbeat digest without touching state.

    Uses the same message-building pipeline as the scheduled thread so
    the preview matches what the daily firing would send — but does not
    advance last_heartbeat or reset counters, so the scheduled thread
    still fires normally afterward.
    """
    from pathlib import Path

    from .config import build
    from .heartbeat import Heartbeat
    from .store import open_store
    from .transport import Transport

    # The CLI entry point lives in .venv/bin, which _infer_project_root()
    # (correctly) rejects as a wrong-default. Fall back to cwd unless the
    # user has set ML3ERROR_PROJECT_ROOT explicitly.
    kwargs: dict = {}
    if not os.environ.get("ML3ERROR_PROJECT_ROOT"):
        kwargs["project_root"] = Path.cwd()
    cfg = build(**kwargs)
    store, crash = open_store(cfg.state)
    crash.close()
    try:
        transport = Transport(cfg.transport, cfg.transport_config)
        hb = Heartbeat(cfg.project, cfg.heartbeat_time, store, transport, threading.Lock())
        ok = hb.fire(update_state=False)
        if ok:
            print(f"heartbeat preview sent via {cfg.transport!r} "
                  f"for project {cfg.project!r} (state untouched)")
            return 0
        print(
            f"heartbeat preview FAILED via {cfg.transport!r} — "
            "check credentials or transport availability",
            file=sys.stderr,
        )
        return 1
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    # The web path doesn't go through config.build(), so load .env here
    # too. Pin to cwd so we don't pick up stray .env files higher up.
    load_dotenv(dotenv_path=".env", override=False)
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "web":
        serve(
            _state_url(args.state),
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
        return 0
    if args.command == "heartbeat":
        if args.state:
            os.environ["ML3ERROR_STATE"] = args.state
        return _send_heartbeat()
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
