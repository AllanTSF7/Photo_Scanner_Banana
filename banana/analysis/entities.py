"""Derive People / Places / Events from a photo description (offline).

spaCy `en_core_web_sm` NER when installed ("ner" extra), plus rules that cover what it misses on photo captions:
OCR-joined words ("JimmyDawley"), photo-lab stamps ("ORIGINAL"), event words ("Field Trip", "birthday"),
family words ("Grandma", "Uncle Bob"), and names already confirmed on other scans.
"""

from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass, field

NOISE = {
    "original", "copy", "proof", "kodak", "fuji", "fujifilm", "agfa", "konica", "polaroid", "ilford", "paper",
    "print", "prints", "photo", "photos", "lab", "negative", "reprint", "do not bend", "made in usa",
}
KIN = r"(?:Grandma|Grandpa|Grandmother|Grandfather|Granny|Nana|Papa|Mom|Mother|Dad|Father|Mommy|Daddy)"
KIN_WITH_NAME = r"(?:Uncle|Aunt|Auntie|Cousin|Grandma|Grandpa|Great\s+Grandma|Great\s+Grandpa)\s+[A-Z][a-z]+"
EVENT_WORDS = [
    "field trip", "baby shower", "bridal shower", "bar mitzvah", "bat mitzvah", "first communion", "new year's eve",
    "new year", "fourth of july", "4th of july", "family reunion", "birthday", "wedding", "anniversary", "graduation",
    "christmas", "xmas", "thanksgiving", "easter", "halloween", "hanukkah", "vacation", "holiday", "reunion", "party",
    "picnic", "baptism", "christening", "communion", "confirmation", "funeral", "prom", "recital", "retirement",
    "camping", "camp", "trip", "cruise", "concert", "parade", "homecoming", "valentine's day",
]
_EVENT_RE = re.compile(
    r"(?:\b(\d{1,3})(?:st|nd|rd|th)\s+)?\b(" + "|".join(re.escape(w) for w in EVENT_WORDS) + r")\b", re.IGNORECASE
)
_CAMEL = re.compile(r"\b([A-Z][a-z]{2,})([A-Z][a-z]{2,})\b")  # JimmyDawley -> Jimmy Dawley (not McDonald)


@dataclass
class Entities:
    people: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_NAME_PREFIXES = {"mac", "van", "von", "del", "della", "des", "dela", "san", "fitz"}  # MacArthur, VanHorn, FitzGerald


def split_joined_words(text: str) -> str:
    """Split words OCR glued together ("JimmyDawley"), but not surnames like MacArthur or McDonald."""
    return _CAMEL.sub(lambda m: m.group(0) if m.group(1).lower() in _NAME_PREFIXES else f"{m.group(1)} {m.group(2)}", text)


def _clean(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" .,;:-–—'\"")
    value = re.sub(r"['’]s$", "", value)
    if value.isupper() and len(value) > 3:
        value = value.title()
    return value


def _add(target: list[str], value: str) -> None:
    value = _clean(value)
    if len(value) < 2 or value.lower() in NOISE or not re.search(r"[A-Za-z]", value):
        return
    if value.lower() not in {v.lower() for v in target}:
        target.append(value)


class _Nlp:
    def __init__(self) -> None:
        self._nlp = None
        self._error: str | None = None
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self._nlp is None and self._error is None:
                try:
                    import spacy

                    self._nlp = spacy.load("en_core_web_sm", exclude=["lemmatizer"])
                except Exception as exc:  # noqa: BLE001 - rules-only fallback, reported by health check
                    self._error = f"{type(exc).__name__}: {exc}"
            return self._nlp

    @property
    def error(self) -> str | None:
        return self._error


NLP = _Nlp()


def engine_name() -> str:
    return "spacy-en_core_web_sm + rules" if NLP.get() is not None else "rules only"


def extract(text: str, known: Entities | None = None) -> Entities:
    """People, places and events mentioned in `text`, in order of appearance."""
    result = Entities()
    if not text or not text.strip():
        return result
    normalized = split_joined_words(text)
    sentences = ". ".join(part.strip() for part in normalized.splitlines() if part.strip())

    # Names already used on other scans are trusted first (the family's own vocabulary).
    if known is not None:
        for field_name in ("people", "places", "events"):
            for value in getattr(known, field_name):
                if value and re.search(rf"(?<!\w){re.escape(value)}(?!\w)", normalized, re.IGNORECASE):
                    _add(getattr(result, field_name), value)

    for m in re.finditer(KIN_WITH_NAME, sentences):
        _add(result.people, m.group(0))
    for m in re.finditer(rf"\b{KIN}\b", sentences):
        if not any(m.group(0) in p for p in result.people):
            _add(result.people, m.group(0))

    nlp = NLP.get()
    if nlp is not None:
        taken = [p.lower() for p in result.people + result.places + result.events]
        for ent in nlp(sentences).ents:
            value = ent.text
            if any(value.lower() in t or t in value.lower() for t in taken):
                continue
            if ent.label_ == "PERSON":
                if not _EVENT_RE.search(value) and not _looks_like_date(value):
                    _add(result.people, value)
            elif ent.label_ in ("GPE", "LOC", "FAC", "ORG"):
                # ORG on photo captions is almost always a venue/business ("Chocolat Design").
                # "Chocolat Design - Field Trip": keep the part before the dash, the rest is the event.
                value = re.split(r"\s[-–—]\s", value)[0]
                value = _EVENT_RE.sub("", value).strip(" -–—")
                _add(result.places, value)
            elif ent.label_ == "EVENT":
                _add(result.events, value)

    for m in _EVENT_RE.finditer(sentences):
        ordinal, word = m.group(1), m.group(2)
        label = word.title().replace("'S", "'s")
        if label.lower() == "xmas":
            label = "Christmas"
        _add(result.events, f"{ordinal}{_ordinal_suffix(ordinal)} {label}" if ordinal else label)

    # A value can't be both a person and a place; people win for family words, places otherwise.
    result.places = [p for p in result.places if p.lower() not in {x.lower() for x in result.people}]
    result.events = [e for e in result.events if e.lower() not in {x.lower() for x in result.places}]
    return result


# Exact month names/abbreviations only ("Mary" must not look like "Mar"). "May" is left out: it's also a name.
_MONTH_WORDS = re.compile(
    r"^(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|june?|july?|aug(ust)?|sept?(ember)?|oct(ober)?|nov(ember)?"
    r"|dec(ember)?|spring|summer|fall|autumn|winter)\.?$",
    re.I,
)


def _looks_like_date(value: str) -> bool:
    return bool(re.search(r"\d", value)) or bool(_MONTH_WORDS.match(value.strip()))


def _ordinal_suffix(number: str) -> str:
    n = int(number)
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
