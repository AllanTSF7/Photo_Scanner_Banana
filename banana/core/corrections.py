"""Learning loop, Stage 1: correction capture.

Every automatic value is recorded in `scan.suggestions[field] = {value, producer, ...}` when it's produced.
When the operator approves a scan, `record_approval` compares each suggestion with the committed value and appends
one CorrectionEvent per item: kept / edited / removed / added. Events are append-only (DB triggers), only approvals
write them, and every event names its producer so data from a retired model can be filtered out later.
"""

from __future__ import annotations

import functools
import uuid
from importlib import metadata
from pathlib import Path

from sqlmodel import Session, select

from banana import imaging
from banana.analysis import autocorrect
from banana.config import Settings
from banana.models import Batch, CorrectionEvent, Scan, ScanStatus

# Scans whose events may be used as training data. Pending and rejected scans never are.
TRAINING_STATUSES = (ScanStatus.APPROVED.value, ScanStatus.EXPORTED.value, ScanStatus.STACKED.value)

FIELDS = ("ocr_line", "person", "place", "event", "date", "rotation", "crop", "pairing", "duplicate", "blank_back")
OPERATOR = "operator"
NO_MODEL = "none@0"  # no model proposes this value yet (e.g. rotation): the default is still recorded


@functools.lru_cache(maxsize=None)
def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "missing"


def producers(settings: Settings) -> dict[str, str]:
    """name@version of everything that makes suggestions. Changing a rule's behavior means bumping its version."""
    return {
        "ocr_line": f"rapidocr-ppocr@{_version('rapidocr_onnxruntime')}",
        "description": "ocr-description-filter@1",
        "date": "date-rules@3",  # 2: month typo correction. 3: swapped letters too ("Januray")
        "autocorrect": autocorrect.VERSION,
        "entities": f"spacy-en_core_web_sm@{_version('en_core_web_sm')}+rules@1",
        "entities_rules_only": "entity-rules@1",
        "crop": "photo_bbox@1",
        "rotation": NO_MODEL,
        "pairing": f"pairing@{settings.scanner.first_side}-first",
        "duplicate": f"dhash@1(max={settings.analysis.dhash_max_distance})",
        "blank_back": f"edge-density@1(threshold={settings.analysis.blank_edge_density})",
    }


def suggest(scan: Scan, key: str, value, producer: str, **extra) -> None:
    """Record what the app proposed for `key` (reassigning the dict so SQLAlchemy sees the JSON change)."""
    suggestions = dict(scan.suggestions or {})
    suggestions[key] = {"value": value, "producer": producer, **extra}
    scan.suggestions = suggestions


def _side_values(scan: Scan) -> dict:
    return {"front": Path(scan.front_path).name, "back": Path(scan.back_path).name if scan.back_path else None}


def _action(suggested, approved) -> str | None:
    if suggested in (None, [], "") and approved in (None, [], ""):
        return None
    if suggested in (None, [], ""):
        return "added"
    if approved in (None, [], ""):
        return "removed"
    return "kept" if suggested == approved else "edited"


def _line_image(scan: Scan, index: int, line: dict, approval_id: str, settings: Settings) -> str | None:
    """Save the cropped line image next to the event: a corrected line is worthless for training without it."""
    frame = scan.ocr_frame or {}
    if not scan.back_path or not line.get("box"):
        return None
    try:
        crop = tuple(frame["crop"]) if frame.get("crop") else None
        image = imaging.open_edited(Path(scan.back_path), imaging.Edit(crop, frame.get("rotation", 0)), frame.get("max_side"))
        xs = [p[0] for p in line["box"]]
        ys = [p[1] for p in line["box"]]
        pad = 4
        box = (max(0, min(xs) - pad), max(0, min(ys) - pad), min(image.width, max(xs) + pad), min(image.height, max(ys) + pad))
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        out_dir = settings.paths.data_dir / "training" / "ocr_lines"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"scan{scan.id:06d}_{approval_id[:8]}_line{index:02d}.png"
        image.crop(box).save(path)
        return str(path)
    except (OSError, ValueError, KeyError):
        return None


