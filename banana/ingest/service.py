"""Inbox ingest: pair scanner files, move them to the archive, run the cheap per-image checks."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import func
from sqlmodel import Session, select

import numpy as np

from banana import core, imaging
from banana.core import corrections, dictionary
from banana.analysis import autocorrect, ocr
from banana.analysis.date_parse import parse_best
from banana.analysis.ocr import orientation
from banana.analysis.entities import Entities, extract
from banana.config import Settings
from banana.ingest.pairing import pair_files
from banana.models import Batch, Scan, ScanStatus


log = logging.getLogger(__name__)

UNREADABLE_DIR = "_unreadable"  # inside the inbox; ingest only reads top-level files, so it is never re-read
ALREADY_INGESTED_DIR = "_already_ingested"


@dataclass
class IngestReport:
    batch: str | None = None
    created: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # same name AND same content as an ingested scan
    unmatched: list[str] = field(default_factory=list)
    possible_duplicates: list[int] = field(default_factory=list)
    not_ready: list[str] = field(default_factory=list)  # still being written: picked up next time
    failed: list[dict] = field(default_factory=list)  # {"name", "error"}; left in the inbox, retried next time
    unreadable: list[str] = field(default_factory=list)  # failed too often: moved to inbox/_unreadable/


# Attempts per photo that failed to open/decode, kept for the life of the process: {base: (count, first_seen)}.
_failures: dict[str, tuple[int, float]] = {}


def _move(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.move(src, dest)
    return dest


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _group_files(group) -> list[Path]:
    return [p for p in (group.original, group.enhanced, group.back) if p is not None]


def _ready(path: Path, now: float, settle_seconds: float) -> bool:
    """A file another program is still writing has a fresh modification time; leave it for the next pass."""
    try:
        return now - path.stat().st_mtime >= settle_seconds
    except OSError:
        return False


def _unique_batch_name(session: Session, base: str) -> str:
    """Batch names are unique and timestamped to the second; two ingests in the same second (a recovery right
    after a scan's own ingest) would otherwise fail on the unique constraint after files were already moved."""
    name, n = base, 2
    while session.exec(select(Batch).where(Batch.name == name)).first():
        name, n = f"{base}-{n}", n + 1
    return name


def ingest_inbox(
    session: Session, settings: Settings, batch_name: str | None = None, *, trusted: Iterable[str] = (),
) -> IngestReport:
    """Ingest every ready photo in the inbox, one photo per transaction.

    Each photo is analysed (crop, dHash, blank back, OCR) BEFORE anything is written, then its files are moved
    to the archive and its row committed straight away. The SQLite write lock is held for milliseconds per
    photo instead of for the whole batch (which made the operator's saves fail with "database is locked"),
    and a crash can at most leave the one photo in flight. If the commit fails, its files go back to the inbox.

    `trusted`: file names this app just placed itself (a finished scan run), exempt from the "still being
    written" check. Anything else must be untouched for `ingest.settle_seconds` first.
    """
    inbox = settings.paths.inbox
    files = [p for p in inbox.iterdir() if p.is_file()] if inbox.exists() else []
    trusted = set(trusted)
    now = time.time()
    ready = [p for p in files if p.name in trusted or _ready(p, now, settings.ingest.settle_seconds)]
    paired = pair_files(ready, settings.pairing.pattern)
    report = IngestReport(unmatched=[p.name for p in paired.unmatched])
    report.not_ready = sorted(p.name for p in files if p not in ready)
    report.skipped += [g.base for g in paired.backs_without_front]
    if not paired.groups:
        return report

    name = _unique_batch_name(session, batch_name or f"inbox-{datetime.now():%Y%m%d-%H%M%S}")
    session.rollback()  # end the read transaction: nothing below should hold the database open between photos
    batch: Batch | None = None
    archive = settings.paths.archive / name

    for group in paired.groups:
        try:
            key, digest = _source_key(session, group, settings)
            if key is None:
                _move_aside(group, inbox / ALREADY_INGESTED_DIR)
                report.skipped.append(group.base)
                continue
            scan = _analyzed_scan(session, group, key, digest, settings)
        except (OSError, ValueError) as exc:  # can't open/decode: contained to this photo, retried next time
            session.rollback()
            _record_failure(group, exc, inbox, report, settings)
            continue
        session.rollback()  # analysis only read; drop that read transaction before writing

        moved: list[tuple[Path, Path]] = []
        try:
            for src in _group_files(group):
                moved.append((src, _move(src, archive)))
            new_paths = {src: dest for src, dest in moved}
            scan.front_path = str(new_paths[Path(scan.front_path)])
            if scan.front_enhanced_path:
                scan.front_enhanced_path = str(new_paths[Path(scan.front_enhanced_path)])
            if scan.back_path:
                scan.back_path = str(new_paths[Path(scan.back_path)])
            if batch is None:
                batch = Batch(name=name)
                session.add(batch)
                session.flush()
                report.batch = name
            scan.batch_id = batch.id
            session.add(scan)
            session.commit()
        except Exception:
            session.rollback()
            for src, dest in reversed(moved):  # never leave archived files without a row
                if dest.exists() and not src.exists():
                    shutil.move(dest, src)
            if batch is not None and batch.id is not None and session.get(Batch, batch.id) is None:
                batch = None  # the batch insert was rolled back with it
            raise
        _failures.pop(group.base.lower(), None)
        if scan.duplicate_group_id:
            report.possible_duplicates.append(scan.id)
        report.created.append(scan.id)
    return report


def _source_key(session: Session, group, settings: Settings) -> tuple[str | None, str]:
    """The key a new scan is stored under, or None when this exact photo was already ingested.

    Scanner software can restart its numbering (FastFoto_0001 again), so a name alone doesn't identify a
    photo. Same name + same content: already ingested. Same name + different content: a new photo, stored
    as `<name>~<hash8>`. A row from before content hashes were stored gets its hash filled in here.
    """
    front = group.front(settings.pairing.front_variant)
    digest = file_sha256(front)
    for candidate in (group.base, f"{group.base}~{digest[:8]}"):
        existing = session.exec(select(Scan).where(func.lower(Scan.source_key) == candidate.lower())).first()
        if existing is None:
            return candidate, digest
        known = existing.source_sha256
        if known is None and Path(existing.front_path).exists():
            known = file_sha256(Path(existing.front_path))
            existing.source_sha256 = known
            session.add(existing)
            session.commit()
        if known == digest:
            return None, digest
    return f"{group.base}~{digest[:16]}", digest


def _analyzed_scan(session: Session, group, key: str, digest: str, settings: Settings) -> Scan:
    """A fully analysed Scan for `group`, still pointing at the inbox files and not yet in the session."""
    front = group.front(settings.pairing.front_variant)
    scan = Scan(
        batch_id=0,
        source_key=key,
        source_sha256=digest,
        front_path=str(front),
        front_enhanced_path=str(group.enhanced) if group.enhanced and group.enhanced != front else None,
        back_path=str(group.back) if group.back else None,
        status=ScanStatus.NEEDS_REVIEW.value,
    )
    corrections.suggest(
        scan, "pairing", {"front": front.name, "back": group.back.name if group.back else None},
        corrections.producers(settings)["pairing"],
    )
    analyze_scan(session, scan, settings, add=False)
    return scan


def _move_aside(group, folder: Path) -> None:
    for src in _group_files(group):
        dest = folder / src.name
        if dest.exists():
            dest = folder / f"{src.stem}-{int(time.time())}{src.suffix}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dest)


def _record_failure(group, exc: Exception, inbox: Path, report: IngestReport, settings: Settings) -> None:
    count, first_seen = _failures.get(group.base.lower(), (0, time.time()))
    count += 1
    log.warning("ingest: %s could not be read (attempt %d): %s", group.base, count, exc)
    gave_up = count >= settings.ingest.unreadable_after or time.time() - first_seen >= settings.ingest.unreadable_minutes * 60
    if gave_up:
        _move_aside(group, inbox / UNREADABLE_DIR)
        _failures.pop(group.base.lower(), None)
        report.unreadable.append(group.base)
        log.warning("ingest: %s moved to %s after %d attempt(s)", group.base, UNREADABLE_DIR, count)
    else:
        _failures[group.base.lower()] = (count, first_seen)
        report.failed.append({"name": group.base, "error": str(exc)})


def archive_orphans(session: Session, settings: Settings) -> list[Path]:
    """Files in the archive that no scan points at: left by an ingest that crashed before this version
    committed per photo. Reported, never moved or deleted."""
    archive = settings.paths.archive
    if not archive.exists():
        return []
    known = set()
    for row in session.exec(select(Scan.front_path, Scan.back_path, Scan.front_enhanced_path)):
        known.update(os.path.normcase(os.path.abspath(p)) for p in row if p)
    return sorted(
        p for p in archive.rglob("*")
        if p.is_file() and not p.name.startswith(".") and os.path.normcase(os.path.abspath(p)) not in known
    )


def analyze_scan(
    session: Session, scan: Scan, settings: Settings, *, detect_crops: bool = True, add: bool = True,
) -> Scan:
    """Crop detection, dHash and blank-back check, all on the cropped + rotated photo. Keeps manual edits.
    `add=False` only reads the database (ingest adds and commits the scan itself afterwards)."""
    front = Path(scan.front_path)
    back = Path(scan.back_path) if scan.back_path else None
    produced = corrections.producers(settings)
    if detect_crops:
        scan.front_crop = imaging.detect_crop(front)
        scan.back_crop = imaging.detect_crop(back) if back else None
        corrections.suggest(scan, "crop_front", scan.front_crop, produced["crop"])
        if back:
            corrections.suggest(scan, "crop_back", scan.back_crop, produced["crop"])

    value = core.dhash(imaging.analysis_gray(front, imaging.Edit(_box(scan.front_crop), 0), 512))
    scan.dhash_hex = f"{value:016x}"
    if back:
        gray = imaging.analysis_gray(back, imaging.Edit(_box(scan.back_crop), 0), 1024)
        metrics = core.blank_metrics(gray)
        scan.back_type = "blank" if metrics.edge_density < settings.analysis.blank_edge_density else "content"
        scan.keep_back = scan.back_type != "blank"
        corrections.suggest(scan, "blank_back", scan.back_type, produced["blank_back"],
                            edge_density=round(metrics.edge_density, 6))
    else:
        scan.back_type, scan.keep_back = None, False

    scan.duplicate_group_id = None
    distance = None
    others = session.exec(select(Scan).where(Scan.id != scan.id, Scan.dhash_hex.is_not(None)).order_by(Scan.id))
    for other in others:
        d = core.hamming(value, int(other.dhash_hex, 16))
        if d <= settings.analysis.dhash_max_distance:
            scan.duplicate_group_id = other.duplicate_group_id or other.id
            distance = d
            break
    corrections.suggest(scan, "duplicate", scan.duplicate_group_id, produced["duplicate"], distance=distance)
    if settings.analysis.read_text and back and scan.back_type != "blank":
        read_back_text(scan, settings, known=known_entities(session, exclude_id=scan.id),
                       learned=dictionary.build(session))
    if add:
        session.add(scan)
    return scan


def _reanchor_duplicate_group(session: Session, followers: list[Scan]) -> None:
    """Re-point a group of scans that shared a now-gone anchor: promote a survivor, or clear it if there's
    only one left (nothing left to call it a possible rescan of)."""
    if len(followers) == 1:
        followers[0].duplicate_group_id = None
    else:
        new_anchor, *rest = sorted(followers, key=lambda s: s.id)
        new_anchor.duplicate_group_id = None  # the anchor itself never carries a duplicate_group_id
        for follower in rest:
            follower.duplicate_group_id = new_anchor.id
    for follower in followers:
        session.add(follower)


def release_from_duplicate_group(session: Session, scan: Scan) -> None:
    """Before deleting `scan`, re-point anything that named it as their duplicate-group anchor.

    `duplicate_group_id` isn't a real foreign key (a scan can be deleted long after it's flagged others), so
    without this a deleted scan's id would stay stuck in every follower's `duplicate_group_id` forever - the
    UI would keep saying "possible rescan of #N" for an id that no longer exists.
    """
    followers = list(session.exec(select(Scan).where(Scan.duplicate_group_id == scan.id)))
    if followers:
        _reanchor_duplicate_group(session, followers)


def repair_dangling_duplicate_groups(session: Session) -> int:
    """Fix any `duplicate_group_id` left pointing at a scan that no longer exists - e.g. one deleted before
    `release_from_duplicate_group` existed. Returns how many scans were changed. Safe to run any time; a no-op
    when there's nothing dangling."""
    existing_ids = set(session.exec(select(Scan.id)))
    targets = {
        gid for gid in session.exec(select(Scan.duplicate_group_id).where(Scan.duplicate_group_id.is_not(None)))
        if gid not in existing_ids
    }
    changed = 0
    for target in targets:
        followers = list(session.exec(select(Scan).where(Scan.duplicate_group_id == target)))
        _reanchor_duplicate_group(session, followers)
        changed += len(followers)
    return changed


def known_entities(session: Session, exclude_id: int | None = None) -> Entities:
    """Every person, place and event already entered on other scans: the family's own vocabulary."""
    known = Entities()
    for other in session.exec(select(Scan).where(Scan.id != (exclude_id or -1))):
        for name in ("people", "places", "events"):
            target = getattr(known, name)
            for value in getattr(other, name) or []:
                if value not in target:
                    target.append(value)
    return known


def extract_for_scan(
    text: str, settings: Settings, known: Entities | None, learned: dictionary.CorrectionDictionary | None
) -> tuple[Entities, str]:
    """Entities from `text` minus values the operator keeps removing. Returns (entities, producer)."""
    found = extract(text, known)
    if learned is not None:
        for field_name, key in (("person", "people"), ("place", "places"), ("event", "events")):
            setattr(found, key, [v for v in getattr(found, key) if not learned.is_suppressed(field_name, v)])
    produced = corrections.producers(settings)
    producer = produced["entities"] if entity_engine_available() else produced["entities_rules_only"]
    if learned is not None and learned.size:
        producer += f"+{learned.version}"
    return found, producer


def record_entity_suggestions(scan: Scan, found: Entities, producer: str) -> None:
    for key in ("people", "places", "events"):
        corrections.suggest(scan, key, getattr(found, key), producer)


def derive_entities(
    scan: Scan, settings: Settings, known: Entities | None = None,
    learned: dictionary.CorrectionDictionary | None = None,
) -> None:
    """Fill People/Places/Events that are still empty from the description."""
    if not settings.analysis.derive_entities or not (scan.description or "").strip():
        return
    found, producer = extract_for_scan(scan.description, settings, known, learned)
    record_entity_suggestions(scan, found, producer)
    for name in ("people", "places", "events"):
        if not getattr(scan, name) and getattr(found, name):
            setattr(scan, name, getattr(found, name))


def apply_orientation_suggestion(scan: Scan, settings: Settings, reader: ocr.Reader) -> None:
    """Auto-rotate front and back to upright, from the back's OCR orientation (0/90/180/270 search).

    Front and back share one physical duplex pass - the back is mirrored left-right relative to the front, not
    independently rotated - so whichever rotation makes the back's text read upright is, in the ordinary case, the
    front's correct rotation too. Runs at most once: skipped once anything (operator or this search) has already
    decided a rotation, so it never overrides a manual choice and re-analyze/read-text keep their "rotations are
    kept" contract for free. No back, blank back, or no legible text at any angle: no signal, nothing is guessed.
    """
    if not settings.analysis.orientation_search or not scan.back_path:
        return
    if scan.back_rotation or (scan.suggestions or {}).get("rotation_back"):
        return
    result = orientation.search(
        reader, Path(scan.back_path), scan.back_crop, settings.analysis.orientation_search_max_side
    )
    if result.score <= 0.0:
        return
    producer = corrections.producers(settings)["rotation_auto"]
    corrections.suggest(scan, "rotation_front", result.angle, producer, confidence=round(result.score, 3))
    corrections.suggest(scan, "rotation_back", result.angle, producer, confidence=round(result.score, 3))
    scan.front_rotation = result.angle
    scan.back_rotation = result.angle


def read_back_text(
    scan: Scan, settings: Settings, known: Entities | None = None,
    learned: dictionary.CorrectionDictionary | None = None,
) -> list[ocr.TextLine] | None:
    """OCR the (cropped, rotated) back, apply the correction dictionary, then fill description/date/entities
    only where they're still empty. Returns None when no OCR engine is available."""
    reader = ocr.get_reader()
    if reader is None or not scan.back_path:
        return None
    apply_orientation_suggestion(scan, settings, reader)  # before building `edit`, so a fresh rotation takes effect
    produced = corrections.producers(settings)
    edit = imaging.Edit(_box(scan.back_crop), scan.back_rotation or 0)
    image = np.asarray(imaging.open_edited(Path(scan.back_path), edit, settings.analysis.ocr_max_side))
    lines = reader.read(image)

    protected = list((known.people if known else []) + (known.places if known else []))
    stored = []
    for line in lines:
        entry = {**ocr.lines_to_json([line])[0], "raw": line.text}
        if learned is not None:
            fixed, changed = learned.correct_line(line.text)
            if changed:
                line.text = fixed
                entry["text"] = fixed
                entry["dictionary"] = learned.version
        spelled, fixes = autocorrect.correct(line.text, protected=protected)
        if fixes:  # "Januaru" -> "January": the operator shouldn't have to retype it on every back
            line.text = spelled
            entry["text"] = spelled
            entry["autocorrect"] = [f.to_dict() for f in fixes]
        stored.append(entry)
    scan.ocr_lines = stored
    scan.ocr_engine = reader.name
    scan.ocr_frame = {"crop": scan.back_crop, "rotation": scan.back_rotation or 0, "max_side": settings.analysis.ocr_max_side}
    corrections.suggest(scan, "ocr_line", [e["text"] for e in stored], produced["ocr_line"])

    if not (scan.description or "").strip():
        scan.description = ocr.description_from(lines) or None
        if scan.description:
            corrections.suggest(scan, "description", scan.description, produced["description"])
    if scan.date_precision == "unknown":
        text = " \n ".join(line.text for line in lines if line.score >= 0.5)
        best = parse_best(text, two_digit_year_pivot=settings.dates.two_digit_year_pivot)
        if best is not None:
            scan.set_photo_date(best.date, "back_ocr")
            corrections.suggest(scan, "date", best.date.label(), produced["date"], raw=best.raw)
    derive_entities(scan, settings, known, learned)
    return lines


def entity_engine_available() -> bool:
    from banana.analysis import entities

    return entities.NLP.get() is not None


def _box(crop: list | None) -> tuple[int, int, int, int] | None:
    return tuple(crop) if crop else None
