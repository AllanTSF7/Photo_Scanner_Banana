"""Metadata written for Immich: dates, description and hierarchical tags as ExifTool assignments."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from banana.dates import PhotoDate

CREATOR_TOOL = "Photo Scanner"
DESCRIPTION_TAGS =("EXIF:ImageDescription", "XMP-dc:Description")
DATE_TAGS = ("EXIF:DateTimeOriginal", "EXIF:CreateDate", "XMP-exif:DateTimeOriginal", "XMP-xmp:CreateDate")


def _clean(value: str) -> str:
    # "/" and "|" are hierarchy separators in TagsList / HierarchicalSubject.
    return re.sub(r"\s+", " ", re.sub(r"[/|]", "-", value)).strip()


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for v in values:
        if v and v.casefold() not in seen:
            seen.add(v.casefold())
            out.append(v)
    return out


@dataclass
class PhotoMetadata:
    date: PhotoDate
    description: str = ""
    people: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)  # free-form, may already contain "/" hierarchy
    box_label: str | None = None
    has_back: bool = False

    def full_description(self) -> str:
        text = self.description.strip()
        if self.date.is_approximate and self.date.label() != "unknown":
            text = f"{text} (date approx: {self.date.label()})".strip()
        return text

    def hierarchical_tags(self, *, is_back: bool = False) -> list[list[str]]:
        tags: list[list[str]] = []
        tags += [["People", _clean(p)] for p in self.people]
        tags += [["Places", _clean(p)] for p in self.places]
        tags += [["Events", _clean(e)] for e in self.events]
        tags += [[_clean(part) for part in t.split("/")] for t in self.tags]
        if self.box_label:
            tags.append(["Box", _clean(self.box_label)])
        if self.date.is_approximate:
            tags.append(["Scan", "DateApprox"])
        if is_back:
            tags.append(["Scan", "Back"])
        elif self.has_back:
            tags.append(["Scan", "HasBack"])
        tags = [[p for p in t if p] for t in tags]
        unique = _unique(["/".join(t) for t in tags if t])
        return [t.split("/") for t in unique]

    def expected_values(self, *, is_back: bool = False) -> dict[str, str | list[str] | None]:
        """Tag -> value (None deletes). Keys use ExifTool write groups."""
        hierarchy = self.hierarchical_tags(is_back=is_back)
        exif_date = self.date.exif_datetime()
        # CreatorTool guarantees a non-empty XMP packet, so a sidecar can always be created.
        values: dict[str, str | list[str] | None] = {"XMP-xmp:CreatorTool": CREATOR_TOOL}
        values.update({tag: exif_date for tag in DATE_TAGS})
        description = self.full_description() or None
        values.update({tag: description for tag in DESCRIPTION_TAGS})
        values["XMP-dc:Subject"] = _unique([t[-1] for t in hierarchy])
        values["XMP-digiKam:TagsList"] = ["/".join(t) for t in hierarchy]
        values["XMP-lr:HierarchicalSubject"] = ["|".join(t) for t in hierarchy]
        return values