def build_events(scan: Scan, settings: Settings, approval_id: str) -> list[CorrectionEvent]:
    s = scan.suggestions or {}
    p = producers(settings)
    events: list[CorrectionEvent] = []

    def add(field: str, suggested, approved, producer: str, asset_ref: dict | None = None, action: str | None = None):
        action = action or _action(suggested, approved)
        if action is not None:
            events.append(CorrectionEvent(
                scan_id=scan.id, approval_id=approval_id, field=field, action=action,
                suggested=suggested, approved=approved, producer=producer, asset_ref=asset_ref,
            ))

    # OCR lines: kept, edited (operator corrected the text) or removed (marked "not text").
    ocr_producer = (s.get("ocr_line") or {}).get("producer", p["ocr_line"])
    for index, line in enumerate(scan.ocr_lines or []):
        shown = line.get("text")
        if line.get("removed"):
            action, approved = "removed", None
        elif line.get("corrected") not in (None, shown):
            action, approved = "edited", line["corrected"]
        else:
            action, approved = "kept", shown
        asset = {"bbox": line.get("box"), "frame": scan.ocr_frame, "raw_ocr": line.get("raw", shown),
                 "score": line.get("score"), "index": index}
        if action != "removed":
            asset["image"] = _line_image(scan, index, line, approval_id, settings)
        producer = ocr_producer + (f"+{line['dictionary']}" if line.get("dictionary") else "")
        add("ocr_line", shown, approved, producer, asset, action)

    # People / places / events: per value.
    for field, key in (("person", "people"), ("place", "places"), ("event", "events")):
        entry = s.get(key) or {}
        suggested = entry.get("value") or []
        producer = entry.get("producer", p["entities"])
        approved = list(getattr(scan, key) or [])
        lower_approved = {v.lower() for v in approved}
        lower_suggested = {v.lower() for v in suggested}
        for value in suggested:
            add(field, value, value if value.lower() in lower_approved else None, producer,
                action="kept" if value.lower() in lower_approved else "removed")
        for value in approved:
            if value.lower() not in lower_suggested:
                add(field, None, value, OPERATOR, action="added")

    # Date.
    date_entry = s.get("date") or {}
    approved_date = scan.photo_date().label() if scan.date_precision != "unknown" else None
    add("date", date_entry.get("value"), approved_date, date_entry.get("producer", OPERATOR))

    # Rotation per side (no model yet: the suggestion is 0 from "none@0").
    for side in ("front", "back"):
        if side == "back" and not scan.back_path:
            continue
        entry = s.get(f"rotation_{side}") or {"value": 0, "producer": p["rotation"]}
        approved_rotation = getattr(scan, f"{side}_rotation") or 0
        add("rotation", entry["value"], approved_rotation, entry["producer"], {"side": side},
            action="kept" if entry["value"] == approved_rotation else "edited")

    # Crop per side.
    for side in ("front", "back"):
        entry = s.get(f"crop_{side}")
        if entry is None:
            continue
        approved_crop = getattr(scan, f"{side}_crop")
        add("crop", entry["value"], approved_crop, entry["producer"], {"side": side},
            action="kept" if entry["value"] == approved_crop else "edited")

    # Front/back pairing.
    entry = s.get("pairing")
    if entry is not None:
        current = _side_values(scan)
        add("pairing", entry["value"], current, entry["producer"], action="kept" if entry["value"] == current else "edited")

    # Duplicate: the app's flag vs the operator's Flag duplicate decision.
    #   flagged + operator agrees -> kept;  flagged + operator doesn't -> removed (false alarm);
    #   not flagged + operator flags -> added (missed duplicate).
    entry = s.get("duplicate") or {}
    app_flag = bool(entry.get("value"))
    operator_flag = bool(scan.operator_duplicate)
    if app_flag or operator_flag:
        action = "kept" if app_flag and operator_flag else "removed" if app_flag else "added"
        add("duplicate", entry.get("value") if app_flag else None, {"duplicate": operator_flag},
            entry.get("producer", OPERATOR) if app_flag else OPERATOR, {"distance": entry.get("distance")}, action=action)

    # Blank-back decision.
    entry = s.get("blank_back")
    if entry is not None:
        approved_keep = bool(scan.keep_back)
        suggested_keep = entry["value"] != "blank"
        add("blank_back", entry["value"], "content" if approved_keep else "blank", entry["producer"],
            {"edge_density": entry.get("edge_density")}, action="kept" if suggested_keep == approved_keep else "edited")
    return events


def _signature(events: list[CorrectionEvent]) -> list[tuple]:
    return sorted((e.field, e.action, repr(e.suggested), repr(e.approved), e.producer) for e in events)


def record_approval(session: Session, scan: Scan, settings: Settings) -> list[CorrectionEvent]:
    """Append this approval's events. Re-approving an unchanged scan writes nothing; any change writes a new set."""
    approval_id = uuid.uuid4().hex
    events = build_events(scan, settings, approval_id)
    last = session.exec(
        select(CorrectionEvent).where(CorrectionEvent.scan_id == scan.id).order_by(CorrectionEvent.id.desc())
    ).first()
    if last is not None:
        previous = list(session.exec(select(CorrectionEvent).where(CorrectionEvent.approval_id == last.approval_id)))
        if _signature(previous) == _signature(events):
            _discard_line_images(events)
            return []
    batch = session.get(Batch, scan.batch_id)
    for event in events:
        event.batch = batch.name if batch else None
        session.add(event)
    return events


def _discard_line_images(events: list[CorrectionEvent]) -> None:
    for event in events:
        image = (event.asset_ref or {}).get("image")
        if image:
            Path(image).unlink(missing_ok=True)


def training_events(session: Session, field: str | None = None) -> list[CorrectionEvent]:
    """Events usable for training: latest approval per scan, and only for scans that are currently approved/exported."""
    rows = session.exec(
        select(CorrectionEvent, Scan.status).join(Scan, Scan.id == CorrectionEvent.scan_id).order_by(CorrectionEvent.id)
    ).all()
    latest: dict[int, str] = {}
    for event, _status in rows:
        latest[event.scan_id] = event.approval_id
    return [
        event for event, status in rows
        if status in TRAINING_STATUSES and latest[event.scan_id] == event.approval_id and (field is None or event.field == field)
    ]
