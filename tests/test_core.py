import numpy as np
import pytest
from PIL import Image

from banana.core import reference

try:
    import banana_core
except ImportError:
    banana_core = None

rng = np.random.default_rng(1234)
SHAPES = [(8, 9), (37, 53), (480, 640), (1001, 733)]


def _images():
    for h, w in SHAPES:
        yield rng.integers(0, 256, size=(h, w), dtype=np.uint8)
        yield np.full((h, w), 250, dtype=np.uint8)


def test_dhash_detects_near_duplicates_not_different_photos():
    base = rng.integers(0, 256, size=(60, 80), dtype=np.uint8)
    photo = np.asarray(Image.fromarray(base).resize((1200, 900), Image.Resampling.BICUBIC))
    rescan = np.asarray(Image.fromarray(photo).resize((1000, 750), Image.Resampling.BILINEAR))
    other = np.asarray(Image.fromarray(rng.integers(0, 256, size=(60, 80), dtype=np.uint8)).resize((1200, 900)))

    h = reference.dhash(photo)
    assert reference.hamming(h, reference.dhash(rescan)) <= 6
    assert reference.hamming(h, reference.dhash(other)) > 16


def test_blank_metrics_separate_blank_from_writing():
    paper = rng.normal(235, 3, size=(400, 600)).clip(0, 255).astype(np.uint8)
    written = paper.copy()
    written[100:110, 50:550] = 40
    written[200:210, 80:500] = 40
    blank = reference.blank_metrics(paper)
    ink = reference.blank_metrics(written)
    assert blank.edge_density < 0.001
    assert ink.edge_density > blank.edge_density * 10
    assert ink.std > blank.std


@pytest.mark.skipif(banana_core is None or not banana_core.HAVE_OPENCV, reason="native decode not built")
@pytest.mark.parametrize(("suffix", "size"), [(".jpg", (3000, 2000)), (".jpg", (900, 600)), (".png", (3000, 2000))])
def test_native_decode_close_to_pillow(tmp_path, suffix, size):
    # Decoders differ slightly (DCT scaling, resampling), so compare shape and content, not bits.
    base = rng.integers(0, 256, size=(40, 60), dtype=np.uint8)
    path = tmp_path / f"scan{suffix}"
    Image.fromarray(base).resize(size, Image.Resampling.BICUBIC).save(path)
    ours = banana_core.decode_gray(str(path), 1024)
    ref = reference.decode_gray(path, 1024)
    assert ours.shape == ref.shape
    assert np.abs(ours.astype(int) - ref.astype(int)).mean() < 6


@pytest.mark.skipif(banana_core is None, reason="native banana_core not built")
@pytest.mark.parametrize("img",list(_images()), ids=lambda a: f"{a.shape[1]}x{a.shape[0]}")
def test_native_matches_reference(img):
    assert banana_core.dhash(img) == reference.dhash(img)
    mean, std, edge = banana_core.blank_metrics(img, 24)
    ref = reference.blank_metrics(img, 24)
    assert mean == pytest.approx(ref.mean, abs=1e-9)
    assert std == pytest.approx(ref.std, abs=1e-9)
    assert edge == ref.edge_density
