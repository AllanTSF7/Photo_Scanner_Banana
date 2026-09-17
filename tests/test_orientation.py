"""Orientation search: recovers the rotation that makes a photo back's text read upright.

Two independent, sequential signals (see banana/analysis/ocr/orientation.py for why a single OCR-confidence
score can't discriminate rotation on its own): line-box shape picks the axis (0/180 vs 90/270), then
recognition with the angle classifier disabled picks the direction within that axis. Both phases must clear a
margin or the result is "no signal" - never a guess.
"""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from banana import imaging
from banana.analysis import ocr
from banana.analysis.ocr import TextLine, orientation

# The correction needed to undo a raw scan produced by rotating an upright image forward by `angle`.
_INVERSE = {0: 0, 90: 270, 180: 180, 270: 90}

WIDE_BOX = [[0, 0], [100, 0], [100, 20], [0, 20]]  # w=100 h=20: a normal text line
NARROW_BOX = [[0, 0], [20, 0], [20, 100], [0, 100]]  # w=20 h=100: sideways


class ScriptedReader:
    """Returns pre-scripted responses in call order, ignoring the actual image - for exercising the two-phase
    logic directly without needing OCR to really distinguish synthetic fixtures."""

    name = "fake"

    def __init__(self, responses: list[list[TextLine]]):
        self._responses = iter(responses)

    def read(self, image, use_cls: bool = True):
        return next(self._responses)


def _lines(score: float, box: list) -> list[TextLine]:
    return [TextLine("x", score, box)] if score else []


def _upright(path: Path) -> None:
    image = Image.new("RGB", (1400, 700), (250, 246, 236))
    draw = ImageDraw.Draw(image)
    for i, text in enumerate(["Grandma and John", "Lake Erie", "July 4, 1976"]):
        draw.text((120, 120 + i * 150), text, fill=(20, 20, 20), font_size=90)
    image.save(path)


@pytest.mark.skipif(ocr.get_reader() is None, reason="rapidocr_onnxruntime not installed")
@pytest.mark.parametrize("angle", imaging.ROTATIONS)
def test_search_recovers_the_upright_rotation(tmp_path, angle):
    upright = tmp_path / "upright.jpg"
    _upright(upright)
    raw = tmp_path / f"raw_{angle}.jpg"
    imaging.open_edited(upright, imaging.Edit(None, angle)).save(raw)

    result = orientation.search(ocr.get_reader(), raw, None)
    assert result.angle == _INVERSE[angle]
    assert result.score > 0.0


def test_no_signal_on_a_blank_back(tmp_path):
    blank = tmp_path / "blank.jpg"
    Image.new("RGB", (1400, 700), (250, 246, 236)).save(blank)
    reader = ScriptedReader([[], [], [], []])  # nothing detected at any of the 4 phase-1 angles
    assert orientation.search(reader, blank, None) == orientation.Orientation(0, 0.0)


def test_ambiguous_axis_is_too_close_to_call(tmp_path):
    same = tmp_path / "same.jpg"
    Image.new("RGB", (100, 100), (255, 255, 255)).save(same)
    # Every angle looks equally "wide": phase 1 can't tell 0/180 from 90/270 apart.
    reader = ScriptedReader([_lines(0.9, WIDE_BOX)] * 4)
    assert orientation.search(reader, same, None) == orientation.Orientation(0, 0.0)


def test_both_directions_read_is_too_close_to_call(tmp_path):
    same = tmp_path / "same.jpg"
    Image.new("RGB", (100, 100), (255, 255, 255)).save(same)
    # Phase 1: 0 and 180 are clearly the wide axis, 90/270 clearly narrow.
    phase1 = [_lines(0.9, WIDE_BOX), _lines(0.9, NARROW_BOX), _lines(0.9, WIDE_BOX), _lines(0.9, NARROW_BOX)]
    # Phase 2 (angle classifier off, tried on 0 then 180): both "read" confidently - can't tell which is upright.
    phase2 = [_lines(0.9, WIDE_BOX), _lines(0.9, WIDE_BOX)]
    reader = ScriptedReader(phase1 + phase2)
    assert orientation.search(reader, same, None) == orientation.Orientation(0, 0.0)


