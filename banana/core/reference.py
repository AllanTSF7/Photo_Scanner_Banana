"""NumPy reference implementations. native/src/*.cpp must produce identical results (see tests)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

DHASH_ROWS = 8
DHASH_COLS = 9
DEFAULT_EDGE_THRESHOLD = 24


def _bounds(size: int, parts: int) -> np.ndarray:
    return np.array([(i * size) // parts for i in range(parts + 1)], dtype=np.int64)


def dhash(gray: np.ndarray) -> int:
    """64-bit difference hash over an 8x9 box-averaged grid.

    Block means are compared by integer cross-multiplication (sum_a * count_b vs sum_b * count_a)
    so that the C++ port can match bit-for-bit without floating point.
    Bit order: row-major, most significant bit first; bit set when the right block is brighter.
    """
    h, w = gray.shape
    if h < DHASH_ROWS or w < DHASH_COLS:
        raise ValueError(f"image {w}x{h} too small for dHash")
    ys, xs = _bounds(h, DHASH_ROWS), _bounds(w, DHASH_COLS)
    sums = np.add.reduceat(np.add.reduceat(gray.astype(np.int64), ys[:-1], axis=0), xs[:-1], axis=1)
    counts = np.diff(ys)[:, None] * np.diff(xs)[None, :]
    right_brighter = sums[:, 1:] * counts[:, :-1] > sums[:, :-1] * counts[:, 1:]
    value = 0
    for bit in right_brighter.ravel():
        value = (value << 1) | int(bit)
    return value


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


@dataclass(frozen=True)
class BlankMetrics:
    mean: float
    std: float
    edge_density: float


def blank_metrics(gray: np.ndarray, edge_threshold: int = DEFAULT_EDGE_THRESHOLD) -> BlankMetrics:
    """Brightness stats plus fraction of pixels whose |dx|+|dy| exceeds edge_threshold."""
    h, w = gray.shape
    if h < 2 or w < 2:
        raise ValueError("image too small")
    n = h * w
    total = int(gray.sum(dtype=np.uint64))
    total_sq = int((gray.astype(np.uint64) ** 2).sum())
    mean = total / n
    std = max(total_sq / n - mean * mean, 0.0) ** 0.5
    g = gray.astype(np.int16)
    base = g[:-1, :-1]
    grad = np.abs(g[:-1, 1:] - base) + np.abs(g[1:, :-1] - base)
    edges = int(np.count_nonzero(grad > edge_threshold))
    return BlankMetrics(mean, std, edges / ((h - 1) * (w - 1)))


def _longest_run(mask: np.ndarray, max_gap: int) -> tuple[int, int] | None:
    """[start, end) of the longest run of True, bridging gaps of up to max_gap False values."""
    best = None
    start = None
    last_true = -10**9
    for i, value in enumerate(mask.tolist()):
        if value:
            if start is None or i - last_true - 1 > max_gap:
                start = i
            last_true = i
            if best is None or (last_true + 1 - start) > (best[1] - best[0]):
                best = (start, last_true + 1)
    return best


def photo_bbox(rgb: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding box (x0, y0, x1, y1), end-exclusive, of the photo on a sheet-fed scan.

    Tuned on the Epson FF-680W through SANE epsonds (no hardware crop): the photo lies on a
    blue-gray backing, and the page below the scanned length is padded with pure white.
    All integer arithmetic, so the C++ port can match exactly. Input: uint8 [h, w, 3], ideally ~1/8 scale.
    """
    h, w, _ = rgb.shape
    if h < 8 or w < 8:
        return None
    px = rgb.astype(np.int32)
    r, g, b = px[..., 0], px[..., 1], px[..., 2]

    white = (r >= 250) & (g >= 250) & (b >= 250)
    full_white_row = white.sum(axis=1) * 100 >= 98 * w
    scan_end = h
    while scan_end > 0 and full_white_row[scan_end - 1]:
        scan_end -= 1
    if scan_end == 0:
        return None

    total = r + g + b
    spread = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    backing = (total >= 510) & (total <= 720) & (b - r >= 4) & (b - r <= 40) & (spread <= 45)
    backing = backing[:scan_end]

    photo_rows = backing.sum(axis=1) * 100 < 85 * w
    photo_cols = backing.sum(axis=0) * 100 < 85 * scan_end
    rows = _longest_run(photo_rows, max_gap=max(1, scan_end // 33))
    cols = _longest_run(photo_cols, max_gap=max(1, w // 33))
    if rows is None or cols is None:
        return None
    y0, y1 = rows
    x0, x1 = cols
    if (y1 - y0) * 10 < scan_end // 10 or (x1 - x0) * 10 < w // 10:  # smaller than 1% of the area side: noise
        return None
    return x0, y0, x1, y1


def decode_gray(path: Path, max_side: int = 1024) -> np.ndarray:
    with Image.open(path) as img:
        img.draft("L", (max_side, max_side))  # JPEG DCT scaling; no-op for other formats
        gray = img.convert("L")
        if max(gray.size) > max_side:
            gray.thumbnail((max_side, max_side), Image.Resampling.BOX)
        return np.asarray(gray, dtype=np.uint8).copy()
