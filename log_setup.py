"""Shared logging setup for all migration scripts.

Provides a single session log (latest.log) and a daily rotating archive (migrate.log).
All scripts write to the same files, distinguished by logger name in the format.
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler

DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(DIR, "logs")

os.makedirs(LOG_DIR, exist_ok=True)

LATEST_LOG = os.path.join(LOG_DIR, "latest.log")
DAILY_LOG = os.path.join(LOG_DIR, "migrate.log")

_FILE_FMT = logging.Formatter(
    "%(asctime)s [%(name)s] %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_CONSOLE_FMT = logging.Formatter("%(message)s")

_latest_handler = None
_daily_handler = None


def _get_file_handlers():
    """Lazily create shared file handlers (one instance each)."""
    global _latest_handler, _daily_handler

    if _latest_handler is None:
        _latest_handler = logging.FileHandler(LATEST_LOG, mode="a", encoding="utf-8")
        _latest_handler.setLevel(logging.DEBUG)
        _latest_handler.setFormatter(_FILE_FMT)

    if _daily_handler is None:
        _daily_handler = TimedRotatingFileHandler(
            DAILY_LOG, when="midnight", backupCount=0, encoding="utf-8",
        )
        _daily_handler.setLevel(logging.DEBUG)
        _daily_handler.setFormatter(_FILE_FMT)
        _daily_handler.namer = lambda name: name.replace(".log.", ".") + ".log"

    return _latest_handler, _daily_handler


def get_logger(name):
    """Return a named logger with console + latest.log + migrate.log handlers."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(_CONSOLE_FMT)
    logger.addHandler(console)

    latest, daily = _get_file_handlers()
    logger.addHandler(latest)
    logger.addHandler(daily)

    return logger


def reset_latest():
    """Truncate latest.log at session start."""
    open(LATEST_LOG, "w").close()
