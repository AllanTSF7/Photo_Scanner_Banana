"""Photo detection on raw scans, and rendering with non-destructive crop + rotation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from banana import core

ROTATIONS = (0, 90, 180, 270)
_TRANSPOSE = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
DETECT_SCALE = 8  # detect on ~1/8 scale (JPEG DCT scaling makes this cheap)
FULL_PAGE_AREA_PERCENT = 97  # a box covering at least this much of the page means "no crop needed"


@dataclass(frozen=True)
class Edit:
    crop: tuple[int, int, int, int] | None = None
    rotation: int = 0

    @property
    def is_identity(self) -> bool:
        return self.crop is None and self.rotation == 0


def detect_crop(path: Path) -> list[int] | None:
    """Full-resolution crop box of the photo on a raw scan, or None when the photo fills the page."""
    with Image.open(path) as im:
        full_w, full_h = im.size
        im.draft("RGB", (max(1, full_w // DETECT_SCALE), max(1, full_h // DETECT_SCALE)))
        small = im.convert("RGB")  # raw pixel space: crop boxes are in stored-pixel coordinates
    box = core.photo_bbox(np.asarray(small))
    if box is None:
        return None
    x0, y0, x1, y1 = box
    if (x1 - x0) * (y1 - y0) * 100 >= FULL_PAGE_AREA_PERCENT * small.width * small.height:
        return None
    sx, sy = full_w / small.width, full_h / small.height
    # Inset one detection pixel on each side so no backing fringe survives the upscale.
    left, top = (x0 + 1) * sx, (y0 + 1) * sy
    right, bottom = (x1 - 1) * sx, (y1 - 1) * sy
    return [int(left), int(top), int(right), int(bottom)]


def open_edited(path: Path, edit: Edit, max_side: int | None = None) -> Image.Image:
    """Open `path` with crop then rotation applied; optionally downscaled so the long side <= max_side."""
    im = Image.open(path)
    full_w, full_h = im.size
    if max_side:
        crop_w = (edit.crop[2] - edit.crop[0]) if edit.crop else full_w
        crop_h = (edit.crop[3] - edit.crop[1]) if edit.crop else full_h
        scale = max(crop_w, crop_h) / max_side
        if scale > 1:
            im.draft("RGB", (int(full_w / scale), int(full_h / scale)))
    im = im.convert("RGB")
    if edit.crop:
        fx, fy = im.width / full_w, im.height / full_h
        x0, y0, x1, y1 = edit.crop
        im = im.crop((int(x0 * fx), int(y0 * fy), max(int(x1 * fx), int(x0 * fx) + 1), max(int(y1 * fy), int(y0 * fy) + 1)))
    if edit.rotation in _TRANSPOSE:
        im = im.transpose(_TRANSPOSE[edit.rotation])
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return im


def save_edited(src: Path, dest: Path, edit: Edit, quality: int = 95) -> None:
    """Full-resolution render of an edited scan to JPEG, keeping the scan's dpi and ICC profile."""
    with Image.open(src) as original:
        dpi = original.info.get("dpi")
        icc = original.info.get("icc_profile")
    image = open_edited(src, edit)
    options = {"quality": quality, "subsampling": 0}
    if dpi:
        options["dpi"] = dpi
    if icc:
        options["icc_profile"] = icc
    image.save(dest, "JPEG", **options)


def analysis_gray(path: Path, edit: Edit, max_side: int) -> np.ndarray:
    return np.asarray(open_edited(path, edit, max_side).convert("L"), dtype=np.uint8)
