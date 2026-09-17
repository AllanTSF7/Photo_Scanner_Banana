import pytest

from banana.analysis.date_parse import find_dates, parse_best
from banana.dates import PhotoDate, Precision

P = Precision
PIVOT = 26


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1984-12-25", PhotoDate(P.DAY, 1984, 12, 25)),
        ("12/25/84", PhotoDate(P.DAY, 1984, 12, 25)),
        ("25.12.1984", PhotoDate(P.DAY, 1984, 12, 25)),
        ("Dec 25, 1984", PhotoDate(P.DAY, 1984, 12, 25)),
        ("December 25th 1984", PhotoDate(P.DAY, 1984, 12, 25)),
        ("25th of December, 1984", PhotoDate(P.DAY, 1984, 12, 25)),
        ("Xmas '84", PhotoDate(P.DAY, 1984, 12, 25)),
        ("Christmas Eve 1990", PhotoDate(P.DAY, 1990, 12, 24)),
        ("New Year's Eve 1999", PhotoDate(P.DAY, 1999, 12, 31)),
        ("Easter 1985", PhotoDate(P.DAY, 1985, 4, 7)),
        ("Thanksgiving 1983", PhotoDate(P.DAY, 1983, 11, 24)),
        ("4th of July 1976", PhotoDate(P.DAY, 1976, 7, 4)),
        ("Dec 84", PhotoDate(P.MONTH, 1984, 12)),
        ("Dec. '84", PhotoDate(P.MONTH, 1984, 12)),
        ("June 1979", PhotoDate(P.MONTH, 1979, 6)),
        ("Summer of '79", PhotoDate(P.SEASON, 1979, season="summer")),
        ("Autumn 1988", PhotoDate(P.SEASON, 1988, season="fall")),
        ("circa 1950", PhotoDate(P.YEAR, 1950, circa=True)),
        ("~1962", PhotoDate(P.YEAR, 1962, circa=True)),
        ("Grandma's house 1984", PhotoDate(P.YEAR, 1984)),
        ("'84", PhotoDate(P.YEAR, 1984)),
        ("the 1980s", PhotoDate(P.DECADE, 1980)),
        ("late 70's", PhotoDate(P.DECADE, 1970)),
        ("John & Mary at the lake, July 4 1976", PhotoDate(P.DAY, 1976, 7, 4)),
        ("Januaru 12, 2006", PhotoDate(P.DAY, 2006, 1, 12)),  # real label typo read by OCR
        ("Febuary 3 1990", PhotoDate(P.DAY, 1990, 2, 3)),
        ("Chocolat Design - Field Trip / Januaru 12, 2006", PhotoDate(P.DAY, 2006, 1, 12)),
    ],
)
def test_parse_best(text, expected):
    best = parse_best(text, two_digit_year_pivot=PIVOT)
    assert best is not None, text
    assert best.date == expected


@pytest.mark.parametrize("text", ["", "Grandma and John", "Kodak paper 4521 8874", "Room 12", "3/45/12", "Marcy and Junie"])
def test_no_date(text):
    assert parse_best(text, two_digit_year_pivot=PIVOT) is None


def test_two_digit_pivot():
    assert parse_best("'24", two_digit_year_pivot=26).date.year == 2024
    assert parse_best("'27", two_digit_year_pivot=26).date.year == 1927


def test_overlapping_candidates_keep_most_precise():
    candidates = find_dates("Dec 25, 1984 and later 1990", two_digit_year_pivot=PIVOT)
    assert [c.date for c in candidates] == [PhotoDate(P.DAY, 1984, 12, 25), PhotoDate(P.YEAR, 1990)]


def test_future_year_rejected():
    assert parse_best("2999") is None
