"""Orientation search for photo backs: try each rotation and pick whichever is actually upright.

Offline and deterministic - no model beyond the OCR engine already in use for printed text. The photo's crop is
orientation-agnostic and already applied before this runs (see `imaging.detect_crop`); this only decides which
way is "up".

Front and back of one photo are captured in the same duplex pass - the back is mirrored left-right relative to
the front, not independently rotated - so whichever rotation makes the back's text read upright is, in the
ordinary case (a label written the same way up as the photo displays), also the front's correct rotation. The
caller (`banana.ingest.service.apply_orientation_suggestion`) applies one detected angle to both sides.

No back, a blank back, or no legible text at any angle: there's no signal, and none is guessed - `search`
returns a zero-confidence result rather than defaulting to a rotation.

**Why this needs two phases, not just "OCR confidence at each angle":**
RapidOCR (PP-OCR) is already close to rotation-invariant for *reading* text - its detector finds a line's box
whichever way it's turned, a hard-coded step re-rotates any detected crop that comes out taller than wide before
recognition, and its angle classifier (`use_cls`) auto-corrects text upside down within its own line. Scoring by
recognition confidence at each of the 4 rotations was tried first and measured to give near-identical scores at
every angle - it genuinely cannot tell "sideways" or "upside down" apart from "upright" by confidence alone
(verified empirically, not assumed). Two independent signals that *do* discriminate:

1. **Line shape** (not confidence): a detected box's own bounding rectangle, in the *unrotated-by-RapidOCR*
   image coordinates, is wide (width > height) when the candidate rotation has text running normally along its
   line, and tall/narrow when the candidate is off by 90 degrees - real handwriting/print lines are wide, not
   tall. This picks the right pair of candidates - {0, 180} or {90, 270} - straight from geometry.
2. **Recognition with the angle classifier turned off** (`reader.read(image, use_cls=False)`): without that
   auto-correction, recognition reads confidently when text is right-side up and reads much worse - garbled,
   lower-confidence, sometimes nothing at all - upside down. On real handwriting the wrong direction doesn't
   reliably score near zero (unlike clean synthetic text), so this compares the winner against the loser by a
   margin, the same way phase 1 does, rather than requiring the loser to fall below a fixed cutoff.

Both phases require a clear margin before trusting a winner; either one coming back ambiguous means "no signal."
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from banana import imaging
from banana.analysis.ocr import Reader, TextLine

# Phase 1 (line shape): the wider pair's average width/height ratio must clear the narrower pair's by this
# factor before "which axis" is trusted.
AXIS_MIN_MARGIN = 2.0
# Phase 2 (classifier off): the winner's score must clear the loser's by this factor. A ratio, not an absolute
# cutoff on the loser - real (messy, handwritten) text read upside down doesn't reliably score near zero, it
# just scores much worse than the same text read right-side up (measured: a garbled low-but-nonzero score is
# common for the wrong direction, so rejecting on "the loser also scored something" throws out real signal).
DIRECTION_MIN_MARGIN = 2.0
SEARCH_MAX_SIDE = 300  # small and cheap: run per rotation (up to 6 passes), the real OCR pass runs once at full size


@dataclass(frozen=True)
class Orientation:
    angle: int  # one of imaging.ROTATIONS
    score: float  # 0.0 means "no signal": nothing legible, or either phase was too close to call


def _wideness(lines: list[TextLine]) -> float:
    """Average width/height of detected line boxes - high for normally-laid-out text, low when it's sideways."""
    total_w = sum(max(p[0] for p in line.box) - min(p[0] for p in line.box) for line in lines)
    total_h = sum(max(p[1] for p in line.box) - min(p[1] for p in line.box) for line in lines)
    return total_w / max(1, total_h) if lines else 0.0


def _candidate(reader: Reader, back_path: Path, back_crop, angle: int, small_side: int, *, use_cls: bool):
    edit = imaging.Edit(tuple(back_crop) if back_crop else None, angle)
    image = np.asarray(imaging.open_edited(back_path, edit, small_side))
    return reader.read(image, use_cls=use_cls)


def search(
    reader: Reader, back_path: Path, back_crop: list | tuple | None, small_side: int = SEARCH_MAX_SIDE
) -> Orientation:
    """Try every rotation in `imaging.ROTATIONS` on a small copy of the cropped back; return the best."""
    wideness = {angle: _wideness(_candidate(reader, back_path, back_crop, angle, small_side, use_cls=True))
                for angle in imaging.ROTATIONS}
    by_wideness = sorted(imaging.ROTATIONS, key=wideness.get, reverse=True)
    axis, rejected = by_wideness[:2], by_wideness[2:]
    if wideness[axis[0]] <= 0.0 or wideness[axis[0]] < wideness[rejected[0]] * AXIS_MIN_MARGIN:
        return Orientation(0, 0.0)  # can't tell which pair of angles is even the right axis

    direction = {
        angle: sum(line.score for line in _candidate(reader, back_path, back_crop, angle, small_side, use_cls=False))
        for angle in axis
    }
    winner, loser = sorted(axis, key=direction.get, reverse=True)
    if direction[winner] <= 0.0 or direction[winner] < direction[loser] * DIRECTION_MIN_MARGIN:
        return Orientation(0, 0.0)  # too close to call (or nothing legible either way)
    return Orientation(winner, direction[winner])
