from __future__ import annotations

import asyncio
import sys
import threading
from types import TracebackType
from typing import Any, Callable

CrashHandler = Callable[[type[BaseException], BaseException, TracebackType | None], None]
ThreadHandler = Callable[[BaseException, TracebackType | None], None]
AsyncioHandler = Callable[[BaseException], None]


def install_excepthook(handler: CrashHandler) -> Callable[[], None]:
    # Chain: call our handler first, then whatever was installed before.
    # This lets us send our notification while still letting the default
    # (or user's) traceback printer run so the terminal still shows the
    # error. Both sides are wrapped in try/except so a crash in either
    # can't prevent the other from running.
    previous = sys.excepthook

    def hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        try:
            handler(exc_type, exc, tb)
        except Exception:
            pass
        try:
            previous(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = hook

    def uninstall() -> None:
        if sys.excepthook is hook:
            sys.excepthook = previous

    return uninstall


def install_threading_hook(handler: ThreadHandler) -> Callable[[], None]:
    previous = threading.excepthook

    def hook(args: threading.ExceptHookArgs) -> None:
        try:
            if args.exc_value is not None:
                handler(args.exc_value, args.exc_traceback)
        except Exception:
            pass
        try:
            previous(args)
        except Exception:
            pass

    threading.excepthook = hook

    def uninstall() -> None:
        if threading.excepthook is hook:
            threading.excepthook = previous

    return uninstall


def install_asyncio_hook(handler: AsyncioHandler) -> Callable[[], None]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as e:
        raise RuntimeError(
            "hooks=['asyncio'] requires a running event loop; "
            "call ml3error.init() from inside a coroutine or after the loop starts."
        ) from e

    previous = loop.get_exception_handler()

    def handler_wrapper(loop_: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, BaseException):
            try:
                handler(exc)
            except Exception:
                pass
        if previous is not None:
            try:
                previous(loop_, context)
            except Exception:
                pass
        else:
            loop_.default_exception_handler(context)

    loop.set_exception_handler(handler_wrapper)

    def uninstall() -> None:
        if loop.get_exception_handler() is handler_wrapper:
            loop.set_exception_handler(previous)

    return uninstall
