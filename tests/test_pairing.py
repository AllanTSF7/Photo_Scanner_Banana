from pathlib import Path

from banana.config import DEFAULT_PAIRING_PATTERN
from banana.ingest.pairing import pair_files


def test_fastfoto_naming():
    names = [
        "Box3_0001.jpg", "Box3_0001_a.jpg", "Box3_0001_b.jpg",
        "Box3_0002.jpg", "Box3_0002_a.jpg",
        "Box3_0003_B.JPG",
        "notes.txt", "Thumbs.db",
    ]
    result = pair_files([Path("/in", n) for n in names], DEFAULT_PAIRING_PATTERN)

    by_base = {g.base: g for g in result.groups}
    assert set(by_base) == {"Box3_0001", "Box3_0002"}
    first = by_base["Box3_0001"]
    assert first.front("original").name == "Box3_0001.jpg"
    assert first.front("enhanced").name == "Box3_0001_a.jpg"
    assert first.back.name == "Box3_0001_b.jpg"
    assert by_base["Box3_0002"].back is None
    assert [g.base for g in result.backs_without_front] == ["Box3_0003"]
    assert {p.name for p in result.unmatched} == {"notes.txt", "Thumbs.db"}


def test_enhanced_only_falls_back():
    result = pair_files([Path("x_0007_a.jpg")], DEFAULT_PAIRING_PATTERN)
    assert result.groups[0].front("original").name == "x_0007_a.jpg"
