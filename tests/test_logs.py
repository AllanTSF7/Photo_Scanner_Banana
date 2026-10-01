"""banana.logs: timestamped rotating log that keeps writes and errors and drops the UI's polling."""

from __future__ import annotations

import logging
import logging.config
import re

from banana.logs import LOG_NAME, QuietReads, uvicorn_log_config


def _access(method: str, path: str, status: int) -> logging.LogRecord:
    # Same shape uvicorn's access logger emits: (client, method, path, http_version, status)
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", method, path, "1.1", status), None,
    )


def test_successful_reads_are_dropped_but_writes_and_errors_kept():
    f = QuietReads()
    assert not f.filter(_access("GET", "/api/scanner", 200))
    assert not f.filter(_access("GET", "/api/scans/8/image/front", 304))
    assert f.filter(_access("GET", "/api/scans/8", 500))
    assert f.filter(_access("GET", "/api/scans/8", 404))
    assert f.filter(_access("PATCH", "/api/scans/8", 200))
    assert f.filter(_access("POST", "/api/export", 200))


def test_unexpected_record_shapes_are_never_dropped():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "plain message", None, None)
    assert QuietReads().filter(record)


def test_log_config_writes_timestamped_lines_to_a_rotating_file(tmp_path):
    log_dir = tmp_path / "logs"
    logging.config.dictConfig(uvicorn_log_config(log_dir))
    try:
        logging.getLogger("banana.scanner").warning("scan stopped after 33 pages")
        access = logging.getLogger("uvicorn.access")
        access.handle(_access("GET", "/api/health", 200))
        access.handle(_access("PATCH", "/api/scans/8", 500))
    finally:
        for name in ("banana", "uvicorn", "uvicorn.error", "uvicorn.access"):
            for h in list(logging.getLogger(name).handlers):
                h.close()
                logging.getLogger(name).removeHandler(h)

    text = (log_dir / LOG_NAME).read_text(encoding="utf-8")
    assert re.search(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} WARNING banana.scanner: scan stopped", text, re.M)
    assert "PATCH /api/scans/8" in text
    assert "/api/health" not in text
    handler = uvicorn_log_config(log_dir)["handlers"]["main"]
    assert handler["class"] == "logging.handlers.RotatingFileHandler" and handler["backupCount"] >= 1
