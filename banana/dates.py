"""Photo dates with explicit precision.

EXIF needs a full timestamp, but a photo back often only says "Summer '79" or "1980s".
PhotoDate keeps what is actually known and derives the placeholder timestamp used for
Immich's timeline plus a human label for the description.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from enum import Enum


class Precision(str, Enum):
    DAY = "day"
    MONTH = "month"
    SEASON = "season"
    YEAR = "year"
    DECADE = "decade"
    UNKNOWN = "unknown"


PRECISION_RANK = {
    Precision.DAY: 5,
    Precision.MONTH: 4,
    Precision.SEASON: 3,
    Precision.YEAR: 2,
    Precision.DECADE: 1,
    Precision.UNKNOWN: 0,
}

# Placeholder (month, day) used for a season; winter means January of that year.
SEASON_MIDPOINT = {"spring": (4, 15), "summer": (7, 15), "fall": (10, 15), "winter": (1, 15)}


@dataclass(frozen=True)
class PhotoDate:
    precision: Precision
    year: int | None = None
    month: int | None = None
    day: int | None = None
    season: str | None = None
    circa: bool = False

    def __post_init__(self) -> None:
        p = self.precision
        if p is Precision.UNKNOWN:
            return
        if self.year is None:
            raise ValueError(f"{p.value} precision requires a year")
        if p is Precision.DAY:
            if self.month is None or self.day is None:
                raise ValueError("day precision requires month and day")
            date(self.year, self.month, self.day)  # raises on invalid dates
        elif p is Precision.MONTH:
            if self.month is None or not 1 <= self.month <= 12:
                raise ValueError("month precision requires a valid month")
        elif p is Precision.SEASON:
            if self.season not in SEASON_MIDPOINT:
                raise ValueError(f"unknown season {self.season!r}")
        elif p is Precision.DECADE and self.year % 10:
            raise ValueError("decade precision requires a year ending in 0")

    @classmethod
    def unknown(cls) -> PhotoDate:
        return cls(Precision.UNKNOWN)

    @property
    def is_approximate(self) -> bool:
        return self.precision is not Precision.DAY or self.circa

    def exif_datetime(self) -> str | None:
        """Value for EXIF DateTimeOriginal ("YYYY:MM:DD HH:MM:SS"), or None if unknown."""
        p = self.precision
        if p is Precision.UNKNOWN:
            return None
        y = self.year
        if p is Precision.DAY:
            m, d = self.month, self.day
        elif p is Precision.MONTH:
            m, d = self.month, 1
        elif p is Precision.SEASON:
            m, d = SEASON_MIDPOINT[self.season]
        elif p is Precision.YEAR:
            m, d = 7, 1
        else:  # decade
            y, m, d = y + 5, 7, 1
        return f"{y:04d}:{m:02d}:{d:02d} 12:00:00"

    def label(self) -> str:
        p = self.precision
        if p is Precision.UNKNOWN:
            return "unknown"
        if p is Precision.DAY:
            text = f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        elif p is Precision.MONTH:
            text = f"{calendar.month_abbr[self.month]} {self.year}"
        elif p is Precision.SEASON:
            text = f"{self.season.capitalize()} {self.year}"
        elif p is Precision.YEAR:
            text = str(self.year)
        else:
            text = f"{self.year}s"
        return f"c. {text}" if self.circa else text
