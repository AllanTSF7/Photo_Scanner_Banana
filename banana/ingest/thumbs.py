"""Cached JPEG previews for the review UI (crop + rotation applied)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from banana.imaging import Edit, open_edited


def preview(src: Path, cache_dir: Path, max_side: int, edit: Edit = Edit()) -> Path:
    stat = src.stat()
    key = hashlib.sha1(
        f"{src}|{stat.st_mtime_ns}|{stat.st_size}|{max_side}|{edit.crop}|{edit.rotation}".encode()
    ).hexdigest()
    out = cache_dir / f"{key}.jpg"
    if out.exists():
        return out
    cache_dir.mkdir(parents=True, exist_ok=True)
    image = open_edited(src, edit, max_side)
    tmp = out.with_suffix(".tmp")
    image.save(tmp, "JPEG", quality=85)
    tmp.replace(out)
    return out
