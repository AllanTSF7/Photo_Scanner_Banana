"""One ingest at a time, from every entry point: a finished scan, the Ingest inbox button, scan recovery,
and the inbox watcher. Before this, a scan's own ingest and the button could run at once over the same files.
"""

from __future__ import annotations

import logging
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy.engine import Engine

from banana import db
from banana.config import Settings
from banana.ingest import service

log = logging.getLogger(__name__)


class IngestRunner:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        self._lock = threading.Lock()
        self._state = threading.Lock()  # guards the fields below
        self.running_since: str | None = None
        self.last_run: str | None = None
        self.last_reason: str | None = None
        self.last_result: dict | None = None
        self.last_error: str | None = None
        self.runs = 0  # bumps whenever an ingest created scans, so the UI knows to refresh
        self.orphans: list[str] = []
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        self._held = threading.Event()  # set by hold(): the watcher stays out

    # ------------------------------------------------------------ running
    def run(self, reason: str = "manual", trusted: Iterable[str] = (), set_aside_rescans: Iterable[str] = ()) -> dict:
        """Ingest the inbox. Waits for an ingest already in progress, then runs (they queue, never overlap).
        `set_aside_rescans`: see `service.ingest_inbox`."""
        with self._lock:
            with self._state:
                self.running_since = datetime.now().isoformat(timespec="seconds")
            try:
                with db.session(self.engine) as session:
                    report = service.ingest_inbox(session, self.settings, trusted=trusted,
                                                  set_aside_rescans=set_aside_rescans)
                result = report.__dict__
                error = None
            except Exception as exc:
                log.exception("ingest (%s) failed", reason)
                with self._state:
                    self.last_error = str(exc)
                raise
            finally:
                with self._state:
                    self.running_since = None
                    self.last_run = datetime.now().isoformat(timespec="seconds")
                    self.last_reason = reason
            with self._state:
                self.last_result, self.last_error = result, error
                if result["created"]:
                    self.runs += 1
        if result["created"] or result["failed"] or result["unreadable"] or result["already_scanned"]:
            log.info(
                "ingest (%s): %d created, %d possible rescan(s), %d skipped, %d not ready, %d failed, %d unreadable, "
                "%d already scanned",
                reason, len(result["created"]), len(result["possible_duplicates"]), len(result["skipped"]),
                len(result["not_ready"]), len(result["failed"]), len(result["unreadable"]),
                len(result["already_scanned"]),
            )
        return result

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @contextmanager
    def hold(self):
        """Keep the watcher from starting an ingest, e.g. while a recovery places pages and then ingests them
        itself: recovered files keep their old timestamps, so the watcher would otherwise take them as settled
        and ingest them first, without the already-scanned check."""
        self._held.set()
        try:
            yield
        finally:
            self._held.clear()

    # ------------------------------------------------------------ watcher
    def start_watching(self) -> None:
        if self._watcher is not None or not self.settings.ingest.watch_inbox:
            return
        self._watcher = threading.Thread(target=self._watch, daemon=True, name="inbox-watcher")
        self._watcher.start()

    def stop_watching(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        while not self._stop.wait(self.settings.ingest.watch_seconds):
            try:
                if not self.busy and not self._held.is_set() and self._has_files():
                    self.run(reason="watcher")
            except Exception:  # noqa: BLE001 - logged in run(); the watcher must keep going
                pass

    def _has_files(self) -> bool:
        inbox = self.settings.paths.inbox
        return inbox.exists() and any(p.is_file() for p in inbox.iterdir())

    # ------------------------------------------------------------ problems the operator should see
    def _set_aside(self, name: str) -> list[str]:
        folder = self.settings.paths.inbox / name
        return sorted(p.name for p in folder.iterdir() if p.is_file()) if folder.exists() else []

    def _put_back(self, name: str) -> int:
        folder = self.settings.paths.inbox / name
        moved = 0
        for path in sorted(folder.iterdir()) if folder.exists() else []:
            target = self.settings.paths.inbox / path.name
            if path.is_file() and not target.exists():
                shutil.move(path, target)
                moved += 1
        return moved

    def unreadable(self) -> list[str]:
        return self._set_aside(service.UNREADABLE_DIR)

    def retry_unreadable(self) -> int:
        """Put every file from inbox/_unreadable back into the inbox for another try."""
        moved = self._put_back(service.UNREADABLE_DIR)
        service._failures.clear()
        return moved

    def already_scanned(self) -> list[str]:
        return self._set_aside(service.ALREADY_SCANNED_DIR)

    def put_back_already_scanned(self) -> int:
        """Put every recovered photo from inbox/_already_scanned back into the inbox; the next ingest adds them to
        review, flagged as possible rescans."""
        return self._put_back(service.ALREADY_SCANNED_DIR)

    def check_orphans(self) -> list[str]:
        with db.session(self.engine) as session:
            found = service.archive_orphans(session, self.settings)
        archive = self.settings.paths.archive
        names = [str(Path(p).relative_to(archive)) for p in found]
        with self._state:
            self.orphans = names
        if names:
            log.warning("%d archived file(s) have no scan in the database, e.g. %s", len(names), names[:3])
        return names

    def status(self) -> dict:
        with self._state:
            return {
                "watching": self._watcher is not None and self._watcher.is_alive(),
                "watch_seconds": self.settings.ingest.watch_seconds,
                "running": self.running_since is not None,
                "running_since": self.running_since,
                "last_run": self.last_run,
                "last_reason": self.last_reason,
                "last_result": self.last_result,
                "last_error": self.last_error,
                "runs": self.runs,
                "unreadable": self.unreadable(),
                "already_scanned": self.already_scanned(),
                "orphans": len(self.orphans),
                "orphan_examples": self.orphans[:5],
            }
