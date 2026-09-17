"""Inbox ingest: pair scanner files, move them to the archive, run the cheap per-image checks."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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


@dataclass
class IngestReport:
    batch: str | None = None
    created: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    possible_duplicates: list[int] = field(default_factory=list)


def _move(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.move(src, dest)
    return dest


def ingest_inbox(session: Session, settings: Settings, batch_name: str | None = None) -> IngestReport:
    inbox = settings.paths.inbox
    files = [p for p in inbox.iterdir() if p.is_file()] if inbox.exists() else []
    paired = pair_files(files, settings.pairing.pattern)
    report = IngestReport(unmatched=[p.name for p in paired.unmatched])
    report.skipped += [g.base for g in paired.backs_without_front]
    if not paired.groups:
        return report

    name = batch_name or f"inbox-{datetime.now():%Y%m%d-%H%M%S}"
    batch = Batch(name=name)
    session.add(batch)
    session.flush()
    report.batch = name
    archive = settings.paths.archive / name

    for group in paired.groups:
        if session.exec(select(Scan).where(Scan.source_key == group.base)).first():
            report.skipped.append(group.base)
            continue
        original = _move(group.original, archive) if group.original else None
        enhanced = _move(group.enhanced, archive) if group.enhanced else None
        back = _move(group.back, archive) if group.back else None
        front = (enhanced or original) if settings.pairing.front_variant == "enhanced" else (original or enhanced)

        scan = Scan(
            batch_id=batch.id,
            source_key=group.base,
            front_path=str(front),
            front_enhanced_path=str(enhanced) if enhanced and enhanced != front else None,
            back_path=str(back) if back else None,
            status=ScanStatus.NEEDS_REVIEW.value,
        )
        session.add(scan)
        session.flush()
        corrections.suggest(
            scan, "pairing", {"front": front.name, "back": back.name if back else None},
            corrections.producers(settings)["pairing"],
        )
        analyze_scan(session, scan, settings)
        if scan.duplicate_group_id:
            report.possible_duplicates.append(scan.id)
        report.created.append(scan.id)

    session.commit()
    return report


def analyze_scan(session: Session, scan: Scan, settings: Settings, *, detect_crops: bool = True) -> Scan:
    """Crop detection, dHash and blank-back check, all on the cropped + rotated photo. Keeps manual edits."""
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
    session.add(scan)
    return scan


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
