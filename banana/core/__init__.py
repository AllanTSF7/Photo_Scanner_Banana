"""Per-image hot paths. Uses the C++ `banana_core` extension when installed, else the NumPy reference.

Set BANANA_DISABLE_NATIVE=1 to force the reference implementation.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from banana.core import reference
from banana.core.reference import BlankMetrics, hamming

_native = None
if not os.environ.get("BANANA_DISABLE_NATIVE"):
    try:
        import banana_core as _native  # type: ignore[no-redef]
    except ImportError:
        _native = None

NATIVE_AVAILABLE = _native is not None

__all__ = ["NATIVE_AVAILABLE", "BlankMetrics", "blank_metrics", "decode_gray", "dhash", "hamming", "photo_bbox"]


def dhash(gray: np.ndarray) -> int:
    gray = np.ascontiguousarray(gray, dtype=np.uint8)
    if _native is not None:
        return int(_native.dhash(gray))
    return reference.dhash(gray)


def blank_metrics(gray: np.ndarray, edge_threshold: int = reference.DEFAULT_EDGE_THRESHOLD) -> BlankMetrics:
    gray = np.ascontiguousarray(gray, dtype=np.uint8)
    if _native is not None:
        mean, std, edge = _native.blank_metrics(gray, edge_threshold)
        return BlankMetrics(mean, std, edge)
    return reference.blank_metrics(gray, edge_threshold)


def photo_bbox(rgb: np.ndarray) -> tuple[int, int, int, int] | None:
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    if _native is not None and hasattr(_native, "photo_bbox"):
        box = _native.photo_bbox(rgb)
        return tuple(box) if box is not None else None
    return reference.photo_bbox(rgb)


def decode_gray(path: Path, max_side: int = 1024) -> np.ndarray:
    """Decode to grayscale, using DCT-domain downscaling when possible (not bit-identical across backends)."""
    if _native is not None and getattr(_native, "HAVE_OPENCV", False):
        return _native.decode_gray(str(path), max_side)
    return reference.decode_gray(path, max_side)
