"""Learning loop, Stage 2: correction dictionary (no training).

Built from correction events of approved scans only:
- OCR lines: exact line fixes (normalized suggested line -> approved line) and word fixes taken from small edits
  ("JimmyDawley" -> "Jimmy Dawley"). Applied to new OCR output before it's shown; each line records it was changed.
- Entities: values the operator removed repeatedly (and never kept) stop being suggested.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlmodel import Session

from banana.core.corrections import training_events

MIN_REMOVALS_TO_SUPPRESS = 2
_WORD = re.compile(r"\S+")


def normalize(text: str) -> str:
    """Line key: OCR of the same label varies in spacing and punctuation ("Xmas'84" / "Xmas '84"), so ignore both."""
    return re.sub(r"[\W_]+", "", (text or "").lower())


@dataclass
class CorrectionDictionary:
    lines: dict[str, str] = field(default_factory=dict)  # normalized OCR line -> approved text
    words: dict[str, str] = field(default_factory=dict)  # exact OCR token(s) -> approved token(s)
    suppressed: dict[str, set[str]] = field(default_factory=lambda: {"person": set(), "place": set(), "event": set()})

    @property
    def version(self) -> str:
        return f"dictionary@{len(self.lines)}l{len(self.words)}w"

    @property
    def size(self) -> int:
        return len(self.lines) + len(self.words) + sum(len(v) for v in self.suppressed.values())

    def correct_line(self, text: str) -> tuple[str, bool]:
        """Apply line then word fixes. Returns (text, changed)."""
        fixed = self.lines.get(normalize(text))
        if fixed is not None:
            return fixed, fixed != text
        out = text
        for wrong, right in sorted(self.words.items(), key=lambda kv: -len(kv[0])):
            out = re.sub(rf"(?<!\S){re.escape(wrong)}(?!\S)", right, out)
        return out, out != text

    def is_suppressed(self, field_name: str, value: str) -> bool:
        return value.lower() in self.suppressed.get(field_name, set())

    def to_dict(self, limit: int = 50) -> dict:
        return {
            "version": self.version,
            "lines": [{"from": k, "to": v} for k, v in list(self.lines.items())[:limit]],
            "words": [{"from": k, "to": v} for k, v in list(self.words.items())[:limit]],
            "suppressed": {k: sorted(v)[:limit] for k, v in self.suppressed.items()},
        }


def _word_fixes(suggested: str, approved: str) -> dict[str, str]:
    a, b = _WORD.findall(suggested), _WORD.findall(approved)
    fixes: dict[str, str] = {}
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "replace" and (i2 - i1) <= 2 and (j2 - j1) <= 3:
            wrong, right = " ".join(a[i1:i2]), " ".join(b[j1:j2])
            # Only near-misses (typos, joined/split words): unrelated rewrites aren't dictionary material.
            if difflib.SequenceMatcher(a=wrong.lower().replace(" ", ""), b=right.lower().replace(" ", "")).ratio() >= 0.6:
                fixes[wrong] = right
    return fixes


def build(session: Session) -> CorrectionDictionary:
    dictionary = CorrectionDictionary()
    removed: Counter = Counter()
    confirmed: Counter = Counter()
    for event in training_events(session):
        if event.field == "ocr_line" and event.action == "edited" and event.suggested and event.approved:
            raw = (event.asset_ref or {}).get("raw_ocr") or event.suggested
            dictionary.lines[normalize(raw)] = event.approved
            dictionary.words.update(_word_fixes(raw, event.approved))
        elif event.field in ("person", "place", "event"):
            value = (event.suggested or event.approved or "").lower()
            if event.action == "removed":
                removed[(event.field, value)] += 1
            elif event.action in ("kept", "added"):
                confirmed[(event.field, value)] += 1
    for (field_name, value), count in removed.items():
        if count >= MIN_REMOVALS_TO_SUPPRESS and confirmed[(field_name, value)] == 0:
            dictionary.suppressed[field_name].add(value)
    return dictionary


_cache: dict[str, tuple[tuple, CorrectionDictionary]] = {}


def cached_build(session: Session) -> CorrectionDictionary:
    """`build`, reused until its inputs change. Entity extraction runs while the operator types, and rebuilding
    from every correction event each time was the bulk of its cost. Events are append-only and only count for
    approved/exported scans, so (newest event id, scan count, newest scan update) changes whenever the result can."""
    from sqlalchemy import func
    from sqlmodel import select

    from banana.models import CorrectionEvent, Scan

    key = (
        session.exec(select(func.max(CorrectionEvent.id))).one(),
        *session.exec(select(func.count(Scan.id), func.max(Scan.updated_at))).one(),
    )
    hit = _cache.get(str(session.get_bind().url))
    if hit is not None and hit[0] == key:
        return hit[1]
    dictionary = build(session)
    _cache[str(session.get_bind().url)] = (key, dictionary)
    return dictionary
