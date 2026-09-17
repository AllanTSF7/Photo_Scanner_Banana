"""Autocorrect for scan text, so a recurring misspelling never has to be retyped.

Offline, deterministic, no model. Two sources, applied in order:

1. the **learned correction dictionary** (learning loop, Stage 2) — fixes the operator already approved
   on an earlier scan ("JimmyDawley" -> "Jimmy Dawley");
2. a small **vocabulary** of words that keep showing up on photo backs: months, weekdays, seasons,
   holidays and occasions. A word is replaced only when exactly one vocabulary word is a single typo
   away - one changed, added or dropped letter, or two letters swapped - so "Mary" never becomes
   "March" and an unfamiliar surname is left alone.

Safety rules, in order of how often they save us:
- words shorter than MIN_LENGTH are never touched (too many short names sit one edit from a real word);
- a word that is already in the vocabulary - including its plural or possessive, "Grandma's" - or is a
  name confirmed on another scan (`protected`), is never touched;
- an ambiguous word - two vocabulary words one edit away - is left for the operator.

Nothing here auto-approves anything: the caller shows what was changed and can undo it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from banana.core.dictionary import CorrectionDictionary

MIN_LENGTH = 5
VERSION = "autocorrect@1"

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SEASONS = ("spring", "summer", "autumn", "winter")
OCCASIONS = (
    "christmas", "easter", "thanksgiving", "halloween", "hanukkah", "passover", "birthday",
    "wedding", "anniversary", "graduation", "reunion", "funeral", "baptism", "christening",
    "communion", "confirmation", "honeymoon", "vacation", "holiday", "picnic", "parade",
    "party", "church", "school", "grandma", "grandpa", "grandmother", "grandfather",
    "mother", "father", "brother", "sister", "cousin", "uncle", "aunt", "family",
    "beach", "cabin", "kitchen", "backyard", "garden", "house", "hospital", "airport",
)
VOCABULARY = frozenset(MONTHS + WEEKDAYS + SEASONS + OCCASIONS)

# A word, keeping internal apostrophes so "Grandma's" is one token and stays intact.
_WORD = re.compile(r"[A-Za-z]+(?:['‘’][A-Za-z]+)*")


@dataclass(frozen=True)
class Fix:
    before: str
    after: str
    source: str  # "dictionary" (learned from an approval) or "spelling" (vocabulary)

    def to_dict(self) -> dict:
        return {"from": self.before, "to": self.after, "source": self.source}


def one_edit_apart(a: str, b: str) -> bool:
    """True when a and b differ by exactly one substitution, insertion or deletion."""
    if abs(len(a) - len(b)) > 1 or a == b:
        return a == b
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = sorted((a, b), key=len)
    i = next((k for k in range(len(short)) if short[k] != long_[k]), len(short))
    return short[i:] == long_[i + 1:]


def _transposed(a: str, b: str) -> bool:
    """True for a swap of two neighbouring letters ("Summre" / "summer"), the other everyday typo."""
    if len(a) != len(b):
        return False
    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]


def close_match(a: str, b: str) -> bool:
    """One typo apart: a single edit, or two neighbouring letters swapped."""
    return one_edit_apart(a, b) or _transposed(a, b)


_PLURAL = re.compile(r"(?:['‘’]s|s)$")


def _known(lower: str, vocabulary: frozenset[str]) -> bool:
    return lower in vocabulary or _PLURAL.sub("", lower) in vocabulary


def _match_case(original: str, replacement: str) -> str:
    if original.isupper() and len(original) > 1:
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _vocabulary_fix(word: str, vocabulary: frozenset[str]) -> str | None:
    lower = word.lower()
    matches = [name for name in vocabulary if close_match(lower, name)]
    return matches[0] if len(matches) == 1 else None


def correct(
    text: str,
    *,
    learned: "CorrectionDictionary | None" = None,
    protected: tuple[str, ...] | list[str] = (),
    vocabulary: frozenset[str] = VOCABULARY,
) -> tuple[str, list[Fix]]:
    """Return (corrected text, the fixes applied). Unchanged text comes back with an empty list."""
    if not text or not text.strip():
        return text, []

    from banana.core.dictionary import normalize  # imported here so date_parse can reuse close_match cheaply

    fixes: list[Fix] = []
    if learned is not None:
        mapped = learned.lines.get(normalize(text))
        if mapped is not None and mapped != text:  # the whole value is a line the operator already fixed
            return mapped, [Fix(text, mapped, "dictionary")]

    words = {wrong.lower(): right for wrong, right in (learned.words if learned else {}).items() if " " not in wrong}
    keep = {w.lower() for w in protected} | {w.lower() for name in protected for w in _WORD.findall(name)}

    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        lower = word.lower()
        if lower in keep or _PLURAL.sub("", lower) in keep:
            return word
        learned_fix = words.get(lower)
        if learned_fix is not None and learned_fix != word:
            fixes.append(Fix(word, learned_fix, "dictionary"))
            return learned_fix
        if len(word) < MIN_LENGTH or _known(lower, vocabulary):
            return word
        name = _vocabulary_fix(word, vocabulary)
        if name is None:
            return word
        fixed = _match_case(word, name)
        fixes.append(Fix(word, fixed, "spelling"))
        return fixed

    return _WORD.sub(replace, text), fixes
