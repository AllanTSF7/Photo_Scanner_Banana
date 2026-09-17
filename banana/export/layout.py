"""Where an exported scan lives inside the Immich library root."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

from banana.dates import PhotoDate, Precision

STAGING_DIR = ".staging"  # add `**/.staging/**` to the Immich library exclusion patterns

Side = Literal["A", "B"]


def slug(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip("-.")
    return cleaned or "batch"


def relative_dir(photo_date: PhotoDate, batch_name: str) -> PurePosixPath:
    p, y = photo_date.precision, photo_date.year
    if p is Precision.DAY:
        return PurePosixPath(f"{y:04d}", f"{y:04d}-{photo_date.month:02d}-{photo_date.day:02d}")
    if p is Precision.MONTH:
        return PurePosixPath(f"{y:04d}", f"{y:04d}-{photo_date.month:02d}")
    if p is Precision.SEASON:
        return PurePosixPath(f"{y:04d}", f"{y:04d}-{photo_date.season}")
    if p is Precision.YEAR:
        return PurePosixPath(f"{y:04d}", f"{y:04d}-undated")
    return PurePosixPath("undated", slug(batch_name))


def scan_filename(scan_id: int, side: Side, suffix: str) -> str:
    return f"SCAN_{scan_id:06d}_{side}{suffix.lower()}"


def relative_path(scan_id: int, side: Side, suffix: str, photo_date: PhotoDate, batch_name: str) -> PurePosixPath:
    return relative_dir(photo_date, batch_name) / scan_filename(scan_id, side, suffix)


def sidecar_name(image_name: str) -> str:
    return f"{image_name}.xmp"
