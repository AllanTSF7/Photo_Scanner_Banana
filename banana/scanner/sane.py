"""Direct scanning through SANE `scanimage` (Linux).

Pages are written into a hidden staging folder inside the inbox (ingest only reads top-level files),
then renamed in scan order into the pairing convention: odd pages -> <run>_NNNN.jpg (front),
even pages -> <run>_NNNN_b.jpg (back) when scanning duplex.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from banana.config import ScannerConfig

log = logging.getLogger(__name__)

STAGING_PREFIX = ".scanning-"
PAGE_PATTERN = "page_%04d.jpg"
_PAGE_RE = re.compile(r"^page_(\d{4})\.jpg$")


def is_online(cfg: ScannerConfig, timeout: float = 1.5) -> bool:
    if not cfg.host:
        return False
    try:
        with socket.create_connection((cfg.host, cfg.port), timeout=timeout):
            return True
    except OSError:
        return False


def scanimage_args(cfg: ScannerConfig, staging: Path, count: str = "all") -> list[str]:
    args = [
        cfg.scanimage,
        "-d", cfg.device,
        "--source", cfg.source,
        "--mode", cfg.mode,
        "--resolution", str(cfg.resolution),
        f"--adf-crp={'yes' if cfg.auto_crop else 'no'}",
        f"--adf-skew={'yes' if cfg.skew_correction else 'no'}",
        "--format=jpeg",
        f"--batch={staging / PAGE_PATTERN}",
    ]
    if count == "one":
        args.append(f"--batch-count={2 if cfg.duplex else 1}")
    else:
        # A ceiling, not a forced count: scanimage's own "out of documents" detection still stops early on a
        # smaller stack. This just keeps "Whole stack" from running past the ADF hopper's real capacity.
        args.append(f"--batch-count={cfg.max_feeder_count * (2 if cfg.duplex else 1)}")
    return args


def staged_pages(staging: Path) -> list[Path]:
    pages = [p for p in staging.iterdir() if _PAGE_RE.match(p.name)] if staging.exists() else []
    return sorted(pages, key=lambda p: int(_PAGE_RE.match(p.name).group(1)))


def place_pages(pages: list[Path], inbox: Path, run_name: str, duplex: bool, first_side: str = "front") -> list[Path]:
    """Rename staged pages into the inbox using the front/back naming convention.

    first_side="back": the scanner reads the photo's back first (FF-680W with photos loaded face down),
    so the odd page of each pair is the back.
    """
    placed = []
    step = 2 if duplex else 1
    for index in range(0, len(pages), step):
        number = index // step + 1
        pair = pages[index:index + step]
        if duplex and first_side == "back" and len(pair) == 2:
            pair = [pair[1], pair[0]]
        front = inbox / f"{run_name}_{number:04d}.jpg"
        os.replace(pair[0], front)
        placed.append(front)
        if len(pair) == 2:
            back = inbox / f"{run_name}_{number:04d}_b.jpg"
            os.replace(pair[1], back)
            placed.append(back)
    return placed


@dataclass
class ScanRun:
    state: str = "idle"  # idle | scanning | done | failed
    phase: str | None = None  # while scanning: "feeding" (scanimage running) | "ingesting"
    destination: str = "review"  # review: ingest into the app when done | inbox: leave files in the inbox
    count: str = "all"  # all: whole feeder | one: a single photo
    run_name: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    pages: int = 0
    files: list[str] = field(default_factory=list)
    message: str = ""
    warning: str = ""  # set when the run finished but something needs the operator's attention
    ingest: dict | None = None


class ScanController:
    """One scan at a time, run in a background thread; state is polled by the UI."""

    def __init__(self, cfg: ScannerConfig, inbox: Path) -> None:
        self.cfg = cfg
        self.inbox = inbox
        self._lock = threading.Lock()
        self._run = ScanRun()
        self._staging: Path | None = None

    def status(self) -> dict:
        with self._lock:
            run = self._run
            # Count staged pages only while scanimage runs; afterwards they've been moved and run.pages is final.
            if run.state == "scanning" and run.phase == "feeding" and self._staging is not None:
                run.pages = len(staged_pages(self._staging))
            return dict(run.__dict__)

    def start(self, on_complete=None, destination: str | None = None, count: str = "all") -> dict:
        """Start a scan. `on_complete` (the ingest) runs only when the destination is "review"."""
        destination = destination or self.cfg.after_scan
        if destination not in ("review", "inbox"):
            raise ValueError(f"unknown destination {destination!r}")
        if count not in ("all", "one"):
            raise ValueError(f"unknown count {count!r}")
        with self._lock:
            if self._run.state == "scanning":
                raise RuntimeError("a scan is already running")
            if not self.cfg.host and not self.cfg.sane_device:
                raise RuntimeError("no scanner configured (scanner.host)")
            name = f"scan{datetime.now():%Y%m%d%H%M%S}"
            self._staging = self.inbox / f"{STAGING_PREFIX}{name}"
            self._run = ScanRun(
                state="scanning", phase="feeding", destination=destination, count=count, run_name=name,
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
        callback = on_complete if destination == "review" else None
        threading.Thread(target=self._work, args=(callback,), daemon=True, name=f"scan-{name}").start()
        return self.status()

    def _work(self, on_complete) -> None:
        staging = self._staging
        run = self._run
        log.info("scan %s started (%s, destination %s)", run.run_name, run.count, run.destination)
        try:
            self.inbox.mkdir(parents=True, exist_ok=True)
            staging.mkdir(parents=True)
            if self.cfg.effective_backend == "twain":
                from banana.scanner.twain_scan import ScanInterrupted, acquire_pages

                try:
                    acquire_pages(self.cfg, staging, run.count)
                except ScanInterrupted as exc:
                    # The pages that arrived are fine: place and ingest them, and say what stopped the driver.
                    log.warning("scan %s: %s", run.run_name, exc, exc_info=exc.cause)
                    with self._lock:
                        run.warning = f"The scanner stopped with an error ({exc.cause}). All {len(exc.pages)} page(s) it sent were kept."
                pages = staged_pages(staging)
                if not pages:
                    raise RuntimeError("The scanner returned no pages.")
            else:
                try:
                    proc = subprocess.run(
                        scanimage_args(self.cfg, staging, run.count),
                        capture_output=True, text=True, timeout=self.cfg.timeout_seconds,
                    )
                except FileNotFoundError as exc:
                    raise RuntimeError(
                        f"SANE's scanimage wasn't found ({self.cfg.scanimage}). On Windows, set "
                        "scanner.backend to \"twain\" (or \"auto\") to use Epson's driver instead."
                    ) from exc
                pages = staged_pages(staging)
                output = (proc.stderr or proc.stdout or "").strip()
                if not pages:
                    raise RuntimeError(_explain(output) or f"scanimage exited with code {proc.returncode}")
            with self._lock:
                run.pages, run.phase = len(pages), "ingesting"
            files = place_pages(pages, self.inbox, run.run_name, self.cfg.duplex, self.cfg.first_side)
            photos = (len(pages) + 1) // 2 if self.cfg.duplex else len(pages)
            message = f"Scanned {photos} photo(s), {len(pages)} page(s)"
            if self.cfg.duplex and len(pages) % 2:
                message += "; odd page count, the last photo has no back"
            if run.count == "all" and photos >= self.cfg.max_feeder_count:
                message += f"; feeder capped at {self.cfg.max_feeder_count}, scan again for more"
            if run.destination == "inbox":
                message += f"; {len(files)} file(s) left in the inbox"
            with self._lock:
                run.pages, run.files, run.message = len(pages), [f.name for f in files], message
            if on_complete is not None:
                result = on_complete()
                with self._lock:
                    run.ingest = result
            with self._lock:
                run.state, run.phase = "done", None
            log.info("scan %s done: %s", run.run_name, message)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            log.exception("scan %s failed", run.run_name)
            with self._lock:
                run.state, run.phase, run.message = "failed", None, str(exc)
        finally:
            with self._lock:
                run.finished_at = datetime.now().isoformat(timespec="seconds")
            # Keep the folder if anything was left behind (a failure mid-rename, a BMP that couldn't be
            # converted), so nothing is lost; stranded() and recover() pick it up from there.
            if staging is not None and staging.exists() and not any(staging.iterdir()):
                shutil.rmtree(staging, ignore_errors=True)
            elif staging is not None and staging.exists():
                left = len(list(staging.iterdir()))
                log.warning("scan %s left %d file(s) in %s", run.run_name, left, staging)
                with self._lock:
                    run.warning = (run.warning + " " if run.warning else "") + (
                        f"{left} file(s) could not be moved into the inbox; use Recover scans to retry."
                    )

    def stranded(self) -> list[dict]:
        """Staging folders that still hold files, other than the run in progress."""
        active = self._staging if self._run.state == "scanning" else None
        return [
            {"run": d.name.removeprefix(STAGING_PREFIX), "files": sum(1 for _ in d.iterdir())}
            for d in sorted(self.inbox.glob(f"{STAGING_PREFIX}*")) if d.is_dir() and d != active and any(d.iterdir())
        ] if self.inbox.exists() else []

    def recover(self) -> dict:
        """Finish every stranded run: convert leftover BMPs, place pages into the inbox under the run's own
        name (so names can't collide with anything already ingested), and remove folders left empty.
        Ingest picks the files up from the inbox like any other scan."""
        with self._lock:
            if self._run.state == "scanning":
                raise RuntimeError("a scan is running; recover once it finishes")
        from banana.scanner.twain_scan import convert_leftovers

        recovered, kept = [], []
        for item in self.stranded():
            folder = self.inbox / f"{STAGING_PREFIX}{item['run']}"
            try:
                convert_leftovers(folder, self.cfg.mode)
                pages = staged_pages(folder)
                files = place_pages(pages, self.inbox, item["run"], self.cfg.duplex, self.cfg.first_side) if pages else []
            except OSError:
                log.exception("recovering %s failed", folder)
                files = []
            if not any(folder.iterdir()):
                folder.rmdir()
            else:
                kept.append({"run": item["run"], "files": sum(1 for _ in folder.iterdir())})
            if files:
                recovered.append({"run": item["run"], "files": len(files)})
            log.info("recovered scan %s: %d file(s) placed in the inbox", item["run"], len(files))
        return {"recovered": recovered, "kept": kept}


def _explain(output: str) -> str:
    lowered = output.lower()
    if "out of documents" in lowered or "no docs" in lowered:
        return "No photos in the feeder. Load the stack and try again."
    if "invalid argument" in lowered or "no such device" in lowered:
        return f"Scanner not found. {output}"
    if "busy" in lowered:
        return "Scanner is busy (another program may be using it)."
    return output
