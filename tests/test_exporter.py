"""Exporter round-trip through a real ExifTool. Skipped when ExifTool is not installed."""

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from banana.dates import PhotoDate, Precision
from banana.export.exiftool_writer import ExifToolWriter
from banana.export.exporter import Exporter, ExportRequest
from banana.export.metadata import PhotoMetadata

_VENDORED = Path(__file__).resolve().parents[1] / "vendor" / "exiftool_win64" / "exiftool.exe"  # what the Windows app ships
EXIFTOOL = shutil.which("exiftool") or (str(_VENDORED) if _VENDORED.exists() else None)
pytestmark = pytest.mark.skipif(EXIFTOOL is None, reason="exiftool not installed")


def _jpeg(path: Path, seed: int) -> Path:
    arr = np.random.default_rng(seed).integers(0, 256, size=(60, 90, 3), dtype=np.uint8)
    Image.fromarray(arr).save(path, quality=90)
    return path


@pytest.fixture
def writer():
    with ExifToolWriter(EXIFTOOL) as w:
        yield w


def test_export_front_and_back(tmp_path, writer):
    front = _jpeg(tmp_path / "Box3_0001.jpg", 1)
    back = _jpeg(tmp_path / "Box3_0001_b.jpg", 2)
    originals = {p: p.read_bytes() for p in (front, back)}
    root = tmp_path / "sorted"
    meta = PhotoMetadata(
        date=PhotoDate(Precision.DAY, 1984, 12, 25),
        description="Christmas morning at Grandma's\nJohn got the bike",
        people=["Grandma", "John"], events=["Christmas"], places=["Grandma's House"], box_label="Attic Box 3",
    )

    result = Exporter(root, writer).export(ExportRequest(42, "Attic Box 3", front, back, meta))

    assert str(result.front_rel) == "1984/1984-12-25/SCAN_000042_A.jpg"
    assert str(result.back_rel) == "1984/1984-12-25/SCAN_000042_B.jpg"
    for rel in (result.front_rel, result.back_rel):
        assert (root / rel).exists() and (root / f"{rel}.xmp").exists()
    assert not any((root / ".staging").iterdir())
    assert {p: p.read_bytes() for p in originals} == originals  # originals untouched

    sidecar = writer.read(root / f"{result.front_rel}.xmp")
    assert sidecar["XMP-exif:DateTimeOriginal"] == "1984:12:25 12:00:00"
    assert sidecar["XMP-dc:Description"] == "Christmas morning at Grandma's\nJohn got the bike"
    assert "People/John" in sidecar["XMP-digiKam:TagsList"]
    assert "Scan/HasBack" in sidecar["XMP-digiKam:TagsList"]


def test_reexport_with_new_date_moves_files(tmp_path, writer):
    front = _jpeg(tmp_path / "x_0001.jpg", 3)
    root = tmp_path / "sorted"
    exporter = Exporter(root, writer)
    first = exporter.export(ExportRequest(7, "b", front, None, PhotoMetadata(date=PhotoDate.unknown())))
    second = exporter.export(
        ExportRequest(7, "b", front, None, PhotoMetadata(date=PhotoDate(Precision.MONTH, 1979, 6), tags=["Family/Smiths"])),
        previous=first,
    )
    assert not (root / first.front_rel).exists()
    assert not (root / f"{first.front_rel}.xmp").exists()
    assert str(second.front_rel) == "1979/1979-06/SCAN_000007_A.jpg"
    tags = writer.read(root / second.front_rel)["XMP-lr:HierarchicalSubject"]
    assert json.dumps(tags) == json.dumps(["Family|Smiths", "Scan|DateApprox"])


def test_text_outside_ascii_round_trips(tmp_path, writer):
    """B-43: on Windows, text went to ExifTool as cp1252: "·" and "—" came back as "?" and "∈" or CJK crashed the
    write, so 10 approved scans could never be exported. These are characters from those scans' OCR text."""
    front = _jpeg(tmp_path / "Box3_0002.jpg", 3)
    root = tmp_path / "sorted"
    text = "CO., NAPA 1995 — Sept. 2·S § ∈ 人力 Café"
    meta = PhotoMetadata(date=PhotoDate(Precision.DAY, 1995, 9, 2), description=text, people=["Zoë Müller"],
                         places=["Napa — Calif."])

    result = Exporter(root, writer).export(ExportRequest(43, None, front, None, meta))  # raises if readback differs

    sidecar = writer.read(root / f"{result.front_rel}.xmp")
    assert sidecar["XMP-dc:Description"] == text
    assert "People/Zoë Müller" in sidecar["XMP-digiKam:TagsList"]
    assert writer.read(root / result.front_rel)["IFD0:ImageDescription"] == text
