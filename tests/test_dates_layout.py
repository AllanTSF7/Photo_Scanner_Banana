from pathlib import PurePosixPath

import pytest

from banana.dates import PhotoDate, Precision
from banana.export import layout
from banana.export.metadata import PhotoMetadata

P = Precision


@pytest.mark.parametrize(
    ("d", "exif", "folder", "label"),
    [
        (PhotoDate(P.DAY, 1984, 12, 25), "1984:12:25 12:00:00", "1984/1984-12-25", "1984-12-25"),
        (PhotoDate(P.MONTH, 1984, 12), "1984:12:01 12:00:00", "1984/1984-12", "Dec 1984"),
        (PhotoDate(P.SEASON, 1979, season="summer"), "1979:07:15 12:00:00", "1979/1979-summer", "Summer 1979"),
        (PhotoDate(P.YEAR, 1950, circa=True), "1950:07:01 12:00:00", "1950/1950-undated", "c. 1950"),
        (PhotoDate(P.DECADE, 1980), "1985:07:01 12:00:00", "undated/Attic-Box-3", "1980s"),
        (PhotoDate.unknown(), None, "undated/Attic-Box-3", "unknown"),
    ],
)
def test_date_rules(d, exif, folder, label):
    assert d.exif_datetime() == exif
    assert layout.relative_dir(d, "Attic Box #3") == PurePosixPath(folder)
    assert d.label() == label


def test_invalid_dates():
    with pytest.raises(ValueError):
        PhotoDate(P.DAY, 1985, 2, 30)
    with pytest.raises(ValueError):
        PhotoDate(P.DECADE, 1984)
    with pytest.raises(ValueError):
        PhotoDate(P.SEASON, 1984, season="monsoon")


def test_filenames():
    rel = layout.relative_path(42, "A", ".JPG", PhotoDate(P.DAY, 1984, 12, 25), "b")
    assert str(rel) == "1984/1984-12-25/SCAN_000042_A.jpg"
    assert layout.sidecar_name(rel.name) == "SCAN_000042_A.jpg.xmp"


def test_metadata_values():
    meta = PhotoMetadata(
        date=PhotoDate(P.YEAR, 1984),
        description="Christmas morning at Grandma's",
        people=["Grandma", "John", "john"],
        places=["Grandma's House / Ohio"],
        events=["Christmas"],
        tags=["Family/Smiths"],
        box_label="Attic Box 3",
        has_back=True,
    )
    values = meta.expected_values()
    assert values["EXIF:DateTimeOriginal"] == "1984:07:01 12:00:00"
    assert values["XMP-dc:Description"] == "Christmas morning at Grandma's (date approx: 1984)"
    assert values["XMP-digiKam:TagsList"] == [
        "People/Grandma", "People/John", "Places/Grandma's House - Ohio", "Events/Christmas",
        "Family/Smiths", "Box/Attic Box 3", "Scan/DateApprox", "Scan/HasBack",
    ]
    assert values["XMP-lr:HierarchicalSubject"][0] == "People|Grandma"
    assert values["XMP-dc:Subject"] == [
        "Grandma", "John", "Grandma's House - Ohio", "Christmas", "Smiths", "Attic Box 3", "DateApprox", "HasBack",
    ]
    back = meta.expected_values(is_back=True)
    assert "Scan/Back" in back["XMP-digiKam:TagsList"] and "Scan/HasBack" not in back["XMP-digiKam:TagsList"]


def test_unknown_date_clears_date_tags():
    values = PhotoMetadata(date=PhotoDate.unknown()).expected_values()
    assert values["EXIF:DateTimeOriginal"] is None
    assert values["XMP-dc:Description"] is None
