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


class ExifToolWriter:
    def __init__(self, executable: str = "exiftool") -> None:
        self._et = exiftool.ExifToolHelper(executable=executable, common_args=["-charset", "filename=utf8"])

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
            got = _as_list(actual.get(_READ_KEYS[tag]))
            want = _as_list(expected)
            if got != want:
                problems.append(f"{tag}: expected {want!r}, got {got!r}")
        if problems:
            raise MetadataVerificationError(f"{path}: " + "; ".join(problems))
