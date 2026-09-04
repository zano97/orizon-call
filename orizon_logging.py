"""
Centralised logging configuration for Orizon Call.

Console handler at the caller-selected level (DEBUG/INFO/WARNING/ERROR)
plus a rotating file handler at ~/.orizon-call/logs/orizon.log so users
can attach logs when reporting bugs.

Modules just do `logger = logging.getLogger(__name__)` and inherit
this configuration. Calling setup_logging() more than once is a no-op.
"""

import logging
import logging.handlers
import sys
import threading
from pathlib import Path


_CONFIGURED = False
_ROOT_NAME = "orizon"


def _log_dir() -> Path:
    base = Path.home() / ".orizon-call"
    logs = base / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


def setup_logging(verbose: bool = False, quiet: bool = False) -> logging.Logger:
    """Configure the 'orizon' logger tree. Returns the root logger."""
    global _CONFIGURED
    root = logging.getLogger(_ROOT_NAME)
    if _CONFIGURED:
        return root

    root.setLevel(logging.DEBUG)  # handlers filter further
    root.propagate = False

    console_level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    # Under pythonw.exe (Windows launcher/shortcut) there is no console and
    # sys.stderr is None: a StreamHandler on it would raise on every record.
    if sys.stderr is not None:
        console = logging.StreamHandler(stream=sys.stderr)
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(console)

    try:
        log_path = _log_dir() / "orizon.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
        ))
        root.addHandler(file_handler)
    except OSError:
        # Disk full / read-only home? Console-only is acceptable.
        pass

    _CONFIGURED = True
    return root


def install_thread_excepthook() -> None:
    """Route uncaught exceptions from worker threads (writer, watchdog,
    API handlers, start/stop workers) into the log instead of a bare
    traceback on a stderr that may not even exist under pythonw."""
    logger = logging.getLogger(f"{_ROOT_NAME}.threads")

    def _hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        thread_name = args.thread.name if args.thread is not None else "?"
        logger.error(
            "Unhandled exception in thread %s", thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _hook


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under 'orizon.<name>'."""
    if name.startswith(_ROOT_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_NAME}.{name}")
