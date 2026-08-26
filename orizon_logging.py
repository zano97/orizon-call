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


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under 'orizon.<name>'."""
    if name.startswith(_ROOT_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_ROOT_NAME}.{name}")
