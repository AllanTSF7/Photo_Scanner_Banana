"""Read-only Immich duplicate check: exact-checksum verification for exported scans, plus our own perceptual
dHash comparison against the configured library's thumbnails (see banana/immich/client.py's docstring for why
Immich itself cannot answer "is this external image already in the library" - there is no such API).

Both passes only ever run when the operator explicitly triggers a check (`ImmichCheckController.start`) - never
automatically on ingest - and only when Immich checking is enabled (`banana.immich.client.build_client` returns
`None` otherwise, which is the one choke point every caller here goes through).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from sqlmodel import Session, select

from banana import core, db, imaging
from banana.config import Settings
from banana.core import corrections
from banana.immich.client import ImmichClient, build_client
from banana.immich.settings import get_effective
from banana.models import Export, ImmichAssetHash, Scan, ScanStatus, utcnow


@dataclass
class ExactCheckStats:
    checked: int = 0
    matched: int = 0


@dataclass
class AssetRefreshStats:
    scanned: int = 0
    refreshed: int = 0


@dataclass
class MatchStats:
    matched: int = 0


def check_exact(client: ImmichClient, session: Session) -> ExactCheckStats:
    """Verify every exported scan's file against Immich by SHA1. This checks the file that was actually
    written to the Immich library folder (computed at export time), not the archived original - `Exporter`
    re-encodes/rewrites EXIF on export, so the archived original essentially never byte-matches anything Immich
    stores. This is export *verification* ("did this reach Immich already"), not pre-export prevention - it
    can only confirm a match once export has actually happened."""
    stats = ExactCheckStats()
    items: list[tuple[str, str]] = []
    by_key: dict[str, tuple[Export, str]] = {}
    for export in session.exec(select(Export)):
        for side, checksum in (("front", export.front_sha1), ("back", export.back_sha1)):
            if checksum:
                key = f"{export.id}:{side}"
                items.append((key, checksum))
                by_key[key] = (export, side)
    if not items:
        return stats
    for key, result in client.bulk_upload_check(items).items():
        export, side = by_key[key]
        asset_id = result.get("assetId") if result.get("action") == "reject" else None
        setattr(export, f"immich_{side}_id", asset_id)
        export.immich_checked_at = utcnow()
        session.add(export)
        stats.checked += 1
        stats.matched += bool(asset_id)
    session.commit()
    return stats


def refresh_asset_hashes(
    client: ImmichClient, session: Session, library_id: str, on_progress: Callable[[AssetRefreshStats], None] | None = None,
) -> AssetRefreshStats:
    """Paginate the configured library; fetch a thumbnail and compute our own dHash only for assets that are
    new or whose checksum changed since last time, so a repeat run is cheap once the library is cached."""
    stats = AssetRefreshStats()
    page = 1
    while True:
        result = client.search_metadata_page(library_id, page)
        assets = (result.get("assets") or {}).get("items") or result.get("items") or []
        if not assets:
            break
        for asset in assets:
            stats.scanned += 1
            if on_progress and stats.scanned % 50 == 0:
                on_progress(stats)
            asset_id, checksum = asset["id"], asset.get("checksum")
            cached = session.get(ImmichAssetHash, asset_id)
            if cached is not None and checksum is not None and cached.checksum == checksum:
                continue
            try:
                gray = imaging.analysis_gray_bytes(client.thumbnail_bytes(asset_id), 512)
                dhash_hex = f"{core.dhash(gray):016x}"
            except Exception:  # noqa: BLE001 - one bad thumbnail shouldn't stop the whole refresh
                continue
            row = cached or ImmichAssetHash(asset_id=asset_id)
            row.checksum, row.dhash_hex, row.fetched_at = checksum, dhash_hex, utcnow()
            session.add(row)
            stats.refreshed += 1
        session.commit()
        next_page = (result.get("assets") or {}).get("nextPage") or result.get("nextPage")
        if not next_page:
            break
        page = next_page if isinstance(next_page, int) else page + 1
    return stats


def _scan_fingerprints(scan: Scan) -> list[int]:
    """The scan's dHash at 0/90/180/270 degrees. The stored `dhash_hex` is taken from the unrotated crop (it has
    to stay that way for local rescan detection), but Immich's copy of a photo is upright, and a scan fed
    through the feeder sideways or upside down matches it only when turned the same way."""
    stored = int(scan.dhash_hex, 16)
    try:
        crop = tuple(scan.front_crop) if scan.front_crop else None
        gray = imaging.analysis_gray(Path(scan.front_path), imaging.Edit(crop, 0), 512)
    except Exception:  # noqa: BLE001 - file gone or unreadable: fall back to the stored, unrotated fingerprint
        return [stored]
    return [stored] + [core.dhash(np.ascontiguousarray(np.rot90(gray, k))) for k in (1, 2, 3)]


def _distances(asset_hashes: np.ndarray, value: int) -> np.ndarray:
    """Hamming distance from `value` to every asset hash at once (83k+ assets per scan, so not a Python loop)."""
    xor = asset_hashes ^ np.uint64(value)
    return np.unpackbits(xor.view(np.uint8).reshape(-1, 8), axis=1).sum(axis=1)


def find_matches(session: Session, settings: Settings) -> MatchStats:
    """Compare every (non-rejected) scan's own dHash - at all four rotations - against the cached Immich asset
    hashes, with the same algorithm and threshold already used for local rescan detection
    (banana/ingest/service.py::analyze_scan), just against a different pool. Never auto-applied: only ever
    recorded as a suggestion."""
    stats = MatchStats()
    hashes = list(session.exec(select(ImmichAssetHash).where(ImmichAssetHash.dhash_hex.is_not(None))))
    if not hashes:
        return stats
    asset_ids = [h.asset_id for h in hashes]
    asset_hashes = np.array([int(h.dhash_hex, 16) for h in hashes], dtype=np.uint64)
    produced = corrections.producers(settings)["immich_duplicate"]
    scans = session.exec(select(Scan).where(Scan.dhash_hex.is_not(None), Scan.status != ScanStatus.REJECTED.value))
    for scan in scans:
        best: tuple[str, int] | None = None
        for value in _scan_fingerprints(scan):
            distances = _distances(asset_hashes, value)
            i = int(distances.argmin())
            d = int(distances[i])
            if d <= settings.analysis.dhash_max_distance and (best is None or d < best[1]):
                best = (asset_ids[i], d)
        if best is None:
            continue
        asset_id, distance = best
        scan.immich_duplicate_asset_id = asset_id
        corrections.suggest(scan, "immich_duplicate", {"asset_id": asset_id, "distance": distance}, produced)
        session.add(scan)
        stats.matched += 1
    session.commit()
    return stats


@dataclass
class CheckRun:
    state: str = "idle"  # idle | running | done | failed
    phase: str | None = None  # exact | assets | matching
    started_at: str | None = None
    finished_at: str | None = None
    message: str = ""
    stats: dict | None = None
    done: int = 0  # library photos read so far, and how many Immich says there are (for the progress bar)
    total: int | None = None


class ImmichCheckController:
    """One check at a time, run in a background thread; polled by the UI - the same shape
    `banana.scanner.sane.ScanController` already uses for the SANE scan (state/phase/lock, polled via a status
    endpoint), which established this pattern for exactly this kind of problem: the operator clicks a button,
    something slow and network-bound happens, the UI polls a status. Deliberately not `banana/jobs.py`'s
    worker-queue - that's the right home if this ever needs the separate GPU worker process in production, but
    routing through it here would mean the check silently does nothing unless a second `banana worker` process
    is also running, which isn't true of the normal single-process dev/small-deployment setup.
    """

    def __init__(self, engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings
        self._lock = threading.Lock()
        self._run = CheckRun()

    def status(self) -> dict:
        with self._lock:
            return dict(self._run.__dict__)

    def start(self) -> dict:
        with self._lock:
            if self._run.state == "running":
                raise RuntimeError("a check is already running")
            with db.session(self._engine) as session:
                cfg = get_effective(session, self._settings)
            if build_client(cfg) is None:
                raise RuntimeError("Immich checking isn't enabled")
            self._run = CheckRun(state="running", phase="exact", started_at=datetime.now().isoformat(timespec="seconds"))
        threading.Thread(target=self._work, daemon=True, name="immich-check").start()
        return self.status()

    def _progress(self, stats: AssetRefreshStats) -> None:
        with self._lock:
            self._run.done = stats.scanned
            self._run.message = f"Reading the Immich library: {stats.scanned} photos looked at, {stats.refreshed} new"

    def _phase(self, phase: str) -> None:
        with self._lock:
            self._run.phase = phase

    def _work(self) -> None:
        run = self._run
        try:
            with db.session(self._engine) as session:
                cfg = get_effective(session, self._settings)
                with build_client(cfg) as client:  # already confirmed non-None in start()
                    exact = check_exact(client, session)
                    self._phase("assets")
                    try:
                        images = client.statistics().get("images")
                        with self._lock:
                            run.total = int(images) if images is not None else None
                    except Exception:  # noqa: BLE001 - the bar is a nicety; the check itself doesn't need the total
                        pass
                    refreshed = refresh_asset_hashes(client, session, cfg.library_id, self._progress)
                    self._phase("matching")
                    matched = find_matches(session, self._settings)
            with self._lock:
                run.state, run.phase, run.done = "done", None, refreshed.scanned
                run.stats = {
                    "exports_checked": exact.checked, "exports_matched": exact.matched,
                    "assets_scanned": refreshed.scanned, "assets_refreshed": refreshed.refreshed,
                    "scans_matched": matched.matched,
                }
                run.message = (
                    f"Checked {exact.checked} exported file(s), {exact.matched} already in Immich; "
                    f"{refreshed.scanned} library asset(s) scanned ({refreshed.refreshed} new/changed); "
                    f"{matched.matched} scan(s) look like something already in Immich"
                )
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, same as ScanController
            with self._lock:
                run.state, run.phase, run.message = "failed", None, str(exc)
        finally:
            with self._lock:
                run.finished_at = datetime.now().isoformat(timespec="seconds")
