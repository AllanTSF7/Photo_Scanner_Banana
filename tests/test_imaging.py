"""Photo detection and non-destructive edits, on synthetic pages shaped like FF-680W SANE output:
a blue-gray backing band at the top holding the photo, pure white padding below."""

import sqlite3

import numpy as np
import pytest
from PIL import Image

from banana import core, db, imaging
from banana.core import reference

try:
    import banana_core
except ImportError:
    banana_core = None

W, H = 637, 1161  # 1/8 of 5096 x 9283
BACKING_END = 330


def synthetic_page(photo_box=(95, 1, 547, 303), kind="picture", seed=0, backing_end=BACKING_END) -> np.ndarray:
    rng = np.random.default_rng(seed)
    page = np.full((H, W, 3), 255, np.uint8)
    rows = np.arange(backing_end)[:, None]
    page[:backing_end, :, 0] = 196 + rows * 20 // backing_end  # slight gradient down the page, like the real scans
    page[:backing_end, :, 1] = 203 + rows * 18 // backing_end
    page[:backing_end, :, 2] = 213 + rows * 16 // backing_end
    x0, y0, x1, y1 = photo_box
    if kind == "picture":
        page[y0:y1, x0:x1] = rng.integers(0, 256, size=(y1 - y0, x1 - x0, 3), dtype=np.uint8)
    else:  # cream photo back with a printed label
        page[y0:y1, x0:x1] = (252, 247, 234)
        page[y0 + 40:y0 + 60, x0 + 200:x0 + 380] = (40, 40, 40)
    return page


@pytest.mark.parametrize("kind", ["picture", "back"])
@pytest.mark.parametrize("box", [(95, 1, 547, 303), (0, 10, 452, 312), (300, 40, 637, 290), (150, 5, 400, 200)])
def test_photo_bbox_finds_photo(kind, box):
    found = reference.photo_bbox(synthetic_page(box, kind))
    assert found is not None
    assert all(abs(a - b) <= 2 for a, b in zip(found, box)), (found, box)


def test_photo_bbox_empty_backing_and_blank_page():
    empty = synthetic_page()
    empty[:BACKING_END] = (200, 206, 216)
    assert reference.photo_bbox(empty) is None
    assert reference.photo_bbox(np.full((H, W, 3), 255, np.uint8)) is None


@pytest.mark.skipif(banana_core is None or not hasattr(banana_core, "photo_bbox"), reason="native photo_bbox not built")
@pytest.mark.parametrize("seed", range(6))
def test_native_photo_bbox_matches_reference(seed):
    rng = np.random.default_rng(seed)
    x0, y0 = int(rng.integers(0, 200)), int(rng.integers(0, 60))
    page = synthetic_page((x0, y0, x0 + 420, y0 + 260), "picture" if seed % 2 else "back", seed)
    native = banana_core.photo_bbox(page)
    assert (tuple(native) if native is not None else None) == reference.photo_bbox(page)
    noise = rng.integers(0, 256, size=(97, 131, 3), dtype=np.uint8)
    native = banana_core.photo_bbox(noise)
    assert (tuple(native) if native is not None else None) == reference.photo_bbox(noise)


def _save_page(tmp_path, scale=4, **kwargs):
    page = Image.fromarray(synthetic_page(**kwargs)).resize((W * scale, H * scale), Image.Resampling.NEAREST)
    path = tmp_path / "page.jpg"
    page.save(path, quality=95, dpi=(600, 600))
    return path


def test_detect_crop_full_resolution(tmp_path):
    path = _save_page(tmp_path, photo_box=(95, 1, 547, 303))
    crop = imaging.detect_crop(path)
    x0, y0, x1, y1 = crop
    # 1/8 detection on a 4x page -> coordinates scaled back up, inset by one detection pixel
    assert abs(x0 - 96 * 4) <= 8 and abs(x1 - 546 * 4) <= 8
    assert abs(y1 - 302 * 4) <= 8
    assert 1700 <= x1 - x0 <= 1820


def test_no_crop_when_photo_fills_page(tmp_path):
    path = tmp_path / "full.jpg"
    Image.fromarray(np.random.default_rng(1).integers(0, 256, (600, 900, 3), dtype=np.uint8)).save(path)
    assert imaging.detect_crop(path) is None


@pytest.mark.parametrize("rotation,expected", [(0, (400, 200)), (90, (200, 400)), (180, (400, 200)), (270, (200, 400))])
def test_open_edited_crop_and_rotation(tmp_path, rotation, expected):
    arr = np.zeros((600, 1000, 3), np.uint8)
    arr[100:300, 300:700] = (255, 0, 0)
    arr[100:120, 300:320] = (0, 255, 0)  # marker at the crop's top-left
    path = tmp_path / "x.png"
    Image.fromarray(arr).save(path)
    image = imaging.open_edited(path, imaging.Edit((300, 100, 700, 300), rotation))
    assert image.size == expected
    corner = {0: (5, 5), 90: (expected[0] - 6, 5), 180: (expected[0] - 6, expected[1] - 6), 270: (5, expected[1] - 6)}
    assert image.getpixel(corner[rotation])[1] > 200  # green marker followed the rotation (clockwise)


def test_save_edited_keeps_dpi(tmp_path):
    src = tmp_path / "s.jpg"
    Image.new("RGB", (800, 600), (10, 20, 30)).save(src, dpi=(600, 600))
    dest = tmp_path / "d.jpg"
    imaging.save_edited(src, dest, imaging.Edit((100, 100, 500, 400), 90))
    with Image.open(dest) as out:
        assert out.size == (300, 400)
        assert round(out.info["dpi"][0]) == 600


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE scan (id INTEGER PRIMARY KEY, batch_id INTEGER, source_key TEXT, front_path TEXT)")
    con.commit()
    con.close()
    db.make_engine(path)
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(scan)")}
    assert {"front_crop", "back_crop", "front_rotation", "back_rotation", "keep_back"} <= columns


def test_dispatcher_uses_reference_or_native():
    page = synthetic_page()
    assert core.photo_bbox(page) == reference.photo_bbox(page)
