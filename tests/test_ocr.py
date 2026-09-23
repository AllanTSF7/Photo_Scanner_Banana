"""Text reading on backs: line filtering, prefill rules, and a real OCR smoke test when the engine is installed."""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from banana.analysis import ocr
from banana.config import Settings
from banana.models import Scan

LABEL = [("E66817567/67", 0.86), ("ORIGINAL", 0.99), ("KINKAID 867_0867", 0.68),
         ("JimmyDawley", 0.99), ("Chocolat Design - Field Trip", 0.97), ("Januaru 12, 2006", 0.99)]


class FakeReader:
    name = "fake"

    def __init__(self, lines):
        self.lines = [ocr.TextLine(text, score, [[0, 0], [1, 0], [1, 1], [0, 1]]) for text, score in lines]
        self.calls = 0

    def read(self, image, use_cls: bool = True):
        self.calls += 1
        return self.lines


def test_description_skips_codes_and_low_confidence():
    lines = FakeReader(LABEL).lines
    assert ocr.description_from(lines) == "ORIGINAL\nJimmy Dawley\nChocolat Design - Field Trip\nJanuaru 12, 2006"


def _scan(tmp_path, **kwargs) -> Scan:
    back = tmp_path / "back.jpg"
    Image.new("RGB", (600, 400), (250, 246, 236)).save(back)
    return Scan(batch_id=1, source_key="k", front_path=str(back), back_path=str(back), **kwargs)


def test_read_back_text_prefills_empty_fields(tmp_path, monkeypatch):
    from banana.ingest import service

    reader = FakeReader(LABEL)
    monkeypatch.setattr(ocr, "get_reader", lambda: reader)
    scan = _scan(tmp_path)
    service.read_back_text(scan, Settings())
    assert scan.ocr_engine == "fake" and len(scan.ocr_lines) == 6
    assert scan.description.startswith("ORIGINAL\nJimmy Dawley")
    assert scan.photo_date().label() == "2006-01-12" and scan.date_source == "back_ocr"
    assert scan.description.split("\n")[1] == "Jimmy Dawley"  # OCR-joined words split
    assert scan.events == ["Field Trip"]  # derived from the description


def test_read_back_text_never_overwrites_user_input(tmp_path, monkeypatch):
    from banana.dates import PhotoDate, Precision
    from banana.ingest import service

    monkeypatch.setattr(ocr, "get_reader", lambda: FakeReader(LABEL))
    scan = _scan(tmp_path, description="typed by operator")
    scan.set_photo_date(PhotoDate(Precision.YEAR, 2005), "manual")
    service.read_back_text(scan, Settings())
    assert scan.description == "typed by operator"
    assert scan.photo_date().label() == "2005"
    assert len(scan.ocr_lines) == 6  # lines are still stored for the panel


def test_no_reader_returns_none(tmp_path, monkeypatch):
    from banana.ingest import service

    monkeypatch.setattr(ocr, "get_reader", lambda: None)
    scan = _scan(tmp_path)
    assert service.read_back_text(scan, Settings()) is None
    assert scan.ocr_lines == [] and scan.description is None


@pytest.mark.skipif(ocr.get_reader() is None, reason="rapidocr_onnxruntime not installed")
def test_real_ocr_reads_printed_label(tmp_path):
    image = Image.new("RGB", (1400, 700), (250, 246, 236))
    draw = ImageDraw.Draw(image)
    for i, text in enumerate(["Grandma and John", "Lake Erie", "July 4, 1976"]):
        draw.text((120, 120 + i * 150), text, fill=(20, 20, 20), font_size=90)
    lines = ocr.get_reader().read(np.asarray(image.rotate(180)))  # labels are often upside down
    joined = " ".join(line.text for line in lines).lower().replace(" ", "")
    assert "grandma" in joined and "1976" in joined
