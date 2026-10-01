"""Log setup for the server: timestamped, rotating, and quiet about the UI's own polling.

The UI polls scanner/health/summary every second or two and fetches images constantly; logging each of
those buried the 20 real errors of the first production day under ~29k lines with no timestamps. Here a
successful GET is not logged, while writes, errors, and the app's own events (scan runs, ingest,
export) are - each with a timestamp, so an incident can be lined up with files on disk.
"""

from __future__ import annotations

import logging
from pathlib import Path

LOG_NAME = "photoscanner.log"
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class QuietReads(logging.Filter):
    """Drop uvicorn access records for successful GET/HEAD requests; keep writes and anything >= 400."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        method, status = args[1], args[4]
        try:
            status = int(status)
        except (TypeError, ValueError):
            return True
        return not (method in ("GET", "HEAD") and status < 400)


def uvicorn_log_config(log_dir: Path | None) -> dict:
    """A `log_config` for uvicorn.run. With a log_dir, everything goes to a rotating file; without one
    (a console run), to stderr. The `banana` logger shares the same handler."""
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(log_dir / LOG_NAME),
            "maxBytes": MAX_BYTES,
            "backupCount": BACKUPS,
            "encoding": "utf-8",
            "formatter": "plain",
        }
    else:
        handler = {"class": "logging.StreamHandler", "formatter": "plain"}
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"plain": {"format": FORMAT}},
        "filters": {"quiet_reads": {"()": QuietReads}},
        "handlers": {
            "main": handler,
            "access": {**handler, "filters": ["quiet_reads"]},
        },
        "loggers": {
            "uvicorn": {"handlers": ["main"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["main"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
            "banana": {"handlers": ["main"], "level": "INFO", "propagate": False},
        },
    }
