"""Glue between DB rows and the exporter, plus the `--manual-json` import used before the ML stages exist."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from pydantic import BaseModel
from sqlmodel import Session, select

from banana.analysis.date_parse import parse_best
from banana.dates import PhotoDate, Precision
from banana.export.exporter import Exporter, ExportRequest, ExportResult
from banana.export.metadata import PhotoMetadata
from banana.imaging import Edit
from banana.models import Batch, Export, Scan, ScanStatus, utcnow


class ManualDate(BaseModel):
    precision: Precision
    year: int | None = None
    month: int | None = None
    day: int | None = None
    season: str | None = None
    circa: bool = False


class ManualItem(BaseModel):
    front: Path
    back: Path | None = None
    date: str | ManualDate | None = None
    description: str = ""
    people: list[str] = []
    places: list[str] = []
    events: list[str] = []
    tags: list[str] = []


class ManualBatch(BaseModel):
    batch: str
    box_label: str | None = None
    items: list[ManualItem]


def resolve_date(value: str | ManualDate | None, pivot: int | None) -> PhotoDate:
    if value is None:
        return PhotoDate.unknown()
    if isinstance(value, ManualDate):
        return PhotoDate(**value.model_dump())
    best = parse_best(value, two_digit_year_pivot=pivot)
    if best is None:
        raise ValueError(f"could not parse date {value!r}")
    return best.date


def import_manual(session: Session, spec: ManualBatch, base_dir: Path, pivot: int | None) -> list[Scan]:
    batch = session.exec(select(Batch).where(Batch.name == spec.batch)).first()
    if batch is None:
        batch = Batch(name=spec.batch, box_label=spec.box_label)
        session.add(batch)
        session.flush()

    scans = []
    for item in spec.items:
        front = (base_dir / item.front).resolve()
        back = (base_dir / item.back).resolve() if item.back else None
        key = str(front)
        scan = session.exec(select(Scan).where(Scan.source_key == key)).first() or Scan(
            batch_id=batch.id, source_key=key, front_path=key
        )
        scan.back_path = str(back) if back else None
        scan.keep_back = back is not None
        scan.set_photo_date(resolve_date(item.date, pivot), "manual")
        scan.description = item.description
        scan.people, scan.places, scan.events, scan.tags = item.people, item.places, item.events, item.tags
        scan.status = ScanStatus.APPROVED.value
        scan.updated_at = utcnow()
        session.add(scan)
        scans.append(scan)
    session.commit()
    return scans


def scan_edit(scan: Scan, side: str) -> Edit:
    crop = scan.front_crop if side == "front" else scan.back_crop
    rotation = scan.front_rotation if side == "front" else scan.back_rotation
    return Edit(tuple(crop) if crop else None, rotation or 0)


def export_scan(session: Session, scan: Scan, exporter: Exporter) -> Export:
    batch = session.get(Batch, scan.batch_id)
    meta = PhotoMetadata(
        date=scan.photo_date(),
        description=scan.description or "",
        people=list(scan.people), places=list(scan.places), events=list(scan.events), tags=list(scan.tags),
        box_label=batch.box_label,
    )
    back = Path(scan.back_path) if scan.back_path and scan.keep_back else None
    record = session.exec(select(Export).where(Export.scan_id == scan.id)).first()
    previous = None
    if record is not None:
        previous = ExportResult(
            PurePosixPath(record.front_rel), record.front_sha256,
            PurePosixPath(record.back_rel) if record.back_rel else None, record.back_sha256,
        )

    request = ExportRequest(
        scan.id, batch.name, Path(scan.front_path), back, meta,
        front_edit=scan_edit(scan, "front"), back_edit=scan_edit(scan, "back"),
    )
    result = exporter.export(request, previous)

    record = record or Export(scan_id=scan.id, front_rel="", front_sha256="")
    record.front_rel, record.front_sha256 = str(result.front_rel), result.front_sha256
    record.back_rel = str(result.back_rel) if result.back_rel else None
    record.back_sha256 = result.back_sha256
    record.exported_at = utcnow()
    scan.status = ScanStatus.EXPORTED.value
    scan.updated_at = utcnow()
    session.add_all([record, scan])
    session.commit()
    return record