def test_neither_direction_reads_is_too_close_to_call(tmp_path):
    same = tmp_path / "same.jpg"
    Image.new("RGB", (100, 100), (255, 255, 255)).save(same)
    phase1 = [_lines(0.9, WIDE_BOX), _lines(0.9, NARROW_BOX), _lines(0.9, WIDE_BOX), _lines(0.9, NARROW_BOX)]
    phase2 = [_lines(0, WIDE_BOX), _lines(0, WIDE_BOX)]  # angle classifier off: neither recognizes anything
    reader = ScriptedReader(phase1 + phase2)
    assert orientation.search(reader, same, None) == orientation.Orientation(0, 0.0)


@pytest.mark.skipif(ocr.get_reader() is None, reason="rapidocr_onnxruntime not installed")
def test_read_back_text_auto_rotates_front_and_back(tmp_path):
    """End to end: ingesting a sideways back rotates both sides upright and records why."""
    from banana.config import Settings
    from banana.ingest import service
    from banana.models import Scan

    upright = tmp_path / "upright.jpg"
    _upright(upright)
    back = tmp_path / "back.jpg"
    imaging.open_edited(upright, imaging.Edit(None, 90)).save(back)  # fed in sideways

    scan = Scan(batch_id=1, source_key="k", front_path=str(back), back_path=str(back))
    service.read_back_text(scan, Settings())

    assert scan.front_rotation == scan.back_rotation == _INVERSE[90] == 270
    assert scan.suggestions["rotation_front"]["value"] == 270
    assert scan.suggestions["rotation_back"]["value"] == 270
    producer = scan.suggestions["rotation_back"]["producer"]
    assert producer.startswith("back-ocr-orientation@")

    # A rotation already decided (operator or a previous run) is never re-guessed.
    again = Scan(batch_id=1, source_key="k2", front_path=str(back), back_path=str(back), back_rotation=90)
    service.read_back_text(again, Settings())
    assert again.back_rotation == 90 and "rotation_back" not in (again.suggestions or {})


def test_clear_winner_in_phase_two(tmp_path):
    same = tmp_path / "same.jpg"
    Image.new("RGB", (100, 100), (255, 255, 255)).save(same)
    phase1 = [_lines(0.9, WIDE_BOX), _lines(0.9, NARROW_BOX), _lines(0.9, WIDE_BOX), _lines(0.9, NARROW_BOX)]
    phase2 = [_lines(0.95, WIDE_BOX), _lines(0, WIDE_BOX)]  # 0 reads with the classifier off, 180 doesn't
    reader = ScriptedReader(phase1 + phase2)
    result = orientation.search(reader, same, None)
    assert result.angle == 0 and result.score == pytest.approx(0.95)


def test_a_garbled_but_nonzero_loser_score_does_not_block_a_clear_winner(tmp_path):
    # Regression (found on a real scan): upside-down handwriting doesn't reliably score near zero with the
    # classifier off - it can still produce a moderate, garbled score. A margin over the loser is required,
    # not an absolute cutoff that would reject this as "both read, can't tell".
    same = tmp_path / "same.jpg"
    Image.new("RGB", (100, 100), (255, 255, 255)).save(same)
    phase1 = [_lines(0.9, WIDE_BOX), _lines(0.9, NARROW_BOX), _lines(0.9, WIDE_BOX), _lines(0.9, NARROW_BOX)]
    phase2 = [_lines(0.674, WIDE_BOX), _lines(3.838, WIDE_BOX)]  # measured values from the real failing scan
    reader = ScriptedReader(phase1 + phase2)
    result = orientation.search(reader, same, None)
    assert result.angle == 180 and result.score == pytest.approx(3.838)


def test_direction_scores_too_close_together_is_still_too_close_to_call(tmp_path):
    same = tmp_path / "same.jpg"
    Image.new("RGB", (100, 100), (255, 255, 255)).save(same)
    phase1 = [_lines(0.9, WIDE_BOX), _lines(0.9, NARROW_BOX), _lines(0.9, WIDE_BOX), _lines(0.9, NARROW_BOX)]
    phase2 = [_lines(1.0, WIDE_BOX), _lines(0.9, WIDE_BOX)]  # close enough that it's not a real margin
    reader = ScriptedReader(phase1 + phase2)
    assert orientation.search(reader, same, None) == orientation.Orientation(0, 0.0)
