"""Extract dates from photo-back text ("Xmas '84", "Summer of 1979", "12/25/84", "1980s").

Rule-based and deterministic: every pattern yields a candidate with a confidence, overlapping
candidates keep the most precise one, and the caller (triage UI) always gets to confirm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from banana.analysis.autocorrect import close_match
from banana.dates import PRECISION_RANK, PhotoDate, Precision

MIN_YEAR = 1850

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MON = "(?P<mon>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_APOS = "['‘’]"
_Y4 = r"(?:18|19|20)\d{2}"
# Four-digit year, or two digits with an optional apostrophe ("84", "'84").
_Y42 = rf"(?P<y>{_Y4}|{_APOS}?\d{{2}})(?!\d)"
_ORD = r"(?:st|nd|rd|th)?"


@dataclass(frozen=True)
class DateCandidate:
    date: PhotoDate
    raw: str
    confidence: float
    span: tuple[int, int]


def _pivot(pivot: int | None) -> int:
    return date.today().year % 100 if pivot is None else pivot


def _year(token: str, pivot: int | None) -> int:
    digits = token.lstrip("'‘’")
    if len(digits) == 4:
        return int(digits)
    yy = int(digits)
    return 2000 + yy if yy <= _pivot(pivot) else 1900 + yy


def _valid_year(y: int) -> bool:
    return MIN_YEAR <= y <= date.today().year


def _easter(y: int) -> tuple[int, int]:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    n = h + l - 7 * m + 114
    return n // 31, n % 31 + 1


def _thanksgiving(y: int) -> tuple[int, int]:
    first_thursday = 1 + (3 - date(y, 11, 1).weekday()) % 7
    return 11, first_thursday + 21


def _holiday(name: str, y: int) -> tuple[int, int]:
    n = re.sub(r"\s+", " ", name.lower())
    if "eve" in n and ("christmas" in n or "mas" in n):
        return 12, 24
    if "christmas" in n or "mas" in n:
        return 12, 25
    if "new year" in n:
        return (12, 31) if "eve" in n else (1, 1)
    if "halloween" in n:
        return 10, 31
    if "valentine" in n:
        return 2, 14
    if "july" in n:
        return 7, 4
    if "easter" in n:
        return _easter(y)
    if "thanksgiving" in n:
        return _thanksgiving(y)
    raise ValueError(name)


_HOLIDAY = (
    r"(?P<h>x-?mas(?:\s+eve)?|christmas(?:\s+(?:eve|day|morning))?|new\s+year" + _APOS
    + r"?s(?:\s+(?:eve|day))?|halloween|easter|thanksgiving|(?:4th|fourth)\s+of\s+july"
    r"|valentine" + _APOS + r"?s(?:\s+day)?)"
)

_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    ("iso", re.compile(rf"(?<!\d)(?P<y>{_Y4})[-/.](?P<m>\d{{1,2}})[-/.](?P<d>\d{{1,2}})(?!\d)"), 0.95),
    ("numeric", re.compile(r"(?<!\d)(?P<a>\d{1,2})[-/.](?P<b>\d{1,2})[-/.](?P<y>\d{4}|\d{2})(?!\d)"), 0.85),
    ("mon_day_year", re.compile(rf"\b{_MON}\s+(?P<d>\d{{1,2}}){_ORD},?\s+{_Y42}", re.I), 0.9),
    ("day_mon_year", re.compile(rf"(?<!\d)(?P<d>\d{{1,2}}){_ORD}\s+(?:of\s+)?{_MON},?\s+{_Y42}", re.I), 0.9),
    ("mon_year", re.compile(rf"\b{_MON},?\s+(?:of\s+)?{_Y42}(?!(?:st|nd|rd|th)\b|,?\s+\d)", re.I), 0.85),
    ("holiday", re.compile(rf"\b{_HOLIDAY}\s*,?\s*(?:of\s+)?{_Y42}", re.I), 0.8),
    ("season", re.compile(rf"\b(?P<s>spring|summer|fall|autumn|winter)\s*,?\s*(?:of\s+)?{_Y42}", re.I), 0.75),
    ("circa", re.compile(rf"(?<!\w)(?:c\.|ca\.|circa|about|approx\.?|around|~)\s*(?P<y>{_Y4})(?!\d)", re.I), 0.7),
    ("decade", re.compile(rf"(?<![\w])(?:the\s+)?(?P<dec>(?:18|19|20)\d0|{_APOS}?\d0){_APOS}?s\b", re.I), 0.6),
    ("year", re.compile(rf"(?<![\d/.-])(?P<y>{_Y4})(?![\d/-]|{_APOS}?s\b)"), 0.6),
    ("apos_year", re.compile(rf"(?<![\w{_APOS[1:-1]}]){_APOS}(?P<y>\d{{2}})\b"), 0.5),
]


def _build(kind: str, m: re.Match[str], pivot: int | None) -> PhotoDate | None:
    g = m.groupdict()
    if kind == "iso":
        return PhotoDate(Precision.DAY, int(g["y"]), int(g["m"]), int(g["d"]))
    if kind == "numeric":
        a, b = int(g["a"]), int(g["b"])
        month, day = (b, a) if a > 12 and b <= 12 else (a, b)  # US order unless impossible
        return PhotoDate(Precision.DAY, _year(g["y"], pivot), month, day)
    if kind in ("mon_day_year", "day_mon_year"):
        return PhotoDate(Precision.DAY, _year(g["y"], pivot), _MONTHS[g["mon"].lower()], int(g["d"]))
    if kind == "mon_year":
        return PhotoDate(Precision.MONTH, _year(g["y"], pivot), _MONTHS[g["mon"].lower()])
    if kind == "holiday":
        y = _year(g["y"], pivot)
        month, day = _holiday(g["h"], y)
        return PhotoDate(Precision.DAY, y, month, day)
    if kind == "season":
        season = g["s"].lower()
        return PhotoDate(Precision.SEASON, _year(g["y"], pivot), season="fall" if season == "autumn" else season)
    if kind == "circa":
        return PhotoDate(Precision.YEAR, int(g["y"]), circa=True)
    if kind == "decade":
        return PhotoDate(Precision.DECADE, _year(g["dec"], pivot))
    if kind in ("year", "apos_year"):
        return PhotoDate(Precision.YEAR, _year(g["y"], pivot))
    raise ValueError(kind)


_FUZZY_MONTHS = [name for name in _MONTHS if len(name) >= 7]  # january, february, september, october, ...


def _fix_month_typos(text: str) -> str:
    """Labels and OCR misspell long month names ("Januaru", "Febuary", "Januray"); replace words one typo away."""
    def replace(m: re.Match[str]) -> str:
        word = m.group(0)
        if word.lower() in _MONTHS:
            return word
        return next((name for name in _FUZZY_MONTHS if close_match(word.lower(), name)), word)

    return re.sub(r"[A-Za-z]{6,10}", replace, text)


def find_dates(text: str, *, two_digit_year_pivot: int | None = None) -> list[DateCandidate]:
    """All non-overlapping date candidates, best first. Spans refer to the typo-corrected text."""
    found: list[DateCandidate] = []
    text = _fix_month_typos(text)
    for kind, pattern, confidence in _PATTERNS:
        for m in pattern.finditer(text):
            try:
                d = _build(kind, m, two_digit_year_pivot)
            except ValueError:
                continue
            if d is None or not _valid_year(d.year):
                continue
            found.append(DateCandidate(d, m.group(0), confidence, m.span()))

    found.sort(key=lambda c: (PRECISION_RANK[c.date.precision], c.confidence, c.span[1] - c.span[0]), reverse=True)
    accepted: list[DateCandidate] = []
    for c in found:
        if all(c.span[1] <= a.span[0] or c.span[0] >= a.span[1] for a in accepted):
            accepted.append(c)
    return accepted


def parse_best(text: str, *, two_digit_year_pivot: int | None = None) -> DateCandidate | None:
    candidates = find_dates(text, two_digit_year_pivot=two_digit_year_pivot)
    return candidates[0] if candidates else None
