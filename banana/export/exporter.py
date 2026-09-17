"""Copy a scan into the Immich library with embedded metadata and an XMP sidecar.

Everything is prepared and verified in <sorted>/.staging on the same filesystem, then moved into
place with os.replace (sidecar first, so Immich never sees the image without it).
Originals are only ever read.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from banana.export import layout
from banana.imaging import Edit, save_edited
from banana.export.exiftool_writer import ExifToolWriter
from banana.export.metadata import PhotoMetadata


@dataclass
class ExportRequest:
    scan_id: int
    batch_name: str
    front: Path
    back: Path | None
    meta: PhotoMetadata
    front_edit: Edit = field(default_factory=Edit)
    back_edit: Edit = field(default_factory=Edit)


@dataclass
class ExportResult:
    front_rel: PurePosixPath
    front_sha256: str
    back_rel: PurePosixPath | None = None
    back_sha256: str | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Exporter:
    def __init__(self, sorted_root: Path, writer: ExifToolWriter) -> None:
        self.root = sorted_root
        self.writer = writer

    def export(self, req: ExportRequest, previous: ExportResult | None = None) -> ExportResult:
        req.meta.has_back = req.back is not None
        staging = self.root / layout.STAGING_DIR / f"scan_{req.scan_id:06d}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            sides: list[tuple[layout.Side, Path, Edit]] = [("A", req.front, req.front_edit)]
            if req.back is not None:
                sides.append(("B", req.back, req.back_edit))
            prepared = [self._prepare(req, side, src, edit, staging) for side, src, edit in sides]
            if previous is not None:
                self._remove_previous(previous, {rel for rel, _, _ in prepared})
            for rel, staged, _ in prepared:
                self._move_into_place(rel, staged)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        front_rel, _, front_hash = prepared[0]
        result = ExportResult(front_rel, front_hash)
        if len(prepared) > 1:
            result.back_rel, _, result.back_sha256 = prepared[1]
        return result

    def _prepare(
        self, req: ExportRequest, side: layout.Side, src: Path, edit: Edit, staging: Path
    ) -> tuple[PurePosixPath, Path, str]:
        suffix = src.suffix if edit.is_identity else ".jpg"  # edited images are re-encoded as JPEG (q95)
        rel = layout.relative_path(req.scan_id, side, suffix, req.meta.date, req.batch_name)
        staged = staging / rel.name
        if edit.is_identity:
            shutil.copyfile(src, staged)
        else:
            save_edited(src, staged, edit)
        sidecar = staging / layout.sidecar_name(rel.name)
        is_back = side == "B"
        self.writer.write_image(staged, req.meta, is_back=is_back)
        self.writer.write_sidecar(staged, sidecar)
        self.writer.verify(staged, req.meta, is_back=is_back, xmp_only=False)
        self.writer.verify(sidecar, req.meta, is_back=is_back, xmp_only=True)
        return rel, staged, sha256_file(staged)

    def _move_into_place(self, rel: PurePosixPath, staged: Path) -> None:
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged.with_name(layout.sidecar_name(staged.name)), dest.with_name(layout.sidecar_name(dest.name)))
        os.replace(staged, dest)

    def _remove_previous(self, previous: ExportResult, keep: set[PurePosixPath]) -> None:
        for rel in (previous.front_rel, previous.back_rel):
            if rel is None or rel in keep:
                continue
            path = self.root / rel
            path.unlink(missing_ok=True)
            path.with_name(layout.sidecar_name(path.name)).unlink(missing_ok=True)
