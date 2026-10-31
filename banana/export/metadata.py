"""Metadata written for Immich: dates, description and hierarchical tags as ExifTool assignments."""

from __future__ import annotations

import re
import unicodedata
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


# ---------------------------------------------------------------- what can be written at all

FIELD_LABELS = {"description": "Description", "people": "People", "places": "Places", "events": "Events", "tags": "Tags"}

# Besides printable ASCII, the photo's metadata may carry accented Latin letters and the typographic punctuation
# phone keyboards produce. Anything else - CJK punctuation that OCR reads off a label ("、"), symbols such as
# "·" or "§", control characters - is reported with its name and a replacement instead of being written, so no
# stray character reaches the library without the operator choosing it.
_ALLOWED_PUNCTUATION = set("‘’‚“”„–—…€£°¿¡«»")
_REPLACEMENTS = {
    "、": ",", "。": ".", "・": "-", "·": "-", "•": "-", "〜": "~", "「": '"', "」": '"', "『": '"', "』": '"',
    "′": "'", "″": '"', " ": " ", "­": "", "§": "", "∈": "", "†": "", "\t": " ", "\r": "",
}
# "/" separates tag levels and "|" separates them in Lightroom's field: in a name they would be changed to "-"
# on export (_clean), so the operator is asked instead of having the name altered silently.
_SEPARATORS = {"people": "/|", "places": "/|", "events": "/|", "tags": "|"}


def _allowed(ch: str, *, multiline: bool) -> bool:
    code = ord(ch)
    if 0x20 <= code <= 0x7E:
        return True
    if ch == "\n":
        return multiline
    if 0xC0 <= code <= 0x24F:  # Latin-1 Supplement and Latin Extended-A/B: letters only (not "·", "§", "¶")
        return unicodedata.category(ch).startswith("L")
    return ch in _ALLOWED_PUNCTUATION


def _replacement(ch: str) -> str | None:
    if ch in _REPLACEMENTS:
        return _REPLACEMENTS[ch]
    folded = unicodedata.normalize("NFKC", ch)  # fullwidth "，" -> ",", "①" -> "1"
    if folded != ch and all(_allowed(c, multiline=False) for c in folded):
        return folded
    return None


_NAMES = {"\t": "tab", "\r": "carriage return", "\n": "line break"}


def _describe(ch: str, field: str) -> str:
    if ch in _SEPARATORS.get(field, ""):
        return f'"{ch}" separates tag levels: use "-" instead'
    name = _NAMES.get(ch) or (unicodedata.name(ch).lower() if unicodedata.name(ch, "") else f"U+{ord(ch):04X}")
    replacement = _replacement(ch)
    visible = unicodedata.category(ch)[0] not in "CZ"  # don't print control or space characters themselves
    label = f'"{ch}" ({name})' if visible else f"a {name} character"
    if not replacement:
        hint = "remove it"
    elif replacement == " ":
        hint = "use a normal space"
    else:
        hint = f'use "{replacement}" instead'
    return f"{label}: {hint}"


def text_problems(fields: dict[str, str | list[str] | None]) -> list[str]:
    """Why these field values can't go into a photo's metadata as they stand, one line per field, naming each
    offending character and what to type instead - e.g. 'Description: "、" (ideographic comma): use "," instead'.
    Empty when everything is writable. Keys are the scan's field names (description, people, places, events,
    tags); the description may span lines, the others may not."""
    problems: list[str] = []
    for field, value in fields.items():
        values = value if isinstance(value, list) else [value or ""]
        found: dict[str, None] = {}
        for text in values:
            for ch in text or "":
                if not _allowed(ch, multiline=field == "description") or ch in _SEPARATORS.get(field, ""):
                    found.setdefault(ch)
        if found:
            problems.append(f"{FIELD_LABELS.get(field, field)}: " + "; ".join(_describe(ch, field) for ch in found))
    return problems
