"""Write and read back metadata with a long-lived ExifTool process (the same parser Immich uses)."""

from __future__ import annotations

import html
from pathlib import Path

import exiftool

from banana.export.metadata import PhotoMetadata

# Readback keys (-G1 family names) for each written tag.
_READ_KEYS = {
    "XMP-xmp:CreatorTool": "XMP-xmp:CreatorTool",
    "EXIF:DateTimeOriginal": "ExifIFD:DateTimeOriginal",
    "EXIF:CreateDate": "ExifIFD:CreateDate",
    "EXIF:ImageDescription": "IFD0:ImageDescription",
    "XMP-exif:DateTimeOriginal": "XMP-exif:DateTimeOriginal",
    "XMP-xmp:CreateDate": "XMP-xmp:CreateDate",
    "XMP-dc:Description": "XMP-dc:Description",
    "XMP-dc:Subject": "XMP-dc:Subject",
    "XMP-digiKam:TagsList": "XMP-digiKam:TagsList",
    "XMP-lr:HierarchicalSubject": "XMP-lr:HierarchicalSubject",
}


# What each written tag is called when a verification problem is reported to the operator.
_FIELD_OF_TAG = {
    "XMP-xmp:CreatorTool": "creator tool",
    "EXIF:DateTimeOriginal": "date", "EXIF:CreateDate": "date", "XMP-exif:DateTimeOriginal": "date",
    "XMP-xmp:CreateDate": "date",
    "EXIF:ImageDescription": "description", "XMP-dc:Description": "description",
    "XMP-dc:Subject": "tags", "XMP-digiKam:TagsList": "tags", "XMP-lr:HierarchicalSubject": "tags",
}


class MetadataVerificationError(RuntimeError):
    pass


def _escape(value: str) -> str:
    # With -E, values are HTML-unescaped by ExifTool; this keeps newlines out of the -stay_open arg stream.
    return html.escape(value, quote=False).replace("\n", "&#xa;")


def assignment_args(values: dict[str, str | list[str] | None]) -> list[str]:
    args: list[str] = []
    for tag, value in values.items():
        if value is None or value == []:
            args.append(f"-{tag}=")
        elif isinstance(value, list):
            # Repeated "=" assignments in one command replace the whole list.
            args += [f"-{tag}={_escape(v)}" for v in value]
        else:
            args.append(f"-{tag}={_escape(value)}")
    return args


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _read_list(value: object) -> list[object]:
    """What ExifTool read back, untyped: a list tag with one entry comes back as a scalar."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _same(want: str, got: object) -> bool:
    """ExifTool's JSON output types values: a tag "1.50" reads back as 1.5, "007" as 7 and "true" as True. The
    file holds the text that was written - that is not a failed write, so compare by value, not by spelling."""
    if isinstance(got, bool):
        return want.strip().lower() == str(got).lower()
    if isinstance(got, (int, float)):
        try:
            return float(want) == float(got)
        except ValueError:
            return False
    return str(got) == want


class ExifToolWriter:
    def __init__(self, executable: str = "exiftool") -> None:
        # encoding="utf-8": ExifTool reads its arguments as UTF-8, but pyexiftool defaults to the locale encoding.
        # On Windows that's cp1252: "·" and "—" came back as "?" (verification failed) and "∈" or CJK text from
        # OCR crashed the write, so those scans could never be exported (B-43).
        self._et = exiftool.ExifToolHelper(
            executable=executable, common_args=["-charset", "filename=utf8"], encoding="utf-8",
        )

    def __enter__(self) -> ExifToolWriter:
        self._et.run()
        return self

    def __exit__(self, *exc: object) -> None:
        self._et.terminate()

    @property
    def version(self) -> str:
        return self._et.version

    def write_image(self, image: Path, meta: PhotoMetadata, *, is_back: bool) -> None:
        args = ["-E", "-overwrite_original", "-P", *assignment_args(meta.expected_values(is_back=is_back)), str(image)]
        self._et.execute(*args)

    def write_sidecar(self, image: Path, sidecar: Path) -> None:
        """Create `sidecar` from the XMP already written into `image`."""
        sidecar.unlink(missing_ok=True)
        self._et.execute("-tagsfromfile", str(image), "-XMP:all", str(sidecar))

    def read(self, path: Path) -> dict[str, object]:
        return self._et.get_metadata(str(path), params=["-G1"])[0]

    def verify(self, path: Path, meta: PhotoMetadata, *, is_back: bool, xmp_only: bool) -> None:
        actual = self.read(path)
        problems = []
        for tag, expected in meta.expected_values(is_back=is_back).items():
            if xmp_only and not tag.startswith("XMP-"):
                continue
            got = _read_list(actual.get(_READ_KEYS[tag]))
            want = _as_list(expected)
            if len(got) != len(want) or not all(_same(w, g) for w, g in zip(want, got)):
                shown = [str(g) for g in got]
                problems.append(f"{_FIELD_OF_TAG.get(tag, tag)} ({tag}): expected {want!r}, read back {shown!r}")
        if problems:
            raise MetadataVerificationError(f"{path.name}: " + "; ".join(problems))
